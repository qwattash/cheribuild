#
# Copyright (c) 2016 Alex Richardson
# All rights reserved.
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory under DARPA/AFRL contract FA8750-10-C-0237
# ("CTSRD"), as part of the DARPA CRASH research programme.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
import io
import inspect
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import errno
import sys
from collections import OrderedDict
from enum import Enum
from pathlib import Path
from copy import deepcopy

from ..config.loader import ConfigLoaderBase, ComputedDefaultValue
from ..config.chericonfig import CheriConfig, CrossCompileTarget
from ..targets import Target, MultiArchTarget, targetManager
from ..filesystemutils import FileSystemUtils
from ..utils import *

__all__ = ["Project", "CMakeProject", "AutotoolsProject", "TargetAlias", "TargetAliasWithDependencies", # no-combine
           "SimpleProject", "CheriConfig", "flushStdio", "MakeOptions", "MakeCommandKind"]  # no-combine


def flushStdio(stream):
    while True:
        try:
            # can lead to EWOULDBLOCK if stream cannot be flushed immediately
            stream.flush()
            break
        except BlockingIOError as e:
            if e.errno != errno.EWOULDBLOCK:
                raise
            else:
                time.sleep(0.1)


class ProjectSubclassDefinitionHook(type):
    def __init__(cls, name: str, bases, clsdict):
        super().__init__(name, bases, clsdict)
        if clsdict.get("doNotAddToTargets"):
            return  # if doNotAddToTargets is defined within the class we skip it

        projectName = None
        if "projectName" in clsdict:
            projectName = clsdict["projectName"]
        else:
            # fall back to name of target then infer from class name
            # if targetName:
            #     projectName = targetName
            if name.startswith("Build"):
                projectName = name[len("Build"):].replace("_", "-")
            cls.projectName = projectName

        # load "target" field first then check project name (as that might default to target)
        targetName = None
        if "target" in clsdict:
            targetName = clsdict["target"]
        elif projectName:
            targetName = projectName.lower()
            cls.target = targetName

        if not targetName:
            sys.exit("target name is not set and cannot infer from class " + name +
                     " -- set projectName=, target= or doNotAddToTargets=True")

        if cls.__dict__.get("dependenciesMustBeBuilt"):
            if not cls.dependencies:
                sys.exit("PseudoTarget with no dependencies should not exist!! Target name = " + targetName)
        deps = cls.dependencies
        if deps and callable(deps):
            deps = deps(cls)
        if hasattr(cls, "supported_architectures"):
            # Add a the target for the default architecture
            targetManager.addTarget(MultiArchTarget(targetName, cls, None, dependencies=set(deps)))
            for arch in cls.supported_architectures:
                assert isinstance(arch, CrossCompileTarget)
                # create a new class to ensure different build dirs and config name strings
                new_name = targetName + "-" + arch.value
                new_dict = cls.__dict__.copy()
                new_dict["crossCompileTarget"] = arch
                new_dict["doNotAddToTargets"] = True  # We are already adding it here
                new_dict["target"] = new_name
                new_dict["synthetic"] = True  # We are already adding it here
                new_type = type(cls.__name__ + "_" + arch.name, (cls,) + cls.__bases__, new_dict)
                targetManager.addTarget(MultiArchTarget(new_name, new_type, arch, dependencies=set(deps)))
        else:
            # Only one target is supported:
            targetManager.addTarget(Target(targetName, cls, dependencies=set(deps)))
        # print("Adding target", targetName, "with deps:", cls.dependencies)


class SimpleProject(FileSystemUtils, metaclass=ProjectSubclassDefinitionHook):
    _configLoader = None  # type: ConfigLoaderBase

    # These two class variables can be defined in subclasses to customize dependency ordering of targets
    target = ""  # type: str
    projectName = None
    dependencies = []  # type: typing.List[str]
    dependenciesMustBeBuilt = False
    isAlias = False
    sourceDir = None
    buildDir = None
    installDir = None

    @classmethod
    def allDependencyNames(cls) -> list:
        dependencies = cls.dependencies
        result = []
        if callable(dependencies):
            dependencies = dependencies(cls)
        for dep in dependencies:
            if callable(dep):
                dep = dep(cls)
            if dep not in result:
                result.append(dep)
            # now recursively add the other deps:
            recursive_deps = targetManager.targetMap[dep].projectClass.allDependencyNames()
            for r in recursive_deps:
                if r not in result:
                    result.append(r)
        return result

    # Project subclasses will automatically have a target based on their name generated unless they add this:
    doNotAddToTargets = True

    # ANSI escape sequence \e[2k clears the whole line, \r resets to beginning of line
    _clearLineSequence = b"\x1b[2K\r"

    _cmakeInstallInstructions = ("Use your package manager to install CMake > 3.4 or run "
                                "`cheribuild.py cmake` to install the latest version locally")
    __commandLineOptionGroup = None

    @classmethod
    def addConfigOption(cls, name: str, default: "typing.Union[Type_T, typing.Callable[[], Type_T]]" = None,
                        kind: "typing.Callable[[str], Type_T]" = str, *,
                        showHelp=False, shortname=None, **kwargs) -> "Type_T":
        configOptionKey = cls.target
        # if cls.target != cls.projectName.lower():
        #    fatalError("Target name does not match project name:", cls.target, "vs", cls.projectName.lower())

        # Hide stuff like --foo/install-directory from --help
        helpHidden = not showHelp

        # check that the group was defined in the current class not a superclass
        if "_commandLineOptionGroup" not in cls.__dict__:
            # noinspection PyProtectedMember
            # has to be a single underscore otherwise the name gets mangled to _Foo__commandlineOptionGroup
            cls._commandLineOptionGroup = cls._configLoader._parser.add_argument_group(
                "Options for target '" + cls.target + "'")

        return cls._configLoader.addOption(configOptionKey + "/" + name, shortname, default=default, type=kind,
                                           _owningClass=cls, group=cls._commandLineOptionGroup, helpHidden=helpHidden,
                                           **kwargs)

    @classmethod
    def addBoolOption(cls, name: str, *, shortname=None, default=False, **kwargs):
        return cls.addConfigOption(name, default=default, kind=bool, shortname=shortname, action="store_true", **kwargs)

    @classmethod
    def addPathOption(cls, name: str, *, shortname=None, **kwargs):
        return cls.addConfigOption(name, kind=Path, shortname=shortname, **kwargs)

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        pass

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.__requiredSystemTools = {}  # type: typing.Dict[str, typing.Any]
        self._systemDepsChecked = False

    def _addRequiredSystemTool(self, executable: str, installInstructions=None, homebrewPackage=None):
        if IS_MAC and not installInstructions:
            if not homebrewPackage:
                homebrewPackage = executable
            self.__requiredSystemTools[executable] = "Run `brew install " + homebrewPackage + "`"
        else:
            self.__requiredSystemTools[executable] = installInstructions

    def queryYesNo(self, message: str = "", *, defaultResult=False, forceResult=True) -> bool:
        yesNoStr = " [Y]/n " if defaultResult else " y/[N] "
        if self.config.pretend:
            print(message + yesNoStr)
            return forceResult  # in pretend mode we always return true
        if self.config.force:
            # in force mode we always return the forced result without prompting the user
            print(message + yesNoStr, "y" if forceResult else "n")
            return forceResult
        if not sys.__stdin__.isatty():
            return defaultResult  # can't get any input -> return the default
        result = input(message + yesNoStr)
        if defaultResult:
            return not result.startswith("n")  # if default is yes accept anything other than strings starting with "n"
        return str(result).lower().startswith("y")  # anything but y will be treated as false

    @staticmethod
    def _handleStdErr(outfile, stream, fileLock, project: "Project"):
        for errLine in stream:
            with fileLock:
                try:
                    # noinspection PyProtectedMember
                    if project._lastStdoutLineCanBeOverwritten:
                        sys.stdout.buffer.write(b"\n")
                        flushStdio(sys.stdout)
                        project._lastStdoutLineCanBeOverwritten = False
                    sys.stderr.buffer.write(errLine)
                    flushStdio(sys.stderr)
                    if not project.config.noLogfile:
                        outfile.write(errLine)
                except ValueError:
                    # Don't print a backtrace on ctrl+C (since that will exit the main thread and close the file)
                    # ValueError: write to closed file
                    continue


    def _lineNotImportantStdoutFilter(self, line: bytes):
        # by default we don't keep any line persistent, just have updating output
        if self._lastStdoutLineCanBeOverwritten:
            sys.stdout.buffer.write(Project._clearLineSequence)
        sys.stdout.buffer.write(line[:-1])  # remove the newline at the end
        sys.stdout.buffer.write(b" ")  # add a space so that there is a gap before error messages
        flushStdio(sys.stdout)
        self._lastStdoutLineCanBeOverwritten = True

    def _showLineStdoutFilter(self, line: bytes):
        if self._lastStdoutLineCanBeOverwritten:
            sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.write(line)
        flushStdio(sys.stdout)
        self._lastStdoutLineCanBeOverwritten = False

    def _stdoutFilter(self, line: bytes):
        self._lineNotImportantStdoutFilter(line)

    def runWithLogfile(self, args: "typing.Sequence[str]", logfileName: str, *, stdoutFilter=None, cwd: Path = None,
                       env: dict = None, appendToLogfile=False) -> None:
        """
        Runs make and logs the output
        config.quiet doesn't display anything, normal only status updates and config.verbose everything
        :param appendToLogfile: whether to append to the logfile if it exists
        :param args: the command to run (e.g. ["make", "-j32"])
        :param logfileName: the name of the logfile (e.g. "build.log")
        :param cwd the directory to run make in (defaults to self.buildDir)
        :param stdoutFilter a filter to use for standard output (a function that takes a single bytes argument)
        :param env the environment to pass to make
        """
        printCommand(args, cwd=cwd, env=env)
        # make sure that env is either None or a os.environ with the updated entries entries
        if env:
            newEnv = os.environ.copy()
            env = {k: str(v) for k, v in env.items()}  # make sure everything is a string
            newEnv.update(env)
        else:
            newEnv = None
        assert not logfileName.startswith("/")
        if self.config.noLogfile:
            logfilePath = Path(os.devnull)
        else:
            logfilePath = self.buildDir / (logfileName + ".log")
            print("Saving build log to", logfilePath)
        if self.config.pretend:
            return
        if self.config.verbose:
            stdoutFilter = None

        if not self.config.noLogfile and logfilePath.is_file() and not appendToLogfile:
            logfilePath.unlink()  # remove old logfile
        args = list(map(str, args))  # make sure all arguments are strings
        cmdStr = " ".join([shlex.quote(s) for s in args])

        if self.config.noLogfile:
            if stdoutFilter is None:
                # just run the process connected to the current stdout/stdin
                check_call_handle_noexec(args, cwd=str(cwd), env=newEnv)
            else:
                make = popen_handle_noexec(args, cwd=str(cwd), stdout=subprocess.PIPE, env=newEnv)
                self.__runProcessWithFilteredOutput(make, None, stdoutFilter, cmdStr)
            return

        # open file in append mode
        with logfilePath.open("ab") as logfile:
            # print the command and then the logfile
            if appendToLogfile:
                logfile.write(b"\n\n")
            if cwd:
                logfile.write(("cd " + shlex.quote(str(cwd)) + " && ").encode("utf-8"))
            logfile.write(cmdStr.encode("utf-8") + b"\n\n")
            if self.config.quiet:
                # a lot more efficient than filtering every line
                check_call_handle_noexec(args, cwd=str(cwd), stdout=logfile, stderr=logfile, env=newEnv)
                return
            make = popen_handle_noexec(args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=newEnv)
            self.__runProcessWithFilteredOutput(make, logfile, stdoutFilter, cmdStr)

    def __runProcessWithFilteredOutput(self, proc: subprocess.Popen, logfile: "typing.Optional[io.FileIO]",
                                       stdoutFilter: "typing.Callable[[bytes], None]", cmdStr: str):
        logfileLock = threading.Lock()  # we need a mutex so the logfile line buffer doesn't get messed up
        stderrThread = None
        if logfile:
            # use a thread to print stderr output and write it to logfile (not using a thread would block)
            stderrThread = threading.Thread(target=self._handleStdErr, args=(logfile, proc.stderr, logfileLock, self))
            stderrThread.start()
        for line in proc.stdout:
            with logfileLock:  # make sure we don't interleave stdout and stderr lines
                if logfile:
                    logfile.write(line)
                if stdoutFilter:
                    stdoutFilter(line)
                else:
                    sys.stdout.buffer.write(line)
                    flushStdio(sys.stdout)
        retcode = proc.wait()
        if stderrThread:
            stderrThread.join()
        # Not sure if the remaining call is needed
        remainingErr, remainingOut = proc.communicate()
        if remainingErr:
            print("Process had remaining stderr:", remainingErr)
            sys.stderr.buffer.write(remainingErr)
            if logfile:
                logfile.write(remainingOut)
        if remainingOut:
            print("Process had remaining stdout:", remainingOut)
            sys.stdout.buffer.write(remainingOut)
            if logfile:
                logfile.write(remainingErr)
        if stdoutFilter and self._lastStdoutLineCanBeOverwritten:
            # add the final new line after the filtering
            sys.stdout.buffer.write(b"\n")
        if retcode:
            message = "Command \"%s\" failed with exit code %d.\n" % (cmdStr, retcode)
            if logfile:
                message += "See " + logfile.name + " for details."
            raise SystemExit(message)

    def dependencyError(self, *args, installInstructions: str = None):
        self._systemDepsChecked = True  # make sure this is always set
        fatalError("Dependency for", self.target, "missing:", *args, fixitHint=installInstructions)

    def checkSystemDependencies(self) -> None:
        """
        Checks that all the system dependencies (required tool, etc) are available
        :return: Throws an error if dependencies are missing
        """
        for (tool, installInstructions) in self.__requiredSystemTools.items():
            if not shutil.which(tool):
                if callable(installInstructions):
                    installInstructions = installInstructions()
                if not installInstructions:
                    installInstructions = "Try installing `" + tool + "` using your system package manager."
                self.dependencyError("Required program", tool, "is missing!", installInstructions=installInstructions)
        self._systemDepsChecked = True

    def process(self):
        raise NotImplementedError()

    def print(self, *args, **kwargs):
        if not self.config.quiet:
            print(*args, **kwargs)

    def verbose_print(self, *args, **kwargs):
        if self.config.verbose:
            print(*args, **kwargs)

    def info(self, *args, **kwargs):
        # TODO: move all those methods here
        statusUpdate(*args, **kwargs)

    def warning(self, *args, **kwargs):
        warningMessage(*args, **kwargs)

def installDirNotSpecified(config: CheriConfig, project: "Project"):
    raise RuntimeError("dummy impl must not be called: " + str(project))


def _defaultBuildDir(config: CheriConfig, project: "Project"):
    # make sure we have different build dirs for LLVM/CHERIBSD/QEMU 128 and 256
    # HACK To access the descriptor without an instance:
    target = project.crossCompileTarget if hasattr(project, "crossCompileTarget") else None
    return config.buildRoot / (project.projectName.lower() + project.buildDirSuffix(config, target))


class MakeCommandKind(Enum):
    DefaultMake = "system default make"
    GnuMake = "GNU make"
    BsdMake = "BSD make"
    Ninja = "ninja"
    CustomMakeTool = "custom make tool"

class MakeOptions(object):
    def __init__(self, kind: MakeCommandKind, **kwargs):
        self._vars = OrderedDict()
        # Used by e.g. FreeBSD:
        self._with_options = OrderedDict()  # type: typing.Dict[str, bool]
        self._flags = list()
        self.env_vars = {}
        self.set(**kwargs)
        self.kind = kind

    def __do_set(self, target_dict: typing.Dict[str, str], **kwargs):
        for k, v in kwargs.items():
            if isinstance(v, bool):
                v = "1" if v else "0"
            target_dict[k] = str(v)

    def set(self, **kwargs):
        self.__do_set(self._vars, **kwargs)

    def set_env(self, **kwargs):
        self.__do_set(self.env_vars, **kwargs)

    def set_with_options(self, **kwargs):
        """
        For every argument in kwargs sets a WITH_FOO if FOO=True or a WITHOUT_FOO if FOO=False
        Used by the FreeBSD build sysmtem: e.g. make -DWITH_MAN / -DWITHOUT_MAN
        :return: dict of VAR=True/False
        """
        for k, v in kwargs.items():
            assert isinstance(v, bool)
            self._with_options[k] = v

    def add_flags(self, *args) -> None:
        """
        :param args: the flags to add (e.g. -j 16, etc.)
        """
        self._flags.extend(args)

    def _get_defined_var(self, name) -> str:
        # BSD make supports a -DVAR syntax but GNU doesn't
        if self.kind == MakeCommandKind.BsdMake:
            return "-D" + name
        else:
            assert self.kind == MakeCommandKind.GnuMake
            return name + "=1"

    @property
    def all_commandline_args(self) -> list:
        assert self.kind
        result = []
        # First all the variables
        for k, v in self._vars.items():
            assert isinstance(v, str)
            if v == "1":
                result.append(self._get_defined_var(k))
            else:
                result.append(k + "=" + v)
        # then the WITH/WITHOUT variables
        for k, v in self._with_options.items():
            result.append(self._get_defined_var("WITH_" if v else "WITHOUT_") + k)
        # and finally the command line flags like -k
        result.extend(self._flags)
        return result

    def remove_var(self, variable):
        if variable in self._vars:
            del self._vars[variable]
        if variable in self._with_options:
            del self._with_options[variable]
        for flag in self._flags.copy():
            if flag.strip() == "-D" + variable or flag.startswith(variable + "="):
                self._flags.remove(flag)

    def remove_flag(self, flag: str):
        if flag in self._flags:
            self._flags.remove(flag)

    def remove_all(self, predicate: "typing.Callable[bool, [str]]"):
        keys = list(self._vars.keys())
        for k in keys:
            if predicate(k):
                del self._vars[k]

    def copy(self):
        return deepcopy(self)

    def update(self, other: "MakeOptions"):
        self._vars.update(other._vars)
        self._with_options.update(other._with_options)
        self._flags.extend(other._flags)
        self.env_vars.update(other.env_vars)


class Project(SimpleProject):
    repository = ""
    gitRevision = None
    gitBranch = ""
    skipGitSubmodules = False
    compileDBRequiresBear = True
    doNotAddToTargets = True

    defaultSourceDir = ComputedDefaultValue(
        function=lambda config, project: Path(config.sourceRoot / project.projectName.lower()),
        asString=lambda cls: "$SOURCE_ROOT/" + cls.projectName.lower())

    appendCheriBitsToBuildDir = False
    """ Whether to append -128/-256 to the computed build directory name"""

    @classmethod
    def projectBuildDirHelpStr(cls):
        result = "$BUILD_ROOT/" + cls.projectName.lower()
        if cls.appendCheriBitsToBuildDir or hasattr(cls, "crossCompileTarget"):
            result += "-$TARGET"
        result += "-build"
        return result

    defaultBuildDir = ComputedDefaultValue(
        function=_defaultBuildDir, asString=lambda cls: cls.projectBuildDirHelpStr())

    make_kind = MakeCommandKind.DefaultMake
    """
    The kind of too that is used for building and installing (defaults to using "make")
    Set this to MakeCommandKind.GnuMake if the build system needs GNU make features or BsdMake if it needs bmake
    """

    # A per-project config option to generate a CMakeLists.txt that just has a custom taget that calls cheribuild.py
    generate_cmakelists = None

    # TODO: remove these three
    @classmethod
    def getSourceDir(cls, config: CheriConfig):
        return cls.sourceDir

    @classmethod
    def getBuildDir(cls, config: CheriConfig):
        return cls.buildDir

    @classmethod
    def getInstallDir(cls, config: CheriConfig):
        return cls.installDir

    @classmethod
    def buildDirSuffix(cls, config: CheriConfig, target: CrossCompileTarget):
        if target is None:
            # HACK since I can't make the class variable in BuildLLVM dynamic
            # TODO: remove once unified SDK is stable
            append_bits = cls.appendCheriBitsToBuildDir
            if cls.target in ("llvm", "qemu") and config.unified_sdk:
                append_bits = False
            return "-" + config.cheriBitsStr + "-build" if append_bits else "-build"
        elif target == CrossCompileTarget.CHERI:
            return "-" + config.cheriBitsStr + "-build"
        else:
            return "-" + target.value + "-build"

    @classmethod
    def buildDirForTarget(cls, config: CheriConfig, target: CrossCompileTarget):
        return config.buildRoot / (cls.projectName.lower() + "-" + cls.buildDirSuffix(config, target))

    _installToSDK = ComputedDefaultValue(
        function=lambda config, project: config.sdkDir,
        asString="$INSTALL_ROOT/sdk256 or $INSTALL_ROOT/sdk128 depending on CHERI bits")
    _installToBootstrapTools = ComputedDefaultValue(
        function=lambda config, project: config.otherToolsDir,
        asString="$INSTALL_ROOT/bootstrap")

    defaultInstallDir = installDirNotSpecified
    """ The default installation directory (will probably be set to _installToSDK or _installToBootstrapTools) """

    # useful for cross compile projects that use a prefix and DESTDIR
    installPrefix = None
    destdir = None

    __can_use_lld_map = dict()  # type: typing.Dict[Path, bool]

    @classmethod
    def canUseLLd(cls, compiler: Path):
        if compiler not in cls.__can_use_lld_map:
            try:
                runCmd([compiler, "-fuse-ld=lld", "-xc", "-o" "-", "-"], runInPretendMode=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       input="int main() { return 0; }\n", printVerboseOnly=True)
                statusUpdate(compiler, "supports -fuse-ld=lld, linking should be much faster!")
                cls.__can_use_lld_map[compiler] = True
            except subprocess.CalledProcessError:
                statusUpdate(compiler, "does not support -fuse-ld=lld, using slower bfd instead")
                cls.__can_use_lld_map[compiler] = False
        return cls.__can_use_lld_map[compiler]

    @classmethod
    def setupConfigOptions(cls, installDirectoryHelp="", **kwargs):
        super().setupConfigOptions(**kwargs)
        # statusUpdate("Setting up config options for", cls, cls.target)
        cls.sourceDir = cls.addPathOption("source-directory", metavar="DIR", default=cls.defaultSourceDir,
                                          help="Override default source directory for " + cls.projectName)
        cls.buildDir = cls.addPathOption("build-directory", metavar="DIR", default=cls.defaultBuildDir,
                                         help="Override default source directory for " + cls.projectName)
        if not installDirectoryHelp:
            installDirectoryHelp = "Override default install directory for " + cls.projectName
        cls.installDir = cls.addPathOption("install-directory", metavar="DIR", help=installDirectoryHelp,
                                           default=cls.defaultInstallDir)
        if "repository" in cls.__dict__:
            cls.gitRevision = cls.addConfigOption("git-revision", kind=str, help="The git revision to checkout prior to"
                                                                                 " building. Useful if HEAD is broken for one project but you still"
                                                                                 " want to update the other projects.",
                                                  metavar="REVISION")
            cls.repository = cls.addConfigOption("repository", kind=str, help="The URL of the git repository",
                                                 default=cls.repository, metavar="REPOSITORY")
        if inspect.getattr_static(cls, "generate_cmakelists") is None:
            cls.generate_cmakelists = cls.addBoolOption("generate-cmakelists",
                                                        help="Generate a CMakeLists.txt that just calls cheribuild. "
                                                             "Useful for IDEs that only support CMake")

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        # set up the install/build/source directories (allowing overrides from config file)

        self.configureCommand = ""
        # non-assignable variables:
        self.configureArgs = []  # type: typing.List[str]
        self.configureEnvironment = {}  # type: typing.Dict[str,str]
        if self.config.createCompilationDB and self.compileDBRequiresBear:
            self._addRequiredSystemTool("bear", installInstructions="Run `cheribuild.py bear`")
        self._lastStdoutLineCanBeOverwritten = False
        self.make_args = MakeOptions(self.make_kind)
        if self.make_kind == MakeCommandKind.DefaultMake:
            self.makeCommand = "make"
        elif self.make_kind == MakeCommandKind.GnuMake:
            if IS_LINUX and not shutil.which("gmake"):
                statusUpdate("Could not find `gmake` command, assuming `make` is GNU make")
                self.makeCommand = "make"
            else:
                self._addRequiredSystemTool("gmake", homebrewPackage="make")
                self.makeCommand = shutil.which("gmake") or "gmake"
        elif self.make_kind == MakeCommandKind.BsdMake:
            if IS_FREEBSD:
                self.makeCommand = shutil.which("make") or "make"
            else:
                if (self.config.otherToolsDir / "bin/bmake").exists():
                    self.makeCommand = (self.config.otherToolsDir / "bin/bmake")
                else:
                    self.makeCommand = shutil.which("bmake") or "bmake"
                self._addRequiredSystemTool("bmake", homebrewPackage="bmake")
        elif self.make_kind == MakeCommandKind.Ninja:
            self.makeCommand = shutil.which("ninja") or "ninja"
            self._addRequiredSystemTool("ninja", homebrewPackage="ninja")
        else:
            self.makeCommand = "make-command-not-set-this-is-probably-an-error"

        self._preventAssign = True

    # Make sure that API is used properly
    def __setattr__(self, name, value):
        # if self.__dict__.get("_locked") and name == "x":
        #     raise AttributeError, "MyClass does not allow assignment to .x member"
        # self.__dict__[name] = value
        if self.__dict__.get("_preventAssign"):
            # assert name not in ("sourceDir", "buildDir", "installDir")
            if name in ("configureArgs", "configureEnvironment", "make_args"):
                import traceback
                traceback.print_stack()
                fatalError("Project." + name + " mustn't be set, only modification is allowed.", "Called from",
                           self.__class__.__name__)
        self.__dict__[name] = value

    def _ensureGitRepoIsCloned(self, *, srcDir: Path, remoteUrl, initialBranch=None, skipSubmodules=False):
        # git-worktree creates a .git file instead of a .git directory so we can't use .is_dir()
        if not (srcDir / ".git").exists():
            print(srcDir, "is not a git repository. Clone it from' " + remoteUrl + "'?", end="")
            if not self.queryYesNo(defaultResult=False):
                fatalError("Sources for", str(srcDir), " missing!")
            cloneCmd = ["git", "clone"]
            if not skipSubmodules:
                cloneCmd.append("--recurse-submodules")
            if initialBranch:
                cloneCmd += ["--branch", initialBranch]
            runCmd(cloneCmd + [remoteUrl, srcDir], cwd="/")

    def _updateGitRepo(self, srcDir: Path, remoteUrl, *, revision=None, initialBranch=None, skipSubmodules=False):
        self._ensureGitRepoIsCloned(srcDir=srcDir, remoteUrl=remoteUrl, initialBranch=initialBranch,
                                    skipSubmodules=skipSubmodules)
        # make sure we run git stash if we discover any local changes
        hasChanges = len(runCmd("git", "diff", "--stat", "--ignore-submodules",
                                captureOutput=True, cwd=srcDir, printVerboseOnly=True).stdout) > 1
        if hasChanges:
            print(coloured(AnsiColour.green, "Local changes detected in", srcDir))
            # TODO: add a config option to skip this query?
            if not self.queryYesNo("Stash the changes, update and reapply?", defaultResult=True, forceResult=True):
                statusUpdate("Skipping update of", srcDir)
                return
            # TODO: ask if we should continue?
            stashResult = runCmd("git", "stash", "save", "Automatic stash by cheribuild.py",
                                 captureOutput=True, cwd=srcDir, printVerboseOnly=True).stdout
            # print("stashResult =", stashResult)
            if "No local changes to save" in stashResult.decode("utf-8"):
                # print("NO REAL CHANGES")
                hasChanges = False  # probably git diff showed something from a submodule
        pullCmd = ["git", "pull"]
        if not skipSubmodules:
            pullCmd.append("--recurse-submodules")
        runCmd(pullCmd + ["--rebase"], cwd=srcDir, printVerboseOnly=True)
        if not skipSubmodules:
            runCmd("git", "submodule", "update", "--recursive", cwd=srcDir, printVerboseOnly=True)
        if hasChanges:
            runCmd("git", "stash", "pop", cwd=srcDir, printVerboseOnly=True)
        if revision:
            runCmd("git", "checkout", revision, cwd=srcDir, printVerboseOnly=True)

    def runMake(self, makeTarget="", *, makeCommand: str = None, options: MakeOptions=None, logfileName: str = None,
                cwd: Path = None, appendToLogfile=False, compilationDbName="compile_commands.json",
                parallel: bool=True, stdoutFilter: "typing.Callable[[bytes], None]" = "__default_filter__") -> None:
        if not makeCommand:
            makeCommand = self.makeCommand
        if not options:
            options = self.make_args
        if not cwd:
            cwd = self.buildDir

        if makeTarget:
            allArgs = options.all_commandline_args + [makeTarget]
            if not logfileName:
                logfileName = Path(makeCommand).name + "." + makeTarget
        else:
            allArgs = options.all_commandline_args
            if not logfileName:
                logfileName = Path(makeCommand).name
        if parallel:
            allArgs.append(self.config.makeJFlag)

        allArgs = [makeCommand] + allArgs
        if self.config.createCompilationDB and self.compileDBRequiresBear:
            allArgs = [shutil.which("bear"), "--cdb", self.buildDir / compilationDbName,
                       "--append"] + allArgs
        if not self.config.makeWithoutNice:
            allArgs = ["nice"] + allArgs
        starttime = time.time()
        if self.config.noLogfile and stdoutFilter == "__default_filter__":
            # if output isatty() (i.e. no logfile) ninja already filters the output -> don't slow this down by
            # adding a redundant filter in python
            if self.makeCommand == "ninja" and makeTarget != "install":
                stdoutFilter = None
        if stdoutFilter == "__default_filter__":
            stdoutFilter = self._stdoutFilter
        # TODO: this should be a super-verbose flag instead
        if self.config.verbose and makeCommand == "ninja":
            allArgs.append("-v")
        if self.config.passDashKToMake:
            allArgs.append("-k")
            if makeCommand == "ninja":
                # ninja needs the maximum number of failed jobs as an argument
                allArgs.append("50")
        env = options.env_vars
        self.runWithLogfile(allArgs, logfileName=logfileName, stdoutFilter=stdoutFilter, cwd=cwd, env=env,
                            appendToLogfile=appendToLogfile)
        # add a newline at the end in case it ended with a filtered line (no final newline)
        print("Running", self.makeCommand, makeTarget, "took", time.time() - starttime, "seconds")

    def update(self):
        if not self.repository:
            fatalError("Cannot update", self.projectName, "as it is missing a git URL", fatalWhenPretending=True)
        self._updateGitRepo(self.sourceDir, self.repository, revision=self.gitRevision, initialBranch=self.gitBranch,
                            skipSubmodules=self.skipGitSubmodules)

    def clean(self) -> ThreadJoiner:
        assert self.config.clean
        # TODO: never use the source dir as a build dir (unfortunately GDB, postgres and elftoolchain won't work)
        # will have to check how well binutils and qemu work there
        if (self.buildDir / ".git").is_dir():
            if (self.buildDir / "GNUmakefile").is_file() and self.make_kind != MakeCommandKind.BsdMake and self.target != "elftoolchain":
                runCmd(self.makeCommand, "distclean", cwd=self.buildDir)
            else:
                # just use git clean for cleanup
                warningMessage(self.projectName, "does not support out-of-source builds, using git clean to remove"
                                                 "build artifacts.")
                # Try to keep project files for IDEs and other dotfiles:
                runCmd("git", "clean", "-dfx", "--exclude=.*", "--exclude=*.kdev4", cwd=self.buildDir)
        else:
            return self.asyncCleanDirectory(self.buildDir, keepRoot=True)
        return ThreadJoiner(None)

    def needsConfigure(self) -> bool:
        """
        :return: Whether the configure command needs to be run (by default assume yes)
        """
        return True

    def configure(self, cwd: Path = None, configure_path: Path=None):
        if cwd is None:
            cwd = self.buildDir
        if not self.needsConfigure() and not self.config.configureOnly and not self.config.forceConfigure:
            if not self.config.pretend and not self.config.clean:
                return

        _configure_path = self.configureCommand
        if configure_path:
            _configure_path = configure_path
        if _configure_path:
            self.runWithLogfile([_configure_path] + self.configureArgs,
                                logfileName="configure", cwd=cwd, env=self.configureEnvironment)

    def compile(self, cwd: Path = None):
        if cwd is None:
            cwd = self.buildDir
        self.runMake("all", cwd=cwd)

    @property
    def makeInstallEnv(self):
        if self.destdir:
            env = self.make_args.env_vars.copy()
            env["DESTDIR"] = str(self.destdir)
            return env
        return self.make_args.env_vars

    @property
    def real_install_root_dir(self):
        """
        :return: the real install root directory (e.g. if prefix == /usr/local and desdir == /tmp/benchdir it will
         return /tmp/benchdir/usr/local
        """
        if self.destdir is not None:
            assert self.installPrefix
            return self.destdir / self.installPrefix.relative_to(Path("/"))
        return self.installDir

    def runMakeInstall(self, *, options: MakeOptions=None, target="install", _stdoutFilter="__default_filter__", cwd=None,
                       parallel=False, **kwargs):
        if options is None:
            options = self.make_args.copy()
        else:
            options = options.copy()
        options.env_vars.update(self.makeInstallEnv)
        self.runMake(makeTarget=target, options=options, stdoutFilter=_stdoutFilter, cwd=cwd,
                     parallel=parallel, **kwargs)

    def install(self, _stdoutFilter="__default_filter__"):
        self.runMakeInstall(_stdoutFilter=_stdoutFilter)

    def _do_generate_cmakelists(self):
        assert not isinstance(self, CMakeProject), self
        cmakelists = """
# Do not edit!
# Generated by cheribuild.py
#
cmake_minimum_required(VERSION 3.8)
project({project} LANGUAGES NONE)
set(CLEAR_MAKEENV env -u MAKEFLAGS -u MAKELEVEL -u MAKE -u MAKE_TERMERR -u MAKE_TERMOUT -u MFLAGS)
add_custom_target(cheribuild ALL VERBATIM USES_TERMINAL COMMAND {command} --skip-update --skip-install {target})
add_custom_target(cheribuild-j1 VERBATIM USES_TERMINAL COMMAND {command} --skip-update -j1 {target})
add_custom_target(cheribuild-verbose VERBATIM USES_TERMINAL COMMAND {command} --skip-update -v {target})
add_custom_target(cheribuild-verbose-j1 VERBATIM USES_TERMINAL COMMAND {command} --skip-update -v -j1 {target})

add_custom_target(cheribuild-with-install VERBATIM USES_TERMINAL COMMAND {command} --skip-update {target})
add_custom_target(cheribuild-full VERBATIM USES_TERMINAL COMMAND {command} {target})
""".format(command="${CLEAR_MAKEENV} " + sys.argv[0], project=self.projectName, target=self.target)
        target_file = self.sourceDir / "CMakeLists.txt"
        create = True
        if target_file.exists():
            existing_code = target_file.read_text()
            if existing_code == cmakelists:
                create = False
            elif "Generated by cheribuild.py" not in existing_code:
                print("A different CMakeLists.txt already exists. Contents:\n",
                      coloured(AnsiColour.green, existing_code), end="")
                if not self.queryYesNo("Overwrite?", forceResult=False):
                    create = False
        if create:
            self.writeFile(target_file, cmakelists, overwrite=True)

    @property
    def display_name(self):
        return self.projectName

    def process(self):
        if self.generate_cmakelists:
            self._do_generate_cmakelists()
        if self.config.verbose:
            installDir = self.installDir
            if self.destdir is not None:
                installDir = str(self.destdir) + str(self.installPrefix)
            print(self.projectName, "directories: source=%s, build=%s, install=%s" %
                  (self.sourceDir, self.buildDir, installDir))
        if not self.config.skipUpdate:
            self.update()
        if not self._systemDepsChecked:
            self.checkSystemDependencies()
        assert self._systemDepsChecked, "self._systemDepsChecked must be set by now!"

        # run the rm -rf <build dir> in the background
        cleaningTask = self.clean() if self.config.clean else ThreadJoiner(None)
        with cleaningTask:
            if not self.buildDir.is_dir():
                self.makedirs(self.buildDir)
            if not self.config.skipConfigure or self.config.configureOnly:
                statusUpdate("Configuring", self.display_name, "... ")
                self.configure()
            if self.config.configureOnly:
                return
            statusUpdate("Building", self.display_name, "... ")
            self.compile()
            if not self.config.skipInstall:
                statusUpdate("Installing", self.display_name, "... ")
                self.install()


class CMakeProject(Project):
    """
    Like Project but automatically sets up the defaults for CMake projects
    Sets configure command to CMake, adds -DCMAKE_INSTALL_PREFIX=installdir
    and checks that CMake is installed
    """
    doNotAddToTargets = True
    compileDBRequiresBear = False  # cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON does it
    generate_cmakelists = False  # There is already a CMakeLists.txt

    class Generator(Enum):
        Default = 0
        Ninja = 1
        Makefiles = 2

    defaultCMakeBuildType = "Release"

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)
        cls.cmakeBuildType = cls.addConfigOption("build-type", default=cls.defaultCMakeBuildType, metavar="BUILD_TYPE",
                                                 help="The CMake build type (Debug, RelWithDebInfo, Release)")
        cls.cmakeOptions = cls.addConfigOption("cmake-options", default=[], kind=list, metavar="OPTIONS",
                                               help="Additional command line options to pass to CMake")

    def __init__(self, config, generator=Generator.Ninja):
        super().__init__(config)
        self.configureCommand = os.getenv("CMAKE_COMMAND", "cmake")
        self._addRequiredSystemTool("cmake", installInstructions=self._cmakeInstallInstructions)
        self.generator = generator
        self.configureArgs.append(str(self.sourceDir))  # TODO: use undocumented -H and -B options?
        if self.generator == CMakeProject.Generator.Ninja:
            self.configureArgs.append("-GNinja")
            self.makeCommand = "ninja"
            self._addRequiredSystemTool("ninja")
        if self.generator == CMakeProject.Generator.Makefiles:
            self.configureArgs.append("-GUnix Makefiles")

        self.configureArgs.append("-DCMAKE_BUILD_TYPE=" + self.cmakeBuildType)
        # TODO: do it always?
        if self.config.createCompilationDB:
            self.configureArgs.append("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
            # Don't add the user provided options here, add them in configure() so that they are put last
        self.__minimum_cmake_version = tuple()

    def add_cmake_options(self, **kwargs):
        for option, value in kwargs.items():
            if isinstance(value, bool):
                value = "ON" if value else "OFF"
            self.configureArgs.append("-D" + option + "=" + str(value))

    def set_minimum_cmake_version(self, major, minor):
        self.__minimum_cmake_version = (major, minor)

    def _cmakeInstallStdoutFilter(self, line: bytes):
        # don't show the up-to date install lines
        if line.startswith(b"-- Up-to-date:"):
            return
        self._showLineStdoutFilter(line)

    def needsConfigure(self) -> bool:
        if self.config.pretend and (self.config.forceConfigure or self.config.clean):
            return True
        # CMake is smart enough to detect when it must be reconfigured -> skip configure if cache exists
        cmakeCache = self.buildDir / "CMakeCache.txt"
        buildFile = "build.ninja" if self.generator == CMakeProject.Generator.Ninja else "Makefile"
        return not cmakeCache.exists() or not (self.buildDir / buildFile).exists()

    def configure(self, **kwargs):
        if self.installPrefix:
            assert self.destdir, "custom install prefix requires DESTDIR being set!"
            self.add_cmake_options(CMAKE_INSTALL_PREFIX=self.installPrefix)
        else:
            self.add_cmake_options(CMAKE_INSTALL_PREFIX=self.installDir)
        self.configureArgs.extend(self.cmakeOptions)
        # make sure we get a completely fresh cache when --reconfigure is passed:
        cmakeCache = self.buildDir / "CMakeCache.txt"
        if self.config.forceConfigure:
            self.deleteFile(cmakeCache)
        super().configure(**kwargs)

    def install(self, _stdoutFilter="__DEFAULT__"):
        if _stdoutFilter == "__DEFAULT__":
            _stdoutFilter = self._cmakeInstallStdoutFilter
        super().install(_stdoutFilter=_stdoutFilter)

    def _get_cmake_version(self):
        versionPattern = re.compile(b"cmake version (\\d+)\\.(\\d+)\\.?(\\d+)?")
        # cmake prints this output to stdout
        versionString = runCmd(self.configureCommand, "--version", captureOutput=True, printVerboseOnly=True).stdout
        match = versionPattern.search(versionString)
        return tuple(map(int, match.groups())) if match else (0, 0, 0)

    def checkSystemDependencies(self):
        super().checkSystemDependencies()
        if self.__minimum_cmake_version:
            # try to find cmake 3.4 or newer
            versionComponents = self._get_cmake_version()
            # noinspection PyTypeChecker
            if versionComponents < self.__minimum_cmake_version:
                versionStr = ".".join(map(str, versionComponents))
                expectedStr = ".".join(map(str, self.__minimum_cmake_version))
                self.dependencyError("CMake version", versionStr, "is too old (need at least", expectedStr + ")",
                                     installInstructions=self._cmakeInstallInstructions)

    @staticmethod
    def findPackage(name: str) -> bool:
        try:
            cmd = "cmake --find-package -DCOMPILER_ID=Clang -DLANGUAGE=CXX -DMODE=EXIST -DQUIET=TRUE".split()
            cmd.append("-DNAME=" + name)
            return runCmd(cmd).returncode == 0
        except subprocess.CalledProcessError:
            return False


class AutotoolsProject(Project):
    doNotAddToTargets = True
    _configure_supports_prefix = True

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)
        cls.extraConfigureFlags = cls.addConfigOption("configure-options", default=[], kind=list, metavar="OPTIONS",
                                                      help="Additional command line options to pass to configure")

    """
    Like Project but automatically sets up the defaults for autotools like projects
    Sets configure command to ./configure, adds --prefix=installdir
    """

    def __init__(self, config, configureScript="configure"):
        super().__init__(config)
        self.configureCommand = self.sourceDir / configureScript

    def configure(self, **kwargs):
        if self._configure_supports_prefix:
            if self.installPrefix:
                assert self.destdir, "custom install prefix requires DESTDIR being set!"
                self.configureArgs.append("--prefix=" + str(self.installPrefix))
            else:
                self.configureArgs.append("--prefix=" + str(self.installDir))
        if self.extraConfigureFlags:
            self.configureArgs.extend(self.extraConfigureFlags)
        super().configure(**kwargs)

    def needsConfigure(self):
        return not (self.buildDir / "Makefile").exists()

# A target that is just an alias for at least one other targets but does not force building of dependencies
class TargetAlias(SimpleProject):
    doNotAddToTargets = True
    dependenciesMustBeBuilt = False
    hasSourceFiles = False
    isAlias = True

    def process(self):
        assert len(self.dependencies) > 0


# A target that does nothing (used for e.g. the "all" target)
class TargetAliasWithDependencies(TargetAlias):
    doNotAddToTargets = True
    dependenciesMustBeBuilt = True
    hasSourceFiles = False


class BuildAll(TargetAliasWithDependencies):
    dependencies = ["qemu", "sdk", "disk-image", "run"]
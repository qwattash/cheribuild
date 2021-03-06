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
import sys

from .crosscompileproject import *
from .cheribsd import BuildCHERIBSD
from ..build_qemu import BuildQEMU
from ..llvm import BuildCheriLLVM
from ..run_qemu import LaunchCheriBSD
from ...config.loader import ComputedDefaultValue
from ...utils import OSInfo, setEnv, runCmd, warningMessage, commandline_to_str, IS_MAC
from ..project import ReuseOtherProjectRepository
import os


def _cxx_install_dir(config: CheriConfig, project):
    if project.get_crosscompile_target(config) == CrossCompileTarget.NATIVE:
        return _INVALID_INSTALL_DIR
    return BuildCHERIBSD.rootfsDir(project, config) / "opt/c++"


installToCXXDir = ComputedDefaultValue(function=_cxx_install_dir, asString="$CHERIBSD_ROOTFS/opt/c++")


class BuildLibunwind(CrossCompileCMakeProject):
    # TODO: add an option to allow upstream llvm?
    repository = ReuseOtherProjectRepository(BuildCheriLLVM, subdirectory="libunwind")
    defaultInstallDir = installToCXXDir

    @property
    def should_use_sdk_clang(self):
        if self.compiling_for_host() and not self.config.use_sdk_clang_for_native_xbuild:
            return False
        return True

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        # Adding -ldl won't work: no libdl in /usr/libcheri
        self.add_cmake_options(LIBUNWIND_HAS_DL_LIB=False)
        self.lit_path = BuildCheriLLVM.getBuildDir(self) / "bin/llvm-lit"
        self.add_cmake_options(
            #  LLVM_CONFIG_PATH=self.compiler_dir / "llvm-config",
            LLVM_PATH=BuildCheriLLVM.getSourceDir(self) / "llvm",
            LLVM_EXTERNAL_LIT=self.lit_path,
        )

    def configure(self, **kwargs):
        # TODO: should share some code with libcxx
        # to find the libcxx lit config files and library:
        test_compiler_flags = commandline_to_str(self.default_compiler_flags)
        test_linker_flags = commandline_to_str(self.default_ldflags)

        self.add_cmake_options(# LIBUNWIND_LIBCXX_PATH=BuildLibCXX.getSourceDir(self),
                               LIBUNWIND_LIBCXX_PATH=self.repository.source_project.getSourceDir(self) / "libcxx",
                               # Should use libc++ from sysroot
                               # LIBUNWIND_LIBCXX_LIBRARY_PATH=BuildLibCXX.getBuildDir(self) / "lib",
                               LIBUNWIND_LIBCXX_LIBRARY_PATH="",
                               LIBUNWIND_TEST_LINKER_FLAGS=test_linker_flags,
                               LIBUNWIND_TEST_COMPILER_FLAGS=test_compiler_flags,
                               LIBUNWIND_ENABLE_ASSERTIONS=True,
                               )
        # Lit multiprocessing seems broken with python 2.7 on FreeBSD (and python 3 seems faster at least for libunwind/libcxx)
        self.add_cmake_options(PYTHON_EXECUTABLE=sys.executable)
        if self.compiling_for_host():
            if IS_MAC or OSInfo.isUbuntu():
                # Can't link libc++abi on MacOS and libsupc++ statically on Ubuntu
                self.add_cmake_options(LIBUNWIND_TEST_ENABLE_EXCEPTIONS=False)
                # Static linking is broken on Ubuntu 16.04
                self.add_cmake_options(LIBUINWIND_BUILD_STATIC_TEST_BINARIES=False)
        else:
            self.add_cmake_options(LIBCXX_ENABLE_SHARED=False,
                                   LIBUNWIND_ENABLE_SHARED=True)
            collect_test_binaries = self.buildDir / "test-output"
            executor = "CollectBinariesExecutor(\\\"{path}\\\", self)".format(path=collect_test_binaries)
            self.add_cmake_options(
                LLVM_LIT_ARGS="--xunit-xml-output " + os.getenv("WORKSPACE", ".") +
                              "/libunwind-test-results.xml --max-time 3600 --timeout 120 -s -vv -j1",
                LIBUNWIND_TARGET_TRIPLE=self.targetTriple, LIBUNWIND_SYSROOT=self.sdkSysroot)

            target_info = "libcxx.test.target_info.CheriBSDRemoteTI"
            # add the config options required for running tests:
            self.add_cmake_options(LIBUNWIND_EXECUTOR=executor, LIBUNWIND_TARGET_INFO=target_info,
                                   LIBUNWIND_CXX_ABI_LIBNAME="libcxxrt")
            version_script = self.sourceDir / "Version.map.FreeBSD"
            if not version_script.exists():
                self.fatal("libunwind version script is missing, please update llvm-project!")
            self.add_cmake_options(LIBUNWIND_USE_VERSION_SCRIPT=version_script)

        # Do not link against libgcc_s when building the shared library:
        self.add_cmake_options(LIBUNWIND_USE_COMPILER_RT=True)
        super().configure(**kwargs)

    def process(self):
        # TODO: update libcxxrt to always build against host/cheribsd version
        if False:
            if self.compiling_for_host():
                self.warning("Libunwind should be provided by the host OS, are you sure you")
            else:
                self.warning("Libunwind is included as part of the CheriBSD sysroot, this target only needs"
                             " to be run if you are testing new features in libunwind.")
            if not self.queryYesNo("Continue anyway?", defaultResult=True):
                return
        super().process()

    def run_tests(self):
        if self.compiling_for_host():
            runCmd("ninja", "check-unwind", "-v", cwd=self.buildDir)
        else:
            # Check that the four tests compile and then attempt to run them:
            # TODO: run the three combinations here too?
            runCmd("ninja", "check-unwind", "-v", cwd=self.buildDir)
            self.run_cheribsd_test_script("run_libunwind_tests.py", "--lit-debug-output",
                                          "--llvm-lit-path", self.lit_path, mount_sysroot=True)



class BuildLibCXXRT(CrossCompileCMakeProject):
    repository = GitRepository("https://github.com/CTSRD-CHERI/libcxxrt.git")
    defaultInstallDir = installToCXXDir
    dependencies = ["libunwind"]

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.add_cmake_options(LIBUNWIND_PATH=BuildLibunwind.getInstallDir(self) / "lib",
                               CMAKE_INSTALL_RPATH_USE_LINK_PATH=True)
        if self.compiling_for_host():
            assert not self.baremetal
            self.add_cmake_options(BUILD_TESTS=True, TEST_LIBUNWIND=True)
            if OSInfo.isUbuntu():
                self.add_cmake_options(COMPARE_TEST_OUTPUT_TO_SYSTEM_OUTPUT=False)
                # Seems to be needed for at least jenkins (it says relink with -pie)
                self.add_cmake_options(CMAKE_POSITION_INDEPENDENT_CODE=True)
                # The static libc.a on Ubuntu is not compiled with -fPIC so we can't link to it..
                self.add_cmake_options(NO_STATIC_TEST=True)
            self.add_cmake_options(NO_UNWIND_LIBRARY=False)
        else:
            # TODO: __sync_fetch_and_add in exceptions code
            self.add_cmake_options(NO_SHARED=self.force_static_linkage,
                                   DISABLE_EXCEPTIONS_RTTI=False,
                                   NO_UNWIND_LIBRARY=False)
            self.add_cmake_options(COMPARE_TEST_OUTPUT_TO_SYSTEM_OUTPUT=False)
            if not self.baremetal:
                self.add_cmake_options(BUILD_TESTS=True, TEST_LIBUNWIND=True)

    def install(self, **kwargs):
        libdir = self.installDir / "libcheri" if self.compiling_for_cheri() else self.installDir / "lib"
        self.installFile(self.buildDir / "lib/libcxxrt.a", libdir / "libcxxrt.a", force=True)
        # self.installFile(self.buildDir / "lib/libcxxrt.a", libdir / "libcxxrt.so", force=True)
        # self.installFile(self.buildDir / "lib/libcxxrt.so", self.installDir / "usr/libcheri/libcxxrt.so", force=True)

    def run_tests(self):
        # TODO: this won't work on macOS
        with setEnv(LD_LIBRARY_PATH=self.buildDir / "lib"):
            if self.compiling_for_host():
                runCmd("ctest", ".", "-VV", cwd=self.buildDir)
            else:
                self.run_cheribsd_test_script("run_libcxxrt_tests.py",
                                              "--libunwind-build-dir", BuildLibunwind.getBuildDir(self),
                                              mount_builddir=True, mount_sysroot=True)


class BuildLibCXX(CrossCompileCMakeProject):
    # TODO: add an option to allow upstream llvm?
    repository = ReuseOtherProjectRepository(BuildCheriLLVM, subdirectory="libcxx")
    defaultInstallDir = installToCXXDir
    dependencies = ["libcxxrt"]

    @property
    def should_use_sdk_clang(self):
        if self.compiling_for_host() and not self.config.use_sdk_clang_for_native_xbuild:
            return False
        return True

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)
        cls.only_compile_tests = cls.addBoolOption("only-compile-tests",
                                                   help="Don't attempt to run tests, only compile them")
        cls.exceptions = cls.addBoolOption("exceptions", default=True, help="Build with support for C++ exceptions")
        cls.collect_test_binaries = cls.addPathOption("collect-test-binaries", metavar="TEST_PATH",
                                                      help="Instead of running tests copy them to $TEST_PATH")
        cls.nfs_mounted_path = cls.addPathOption("nfs-mounted-path", metavar="PATH", help="Use a PATH as a directory"
                                                                                          "that is NFS mounted inside QEMU instead of using scp to copy "
                                                                                          "individual tests")
        cls.nfs_path_in_qemu = cls.addPathOption("nfs-mounted-path-in-qemu", metavar="PATH",
                                                 help="The path used inside QEMU to refer to nfs-mounted-path")
        cls.qemu_host = cls.addConfigOption("ssh-host", help="The QEMU SSH hostname to connect to for running tests",
                                            default=lambda c, p: "localhost")
        cls.qemu_port = cls.addConfigOption("ssh-port", help="The QEMU SSH port to connect to for running tests",
                                            default=lambda c, p: LaunchCheriBSD.get_instance(p, c).sshForwardingPort)
        cls.qemu_user = cls.addConfigOption("ssh-user", default="root", help="The CheriBSD used for running tests")

        cls.test_jobs = cls.addConfigOption("parallel-test-jobs", help="Number of QEMU instances spawned to run tests "
                                                                       "(default: number of build jobs (-j flag) / 2)",
                                            default=lambda c, p: c.makeJobs / 2, kind=int)

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        if self.qemu_host:
            self.qemu_host = os.path.expandvars(self.qemu_host)
        self.libcxx_lit_jobs = ""


        if self.compiling_for_host():
            self.add_cmake_options(LIBCXX_ENABLE_SHARED=True, LIBCXX_ENABLE_STATIC_ABI_LIBRARY=False)
            if OSInfo.isUbuntu():
                # Ubuntu packagers think that static linking should not be possible....
                self.add_cmake_options(LIBCXX_ENABLE_STATIC=False)
        else:
            self.addCrossFlags()
        # add the common test options
        self.add_cmake_options(
            CMAKE_INSTALL_RPATH_USE_LINK_PATH=True,  # Fix finding libunwind.so
            LIBCXX_INCLUDE_TESTS=True,
            LLVM_PATH=BuildCheriLLVM.getSourceDir(self) / "llvm",
            LLVM_EXTERNAL_LIT=BuildCheriLLVM.getBuildDir(self) / "bin/llvm-lit",
            LIBCXXABI_USE_LLVM_UNWINDER=False,  # we have a fake libunwind in libcxxrt
            LLVM_LIT_ARGS="--xunit-xml-output " + os.getenv("WORKSPACE", ".") +
                          "/libcxx-test-results.xml --max-time 3600 --timeout 120 -s -vv" + self.libcxx_lit_jobs
        )
        # Lit multiprocessing seems broken with python 2.7 on FreeBSD (and python 3 seems faster at least for libunwind/libcxx)
        self.add_cmake_options(PYTHON_EXECUTABLE=sys.executable)
        # select libcxxrt as the runtime library (except on macos where this doesn't seem to work very well)
        if not (self.compiling_for_host() and IS_MAC):
            self.add_cmake_options(
                LIBCXX_CXX_ABI="libcxxrt",
                LIBCXX_CXX_ABI_LIBNAME="libcxxrt",
                LIBCXX_CXX_ABI_INCLUDE_PATHS=BuildLibCXXRT.getSourceDir(self) / "src",
                LIBCXX_CXX_ABI_LIBRARY_PATH=BuildLibCXXRT.getBuildDir(self) / "lib",
            )
            # use llvm libunwind when testing
            self.add_cmake_options(LIBCXX_STATIC_CXX_ABI_LIBRARY_NEEDS_UNWIND_LIBRARY=True,
                                   LIBCXX_CXX_ABI_UNWIND_LIBRARY="unwind",
                                   LIBCXX_CXX_ABI_UNWIND_LIBRARY_PATH=BuildLibunwind.getBuildDir(self) / "lib")

        if not self.exceptions or self.baremetal:
            self.add_cmake_options(LIBCXX_ENABLE_EXCEPTIONS=False, LIBCXX_ENABLE_RTTI=False)
        else:
            self.add_cmake_options(LIBCXX_ENABLE_EXCEPTIONS=True, LIBCXX_ENABLE_RTTI=True)
        # TODO: remove this once stuff has been fixed:
        self.common_warning_flags.append("-Wno-ignored-attributes")
        print(self.common_warning_flags)

    def addCrossFlags(self):
        # TODO: do I even need the toolchain file to cross compile?

        self.add_cmake_options(LIBCXX_TARGET_TRIPLE=self.targetTriple,
                               LIBCXX_SYSROOT=self.sdkSysroot)

        if self.compiling_for_cheri():
            # Ensure that we don't have failing tests due to cheri bugs
            self.common_warning_flags.append("-Werror=cheri")

        # We need to build with -G0 otherwise we get R_MIPS_GPREL16 out of range linker errors
        test_compile_flags = commandline_to_str(self.default_compiler_flags)
        test_linker_flags = commandline_to_str(self.default_ldflags)
        print("test_compile_flags:", test_compile_flags)

        if self.baremetal:
            if self.compiling_for_mips():
                test_compile_flags += " -fno-pic -mno-abicalls"
            self.add_cmake_options(
                LIBCXX_ENABLE_FILESYSTEM=False,
                LIBCXX_USE_COMPILER_RT=True,
                LIBCXX_ENABLE_STDIN=False,  # currently not support on baremetal QEMU
                LIBCXX_ENABLE_GLOBAL_FILESYSTEM_NAMESPACE=False,  # no filesystem on baremetal QEMU
                # TODO: we should be able to implement this in QEMU
                LIBCXX_ENABLE_MONOTONIC_CLOCK=False,  # no monotonic clock for now
            )
            test_linker_flags += " -Wl,-T,qemu-malta.ld"


        self.add_cmake_options(LIBCXX_TEST_COMPILER_FLAGS=test_compile_flags,
                               LIBCXX_TEST_LINKER_FLAGS=test_linker_flags,
                               LIBCXX_SLOW_TEST_HOST=True) # some tests need more tolerance/less iterations on QEMU

        self.add_cmake_options(
            LIBCXX_ENABLE_SHARED=False,  # not yet
            LIBCXX_ENABLE_STATIC=True,
            LIBCXX_ENABLE_THREADS=not self.baremetal,  # no threads on baremetal newlib
            # baremetal the -fPIC build doesn't work for some reason (runs out of CALL16 relocations)
            # Not sure how this can happen since LLD includes multigot
            LIBCXX_BUILD_POSITION_DEPENDENT=self.baremetal,

            LIBCXX_ENABLE_EXPERIMENTAL_LIBRARY=False,  # not yet
            LIBCXX_INCLUDE_BENCHMARKS=False,
            LIBCXX_INCLUDE_DOCS=False,
            # When cross compiling we link the ABI library statically (except baremetal since that doens;t have it yet)
            LIBCXX_ENABLE_STATIC_ABI_LIBRARY=not self.baremetal,
        )
        if self.only_compile_tests:
            executor = "CompileOnlyExecutor()"
        elif self.collect_test_binaries:
            executor = "CollectBinariesExecutor(\\\"{path}\\\", self)".format(path=self.collect_test_binaries)
        elif self.baremetal:
            run_qemu_script = self.config.sdkDir / "baremetal/mips64-qemu-elf/bin/run_with_qemu.py"
            if not run_qemu_script.exists():
                warningMessage("run_with_qemu.py is needed to run libcxx baremetal tests but could not find it:",
                               run_qemu_script, "does not exist")
            prefix = [str(run_qemu_script), "--qemu", str(BuildQEMU.qemu_binary(self)), "--timeout", "20"]
            prefix_list = '[\\\"' + "\\\", \\\"".join(prefix) + "\\\"]"
            executor = "PrefixExecutor(" + prefix_list + ", LocalExecutor())"
            print(executor)
        elif self.nfs_mounted_path:
            self.libcxx_lit_jobs = " -j1" # We can only run one job here since we are using scp
            executor = "SSHExecutorWithNFSMount(\\\"{host}\\\", nfs_dir=\\\"{nfs_dir}\\\", path_in_target=\\\"{nfs_in_target}\\\"," \
                       " config=self, username=\\\"{user}\\\", port={port})".format(host=self.qemu_host, user=self.qemu_user,
                                                                                    port=self.qemu_port,
                                                                                    nfs_dir=self.nfs_mounted_path,
                                                                                    nfs_in_target=self.nfs_path_in_qemu)
        else:
            self.libcxx_lit_jobs = " -j1" # We can only run one job here since we are using scp
            executor = "SSHExecutor('{host}', username='{user}', port={port}, config=self)".format(
                host=self.qemu_host, user=self.qemu_user, port=self.qemu_port)
        if self.baremetal:
            target_info = "libcxx.test.target_info.BaremetalNewlibTI"
        else:
            target_info = "libcxx.test.target_info.CheriBSDRemoteTI"
        # add the config options required for running tests:
        self.add_cmake_options(LIBCXX_EXECUTOR=executor, LIBCXX_TARGET_INFO=target_info, LIBCXX_RUN_LONG_TESTS=False)

    def run_tests(self):
        if self.compiling_for_host():
            runCmd("ninja", "check-cxx", "-v", cwd=self.buildDir)
        else:
            #  "--lit-debug-output"?
            self.run_cheribsd_test_script("run_libcxx_tests.py", "--parallel-jobs", self.test_jobs,
                                          # long running test -> speed up by using a kernel without invariants
                                          use_benchmark_kernel_by_default=True)

class BuildCompilerRt(CrossCompileCMakeProject):
    # TODO: add an option to allow upstream llvm?
    repository = ReuseOtherProjectRepository(BuildCheriLLVM, subdirectory="compiler-rt")
    projectName = "compiler-rt"
    crossInstallDir = CrossInstallDir.COMPILER_RESOURCE_DIR
    _check_install_dir_conflict = False
    supported_architectures = CrossCompileAutotoolsProject.CAN_TARGET_ALL_TARGETS
    default_architecture = CrossCompileTarget.CHERI

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.add_cmake_options(
            # LLVM_CONFIG_PATH=BuildCheriLLVM.buildDir / "bin/llvm-config",
            LLVM_CONFIG_PATH=self.config.sdkBinDir / "llvm-config",
            LLVM_EXTERNAL_LIT=BuildCheriLLVM.getBuildDir(self) / "bin/llvm-lit",
            COMPILER_RT_BUILD_BUILTINS=True,
            COMPILER_RT_BUILD_SANITIZERS=True,
            COMPILER_RT_BUILD_XRAY=False,
            COMPILER_RT_BUILD_LIBFUZZER=True,
            COMPILER_RT_BUILD_PROFILE=False,
            COMPILER_RT_BAREMETAL_BUILD=self.baremetal,
            # COMPILER_RT_DEFAULT_TARGET_ONLY=True,
            # BUILTIN_SUPPORTED_ARCH="mips64",
            TARGET_TRIPLE=self.targetTriple,
            # LLVM_ENABLE_PER_TARGET_RUNTIME_DIR=True,
        )
        if self.debugInfo:
            self.add_cmake_options(COMPILER_RT_DEBUG=True)

        if self.compiling_for_mips() or self.compiling_for_cheri():
            # self.add_cmake_options(COMPILER_RT_DEFAULT_TARGET_ARCH="mips")
            self.add_cmake_options(COMPILER_RT_DEFAULT_TARGET_ONLY=True)

    def install(self, **kwargs):
        super().install(**kwargs)
        if self.compiling_for_cheri():
            # HACK: we don't really need the ubsan runtime but the toolchain pulls it in automatically
            # TODO: is there an easier way to create an empty archive?
            ubsan_runtime_path = self.installDir / ("lib/freebsd/libclang_rt.ubsan_standalone-mips64c" + self.config.cheriBitsStr + ".a")
            if not ubsan_runtime_path.exists():
                self.warning("Did not install ubsan runtime", ubsan_runtime_path)


class BuildCompilerRtBaremetal(CrossCompileCMakeProject):
    # TODO: add an option to allow upstream llvm?
    repository = ReuseOtherProjectRepository(BuildCheriLLVM, subdirectory="compiler-rt")
    projectName = "compiler-rt-baremetal"
    crossInstallDir = CrossInstallDir.SDK
    dependencies = ["newlib-baremetal"]
    baremetal = True
    supported_architectures = CrossCompileAutotoolsProject.CAN_TARGET_ALL_BAREMETAL_TARGETS
    default_architecture = CrossCompileTarget.MIPS

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        # self.COMMON_FLAGS.append("-v")
        self.COMMON_FLAGS.append("-ffreestanding")
        if self.compiling_for_mips():
            self.add_cmake_options(COMPILER_RT_HAS_FPIC_FLAG=False)  # HACK: currently we build everything as -fno-pic
        self.add_cmake_options(
            # LLVM_CONFIG_PATH=BuildCheriLLVM.buildDir / "bin/llvm-config",
            LLVM_CONFIG_PATH=self.config.sdkBinDir / "llvm-config",
            LLVM_EXTERNAL_LIT=BuildCheriLLVM.getBuildDir(self) / "bin/llvm-lit",
            COMPILER_RT_BUILD_BUILTINS=True,
            COMPILER_RT_BUILD_SANITIZERS=False,
            COMPILER_RT_BUILD_XRAY=False,
            COMPILER_RT_BUILD_LIBFUZZER=False,
            COMPILER_RT_BUILD_PROFILE=False,
            COMPILER_RT_BAREMETAL_BUILD=self.baremetal,
            COMPILER_RT_DEFAULT_TARGET_ONLY=True,
            # BUILTIN_SUPPORTED_ARCH="mips64",
            TARGET_TRIPLE=self.targetTriple,
        )
        if self.debugInfo:
            self.add_cmake_options(COMPILER_RT_DEBUG=True)
        if self.compiling_for_mips() or self.compiling_for_cheri():
            # self.add_cmake_options(COMPILER_RT_DEFAULT_TARGET_ARCH="mips")
            self.add_cmake_options(COMPILER_RT_DEFAULT_TARGET_ONLY=True)

    def configure(self, **kwargs):
        self.configureArgs[0] = str(self.sourceDir / "lib/builtins")
        super().configure()

    def install(self, **kwargs):
        super().install(**kwargs)

        libname = "libclang_rt.builtins-" + self.triple_arch + ".a"
        self.moveFile(self.installDir / "lib/generic" / libname, self.real_install_root_dir / "lib" / libname)
        if self.compiling_for_cheri():
            # compatibility with older compilers
            self.createSymlink(self.real_install_root_dir / "lib" / libname,
                               self.real_install_root_dir / "lib" / "libclang_rt.builtins-cheri.a", print_verbose_only=False)
            self.createSymlink(self.real_install_root_dir / "lib" / libname,
                               self.real_install_root_dir / "lib" / "libclang_rt.builtins-mips64.a", print_verbose_only=False)
        # HACK: we don't really need libunwind but the toolchain pulls it in automatically
        # TODO: is there an easier way to create empty .a files?
        runCmd("ar", "rcv", self.installDir / "lib/libunwind.a", "/dev/null")
        runCmd("ar", "dv", self.installDir / "lib/libunwind.a", "null")
        runCmd("ar", "t", self.installDir / "lib/libunwind.a")  # should be empty now


class BuildLibCXXBaremetal(BuildLibCXX):
    repository = GitRepository("https://github.com/CTSRD-CHERI/libcxx.git")
    dependencies = ["libcxxrt-baremetal"]
    projectName = "libcxx-baremetal"
    # target = "libcxx-baremetal"
    baremetal = True
    supported_architectures = CrossCompileAutotoolsProject.CAN_TARGET_ALL_BAREMETAL_TARGETS
    crossInstallDir = CrossInstallDir.SDK
    default_architecture = CrossCompileTarget.MIPS
    defaultCMakeBuildType = "Debug"

    def __init__(self, config: CheriConfig):
        super().__init__(config)

        # self.COMMON_FLAGS.append("-v")
        # Seems to be necessary :(
        # self.COMMON_FLAGS.extend(["-mxgot", "-mllvm", "-mxmxgot"])
        self.COMMON_FLAGS.append("-O0")


class BuildLibCXXRTBaremetal(BuildLibCXXRT):
    repository = GitRepository("https://github.com/CTSRD-CHERI/libcxxrt.git")
    projectName = "libcxxrt-baremetal"
    dependencies = ["newlib-baremetal", "compiler-rt-baremetal"]
    crossInstallDir = CrossInstallDir.SDK
    baremetal = True
    supported_architectures = CrossCompileAutotoolsProject.CAN_TARGET_ALL_BAREMETAL_TARGETS
    default_architecture = CrossCompileTarget.MIPS

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.COMMON_FLAGS.append("-Dsched_yield=abort")  # UNIPROCESSOR, should never happen

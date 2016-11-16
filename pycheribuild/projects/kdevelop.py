from ..project import CMakeProject, Project
from ..utils import *
from pathlib import Path
import tempfile

import os


def kdevInstallDir(config: CheriConfig):
    return config.sdkDir


class BuildLibKompareDiff2(CMakeProject):
    defaultCMakeBuildType = "Debug"

    def __init__(self, config: CheriConfig):
        super().__init__(config, installDir=kdevInstallDir(config), gitUrl="git://anongit.kde.org/libkomparediff2.git")


class BuildKDevplatform(CMakeProject):
    dependencies = ["libkomparediff2"]
    defaultCMakeBuildType = "Debug"

    def __init__(self, config: CheriConfig):
        super().__init__(config, installDir=kdevInstallDir(config), appendCheriBitsToBuildDir=True,
                         gitUrl="https://github.com/RichardsonAlex/kdevplatform.git")
        self.gitBranch = "cheri"
        self.configureArgs.append("-DBUILD_git=OFF")


class BuildKDevelop(CMakeProject):
    dependencies = ["kdevplatform", "llvm"]
    defaultCMakeBuildType = "Debug"

    def __init__(self, config: CheriConfig):
        super().__init__(config, installDir=kdevInstallDir(config), appendCheriBitsToBuildDir=True,
                         gitUrl="https://github.com/RichardsonAlex/kdevelop.git")
        # Tell kdevelop to use the CHERI clang
        self.configureArgs.append("-DLLVM_ROOT=" + str(self.config.sdkDir))
        # install the wrapper script that sets the right environment variables
        self.configureArgs.append("-DINSTALL_KDEVELOP_LAUNCH_WRAPPER=ON")
        self.gitBranch = "cheri"


class StartKDevelop(Project):
    target = "run-kdevelop"
    dependencies = ["kdevelop"]

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self._addRequiredSystemTool("cmake")
        self._addRequiredSystemTool("qtpaths")

    def process(self):
        kdevelopBinary = self.config.sdkDir / "bin/start-kdevelop.py"
        if not kdevelopBinary.exists():
            self.dependencyError("KDevelop is missing:", kdevelopBinary,
                                 installInstructions="Run `cheribuild.py kdevelop` or `cheribuild.py " +
                                                     self.target + " -d`.")
        runCmd(kdevelopBinary, "--ps")
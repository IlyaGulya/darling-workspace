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
        if rootless_marker not in environment:
            continue
        owns_prefix = prefix_marker in environment
        if not owns_prefix:
            for proc_link in (process_dir / "cwd", process_dir / "exe"):
                try:
                    proc_target = proc_link.resolve(strict=False)
                    proc_target.relative_to(resolved_prefix)
                except (OSError, ValueError):
                    continue
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


def prefix_process_snapshot(prefix: Path, entries: Iterable[ProcessEntry]) -> list[str]:
    """Return the darlingserver process tree rooted at ``prefix``.

    ``darlingserver`` is launched with the prefix as argv[1].  Children do not
    carry that argv, so snapshot discovery first finds matching server roots and
    then walks the process parent graph.
    """

    prefix_text = str(prefix)
    children: dict[int, list[int]] = {}
    args_by_pid: dict[int, str] = {}
    roots: list[int] = []
    for pid, ppid, args in entries:
        args_by_pid[pid] = args
        children.setdefault(ppid, []).append(pid)
        argv = args.split()
        if len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and argv[1] == prefix_text:
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


def darlingserver_pids_for_prefix(prefix: Path, entries: Iterable[ProcessEntry]) -> list[int]:
    prefix_text = str(prefix)
    pids: list[int] = []
    for pid, _, args in entries:
        argv = args.split()
        if len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and argv[1] == prefix_text:
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
        entries = prefix_process_snapshot(prefix, self.ps_entries())
        entries.extend(rootless_prefix_process_snapshot(prefix))
        return sorted(set(entries))

    def _kill_server(self, prefix: Path) -> None:
        pids = darlingserver_pids_for_prefix(prefix, self.ps_entries())
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

    def finalize(self, prefix: Path) -> bool:
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
        launcher = self.resolve_launcher(str(prefix))
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
                    break
                detail = process_output_text(result).strip()
                if (
                    result.returncode != 0
                    and not result.timed_out
                    and "Darling container is not running" in detail
                ):
                    self.inf(f"Darling prefix already stopped: {prefix}")
                    shutdown_ok = True
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
        # Always run the host-side cleanup oracle. A failed launcher shutdown
        # remains a failure, but skipping cleanup would leave diagnostics dirty.
        final_ok = self.finalize(prefix)
        return shutdown_ok and final_ok

    @contextmanager
    def locked(self, prefix: Path) -> Iterator[None]:
        lock_path = prefix / ".west-test.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            self.inf(f"lock Darling prefix: {prefix}")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

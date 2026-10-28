"""Darling prefix lifecycle helpers for ``west test``."""

from __future__ import annotations

import base64
import errno
import os
import hashlib
import fcntl
import resource
import shutil
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from shlex import quote
from typing import Callable, Iterable, Iterator, Mapping

try:
    from .test_guest_execution import run_guest_argv, run_guest_shell, shutdown_guest_prefix
    from .test_execution import process_output_text, run_bounded
    from .runtime_install import RuntimeInstallationError, prefix_path_is_safe, resolve_runtime
except ImportError:
    from test_guest_execution import run_guest_argv, run_guest_shell, shutdown_guest_prefix
    from test_execution import process_output_text, run_bounded
    from runtime_install import RuntimeInstallationError, prefix_path_is_safe, resolve_runtime

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


def prefix_lease_path(prefix: str | Path) -> Path:
    """Return the persistent external lease pathname for one safe guest prefix."""
    if not prefix_path_is_safe(prefix):
        raise ValueError(f"refusing lease for unsafe Darling prefix path: {prefix}")
    path = Path(prefix)
    leaf_digest = hashlib.sha256(os.fsencode(path.name)).hexdigest()
    return path.parent / f".west-prefix-lease-{leaf_digest}"


def validate_borrowed_prefix_lease(prefix: str | Path) -> int | None:
    """Accept only an inherited descriptor holding this prefix's exclusive lease."""
    marker = os.environ.get("WEST_TEST_PREFIX_LEASE")
    if marker is None:
        return None
    if marker != os.fspath(prefix):
        raise ValueError("borrowed prefix lease does not match the requested prefix")
    lease = prefix_lease_path(prefix)
    descriptor = int(os.environ.get("WEST_TEST_PREFIX_LEASE_FD", "8"))
    metadata = os.fstat(descriptor)
    expected = os.open(lease, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        selected = os.fstat(expected)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or (metadata.st_dev, metadata.st_ino) != (selected.st_dev, selected.st_ino)
        ):
            raise ValueError(f"borrowed descriptor is not the owned prefix lease: {lease}")
    finally:
        os.close(expected)
    # fdinfo describes locks on this open-file description, unlike a pathname
    # probe which could mistake a different process's lock for our own.
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as info:
        for line in info:
            fields = line.split()
            if (
                len(fields) == 9 and fields[0] == "lock:"
                and fields[2:5] == ["FLOCK", "ADVISORY", "WRITE"]
                and fields[7:] == ["0", "EOF"]
            ):
                return descriptor
    raise ValueError(f"borrowed prefix lease descriptor is not exclusively locked: {lease}")


@contextmanager
def prefix_lease(
    prefix: str | Path, *, log: Callable[[str], None] | None = None,
) -> Iterator[int]:
    """Hold the shared external lease without creating anything in the prefix."""
    borrowed = validate_borrowed_prefix_lease(prefix)
    if borrowed is not None:
        yield borrowed
        return
    lease = prefix_lease_path(prefix)
    lease.parent.mkdir(parents=True, exist_ok=True)
    if not prefix_path_is_safe(prefix):
        raise ValueError(f"refusing lock for unsafe Darling prefix path: {prefix}")
    parent_descriptor = os.open(
        lease.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        descriptor = os.open(
            lease.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=parent_descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise ValueError(f"Darling prefix lease is not a regular file owned by the current user: {lease}")
            if log is not None:
                log(f"lock Darling prefix: {prefix}")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield descriptor
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)



def _runtime_retains_prefix(process_dir: Path, prefix: Path) -> bool:
    try:
        if process_dir.stat().st_uid != os.geteuid():
            return False
        executable = os.readlink(process_dir / "exe").removesuffix(" (deleted)")
        if Path(executable).name not in {"darling", "mldr", "darlingserver"}:
            return False
        descriptors = list((process_dir / "fd").iterdir())
    except OSError:
        return False
    # Guest exec may scrub every environment marker, and the server receives
    # its prefix as a directory capability rather than a pathname argument.
    # Require the runtime executable and that exact retained directory inode;
    # cwd or a shared installation path alone cannot establish ownership.
    for descriptor in descriptors:
        try:
            if descriptor.samefile(prefix):
                return True
        except OSError:
            continue
    return False


def rootless_prefix_process_snapshot(
    prefix: Path,
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
) -> list[str]:
    """List rootless guest processes that explicitly belong to ``prefix``.

    A rootless guest can re-parent itself to init and scrub DARLING_* from its
    environment. In that case, identify the loader's retained prefix directory
    capability, not its guest argv, cwd, or shared installation path.
    """

    current_pid = os.getpid() if current_pid is None else current_pid
    if not prefix_path_is_safe(prefix):
        return []
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
        # Environment variables are inherited by host observers and can be
        # unreadable on a real runtime. Only the retained capability identifies
        # ownership, even when no environment can be inspected.
        if not _runtime_retains_prefix(process_dir, prefix):
            continue
        try:
            argv = [part.decode(errors="replace") for part in (process_dir / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            argv = []
        entries.append(f"{pid} {' '.join(argv) if argv else '[unknown]'}")
    return entries

def _prefix_marked_runtime_snapshot(prefix: Path) -> list[str]:
    """Report possible residue without granting marker-only signal authority."""
    marker = b"DARLING_PREFIX=" + os.fsencode(prefix)
    entries = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            if process_dir.stat().st_uid != os.geteuid():
                continue
            executable = os.readlink(process_dir / "exe").removesuffix(" (deleted)")
            if Path(executable).name not in {"darling", "mldr", "darlingserver"}:
                continue
            if marker not in (process_dir / "environ").read_bytes().split(b"\0"):
                continue
            argv = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            continue
        entries.append(f"{process_dir.name} {argv.strip() or '[unknown]'}")
    return entries





def cleanup_verified_prefix_processes(prefix: Path) -> RootlessPrefixCleanupResult:
    """Rescue the shared census through pinned, revalidated process handles."""
    result = RootlessPrefixCleanupResult()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        entries = prefix_process_ownership_snapshot(prefix)
        if not entries:
            return result
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            result.problems.append("verified prefix rescue unavailable: Python/Linux pidfd signaling unsupported")
            return result
        for entry in entries:
            pid = int(entry.split(" ", 1)[0])
            descriptor = None
            try:
                process = Path("/proc") / str(pid)
                before = (process / "stat").read_bytes().rsplit(b")", 1)[1].split()[19]
                descriptor = os.pidfd_open(pid)
                # The pidfd is acquired before revalidation. If this PID exited
                # and was reused at either boundary, never authorize the handle.
                after = (process / "stat").read_bytes().rsplit(b")", 1)[1].split()[19]
                if before != after:
                    continue
                if not _runtime_retains_prefix(process, prefix):
                    result.problems.append(
                        f"verified prefix rescue refused pid={pid}: no retained runtime prefix capability after pin"
                    )
                    continue
                signal.pidfd_send_signal(descriptor, sig)
                result.changed.append(
                    f"sent {signal.Signals(sig).name} through verified pidfd to prefix process {pid}"
                )
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError, IndexError) as error:
                result.problems.append(f"verified prefix rescue pid={pid} unavailable: {error}")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        time.sleep(1)
    result.problems.extend(
        f"prefix process survived verified rescue: {entry}"
        for entry in prefix_process_ownership_snapshot(prefix)
    )
    return result


def _server_owns_prefix(pid: int, args: str, prefix: Path) -> bool:
    argv = args.split()
    return bool(argv) and Path(argv[0]).name == "darlingserver" and _runtime_retains_prefix(Path("/proc") / str(pid), prefix)


def prefix_process_snapshot(prefix: Path, entries: Iterable[ProcessEntry]) -> list[str]:
    """Return the darlingserver process tree rooted at ``prefix``.

    The server retains the exact prefix directory capability. Children need
    neither markers nor that FD, so find verified server roots before walking
    the process parent graph.
    """

    if not prefix_path_is_safe(prefix):
        return []

    children: dict[int, list[int]] = {}
    args_by_pid: dict[int, str] = {}
    roots: list[int] = []
    for pid, ppid, args in entries:
        args_by_pid[pid] = args
        children.setdefault(ppid, []).append(pid)
        if _server_owns_prefix(pid, args, prefix):
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


def system_process_entries() -> list[ProcessEntry]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, check=True,
    )
    entries = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            entries.append((int(parts[0]), int(parts[1]), parts[2]))
    return entries


def prefix_process_ownership_snapshot(
    prefix: Path, entries: Iterable[ProcessEntry] | None = None,
) -> list[str]:
    """Share runtime capabilities, server descendants and local image ownership."""
    if not prefix_path_is_safe(prefix):
        raise ValueError(f"unsafe Darling prefix path: {prefix}")
    entries = system_process_entries() if entries is None else entries
    if not isinstance(entries, (list, tuple)):
        entries = tuple(entries)
    parents = {pid: ppid for pid, ppid, _ in entries}
    ancestors: set[int] = set()
    caller = os.getpid()
    while caller and caller not in ancestors:
        ancestors.add(caller)
        caller = parents.get(caller, 0)
    reports = {}
    for row in prefix_process_snapshot(prefix, entries):
        reports[int(row.split(" ", 1)[0])] = row
    for row in rootless_prefix_process_snapshot(prefix):
        reports[int(row.split(" ", 1)[0])] = row
    runtime_comms = {"mldr", "launchd", "vchroot", "shellspawn", "darlingserver"}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.geteuid():
                continue
        except OSError:
            continue
        try:
            executable = os.readlink(process / "exe").removesuffix(" (deleted)")
        except OSError:
            executable = ""
        pid = int(process.name)
        try:
            arguments = [part.decode(errors="replace") for part in (process / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            arguments = []
        if Path(executable).name == "fuse-overlayfs":
            layer = False
            for argument in arguments:
                if not argument.startswith("/"):
                    continue
                try:
                    if Path(argument).samefile(prefix):
                        layer = True
                        break
                except OSError:
                    continue
            if layer:
                reports.pop(pid, None)
                continue
        local_image = bool(executable) and (Path(executable) == prefix or prefix in Path(executable).parents)
        if not local_image:
            try:
                comm = (process / "comm").read_text().strip()
                if comm not in runtime_comms:
                    continue
                maps = (process / "maps").read_text(errors="replace")
            except OSError:
                continue
            if f" {prefix}/" not in maps:
                continue
        reports[pid] = f"{pid} {' '.join(arguments) or '[unknown]'}"
    return [reports[pid] for pid in sorted(reports) if pid not in ancestors]


def darlingserver_pids_for_prefix(prefix: Path, entries: Iterable[ProcessEntry]) -> list[int]:
    if not prefix_path_is_safe(prefix):
        return []
    pids: list[int] = []
    for pid, _, args in entries:
        if _server_owns_prefix(pid, args, prefix):
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
        return system_process_entries()

    def process_snapshot(self, prefix: Path) -> list[str]:
        return prefix_process_ownership_snapshot(prefix, self.ps_entries())


    def finalize(self, prefix: Path) -> bool:
        if not prefix_path_is_safe(prefix):
            self.err(f"refusing cleanup for unsafe Darling prefix path: {prefix}")
            return False
        prefix = Path(prefix)
        rescued = bool(self.process_snapshot(prefix))
        if rescued:
            self.err(f"product shutdown left live prefix process(es) for {prefix}; external cleanup is rescue, not PASS")
        rootless_cleanup = cleanup_verified_prefix_processes(prefix)
        if rootless_cleanup.changed:
            rescued = True
            self.err(f"external prefix rescue signalled process(es) for {prefix}; cleanup is not a product PASS")
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
        try:
            unverified = _prefix_marked_runtime_snapshot(prefix)
        except OSError as error:
            self.err(f"cannot verify residual Darling processes for {prefix}: {error}")
            return False
        if unverified:
            self.err(f"unverified runtime residue for {prefix}; prefix markers do not authorize a signal:")
            for entry in unverified:
                self.err(f"  {entry}")
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
        return socket_cleanup.success and not rescued

    def shutdown(
        self,
        prefix: Path,
        *,
        keep_running: bool = False,
        extra_env: Mapping[str, str] | None = None,
    ) -> bool:
        if not prefix_path_is_safe(prefix):
            self.err(f"refusing shutdown for unsafe Darling prefix path: {prefix}")
            return False
        prefix = Path(prefix)
        if keep_running:
            return True
        if not self.process_snapshot(prefix):
            return self.finalize(prefix)
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
                    break
                detail = process_output_text(result).strip()
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
    def locked(self, prefix: str | Path) -> Iterator[int]:
        with prefix_lease(prefix, log=self.inf) as descriptor:
            yield descriptor


# The runtime records a failed bootstrap has to hand back, and the name each one is
# retained under. They are read AFTER the guest is gone: nothing here wraps a guest,
# changes its stdout/stderr, or runs on the successful path.
BOOTSTRAP_LOG_SOURCES = (
    ("private/var/log/dserver-rpc-trace.log", "darlingserver-rpc.log"),
    ("private/var/log/dserver.log", "darlingserver.log"),
    (".west-rootless-boot.log", "rootless-boot.log"),
    ("private/var/tmp/.west-rootless-boot.log", "rootless-boot-guest.log"),
    (".west-rootless-guest-fd.log", "rootless-guest-fd.log"),
)


class PrefixLifecycleMixin:

    def _resolve_prefix(self, args) -> str | None:
        self._prefix_env = {}
        prefix = args.prefix or os.environ.get("DPREFIX")
        if prefix is None:
            return None
        raw = os.path.expanduser(os.fspath(prefix))
        if not prefix_path_is_safe(raw):
            self.die(f"unsafe Darling prefix path: {raw}")
        return raw

    def _resolve_darling_launcher(self, prefix: str | None) -> str | None:
        """Resolve the selected installation independently of writable state."""

        runtime = getattr(self, "_runtime", None)
        if runtime is not None:
            return str(runtime.launcher)
        root = getattr(self, "_runtime_root", None)
        if not (root or os.environ.get("DARLING_RUNTIME_ROOT")
                or os.environ.get("DARLING_LAUNCHER") or shutil.which("darling")):
            return None
        try:
            runtime = resolve_runtime(root)
        except RuntimeInstallationError as error:
            self.die(str(error))
        self._runtime = runtime
        return str(runtime.launcher)

    def _darling_prefix_env(self, prefix: str | Path) -> dict[str, str]:
        return self._installed_runtime().environment(prefix)

    def _verify_prefix_idle(self) -> bool:
        """Run the host-side idle oracle after a guest test claims shutdown."""

        prefix = getattr(self, "_prefix", None)
        if not prefix:
            self.err("clean-shutdown verification needs a selected Darling prefix")
            return False
        return self._prefix_lifecycle_owner().finalize(Path(prefix))

    def _ps_entries(self) -> list[tuple[int, int, str]]:
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

    def _prefix_process_snapshot(self, prefix: Path) -> list[str]:
        return self._prefix_lifecycle_owner().process_snapshot(prefix)

    def _boot_eunion_runtime_prefix(self, invocation, env, prefix: Path) -> None:
        launcher = (
            (env or {}).get("DARLING_LAUNCHER")
            or (env or {}).get("DARLING")
            or self._resolve_darling_launcher(str(prefix))
        )
        if not launcher:
            self.die(f"{invocation['name']}: darling-eunion-prefix needs a Darling launcher")

        child_env = dict(env or os.environ.copy())
        child_env.update(self._darling_prefix_env(prefix))
        timeout_seconds = self._eunion_bootstrap_timeout_seconds(invocation)
        command_prefix: tuple[str, ...] = ()
        trace_dir = getattr(self, "_bootstrap_syscall_trace", None)
        stack_sample_dir = getattr(self, "_bootstrap_stack_sample", None)
        if trace_dir is not None:
            trace_dir = self._resolve_bootstrap_diagnostic_dir(trace_dir)
            trace_dir.mkdir(parents=True, exist_ok=True)
            trace_prefix = trace_dir / "eunion-bootstrap"
            command_prefix = ("strace", "-D", "-ff", "-o", str(trace_prefix))
            self.inf(f"{invocation['name']}: E-UNION bootstrap syscall trace: {trace_dir}")
        elif stack_sample_dir is not None:
            stack_sample_dir = Path(stack_sample_dir)
            command_prefix = self._bootstrap_stack_sample_command(
                stack_sample_dir,
                sample_name="eunion-bootstrap",
                label=f"{invocation['name']}: E-UNION bootstrap",
            )
        result = run_guest_shell(
            str(launcher),
            prefix,
            "/usr/bin/true",
            cwd=Path.cwd(),
            env=child_env,
            timeout_seconds=timeout_seconds,
            capture_output=True,
            command_prefix=command_prefix,
        )
        if stack_sample_dir is not None:
            self._render_bootstrap_stack_sample(
                stack_sample_dir,
                sample_name="eunion-bootstrap",
                label=f"{invocation['name']}: E-UNION bootstrap",
            )
        diagnostic_dir = trace_dir or stack_sample_dir
        if diagnostic_dir is not None:
            self._capture_bootstrap_server_trace(
                prefix,
                diagnostic_dir,
                label=f"{invocation['name']}: E-UNION bootstrap",
            )
        if result.returncode != 0:
            output = process_output_text(result).strip()
            evidence = getattr(self, "_active_runtime_evidence", None)
            if evidence is not None:
                evidence.record_failure_detail(
                    phase="bootstrap",
                    summary="E-UNION runtime readiness executable did not reach a verdict",
                    returncode=result.returncode,
                    command=[
                        str(launcher),
                        "shell", "/bin/bash", "--login", "-c", "/usr/bin/true",
                    ],
                    output=output,
                    artifacts=[
                        Path(prefix) / ".west-rootless-boot.log",
                        Path(prefix) / "private/var/tmp/.west-rootless-boot.log",
                        Path(prefix) / ".west-rootless-guest-fd.log",
                    ],
                )
            if output:
                self.err(f"{invocation['name']}: E-UNION prefix bootstrap output:\n{output}")
            elif result.timed_out:
                diagnostic_hint = ""
                if trace_dir is not None:
                    diagnostic_hint = f"; syscall trace: {trace_dir}"
                elif stack_sample_dir is not None:
                    diagnostic_hint = f"; stack sample: {stack_sample_dir}"
                self.err(
                    f"{invocation['name']}: E-UNION runtime readiness timed out after {timeout_seconds}s "
                    f"without output{diagnostic_hint}"
                )
            self._record_failure_phase(invocation, "bootstrap")
            self.die(
                f"{invocation['name']}: failed to boot Darling E-UNION prefix "
                f"before fixture setup (rc={result.returncode})"
            )

    @contextmanager
    def _eunion_prefix_context(self, invocation, env):
        resources = set(invocation.get("requires_resources", []))
        if "darling-eunion-prefix" not in resources:
            yield
            return

        prefix_text = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{invocation['name']}: darling-eunion-prefix needs DPREFIX")
        prefix = Path(prefix_text)
        runtime = self._installed_runtime()
        namespace = Path("/usr/share/darling/tests/eunion")
        assertions = []
        symlink_assertions = []
        roots: set[Path] = set()

        def fixture_paths(guest_path):
            path = Path(str(guest_path))
            try:
                relative = path.relative_to(namespace)
            except ValueError:
                self.die(f"{invocation['name']}: E-UNION fixture path must be under {namespace}: {path}")
            if not relative.parts or ".." in relative.parts:
                self.die(f"{invocation['name']}: invalid E-UNION fixture path: {path}")
            roots.add(prefix / namespace.relative_to("/") / relative.parts[0])
            return prefix / path.relative_to("/"), runtime.system_root / path.relative_to("/")

        for spec in invocation.get("eunion_template_files", []):
            if not isinstance(spec, dict):
                self.die(f"{invocation['name']}: eunion-template-files entries must be mappings")
            upper, lower = fixture_paths(spec.get("guest-path", ""))
            if os.path.lexists(upper):
                self.die(f"{invocation['name']}: E-UNION lower fixture would be shadowed by upper file: {upper}")
            if not lower.is_file() or lower.is_symlink():
                self.die(
                    f"{invocation['name']}: installed E-UNION input is missing: {lower}; "
                    "install the optional product tests-data component before publication (--with-test-data)"
                )
            assertions.append({
                "path": lower, "contents": str(spec.get("contents", "")),
                "mode": spec.get("mode"), "xattrs": spec.get("xattrs") or {},
                "absent_xattrs": spec.get("absent-xattrs") or [],
            })
        self._verify_eunion_template_files_after(invocation, assertions)
        for spec in invocation.get("eunion_template_symlinks", []):
            if not isinstance(spec, dict):
                self.die(f"{invocation['name']}: eunion-template-symlinks entries must be mappings")
            upper, lower = fixture_paths(spec.get("guest-path", ""))
            target = str(spec.get("target", ""))
            if os.path.lexists(upper):
                self.die(f"{invocation['name']}: E-UNION symlink fixture shadowed by upper path: {upper}")
            if not lower.is_symlink() or os.readlink(lower) != target:
                self.die(f"{invocation['name']}: installed E-UNION symlink input mismatch: {lower}; install tests-data before publication")
            symlink_assertions.append((lower, target))
        upper_files = []
        for spec in invocation.get("eunion_upper_files", []):
            if not isinstance(spec, dict):
                self.die(f"{invocation['name']}: eunion-upper-files entries must be mappings")
            upper, _ = fixture_paths(spec.get("guest-path", ""))
            upper_files.append((upper, str(spec.get("contents", ""))))
        for path in invocation.get("eunion_cleanup_dirs", []):
            fixture_paths(path)
        for path in invocation.get("eunion_forbid_template_paths", []):
            fixture_paths(path)
        for path in invocation.get("eunion_require_upper_paths", []):
            fixture_paths(path)
        for root in roots:
            if os.path.lexists(root):
                self.die(f"{invocation['name']}: E-UNION case upper state already exists: {root}")

        self._shutdown_runtime_prefix(prefix)
        created_roots = []
        probe_dirs = []
        try:
            self._boot_eunion_runtime_prefix(invocation, env, prefix)
            for root in sorted(roots):
                root.mkdir(parents=True)
                created_roots.append(root)
            for upper, contents in upper_files:
                upper.parent.mkdir(parents=True, exist_ok=True)
                upper.write_text(contents)
            self._verify_eunion_runtime_prefix(invocation, env, prefix, probe_dirs)
            try:
                yield
            finally:
                self._verify_eunion_forbidden_template_paths_after(
                    invocation, runtime.system_root, invocation.get("eunion_forbid_template_paths", []),
                )
                self._verify_eunion_upper_paths_after(
                    invocation, prefix, invocation.get("eunion_require_upper_paths", []),
                )
        finally:
            self._shutdown_runtime_prefix(prefix)
            try:
                self._verify_eunion_template_files_after(invocation, assertions)
                for lower, target in symlink_assertions:
                    if not lower.is_symlink() or os.readlink(lower) != target:
                        self.die(f"{invocation['name']}: installed E-UNION symlink input changed: {lower}")
            finally:
                for root in reversed(probe_dirs + created_roots):
                    if root.is_symlink() or root.is_file():
                        root.unlink()
                    elif root.exists():
                        shutil.rmtree(root)

    def _eunion_bootstrap_timeout_seconds(self, invocation) -> int:
        override = getattr(self, "_bootstrap_timeout_seconds", None)
        if override is not None:
            return override
        return 15

    def _bootstrap_diagnostic_dir(self) -> Path | None:
        """Where this bootstrap's post-mortem records belong, tracer or not.

        An opt-in trace names its own directory; without one the owned runtime
        evidence unit already holds the run's identity and lifetime, so its
        `diagnostics/` subdirectory is the durable place -- the prefix itself is
        torn down by the failure path.
        """

        for attribute in ("_bootstrap_syscall_trace", "_bootstrap_stack_sample"):
            value = getattr(self, attribute, None)
            if value is not None:
                return self._resolve_bootstrap_diagnostic_dir(value)
        evidence = getattr(self, "_active_runtime_evidence", None)
        directory = getattr(evidence, "directory", None)
        if directory is None:
            return None
        target = Path(directory) / "diagnostics"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _resolve_bootstrap_diagnostic_dir(self, value: str | Path) -> Path:
        """Resolve diagnostic output like the command's configured working tree."""

        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(getattr(self, "topdir", Path.cwd())) / path
        return path.resolve()

    def _bootstrap_diagnostic_command_prefix(
        self, sample_name: str
    ) -> tuple[str, ...]:
        """The capture prefix EVERY bootstrap guest phase runs under, or ().

        The login shell and the guest toolchain installer are two launches of the
        same runtime, and only the second one was ever observed to hang. A capture
        wired into the first phase therefore records the wrong launch; this method
        is the single construction both phases use, and ``sample_name`` keeps their
        captures apart (``strace -ff`` appends the tid, so one base name per phase
        keeps each launch's files readable on their own).
        """

        trace_dir = getattr(self, "_bootstrap_syscall_trace", None)
        if trace_dir is not None:
            if shutil.which("strace") is None:
                self.die("--bootstrap-syscall-trace requires strace on the host")
            trace_dir = self._resolve_bootstrap_diagnostic_dir(trace_dir)
            trace_dir.mkdir(parents=True, exist_ok=True)
            self.inf(f"prefix bootstrap syscall trace: {trace_dir}")
            return (
                "strace",
                "-D",
                "-ff",
                "-i",
                "-tt",
                "-v",
                "-s",
                "160",
                "-o",
                str(trace_dir / sample_name),
            )
        stack_sample_dir = getattr(self, "_bootstrap_stack_sample", None)
        if stack_sample_dir is not None:
            sample_dir = self._resolve_bootstrap_diagnostic_dir(stack_sample_dir)
            return self._bootstrap_stack_sample_command(
                sample_dir,
                sample_name=sample_name,
                label="prefix bootstrap",
            )
        return ()

    def _bootstrap_stack_sample_command(
        self,
        sample_dir: Path,
        *,
        sample_name: str,
        label: str,
    ) -> tuple[str, ...]:
        if shutil.which("perf") is None:
            self.die("--bootstrap-stack-sample requires perf on the host")
        sample_dir.mkdir(parents=True, exist_ok=True)
        self.inf(f"{label} stack sample: {sample_dir}")
        return (
            "perf",
            "record",
            "--all-user",
            "--call-graph",
            "fp",
            "--output",
            str(sample_dir / f"{sample_name}.perf.data"),
            "--",
        )

    def _render_bootstrap_stack_sample(
        self,
        sample_dir: Path,
        *,
        sample_name: str,
        label: str,
    ) -> None:
        sample_data = sample_dir / f"{sample_name}.perf.data"
        if not sample_data.is_file():
            self.die(f"{label} stack sample was not written: {sample_data}")
        rendered_sample = self._run_bounded(
            ["perf", "script", "--input", str(sample_data)],
            cwd=Path(self.topdir),
            env=None,
            timeout_seconds=30,
            capture_output=True,
        )
        if rendered_sample.timed_out or rendered_sample.returncode != 0:
            self.die(f"{label} stack sample could not be rendered: {sample_data}")
        (sample_dir / f"{sample_name}.perf.txt").write_text(
            f"{rendered_sample.stdout}{rendered_sample.stderr}"
        )

    def _capture_bootstrap_server_trace(
        self,
        prefix: Path,
        diagnostic_dir: Path,
        *,
        label: str,
    ) -> None:
        """Copy the runtime's own records out of the prefix before teardown.

        Two different server records answer two different questions: the RPC trace says which requests crossed,
        `dserver.log` carries the runtime's own tags (doorbell routes, lane attaches, refused/armed plane calls)
        that name WHY one did not. The launcher and guest boot logs say whether a guest was ever started at all.
        None of them survives the prefix teardown, and taking them is a post-mortem read, not instrumentation:
        no guest is wrapped, and the workload's stdout/stderr is not touched.

        A source that is not there is REPORTED with its reason (an absent instrument is not a zero), so a
        retained unit never implies a record it does not hold.
        """

        for source_name, captured_name in BOOTSTRAP_LOG_SOURCES:
            source = prefix / source_name
            captured = diagnostic_dir / captured_name
            try:
                if not source.exists():
                    self.inf(f"{label} {captured_name}: missing ({source_name}: absent)")
                    continue
                if source.is_dir():
                    self.inf(f"{label} {captured_name}: missing ({source_name}: a directory)")
                    continue
                if captured.exists():
                    captured.unlink()
                shutil.copy2(source, captured)
            except OSError as error:
                self.inf(
                    f"{label} {captured_name}: missing ({source_name}: {error.strerror or error})"
                )
                continue
            self.inf(f"{label} {captured_name}: retained {captured}")

        # The guest's own prerequisites are part of a failure's record: a package
        # extractor reports "Archive parent is not a directory: /tmp" and the prefix
        # is torn down before anyone can look. MEASURED need (gate run 1547): the
        # question "was /tmp absent, a file, or a symlink to a missing target" had
        # no answer after the run.
        tmp_report = diagnostic_dir / "prefix-tmp-state.txt"
        lines = []
        for rel in ("tmp", "private/tmp", "private/var/tmp", "var/tmp", "var/run"):
            path = prefix / rel
            try:
                status = path.lstat()
            except FileNotFoundError:
                lines.append(f"{rel}: absent")
                continue
            except OSError as error:
                lines.append(f"{rel}: unreadable ({error.strerror or error})")
                continue
            mode = oct(stat.S_IMODE(status.st_mode))
            if stat.S_ISLNK(status.st_mode):
                target = os.readlink(path)
                try:
                    resolved = path.resolve(strict=True)
                    state = "resolves"
                    resolved_kind = "dir" if resolved.is_dir() else ("file" if resolved.is_file() else "other")
                except OSError as error:
                    state = f"BROKEN ({error.strerror or error})"
                    resolved_kind = "-"
                lines.append(f"{rel}: symlink -> {target} {state} target_kind={resolved_kind} mode={mode}")
            elif stat.S_ISDIR(status.st_mode):
                lines.append(f"{rel}: dir mode={mode}")
            elif stat.S_ISREG(status.st_mode):
                lines.append(f"{rel}: FILE mode={mode}")
            else:
                lines.append(f"{rel}: other mode={mode}")
        try:
            tmp_report.write_text("\n".join(lines) + "\n")
        except OSError as error:
            self.inf(f"{label} prefix-tmp-state.txt: missing (write: {error.strerror or error})")
        else:
            self.inf(f"{label} prefix-tmp-state.txt: retained {tmp_report}")

        launchd_dir = prefix / "var/log/com.apple.launchd"
        if not launchd_dir.is_dir():
            self.inf(f"{label} launchd logs: missing (var/log/com.apple.launchd: absent)")
            return
        for source in sorted(launchd_dir.iterdir()):
            if not source.is_file():
                continue
            captured = diagnostic_dir / f"launchd-{source.name}"
            try:
                if captured.exists():
                    captured.unlink()
                shutil.copy2(source, captured)
            except OSError as error:
                self.inf(
                    f"{label} {captured.name}: missing ({source.name}: {error.strerror or error})"
                )
                continue
            self.inf(f"{label} {captured.name}: retained {captured}")

    def _parse_file_mode(self, invocation, field: str, index: int, value) -> int:
        try:
            if isinstance(value, int):
                return value
            return int(str(value), 8)
        except (TypeError, ValueError):
            self.die(f"{invocation['name']}: {field}[{index}] has invalid mode: {value!r}")

    def _verify_eunion_template_files_after(self, invocation, assertions) -> None:
        for assertion in assertions:
            path = assertion["path"]
            expected = assertion["contents"]
            try:
                got = path.read_text()
            except FileNotFoundError:
                self.die(f"{invocation['name']}: E-UNION template fixture was removed: {path}")
            if got != expected:
                self.die(f"{invocation['name']}: E-UNION template fixture was modified: {path}")
            if assertion.get("mode") is not None:
                expected_mode = self._parse_file_mode(invocation, "eunion-template-files", 0, assertion["mode"])
                actual_mode = path.stat().st_mode & 0o7777
                if actual_mode != expected_mode:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture mode changed: "
                        f"{path} got {actual_mode:o} want {expected_mode:o}"
                    )
            for name, expected_value in assertion.get("xattrs", {}).items():
                if isinstance(expected_value, str):
                    expected_bytes = expected_value.encode()
                elif (
                    isinstance(expected_value, dict)
                    and set(expected_value) == {"base64"}
                    and isinstance(expected_value["base64"], str)
                ):
                    try:
                        expected_bytes = base64.b64decode(expected_value["base64"], validate=True)
                    except ValueError as exc:
                        self.die(
                            f"{invocation['name']}: invalid E-UNION fixture xattr encoding "
                            f"{name} on {path}: {exc}"
                        )
                else:
                    self.die(
                        f"{invocation['name']}: E-UNION fixture xattr {name} on {path} "
                        "must be text or a mapping containing only base64"
                    )
                try:
                    got_value = os.getxattr(path, name.encode())
                except OSError as exc:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture xattr missing "
                        f"{name} on {path}: {exc}"
                    )
                if got_value != expected_bytes:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture xattr changed: "
                        f"{path} {name} got {got_value!r} want {expected_bytes!r}"
                    )
            for name in assertion.get("absent_xattrs", []):
                try:
                    got_value = os.getxattr(path, name.encode())
                except OSError as exc:
                    if exc.errno in (errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)):
                        continue
                    self.die(
                        f"{invocation['name']}: cannot verify absent E-UNION template xattr "
                        f"{name} on {path}: {exc}"
                    )
                self.die(
                    f"{invocation['name']}: E-UNION template fixture xattr was added: "
                    f"{path} {name}={got_value!r}"
                )

    def _verify_eunion_forbidden_template_paths_after(
        self,
        invocation,
        system_root: Path,
        guest_paths: list[str],
    ) -> None:
        for guest_path in guest_paths:
            rel = Path(str(guest_path).lstrip("/"))
            lower_path = system_root / rel
            if os.path.lexists(lower_path):
                self.die(
                    f"{invocation['name']}: forbidden E-UNION template path was created: "
                    f"{guest_path} ({lower_path})"
                )

    def _verify_eunion_upper_paths_after(
        self,
        invocation,
        prefix: Path,
        guest_paths: list[str],
    ) -> None:
        for guest_path in guest_paths:
            rel = Path(str(guest_path).lstrip("/"))
            upper_path = prefix / rel
            if not os.path.lexists(upper_path):
                self.die(
                    f"{invocation['name']}: required E-UNION upper path is missing: "
                    f"{guest_path} ({upper_path})"
                )

    def _verify_eunion_runtime_prefix(self, invocation, env, prefix: Path, probe_dirs) -> None:
        launcher = (
            (env or {}).get("DARLING_LAUNCHER")
            or (env or {}).get("DARLING")
            or self._resolve_darling_launcher(str(prefix))
        )
        if not launcher:
            self.die(f"{invocation['name']}: darling-eunion-prefix needs a Darling launcher")

        guest_dir = "/usr/share/darling/tests/eunion/probe"
        upper_dir = prefix / guest_dir.lstrip("/")
        lower_dir = self._installed_runtime().system_root / guest_dir.lstrip("/")
        if os.path.lexists(upper_dir):
            self.die(f"{invocation['name']}: E-UNION probe upper state already exists: {upper_dir}")
        try:
            probe_inputs = ((lower_dir / "lower.txt").read_text(), (lower_dir / "shadow.txt").read_text())
        except OSError as error:
            self.die(f"{invocation['name']}: installed E-UNION probe inputs missing; install tests-data before publication: {error}")
        if probe_inputs != ("LOWER\n", "LOWER_SHADOW\n"):
            self.die(f"{invocation['name']}: installed E-UNION probe inputs mismatch; install tests-data before publication")
        upper_dir.mkdir(parents=True)
        probe_dirs.append(upper_dir)
        (upper_dir / "upper.txt").write_text("UPPER\n")
        (upper_dir / "shadow.txt").write_text("UPPER_SHADOW\n")

        child_env = dict(env or os.environ.copy())
        child_env.update(self._darling_prefix_env(prefix))
        script = (
            "set -e; "
            f"test \"$(cat {quote(guest_dir + '/lower.txt')})\" = LOWER; "
            f"test \"$(cat {quote(guest_dir + '/upper.txt')})\" = UPPER; "
            f"test \"$(cat {quote(guest_dir + '/shadow.txt')})\" = UPPER_SHADOW"
        )
        output_path = upper_dir / "probe-output.txt"
        with output_path.open("w+") as output:
            result = run_guest_shell(
                str(launcher),
                prefix,
                script,
                cwd=Path.cwd(),
                env=child_env,
                timeout_seconds=15,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        if result.returncode != 0:
            output = output_path.read_text(errors="replace").strip()
            if output:
                self.err(output)
            self.die(
                f"{invocation['name']}: Darling prefix is not running as an "
                "active E-UNION upper-over-template root; upper/lower probe failed"
            )

    @staticmethod
    def _bootstrap_runtime_state(prefix: Path) -> str:
        """Capture rootless state before prefix cleanup removes the evidence."""

        lines = ["--- bootstrap runtime state ---", "rootless processes:"]
        processes = rootless_prefix_process_snapshot(prefix)
        lines.extend(processes or ["<none>"])
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        lines.extend((f"RLIMIT_NOFILE soft={soft} hard={hard}",))
        nr_open = Path("/proc/sys/fs/nr_open")
        if nr_open.is_file():
            lines.append(f"/proc/sys/fs/nr_open={nr_open.read_text().strip()}")
        lines.append("runtime paths:")
        for relative in (
            ".darlingserver.stat.sock",
            "var/run/shellspawn.sock",
            "var/tmp/launchd/sock",
            ".west-rootless-boot.log",
            ".west-rootless-guest-fd.log",
            "private/var/tmp/.west-rootless-boot.log",
            "private/var/log/dserver-rpc-trace.log",
        ):
            path = prefix / relative
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                lines.append(f"{relative}: absent")
                continue
            kind = "socket" if stat.S_ISSOCK(mode) else "file"
            lines.append(f"{relative}: {kind} mode={oct(stat.S_IMODE(mode))}")
        return "\n".join(lines)


def _lease_exec() -> None:
    """Exec a shell lifecycle owner with lease FD 8; runtime children close it."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Hold one prefix lease while executing its lifecycle owner.")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--list-owned", action="store_true")
    parser.add_argument(
        "--list-residual", action="store_true",
        help="read-only residual PID census, including unverified markers; not signal authority",
    )
    parser.add_argument("--validate-lease", action="store_true")
    parser.add_argument("--rescue", action="store_true",
                        help="verified pidfd rescue: 0=no action, 3=rescued, 2=unavailable, 4=rescue incomplete")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command and not args.list_owned and not args.list_residual and not args.validate_lease and not args.rescue:
        parser.error("a command is required after --")
    if not prefix_path_is_safe(args.prefix):
        parser.error(f"unsafe Darling prefix path: {args.prefix}")
    if args.rescue:
        if command or args.list_owned or args.list_residual or args.validate_lease:
            parser.error("--rescue cannot execute a command or select another operation")
        try:
            with prefix_lease(args.prefix):
                result = cleanup_verified_prefix_processes(Path(args.prefix))
            for message in result.changed + result.problems:
                print(f"PREFIX-RESCUE {message}", file=sys.stderr)
            raise SystemExit((4 if result.changed else 2) if not result.success else 3 if result.changed else 0)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"prefix rescue unavailable: {error}", file=sys.stderr)
            raise SystemExit(2)
    if args.list_owned or args.list_residual:
        if command or (args.list_owned and args.list_residual):
            parser.error("select one process census without a command")
        try:
            rows = prefix_process_ownership_snapshot(Path(args.prefix))
            if args.list_residual:
                rows += _prefix_marked_runtime_snapshot(Path(args.prefix))
            for pid in sorted({int(row.split(" ", 1)[0]) for row in rows}):
                print(pid)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"prefix census unavailable: {error}", file=sys.stderr)
            raise SystemExit(2)
        return
    if args.validate_lease:
        if command or args.list_owned or args.list_residual:
            parser.error("--validate-lease cannot execute a command or list processes")
        try:
            if validate_borrowed_prefix_lease(args.prefix) is None:
                raise ValueError("no inherited prefix lease was supplied")
        except (OSError, ValueError) as error:
            parser.error(str(error))
        return
    try:
        with prefix_lease(args.prefix) as descriptor:
            if descriptor == 8:
                os.set_inheritable(descriptor, True)
            else:
                os.dup2(descriptor, 8, inheritable=True)
                os.set_inheritable(descriptor, False)
            environment = dict(os.environ, WEST_TEST_PREFIX_LEASE=args.prefix, WEST_TEST_PREFIX_LEASE_FD="8")
            os.execvpe(command[0], command, environment)
    except (OSError, ValueError) as error:
        print(f"prefix lease: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    _lease_exec()

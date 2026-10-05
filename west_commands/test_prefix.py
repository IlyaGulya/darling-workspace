"""Darling prefix lifecycle helpers for ``west test``."""

from __future__ import annotations

import os
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
except ImportError:
    from test_guest_execution import run_guest_argv, run_guest_shell, shutdown_guest_prefix
    from test_execution import process_output_text, run_bounded

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


def _runtime_retains_prefix(process_dir: Path, prefix: Path) -> bool:
    try:
        executable = os.readlink(process_dir / "exe").removesuffix(" (deleted)")
        if Path(executable).name not in {"mldr", "darlingserver"}:
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
        owns_prefix = rootless_marker in environment and prefix_marker in environment
        if not owns_prefix:
            owns_prefix = _runtime_retains_prefix(process_dir, prefix)
        if not owns_prefix and rootless_marker in environment:
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
    """Terminate only rootless processes with this prefix's ownership evidence."""

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


def _server_owns_prefix(pid: int, args: str, prefix: Path) -> bool:
    argv = args.split()
    return len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and (
        argv[1] == str(prefix) or _runtime_retains_prefix(Path("/proc") / str(pid), prefix)
    )


def prefix_process_snapshot(prefix: Path, entries: Iterable[ProcessEntry]) -> list[str]:
    """Return the darlingserver process tree rooted at ``prefix``.

    The server carries either a prefix pathname argument or a retained directory
    capability. Children need neither, so find server roots before walking the
    process parent graph.
    """

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


def darlingserver_pids_for_prefix(prefix: Path, entries: Iterable[ProcessEntry]) -> list[int]:
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


class PrefixLifecycleMixin:

    def _resolve_prefix(self, args) -> str | None:
        self._prefix_env = {}
        if args.no_overlayfs:
            self._prefix_env["DARLING_NOOVERLAYFS"] = "1"
        if args.prefix and args.prefix_profile:
            self.die("--prefix and --prefix-profile are mutually exclusive")
        prefix = None
        if args.prefix:
            prefix = args.prefix
            if prefix.startswith("existing:"):
                prefix = prefix.removeprefix("existing:")
        elif args.prefix_profile:
            profiles = {
                "homebrew": "~/work/darling-prefix-homebrew-test",
                "smoke": "~/work/darling-prefix-smoke",
            }
            if args.prefix_profile == "homebrew":
                self._prefix_env["DARLING_NOOVERLAYFS"] = "1"
            prefix = profiles.get(args.prefix_profile, args.prefix_profile)
        elif os.environ.get("DPREFIX"):
            prefix = os.environ["DPREFIX"]
        if prefix is None:
            return None
        resolved = str(Path(prefix).expanduser())
        self._load_retained_prefix_env(Path(resolved))
        return resolved

    def _resolve_darling_launcher(self, prefix: str | None) -> str | None:
        """Resolve the launcher for one prefix, or for the ambient environment.

        Shared by every command that must own or stop a prefix, so the two paths cannot drift: an explicit
        prefix is a runtime identity, and falling back to another prefix's launcher would silently mix
        launcher and DPREFIX.
        """

        if prefix:
            candidate = Path(prefix).expanduser() / "bin" / "darling"
            if candidate.exists():
                return str(candidate)
            # An explicit prefix is a runtime identity, not just an artifact
            # directory. Falling back to another prefix's launcher silently
            # mixes launcher and DPREFIX, which can make a broken named prefix
            # appear usable for one test lifecycle.
            return None
        if os.environ.get("DARLING"):
            return os.environ["DARLING"]
        if os.environ.get("DARLING_LAUNCHER"):
            return os.environ["DARLING_LAUNCHER"]
        candidate = Path("~/work/darling-prefix/bin/darling").expanduser()
        if candidate.exists():
            return str(candidate)
        return None

    def _darling_prefix_env(self, prefix: str | Path) -> dict[str, str]:
        prefix_text = str(prefix)
        env = {
            "DPREFIX": prefix_text,
            "DARLING_PREFIX": prefix_text,
        }
        env.update(getattr(self, "_prefix_env", {}))
        return env

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
        result = self._run_guest_argv(
            str(launcher),
            prefix,
            ["/usr/bin/true"],
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
                        "exec",
                        "/usr/bin/true",
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
        marker = prefix / ".union-work"
        created_marker = False
        created_template_files: list[Path] = []
        created_template_symlinks: list[Path] = []
        created_template_dirs: list[Path] = []
        created_upper_files: list[Path] = []
        created_upper_dirs: list[Path] = []
        cleanup_dirs: list[tuple[Path, Path]] = []
        template_assertions: list[dict] = []
        forbidden_template_paths = list(invocation.get("eunion_forbid_template_paths", []))
        required_upper_paths = list(invocation.get("eunion_require_upper_paths", []))
        probe_dirs: list[tuple[Path, Path]] = []
        blocked_upper_files: list[Path] = []

        def cleanup_fixture_state() -> None:
            for path in reversed(created_upper_files):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(created_template_files):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(created_template_symlinks):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for path in reversed(created_upper_dirs):
                try:
                    path.rmdir()
                except OSError:
                    pass
            for path in reversed(created_template_dirs):
                try:
                    path.rmdir()
                except OSError:
                    pass
            for upper_dir, lower_dir in reversed(cleanup_dirs):
                shutil.rmtree(upper_dir, ignore_errors=True)
                shutil.rmtree(lower_dir, ignore_errors=True)
            for upper_dir, lower_dir in reversed(probe_dirs):
                shutil.rmtree(upper_dir, ignore_errors=True)
                shutil.rmtree(lower_dir, ignore_errors=True)
            if created_marker:
                try:
                    marker.rmdir()
                except OSError:
                    self.err(f"{invocation['name']}: preserving non-empty E-UNION marker {marker}")

        try:
            if marker.exists() and not marker.is_dir():
                self.die(f"{invocation['name']}: E-UNION marker is not a directory: {marker}")
            if not marker.exists():
                marker.mkdir(parents=True, mode=0o700)
                created_marker = True

            for index, guest_path in enumerate(invocation.get("eunion_cleanup_dirs", [])):
                guest_path = str(guest_path)
                if (
                    not guest_path.startswith("/private/var/tmp/west-")
                    or ".." in Path(guest_path).parts
                ):
                    self.die(
                        f"{invocation['name']}: eunion-cleanup-dirs[{index}] needs "
                        "an absolute /private/var/tmp/west-* guest path without '..'"
                    )
                rel = Path(guest_path.lstrip("/"))
                cleanup_dirs.append((prefix / rel, prefix / "libexec/darling" / rel))

            self._shutdown_runtime_prefix(prefix)
            self._boot_eunion_runtime_prefix(invocation, env, prefix)

            for index, spec in enumerate(invocation.get("eunion_template_files", [])):
                if not isinstance(spec, dict):
                    self.die(f"{invocation['name']}: eunion-template-files entries must be mappings")
                guest_path = str(spec.get("guest-path", ""))
                if not guest_path.startswith("/") or ".." in Path(guest_path).parts:
                    self.die(
                        f"{invocation['name']}: eunion-template-files[{index}] needs "
                        "an absolute guest-path without '..'"
                    )
                rel = Path(guest_path.lstrip("/"))
                upper_path = prefix / rel
                lower_path = prefix / "libexec/darling" / rel
                if upper_path.exists():
                    blocked_upper_files.append(upper_path)
                    continue
                created_template_dirs.extend(
                    self._mkdirs_for_fixture(lower_path.parent, prefix / "libexec/darling")
                )
                if not lower_path.exists():
                    lower_path.write_text(str(spec.get("contents", "")))
                    created_template_files.append(lower_path)
                if "mode" in spec:
                    lower_path.chmod(self._parse_file_mode(invocation, "eunion-template-files", index, spec["mode"]))
                for name, value in (spec.get("xattrs") or {}).items():
                    try:
                        os.setxattr(lower_path, str(name).encode(), str(value).encode())
                    except OSError as exc:
                        self.die(
                            f"{invocation['name']}: failed to set E-UNION template xattr "
                            f"{name} on {lower_path}: {exc}"
                        )
                template_assertions.append(
                    {
                        "path": lower_path,
                        "contents": str(spec.get("contents", "")),
                        "mode": spec.get("mode"),
                        "xattrs": {str(k): str(v) for k, v in (spec.get("xattrs") or {}).items()},
                        "absent_xattrs": [str(item) for item in spec.get("absent-xattrs", [])],
                    }
                )
            if blocked_upper_files:
                self.die(
                    f"{invocation['name']}: E-UNION lower fixture would be shadowed by "
                    f"upper file(s): {', '.join(str(path) for path in blocked_upper_files)}"
                )

            for index, spec in enumerate(invocation.get("eunion_template_symlinks", [])):
                if not isinstance(spec, dict):
                    self.die(f"{invocation['name']}: eunion-template-symlinks entries must be mappings")
                guest_path = str(spec.get("guest-path", ""))
                target = str(spec.get("target", ""))
                if not guest_path.startswith("/") or ".." in Path(guest_path).parts:
                    self.die(
                        f"{invocation['name']}: eunion-template-symlinks[{index}] needs "
                        "an absolute guest-path without '..'"
                    )
                allow_parent_target = bool(spec.get("allow-parent-target", False))
                if not target or target.startswith("/") or (
                    not allow_parent_target and ".." in Path(target).parts
                ):
                    self.die(
                        f"{invocation['name']}: eunion-template-symlinks[{index}] needs "
                        "a non-empty relative target without '..' unless "
                        "allow-parent-target is true"
                    )
                rel = Path(guest_path.lstrip("/"))
                upper_path = prefix / rel
                lower_path = prefix / "libexec/darling" / rel
                if upper_path.exists() or upper_path.is_symlink():
                    self.die(f"{invocation['name']}: E-UNION symlink fixture shadowed by upper path: {upper_path}")
                created_template_dirs.extend(
                    self._mkdirs_for_fixture(lower_path.parent, prefix / "libexec/darling")
                )
                if lower_path.exists() or lower_path.is_symlink():
                    self.die(f"{invocation['name']}: E-UNION symlink fixture already exists: {lower_path}")
                lower_path.symlink_to(target)
                created_template_symlinks.append(lower_path)

            for index, spec in enumerate(invocation.get("eunion_upper_files", [])):
                if not isinstance(spec, dict):
                    self.die(f"{invocation['name']}: eunion-upper-files entries must be mappings")
                guest_path = str(spec.get("guest-path", ""))
                if not guest_path.startswith("/") or ".." in Path(guest_path).parts:
                    self.die(
                        f"{invocation['name']}: eunion-upper-files[{index}] needs "
                        "an absolute guest-path without '..'"
                    )
                upper_path = prefix / guest_path.lstrip("/")
                if upper_path.exists():
                    self.die(f"{invocation['name']}: E-UNION upper fixture already exists: {upper_path}")
                created_upper_dirs.extend(self._mkdirs_for_fixture(upper_path.parent, prefix))
                upper_path.write_text(str(spec.get("contents", "")))
                created_upper_files.append(upper_path)
            self._verify_eunion_runtime_prefix(invocation, env, prefix, probe_dirs)
        except BaseException:
            self._shutdown_runtime_prefix(prefix)
            cleanup_fixture_state()
            raise

        try:
            yield
        finally:
            try:
                self._verify_eunion_forbidden_template_paths_after(
                    invocation,
                    prefix,
                    forbidden_template_paths,
                )
                self._verify_eunion_upper_paths_after(
                    invocation,
                    prefix,
                    required_upper_paths,
                )
            finally:
                self._shutdown_runtime_prefix(prefix)
                try:
                    if invocation.get("eunion_verify_template_files_after"):
                        self._verify_eunion_template_files_after(invocation, template_assertions)
                finally:
                    cleanup_fixture_state()

    def _eunion_bootstrap_timeout_seconds(self, invocation) -> int:
        override = getattr(self, "_bootstrap_timeout_seconds", None)
        if override is not None:
            return override
        profile_name = invocation.get("runtime-profile")
        if profile_name:
            definition = self._ctest_runtime_profile_definitions().get(profile_name)
            if definition is not None:
                return int(definition["bootstrap-smoke-timeout-seconds"])
        return 15

    def _resolve_bootstrap_diagnostic_dir(self, value: str | Path) -> Path:
        """Resolve diagnostic output like the command's configured working tree."""

        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(getattr(self, "topdir", Path.cwd())) / path
        return path.resolve()

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
        server_trace = prefix / "private/var/log/dserver-rpc-trace.log"
        if not server_trace.is_file():
            return
        captured_server_trace = diagnostic_dir / "darlingserver-rpc.log"
        shutil.copy2(server_trace, captured_server_trace)
        self.inf(f"{label} server trace: {captured_server_trace}")

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
                try:
                    got_value = os.getxattr(path, name.encode()).decode()
                except OSError as exc:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture xattr missing "
                        f"{name} on {path}: {exc}"
                    )
                if got_value != expected_value:
                    self.die(
                        f"{invocation['name']}: E-UNION template fixture xattr changed: "
                        f"{path} {name} got {got_value!r} want {expected_value!r}"
                    )
            for name in assertion.get("absent_xattrs", []):
                try:
                    got_value = os.getxattr(path, name.encode())
                except OSError:
                    continue
                self.die(
                    f"{invocation['name']}: E-UNION template fixture xattr was added: "
                    f"{path} {name}={got_value!r}"
                )

    def _verify_eunion_forbidden_template_paths_after(
        self,
        invocation,
        prefix: Path,
        guest_paths: list[str],
    ) -> None:
        for guest_path in guest_paths:
            rel = Path(str(guest_path).lstrip("/"))
            lower_path = prefix / "libexec/darling" / rel
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

        name = f"west-eunion-probe-{os.getpid()}-{int(time.time() * 1000)}"
        guest_dir = f"/private/var/tmp/{name}"
        upper_dir = prefix / "private/var/tmp" / name
        lower_dir = prefix / "libexec/darling/private/var/tmp" / name
        upper_dir.mkdir(parents=True)
        lower_dir.mkdir(parents=True)
        probe_dirs.append((upper_dir, lower_dir))
        (lower_dir / "lower.txt").write_text("LOWER\n")
        (lower_dir / "shadow.txt").write_text("LOWER_SHADOW\n")
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
        output_path = lower_dir / "probe-output.txt"
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

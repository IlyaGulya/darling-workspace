"""Darling prefix prerequisite checks and repair helpers."""

from __future__ import annotations

import os
import errno
import signal
import stat
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


TMP_RELS = ("private/var/tmp",)
PRIVATE_TMP_RELS = ("private/tmp",)
ROOT_TMP_RELS = ("tmp",)
ROOTLESS_RUNTIME_DIR_RELS = (
    "private/var/db/launchd.db/com.apple.launchd",
    "var/run",
    "var/tmp",
)
CANONICAL_CLT_REL = Path("Library/Developer/CommandLineTools")
DARLING_CLT_CLANG_REL = Path("Library/Developer/DarlingCLT/usr/bin/clang")
DARLING_CLT_CLANG_TARGET = Path("../../../CommandLineTools/usr/bin/clang")
INIT_PID_REL = Path(".init.pid")
SERVER_SOCKET_REL = Path(".darlingserver.sock")
@dataclass
class PrefixRepairResult:
    changed: list[str] = field(default_factory=list)
    ok: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def extend(self, other: "PrefixRepairResult") -> None:
        self.changed.extend(other.changed)
        self.ok.extend(other.ok)
        self.problems.extend(other.problems)

    @property
    def success(self) -> bool:
        return not self.problems


def prefix_roots(
    prefix: Path, system_root: Path | None = None
) -> list[tuple[str, Path]]:
    """Guest-visible roots holding writable state and immutable runtime files.

    The writable prefix is always first. A selected installation's system root is
    added only for *reads* (a guest toolchain may legitimately come from the
    immutable image); repair never writes there.
    """

    roots = [("prefix root", prefix)]
    if system_root is not None and Path(system_root) != Path(prefix):
        roots.append(("runtime system root", Path(system_root)))
    return roots


class PrefixBootPhase(Enum):
    PREPARED = "prepared"
    READY = "ready"


def prefix_boot_phase(prefix: Path) -> PrefixBootPhase:
    """Recognize a typed, identity-bound prefix that has not published a runtime."""
    state = prefix / ".darling-prefix-state-v2"
    if state.is_symlink() or os.path.lexists(prefix / INIT_PID_REL):
        return PrefixBootPhase.READY
    try:
        lines = state.read_text().splitlines()
        if len(lines) != 9 or lines[0] != "DARLING_PREFIX_STATE_V2":
            return PrefixBootPhase.READY
        fields = dict(line.split("=", 1) for line in lines[1:])
        identity = prefix.stat()
        if (
            len(fields) == 8
            and fields["schema_version"] == "2"
            and fields["runtime_mode"] == "rootless-eunion"
            and int(fields["generation"]) > 0
            and int(fields["prefix_device"]) == identity.st_dev
            and int(fields["prefix_inode"]) == identity.st_ino
            and int(fields["owner_uid"]) == identity.st_uid
            and int(fields["owner_gid"]) == identity.st_gid
            and fields["provenance"]
        ):
            return PrefixBootPhase.PREPARED
    except (OSError, UnicodeError, ValueError, KeyError):
        pass
    return PrefixBootPhase.READY


def prefix_boot_prerequisite_problems(
    prefix: Path, *, phase: PrefixBootPhase = PrefixBootPhase.READY
) -> list[str]:
    """Check ready-state postconditions unless first-boot preparation is explicit."""
    problems = []
    temporary_dirs = (*TMP_RELS, *PRIVATE_TMP_RELS, *ROOT_TMP_RELS)
    for rel in (*temporary_dirs, *ROOTLESS_RUNTIME_DIR_RELS):
        path = prefix / rel
        if not path.is_dir():
            if phase is PrefixBootPhase.PREPARED and all(
                not os.path.lexists(parent) or parent.is_dir()
                for parent in (path, *path.parents)
            ):
                continue
            problems.append(f"{rel} missing in Darling prefix")
            continue
        if rel in temporary_dirs:
            mode = path.stat().st_mode & 0o7777
            if mode != 0o1777:
                problems.append(f"{rel} mode {mode:o}, expected 1777")
    return problems


EUNION_BINARY_MARKERS = (
    b"/.union-work",
    b"user.union.whiteout",
    b"user.union.opaque",
)


def eunion_prefix_prerequisite_problems(
    prefix: Path, *, system_root: Path | None = None
) -> list[str]:
    """Check the installed runtime's kernel image without a legacy combined-root fallback."""

    problems: list[str] = []
    roots = [root for _name, root in prefix_roots(prefix, system_root)]
    kernels = [root / "usr/lib/system/libsystem_kernel.dylib" for root in roots]
    kernels = [kernel for kernel in kernels if kernel.is_file()]
    if not kernels:
        problems.append("libsystem_kernel.dylib missing in guest filesystem or runtime system root")
        return problems
    supported = False
    for kernel in kernels:
        try:
            data = kernel.read_bytes()
        except OSError as error:
            problems.append(f"cannot read {kernel}: {error}")
            continue
        if all(marker in data for marker in EUNION_BINARY_MARKERS):
            supported = True
            break
    if not supported:
        problems.append(
            "installed libsystem_kernel.dylib lacks E-UNION markers; rebuild "
            "Darling with -DDARLING_EUNION=ON and install a reviewed runtime"
        )
    return problems


def guest_c_fixture_prerequisite_problems(
    prefix: Path,
    guest_cc: str,
    guest_cflags: str,
    *,
    system_root: Path | None = None,
) -> list[str]:
    """Read guest prerequisites from writable upper state or the selected image.

    Missing upper paths may be supplied by the installed lower image; neither
    this check nor repair requires a duplicate copy in both roots.
    """
    problems = []

    def check_guest_path(guest_path: str, description: str):
        rel = guest_path.lstrip("/")
        roots = prefix_roots(prefix, system_root)
        for _root_name, root in roots:
            host_path = root / rel
            if host_path.exists():
                return
            # An upper entry shadows the lower image, including dangling links.
            if host_path.is_symlink():
                break
        problems.append(f"{description} missing in selected guest filesystem: {guest_path}")

    if guest_cc.startswith("/Library/Developer/CommandLineTools/"):
        check_guest_path(guest_cc, "guest compiler")
    words = guest_cflags.split()
    for index, word in enumerate(words):
        if word == "-isysroot" and index + 1 < len(words):
            sysroot = words[index + 1]
            if sysroot.startswith("/Library/Developer/CommandLineTools/"):
                check_guest_path(sysroot, "guest SDK sysroot")
    return problems


def _decode_mountinfo_path(path: str) -> str:
    out = []
    index = 0
    while index < len(path):
        if (
            path[index] == "\\"
            and index + 3 < len(path)
            and all(ch in "01234567" for ch in path[index + 1:index + 4])
        ):
            out.append(chr(int(path[index + 1:index + 4], 8)))
            index += 4
            continue
        out.append(path[index])
        index += 1
    return "".join(out)


def prefix_mount_targets(
    prefix: Path,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> list[Path]:
    prefix = prefix.resolve()
    targets = []
    try:
        lines = mountinfo_path.read_text().splitlines()
    except FileNotFoundError:
        return targets
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        target = Path(_decode_mountinfo_path(fields[4])).resolve()
        if target == prefix or prefix in target.parents:
            targets.append(target)
    return targets


def _parse_fuser_pids(output: str) -> set[int]:
    pids: set[int] = set()
    for token in output.replace(":", " ").split():
        if "/" in token:
            continue
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits:
            pids.add(int(digits))
    return pids


def _pid_argv(pid: int) -> list[str]:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _is_darling_runtime_process(pid: int, *, argv: list[str] | None = None) -> bool:
    if argv is None:
        argv = _pid_argv(pid)
    if not argv:
        try:
            name = (Path("/proc") / str(pid) / "comm").read_text(errors="replace").strip()
        except OSError:
            return False
    else:
        name = Path(argv[0]).name
    return name in {"darling", "darlingserver", "mldr", "vchroot"}


def _prefix_mount_holder_pids(
    targets: list[Path],
    *,
    runner=subprocess.run,
) -> set[int]:
    pids: set[int] = set()
    for target in targets:
        completed = runner(
            ["fuser", "-m", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode not in (0, 1):
            continue
        pids.update(_parse_fuser_pids(f"{completed.stdout}\n{completed.stderr}"))
    return pids


def _terminate_pids(
    pids: set[int],
    *,
    result: PrefixRepairResult,
    kill_func=os.kill,
    sleep_func=time.sleep,
) -> None:
    if not pids:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        live: set[int] = set()
        for pid in sorted(pids):
            try:
                kill_func(pid, 0)
                live.add(pid)
            except ProcessLookupError:
                continue
            except PermissionError:
                live.add(pid)
        if not live:
            return
        for pid in sorted(live):
            try:
                kill_func(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                result.problems.append(f"cannot kill Darling prefix process {pid}: {error}")
        result.changed.append(
            f"sent {signal.Signals(sig).name} to Darling prefix process(es): "
            f"{', '.join(str(pid) for pid in sorted(live))}"
        )
        sleep_func(1)


def cleanup_prefix_processes_for_mounts(
    targets: list[Path],
    *,
    runner=subprocess.run,
    kill_func=os.kill,
    sleep_func=time.sleep,
    argv_for_pid=_pid_argv,
) -> PrefixRepairResult:
    result = PrefixRepairResult()
    holder_pids = _prefix_mount_holder_pids(targets, runner=runner)
    darling_pids = {
        pid
        for pid in holder_pids
        if _is_darling_runtime_process(pid, argv=argv_for_pid(pid))
    }
    if not darling_pids:
        result.ok.append("no Darling runtime processes hold prefix mounts")
        return result
    _terminate_pids(
        darling_pids,
        result=result,
        kill_func=kill_func,
        sleep_func=sleep_func,
    )
    return result


def cleanup_prefix_mounts(
    prefix: Path,
    *,
    runner=subprocess.run,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    kill_func=os.kill,
    sleep_func=time.sleep,
    argv_for_pid=_pid_argv,
) -> PrefixRepairResult:
    result = PrefixRepairResult()
    targets = prefix_mount_targets(prefix, mountinfo_path=mountinfo_path)
    if not targets:
        result.ok.append("no mounted filesystems under prefix")
        return result

    counts = Counter(targets)
    ordered_targets = sorted(counts, key=lambda item: len(item.parts), reverse=True)
    failed_targets: list[Path] = []
    failed_details: dict[Path, str] = {}
    for target in ordered_targets:
        unmounted = 0
        for _ in range(counts[target]):
            completed = runner(
                ["umount", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0 and runner is subprocess.run:
                detail = completed.stderr.strip() or completed.stdout.strip()
                if "must be superuser" in detail or "not superuser" in detail:
                    completed = runner(
                        ["sudo", "-n", "umount", str(target)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
            if completed.returncode == 0:
                unmounted += 1
                continue
            detail = completed.stderr.strip() or completed.stdout.strip() or f"rc={completed.returncode}"
            failed_targets.append(target)
            failed_details[target] = detail
            break
        if unmounted == 1:
            result.changed.append(f"unmounted {target}")
        elif unmounted:
            result.changed.append(f"unmounted {target} ({unmounted} mount(s))")

    if failed_targets:
        process_cleanup = cleanup_prefix_processes_for_mounts(
            failed_targets,
            runner=runner,
            kill_func=kill_func,
            sleep_func=sleep_func,
            argv_for_pid=argv_for_pid,
        )
        result.extend(process_cleanup)
        if process_cleanup.changed:
            for target in sorted(set(failed_targets), key=lambda item: len(item.parts), reverse=True):
                completed = runner(
                    ["umount", str(target)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode == 0:
                    result.changed.append(f"unmounted {target} after killing Darling process(es)")
    remaining = prefix_mount_targets(prefix, mountinfo_path=mountinfo_path)
    if remaining:
        for target in remaining:
            if target in failed_details:
                result.problems.append(
                    f"failed to unmount {target} ({counts[target]} mount(s)): "
                    f"{failed_details[target]}"
                )
            result.problems.append(f"still mounted under prefix: {target}")
    return result


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        return True
    return True


def darling_init_pid_is_usable(pid: int) -> bool:
    if not _pid_is_alive(pid):
        return False
    try:
        os.stat(Path("/proc") / str(pid) / "ns/mnt")
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ESRCH):
            return False
        return True
    return True


def _repair_stale_init_pid(prefix: Path, result: PrefixRepairResult, *, check: bool) -> None:
    init_pid = prefix / INIT_PID_REL
    server_socket = prefix / SERVER_SOCKET_REL
    if not init_pid.exists():
        result.ok.append(f"{INIT_PID_REL} absent")
        _repair_stale_server_socket(
            server_socket,
            result,
            check=check,
            reason=f"{INIT_PID_REL} is absent",
        )
        return
    if not init_pid.is_file() and not init_pid.is_symlink():
        result.problems.append(f"{INIT_PID_REL} exists but is not a regular file")
        return

    raw_pid = init_pid.read_text(errors="replace").strip()
    try:
        pid = int(raw_pid)
    except ValueError:
        if check:
            result.problems.append(f"{INIT_PID_REL} contains invalid pid {raw_pid!r}")
            _repair_stale_server_socket(
                server_socket,
                result,
                check=check,
                reason=f"{INIT_PID_REL} is invalid",
            )
            return
        init_pid.unlink()
        result.changed.append(f"removed invalid {INIT_PID_REL}")
        _repair_stale_server_socket(
            server_socket,
            result,
            check=check,
            reason=f"{INIT_PID_REL} is invalid",
        )
        return

    if darling_init_pid_is_usable(pid):
        result.ok.append(f"{INIT_PID_REL} points to live pid {pid}")
        return
    if check:
        result.problems.append(f"{INIT_PID_REL} points to stale pid {pid}")
        _repair_stale_server_socket(
            server_socket,
            result,
            check=check,
            reason=f"{INIT_PID_REL} points to stale pid {pid}",
        )
        return
    init_pid.unlink()
    result.changed.append(f"removed stale {INIT_PID_REL} for pid {pid}")
    _repair_stale_server_socket(
        server_socket,
        result,
        check=check,
        reason=f"{INIT_PID_REL} points to stale pid {pid}",
    )


def _repair_stale_server_socket(
    server_socket: Path,
    result: PrefixRepairResult,
    *,
    check: bool,
    reason: str,
) -> None:
    if not server_socket.exists() and not server_socket.is_symlink():
        return
    try:
        mode = server_socket.lstat().st_mode
    except OSError as error:
        result.problems.append(f"cannot inspect {SERVER_SOCKET_REL}: {error}")
        return
    if not (stat.S_ISSOCK(mode) or stat.S_ISLNK(mode)):
        result.problems.append(f"{SERVER_SOCKET_REL} exists but is not a socket")
        return
    if check:
        result.problems.append(f"{SERVER_SOCKET_REL} is stale because {reason}")
        return
    server_socket.unlink()
    result.changed.append(f"removed stale {SERVER_SOCKET_REL} ({reason})")


def _candidate_clt_dirs(root: Path) -> list[Path]:
    developer = root / "Library/Developer"
    if not developer.is_dir():
        return []
    return sorted(
        (
            path
            for path in developer.glob("CommandLineTools.apple-clt-*")
            if path.is_dir()
        ),
        reverse=True,
    )


def _repair_tmp_dirs(prefix: Path, result: PrefixRepairResult, *, check: bool) -> None:
    for rel in (*TMP_RELS, *PRIVATE_TMP_RELS, *ROOT_TMP_RELS):
        path = prefix / rel
        if path.is_dir():
            mode = path.stat().st_mode & 0o7777
            if mode == 0o1777:
                result.ok.append(f"{rel} exists with mode 1777")
            elif check:
                result.problems.append(f"{rel} mode {mode:o}, expected 1777")
            else:
                path.chmod(0o1777)
                result.changed.append(f"chmod 1777 {rel}")
            continue
        if path.exists():
            result.problems.append(f"{rel} exists but is not a directory")
            continue
        if check:
            result.problems.append(f"{rel} missing")
            continue
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o1777)
        result.changed.append(f"created {rel} with mode 1777")


def repair_prefix_boot_prerequisites(prefix: Path) -> PrefixRepairResult:
    """Leave rootless boot initialization to Darling's launchd product code."""

    result = PrefixRepairResult()
    result.ok.append("rootless boot directories are initialized by Darling launchd")
    return result


def _repair_clt_for_root(
    root_name: str,
    root: Path,
    result: PrefixRepairResult,
    *,
    check: bool,
) -> None:
    link = root / CANONICAL_CLT_REL
    if link.exists():
        if (link / "usr/bin/clang").exists():
            result.ok.append(f"{root_name}: canonical CommandLineTools has clang")
        else:
            result.problems.append(f"{root_name}: {CANONICAL_CLT_REL}/usr/bin/clang missing")
        return

    if link.is_symlink():
        if check:
            result.problems.append(f"{root_name}: dangling {CANONICAL_CLT_REL} symlink")
            return
        link.unlink()
        result.changed.append(f"{root_name}: removed dangling {CANONICAL_CLT_REL}")

    candidates = _candidate_clt_dirs(root)
    if not candidates:
        result.problems.append(
            f"{root_name}: no CommandLineTools.apple-clt-* candidate under Library/Developer"
        )
        return
    candidate = candidates[0]
    if not (candidate / "usr/bin/clang").exists():
        result.problems.append(f"{root_name}: selected CLT candidate lacks usr/bin/clang: {candidate.name}")
        return
    if check:
        result.problems.append(f"{root_name}: canonical {CANONICAL_CLT_REL} symlink missing")
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(candidate.name)
    result.changed.append(f"{root_name}: linked {CANONICAL_CLT_REL} -> {candidate.name}")


def _repair_darling_clang_for_root(
    root_name: str,
    root: Path,
    result: PrefixRepairResult,
    *,
    check: bool,
) -> None:
    clang = root / DARLING_CLT_CLANG_REL
    target = root / CANONICAL_CLT_REL / "usr/bin/clang"
    if clang.exists():
        result.ok.append(f"{root_name}: DarlingCLT clang exists")
        return
    if clang.is_symlink():
        if check:
            result.problems.append(f"{root_name}: dangling {DARLING_CLT_CLANG_REL} symlink")
            return
        clang.unlink()
        result.changed.append(f"{root_name}: removed dangling {DARLING_CLT_CLANG_REL}")
    if check:
        result.problems.append(f"{root_name}: DarlingCLT clang link missing")
        return
    if not target.exists():
        result.problems.append(
            f"{root_name}: cannot link DarlingCLT clang; canonical CLT clang is missing"
        )
        return
    clang.parent.mkdir(parents=True, exist_ok=True)
    clang.symlink_to(DARLING_CLT_CLANG_TARGET)
    result.changed.append(f"{root_name}: linked {DARLING_CLT_CLANG_REL}")


def repair_prefix_prerequisites(
    prefix: Path,
    *,
    check: bool = False,
    system_root: Path | None = None,
) -> PrefixRepairResult:
    """Repair guest state only, using *system_root* solely as read-only context."""
    result = PrefixRepairResult()
    _repair_stale_init_pid(prefix, result, check=check)
    result.extend(repair_prefix_boot_prerequisites(prefix) if not check else PrefixRepairResult())
    if check:
        _repair_tmp_dirs(prefix, result, check=True)
        for rel in ROOTLESS_RUNTIME_DIR_RELS:
            if not (prefix / rel).is_dir():
                result.problems.append(f"{rel} missing")
    # Lower-image prerequisites are read-only context, never a link/copy source.
    upper_clt = prefix / CANONICAL_CLT_REL
    lower_clt = system_root / CANONICAL_CLT_REL if system_root is not None else None
    if (
        not upper_clt.exists()
        and not upper_clt.is_symlink()
        and not _candidate_clt_dirs(prefix)
        and lower_clt is not None
        and (lower_clt / "usr/bin/clang").exists()
    ):
        result.ok.append("runtime system root: canonical CommandLineTools has clang")
    else:
        _repair_clt_for_root("prefix root", prefix, result, check=check)
        _repair_darling_clang_for_root("prefix root", prefix, result, check=check)
    return result

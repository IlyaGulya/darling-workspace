"""Darling lanes backed by containerd's existing FUSE snapshot store.

The snapshotter owns layer allocation, commit, ancestry and reference protection.
Darling owns clone identity; workspace leases and user scopes own execution.
Freezing retains the original mounted view: replacing it cannot preserve the
product's lifecycle identity and is never repaired by rewriting its record.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterator, Mapping

try:
    from .runtime_install import InstalledRuntime, RuntimeInstallationError, resolve_runtime
except ImportError:  # Loaded as a West extension module, not a package.
    from runtime_install import InstalledRuntime, RuntimeInstallationError, resolve_runtime

try:
    from .runtime_retention import leased_runtime, pin_runtime, unpin_runtime
except ImportError:
    from runtime_retention import leased_runtime, pin_runtime, unpin_runtime

from lane_snapshotter import (
    KIND_LABEL, STORE_LABEL, TREE_LABEL, SnapshotStore, SnapshotStoreError, require_allow_root,
    write_json,
)

LANE_SCHEMA = 2
LANE_MARKER = "lane.json"
RETIRED_MARKER = ".retired-lane.json"
LANE_ROOT_ENV = "DW_LANE_ROOT"
LANE_ENV = "DW_LANE"
LANE_NAME_ENV = "DW_LANE_NAME"
LANE_SOURCE_ENV = "DW_LANE_SOURCE"
LANE_BUILD_DIR_ENV = "DW_LANE_BUILD_DIR"
SCOPE_ACTIVE_ENV = "DW_LANE_SCOPE_ACTIVE"
SCOPE_UNIT_PREFIX = "darling-lane-"

LANE_ROOT_DIRNAME = ".darling-lanes"
PREFIX_DIRNAME = "merged"
STATE_DIRNAME = "state"
TMP_DIRNAME = "tmp"
RUN_DIRNAME = "run"
BUILD_DIRNAME = "build"
LOGS_DIRNAME = "logs"
CORES_DIRNAME = "cores"
RECEIPTS_DIRNAME = "receipts"

#: Directories the lane owns. ``source`` is deliberately absent: an independent
#: source checkout is created by the existing authoring tooling and merely
#: *recorded* here, so the lane cannot pretend it materialized one.
LANE_SUBDIRS = (
    PREFIX_DIRNAME,
    STATE_DIRNAME,
    TMP_DIRNAME,
    RUN_DIRNAME,
    BUILD_DIRNAME,
    LOGS_DIRNAME,
    CORES_DIRNAME,
    RECEIPTS_DIRNAME,
)

#: The workspace's own content-baseline tool for runtime artifacts. A lane records the selected
#: immutable installation's artifact CONTENT through it, because a stat-only tree digest is an
#: identity check and cannot claim that a byte is unchanged.
ARTIFACT_MANIFEST_TOOL = Path(__file__).resolve().parents[1] / "scripts" / "darling-artifact-manifest.sh"
RUNTIME_ARTIFACT_MANIFEST = "runtime-artifacts.txt"

PREFIX_STATE_FILENAME = ".darling-prefix-state-v2"

FUSE_OVERLAYFS = "fuse-overlayfs"
FUSERMOUNT = "fusermount3"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


#: Every external command a lane runs is bounded. MEASURED: an interrupted contract left a
#: `fusermount3 -u` in uninterruptible sleep, and an unbounded `subprocess.run` around it hung the
#: next run at its first unmount. A tool that stops one experiment must not hang on that experiment.
COMMAND_TIMEOUT_SECONDS = 30.0


def _run(
    runner: Callable[..., Any],
    argv: list[str],
    *,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run one command with a deadline, reporting a timeout as an ordinary failure."""

    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if runner is not subprocess.run:
        # An injected runner is a test double with its own signature; the deadline is not its concern.
        return runner(argv, **kwargs)
    try:
        return subprocess.run(argv, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            argv, returncode=124, stdout="", stderr=f"timed out after {timeout}s: {error}"
        )


class LaneError(RuntimeError):
    """A lane operation that must stop rather than guess."""


@dataclass
class LaneResult:
    changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems


# --------------------------------------------------------------------------
# Pure layout and identity helpers (no side effects; used by the contracts).
# --------------------------------------------------------------------------


def validate_lane_name(name: str) -> str:
    """Return ``name`` if it is a safe single path component, else raise."""

    if not _NAME_RE.match(name or ""):
        raise LaneError(
            f"invalid lane name {name!r}: use letters, digits, '.', '_' and '-' "
            "(at most 48 characters, starting with a letter or digit)"
        )
    return name


def lane_root(environ: Mapping[str, str], topdir: Path) -> Path:
    """The directory holding lanes: ``DW_LANE_ROOT`` or ``<topdir>/.darling-lanes``."""

    declared = environ.get(LANE_ROOT_ENV)
    if declared:
        return Path(declared).expanduser()
    return Path(topdir) / LANE_ROOT_DIRNAME


def lane_path(root: Path, name: str) -> Path:
    return Path(root) / validate_lane_name(name)


def lane_prefix(path: Path) -> Path:
    """The prefix a run points at: the lane's merged view."""

    return Path(path) / PREFIX_DIRNAME


def lane_state_root(path: Path) -> Path:
    return Path(path) / STATE_DIRNAME


def lane_subdir(path: Path, name: str) -> Path:
    return Path(path) / name


def scope_unit(lane: Path) -> str:
    """The lane's scope unit name, in the bare form ``systemd-run --unit=`` wants.

    The readable lane name is not enough on its own: systemd unit names are global to the user
    manager while lanes are not, so two lane roots that chose the same name would claim one unit -
    and before the digest was added, the second lane's run stopped the first one's experiment. The
    digest is derived from the lane's own path, so a lane's unit is stable for its whole life and a
    different root never collides with it.
    """

    path = Path(lane)
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", path.name)[:32]
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode()).hexdigest()[:12]
    return f"{SCOPE_UNIT_PREFIX}{sanitized}-{digest}"


def scope_ref(unit: str) -> str:
    """The fully qualified unit reference ``systemctl`` needs.

    An unqualified name is resolved against ``.service``, so asking
    ``systemctl --user stop darling-lane-x`` reports "Unit ... .service not
    loaded" and a lane could never be stopped by its own name. Measured: that
    message is what refused the first lane removal.
    """

    return unit if unit.endswith(".scope") else f"{unit}.scope"


def lane_env(path: Path, record: Mapping[str, Any]) -> dict[str, str]:
    """Environment that makes a command run inside the lane and nowhere else.

    ``DARLING_BUILD_DIR`` is the variable the build tooling actually honours: `west darling-build`
    defaults `--build-dir` to it, and the runtime build's configure reads it to inherit the generator
    and compilers of the build it was configured against. A lane therefore points it at its own build
    directory, because an exported *lane-named* variable that nothing reads guarantees no isolation at
    all - MEASURED: `DW_LANE_BUILD_DIR` was set and `west darling-build` still resolved
    `~/work/darling-build`.
    """

    lane = Path(path)
    require_runnable_lane(lane, record)
    build_dir = lane_subdir(lane, BUILD_DIRNAME)
    environment = {
        "DPREFIX": str(lane_prefix(lane)),
        "DARLING_PREFIX": str(lane_prefix(lane)),
        LANE_ENV: str(lane),
        LANE_NAME_ENV: str(record.get("name", lane.name)),
        "DW_STATE_ROOT": str(lane_state_root(lane)),
        "TMPDIR": str(lane_subdir(lane, TMP_DIRNAME)),
        "XDG_RUNTIME_DIR": str(lane_subdir(lane, RUN_DIRNAME)),
        LANE_BUILD_DIR_ENV: str(build_dir),
        "DARLING_BUILD_DIR": str(build_dir),
    }
    source = recorded_source_path(record)
    if source:
        environment[LANE_SOURCE_ENV] = source
    runtime = record.get("runtime")
    if isinstance(runtime, dict):
        root = runtime.get("root")
        launcher = runtime.get("launcher")
        if isinstance(root, str) and root:
            environment["DARLING_RUNTIME_ROOT"] = root
        if isinstance(launcher, str) and launcher:
            environment["DARLING_LAUNCHER"] = launcher
            environment["DARLING"] = launcher
    return environment


def west_command_argv() -> list[str]:
    """Argv prefix that re-invokes the West command currently running."""

    invoked = Path(sys.argv[0])
    if invoked.name == "__main__.py":
        return [sys.executable, "-m", "west"]
    if invoked.suffix == ".py" and invoked.is_file():
        return [sys.executable, str(invoked.resolve())]
    if invoked.is_file():
        return [str(invoked.resolve())]
    executable = shutil.which("west")
    if executable is None:
        raise LaneError("cannot locate the current west executable")
    return [executable]


def source_identity(source: Path, *, runner: Callable[..., Any] = subprocess.run) -> dict[str, str]:
    """Record the source checkout's exact manifest identity.

    The manifest revision is what makes two experiments comparable; a lane that
    cannot name it is a lane whose result cannot be attributed.
    """

    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise LaneError(f"source '{source}' is not a directory")
    try:
        head = _run(
            runner, ["git", "-C", str(source), "rev-parse", "HEAD"], check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise LaneError(f"cannot read the manifest revision of '{source}': {error}") from error
    manifest = source / "west.yml"
    digest = "absent"
    if manifest.is_file():
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    return {"path": str(source), "manifest_head": head, "west_yml_sha256": digest}


# --------------------------------------------------------------------------
# Content identity of the base: the property a lane must not break.
# --------------------------------------------------------------------------


def tree_identity(root: Path) -> str:
    """Digest a tree's *identity* without reading file contents.

    Path, size, mtime, inode and mode are enough to notice any mutation of the
    base, including a lane that wrote through its lower layer by accident, and
    a stat-only walk costs a fraction of hashing a multi-gigabyte prefix. It is
    an identity check, not a backup verifier; `darling-artifact-manifest.sh` is
    the tool for content baselines.
    """

    root = Path(root)
    digest = hashlib.sha256()
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        entries: list[tuple[str, os.stat_result, str]] = []
        for name in dirnames + filenames:
            full = Path(directory) / name
            try:
                stat = os.lstat(full)
            except OSError as error:
                raise LaneError(f"cannot stat '{full}': {error}") from error
            target = os.readlink(full) if os.path.islink(full) else ""
            entries.append((str(full.relative_to(root)), stat, target))
        for relative, stat, target in sorted(entries):
            digest.update(
                "\0".join(
                    (
                        relative,
                        str(stat.st_mode),
                        str(stat.st_size),
                        str(stat.st_mtime_ns),
                        str(stat.st_ino),
                        target,
                    )
                ).encode()
            )
            digest.update(b"\n")
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Process ownership.
# --------------------------------------------------------------------------


def scope_available() -> bool:
    return shutil.which("systemd-run") is not None and shutil.which("systemctl") is not None


#: Printed whenever a lane run is deliberately launched without its cgroup. The wording is the point:
#: nothing about the run looks different afterwards, so the label has to be unmissable.
UNSAFE_NO_SCOPE_NOTICE = (
    "UNSAFE FOR CONCURRENT AGENTS: this run is NOT owned by a lane cgroup. Process cleanup falls "
    "back to prefix ownership, which cannot see a guest that scrubbed its environment and cannot "
    "reach a lane whose prefix was destroyed and recreated. Use it for a single-agent diagnostic only."
)


def require_scope() -> None:
    """Refuse to run in a lane when there is no per-lane cgroup to own it.

    Process ownership is not a convenience here, it is the isolation: without the scope, cleanup has
    only the prefix-anchored evidence, which cannot see a guest that scrubbed its environment and
    cannot reach a lid that was destroyed and recreated. A run that would be parallel must therefore
    fail rather than proceed unowned.
    """

    if not scope_available():
        raise LaneError(
            "no per-lane process isolation available: systemd-run and systemctl are required to "
            "own a lane's process tree (systemd --user must be running). This run would be "
            "unisolated, so it is refused; use --no-scope only for a single-agent diagnostic."
        )


def scope_state(unit: str, *, runner: Callable[..., Any] = subprocess.run) -> str:
    """``"absent"`` when the lane scope does not exist, else its active state."""

    reference = scope_ref(unit)
    try:
        loaded = _run(
            runner, ["systemctl", "--user", "show", "--property=LoadState", "--value", reference]
        )
    except OSError:
        return "absent"
    if (loaded.stdout or "").strip() in ("", "not-found"):
        return "absent"
    try:
        active = _run(runner, ["systemctl", "--user", "is-active", reference])
    except OSError:
        return "absent"
    state = (active.stdout or "").strip()
    return state if state else "absent"


def scope_stop(unit: str, *, runner: Callable[..., Any] = subprocess.run) -> LaneResult:
    result = LaneResult()
    if scope_state(unit, runner=runner) == "absent":
        result.changed.append(f"no {unit} scope to stop")
        return result
    reference = scope_ref(unit)
    completed = _run(runner, ["systemctl", "--user", "stop", reference])
    if completed.returncode != 0:
        result.problems.append(
            f"systemctl --user stop {reference} failed: {completed.stderr.strip()}"
        )
    else:
        result.changed.append(f"stopped {reference}")
    return result


def scope_processes(
    unit: str, *, runner: Callable[..., Any] = subprocess.run, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> list[int]:
    """The PIDs currently inside a scope unit's cgroup."""

    try:
        completed = _run(
            runner,
            [
                "systemctl",
                "--user",
                "show",
                "--property=ControlGroup",
                "--value",
                scope_ref(unit),
            ],
        )
    except OSError:
        return []
    control_group = (completed.stdout or "").strip()
    if not control_group or control_group == "/":
        return []
    procs = Path(cgroup_root) / control_group.lstrip("/") / "cgroup.procs"
    try:
        return [int(line) for line in procs.read_text().split()]
    except (OSError, ValueError):
        return []


def claim_scope(
    unit: str, *, lane: Path, runner: Callable[..., Any] = subprocess.run, timeout: float = 5.0
) -> LaneResult:
    """Make ``unit`` available for a new run, or refuse because the lane is busy.

    Three refusals and one wait, each of them measured:

    * a unit whose description names a *different* lane is left alone. The unit name is global to the
      user manager while a lane is not, so two lane roots that chose the same name would otherwise
      stop each other's experiments - a cross-lane kill, which is the failure this whole mechanism
      exists to prevent;
    * a scope that still holds processes is this lane's previous experiment, and starting a second
      one in the same lane is the collision lanes exist to prevent;
    * a finished scope must be *gone*, not merely stopped, before the name can be used again.
      `systemd-run --scope --unit=X` refuses with "Unit X was already loaded or has a fragment
      file" while systemd still knows the name, so the release is awaited rather than assumed.
    """

    result = LaneResult()
    if scope_state(unit, runner=runner) == "absent":
        return result
    require_own_scope(unit, lane, runner=runner)
    pids = scope_processes(unit, runner=runner)
    if pids:
        raise LaneError(
            f"lane scope {scope_ref(unit)} still owns {len(pids)} process(es) "
            f"({', '.join(str(pid) for pid in pids[:5])}"
            f"{', …' if len(pids) > 5 else ''}); stop it with "
            f"'systemctl --user stop {scope_ref(unit)}' or remove the lane"
        )
    stopped = scope_stop(unit, runner=runner)
    result.changed.extend(stopped.changed)
    result.problems.extend(stopped.problems)
    _run(runner, ["systemctl", "--user", "reset-failed", scope_ref(unit)], timeout=10.0)
    if not await_scope_release(unit, runner=runner, timeout=timeout):
        raise LaneError(
            f"{scope_ref(unit)} is still known to systemd after being stopped; "
            "wait for it to be collected and try again"
        )
    return result


def require_own_scope(
    unit: str, lane: Path, *, runner: Callable[..., Any] = subprocess.run
) -> None:
    """Refuse to touch a scope that belongs to a different lane.

    The unit name is global to the user manager while a lane is not: two lane roots that chose the
    same name would otherwise stop each other's experiments, which is a cross-lane kill.
    """

    actual = scope_description_of(unit, runner=runner)
    expected = scope_description(lane)
    if actual and actual != expected:
        raise LaneError(
            f"{scope_ref(unit)} belongs to {actual!r}, not to {expected!r}; refusing to touch it"
        )


def stop_own_scope(
    unit: str, *, lane: Path, runner: Callable[..., Any] = subprocess.run, timeout: float = 5.0
) -> LaneResult:
    """Stop this lane's scope, its processes included, and await the name's release."""

    result = LaneResult()
    if scope_state(unit, runner=runner) == "absent":
        result.changed.append(f"no {scope_ref(unit)} scope to stop")
        return result
    require_own_scope(unit, lane, runner=runner)
    stopped = scope_stop(unit, runner=runner)
    result.changed.extend(stopped.changed)
    result.problems.extend(stopped.problems)
    _run(runner, ["systemctl", "--user", "reset-failed", scope_ref(unit)], timeout=10.0)
    if not await_scope_release(unit, runner=runner, timeout=timeout):
        result.problems.append(
            f"{scope_ref(unit)} is still known to systemd after being stopped"
        )
    return result


def scope_description(lane: Path) -> str:
    """The transient unit description that identifies a lane's scope."""

    return f"darling isolation lane {Path(lane)}"


def scope_description_of(unit: str, *, runner: Callable[..., Any] = subprocess.run) -> str:
    try:
        completed = _run(
            runner, ["systemctl", "--user", "show", "--property=Description", "--value", scope_ref(unit)]
        )
    except OSError:
        return ""
    return (completed.stdout or "").strip()


def await_scope_release(
    unit: str, *, runner: Callable[..., Any] = subprocess.run, timeout: float = 5.0
) -> bool:
    """Whether the unit name became available again within ``timeout``."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if scope_state(unit, runner=runner) == "absent":
            return True
        time.sleep(0.05)
    return scope_state(unit, runner=runner) == "absent"


def _argument_names_lane(argument: str, lane_spelling: str) -> bool:
    """Whether one argv entry names this lane and not a lane whose name starts the same way.

    The daemon receives its directories both as bare arguments and inside a single
    ``-o lowerdir=…,upperdir=…,workdir=…`` value, so the comparison is per path component:
    a substring test would make lane ``a`` claim lane ``a2``'s daemon, and stopping one lane
    would then stop its neighbour.
    """

    for token in re.split(r"[,=]", argument):
        if token == lane_spelling or token.startswith(lane_spelling + os.sep):
            return True
    return False


def fuse_daemon_pids(
    lane: Path, *, proc_root: Path = Path("/proc"), self_pid: int | None = None
) -> list[int]:
    """PIDs of the FUSE daemon serving exactly this lane.

    Identity is the lane's own path in the daemon's argv, never the program
    name alone: a concurrent lane's daemon names a different directory.
    """

    self_pid = os.getpid() if self_pid is None else self_pid
    lane_spelling = str(Path(lane))
    owned: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return owned
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == self_pid:
            continue
        try:
            argv = [
                part.decode(errors="replace")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
        except OSError:
            continue
        if not argv or Path(argv[0]).name != FUSE_OVERLAYFS:
            continue
        if any(_argument_names_lane(argument, lane_spelling) for argument in argv[1:]):
            owned.append(int(entry.name))
    return owned


# --------------------------------------------------------------------------
# Mount handling.
# --------------------------------------------------------------------------


def mount_targets(path: Path, *, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> list[Path]:
    """Mountpoints at or below ``path``, deepest first."""

    path = Path(path)
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        lines = mountinfo_path.read_text().splitlines()
    except OSError:
        return []
    targets = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        try:
            target = Path(fields[4].replace("\\040", " ")).resolve()
        except OSError:
            continue
        if target == resolved or resolved in target.parents:
            targets.add(target)
    return sorted(targets, key=lambda item: len(item.parts), reverse=True)


def fuse_unmount(mountpoint: Path, *, runner: Callable[..., Any] = subprocess.run) -> LaneResult:
    """Unmount a lane's own FUSE mount, never another lane's.

    The kernel only permits the mounting user to unmount a FUSE mount, so this
    is a per-user operation: no privilege, and no way to reach a sibling lane.
    A lazy unmount is the fallback only after the plain one is refused, and it
    is announced, because a lazy unmount detaches the directory before the
    daemon has released it.
    """

    result = LaneResult()
    mountpoint = Path(mountpoint)
    if not mount_targets(mountpoint):
        result.changed.append(f"{mountpoint} is not mounted")
        return result
    if not shutil.which(FUSERMOUNT):
        result.problems.append(f"{FUSERMOUNT} is unavailable; cannot unmount {mountpoint}")
        return result
    completed = _run(runner, [FUSERMOUNT, "-u", str(mountpoint)], timeout=10.0)
    lazy = False
    if completed.returncode != 0:
        completed = _run(runner, [FUSERMOUNT, "-uz", str(mountpoint)], timeout=10.0)
        lazy = completed.returncode == 0
    if completed.returncode != 0:
        result.problems.append(
            f"{FUSERMOUNT} could not unmount {mountpoint}: {completed.stderr.strip()}"
        )
        return result
    deadline = time.monotonic() + 5.0
    while mount_targets(mountpoint) and time.monotonic() < deadline:
        time.sleep(0.05)
    if mount_targets(mountpoint):
        result.problems.append(f"{mountpoint} is still mounted after unmount")
        return result
    result.changed.append(f"unmounted {mountpoint}{' (lazy)' if lazy else ''}")
    return result


# --------------------------------------------------------------------------
# Records.
# --------------------------------------------------------------------------


def read_record(path: Path) -> dict[str, Any] | None:
    """Read a lane record, or return ``None`` when the directory is not a lane."""

    marker = Path(path) / LANE_MARKER
    try:
        payload = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        raise LaneError(f"invalid lane record: {marker}")
    if payload.get("schema") != LANE_SCHEMA:
        raise LaneError(
            f"{marker} uses unsupported lane schema {payload.get('schema')!r}; "
            "drain legacy lanes through their original owning checkout first. "
            "Existing mounts are never adopted or deleted by the snapshotter cutover."
        )
    return payload


def _retired_identity(path: Path) -> dict[str, Any]:
    from runtime_install import prefix_path_is_safe

    metadata = path.lstat()
    if (not prefix_path_is_safe(path) or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()):
        raise LaneError(f"lane coordination directory is not owned and safe: {path}")
    return {
        "schema": 1, "name": path.name, "root": str(path.parent),
        "uid": os.geteuid(), "device": metadata.st_dev, "inode": metadata.st_ino,
    }


def _check_retired_marker(path: Path) -> None:
    try:
        retired = json.loads((path / RETIRED_MARKER).read_text())
    except (OSError, ValueError) as error:
        raise LaneError(f"no valid retired lane marker at {path}") from error
    if retired != _retired_identity(path):
        raise LaneError(f"retired lane marker does not own this directory: {path}")


def _lane_namespace(path: Path, *, with_data: bool = False) -> list[Path]:
    """Validate the entire namespace before removing any owned lane data."""
    from test_prefix import prefix_lease_path

    _retired_identity(path)
    coordination = {RETIRED_MARKER, ".execution.lock", prefix_lease_path(lane_prefix(path)).name}
    records = {LANE_MARKER, LANE_MARKER + ".new", RETIRED_MARKER + ".new"}
    disposable = []
    for entry in path.iterdir():
        metadata = entry.lstat()
        # The product's sibling workspace is not a snapshotter layer directory.
        if with_data and (entry.name in LANE_SUBDIRS
                          or entry.name == f"{PREFIX_DIRNAME}.workdir"):
            valid = stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
            disposable.append(entry)
        elif (entry.name in coordination or entry.name.startswith(".darling-prefix-lock-v2-")
              or with_data and entry.name in records):
            valid = stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
            if entry.name in records and entry.name != LANE_MARKER:
                disposable.append(entry)
        else:
            raise LaneError(f"unexpected contents in lane coordination directory: {entry}")
        if not valid:
            raise LaneError(f"lane entry is not an owned regular file or data directory: {entry}")
    if os.path.lexists(path / RETIRED_MARKER):
        _check_retired_marker(path)
    return disposable


def _require_retired_lane(path: Path) -> None:
    from test_prefix import prefix_lease_path

    _lane_namespace(path)
    _check_retired_marker(path)
    for name in (".execution.lock", prefix_lease_path(lane_prefix(path)).name):
        if not (path / name).is_file():
            raise LaneError(f"retired lane lost its persistent coordination file: {path / name}")


def _retire_lane(path: Path, record: Mapping[str, Any]) -> None:
    """Retain the locked namespace; publish a tombstone without runtime pins."""
    disposable = _lane_namespace(path, with_data=True)
    if read_record(path) != record:
        raise LaneError(f"lane record changed before retirement: {path}")
    if mount_targets(path):
        raise LaneError(f"lane still has mounted filesystems: {path}")
    for entry in disposable:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    write_json(path / RETIRED_MARKER, _retired_identity(path))
    (path / LANE_MARKER).unlink()
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def recorded_source_path(record: Mapping[str, Any]) -> str | None:
    source = record.get("source")
    return source.get("path") if isinstance(source, dict) else None


def _mount_identity(prefix: Path) -> dict[str, Any]:
    """Bind admission to this boot and exact mounted lifetime, not just a path."""
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        target = fields[4]
        for escaped, literal in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            target = target.replace(escaped, literal)
        if target != str(prefix):
            continue
        separator = fields.index("-")
        if not fields[separator + 1].startswith("fuse") or fields[separator + 2] != FUSE_OVERLAYFS:
            raise LaneError(f"lane prefix is not its FUSE view: {prefix}")
        metadata = prefix.stat()
        return {
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "mount_id": int(fields[0]), "device": metadata.st_dev, "inode": metadata.st_ino,
        }
    raise LaneError(f"lane prefix has lost its original mounted view: {prefix}")


def _snapshot_keys(lane: Path) -> tuple[str, str]:
    token = hashlib.sha256(os.fsencode(lane)).hexdigest()
    return "lane-" + token, "frozen-" + token


def record_problems(path: Path, record: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    path = Path(path)
    if record.get("name") != path.name or record.get("root") != str(path.parent):
        problems.append(f"lane record does not own this path: {path}")
    storage = record.get("storage", {})
    if storage.get("root") != str(path.parent / ".snapshotter"):
        problems.append("lane record names another snapshot store")
    if storage.get("key") not in _snapshot_keys(path):
        problems.append("lane record names another lane's snapshot")
    try:
        if _mount_identity(lane_prefix(path)) != storage.get("mount_identity"):
            problems.append("lane mounted lifetime changed; remount/reanchor is not supported")
    except (LaneError, OSError, ValueError) as error:
        problems.append(str(error))
    runtime = record.get("runtime", {})
    if not isinstance(runtime.get("root"), str) or not Path(runtime["root"]).is_dir():
        problems.append("recorded runtime installation is missing")
    if not isinstance(runtime.get("launcher"), str) or not Path(runtime["launcher"]).is_file():
        problems.append("recorded runtime launcher is missing")
    return problems


def require_runnable_lane(path: Path, record: Mapping[str, Any]) -> None:
    problems = record_problems(path, record)
    if record.get("mode") != "active":
        problems.append(f"lane is {record.get('mode')!r}, not writable; create a child of a frozen lane")
    if problems:
        raise LaneError("; ".join(problems))
    try:
        with SnapshotStore(path.parent).locked() as store:
            info = store.info(record["storage"]["key"])
            if (info is None or info.get("Kind") != "Active"
                    or info.get("Parent") != record["storage"]["parent"]
                    or info.get("Labels", {}).get(STORE_LABEL) != str(store.root)):
                raise LaneError("lane snapshot is absent, committed or owned by another store")
            _check_snapshot_layers(store, record)
    except SnapshotStoreError as error:
        raise LaneError(str(error)) from error


@contextmanager
def _lane_admission(path: Path, *, exclusive: bool = False) -> Iterator[None]:
    """Keep arbitrary exec admission distinct from a guest controller's lease."""
    from runtime_install import prefix_path_is_safe

    if not prefix_path_is_safe(path):
        raise LaneError(f"unsafe lane admission path: {path}")
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        descriptor = os.open(".execution.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise LaneError("lane execution lease is not an owned regular file")
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LaneError(f"lane {path.name!r} has an active execution or lifecycle transition") from error
            try:
                current = os.stat(".execution.lock", dir_fd=directory, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                    raise LaneError("lane admission lease was replaced")
                if not os.path.samestat(os.fstat(directory), path.stat()):
                    raise LaneError("lane directory was replaced during admission")
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


@contextmanager
def lane_execution_lease(path: Path) -> Iterator[dict[str, str]]:
    # Do not lend a prefix lifecycle FD to arbitrary commands: an actual
    # product launcher would pass it on to runtime daemons and guest processes.
    # Nested supported controllers acquire their own normal prefix lease.
    with _lane_admission(path):
        record = read_record(path)
        if record is None:
            raise LaneError(f"lane record is missing: {path}")
        if record.get("managed_run") is not None:
            try:
                from .managed_run import authorize_lane_execution
            except ImportError:
                from managed_run import authorize_lane_execution
            authorize_lane_execution(path, record)
        yield lane_env(path, record)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _base_identity(base: Path) -> dict[str, Any]:
    base = Path(base).expanduser().resolve()
    if not base.is_dir():
        raise LaneError(f"base '{base}' is not a directory")
    if not os.path.lexists(base / PREFIX_STATE_FILENAME):
        raise LaneError(
            f"base '{base}' has no {PREFIX_STATE_FILENAME}; the product bind-clone operation "
            "requires stable guest state, not a launcher in the prefix"
        )
    stat = os.stat(base)
    return {
        "path": str(base),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "identity": tree_identity(base),
    }


def base_in_use(base: Path, *, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> list[str]:
    """Reasons a base prefix must not be used as a lane's lower layer.

    A live base is not merely untidy: its runtime sockets and published init
    PID sit in the lower layer, where the lane would resolve them to the
    *base's* live server, so the two experiments would share one courier. The
    lower layer is only safe while it is inert.
    """

    from test_prefix import prefix_process_ownership_snapshot

    problems: list[str] = []
    if not base.is_dir():
        return problems
    owners = prefix_process_ownership_snapshot(base)
    if owners:
        problems.append("base prefix has live processes: " + "; ".join(owners))
    mounts = mount_targets(base, mountinfo_path=mountinfo_path)
    if mounts:
        problems.append(
            "base prefix has mounted filesystems: " + ", ".join(str(item) for item in mounts)
        )
    return problems


# --------------------------------------------------------------------------
# Create.
# --------------------------------------------------------------------------


def _check_snapshot_layers(store: SnapshotStore, record: Mapping[str, Any]) -> None:
    storage = record["storage"]
    paths = store.recipe_paths(storage["recipe"])
    key = storage["parent"]
    for layer in paths["lowerdir"]:
        info = store.info(key)
        if (info is None or info.get("Kind") != "Committed"
                or info.get("Labels", {}).get(STORE_LABEL) != str(store.root)
                or info.get("Labels", {}).get(TREE_LABEL) != tree_identity(layer)):
            raise LaneError(f"committed parent layer is absent or changed: {key}")
        key = info.get("Parent")
    if key:
        raise LaneError("snapshot ancestry differs from the recorded mount recipe")
    if record.get("mode") == "frozen":
        info = store.info(storage["key"])
        if (info is None or info.get("Kind") != "Committed"
                or info.get("Labels", {}).get(TREE_LABEL) != tree_identity(paths["upperdir"][0])):
            raise LaneError("frozen lane layer changed after commit")


def _base_snapshot_keys(identity: Mapping[str, Any]) -> tuple[str, str]:
    token = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return "base-" + token, "import-" + token


def _import_base(store: SnapshotStore, base: Path, identity: dict[str, Any]) -> str:
    key, active = _base_snapshot_keys(identity)
    existing = store.info(key)
    if existing is not None:
        if (existing.get("Kind") != "Committed"
                or existing.get("Labels", {}).get(STORE_LABEL) != str(store.root)):
            raise LaneError(f"base snapshot is not owned committed state: {key}")
        return key
    _collect_base(store, key)
    from fresh_prefix import _ignore_prefix_transients

    # A parentless snapshot's physical Source is the snapshotter's supported
    # import surface. Actual prefixes are always FUSE children, never bind mounts.
    recipe = store.prepare(active)
    store.label(active, {KIND_LABEL: "base-import"})
    physical = store.layer_path(recipe["Source"])
    try:
        shutil.copytree(base, physical, dirs_exist_ok=True, symlinks=True,
                        ignore=_ignore_prefix_transients(base))
        metadata = base.stat()
        os.chown(physical, metadata.st_uid, metadata.st_gid)
        if tree_identity(base) != identity["identity"]:
            raise LaneError("base changed while importing its snapshot")
        store.label(active, {KIND_LABEL: "base", TREE_LABEL: tree_identity(physical)})
        store.commit(key, active)
    except Exception:
        store.remove(active)
        raise
    return key


def _collect_base(store: SnapshotStore, key: str) -> None:
    info = store.info(key)
    if (info is not None and info.get("Labels", {}).get(KIND_LABEL) == "base"
            and info.get("Labels", {}).get(STORE_LABEL) == str(store.root)
            and not store.children(key)):
        store.remove(key)
    if key.startswith("base-"):
        partial = "import-" + key.removeprefix("base-")
        pending = store.info(partial)
        if (pending is not None and pending.get("Kind") == "Active"
                and pending.get("Labels", {}).get(STORE_LABEL) == str(store.root)
                and pending.get("Labels", {}).get(KIND_LABEL) == "base-import"
                and not store.children(partial)):
            store.remove(partial)


def create_lane(
    base: str | Path | None,
    root: Path,
    name: str,
    *,
    parent: str | None = None,
    source: Path | None = None,
    runtime: InstalledRuntime | None = None,
    runtime_root: str | Path | None = None,
    allow_root: bool = False,
    managed_run: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    clock: Callable[[], str] | None = None,
) -> tuple[LaneResult, Path | None]:
    """Import an inert base once, or branch an explicitly frozen mounted lane.

    ``allow_root`` mounts the new view with the provider's root-and-owner ACL
    instead of the default owner-only session. It requires the host's
    ``user_allow_other`` FUSE policy and is never inherited by later lanes.
    """
    from fresh_prefix import bind_prefix_clone
    from test_prefix import prefix_lease, prefix_process_ownership_snapshot

    if managed_run is not None:
        if (set(managed_run) != {"id", "path"}
                or not re.fullmatch(r"[0-9a-f]{32}", managed_run.get("id", ""))):
            raise LaneError("invalid managed run ownership descriptor")
        owner = Path(managed_run["path"])
        if (not owner.is_absolute() or ".." in owner.parts or owner.is_symlink()
                or not owner.is_dir() or owner.stat().st_uid != os.geteuid()):
            raise LaneError("managed run owner must be an existing owned directory")
    validate_lane_name(name)
    if (base is None) == (parent is None):
        raise LaneError("select exactly one base prefix or frozen parent lane")
    root = Path(root).expanduser()
    lane = lane_path(root, name)
    if os.path.lexists(lane):
        _require_retired_lane(lane)
    parent_record = None
    inherit_runtime = parent is not None and runtime_root is None and runtime is None
    if parent is not None:
        parent_path = lane_path(root, parent)
        parent_record = read_record(parent_path)
        if parent_record is None:
            raise LaneError(f"parent lane does not exist: {parent}")
        base = lane_prefix(parent_path)
        if inherit_runtime:
            runtime_root = parent_record["runtime"]["root"]
    assert base is not None
    try:
        selected = runtime or resolve_runtime(runtime_root)
        selected.environment(os.path.expanduser(os.fspath(base)))
        selected.environment(lane_prefix(lane))
    except RuntimeInstallationError as error:
        raise LaneError(str(error)) from error
    if not shutil.which(FUSERMOUNT):
        raise LaneError(f"{FUSERMOUNT} is required for rootless lane views")
    explicit_source = source_identity(source, runner=runner) if source is not None else None
    result = LaneResult()
    try:
        if allow_root:
            require_allow_root()
        with SnapshotStore(root).locked(create=True) as store, ExitStack() as guards:
            selected = guards.enter_context(leased_runtime(runtime_root, runtime=runtime))
            selected.environment(base)
            selected.environment(lane_prefix(lane))
            # Reserve before writing anything: mkdir must never merge with a
            # direct controller's unrecorded destination.
            retired = os.path.lexists(lane)
            if retired:
                _require_retired_lane(lane)
            else:
                lane.mkdir(mode=0o700)
            guards.enter_context(_lane_admission(lane, exclusive=True))
            guards.enter_context(prefix_lease(lane_prefix(lane), blocking=False))
            if retired:
                _require_retired_lane(lane)
            else:
                _lane_namespace(lane)
                write_json(lane / RETIRED_MARKER, _retired_identity(lane))
            active_key, frozen_key = _snapshot_keys(lane)
            if store.info(active_key) is not None or store.info(frozen_key) is not None:
                raise LaneError(f"destination still has snapshot state; refusing to reuse {lane}")
            with prefix_lease(base, blocking=False):
                identity = _base_identity(base)
                base_path = Path(identity["path"])
                if parent is None:
                    problems = base_in_use(base_path, mountinfo_path=mountinfo_path)
                else:
                    parent_record = read_record(parent_path)
                    if parent_record is None or parent_record.get("mode") != "frozen":
                        raise LaneError(f"parent lane '{parent}' must be frozen explicitly first")
                    if inherit_runtime and parent_record["runtime"]["root"] != str(selected.root):
                        selected = guards.enter_context(leased_runtime(parent_record["runtime"]["root"]))
                        selected.environment(base)
                        selected.environment(lane_prefix(lane))
                    problems = record_problems(parent_path, parent_record)
                    problems += prefix_process_ownership_snapshot(base_path)
                    _check_snapshot_layers(store, parent_record)
                if problems:
                    raise LaneError("clone source is not inert and valid: " + "; ".join(problems))
                parent_key = (_base_snapshot_keys(identity)[0] if parent_record is None
                              else parent_record["storage"]["key"])
                identity_source = (explicit_source if source is not None
                                   else parent_record.get("source") if parent_record else None)
                # The destination guards remain held through publication and
                # any rollback, independently of the source prefix's lease.
                record: dict[str, Any] = {
                    "schema": LANE_SCHEMA, "name": name, "root": str(root),
                    "created": (clock or _utc_now)(), "mode": "creating",
                    "base": identity, "parent": parent, "source": identity_source,
                    "runtime": {"root": str(selected.root), "launcher": str(selected.launcher),
                                "system_root": str(selected.system_root), "identity": selected.summary()},
                    "storage": {"root": str(store.root), "key": active_key, "parent": parent_key},
                }
                if managed_run is not None:
                    metadata = lane.stat()
                    record["managed_run"] = {
                        **managed_run, "device": metadata.st_dev, "inode": metadata.st_ino,
                    }
                try:
                    write_json(lane / LANE_MARKER, record)
                    (lane / RETIRED_MARKER).unlink()
                    pin_runtime(selected.root, "lane:" + str(lane))
                    if parent_record is None:
                        _import_base(store, base_path, identity)
                    for subdir in LANE_SUBDIRS:
                        (lane / subdir).mkdir()
                    os.chmod(lane / RUN_DIRNAME, 0o700)
                    manifest_rel = f"{RECEIPTS_DIRNAME}/{RUNTIME_ARTIFACT_MANIFEST}"
                    manifest = lane / manifest_rel
                    saved = _run(runner, [str(ARTIFACT_MANIFEST_TOOL), "--runtime-root",
                                         str(selected.root), "--save", str(manifest)], timeout=180)
                    if saved.returncode:
                        raise LaneError(f"cannot baseline runtime artifacts: {saved.stderr or saved.stdout}")
                    record["runtime"]["artifact_manifest"] = {
                        "path": manifest_rel, "sha256": _sha256_file(manifest),
                    }
                    recipe = store.prepare(active_key, parent_key)
                    record["storage"]["recipe"] = recipe
                    write_json(lane / LANE_MARKER, record)
                    _check_snapshot_layers(store, record)
                    store.mount(recipe, lane_prefix(lane),
                                lane / LOGS_DIRNAME / "fuse-overlayfs.log", base_path,
                                allow_root=allow_root)
                    bind_prefix_clone(selected, lane_prefix(lane), base_path)
                    record["storage"]["mount_identity"] = _mount_identity(lane_prefix(lane))
                    record["mode"] = "active"
                    write_json(lane / LANE_MARKER, record)
                except Exception as error:
                    try:
                        _lane_namespace(lane, with_data=True)
                        info = store.info(active_key)
                        if info is not None:
                            if (info.get("Labels", {}).get(STORE_LABEL) != str(store.root)
                                    or info.get("Parent") != parent_key):
                                raise LaneError("rollback snapshot is not the owned destination")
                            if store.children(active_key):
                                raise LaneError("rollback snapshot still has descendants")
                        owners = prefix_process_ownership_snapshot(lane_prefix(lane))
                        if owners:
                            raise LaneError("rollback prefix still has owners: " + "; ".join(owners))
                        record["mode"] = "destroying"
                        write_json(lane / LANE_MARKER, record)
                        rollback = fuse_unmount(lane_prefix(lane), runner=runner)
                        if rollback.problems or mount_targets(lane_prefix(lane)):
                            raise LaneError("; ".join(rollback.problems) or "view remains mounted")
                        store.release_mount_service(lane_prefix(lane))
                        if fuse_daemon_pids(lane):
                            raise LaneError("lane provider has not released its view")
                        if info is not None:
                            store.remove(active_key)
                        _collect_base(store, parent_key)
                        _retire_lane(lane, record)
                        unpin_runtime(selected.root, "lane:" + str(lane))
                    except Exception as cleanup_error:
                        record["mode"] = "failed"
                        record["failure"] = str(error)
                        write_json(lane / LANE_MARKER, record)
                        raise LaneError(
                            f"creation failed: {error}; rollback refused: {cleanup_error}; "
                            f"owned state retained at {lane}"
                        ) from error
                    store.stop_if_empty()
                    raise LaneError(f"cannot create lane: {error}") from error
                result.changed += [f"prepared snapshot {active_key} from {parent_key}",
                                   f"bound {lane_prefix(lane)} through product bind-clone"]
            return result, lane
    except (SnapshotStoreError, RuntimeInstallationError, BlockingIOError, OSError, ValueError) as error:
        raise LaneError(str(error)) from error


def freeze_lane(lane: Path, *, runner: Callable[..., Any] = subprocess.run) -> LaneResult:
    """Commit an idle lane while retaining its exact original mounted view."""
    from test_prefix import prefix_lease, prefix_process_ownership_snapshot

    result = LaneResult()
    lane = Path(lane)
    try:
        with SnapshotStore(lane.parent).locked() as store:
            record = read_record(lane)
            if record is None:
                raise LaneError(f"no lane record at {lane}")
            problems = record_problems(lane, record)
            if problems:
                raise LaneError("; ".join(problems))
            if record.get("mode") == "frozen":
                _check_snapshot_layers(store, record)
                result.changed.append("lane is already frozen")
                return result
            if record.get("mode") not in ("active", "freezing"):
                raise LaneError(f"cannot freeze a lane in state {record.get('mode')!r}")
            unit = scope_unit(lane)
            if scope_state(unit, runner=runner) in ("active", "activating", "deactivating", "reloading"):
                raise LaneError("lane scope is active; finish its run before freezing")
            claim_scope(unit, lane=lane, runner=runner)
            with _lane_admission(lane, exclusive=True), prefix_lease(lane_prefix(lane), blocking=False):
                owners = prefix_process_ownership_snapshot(lane_prefix(lane))
                if owners:
                    raise LaneError("lane prefix still has runtime owners: " + "; ".join(owners))
                _check_snapshot_layers(store, record)
                active, committed = _snapshot_keys(lane)
                # The durable admission barrier precedes Commit. An interrupted
                # freeze can only be finished by freeze again, never executed.
                record["mode"] = "freezing"
                write_json(lane / LANE_MARKER, record)
                if store.info(committed) is None:
                    synced = _run(runner, ["sync", "-f", str(lane_prefix(lane))])
                    if synced.returncode:
                        raise LaneError(f"cannot flush lane before commit: {synced.stderr}")
                    upper = store.recipe_paths(record["storage"]["recipe"])["upperdir"][0]
                    store.label(active, {KIND_LABEL: "frozen", TREE_LABEL: tree_identity(upper)})
                    store.commit(committed, active)
                record["storage"]["key"] = committed
                record["mode"] = "frozen"
                _check_snapshot_layers(store, record)
                write_json(lane / LANE_MARKER, record)
                result.changed.append(f"committed {committed}; retained original mounted prefix")
    except (LaneError, SnapshotStoreError, BlockingIOError, OSError, ValueError) as error:
        result.problems.append(str(error))
    return result


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())




# --------------------------------------------------------------------------
# Destroy.
# --------------------------------------------------------------------------


def destroy_lane(
    lane: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    before_retire: Callable[[Path, Mapping[str, Any]], None] | None = None,
) -> LaneResult:
    """Refuse referenced parents before touching their scope or mounted view."""
    from runtime_install import prefix_path_is_safe
    from test_prefix import (
        cleanup_verified_prefix_processes, prefix_lease, prefix_process_ownership_snapshot,
    )

    result = LaneResult()
    lane = Path(lane)
    if not prefix_path_is_safe(lane) or read_record(lane) is None:
        raise LaneError(f"refusing to remove an unsafe or unrecorded lane: {lane}")
    merged = lane_prefix(lane)
    try:
        with SnapshotStore(lane.parent).locked() as store:
            record = read_record(lane)
            if record is None:
                raise LaneError(f"refusing to remove an unrecorded lane: {lane}")
            if (not prefix_path_is_safe(lane) or record.get("name") != lane.name
                    or record.get("root") != str(lane.parent)
                    or record.get("storage", {}).get("key") not in _snapshot_keys(lane)):
                raise LaneError("lane record does not own this directory and snapshot")
            if record["storage"].get("root") != str(store.root):
                raise LaneError("lane record names another snapshot store")
            if record.get("managed_run") is not None and before_retire is None:
                raise LaneError(
                    "managed lane must finish through its run owner; "
                    "use 'west dev lane recover' to retain its evidence before retirement"
                )
            _lane_namespace(lane, with_data=True)
            mode = record.get("mode")
            if mode not in ("creating", "active", "freezing", "frozen", "failed", "destroying"):
                raise LaneError(f"cannot destroy a lane in state {mode!r}")
            if record.get("parent") is None:
                token = hashlib.sha256(json.dumps(record.get("base"), sort_keys=True).encode()).hexdigest()
                expected_parent = "base-" + token
            else:
                expected_parent = _snapshot_keys(lane_path(lane.parent, record["parent"]))[1]
            if record["storage"].get("parent") != expected_parent:
                raise LaneError("lane record does not own its recorded snapshot ancestry")
            key = record["storage"]["key"]
            if record.get("mode") == "freezing" and store.info(_snapshot_keys(lane)[1]):
                key = _snapshot_keys(lane)[1]
            info = store.info(key)
            if info is None:
                if mode not in ("creating", "failed", "destroying"):
                    raise LaneError("recorded snapshot is missing; refusing to discard lane evidence")
            elif (info.get("Labels", {}).get(STORE_LABEL) != str(store.root)
                  or info.get("Parent") != expected_parent
                  or info.get("Kind") not in ("Active", "Committed")):
                raise LaneError("recorded snapshot is not owned lane state")
            parent_info = store.info(expected_parent)
            if (parent_info is not None
                    and parent_info.get("Labels", {}).get(STORE_LABEL) != str(store.root)):
                raise LaneError("recorded parent snapshot belongs to another store")
            children = store.children(key)
            if children:
                raise LaneError("snapshotter parent still has descendants: " + ", ".join(children))
            mounts = mount_targets(lane, mountinfo_path=mountinfo_path)
            if any(target != merged and merged not in target.parents for target in mounts):
                raise LaneError("lane has mounted filesystems outside its owned prefix view")
            if mount_targets(merged, mountinfo_path=mountinfo_path):
                mounted = _mount_identity(merged)
                if ("mount_identity" in record["storage"]
                        and mounted != record["storage"]["mount_identity"]):
                    raise LaneError("lane mounted lifetime changed; refusing to unmount another view")
            stopped = stop_own_scope(scope_unit(lane), lane=lane, runner=runner)
            result.changed.extend(stopped.changed)
            if stopped.problems:
                result.problems.extend(stopped.problems)
                return result
            with _lane_admission(lane, exclusive=True), prefix_lease(merged, blocking=False):
                if read_record(lane) != record:
                    raise LaneError("lane record changed during destruction admission")
                # Publish the actual key before Remove. A crash after removal
                # remains destroyable without pretending an active lane exists.
                record["storage"]["key"] = key
                record["mode"] = "destroying"
                write_json(lane / LANE_MARKER, record)
                cleanup = cleanup_verified_prefix_processes(merged)
                result.changed.extend(cleanup.changed)
                if cleanup.problems:
                    result.problems.extend(cleanup.problems)
                    return result
                owners = prefix_process_ownership_snapshot(merged)
                if owners:
                    raise LaneError("prefix still has owners: " + "; ".join(owners))
                unmounted = fuse_unmount(merged, runner=runner)
                result.changed.extend(unmounted.changed)
                if unmounted.problems or mount_targets(merged, mountinfo_path=mountinfo_path):
                    result.problems.extend(unmounted.problems or ["lane is still mounted"])
                    return result
                store.release_mount_service(merged)
                if fuse_daemon_pids(lane):
                    raise LaneError("lane provider has not released its view")
                if before_retire is not None:
                    before_retire(lane, record)
                if info is not None:
                    store.remove(key)
                _collect_base(store, record["storage"]["parent"])
                _retire_lane(lane, record)
                unpin_runtime(Path(record["runtime"]["root"]), "lane:" + str(lane))
                result.changed.append(f"removed lane {lane} and its snapshot; retained lease namespace")
                store.stop_if_empty()
    except (RuntimeError, BlockingIOError, OSError, ValueError) as error:
        result.problems.append(str(error))
    return result


# --------------------------------------------------------------------------
# Inspect.
# --------------------------------------------------------------------------


def lane_status(
    lane: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> dict[str, Any]:
    """Everything needed to decide whether this lane is still usable."""

    from test_prefix import prefix_process_ownership_snapshot

    lane = Path(lane)
    record = read_record(lane)
    if record is None:
        raise LaneError(f"'{lane}' holds no {LANE_MARKER} with schema {LANE_SCHEMA}")
    merged = lane_prefix(lane)
    unit = scope_unit(lane)
    status: dict[str, Any] = {
        "name": record.get("name"),
        "scope_unit": scope_ref(unit),
        "path": str(lane),
        "prefix": str(merged),
        "state_root": str(lane_state_root(lane)),
        "mounted": bool(mount_targets(merged, mountinfo_path=mountinfo_path)),
        "scope": scope_state(unit, runner=runner),
        "processes": prefix_process_ownership_snapshot(merged) if merged.is_dir() else [],
        "fuse_daemons": fuse_daemon_pids(lane),
        "mode": record.get("mode"),
        "parent": record.get("parent"),
        "snapshot": record["storage"]["key"],
        "storage_root": record["storage"]["root"],
        "snapshot_kind": None, "children": [], "layer_bytes": None, "layer_inodes": None,
        "problems": record_problems(lane, record),
    }
    try:
        with SnapshotStore(lane.parent).locked() as store:
            key = record["storage"]["key"]
            info = store.info(key)
            status["snapshot_kind"] = info.get("Kind") if info else None
            status["children"] = store.children(key)
            if info is None:
                status["problems"].append("recorded snapshot is missing")
            else:
                rows = store.command("usage", "-b", key).stdout.splitlines()
                fields = rows[-1].split()
                status["layer_bytes"], status["layer_inodes"] = int(fields[1]), int(fields[2])
    except (SnapshotStoreError, ValueError, IndexError) as error:
        status["problems"].append(str(error))
    base = record.get("base")
    if isinstance(base, dict) and isinstance(base.get("path"), str):
        current = Path(base["path"])
        status["base"] = str(current)
        status["base_present"] = current.is_dir()
        if current.is_dir():
            stat = os.stat(current)
            status["base_same_identity"] = (stat.st_dev, stat.st_ino) == (
                base.get("device"),
                base.get("inode"),
            )
    return status


def verify_lane(lane: Path, *, runner: Callable[..., Any] = subprocess.run) -> LaneResult:
    """Verify managed immutable layers, mounted lifetime, runtime bytes and source."""

    result = LaneResult()
    lane = Path(lane)
    record = read_record(lane)
    if record is None:
        raise LaneError(f"'{lane}' holds no {LANE_MARKER} with schema {LANE_SCHEMA}")
    for problem in record_problems(lane, record):
        result.problems.append(problem)
    try:
        with SnapshotStore(lane.parent).locked() as store:
            info = store.info(record["storage"]["key"])
            expected_kind = {"active": "Active", "frozen": "Committed"}.get(record.get("mode"))
            if (expected_kind is None or info is None or info.get("Kind") != expected_kind
                    or info.get("Parent") != record["storage"]["parent"]
                    or info.get("Labels", {}).get(STORE_LABEL) != str(store.root)):
                result.problems.append("lane snapshot state or ownership changed")
            _check_snapshot_layers(store, record)
            result.changed.append("managed parent layer tree identities are unchanged")
    except (LaneError, SnapshotStoreError, OSError, ValueError) as error:
        result.problems.append(str(error))
    runtime = record.get("runtime")
    if isinstance(runtime, dict):
        root = runtime.get("root")
        current_runtime = Path(root) if isinstance(root, str) else None
        if current_runtime is None or not current_runtime.is_dir():
            result.problems.append(f"runtime installation is gone: {root!r}")
        else:
            recorded_manifest = runtime.get("artifact_manifest")
            if (isinstance(recorded_manifest, dict)
                    and recorded_manifest.get("path") == f"{RECEIPTS_DIRNAME}/{RUNTIME_ARTIFACT_MANIFEST}"):
                manifest = lane / recorded_manifest["path"]
                if not manifest.is_file():
                    result.problems.append(
                        f"the runtime's artifact manifest {recorded_manifest['path']} is gone"
                    )
                elif _sha256_file(manifest) != recorded_manifest.get("sha256"):
                    result.problems.append(
                        f"the runtime's artifact manifest {recorded_manifest['path']} was edited"
                    )
                else:
                    completed = _run(
                        runner,
                        [
                            str(ARTIFACT_MANIFEST_TOOL),
                            "--runtime-root",
                            str(current_runtime),
                            "--check",
                            str(manifest),
                        ],
                        timeout=180.0,
                    )
                    if completed.returncode != 0:
                        result.problems.append(
                            "the runtime installation's artifacts changed content: "
                            + _tail_text_from_text(completed.stdout or completed.stderr or "")
                        )
                    else:
                        result.changed.append(
                            "the runtime installation's artifact content matches its sha256 baseline"
                        )
    source = record.get("source")
    if isinstance(source, dict) and isinstance(source.get("path"), str):
        current = source_identity(Path(source["path"]), runner=runner)
        if current["manifest_head"] != source.get("manifest_head"):
            result.problems.append(
                f"source '{current['path']}' manifest moved from "
                f"{source.get('manifest_head')} to {current['manifest_head']}"
            )
        else:
            result.changed.append(f"source '{current['path']}' is at {current['manifest_head']}")
    return result


def _tail_text_from_text(text: str, limit: int = 10) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-limit:]) or "(no detail)"


def list_lanes(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        (entry for entry in root.iterdir() if entry.is_dir() and read_record(entry)),
        key=lambda entry: entry.name,
    )




# --------------------------------------------------------------------------
# Running inside a lane.
# --------------------------------------------------------------------------


def env_argv(assignments: Mapping[str, str], command: list[str]) -> list[str]:
    """``env K=V ... command`` — assignments applied to exactly one process tree."""

    argv = ["env"]
    argv.extend(f"{key}={value}" for key, value in sorted(assignments.items()))
    argv.extend(command)
    return argv


def scope_wrap(unit: str, argv: list[str], *, description: str) -> list[str]:
    """Argv that runs ``argv`` as the lane's own cgroup, in the foreground.

    ``--scope`` keeps the command a foreground child, so exit status, stdio,
    signals and the existing job/observer tooling behave exactly as before;
    only the cgroup ownership changes. ``--description`` is how the unit says
    which lane it belongs to, so a scope is never adopted or stopped on its
    name alone.
    """

    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        f"--unit={unit}",
        f"--description={description}",
        "--",
        *argv,
    ]

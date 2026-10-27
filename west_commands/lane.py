"""Parallel-experiment isolation lanes for the Darling West/dwdiag workflow.

A lane is one experiment's private world: its own copy-on-write prefix
instance, state root, temporary/runtime directories, logs, cores and deploy
receipts. Two lanes run two experiments at the same time without sharing
mutable product state, and stopping one cannot reach the other.

WHY THIS SHAPE. Every rule below is a measured property of the supported host
(Ubuntu 24.04, kernel 6.8), not a preference:

* Unprivileged user namespaces are blocked by AppArmor
  (`kernel.apparmor_restrict_unprivileged_userns=1`): `unshare -Ur` cannot even
  write `/proc/self/uid_map`, so a kernel `overlay` mount is impossible without
  root. `fuse-overlayfs` needs no user namespace, no capability and no host
  security change; it mounts through the distribution's setuid `fusermount3`,
  which only permits a mount owned by the calling user and lets that user
  unmount it. This is the same mechanism every rootless container runtime uses.
* Reflink/CoW clones are unavailable on the workspace filesystem (ext4
  refuses `FICLONE`), so `west darling-fresh-prefix --from` degrades to a
  multi-gigabyte byte copy. A lane is a layer, not a copy.
* The launcher resolves its prefix state from `.darling-prefix-state-v2`, which
  binds the prefix directory's device and inode. A lane's merged directory has
  its own identity, so the product's checked ``bind-clone`` operation publishes
  the lane's record through its merged view; that write lands in the lane's
  upper layer and the base stays byte-identical. Workspace Python never parses
  or rewrites the lifecycle record itself.
* `west test` serializes concurrent prefix runs by flocking the prefix's
  PARENT directory. A lane's prefix is `<lane>/merged`, so the lock domain is
  the lane directory: two lanes never serialize, while two prefixes that
  genuinely share one directory still do.
* Lane processes are owned by a per-lane `systemd --user` scope. A scope keeps
  a detached, re-parented, environment-scrubbed descendant in its cgroup after
  the scope's own main process exits, and `systemctl --user stop` kills exactly
  that tree. Name matching cannot see such a process, and a lane whose prefix
  was destroyed and recreated makes even the prefix-anchored probe lose the
  handle; the cgroup does not.

The base prefix is the immutable lower layer. A lane never writes to it: every
change, including the launcher's own runtime state, copy-ups into the lane's
upper layer and is discarded with the lane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping

try:
    from .runtime_install import InstalledRuntime, RuntimeInstallationError, resolve_runtime
except ImportError:  # Loaded as a West extension module, not a package.
    from runtime_install import InstalledRuntime, RuntimeInstallationError, resolve_runtime

LANE_SCHEMA = 1
LANE_MARKER = "lane.json"
LANE_ROOT_ENV = "DW_LANE_ROOT"
LANE_ENV = "DW_LANE"
LANE_NAME_ENV = "DW_LANE_NAME"
LANE_SOURCE_ENV = "DW_LANE_SOURCE"
LANE_BUILD_DIR_ENV = "DW_LANE_BUILD_DIR"
SCOPE_ACTIVE_ENV = "DW_LANE_SCOPE_ACTIVE"
SCOPE_UNIT_PREFIX = "darling-lane-"

LANE_ROOT_DIRNAME = ".darling-lanes"
PREFIX_DIRNAME = "merged"
UPPER_DIRNAME = "upper"
WORK_DIRNAME = "work"
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
    UPPER_DIRNAME,
    WORK_DIRNAME,
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
    if not isinstance(payload, dict) or payload.get("schema") != LANE_SCHEMA:
        return None
    return payload


def recorded_base_path(record: Mapping[str, Any]) -> str | None:
    """The base path a lane record names, in either recorded shape."""

    base = record.get("base")
    if isinstance(base, str):
        return base or None
    if isinstance(base, dict) and isinstance(base.get("path"), str):
        return base["path"] or None
    return None


def recorded_source_path(record: Mapping[str, Any]) -> str | None:
    """The source checkout path a lane record names, in either recorded shape.

    A lane records its source as an identity object (path, manifest revision, `west.yml` digest). The
    environment must carry the *path*; passing the object through `str()` exported a Python dict
    literal, which is not a path any tool can use.
    """

    source = record.get("source")
    if isinstance(source, str):
        return source or None
    if isinstance(source, dict) and isinstance(source.get("path"), str):
        return source["path"] or None
    return None


def record_problems(path: Path, record: Mapping[str, Any]) -> list[str]:
    """Reasons this lane is not usable as recorded."""

    problems: list[str] = []
    name = record.get("name")
    if name != Path(path).name:
        problems.append(f"record names lane {name!r} but the directory is {Path(path).name!r}")
    base = recorded_base_path(record)
    if base is None:
        problems.append("record names no base prefix")
    elif not Path(base).is_dir():
        problems.append(f"recorded base '{base}' is not a directory")
    prefix = lane_prefix(Path(path))
    if not prefix.is_dir():
        problems.append(f"lane guest prefix directory is missing: {prefix}")
    runtime = record.get("runtime")
    if isinstance(runtime, dict):
        root = runtime.get("root")
        launcher = runtime.get("launcher")
        if not isinstance(root, str) or not Path(root).is_dir():
            problems.append(f"recorded runtime root is missing: {root!r}")
        if not isinstance(launcher, str) or not Path(launcher).is_file():
            problems.append(f"recorded runtime launcher is missing: {launcher!r}")
    return problems


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


def create_lane(
    base: str | Path,
    root: Path,
    name: str,
    *,
    source: Path | None = None,
    runtime: InstalledRuntime | None = None,
    runtime_root: str | Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    clock: Callable[[], str] | None = None,
) -> tuple[LaneResult, Path | None]:
    """Create a disposable lane layered over writable guest state ``base``.

    The base is the immutable lower layer; the runtime image is a separate
    immutable installation selected through ``runtime_root``. The product's
    checked ``bind-clone`` operation publishes the lane's own lifecycle record.
    """

    result = LaneResult()
    validate_lane_name(name)
    try:
        selected = runtime or resolve_runtime(runtime_root)
        selected.environment(os.path.expanduser(os.fspath(base)))
    except RuntimeInstallationError as error:
        raise LaneError(str(error)) from error
    if not shutil.which(FUSE_OVERLAYFS) or not shutil.which(FUSERMOUNT):
        raise LaneError(
            f"{FUSE_OVERLAYFS} and {FUSERMOUNT} are required for a rootless "
            "copy-on-write prefix layer"
        )
    identity = _base_identity(base)
    base_path = Path(identity["path"])
    in_use = base_in_use(base_path, mountinfo_path=mountinfo_path)
    if in_use:
        raise LaneError(
            f"base '{base_path}' is not inert: "
            + "; ".join(in_use)
            + " (a lane's lower layer must not be a running experiment)"
        )
    lane = lane_path(root, name)
    if lane.exists():
        raise LaneError(f"lane '{name}' already exists at {lane}")
    for guest_state in (base_path, lane_prefix(lane)):
        try:
            selected.environment(guest_state)
        except RuntimeInstallationError as error:
            raise LaneError(str(error)) from error
    if source is not None:
        identity_source = source_identity(source, runner=runner)
    else:
        identity_source = None

    for subdir in LANE_SUBDIRS:
        (lane / subdir).mkdir(parents=True, exist_ok=True)
    os.chmod(lane / RUN_DIRNAME, 0o700)

    # The immutable installation's artifacts, by CONTENT, through the workspace's own baseline
    # tool. This is the receipt that can say "no runtime byte changed"; the base's tree digest is a
    # cheaper drift detector that also fires on a touched-but-identical file.
    manifest_rel = f"{RECEIPTS_DIRNAME}/{RUNTIME_ARTIFACT_MANIFEST}"
    manifest = lane / manifest_rel
    if not ARTIFACT_MANIFEST_TOOL.exists():
        _discard_lane_tree(lane)
        raise LaneError(
            f"the artifact manifest tool is missing at {ARTIFACT_MANIFEST_TOOL}; a lane will not "
            "claim a content baseline it cannot produce"
        )
    saved = _run(
        runner,
        [
            str(ARTIFACT_MANIFEST_TOOL),
            "--runtime-root",
            str(selected.root),
            "--save",
            str(manifest),
        ],
        timeout=180.0,
    )
    if saved.returncode != 0:
        detail = (saved.stderr or saved.stdout or "").strip()
        _discard_lane_tree(lane)
        raise LaneError(f"cannot baseline the runtime installation's artifacts: {detail}")
    runtime_identity: dict[str, Any] = {
        "root": str(selected.root),
        "launcher": str(selected.launcher),
        "system_root": str(selected.system_root),
        "identity": selected.identity,
        "artifact_manifest": {
            "path": manifest_rel,
            "sha256": _sha256_file(manifest),
        },
    }
    result.changed.append(
        f"recorded the runtime installation's artifact content in {manifest_rel}"
    )

    merged = lane_prefix(lane)
    work = lane / WORK_DIRNAME
    upper = lane / UPPER_DIRNAME
    # The daemon must not inherit our stdout/stderr pipes: `capture_output=True`
    # leaves it holding a write end whose reader exits with this process, and its
    # first log write then kills it with SIGPIPE, leaving a disconnected mount.
    # MEASURED: that is exactly how the first lane died between calls. It writes
    # to its own lane log instead, which is also where a mount failure is read.
    daemon_log = lane / LOGS_DIRNAME / "fuse-overlayfs.log"
    with open(daemon_log, "ab") as log_handle:
        mount = _run(
            runner,
            [
                FUSE_OVERLAYFS,
                "-o",
                f"lowerdir={base_path},upperdir={upper},workdir={work}",
                str(merged),
            ],
            timeout=60.0,
            capture_output=False,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    if mount.returncode != 0 or not mount_targets(merged, mountinfo_path=mountinfo_path):
        detail = _tail_text(daemon_log)
        _discard_lane_tree(lane)
        raise LaneError(
            f"{FUSE_OVERLAYFS} could not mount '{merged}' over '{base_path}': {detail}"
        )
    result.changed.append(f"mounted {merged} over {base_path}")

    try:
        from fresh_prefix import bind_prefix_clone

        bind_prefix_clone(selected, merged, base_path)
    except Exception as error:
        fuse_unmount(merged, runner=runner)
        _discard_lane_tree(lane)
        raise LaneError(f"cannot bind the lane's guest-state clone: {error}") from error
    result.changed.append(
        f"bound {merged} through {selected.launcher} bind-clone {base_path}"
    )

    record: dict[str, Any] = {
        "schema": LANE_SCHEMA,
        "name": name,
        "created": (clock or _utc_now)(),
        "base": identity,
        "runtime": runtime_identity,
        "source": identity_source,
        "root": str(Path(root)),
    }
    (lane / LANE_MARKER).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return result, lane


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _tail_text(path: Path, limit: int = 20) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return "no diagnostic output was captured"
    return "\n".join(lines[-limit:]) or "no diagnostic output was captured"


def _discard_lane_tree(lane: Path) -> None:
    """Remove a lane's directory tree. Only called on a lane we just made."""

    shutil.rmtree(lane, ignore_errors=True)


# --------------------------------------------------------------------------
# Destroy.
# --------------------------------------------------------------------------


def destroy_lane(
    lane: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    kill_func: Callable[[int, int], None] = os.kill,
    sleep_func: Callable[[float], None] = time.sleep,
) -> LaneResult:
    """Stop and remove exactly one lane.

    Order matters and each step is refused rather than forced: stop the lane's
    cgroup, reap processes that provably belong to this prefix, unmount the
    lane's own layer, then delete. Nothing here can reach another lane, and the
    directory is never removed while it is still mounted or still owned.
    """

    from test_prefix import cleanup_rootless_prefix_processes, prefix_process_ownership_snapshot

    result = LaneResult()
    lane = Path(lane)
    record = read_record(lane)
    if record is None:
        raise LaneError(
            f"'{lane}' holds no {LANE_MARKER} with schema {LANE_SCHEMA}; refusing to remove "
            "a directory that is not a lane"
        )
    merged = lane_prefix(lane)
    unit = scope_unit(lane)

    try:
        stopped = stop_own_scope(unit, lane=lane, runner=runner)
    except LaneError as error:
        result.problems.append(str(error))
        return result
    result.changed.extend(stopped.changed)
    result.problems.extend(stopped.problems)

    # Not guarded by `merged.is_dir()`: a disconnected FUSE mount reports False
    # for every stat, and the ownership evidence that still works there is the
    # process environment, which must not be skipped for that reason.
    cleanup = cleanup_rootless_prefix_processes(
        merged, kill_func=kill_func, sleep_func=sleep_func
    )
    result.changed.extend(cleanup.changed)
    result.problems.extend(cleanup.problems)
    leftover = prefix_process_ownership_snapshot(merged)
    if leftover:
        result.problems.append(
            "processes still own this lane's prefix: " + "; ".join(leftover)
        )

    # Unmount BEFORE reaping the daemon. A graceful unmount asks the daemon to release the connection,
    # after which it exits on its own; killing it first turns the mount into a disconnected one, which
    # is exactly the state in which an unmount can block in uninterruptible sleep. MEASURED: a killed
    # lane daemon left `fusermount3 -u` in D state and the next run hung on its first unmount.
    if merged.is_dir() or mount_targets(merged, mountinfo_path=mountinfo_path):
        unmount = fuse_unmount(merged, runner=runner)
        result.changed.extend(unmount.changed)
        result.problems.extend(unmount.problems)

    for pid in fuse_daemon_pids(lane):
        try:
            kill_func(pid, 9)
            result.changed.append(f"stopped the lane's fuse-overlayfs daemon pid={pid}")
        except OSError as error:
            if error.errno != errno.ESRCH:
                result.problems.append(f"cannot stop fuse-overlayfs pid={pid}: {error}")

    remaining = mount_targets(merged, mountinfo_path=mountinfo_path) if merged.exists() else []
    if remaining:
        result.problems.append(
            "refusing to delete a mounted lane: " + ", ".join(str(item) for item in remaining)
        )
        return result
    if result.problems:
        return result

    shutil.rmtree(lane)
    result.changed.append(f"removed {lane}")
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
        "upper_bytes": _directory_bytes(lane / UPPER_DIRNAME),
        "upper_files": _directory_files(lane / UPPER_DIRNAME),
    }
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
    """Assert the lane still describes its base, and that the base is unchanged."""

    result = LaneResult()
    lane = Path(lane)
    record = read_record(lane)
    if record is None:
        raise LaneError(f"'{lane}' holds no {LANE_MARKER} with schema {LANE_SCHEMA}")
    for problem in record_problems(lane, record):
        result.problems.append(problem)
    base = record.get("base")
    if isinstance(base, dict) and isinstance(base.get("path"), str):
        current = Path(base["path"])
        if not current.is_dir():
            result.problems.append(f"base '{current}' is gone")
        else:
            stat = os.stat(current)
            if (stat.st_dev, stat.st_ino) != (base.get("device"), base.get("inode")):
                result.problems.append(
                    f"base '{current}' was replaced (device/inode changed)"
                )
            else:
                # Identity first: path, size, mtime, inode and mode. It catches a replacement or a
                # touch that a content hash would call identical, and it is not a content proof.
                digest = tree_identity(current)
                if digest != base.get("identity"):
                    result.problems.append(
                        f"base '{current}' changed since the lane was created "
                        "(a lane must never write through its lower layer)"
                    )
                else:
                    result.changed.append(f"base '{current}' tree identity is unchanged")
    runtime = record.get("runtime")
    if isinstance(runtime, dict):
        root = runtime.get("root")
        current_runtime = Path(root) if isinstance(root, str) else None
        if current_runtime is None or not current_runtime.is_dir():
            result.problems.append(f"runtime installation is gone: {root!r}")
        else:
            recorded_manifest = runtime.get("artifact_manifest")
            if isinstance(recorded_manifest, dict) and recorded_manifest.get("path"):
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


def _directory_bytes(path: Path) -> int:
    total = 0
    for directory, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(Path(directory) / name).st_size
            except OSError:
                continue
    return total


def _directory_files(path: Path) -> int:
    count = 0
    for _directory, _dirnames, filenames in os.walk(path):
        count += len(filenames)
    return count


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

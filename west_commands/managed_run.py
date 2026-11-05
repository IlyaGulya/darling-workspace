"""Producer-owned experiments: scoped execution, durable evidence and retirement.

The outer owner deliberately stays outside the guest's scope. A command's exit
is not cleanup: the native lane lifecycle must stop the scope and publish the
closed payload before removing any guest state. Recovery uses the same path.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
import uuid
from typing import Any, Callable, Iterator, Mapping

try:
    from .evidence_retention import _process_identity, ack_run_retirement, finalize_run_evidence, finalize_unstarted_run_evidence, prune_run_evidence, register_job_owner
    from .lane import (
        LANE_MARKER, RETIRED_MARKER, LaneError, _lane_admission, _lane_namespace,
        create_lane, destroy_lane, lane_path, read_record, scope_unit, stop_own_scope,
        west_command_argv,
    )
    from .runtime_retention import leased_runtime, pin_runtime, prune_runtimes, runtime_use, unpin_runtime
    from .test_prefix import cleanup_verified_prefix_processes, prefix_lease
    from .test_store import durable_root, write_json
except ImportError:
    from evidence_retention import _process_identity, ack_run_retirement, finalize_run_evidence, finalize_unstarted_run_evidence, prune_run_evidence, register_job_owner
    from lane import (
        LANE_MARKER, RETIRED_MARKER, LaneError, _lane_admission, _lane_namespace,
        create_lane, destroy_lane, lane_path, read_record, scope_unit, stop_own_scope,
        west_command_argv,
    )
    from runtime_retention import leased_runtime, pin_runtime, prune_runtimes, runtime_use, unpin_runtime
    from test_prefix import cleanup_verified_prefix_processes, prefix_lease
    from test_store import durable_root, write_json

SCHEMA = 1
KIND = "managed-lane-run"
RUN_ENV = "DW_MANAGED_RUN"
RUN_RECORD = "run.json"
_RUN_NAME = re.compile(r"run-[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class RetentionPolicy:
    min_free_bytes: int = 30 * 2**30
    evidence_keep_last: int = 5
    evidence_max_bytes: int = 2**30
    runtime_keep_last: int = 2

    def validate(self) -> None:
        if (self.min_free_bytes < 0 or self.evidence_keep_last < 1
                or self.evidence_max_bytes < 1 or self.runtime_keep_last < 2):
            raise LaneError("invalid retention bounds: preserve the newest evidence and two images")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _start_time(pid: int) -> str | None:
    try:
        # comm may contain spaces and parentheses; fields after its last ')' start at state.
        fields = (Path("/proc") / str(pid) / "stat").read_bytes().rsplit(b")", 1)[1].split()
        return None if fields[0] in {b"Z", b"X", b"x"} else fields[19].decode()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, IndexError, UnicodeError) as error:
        raise LaneError(f"process identity is unavailable for pid {pid}: {error}") from error


def _owner_alive(record: Mapping[str, Any]) -> bool:
    owner = record.get("owner", {})
    try:
        current = _start_time(int(owner["pid"]))
    except (KeyError, TypeError, ValueError, LaneError):
        return True
    return current is not None and current == owner.get("start_time")


def _safe_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise LaneError(f"managed state needs an absolute, unambiguous path: {path}")
    for component in (path, *path.parents):
        if os.path.lexists(component) and component.is_symlink():
            raise LaneError(f"managed state cannot follow a symlink: {component}")
    return path


def owner_root(
    topdir: Path, declared: Path | None, environ: Mapping[str, str], *, create: bool = True,
) -> Path:
    root = _safe_path(declared if declared is not None else durable_root(topdir, environ))
    if create:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.exists():
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise LaneError(f"managed state root is not an owned directory: {root}")
    return root


def _read_json(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise LaneError(f"managed record is not an owned regular file: {path}")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise LaneError(f"invalid managed record: {path}")
    return value


def read_run(run: Path, *, root: Path | None = None) -> dict[str, Any]:
    run = _safe_path(run)
    metadata = run.lstat()
    data = _read_json(run / RUN_RECORD)
    expected_root = _safe_path(root if root is not None else Path(data.get("root", "")))
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or run.parent != expected_root / "runs" or not _RUN_NAME.fullmatch(run.name)
            or data.get("schema") != SCHEMA or data.get("kind") != KIND
            or data.get("id") != run.name.removeprefix("run-")
            or data.get("uid") != os.geteuid() or data.get("path") != str(run)
            or data.get("root") != str(expected_root)
            or data.get("directory") != {"device": metadata.st_dev, "inode": metadata.st_ino}):
        raise LaneError(f"run record does not own this directory: {run}")
    selected = data.get("lane")
    if not isinstance(selected, dict):
        raise LaneError(f"run record has no owned lane namespace: {run}")
    try:
        selected_root = _safe_path(Path(selected["root"]))
        selected_path = lane_path(selected_root, selected["name"])
    except (KeyError, TypeError, ValueError) as error:
        raise LaneError(f"run record has an invalid lane namespace: {run}") from error
    if (not selected_root.is_relative_to(expected_root)
            or selected.get("path") != str(selected_path) or selected.get("token") != data["id"]):
        raise LaneError(f"run record does not own its declared lane namespace: {run}")
    return data


@contextmanager
def _run_lock(run: Path, *, create: bool = False) -> Iterator[None]:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(run / ".lock", flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise LaneError(f"run lease is not an owned regular file: {run}")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not os.path.samestat(metadata, (run / ".lock").lstat()):
            raise LaneError(f"run lease changed during admission: {run}")
        yield
    finally:
        os.close(fd)


def _save(data: dict[str, Any], **changes: Any) -> None:
    data.update(changes)
    data["updated"] = _now()
    write_json(Path(data["path"]) / RUN_RECORD, data)


def authorize_lane_execution(lane: Path, record: Mapping[str, Any]) -> None:
    """Prevent an unrelated exec from entering an owner-controlled temporary lane."""
    managed = record.get("managed_run")
    if managed is None:
        return
    run = Path(managed["path"])
    data = read_run(run)
    _matching_lane(data, lane, record)
    if data.get("phase") == "retained" and data.get("keep"):
        return
    if data.get("phase") != "running" or os.environ.get(RUN_ENV) != data["id"]:
        raise LaneError("temporary lane belongs to its managed run; use that owner or explicitly keep it")
    try:
        with _run_lock(run):
            raise LaneError("managed run owner has released its lease; recover it before executing")
    except BlockingIOError:
        if not _owner_alive(data):
            raise LaneError("managed run owner identity changed; recover it before executing")


def _active_payload_owner(data: Mapping[str, Any]) -> dict[str, Any] | None:
    path = Path(data["path"]) / "payload-owner.json"
    if not os.path.lexists(path):
        return None
    reference = _read_json(path)
    if (reference.get("schema") != 1 or reference.get("kind") != "managed-run-payload-owner"
            or reference.get("id") != data["id"] or reference.get("run") != data["path"]
            or reference.get("directory") != data["directory"]
            or reference.get("run_owner") != data["owner"]):
        raise LaneError("payload controller reference belongs to another run generation")
    owner = reference.get("owner", {})
    pid = owner.get("pid")
    if type(pid) is not int or pid <= 0:
        raise LaneError("invalid payload controller identity")
    identity = _process_identity(pid)
    if identity is None or identity["start_time"] != owner.get("start_time"):
        return None
    if identity["uid"] != os.geteuid():
        raise LaneError("payload controller changed user identity")
    ancestor, visited = pid, set()
    while ancestor > 0 and ancestor not in visited:
        visited.add(ancestor)
        current = _process_identity(ancestor)
        if current is None:
            break
        if ancestor == data["owner"]["pid"] and current["start_time"] == data["owner"]["start_time"]:
            return owner
        ancestor = current["ppid"]
    raise LaneError("payload controller is not descended from this managed run owner")


def interrupt_payload(data: Mapping[str, Any], signum: int) -> bool:
    owner = _active_payload_owner(data)
    if owner is None:
        return False
    try:
        descriptor = os.pidfd_open(owner["pid"])
    except ProcessLookupError:
        return False
    try:
        if _start_time(owner["pid"]) != owner["start_time"]:
            return False
        signal.pidfd_send_signal(descriptor, signum)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(descriptor)


def run_lane_payload(argv: list[str], environment: dict[str, str], managed: Mapping[str, Any]) -> int:
    """Forward cancellation to the actual command, not an opaque launch wrapper."""
    run = Path(managed["path"])
    data = read_run(run)
    if data.get("phase") == "retained" and data.get("keep"):
        return subprocess.call(argv, env=environment)
    if data.get("phase") != "running" or environment.get(RUN_ENV) != data["id"]:
        raise LaneError("managed payload lost its active run owner")
    if _active_payload_owner(data) is not None:
        # Nested commands remain below the already registered foreground owner.
        return subprocess.call(argv, env=environment)
    child, interrupted, previous = None, 0, {}
    def forward(signum, frame):
        nonlocal interrupted
        interrupted = interrupted or signum
        if child is not None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, forward)
        write_json(run / "payload-owner.json", {
            "schema": 1, "kind": "managed-run-payload-owner", "id": data["id"],
            "run": str(run), "directory": data["directory"], "run_owner": data["owner"],
            "owner": {"pid": os.getpid(), "start_time": _start_time(os.getpid())},
        })
        child = subprocess.Popen(argv, env=environment)
        if interrupted:
            child.send_signal(interrupted)
        return child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _matching_lane(data: Mapping[str, Any], lane: Path, record: Mapping[str, Any]) -> None:
    selected = data.get("lane", {})
    metadata = lane.lstat()
    managed = record.get("managed_run", {})
    identity = {"device": metadata.st_dev, "inode": metadata.st_ino}
    if (selected.get("path") != str(lane) or selected.get("name") != lane.name
            or selected.get("root") != str(lane.parent)
            or managed.get("id") != data.get("id") or managed.get("path") != data.get("path")
            or {k: managed.get(k) for k in identity} != identity
            or metadata.st_uid != os.geteuid() or not stat.S_ISDIR(metadata.st_mode)
            or ("device" in selected and any(selected.get(k) != v for k, v in identity.items()))
            or record.get("runtime", {}).get("root") != data.get("runtime_root")):
        raise LaneError(f"run does not own this lane generation: {lane}")


def list_runs(root: Path) -> list[dict[str, Any]]:
    store = _safe_path(root) / "runs"
    if not store.exists():
        return []
    rows = []
    for run in sorted(store.iterdir()):
        try:
            rows.append(read_run(run, root=root))
        except (OSError, ValueError, LaneError) as error:
            rows.append({"path": str(run), "phase": "unknown", "preserved": True, "reason": str(error)})
    return rows


def collect_retired(root: Path, policy: RetentionPolicy, *, protect=()) -> list[dict[str, Any]]:
    try:
        rows = prune_run_evidence(
            root, keep_last=policy.evidence_keep_last, max_bytes=policy.evidence_max_bytes,
            protect=protect,
        )
    except (OSError, RuntimeError, ValueError) as error:
        rows = [{"action": "failed", "kind": "run-evidence", "path": str(root), "reason": str(error)}]
    problems = []
    for row in rows:
        if row.get("action") == "deleted":
            try:
                if row.get("runtime_root"):
                    unpin_runtime(Path(row["runtime_root"]), row["runtime_pin_key"])
                    unpin_runtime(Path(row["runtime_root"]), "run:" + row["path"])
                if not ack_run_retirement(row):
                    raise LaneError("run-evidence retirement acknowledgement was refused")
            except (OSError, RuntimeError, ValueError) as error:
                problems.append({"action": "failed", "kind": "runtime-reference",
                                 "path": row["path"], "reason": str(error)})
    rows.extend(problems)
    try:
        rows.extend(prune_runtimes(root, keep_last=policy.runtime_keep_last))
    except (OSError, RuntimeError, ValueError) as error:
        rows.append({"action": "failed", "kind": "runtime-images", "path": str(root), "reason": str(error)})
    return rows


def require_budget(root: Path, minimum: int) -> int:
    available = shutil.disk_usage(root).free
    if available < minimum:
        raise LaneError(
            f"disk reserve prevents a new experiment: {available} bytes free, {minimum} required; "
            "pinned, active and unknown state was preserved"
        )
    return available


class _Command:
    """Bounded-memory live tee; closed files are handed to the evidence publisher."""

    def __init__(self, argv: list[str], run: Path, environ: dict[str, str], *, json_output: bool):
        self.selector = selectors.DefaultSelector()
        self.outputs = {
            "stdout": (run / "command.stdout").open("wb", buffering=0),
            "stderr": (run / "command.stderr").open("wb", buffering=0),
        }
        self.closed = False
        self.complete = True
        self.forward = {"stdout": 2 if json_output else 1, "stderr": 2}
        try:
            self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ)
        except BaseException:
            self.close(strict=False)
            raise
        for name, stream in [("stdout", self.process.stdout), ("stderr", self.process.stderr)]:
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, name)

    def _read(self, key) -> bool:
        data = os.read(key.fileobj.fileno(), 65536)
        if not data:
            self.selector.unregister(key.fileobj)
            key.fileobj.close()
            return False
        self.outputs[key.data].write(data)
        destination = self.forward.get(key.data)
        if destination is not None:
            try:
                remaining = memoryview(data)
                while remaining:
                    remaining = remaining[os.write(destination, remaining):]
            except BrokenPipeError:
                self.forward[key.data] = None
        return True

    def wait(self, cancelled: Callable[[], int], interrupt: Callable[[], bool] | None = None) -> int | None:
        cancellation_deadline = None
        forwarded = False
        while self.selector.get_map() or self.process.poll() is None:
            if cancelled():
                if interrupt is None:
                    try:
                        self.process.terminate()
                    except ProcessLookupError:
                        pass
                    return None
                if cancellation_deadline is None:
                    cancellation_deadline = time.monotonic() + 30
                if not forwarded:
                    forwarded = interrupt()
                if time.monotonic() >= cancellation_deadline:
                    return None
            for key, _ in self.selector.select(timeout=0.2):
                try:
                    self._read(key)
                except BlockingIOError:
                    pass
        return self.process.wait()

    def close(self, *, strict: bool = True) -> None:
        if self.closed:
            return
        self.closed = True
        incomplete = False
        try:
            for key in list(self.selector.get_map().values()):
                try:
                    while self._read(key):
                        pass
                except BlockingIOError:
                    incomplete = True
            if incomplete:
                self.complete = False
        finally:
            for key in list(self.selector.get_map().values()):
                key.fileobj.close()
            self.selector.close()
            for stream in self.outputs.values():
                stream.close()
        process = getattr(self, "process", None)
        if process is not None:
            if strict:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    incomplete = True
                    self.complete = False
            else:
                process.poll()
        if incomplete and strict:
            raise LaneError("command process or output writer remains after its scope stopped; raw state retained")


def _outcome(data: Mapping[str, Any], command: _Command | None) -> None:
    target = Path(data["path"]) / "outcome.json"
    if target.exists():
        return
    write_json(target, {
        "run_id": data["id"], "workload_rc": data.get("workload_rc"),
        "cancel_signal": data.get("cancel_signal"), "error": data.get("error"),
        "host_streams_complete": command.complete if command else data.get(
            "host_streams_complete", True if data.get("command_started", False) is False else None,
        ),
        "command_started": data.get("command_started", False),
    })


def _result_rc(data: Mapping[str, Any], *, cleanup_failed: bool = False) -> int:
    if data.get("cancel_signal"):
        return 128 + int(data["cancel_signal"])
    code = data.get("workload_rc")
    if code is None:
        return 1
    if code < 0:
        return 128 - code
    return code or (1 if cleanup_failed or data.get("error") else 0)


def _finish(data: dict[str, Any], command: _Command | None = None) -> dict[str, Any]:
    run = Path(data["path"])
    lane = Path(data["lane"]["path"])
    record = read_record(lane) if (lane / LANE_MARKER).exists() else None
    if record is not None:
        _matching_lane(data, lane, record)
        def capture(selected: Path, current: Mapping[str, Any]) -> None:
            _matching_lane(data, selected, current)
            if command:
                try:
                    command.close()
                finally:
                    _save(data, host_streams_complete=command.complete, workload_rc=command.process.returncode)
            _outcome(data, command)
            _save(data, phase="capturing")
            evidence = finalize_run_evidence(selected, run)
            if not evidence.get("complete"):
                raise LaneError("required run evidence is incomplete; lane/raw state retained")
            pin_runtime(Path(data["runtime_root"]), "evidence:" + str(run))
            unpin_runtime(Path(data["runtime_root"]), "run:" + str(run))
            _save(data, phase="retiring", evidence=evidence)
        result = destroy_lane(lane, before_retire=capture)
        if not result.success:
            _save(data, phase="blocked", cleanup={"success": False, "problems": result.problems},
                  final_rc=_result_rc(data, cleanup_failed=True))
            return data
        cleanup = {"success": True, "changed": result.changed, "problems": []}
    else:
        if command:
            command.close()
            _save(data, host_streams_complete=command.complete, workload_rc=command.process.returncode)
        _outcome(data, command)
        if lane.exists():
            with _lane_admission(lane, exclusive=True):
                _lane_namespace(lane)
                retired = _read_json(lane / RETIRED_MARKER)
                metadata = lane.stat()
                if (retired.get("device"), retired.get("inode")) != (metadata.st_dev, metadata.st_ino):
                    raise LaneError("retired lane namespace identity changed")
                selected = data.get("lane", {})
                if "device" in selected and (selected["device"], selected["inode"]) != (metadata.st_dev, metadata.st_ino):
                    raise LaneError("run's retired lane namespace was replaced")
                if data.get("runtime_root"):
                    unpin_runtime(Path(data["runtime_root"]), "lane:" + str(lane))
        elif data.get("command_started", False) is not False:
            raise LaneError("an executed run lost its lane and retirement marker; preserving evidence")
        if not data.get("evidence", {}).get("complete"):
            if data.get("command_started", False) is not False:
                raise LaneError("executed run lost its retained payload; no evidence will be invented")
            # A failed create has no guest workload or invented runtime result.
            _save(data, phase="creating")
            evidence = finalize_unstarted_run_evidence(run)
            if not evidence.get("complete"):
                raise LaneError("unstarted run evidence is incomplete")
            _save(data, evidence=evidence)
        cleanup = {"success": True, "changed": ["no remaining owned lane data"], "problems": []}
    if data.get("runtime_root"):
        unpin_runtime(Path(data["runtime_root"]), "run:" + str(run))
    _save(data, phase="completed", completed=_now(), keep=False, cleanup=cleanup, final_rc=_result_rc(data))
    return data


def _keep(data: dict[str, Any], command: _Command | None) -> dict[str, Any]:
    lane = Path(data["lane"]["path"])
    current = read_record(lane)
    if current is None:
        return _finish(data, command)
    _matching_lane(data, lane, current)
    stopped = stop_own_scope(scope_unit(lane), lane=lane)
    problems = list(stopped.problems)
    if not problems:
        with _lane_admission(lane, exclusive=True), prefix_lease(lane / "merged", blocking=False):
            problems.extend(cleanup_verified_prefix_processes(lane / "merged").problems)
    if command:
        command.close(strict=False)
        _save(data, host_streams_complete=command.complete, workload_rc=command.process.returncode)
    _outcome(data, command)
    _save(data, phase="retained" if not problems else "blocked", keep=True,
          cleanup={"success": False, "retained": True, "problems": problems},
          final_rc=_result_rc(data, cleanup_failed=bool(problems)))
    return data


def recover_runs(root: Path, *, run: Path | None = None, release_kept: bool = False) -> list[dict[str, Any]]:
    rows = [read_run(run, root=root)] if run is not None else list_runs(root)
    results = []
    for candidate in rows:
        path = Path(candidate["path"])
        if candidate.get("phase") in {"completed", "unknown"}:
            continue
        try:
            with _run_lock(path):
                data = read_run(path, root=root)
                if data.get("keep") and not release_kept:
                    results.append({"path": str(path), "action": "keep", "reason": "explicitly retained"})
                    continue
                if data.get("phase") not in {"retained", "blocked"} and _owner_alive(data):
                    results.append({"path": str(path), "action": "keep", "reason": "owner identity is still live"})
                    continue
                if data.get("phase") == "retained":
                    selected = Path(data["lane"]["path"])
                    with _lane_admission(selected, exclusive=True):
                        _save(data, phase="retiring", keep=False)
                previous = data["owner"]
                _save(data, previous_owner=previous,
                      owner={"pid": os.getpid(), "start_time": _start_time(os.getpid())})
                try:
                    if os.environ.get("WEST_JOB_STATE_DIR"):
                        register_job_owner(Path(os.environ["WEST_JOB_STATE_DIR"]), path)
                    runtime = data.get("runtime_root")
                    with runtime_use(Path(runtime)) if runtime else nullcontext():
                        result = _finish(data)
                    results.append({"path": str(path), "action": "recovered" if result["phase"] == "completed" else "keep",
                                    "reason": result["phase"], "result": result})
                except (OSError, RuntimeError, ValueError) as error:
                    _save(data, phase="blocked", cleanup={"success": False, "problems": [str(error)]},
                          final_rc=_result_rc(data, cleanup_failed=True))
                    results.append({"path": str(path), "action": "keep", "reason": str(error)})
        except BlockingIOError:
            results.append({"path": str(path), "action": "keep", "reason": "owner/reader lease is busy"})
        except (OSError, RuntimeError, ValueError) as error:
            results.append({"path": str(path), "action": "keep", "reason": str(error)})
    return results




def set_run_pin(root: Path, run: Path, pinned: bool) -> dict[str, Any]:
    with _run_lock(run):
        data = read_run(run, root=root)
        _save(data, pin=bool(pinned))
        return data


def run_experiment(
    root: Path, lane_root: Path, argv: list[str], *, base: Path | None = None,
    parent: str | None = None, runtime_root: Path | None = None, source: Path | None = None,
    name: str | None = None, allow_root: bool = False, keep: bool = False, pin: bool = False,
    policy: RetentionPolicy = RetentionPolicy(), emit: Callable[[str], None] = print,
    json_output: bool = False,
) -> dict[str, Any]:
    policy.validate()
    root = _safe_path(root); lane_root = _safe_path(lane_root)
    if lane_root != root and root not in lane_root.parents:
        raise LaneError("temporary lane root must be inside its declared state owner root")
    if not argv:
        raise LaneError("a managed experiment requires a command")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.stat().st_uid != os.geteuid():
        raise LaneError("experiment state root has another owner")
    recover_runs(root)
    collect_retired(root, policy)
    run_id = uuid.uuid4().hex
    name = name or "r" + run_id[:12]
    selected_lane = lane_path(lane_root, name)
    if os.path.lexists(selected_lane):
        raise LaneError("managed experiments require a new lane name; existing namespaces are not adopted")
    runs = _safe_path(root / "runs"); runs.mkdir(mode=0o700, exist_ok=True)
    run = runs / ("run-" + run_id); run.mkdir(mode=0o700)
    metadata = run.stat()
    data = {
        "schema": SCHEMA, "kind": KIND, "id": run_id, "uid": os.geteuid(),
        "root": str(root), "path": str(run), "directory": {"device": metadata.st_dev, "inode": metadata.st_ino},
        "created": _now(), "phase": "preparing",
        "owner": {"pid": os.getpid(), "start_time": _start_time(os.getpid())},
        "lane": {"path": str(selected_lane), "name": name, "root": str(lane_root), "token": run_id},
        "runtime_root": str(runtime_root) if runtime_root else None, "argv": argv,
        "keep": keep, "pin": pin, "workload_rc": None, "final_rc": None,
        "cleanup": {"success": False, "problems": []}, "evidence": {"complete": False},
        "command_started": False,
    }
    cancelled = 0
    def request_cancel(signum, frame):
        nonlocal cancelled
        cancelled = cancelled or signum
    previous_handlers = {}
    command = None
    with _run_lock(run, create=True):
        _save(data)
        emit(f"RUN={run}")
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, request_cancel)
            if os.environ.get("WEST_JOB_STATE_DIR"):
                register_job_owner(Path(os.environ["WEST_JOB_STATE_DIR"]), run)
                if (Path(os.environ["WEST_JOB_STATE_DIR"]) / "cancel-requested").exists():
                    request_cancel(signal.SIGINT, None)
            if cancelled:
                raise InterruptedError("managed run cancelled before allocation")
            require_budget(root, policy.min_free_bytes)
            if runtime_root is None and parent is not None:
                parent_record = read_record(lane_path(lane_root, parent))
                if parent_record is None:
                    raise LaneError("selected frozen parent is missing")
                runtime_root = Path(parent_record["runtime"]["root"])
            with leased_runtime(runtime_root) as runtime:
                _save(data, runtime_root=str(runtime.root), phase="creating")
                pin_runtime(runtime.root, "run:" + str(run))
                write_json(run / "invocation.json", {
                    "run_id": run_id, "argv": argv, "runtime": runtime.summary(),
                    "base": str(base) if base else None, "parent": parent,
                    "source": str(source) if source else None,
                })
                if cancelled:
                    raise InterruptedError("managed run cancelled before lane creation")
                result, lane = create_lane(
                    base, lane_root, name, parent=parent, source=source, runtime_root=runtime.root,
                    allow_root=allow_root, managed_run={"id": run_id, "path": str(run)},
                )
                if not result.success or lane is None:
                    raise LaneError("; ".join(result.problems))
                metadata = lane.stat()
                data["lane"].update(device=metadata.st_dev, inode=metadata.st_ino)
                _save(data)
                if not cancelled:
                    child_env = dict(os.environ, **{RUN_ENV: run_id, "DW_MANAGED_RUN_DIR": str(run)})
                    # This controller, not the outer job, publishes the lane's
                    # closed payload. Do not register soon-to-retire raw paths
                    # as independent evidence owned by that job.
                    child_env.pop("WEST_JOB_STATE_DIR", None)
                    _save(data, phase="running", command_started=None)
                    command = _Command(
                        [*west_command_argv(), "dev", "lane", "exec", "--root", str(lane_root),
                         name, "--", *argv], run, child_env, json_output=json_output,
                    )
                    _save(data, command_started=True)
                    rc = command.wait(lambda: cancelled, lambda: interrupt_payload(data, cancelled))
                    _save(data, workload_rc=rc, cancel_signal=cancelled or None)
                    if not cancelled:
                        command.close()
                        _save(data, host_streams_complete=command.complete)
                        _outcome(data, command)
                else:
                    _save(data, cancel_signal=cancelled)
                if keep:
                    _keep(data, command)
                else:
                    _finish(data, command)
        except (OSError, RuntimeError, ValueError) as error:
            _save(data, error=str(error), cancel_signal=cancelled or None)
            try:
                _finish(data, command)
            except (OSError, RuntimeError, ValueError) as cleanup_error:
                _save(data, phase="blocked", final_rc=_result_rc(data, cleanup_failed=True),
                      cleanup={"success": False, "problems": [str(cleanup_error)]})
        finally:
            if command:
                command.close(strict=False)
            if cancelled:
                _save(data, cancel_signal=cancelled, final_rc=128 + cancelled)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    data["retention"] = collect_retired(root, policy, protect=(run,))
    return data

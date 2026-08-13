#!/usr/bin/env python3
"""Bounded real-guest acceptance for the opt-in lifecycle writer cohort.

This is an external product observer and fault driver.  It does not implement
the lifecycle reducer or namespace mutation policy; those remain in Rust.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import select
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable

import jsonschema


COMMAND_TIMEOUT_SECONDS = 90.0
COMMAND_KILL_GRACE_SECONDS = 5.0
COMMAND_OUTPUT_BYTES = 1024 * 1024
TRANSITION_TIMEOUT_SECONDS = 30.0
MAX_PROCESSES = 4096
MAX_FDS_PER_PROCESS = 4096
MAX_TREE_ENTRIES = 200_000
MAX_IDENTITY_BYTES = 64 * 1024
MAX_GIT_OUTPUT_BYTES = 1024

RUNTIME_ENDPOINTS = {
    ".init.pid": "regular",
    ".darlingserver.sock": "socket",
    ".lc-v1.sock": "socket",
    "var/run/shellspawn.sock": "socket",
    "var/tmp/launchd/sock": "socket",
}
FORBIDDEN_TAIL_PREFIXES = (
    ".lifecycle-stage-",
    ".lifecycle-quarantine-",
    ".lifecycle-gc-",
)


class ContractError(RuntimeError):
    pass


def _read_json_bounded(path: Path, limit: int) -> Any:
    data = path.read_bytes()
    if len(data) > limit:
        raise ContractError(f"{path}: JSON exceeds {limit} bytes")
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path}: malformed JSON") from error


def _git_oid(repository: Path, expression: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", expression],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractError(f"git identity unavailable for {repository}") from error
    if (
        result.returncode != 0
        or len(result.stdout) > MAX_GIT_OUTPUT_BYTES
        or len(result.stderr) > MAX_GIT_OUTPUT_BYTES
    ):
        raise ContractError(f"git identity rejected for {repository}")
    value = result.stdout.decode("ascii", errors="strict").strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ContractError(f"git identity malformed for {repository}")
    return value


def _contained_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ContractError(f"identity path escapes trusted root: {relative}") from error
    return candidate


def _validate_source_identity(
    identity: dict[str, Any],
    *,
    schema: dict[str, Any],
    workspace: Path,
    evidence_root: Path,
    prefix: Path,
) -> None:
    try:
        jsonschema.Draft202012Validator(schema).validate(identity)
    except jsonschema.ValidationError as error:
        raise ContractError(f"source identity schema rejected: {error.message}") from error

    composed_darling = _contained_path(evidence_root, "composed/darling")
    composed_darlingserver = _contained_path(
        evidence_root, "composed/darling/src/external/darlingserver"
    )
    exact_oids = {
        "workspace.commit": _git_oid(workspace, "HEAD^{commit}"),
        "workspace.tree": _git_oid(workspace, "HEAD^{tree}"),
        "composition.darling_commit": _git_oid(composed_darling, "HEAD^{commit}"),
        "composition.darling_tree": _git_oid(composed_darling, "HEAD^{tree}"),
        "composition.darlingserver_commit": _git_oid(
            composed_darlingserver, "HEAD^{commit}"
        ),
        "composition.darlingserver_tree": _git_oid(
            composed_darlingserver, "HEAD^{tree}"
        ),
    }
    for dotted, actual in exact_oids.items():
        section, field = dotted.split(".")
        if identity[section][field] != actual:
            raise ContractError(f"source identity mismatch: {dotted}")

    source_map = _contained_path(
        evidence_root, identity["composition"]["canonical_source_map"]
    )
    if _sha256(source_map) != identity["composition"]["canonical_source_map_sha256"]:
        raise ContractError("canonical source map digest mismatch")
    source_map_data = _read_json_bounded(source_map, MAX_IDENTITY_BYTES)
    if (
        not isinstance(source_map_data, dict)
        or source_map_data.get("schema") != "homebrew-retained-source-v1"
        or not isinstance(source_map_data.get("modules"), dict)
        or len(source_map_data["modules"]) != 8
    ):
        raise ContractError("canonical source map schema rejected")
    for module, material in sorted(source_map_data["modules"].items()):
        if (
            not isinstance(material, dict)
            or set(material) != {"commit", "tree"}
            or not isinstance(module, str)
        ):
            raise ContractError("canonical source map module rejected")
        repository = _contained_path(evidence_root / "composed", module)
        if _git_oid(repository, f"{material['commit']}^{{commit}}") != material["commit"]:
            raise ContractError(f"canonical source map commit mismatch: {module}")
        if _git_oid(repository, f"{material['commit']}^{{tree}}") != material["tree"]:
            raise ContractError(f"canonical source map tree mismatch: {module}")

    closure_paths = {
        "cohort_routing_rs_sha256": workspace
        / "lifecycle/operation-boundary/src/cohort_routing.rs",
        "cohort_header_sha256": workspace
        / "lifecycle/operation-boundary/include/darling_lifecycle_cohort.h",
        "cohort_client_c_sha256": composed_darling
        / "src/lifecycle/lifecycle_cohort_client.c",
        "launchd_ipc_c_sha256": composed_darling / "src/launchd/src/ipc.c",
        "shellspawn_c_sha256": composed_darling / "src/shellspawn/shellspawn.c",
        "darlingserver_cpp_sha256": composed_darlingserver / "src/darlingserver.cpp",
        "namespace_inventory_sha256": workspace
        / "lifecycle/namespace-writer-inventory-v1.json",
        "routing_harness_sha256": workspace
        / "tests/fixtures/lifecycle-cohort-v1/cohort_client_harness.c",
        "routing_contract_sha256": workspace
        / "tests/west_test_contracts/lifecycle_cohort_routing_contract.py",
        "routing_wrapper_sha256": workspace
        / "tests/run-lifecycle-cohort-routing-contract.sh",
        "routing_documentation_sha256": workspace
        / "docs/lifecycle-cohort-routing-v1.md",
        "writer_documentation_sha256": workspace
        / "docs/rootless-namespace-writers-v1.md",
        "deployed_contract_sha256": Path(__file__).resolve(strict=True),
        "deployed_wrapper_sha256": workspace
        / "tests/run-lifecycle-cohort-deployed-contract.sh",
        "deployed_documentation_sha256": workspace
        / "docs/lifecycle-cohort-deployed-v1.md",
        "source_identity_schema_sha256": workspace
        / "schemas/lifecycle-cohort-deployed-source-v1.schema.json",
    }
    for field, path in closure_paths.items():
        if _sha256(path.resolve(strict=True)) != identity["semantic_closure"][field]:
            raise ContractError(f"semantic closure digest mismatch: {field}")

    for build_input in identity["build"]["inputs"]:
        path = _contained_path(evidence_root, build_input["path"])
        if _sha256(path) != build_input["sha256"]:
            raise ContractError(f"build input digest mismatch: {build_input['path']}")

    artifacts = {
        "darling": prefix / "bin/darling",
        "darlingserver": prefix / "bin/darlingserver",
        "launchd": prefix / "sbin/launchd",
        "shellspawn": prefix / "usr/libexec/shellspawn",
    }
    for name, path in artifacts.items():
        if _sha256(path.resolve(strict=True)) != identity["build"]["artifacts"][name]:
            raise ContractError(f"deployed artifact digest mismatch: {name}")
    state = prefix / ".darling-prefix-state-v2"
    if _sha256(state.resolve(strict=True)) != identity["deployed_prefix"]["state_sha256"]:
        raise ContractError("deployed prefix state digest mismatch")


def _validate_identity_tamper_negatives(
    identity: dict[str, Any],
    **validation: Any,
) -> None:
    mutations = (
        ("workspace-commit", ("workspace", "commit")),
        ("source-map", ("composition", "canonical_source_map_sha256")),
        ("semantic-closure", ("semantic_closure", "cohort_routing_rs_sha256")),
        ("deployed-artifact", ("build", "artifacts", "launchd")),
        ("prefix-state", ("deployed_prefix", "state_sha256")),
    )
    for name, fields in mutations:
        forged = copy.deepcopy(identity)
        target: Any = forged
        for field in fields[:-1]:
            target = target[field]
        target[fields[-1]] = "0" * 64 if len(target[fields[-1]]) == 64 else "0" * 40
        try:
            _validate_source_identity(forged, **validation)
        except ContractError:
            continue
        raise ContractError(f"source identity tamper negative accepted: {name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _bounded_child() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (COMMAND_OUTPUT_BYTES, COMMAND_OUTPUT_BYTES))


def _run(
    argv: list[str],
    *,
    env: dict[str, str],
    evidence_dir: Path,
    name: str,
    expect: str | None = None,
) -> dict[str, Any]:
    stdout_path = evidence_dir / f"{name}.stdout"
    stderr_path = evidence_dir / f"{name}.stderr"
    started = time.monotonic_ns()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            preexec_fn=_bounded_child,
        )
        try:
            returncode = process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=COMMAND_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=COMMAND_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired as error:
                    raise ContractError(f"{name}: owned process group did not terminate") from error
            raise ContractError(f"{name}: command exceeded {COMMAND_TIMEOUT_SECONDS:.0f}s")
    stdout_data = stdout_path.read_bytes()
    stderr_data = stderr_path.read_bytes()
    if len(stdout_data) > COMMAND_OUTPUT_BYTES or len(stderr_data) > COMMAND_OUTPUT_BYTES:
        raise ContractError(f"{name}: command output exceeded the bounded capture")
    if returncode != 0:
        raise ContractError(
            f"{name}: command returned {returncode}; "
            f"stdout={stdout_data[-4096:]!r} stderr={stderr_data[-4096:]!r}"
        )
    if expect is not None and expect.encode() not in stdout_data:
        raise ContractError(f"{name}: missing RPC marker {expect!r}")
    return {
        "name": name,
        "argv": argv,
        "returncode": returncode,
        "elapsed_ns": time.monotonic_ns() - started,
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
        "stdout_bytes": len(stdout_data),
        "stderr_bytes": len(stderr_data),
    }


def _read_proc_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ContractError(f"bounded proc read exceeded for {path}")
    return data


def _starttime(pid: int) -> int | None:
    try:
        raw = _read_proc_bytes(Path(f"/proc/{pid}/stat"), 64 * 1024).decode()
        _comm, fields = raw.rsplit(") ", 1)
        values = fields.split()
        if values[0] == "Z":
            return None
        return int(values[19])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _process_record(pid: int, prefix: Path) -> dict[str, Any] | None:
    process = Path(f"/proc/{pid}")
    try:
        if process.stat().st_uid != os.getuid():
            return None
        environment = _read_proc_bytes(process / "environ", 1024 * 1024).split(b"\0")
        command = _read_proc_bytes(process / "cmdline", 1024 * 1024).split(b"\0")
    except (OSError, ContractError):
        return None
    prefix_bytes = os.fsencode(str(prefix))
    owns_prefix = b"DARLING_PREFIX=" + prefix_bytes in environment
    if not owns_prefix:
        for link_name in ("exe", "cwd"):
            try:
                target = os.fsencode(os.readlink(process / link_name))
            except OSError:
                continue
            if target == prefix_bytes or target.startswith(prefix_bytes + b"/"):
                owns_prefix = True
                break
    if not owns_prefix:
        return None
    starttime = _starttime(pid)
    if starttime is None:
        return None
    return {
        "pid": pid,
        "starttime": starttime,
        "comm": (process / "comm").read_text(errors="replace").strip()[:128],
        "argv": [os.fsdecode(item) for item in command if item][:64],
    }


def _process_census(prefix: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        record = _process_record(int(entry.name), prefix)
        if record is not None:
            records.append(record)
        if len(records) > MAX_PROCESSES:
            raise ContractError("prefix process census exceeded policy maximum")
    records.sort(key=lambda item: (item["pid"], item["starttime"]))
    return records


def _shellspawn(records: list[dict[str, Any]]) -> dict[str, Any]:
    matches = []
    for record in records:
        argv = record["argv"]
        if record["comm"] == "shellspawn" or any(
            item == "/usr/libexec/shellspawn" or item.endswith("/usr/libexec/shellspawn")
            for item in argv
        ):
            matches.append(record)
    if len(matches) != 1:
        raise ContractError(f"expected exactly one live shellspawn, found {matches!r}")
    return matches[0]


def _wait(description: str, predicate: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + TRANSITION_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise ContractError(f"timed out waiting for {description}; last={last!r}")


def _endpoint_state(prefix: Path, relative: str, expected_kind: str) -> dict[str, Any]:
    path = prefix / relative
    opened = os.open(
        path,
        os.O_RDONLY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0)
        | (getattr(os, "O_PATH", 0) if expected_kind == "socket" else 0),
    )
    try:
        state = os.fstat(opened)
    finally:
        os.close(opened)
    if expected_kind == "socket" and not stat.S_ISSOCK(state.st_mode):
        raise ContractError(f"{relative}: expected Unix socket")
    if expected_kind == "regular" and not stat.S_ISREG(state.st_mode):
        raise ContractError(f"{relative}: expected regular file")
    if state.st_uid != os.getuid() or state.st_nlink != 1:
        raise ContractError(f"{relative}: invalid owner/link metadata")
    result: dict[str, Any] = {
        "relative": relative,
        "device": state.st_dev,
        "inode": state.st_ino,
        "ctime_ns": state.st_ctime_ns,
        "mode": stat.S_IMODE(state.st_mode),
        "owner": state.st_uid,
        "nlink": state.st_nlink,
    }
    if relative == ".init.pid":
        value = path.read_text().strip()
        if not value.isdigit() or int(value) <= 1:
            raise ContractError(".init.pid does not contain a usable PID")
        result["pid"] = int(value)
    return result


def _active_snapshot(prefix: Path) -> dict[str, Any]:
    endpoints = {
        relative: _endpoint_state(prefix, relative, kind)
        for relative, kind in RUNTIME_ENDPOINTS.items()
    }
    lock_path = prefix / ".lifecycle.lock"
    lock_state = lock_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(lock_state.st_mode) or lock_state.st_uid != os.getuid():
        raise ContractError("active lifecycle lock has invalid metadata")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise ContractError("active Rust controller does not retain the lifecycle lease")
    finally:
        os.close(descriptor)
    processes = _process_census(prefix)
    shellspawn = _shellspawn(processes)
    return {
        "endpoints": endpoints,
        "lock": {
            "device": lock_state.st_dev,
            "inode": lock_state.st_ino,
            "mode": stat.S_IMODE(lock_state.st_mode),
        },
        "processes": processes,
        "shellspawn": shellspawn,
    }


def _kill_exact_shellspawn(record: dict[str, Any]) -> None:
    pid = int(record["pid"])
    if _starttime(pid) != int(record["starttime"]):
        raise ContractError("shellspawn identity changed before SIGKILL")
    pidfd = os.pidfd_open(pid, 0)
    try:
        if _starttime(pid) != int(record["starttime"]):
            raise ContractError("shellspawn identity changed after pidfd acquisition")
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if not poller.poll(int(TRANSITION_TIMEOUT_SECONDS * 1000)):
            raise ContractError("killed shellspawn pidfd did not become readable")
    finally:
        os.close(pidfd)


def _fd_holders(prefix: Path) -> list[dict[str, Any]]:
    prefix_text = str(prefix)
    holders: list[dict[str, Any]] = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            descriptors = list((process / "fd").iterdir())
        except OSError:
            continue
        if len(descriptors) > MAX_FDS_PER_PROCESS:
            raise ContractError(f"PID {process.name}: FD census exceeded policy maximum")
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            normalized = target.removesuffix(" (deleted)")
            if normalized == prefix_text or normalized.startswith(prefix_text + "/"):
                holders.append(
                    {"pid": int(process.name), "fd": int(descriptor.name), "target": target}
                )
    holders.sort(key=lambda item: (item["pid"], item["fd"]))
    return holders


def _mounts(prefix: Path) -> list[str]:
    prefix_text = str(prefix)
    results = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise ContractError("malformed /proc/self/mountinfo record")
        mountpoint = fields[4].replace("\\040", " ").replace("\\011", "\t")
        if mountpoint == prefix_text or mountpoint.startswith(prefix_text + "/"):
            results.append(mountpoint)
    return sorted(results)


def _open_child_directory(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )


def _namespace_census(
    prefix: Path,
    *,
    directory_opener: Callable[[int, str], int] = _open_child_directory,
) -> tuple[list[str], list[str]]:
    root_fd = os.open(
        prefix,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_CLOEXEC
        | getattr(os, "O_NOFOLLOW", 0),
    )
    stack: list[tuple[int, str]] = [(root_fd, "")]
    endpoints: list[str] = []
    tails: list[str] = []
    count = 0
    try:
        while stack:
            directory_fd, parent = stack.pop()
            try:
                try:
                    with os.scandir(directory_fd) as iterator:
                        entries = sorted(iterator, key=lambda entry: entry.name)
                except OSError as error:
                    raise ContractError(
                        f"fail-closed namespace traversal failed at {parent or '.'}"
                    ) from error
                for entry in entries:
                    count += 1
                    if count > MAX_TREE_ENTRIES:
                        raise ContractError("prefix tree census exceeded policy maximum")
                    relative = f"{parent}/{entry.name}" if parent else entry.name
                    try:
                        state = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise ContractError(
                            f"fail-closed lstat failed at {relative}"
                        ) from error
                    if relative in RUNTIME_ENDPOINTS:
                        endpoints.append(relative)
                    if entry.name.startswith(FORBIDDEN_TAIL_PREFIXES):
                        tails.append(relative)
                    if stat.S_ISDIR(state.st_mode):
                        try:
                            child = directory_opener(directory_fd, entry.name)
                        except OSError as error:
                            raise ContractError(
                                f"fail-closed directory open failed at {relative}"
                            ) from error
                        stack.append((child, relative))
            finally:
                os.close(directory_fd)
    except BaseException:
        for descriptor, _relative in stack:
            os.close(descriptor)
        raise
    return sorted(endpoints), sorted(tails)


def _cleanup_census_negative_fixtures(evidence_dir: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="cleanup-census-", dir=evidence_dir
    ) as temporary:
        root = Path(temporary)
        (root / ".init.pid").symlink_to("missing-target")
        endpoints, tails = _namespace_census(root)
        if endpoints != [".init.pid"] or tails:
            raise ContractError("dangling endpoint symlink escaped cleanup census")
        (root / ".init.pid").unlink()
        (root / "denied").mkdir()

        def deny_directory(parent_fd: int, name: str) -> int:
            if name == "denied":
                raise PermissionError("injected unreadable subtree")
            return _open_child_directory(parent_fd, name)

        try:
            _namespace_census(root, directory_opener=deny_directory)
        except ContractError:
            pass
        else:
            raise ContractError("unreadable subtree was silently skipped")


def _assert_clean(prefix: Path) -> dict[str, Any]:
    remaining_processes = _process_census(prefix)
    remaining_endpoints, tails = _namespace_census(prefix)
    holders = _fd_holders(prefix)
    mounts = _mounts(prefix)
    lock_path = prefix / ".lifecycle.lock"
    try:
        lock_path.lstat()
    except FileNotFoundError:
        raise ContractError("persistent .lifecycle.lock disappeared")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = os.fstat(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except BlockingIOError as error:
        raise ContractError("lifecycle lock remains held after shutdown") from error
    finally:
        os.close(descriptor)
    if remaining_processes or remaining_endpoints or holders or mounts or tails:
        raise ContractError(
            "shutdown cleanup incomplete: "
            f"processes={remaining_processes!r} endpoints={remaining_endpoints!r} "
            f"fd_holders={holders!r} mounts={mounts!r} tails={tails!r}"
        )
    return {
        "processes": [],
        "endpoints": [],
        "fd_holders": [],
        "mounts": [],
        "lock_tails": [],
        "lock": {
            "device": state.st_dev,
            "inode": state.st_ino,
            "mode": stat.S_IMODE(state.st_mode),
            "exclusive_reacquire": True,
        },
    }


def _environment(prefix: Path) -> dict[str, str]:
    result = dict(os.environ)
    result.update(
        {
            "DPREFIX": str(prefix),
            "DARLING_PREFIX": str(prefix),
            "DARLING_ROOTLESS": "1",
            "DARLING_NOOVERLAYFS": "1",
            "DARLING_EUNION": "1",
            "DARLING_LIFECYCLE_COHORT_V1": "1",
        }
    )
    return result


def _rpc(
    launcher: Path,
    prefix: Path,
    evidence_dir: Path,
    commands: list[dict[str, Any]],
    name: str,
    marker: str,
) -> None:
    commands.append(
        _run(
            [str(launcher), "shell", "/bin/bash", "-c", "printf %s \"$1\"", "cohort-rpc", marker],
            env=_environment(prefix),
            evidence_dir=evidence_dir,
            name=name,
            expect=marker,
        )
    )


def _shutdown(
    launcher: Path,
    prefix: Path,
    evidence_dir: Path,
    commands: list[dict[str, Any]],
    name: str,
) -> dict[str, Any]:
    commands.append(
        _run(
            [str(launcher), "shutdown"],
            env=_environment(prefix),
            evidence_dir=evidence_dir,
            name=name,
        )
    )
    return _wait("clean shutdown", lambda: _clean_if_ready(prefix))


def _clean_if_ready(prefix: Path) -> dict[str, Any] | None:
    try:
        return _assert_clean(prefix)
    except ContractError:
        return None


def _best_effort_cleanup(launcher: Path, prefix: Path, evidence_dir: Path) -> None:
    try:
        _run(
            [str(launcher), "shutdown"],
            env=_environment(prefix),
            evidence_dir=evidence_dir,
            name="failure-cleanup-shutdown",
        )
    except Exception:
        pass
    deadline = time.monotonic() + COMMAND_KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        records = _process_census(prefix)
        if not records:
            return
        for record in records:
            try:
                pidfd = os.pidfd_open(record["pid"], 0)
            except OSError:
                continue
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except OSError:
                pass
            finally:
                os.close(pidfd)
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--source-identity", type=Path, required=True)
    parser.add_argument("--source-identity-schema", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()

    launcher = args.launcher.resolve(strict=True)
    prefix = args.prefix.resolve(strict=True)
    workspace = args.workspace_root.resolve(strict=True)
    source_identity_path = args.source_identity.resolve(strict=True)
    source_identity_schema_path = args.source_identity_schema.resolve(strict=True)
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    source_identity = _read_json_bounded(source_identity_path, MAX_IDENTITY_BYTES)
    source_identity_schema = _read_json_bounded(
        source_identity_schema_path, MAX_IDENTITY_BYTES
    )
    validation = {
        "schema": source_identity_schema,
        "workspace": workspace,
        "evidence_root": source_identity_path.parent,
        "prefix": prefix,
    }
    _validate_source_identity(source_identity, **validation)
    _validate_identity_tamper_negatives(source_identity, **validation)
    _cleanup_census_negative_fixtures(evidence_dir)
    report: dict[str, Any] = {
        "schema": "darling-lifecycle-cohort-deployed-v1",
        "source_identity": source_identity,
        "launcher": {"path": str(launcher), "sha256": _sha256(launcher)},
        "prefix": str(prefix),
        "source_identity_validation": {
            "schema": "draft-2020-12",
            "git_identities": 6,
            "semantic_closure_files": 16,
            "build_inputs": len(source_identity["build"]["inputs"]),
            "deployed_artifacts": 4,
            "tamper_negatives": 5,
            "result": "PASS",
        },
        "cleanup_census_negatives": {
            "dangling_endpoint_symlink": "REJECTED",
            "unreadable_subtree": "REJECTED",
        },
        "limits": {
            "command_timeout_seconds": COMMAND_TIMEOUT_SECONDS,
            "transition_timeout_seconds": TRANSITION_TIMEOUT_SECONDS,
            "command_output_bytes": COMMAND_OUTPUT_BYTES,
            "max_processes": MAX_PROCESSES,
            "max_fds_per_process": MAX_FDS_PER_PROCESS,
            "max_tree_entries": MAX_TREE_ENTRIES,
        },
        "commands": [],
        "cycles": [],
        "result": "FAIL",
    }
    report_path = evidence_dir / "report.json"
    try:
        _rpc(launcher, prefix, evidence_dir, report["commands"], "cycle1-rpc-before-kill", "COHORT_RPC_BEFORE_KILL_OK")
        first = _active_snapshot(prefix)
        old_shellspawn = first["shellspawn"]
        old_socket = first["endpoints"]["var/run/shellspawn.sock"]
        _kill_exact_shellspawn(old_shellspawn)

        def restarted() -> dict[str, Any] | None:
            try:
                current = _active_snapshot(prefix)
            except (ContractError, FileNotFoundError, OSError):
                return None
            new_shellspawn = current["shellspawn"]
            new_socket = current["endpoints"]["var/run/shellspawn.sock"]
            if (
                (new_shellspawn["pid"], new_shellspawn["starttime"])
                == (old_shellspawn["pid"], old_shellspawn["starttime"])
                or (new_socket["device"], new_socket["inode"], new_socket["ctime_ns"])
                == (old_socket["device"], old_socket["inode"], old_socket["ctime_ns"])
            ):
                return None
            return current

        after_restart = _wait("KeepAlive shellspawn restart and endpoint republish", restarted)
        _rpc(launcher, prefix, evidence_dir, report["commands"], "cycle1-rpc-after-restart", "COHORT_RPC_AFTER_RESTART_OK")
        clean_one = _shutdown(launcher, prefix, evidence_dir, report["commands"], "cycle1-shutdown")
        report["cycles"].append(
            {
                "cycle": 1,
                "initial": first,
                "shellspawn_sigkill": {
                    "old": old_shellspawn,
                    "new": after_restart["shellspawn"],
                    "old_socket": old_socket,
                    "new_socket": after_restart["endpoints"]["var/run/shellspawn.sock"],
                    "rpc_after_restart": True,
                },
                "cleanup": clean_one,
            }
        )

        _rpc(launcher, prefix, evidence_dir, report["commands"], "cycle2-rpc-reuse", "COHORT_RPC_REUSE_OK")
        second = _active_snapshot(prefix)
        if second["lock"]["inode"] != first["lock"]["inode"] or second["lock"]["device"] != first["lock"]["device"]:
            raise ContractError("reuse replaced the persistent lifecycle lock inode")
        if second["endpoints"][".init.pid"]["pid"] == first["endpoints"][".init.pid"]["pid"]:
            raise ContractError("reuse did not create a fresh session root PID")
        clean_two = _shutdown(launcher, prefix, evidence_dir, report["commands"], "cycle2-shutdown")
        report["cycles"].append({"cycle": 2, "initial": second, "cleanup": clean_two})
        report["final_census"] = _assert_clean(prefix)
        report["result"] = "PASS"
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        _best_effort_cleanup(launcher, prefix, evidence_dir)
        report["failure_cleanup_processes"] = _process_census(prefix)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        raise
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        "LIFECYCLE_COHORT_DEPLOYED_VALID "
        "cycles=2 rpc=3 shellspawn_restart=1 processes=0 endpoints=0 fd_holders=0 mounts=0 lock_tails=0"
    )


if __name__ == "__main__":
    main()

"""Plan, execute, and recover an independent ``west dev start`` checkout."""

from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO


SCHEMA_VERSION = 1
OPERATION = "start"
_COMMAND_TIMEOUT_SECONDS = 60
_CLONE_TIMEOUT_SECONDS = 1800
_STOP_GRACE_SECONDS = 3
_OUTPUT_LIMIT = 256 * 1024
_LS_TREE_LIMIT = 16 * 1024 * 1024
_EVIDENCE_TAIL = 4096
_EVIDENCE_LIMIT = 8 * 1024 * 1024
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_AT_FDCWD = -100
_MARKER_NAME = ".west-dev-start-owner"
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "operation",
    "transaction_id",
    "state",
    "inputs",
    "steps",
    "results",
    "created_paths",
    "returncode",
    "evidence_identity",
    "evidence_generation",
    "quarantine_identity",
    "destination_identity",
    "next_safe_action",
}
_TERMINAL_STATES = {"committed", "rolled_back", "failed"}
_BEAD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TX_RE = re.compile(r"[0-9a-f]{32}\Z")
_OID_RE = re.compile(r"[0-9a-f]{40}\Z")


class StartError(RuntimeError):
    """A start transaction cannot proceed or recover safely."""


class CommandFailure(StartError):
    """An external command failed without leaving its process group alive."""

    def __init__(self, name: str, argv: list[str], returncode: int, detail: str) -> None:
        self.name = name
        self.argv = argv
        self.returncode = returncode
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{name} failed with rc={returncode}{suffix}")


def _identity(path: Path) -> dict[str, int]:
    metadata = path.lstat()
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _same_identity(path: Path, identity: dict[str, int] | None) -> bool:
    if identity is None:
        return False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_dev == identity.get("device")
        and metadata.st_ino == identity.get("inode")
    )


def _same_regular_identity(path: Path, identity: dict[str, int] | None) -> bool:
    if identity is None:
        return False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_dev == identity.get("device")
        and metadata.st_ino == identity.get("inode")
    )


def _validate_identity(value: object, *, allow_none: bool) -> dict[str, int] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise StartError("destination_identity must contain only device and inode")
    device = value.get("device")
    inode = value.get("inode")
    if not isinstance(device, int) or not isinstance(inode, int) or device < 0 or inode <= 0:
        raise StartError("destination_identity is invalid")
    return {"device": device, "inode": inode}


def _real_directory(path: Path, description: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise StartError(f"{description} does not exist: {path}") from error
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise StartError(f"{description} is not a real directory: {path}")
    return path.resolve(strict=True)


def _unused_path(path: Path, description: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists() or path.is_symlink():
        raise StartError(f"{description} already exists: {path}")
    parent = _real_directory(path.parent, f"{description} parent")
    canonical = parent / path.name
    if canonical.exists() or canonical.is_symlink():
        raise StartError(f"{description} already exists: {canonical}")
    if not path.name or path.name in {".", ".."}:
        raise StartError(f"{description} has an invalid name")
    return canonical


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_git_environment(*, skip_lfs_smudge: bool = False) -> dict[str, str]:
    """Build a Git environment that forwards no caller-controlled ``GIT_*`` value.

    The inherited environment is stripped of every ``GIT_*`` entry, so a caller
    cannot smuggle Git policy (credential helpers, protocol overrides, filters)
    into a start transaction. The opt-in ``GIT_LFS_SKIP_SMUDGE`` is the single
    exception, and it is set here from an explicit validated argument rather
    than copied from the caller's environment.
    """
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
        }
    )
    if skip_lfs_smudge:
        environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    return environment


class _BoundedCapture:
    def __init__(self, output_limit: int) -> None:
        self.output_limit = output_limit
        self.output = bytearray()
        self.tail = bytearray()
        self.error: OSError | None = None

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = self.output_limit + 1 - len(self.output)
                if remaining > 0:
                    self.output.extend(chunk[:remaining])
                self.tail.extend(chunk)
                if len(self.tail) > _EVIDENCE_TAIL:
                    del self.tail[:-_EVIDENCE_TAIL]
        except OSError as error:
            self.error = error
        finally:
            stream.close()

    def bounded_output(self) -> bytes:
        if self.error is not None:
            raise StartError(f"cannot capture command output: {self.error}")
        if len(self.output) > self.output_limit:
            raise StartError(
                f"command output exceeded the {self.output_limit}-byte safety limit"
            )
        return bytes(self.output)

    def decoded_tail(self) -> str:
        return bytes(self.tail).decode("utf-8", errors="replace")


def _stop_process_group(process: subprocess.Popen[bytes], first_signal: int) -> None:
    if process.poll() is not None:
        return
    for sig in (first_signal, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=_STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue
        except BaseException:
            continue


def _record_step(
    steps: list[dict[str, Any]], name: str, argv: list[str], *, mutating: bool
) -> None:
    candidate = {"name": name, "argv": list(argv), "mutating": mutating}
    if candidate not in steps:
        steps.append(candidate)


def _run_command(
    name: str,
    argv: list[str],
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    mutating: bool,
    allowed_returncodes: tuple[int, ...] = (0,),
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
    output_limit: int = _OUTPUT_LIMIT,
    skip_lfs_smudge: bool = False,
) -> bytes:
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise StartError(f"{name} has an invalid argv")
    _record_step(steps, name, argv, mutating=mutating)
    started = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_safe_git_environment(skip_lfs_smudge=skip_lfs_smudge),
            start_new_session=True,
        )
    except OSError as error:
        duration = time.perf_counter_ns() - started
        results.append(
            {
                "name": name,
                "returncode": 127,
                "duration_ns": duration,
                "stdout_tail": "",
                "stderr_tail": str(error)[-_EVIDENCE_TAIL:],
            }
        )
        raise CommandFailure(name, argv, 127, str(error)) from error
    assert process.stdout is not None and process.stderr is not None
    stdout_capture = _BoundedCapture(output_limit)
    stderr_capture = _BoundedCapture(0)
    stdout_thread = threading.Thread(
        target=stdout_capture.drain, args=(process.stdout,), daemon=True
    )
    stderr_thread = threading.Thread(
        target=stderr_capture.drain, args=(process.stderr,), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    interrupted: BaseException | None = None
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_group(process, signal.SIGTERM)
    except BaseException as error:
        interrupted = error
        _stop_process_group(process, signal.SIGINT)
    if process.poll() is None:
        _stop_process_group(process, signal.SIGKILL)
    stdout_thread.join(timeout=_STOP_GRACE_SECONDS)
    stderr_thread.join(timeout=_STOP_GRACE_SECONDS)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _stop_process_group(process, signal.SIGKILL)
        stdout_thread.join(timeout=_STOP_GRACE_SECONDS)
        stderr_thread.join(timeout=_STOP_GRACE_SECONDS)
    duration = time.perf_counter_ns() - started
    returncode = process.poll()
    if returncode is None:
        returncode = 130 if interrupted is not None else 124
    stdout_tail = stdout_capture.decoded_tail()
    stderr_tail = stderr_capture.decoded_tail()
    results.append(
        {
            "name": name,
            "returncode": int(returncode),
            "duration_ns": duration,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }
    )
    if interrupted is not None:
        raise interrupted
    if timed_out:
        raise CommandFailure(name, argv, 124, f"timed out after {timeout} seconds")
    if returncode not in allowed_returncodes:
        detail = stderr_tail.strip() or stdout_tail.strip()
        raise CommandFailure(name, argv, int(returncode), detail)
    return stdout_capture.bounded_output()


def _git_argv(*args: str) -> list[str]:
    return [
        "git",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        *args,
    ]


def _git(
    repo: Path,
    name: str,
    args: list[str],
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    mutating: bool = False,
    allowed_returncodes: tuple[int, ...] = (0,),
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
    output_limit: int = _OUTPUT_LIMIT,
    skip_lfs_smudge: bool = False,
) -> bytes:
    return _run_command(
        name,
        _git_argv("-C", str(repo), *args),
        steps,
        results,
        mutating=mutating,
        allowed_returncodes=allowed_returncodes,
        timeout=timeout,
        output_limit=output_limit,
        skip_lfs_smudge=skip_lfs_smudge,
    )


def _decode_line(output: bytes, description: str) -> str:
    try:
        text = output.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise StartError(f"{description} returned non-UTF-8 output") from error
    if not text or "\n" in text or "\x00" in text:
        raise StartError(f"{description} returned an invalid value")
    return text


def _validate_ref_text(ref: str) -> None:
    if (
        not isinstance(ref, str)
        or not ref
        or len(ref) > 1024
        or ref.startswith("-")
        or any(ord(character) < 32 or ord(character) == 127 for character in ref)
        or any(character in ref for character in " ~^:?*[\\{}")
    ):
        raise StartError(f"invalid base ref: {ref!r}")


def _validate_branch(
    source: Path, branch: str, steps: list[dict[str, Any]], results: list[dict[str, Any]]
) -> None:
    if (
        not isinstance(branch, str)
        or not branch
        or len(branch) > 255
        or branch.startswith("-")
        or "@{" in branch
    ):
        raise StartError(f"invalid branch name: {branch!r}")
    output = _git(
        source,
        "validate_branch",
        ["check-ref-format", "--branch", branch],
        steps,
        results,
    )
    if _decode_line(output, "git check-ref-format") != branch:
        raise StartError(f"branch name is not canonical: {branch!r}")


def _validate_module_text(module: str) -> None:
    if not isinstance(module, str) or not module or len(module) > 4096 or "\\" in module:
        raise StartError(f"invalid module: {module!r}")
    if module == "darling":
        return
    path = Path(module)
    if path.is_absolute() or path.as_posix() != module or any(part in {"", ".", ".."} for part in path.parts):
        raise StartError(f"module must be 'darling' or a normalized nested path: {module!r}")


def _require_repo(
    repo: Path,
    label: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    repo = _real_directory(repo, label)
    top = _decode_line(
        _git(repo, f"verify_repository:{label}", ["rev-parse", "--show-toplevel"], steps, results),
        "git rev-parse --show-toplevel",
    )
    if Path(top).resolve(strict=True) != repo:
        raise StartError(f"{label} is not a repository root: {repo}")
    shallow = _decode_line(
        _git(
            repo,
            f"verify_not_shallow:{label}",
            ["rev-parse", "--is-shallow-repository"],
            steps,
            results,
        ),
        "git rev-parse --is-shallow-repository",
    )
    if shallow != "false":
        raise StartError(f"shallow repository is not allowed: {repo}")
    partial = _git(
        repo,
        f"verify_not_partial:{label}",
        ["config", "--local", "--get", "extensions.partialClone"],
        steps,
        results,
        allowed_returncodes=(0, 1),
    ).decode("utf-8", errors="replace").strip()
    if partial:
        raise StartError(f"partial clone repository is not allowed: {repo}")
    promisor = _git(
        repo,
        f"verify_not_promisor:{label}",
        ["config", "--local", "--get-regexp", "^remote\\..*\\.promisor$"],
        steps,
        results,
        allowed_returncodes=(0, 1),
    ).decode("utf-8", errors="replace").strip()
    if promisor:
        raise StartError(f"promisor repository is not allowed: {repo}")
    alternates_text = _decode_line(
        _git(
            repo,
            f"locate_alternates:{label}",
            ["rev-parse", "--git-path", "objects/info/alternates"],
            steps,
            results,
        ),
        "git rev-parse --git-path objects/info/alternates",
    )
    alternates = Path(alternates_text)
    if not alternates.is_absolute():
        alternates = repo / alternates
    if alternates.exists() or alternates.is_symlink():
        raise StartError(f"object alternates are not allowed: {alternates}")


def _require_commit(
    repo: Path,
    revision: str,
    label: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    if not _OID_RE.fullmatch(revision):
        raise StartError(f"{label} does not name a 40-hex commit: {revision!r}")
    _git(
        repo,
        f"require_commit:{label}",
        ["cat-file", "-e", f"{revision}^{{commit}}"],
        steps,
        results,
    )
    _git(
        repo,
        f"verify_connectivity:{label}",
        ["fsck", "--connectivity-only", "--no-dangling", revision],
        steps,
        results,
        timeout=300,
    )

def _require_clone_transferable(
    repo: Path,
    revision: str,
    label: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    advertised = _run_command(
        f"list_advertised_refs:{label}",
        _git_argv(
            "-c",
            "protocol.file.allow=always",
            "ls-remote",
            repo.as_uri(),
        ),
        steps,
        results,
        mutating=False,
        output_limit=4 * 1024 * 1024,
    )
    advertised_oids: list[str] = []
    for line in advertised.decode("utf-8", errors="strict").splitlines():
        oid, separator, ref = line.partition("\t")
        if not separator or not _OID_RE.fullmatch(oid):
            raise StartError(f"canonical repository advertised malformed ref data: {label}")
        clone_fetches_ref = (
            ref == "HEAD"
            or ref.startswith("refs/heads/")
            or ref.startswith("refs/tags/")
        )
        if clone_fetches_ref and oid not in advertised_oids:
            advertised_oids.append(oid)
    for index, advertised_oid in enumerate(advertised_oids):
        _git(
            repo,
            f"check_advertised_reachability:{label}:{index}",
            [
                "merge-base",
                "--is-ancestor",
                revision,
                f"{advertised_oid}^{{commit}}",
            ],
            steps,
            results,
            allowed_returncodes=(0, 1, 128),
        )
        if results[-1]["returncode"] == 0:
            return
    raise StartError(
        f"{label} commit {revision} is unreachable from every advertised ref"
    )



def _gitlinks(
    repo: Path,
    revision: str,
    label: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    output = _git(
        repo,
        f"inspect_gitlinks:{label}",
        ["ls-tree", "-r", "-z", revision],
        steps,
        results,
        output_limit=_LS_TREE_LIMIT,
    )
    entries: list[tuple[str, str]] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, encoded_name = record.partition(b"\t")
        if not separator:
            raise StartError(f"malformed git tree entry in {label}")
        fields = metadata.split()
        if len(fields) != 3 or fields[0] != b"160000" or fields[1] != b"commit":
            if len(fields) == 3 and fields[0] != b"160000":
                continue
            raise StartError(f"malformed gitlink metadata in {label}")
        try:
            name = encoded_name.decode("utf-8", errors="strict")
            revision_text = fields[2].decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise StartError(f"non-UTF-8 gitlink in {label}") from error
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or relative.as_posix() != name
            or any(part in {"", ".", ".."} for part in relative.parts)
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise StartError(f"unsafe gitlink path in {label}: {name!r}")
        if not _OID_RE.fullmatch(revision_text):
            raise StartError(f"gitlink in {label} has a non-SHA-1 object ID")
        entries.append((name, revision_text))
    entries.sort()
    return entries


def _discover_repositories(
    source: Path,
    resolved_base: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    repositories: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(repo: Path, relative: str, revision: str, depth: int) -> None:
        if depth > 128 or len(repositories) >= 4096:
            raise StartError("recursive gitlink closure exceeds the safety limit")
        if relative in seen:
            raise StartError(f"duplicate recursive gitlink path: {relative}")
        seen.add(relative)
        label = "darling" if relative == "." else relative
        _require_repo(repo, label, steps, results)
        _require_commit(repo, revision, label, steps, results)
        _require_clone_transferable(repo, revision, label, steps, results)
        repositories.append(
            {
                "relative_path": relative,
                "source": str(repo),
                "revision": revision,
                "source_identity": _identity(repo),
            }
        )
        for child_name, child_revision in _gitlinks(repo, revision, label, steps, results):
            child_relative = child_name if relative == "." else f"{relative}/{child_name}"
            child_repo = repo / child_name
            if child_repo.is_symlink():
                raise StartError(f"canonical nested repository is a symlink: {child_repo}")
            visit(child_repo, child_relative, child_revision, depth + 1)

    visit(source, ".", resolved_base, 0)
    return repositories


def _staging_path(destination: Path, transaction_id: str) -> Path:
    return destination.parent / f".{destination.name}.west-dev-start-{transaction_id}"


def _clone_argv(source: Path, target: Path) -> list[str]:
    return _git_argv(
        "-c",
        "protocol.file.allow=always",
        "clone",
        "--quiet",
        "--no-local",
        "--no-hardlinks",
        "--no-checkout",
        source.as_uri(),
        str(target),
    )
def _quarantine_path(destination: Path, transaction_id: str) -> Path:
    return destination.parent / f".{destination.name}.west-dev-quarantine-{transaction_id}"


def _canonical_future_path(path: Path, description: str) -> Path:
    try:
        expanded = Path(path).expanduser()
    except (TypeError, ValueError, OSError) as error:
        raise StartError(f"invalid {description}: {path!r}") from error
    absolute = Path(os.path.abspath(expanded))
    parts = absolute.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        candidate = current / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return candidate.joinpath(*parts[index + 1 :])
        if stat.S_ISLNK(metadata.st_mode):
            raise StartError(f"{description} contains a symlink component: {candidate}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise StartError(f"{description} contains a non-directory component: {candidate}")
        current = candidate
    return current


def _normalize_forbidden_roots(roots: list[Path]) -> list[Path]:
    if not isinstance(roots, list):
        raise StartError("forbidden_roots must be a list of active West repository paths")
    normalized: list[Path] = []
    for index, root in enumerate(roots):
        normalized_root = _canonical_future_path(root, f"forbidden root {index}")
        if normalized_root not in normalized:
            normalized.append(normalized_root)
    return normalized


def _reject_forbidden_outputs(
    forbidden_roots: list[Path],
    *paths: Path,
) -> None:
    for path in paths:
        for root in forbidden_roots:
            if _is_within(path, root):
                raise StartError(
                    f"transaction output is inside active West repository {root}: {path}"
                )




def _planned_mutations(
    repositories: list[dict[str, Any]],
    staging: Path,
    module: str,
    branch: str,
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for repository in repositories:
        relative = repository["relative_path"]
        target = staging if relative == "." else staging / relative
        label = "darling" if relative == "." else relative
        planned.append({"name": f"clone:{label}", "argv": _clone_argv(Path(repository["source"]), target), "mutating": True})
        checkout = ["checkout", "--force", "--detach", repository["revision"]]
        planned.append({"name": f"checkout_detached:{label}", "argv": _git_argv("-C", str(target), *checkout), "mutating": True})
        if (module == "darling" and relative == ".") or (
            module != "darling" and module == relative
        ):
            planned.append(
                {
                    "name": f"activate_branch:{label}",
                    "argv": _git_argv("-C", str(target), "checkout", "--force", "-b", branch, repository["revision"]),
                    "mutating": True,
                }
            )
    return planned


def build_start_plan(
    source: Path,
    destination: Path,
    base: str,
    branch: str,
    bead: str,
    module: str,
    evidence: Path,
    forbidden_roots: list[Path],
    *,
    skip_lfs_smudge: bool = False,
) -> dict:
    """Build a pure, exact-OID start plan without creating transaction paths."""

    if not isinstance(skip_lfs_smudge, bool):
        raise StartError("skip_lfs_smudge must be a boolean")

    source = _real_directory(source.expanduser().resolve(), "canonical source")
    destination = _unused_path(destination, "destination")
    evidence = _unused_path(evidence, "evidence")
    transaction_id = uuid.uuid4().hex
    staging = _staging_path(destination, transaction_id)
    quarantine = _quarantine_path(destination, transaction_id)
    evidence_temporary = evidence.with_name(evidence.name + ".tmp")
    lock = _lock_path(evidence)
    normalized_forbidden_roots = _normalize_forbidden_roots(forbidden_roots)
    _reject_forbidden_outputs(
        normalized_forbidden_roots,
        destination,
        staging,
        quarantine,
        evidence,
        evidence_temporary,
        lock,
    )
    if evidence_temporary.exists() or evidence_temporary.is_symlink():
        raise StartError(f"unexpected evidence temporary already exists: {evidence_temporary}")
    if quarantine.exists() or quarantine.is_symlink():
        raise StartError(f"unexpected transaction quarantine already exists: {quarantine}")
    if _is_within(destination, source) or _is_within(evidence, source):
        raise StartError("destination and evidence must not be inside the canonical source")
    if _is_within(evidence, destination):
        raise StartError("evidence must not be inside the destination")
    if destination == evidence:
        raise StartError("destination and evidence must be different paths")
    _validate_ref_text(base)
    _validate_module_text(module)
    if not isinstance(bead, str) or not _BEAD_RE.fullmatch(bead):
        raise StartError(f"invalid bead identifier: {bead!r}")

    steps: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    _require_repo(source, "darling", steps, results)
    _validate_branch(source, branch, steps, results)
    resolved_output = _git(
        source,
        "resolve_base",
        ["rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        steps,
        results,
    )
    resolved_base = _decode_line(resolved_output, "git rev-parse")
    if not _OID_RE.fullmatch(resolved_base):
        raise StartError("resolved base is not a 40-hex commit")
    results[-1]["resolved_commit"] = resolved_base
    repositories = _discover_repositories(source, resolved_base, steps, results)
    nested_paths = {item["relative_path"] for item in repositories if item["relative_path"] != "."}
    if module != "darling" and module not in nested_paths:
        raise StartError(f"requested module is not in the committed recursive gitlink closure: {module}")
    if staging.exists() or staging.is_symlink():
        raise StartError(f"unexpected transaction staging path already exists: {staging}")
    for step in _planned_mutations(repositories, staging, module, branch):
        _record_step(steps, step["name"], step["argv"], mutating=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "operation": OPERATION,
        "transaction_id": transaction_id,
        "state": "planned",
        "inputs": {
            "source": str(source),
            "destination": str(destination),
            "evidence": str(evidence),
            "requested_base": base,
            "resolved_base": resolved_base,
            "branch": branch,
            "bead": bead,
            "module": module,
            "skip_lfs_smudge": skip_lfs_smudge,
            "destination_parent_identity": _identity(destination.parent),
            "evidence_parent_identity": _identity(evidence.parent),
            "repositories": repositories,
            "forbidden_roots": [str(root) for root in normalized_forbidden_roots],
        },
        "steps": steps,
        "results": results,
        "created_paths": [],
        "evidence_generation": 0,
        "evidence_identity": None,
        "quarantine_identity": None,
        "returncode": None,
        "destination_identity": None,
        "next_safe_action": "execute_start",
    }


def _validate_result(result: object) -> None:
    if not isinstance(result, dict):
        raise StartError("result entry is not an object")
    required = {"name", "returncode", "duration_ns", "stdout_tail", "stderr_tail"}
    if not required.issubset(result) or set(result) - (required | {"resolved_commit"}):
        raise StartError("result entry has invalid fields")
    if not isinstance(result["name"], str) or not isinstance(result["returncode"], int):
        raise StartError("result entry has invalid name or returncode")
    if not isinstance(result["duration_ns"], int) or result["duration_ns"] < 0:
        raise StartError("result entry has invalid duration")
    for field in ("stdout_tail", "stderr_tail"):
        if not isinstance(result[field], str) or len(result[field].encode("utf-8")) > _EVIDENCE_TAIL * 4:
            raise StartError("result output tail is invalid")
    if "resolved_commit" in result and not _OID_RE.fullmatch(result["resolved_commit"]):
        raise StartError("result resolved_commit is invalid")


def _validate_artifact(payload: object, *, evidence: Path | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise StartError("start artifact has an invalid top-level schema")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("operation") != OPERATION:
        raise StartError("start artifact has an unsupported schema or operation")
    transaction_id = payload.get("transaction_id")
    if not isinstance(transaction_id, str) or not _TX_RE.fullmatch(transaction_id):
        raise StartError("start artifact has an invalid transaction ID")
    if payload.get("state") not in {"planned", "active", "committed", "rolled_back", "failed"}:
        raise StartError("start artifact has an invalid state")
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        raise StartError("start artifact inputs are invalid")
    required_inputs = {
        "source",
        "destination",
        "evidence",
        "requested_base",
        "resolved_base",
        "branch",
        "bead",
        "module",
        "skip_lfs_smudge",
        "destination_parent_identity",
        "evidence_parent_identity",
        "repositories",
        "forbidden_roots",
    }
    if set(inputs) != required_inputs:
        raise StartError("start artifact inputs have invalid fields")
    for field in ("source", "destination", "evidence"):
        value = inputs.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise StartError(f"start artifact {field} is not an absolute path")
    if evidence is not None and Path(inputs["evidence"]) != evidence:
        raise StartError("start artifact belongs to another evidence path")
    source_path = Path(inputs["source"])
    destination_path = Path(inputs["destination"])
    evidence_path = Path(inputs["evidence"])
    if (
        _is_within(destination_path, source_path)
        or _is_within(evidence_path, source_path)
        or _is_within(evidence_path, destination_path)
        or destination_path == evidence_path
    ):
        raise StartError(
            "start artifact contains overlapping source, destination, or evidence paths"
        )
    _validate_ref_text(inputs.get("requested_base"))
    if (
        not isinstance(inputs.get("resolved_base"), str)
        or not _OID_RE.fullmatch(inputs["resolved_base"])
    ):
        raise StartError("start artifact resolved_base is invalid")
    branch = inputs.get("branch")
    if (
        not isinstance(branch, str)
        or not branch
        or len(branch) > 255
        or branch.startswith("-")
        or "@{" in branch
        or any(ord(character) < 32 or ord(character) == 127 for character in branch)
    ):
        raise StartError("start artifact branch is invalid")
    if not isinstance(inputs.get("bead"), str) or not _BEAD_RE.fullmatch(inputs["bead"]):
        raise StartError("start artifact bead is invalid")
    _validate_module_text(inputs.get("module"))
    if not isinstance(inputs.get("skip_lfs_smudge"), bool):
        raise StartError("start artifact skip_lfs_smudge is invalid")
    _validate_identity(inputs.get("destination_parent_identity"), allow_none=False)
    _validate_identity(inputs.get("evidence_parent_identity"), allow_none=False)
    forbidden_roots = inputs.get("forbidden_roots")
    if (
        not isinstance(forbidden_roots, list)
        or not all(isinstance(root, str) and Path(root).is_absolute() for root in forbidden_roots)
        or len(set(forbidden_roots)) != len(forbidden_roots)
    ):
        raise StartError("start artifact forbidden_roots are invalid")
    normalized_forbidden = _normalize_forbidden_roots(
        [Path(root) for root in forbidden_roots]
    )
    if [str(root) for root in normalized_forbidden] != forbidden_roots:
        raise StartError("start artifact forbidden_roots are not canonical")
    staging_path = _staging_path(destination_path, transaction_id)
    quarantine_path = _quarantine_path(destination_path, transaction_id)
    _reject_forbidden_outputs(
        [Path(root) for root in forbidden_roots],
        destination_path,
        staging_path,
        quarantine_path,
        evidence_path,
        evidence_path.with_name(evidence_path.name + ".tmp"),
        _lock_path(evidence_path),
    )
    repositories = inputs.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise StartError("start artifact repository closure is invalid")
    expected_relatives: set[str] = set()
    for index, repository in enumerate(repositories):
        if not isinstance(repository, dict) or set(repository) != {"relative_path", "source", "revision", "source_identity"}:
            raise StartError("start artifact repository entry is invalid")
        relative = repository.get("relative_path")
        if index == 0 and relative != ".":
            raise StartError("start artifact outer repository is missing")
        if not isinstance(relative, str) or relative in expected_relatives:
            raise StartError("start artifact repository path is invalid")
        if relative != ".":
            relative_path = Path(relative)
            if (
                relative_path.is_absolute()
                or relative_path.as_posix() != relative
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or any(ord(character) < 32 or ord(character) == 127 for character in relative)
            ):
                raise StartError("start artifact repository path is unsafe")
        expected_relatives.add(relative)
        source_path = repository.get("source")
        if not isinstance(source_path, str) or not Path(source_path).is_absolute():
            raise StartError("start artifact repository source is invalid")
        if not isinstance(repository.get("revision"), str) or not _OID_RE.fullmatch(repository["revision"]):
            raise StartError("start artifact repository revision is invalid")
        _validate_identity(repository.get("source_identity"), allow_none=False)
    labels = {"darling" if value == "." else value for value in expected_relatives}
    if inputs["module"] not in labels:
        raise StartError("start artifact module is outside its repository closure")
    steps = payload.get("steps")
    if not isinstance(steps, list):
        raise StartError("start artifact steps are invalid")
    for step in steps:
        if (
            not isinstance(step, dict)
            or set(step) != {"name", "argv", "mutating"}
            or not isinstance(step["name"], str)
            or not isinstance(step["argv"], list)
            or not all(isinstance(arg, str) and arg for arg in step["argv"])
            or not isinstance(step["mutating"], bool)
            or "worktree" in step["argv"]
        ):
            raise StartError("start artifact contains an invalid step")
    results = payload.get("results")
    if not isinstance(results, list):
        raise StartError("start artifact results are invalid")
    for result in results:
        _validate_result(result)
    created_paths = payload.get("created_paths")
    if not isinstance(created_paths, list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in created_paths):
        raise StartError("start artifact created_paths are invalid")
    destination = Path(inputs["destination"])
    staging = _staging_path(destination, transaction_id)
    quarantine = _quarantine_path(destination, transaction_id)
    allowed_created_paths = {destination, staging, quarantine}
    if (
        any(Path(path) not in allowed_created_paths for path in created_paths)
        or len(set(created_paths)) != len(created_paths)
    ):
        raise StartError("start artifact contains an unsafe created path")
    destination_identity = _validate_identity(
        payload.get("destination_identity"), allow_none=True
    )
    evidence_identity = _validate_identity(
        payload.get("evidence_identity"), allow_none=True
    )
    quarantine_identity = _validate_identity(
        payload.get("quarantine_identity"), allow_none=True
    )
    evidence_generation = payload.get("evidence_generation")
    if not isinstance(evidence_generation, int) or evidence_generation < 0:
        raise StartError("start artifact evidence_generation is invalid")
    state = payload["state"]
    if state in {"planned", "rolled_back"} and (
        created_paths
        or destination_identity is not None
        or quarantine_identity is not None
    ):
        raise StartError(f"{state} evidence must not claim created paths")
    if state == "committed" and (
        created_paths != [str(destination)]
        or destination_identity is None
        or quarantine_identity is not None
    ):
        raise StartError("committed evidence does not identify its destination")
    if state == "active":
        reserved_staging = (
            created_paths == [str(staging)]
            and destination_identity is None
            and quarantine_identity is None
        )
        unclaimed = (
            not created_paths
            and destination_identity is None
            and quarantine_identity is None
        )
        claimed = (
            bool(created_paths)
            and destination_identity is not None
            and (str(quarantine) in created_paths)
            == (quarantine_identity is not None)
        )
        if not (reserved_staging or unclaimed or claimed):
            raise StartError("active evidence has inconsistent ownership guards")
    if evidence_identity is not None and evidence_identity["inode"] <= 0:
        raise StartError("start artifact evidence identity is invalid")
    returncode = payload.get("returncode")
    if state in {"planned", "active"} and returncode is not None:
        raise StartError(f"{state} evidence must not have a returncode")
    if state in _TERMINAL_STATES and not isinstance(returncode, int):
        raise StartError(f"{state} evidence must have an integer returncode")
    if not isinstance(payload.get("next_safe_action"), str) or not payload["next_safe_action"]:
        raise StartError("start artifact next_safe_action is invalid")
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
def _rename_noreplace(source: Path, destination: Path) -> None:
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise StartError(
            "atomic no-replace rename is unavailable; refusing unsafe fallback"
        ) from error
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise StartError(
            "atomic no-replace rename is unsupported; refusing unsafe fallback"
        )
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise StartError(f"no-replace rename destination already exists: {destination}")
    raise StartError(
        f"atomic no-replace rename failed from {source} to {destination}: "
        f"{os.strerror(error_number)}"
    )
def _rename_noreplace_at(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
) -> None:
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise StartError(
            "atomic fd-relative no-replace rename is unavailable"
        ) from error
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        source_directory_fd,
        os.fsencode(source_name),
        destination_directory_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    raise StartError(
        f"atomic fd-relative no-replace rename failed: {os.strerror(error_number)}"
    )


def _identity_at(directory_fd: int, name: str, *, regular: bool) -> dict[str, int] | None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    expected_kind = stat.S_ISREG if regular else stat.S_ISDIR
    if not expected_kind(metadata.st_mode):
        return None
    return {"device": metadata.st_dev, "inode": metadata.st_ino}






def _rename_exchange(first: Path, second: Path) -> None:
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise StartError(
            "atomic rename exchange is unavailable; refusing unsafe fallback"
        ) from error
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        _AT_FDCWD,
        os.fsencode(first),
        _AT_FDCWD,
        os.fsencode(second),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise StartError(
            "atomic rename exchange is unsupported; refusing unsafe fallback"
        )
    raise StartError(
        f"atomic rename exchange failed for {first} and {second}: "
        f"{os.strerror(error_number)}"
    )


def _safe_read_json_with_identity(
    path: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise StartError(f"cannot open start evidence safely: {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _EVIDENCE_LIMIT
        ):
            raise StartError(f"start evidence is not an owner-only bounded regular file: {path}")
        observed_identity = {"device": metadata.st_dev, "inode": metadata.st_ino}
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            payload = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StartError(f"cannot read start evidence: {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload, observed_identity


def _safe_read_json(path: Path) -> dict[str, Any]:
    payload, _identity_value = _safe_read_json_with_identity(path)
    return payload
def _safe_read_json_at(
    directory_fd: int, name: str
) -> tuple[dict[str, Any], dict[str, int]]:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _EVIDENCE_LIMIT
        ):
            raise StartError("fd-relative JSON is not an owner-only bounded file")
        identity = {"device": metadata.st_dev, "inode": metadata.st_ino}
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            payload = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StartError(f"cannot read fd-relative JSON safely: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload, identity




def _unlink_if_identity(path: Path, identity: dict[str, int]) -> bool:
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    trash_name = f".west-dev-unlink-{uuid.uuid4().hex}"
    trash_fd = -1
    trash_identity: dict[str, int] | None = None
    try:
        if _identity_at(parent_fd, path.name, regular=True) != identity:
            return False
        os.mkdir(trash_name, 0o700, dir_fd=parent_fd)
        trash_fd = os.open(
            trash_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        trash_metadata = os.fstat(trash_fd)
        trash_identity = {
            "device": trash_metadata.st_dev,
            "inode": trash_metadata.st_ino,
        }
        _rename_noreplace_at(parent_fd, path.name, trash_fd, "victim")
        if _identity_at(trash_fd, "victim", regular=True) != identity:
            _rename_noreplace_at(trash_fd, "victim", parent_fd, path.name)
            return False
        os.unlink("victim", dir_fd=trash_fd)
        os.fsync(trash_fd)
        if _identity_at(parent_fd, trash_name, regular=False) != trash_identity:
            raise StartError("private unlink directory identity changed")
        os.close(trash_fd)
        trash_fd = -1
        os.rmdir(trash_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        trash_identity = None
        return True
    finally:
        if trash_fd >= 0:
            os.close(trash_fd)
        try:
            if (
                trash_identity is not None
                and _identity_at(parent_fd, trash_name, regular=False)
                == trash_identity
            ):
                os.rmdir(trash_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
        except OSError:
            pass
        os.close(parent_fd)


def _durable_write(evidence: Path, payload: dict[str, Any]) -> None:
    _validate_artifact(payload, evidence=evidence)
    existing: dict[str, Any] | None = None
    existing_identity: dict[str, int] | None = None
    if evidence.exists() or evidence.is_symlink():
        existing_raw, existing_identity = _safe_read_json_with_identity(evidence)
        existing = _validate_artifact(existing_raw, evidence=evidence)
        recorded_existing_identity = _validate_identity(
            existing["evidence_identity"], allow_none=False
        )
        if recorded_existing_identity != existing_identity:
            raise StartError("existing evidence inode does not match its recorded identity")
        if (
            payload["evidence_identity"] != existing_identity
            or existing["transaction_id"] != payload["transaction_id"]
            or payload["evidence_generation"] != existing["evidence_generation"]
            or existing["inputs"] != payload["inputs"]
            or (
                existing["state"] != payload["state"]
                and not _valid_transition(existing["state"], payload["state"])
            )
        ):
            raise StartError("refusing to update substituted or unrelated evidence")
    elif (
        payload["evidence_identity"] is not None
        or payload["evidence_generation"] != 0
    ):
        raise StartError("evidence disappeared after its identity was recorded")

    temporary = evidence.with_name(evidence.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise StartError(f"unexpected start evidence temporary exists: {temporary}")
    descriptor = -1
    new_identity: dict[str, int] | None = None
    previous_payload_identity = payload["evidence_identity"]
    previous_generation = payload["evidence_generation"]
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        new_identity = _identity(temporary)
        payload["evidence_identity"] = new_identity
        payload["evidence_generation"] = previous_generation + 1
        _validate_artifact(payload, evidence=evidence)
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > _EVIDENCE_LIMIT:
            raise StartError("start evidence exceeds the size limit")
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        if existing is None:
            _rename_noreplace(temporary, evidence)
            if not _same_regular_identity(evidence, new_identity):
                raise StartError("first evidence publication changed inode")
            _fsync_directory(evidence.parent)
            new_identity = None
            return
        assert existing_identity is not None
        if not _same_regular_identity(evidence, existing_identity):
            raise StartError("evidence path was substituted before atomic update")
        _rename_exchange(evidence, temporary)
        if not (
            _same_regular_identity(evidence, payload["evidence_identity"])
            and _same_regular_identity(temporary, existing_identity)
        ):
            try:
                _rename_exchange(evidence, temporary)
            except BaseException as restore_error:
                raise StartError(
                    f"evidence exchange verification failed and restoration failed: {restore_error}"
                ) from restore_error
            if not (
                _same_regular_identity(evidence, existing_identity)
                and _same_regular_identity(temporary, payload["evidence_identity"])
            ):
                raise StartError("evidence exchange restoration could not verify both inodes")
            raise StartError("evidence path substitution detected during atomic exchange")
        if not _same_regular_identity(temporary, existing_identity):
            try:
                _rename_exchange(evidence, temporary)
            except BaseException as restore_error:
                raise StartError(
                    f"displaced evidence changed and exchange restoration failed: {restore_error}"
                ) from restore_error
            if not (
                _same_regular_identity(evidence, existing_identity)
                and _same_regular_identity(temporary, payload["evidence_identity"])
            ):
                raise StartError("changed displaced evidence could not be restored safely")
            raise StartError("displaced evidence inode changed before cleanup")
        temporary.unlink()
        _fsync_directory(temporary.parent)
        new_identity = None
    except BaseException:
        published = (
            new_identity is not None
            and _same_regular_identity(evidence, new_identity)
        )
        if not published:
            payload["evidence_identity"] = previous_payload_identity
            payload["evidence_generation"] = previous_generation
            if new_identity is not None:
                try:
                    _unlink_if_identity(temporary, new_identity)
                except FileNotFoundError:
                    pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _lock_path(evidence: Path) -> Path:
    return evidence.with_name(f".{evidence.name}.lock")


def _open_lock(evidence: Path):
    path = _lock_path(evidence)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise StartError(f"start lock must be an owner-only regular file: {path}")
    lock = os.fdopen(descriptor, "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise StartError(f"another start transaction holds the lock: {path}") from error
    return lock


def _write_marker(staging: Path, payload: dict[str, Any]) -> None:
    marker = staging / _MARKER_NAME
    data = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "operation": OPERATION,
            "transaction_id": payload["transaction_id"],
            "destination": payload["inputs"]["destination"],
            "evidence": payload["inputs"]["evidence"],
        },
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(staging)


def _marker_proves_ownership(staging: Path, payload: dict[str, Any]) -> bool:
    try:
        if staging.is_symlink() or not staging.is_dir():
            return False
        entries = list(staging.iterdir())
        if entries != [staging / _MARKER_NAME]:
            return False
        marker_payload = _safe_read_json(staging / _MARKER_NAME)
    except (OSError, StartError):
        return False
    return marker_payload == {
        "schema_version": SCHEMA_VERSION,
        "operation": OPERATION,
        "transaction_id": payload["transaction_id"],
        "destination": payload["inputs"]["destination"],
        "evidence": payload["inputs"]["evidence"],
    }
def _reserved_empty_staging(staging: Path, payload: dict[str, Any]) -> bool:
    if payload["created_paths"] != [str(staging)]:
        return False
    try:
        metadata = staging.lstat()
        empty = not any(staging.iterdir())
    except (FileNotFoundError, OSError):
        return False
    return (
        not staging.is_symlink()
        and stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and empty
    )




def _validate_private_quarantine(
    quarantine: Path, identity: dict[str, int]
) -> None:
    try:
        metadata = quarantine.lstat()
    except FileNotFoundError as error:
        raise StartError("recorded rollback quarantine is missing") from error
    if (
        quarantine.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_dev != identity["device"]
        or metadata.st_ino != identity["inode"]
    ):
        raise StartError("rollback quarantine is not the recorded owner-only directory")


def _rollback(payload: dict[str, Any], evidence: Path) -> None:
    destination = Path(payload["inputs"]["destination"])
    staging = _staging_path(destination, payload["transaction_id"])
    quarantine = _quarantine_path(destination, payload["transaction_id"])
    quarantined_tree = quarantine / "owned"
    parent_identity = _validate_identity(
        payload["inputs"]["destination_parent_identity"], allow_none=False
    )
    if not _same_identity(destination.parent, parent_identity):
        raise StartError("destination parent identity changed; refusing rollback")

    quarantine_identity = _validate_identity(
        payload["quarantine_identity"], allow_none=True
    )
    if quarantine.exists() or quarantine.is_symlink():
        if quarantine_identity is None:
            if not _marker_proves_ownership(quarantine, payload):
                raise StartError("unrecorded rollback quarantine has no ownership marker")
            quarantine_identity = _identity(quarantine)
            payload["quarantine_identity"] = quarantine_identity
            if str(quarantine) not in payload["created_paths"]:
                payload["created_paths"].append(str(quarantine))
            _durable_write(evidence, payload)
        _validate_private_quarantine(quarantine, quarantine_identity)
    elif quarantine_identity is not None:
        payload["quarantine_identity"] = None
        payload["created_paths"] = [
            path for path in payload["created_paths"] if path != str(quarantine)
        ]
        quarantine_identity = None

    identity = _validate_identity(payload["destination_identity"], allow_none=True)
    public_candidates = (staging, destination)
    public_present = [
        path for path in public_candidates if path.exists() or path.is_symlink()
    ]
    inside_present = quarantined_tree.exists() or quarantined_tree.is_symlink()
    if identity is None:
        if public_present == [staging] and (
            _marker_proves_ownership(staging, payload)
            or _reserved_empty_staging(staging, payload)
        ):
            identity = _identity(staging)
            payload["destination_identity"] = identity
            payload["created_paths"] = [str(staging)] + (
                [str(quarantine)] if quarantine_identity is not None else []
            )
            _durable_write(evidence, payload)
        elif not public_present and not inside_present:
            if quarantine_identity is None:
                return
        else:
            raise StartError(
                "active transaction has no identity or marker proving tree ownership"
            )

    assert identity is not None
    owned_public = [
        path for path in public_present if _same_identity(path, identity)
    ]
    foreign_public = [path for path in public_present if path not in owned_public]
    owned_inside = inside_present and _same_identity(quarantined_tree, identity)
    if inside_present and not owned_inside:
        raise StartError("private quarantine contains a non-owned tree")
    if len(owned_public) + int(owned_inside) > 1:
        raise StartError("transaction tree identity appears at multiple paths")
    if not owned_public and not owned_inside and quarantine_identity is None:
        payload["created_paths"] = []
        payload["destination_identity"] = None
        _durable_write(evidence, payload)
        if foreign_public:
            raise StartError(
                f"unexpected non-owned path remains after rollback: {foreign_public[0]}"
            )
        return

    if quarantine_identity is None:
        try:
            os.mkdir(quarantine, 0o700)
        except FileExistsError as error:
            raise StartError("rollback quarantine appeared during creation") from error
        os.chmod(quarantine, 0o700, follow_symlinks=False)
        quarantine_identity = _identity(quarantine)
        _write_marker(quarantine, payload)
        _fsync_directory(quarantine.parent)
        payload["quarantine_identity"] = quarantine_identity
        if str(quarantine) not in payload["created_paths"]:
            payload["created_paths"].append(str(quarantine))
        _durable_write(evidence, payload)
    _validate_private_quarantine(quarantine, quarantine_identity)

    if owned_public:
        source = owned_public[0]
        _rename_noreplace(source, quarantined_tree)
        _fsync_directory(quarantine)
        _fsync_directory(quarantine.parent)
        if not (
            _same_identity(quarantine, quarantine_identity)
            and _same_identity(quarantined_tree, identity)
        ):
            raise StartError("quarantined transaction tree failed identity verification")
        payload["created_paths"] = [str(quarantine)]
        _durable_write(evidence, payload)
        owned_inside = True
    quarantine_fd = os.open(
        quarantine,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        metadata = os.fstat(quarantine_fd)
        if {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
        } != quarantine_identity:
            raise StartError("opened private quarantine identity changed")
        names = set(os.listdir(quarantine_fd))
        expected_names = {_MARKER_NAME} | ({"owned"} if owned_inside else set())
        if names != expected_names:
            raise StartError("private quarantine contains unexpected paths")
        if owned_inside:
            if _identity_at(quarantine_fd, "owned", regular=False) != identity:
                raise StartError("quarantined transaction tree identity changed")
            shutil.rmtree("owned", dir_fd=quarantine_fd)
            os.fsync(quarantine_fd)
        marker_payload, marker_identity = _safe_read_json_at(
            quarantine_fd, _MARKER_NAME
        )
        if marker_payload != {
            "schema_version": SCHEMA_VERSION,
            "operation": OPERATION,
            "transaction_id": payload["transaction_id"],
            "destination": payload["inputs"]["destination"],
            "evidence": payload["inputs"]["evidence"],
        }:
            raise StartError("private quarantine ownership marker changed")
        if _identity_at(quarantine_fd, _MARKER_NAME, regular=True) != marker_identity:
            raise StartError("private quarantine marker identity changed")
        os.unlink(_MARKER_NAME, dir_fd=quarantine_fd)
        os.fsync(quarantine_fd)
        if os.listdir(quarantine_fd):
            raise StartError("private quarantine is not empty after cleanup")
        parent_fd = os.open(
            quarantine.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            if _identity_at(parent_fd, quarantine.name, regular=False) != quarantine_identity:
                raise StartError("private quarantine pathname was substituted")
            os.rmdir(quarantine.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(quarantine_fd)
    payload["created_paths"] = []
    payload["destination_identity"] = None
    payload["quarantine_identity"] = None
    _durable_write(evidence, payload)
    if foreign_public:
        raise StartError(
            f"unexpected non-owned path remains after rollback: {foreign_public[0]}"
        )


def _assert_parent_identities(payload: dict[str, Any]) -> None:
    inputs = payload["inputs"]
    for field, identity_field in (
        ("destination", "destination_parent_identity"),
        ("evidence", "evidence_parent_identity"),
    ):
        parent = Path(inputs[field]).parent
        identity = _validate_identity(inputs[identity_field], allow_none=False)
        if not _same_identity(parent, identity):
            raise StartError(f"{field} parent identity changed since planning")


def _assert_sources_match_plan(
    payload: dict[str, Any], steps: list[dict[str, Any]], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    inputs = payload["inputs"]
    source = Path(inputs["source"])
    discovered = _discover_repositories(source, inputs["resolved_base"], steps, results)
    planned = inputs["repositories"]
    for repository in planned:
        if not _same_identity(Path(repository["source"]), repository["source_identity"]):
            raise StartError(f"canonical repository identity changed: {repository['source']}")
    if discovered != planned:
        raise StartError("canonical recursive gitlink closure changed since planning")
    return planned


def _delete_local_heads(
    target: Path,
    label: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    skip_lfs_smudge: bool = False,
) -> None:
    output = _git(
        target,
        f"list_local_branches:{label}",
        ["for-each-ref", "--format=%(refname)", "refs/heads"],
        steps,
        results,
        mutating=False,
        skip_lfs_smudge=skip_lfs_smudge,
    )
    for ref in output.decode("utf-8", errors="strict").splitlines():
        if not ref.startswith("refs/heads/") or "\x00" in ref:
            raise StartError(f"clone returned an unsafe local branch ref: {ref!r}")
        _git(
            target,
            f"delete_local_branch:{label}:{ref[11:]}",
            ["update-ref", "-d", ref],
            steps,
            results,
            mutating=True,
            skip_lfs_smudge=skip_lfs_smudge,
        )


def _materialize_repository(
    repository: dict[str, Any],
    target: Path,
    selected: bool,
    branch: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    skip_lfs_smudge: bool = False,
) -> None:
    relative = repository["relative_path"]
    label = "darling" if relative == "." else relative
    if target.is_symlink():
        raise StartError(f"refusing symlink clone destination: {target}")
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise StartError(f"clone destination unexpectedly exists and is not empty: {target}")
    else:
        parent = target.parent
        if parent.is_symlink() or not parent.is_dir():
            raise StartError(f"clone parent is not a real directory: {parent}")
    _run_command(
        f"clone:{label}",
        _clone_argv(Path(repository["source"]), target),
        steps,
        results,
        mutating=True,
        timeout=_CLONE_TIMEOUT_SECONDS,
        skip_lfs_smudge=skip_lfs_smudge,
    )
    _require_repo(target, f"clone-{label}", steps, results)
    _require_commit(target, repository["revision"], f"clone-{label}", steps, results)
    _git(
        target,
        f"checkout_detached:{label}",
        ["checkout", "--force", "--detach", repository["revision"]],
        steps,
        results,
        mutating=True,
        skip_lfs_smudge=skip_lfs_smudge,
    )
    _delete_local_heads(target, label, steps, results, skip_lfs_smudge=skip_lfs_smudge)
    if selected:
        _git(
            target,
            f"activate_branch:{label}",
            ["checkout", "--force", "-b", branch, repository["revision"]],
            steps,
            results,
            mutating=True,
            skip_lfs_smudge=skip_lfs_smudge,
        )
    head = _decode_line(
        _git(
            target,
            f"verify_head:{label}",
            ["rev-parse", "HEAD"],
            steps,
            results,
            skip_lfs_smudge=skip_lfs_smudge,
        ),
        "git rev-parse HEAD",
    )
    if head != repository["revision"]:
        raise StartError(f"materialized repository has the wrong HEAD: {label}")
    dirty = _git(
        target,
        f"verify_clean:{label}",
        ["status", "--porcelain"],
        steps,
        results,
        skip_lfs_smudge=skip_lfs_smudge,
    )
    if dirty:
        raise StartError(f"materialized repository is dirty: {label}")
def _verify_materialized_closure(
    root: Path,
    resolved_base: str,
    planned: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    observed = _discover_repositories(root, resolved_base, steps, results)
    expected = [
        (
            repository["relative_path"],
            repository["revision"],
            root
            if repository["relative_path"] == "."
            else root / repository["relative_path"],
        )
        for repository in planned
    ]
    actual = [
        (
            repository["relative_path"],
            repository["revision"],
            Path(repository["source"]),
        )
        for repository in observed
    ]
    if actual != expected:
        raise StartError("materialized recursive gitlink closure differs from the plan")




def _failure_returncode(error: BaseException) -> int:
    if isinstance(error, KeyboardInterrupt):
        return 130
    if isinstance(error, CommandFailure):
        return error.returncode
    if isinstance(error, SystemExit) and isinstance(error.code, int):
        return error.code
    return 1


def _append_terminal_result(payload: dict[str, Any], name: str, returncode: int, detail: str) -> None:
    payload["results"].append(
        {
            "name": name,
            "returncode": returncode,
            "duration_ns": 0,
            "stdout_tail": "",
            "stderr_tail": detail[-_EVIDENCE_TAIL:],
        }
    )


def execute_start(plan: dict) -> dict:
    """Execute a validated plan and durably journal every ownership transition."""

    payload = copy.deepcopy(plan)
    _validate_artifact(payload)
    if payload["state"] != "planned" or payload["created_paths"] or payload["destination_identity"] is not None:
        raise StartError("execute_start requires an unexecuted planned artifact")
    evidence = Path(payload["inputs"]["evidence"])
    destination = Path(payload["inputs"]["destination"])
    staging = _staging_path(destination, payload["transaction_id"])
    _assert_parent_identities(payload)
    with _open_lock(evidence):
        if evidence.exists() or evidence.is_symlink():
            raise StartError(f"start evidence already exists; recover it instead: {evidence}")
        temporary = evidence.with_name(evidence.name + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise StartError(f"start evidence temporary already exists; recover it instead: {temporary}")
        _assert_parent_identities(payload)
        quarantine = _quarantine_path(destination, payload["transaction_id"])
        if (
            destination.exists()
            or destination.is_symlink()
            or staging.exists()
            or staging.is_symlink()
            or quarantine.exists()
            or quarantine.is_symlink()
        ):
            raise StartError(
                "destination, transaction staging, or quarantine path unexpectedly exists"
            )
        _durable_write(evidence, payload)
        payload["state"] = "active"
        payload["next_safe_action"] = "recover_start"
        _durable_write(evidence, payload)
        try:
            repositories = _assert_sources_match_plan(payload, payload["steps"], payload["results"])
            payload["created_paths"] = [str(staging)]
            _durable_write(evidence, payload)
            try:
                os.mkdir(staging, 0o700)
            except FileExistsError as error:
                payload["created_paths"] = []
                _durable_write(evidence, payload)
                raise StartError("reserved staging path appeared before creation") from error
            claimed_identity = _identity(staging)
            payload["created_paths"] = [str(staging)]
            payload["destination_identity"] = claimed_identity
            _fsync_directory(staging.parent)
            _write_marker(staging, payload)
            _durable_write(evidence, payload)
            marker = staging / _MARKER_NAME
            marker.unlink()
            _fsync_directory(staging)

            module = payload["inputs"]["module"]
            skip_lfs_smudge = payload["inputs"]["skip_lfs_smudge"]
            for repository in repositories:
                relative = repository["relative_path"]
                label = "darling" if relative == "." else relative
                target = staging if relative == "." else staging / relative
                _materialize_repository(
                    repository,
                    target,
                    (module == "darling" and relative == ".")
                    or (module != "darling" and module == relative),
                    payload["inputs"]["branch"],
                    payload["steps"],
                    payload["results"],
                    skip_lfs_smudge=skip_lfs_smudge,
                )
                _durable_write(evidence, payload)
            _verify_materialized_closure(
                staging,
                payload["inputs"]["resolved_base"],
                repositories,
                payload["steps"],
                payload["results"],
            )
            _durable_write(evidence, payload)

            if destination.exists() or destination.is_symlink():
                raise StartError(f"destination appeared during start: {destination}")
            if not _same_identity(staging, claimed_identity):
                raise StartError("transaction staging identity changed before publication")
            _rename_noreplace(staging, destination)
            _fsync_directory(destination.parent)
            if not _same_identity(destination, claimed_identity):
                raise StartError("published destination identity changed")
            payload["created_paths"] = [str(destination)]
            _durable_write(evidence, payload)
            payload["state"] = "committed"
            payload["returncode"] = 0
            payload["next_safe_action"] = "use_destination"
            _append_terminal_result(payload, "start_transaction", 0, "")
            _durable_write(evidence, payload)
            return payload
        except BaseException as error:
            returncode = _failure_returncode(error)
            handled = isinstance(error, (CommandFailure, StartError, KeyboardInterrupt))
            detail = f"{type(error).__name__}: {error}"
            try:
                _rollback(payload, evidence)
            except BaseException as rollback_error:
                payload["state"] = "failed"
                payload["returncode"] = returncode
                payload["next_safe_action"] = "inspect_ownership_before_manual_recovery"
                _append_terminal_result(
                    payload,
                    "start_transaction",
                    returncode,
                    f"{detail}; rollback failed: {type(rollback_error).__name__}: {rollback_error}",
                )
            else:
                payload["state"] = "rolled_back"
                payload["returncode"] = returncode
                payload["created_paths"] = []
                payload["destination_identity"] = None
                payload["quarantine_identity"] = None
                payload["next_safe_action"] = "build_a_new_start_plan"
                _append_terminal_result(payload, "start_transaction", returncode, detail)
            _durable_write(evidence, payload)
            if handled:
                return payload
            raise


def _valid_transition(old: str, new: str) -> bool:
    if old == new == "active":
        return True
    return (old, new) in {
        ("planned", "active"),
        ("planned", "rolled_back"),
        ("planned", "failed"),
        ("active", "committed"),
        ("active", "rolled_back"),
        ("active", "failed"),
        ("committed", "rolled_back"),
        ("committed", "failed"),
        ("failed", "rolled_back"),
    }


def _load_recoverable_evidence(evidence: Path) -> dict[str, Any]:
    temporary = evidence.with_name(evidence.name + ".tmp")
    main_payload: dict[str, Any] | None = None
    main_identity: dict[str, int] | None = None
    temporary_payload: dict[str, Any] | None = None
    temporary_identity: dict[str, int] | None = None
    if evidence.exists() or evidence.is_symlink():
        main_raw, main_identity = _safe_read_json_with_identity(evidence)
        main_payload = _validate_artifact(main_raw, evidence=evidence)
        if main_payload["evidence_identity"] != main_identity:
            raise StartError("main evidence inode differs from its recorded identity")
    if temporary.exists() or temporary.is_symlink():
        temporary_raw, temporary_identity = _safe_read_json_with_identity(temporary)
        temporary_payload = _validate_artifact(temporary_raw, evidence=evidence)
        if temporary_payload["evidence_identity"] != temporary_identity:
            raise StartError("temporary evidence inode differs from its recorded identity")
    if main_payload is None and temporary_payload is None:
        raise StartError(f"start evidence does not exist: {evidence}")
    if temporary_payload is None:
        assert main_payload is not None
        return main_payload
    if main_payload is None:
        assert temporary_identity is not None
        _rename_noreplace(temporary, evidence)
        if not _same_regular_identity(evidence, temporary_identity):
            raise StartError("recovered first evidence publication changed inode")
        _fsync_directory(evidence.parent)
        return temporary_payload
    assert main_identity is not None and temporary_identity is not None
    if (
        temporary_payload["transaction_id"] != main_payload["transaction_id"]
        or temporary_payload["inputs"] != main_payload["inputs"]
    ):
        raise StartError("evidence temporary belongs to another transaction")
    main_generation = main_payload["evidence_generation"]
    temporary_generation = temporary_payload["evidence_generation"]
    if (
        temporary_generation == main_generation + 1
        and _valid_transition(main_payload["state"], temporary_payload["state"])
    ):
        _rename_exchange(evidence, temporary)
        if not (
            _same_regular_identity(evidence, temporary_identity)
            and _same_regular_identity(temporary, main_identity)
        ):
            _rename_exchange(evidence, temporary)
            raise StartError("could not verify or retain a recovered evidence exchange")
        if not _unlink_if_identity(temporary, main_identity):
            raise StartError("displaced main evidence changed during recovery")
        return temporary_payload
    if (
        main_generation == temporary_generation + 1
        and _valid_transition(temporary_payload["state"], main_payload["state"])
    ):
        if not _unlink_if_identity(temporary, temporary_identity):
            raise StartError("displaced temporary evidence changed during recovery")
        return main_payload
    raise StartError("main and temporary evidence generations are inconsistent")


def recover_start(evidence: Path) -> dict:
    """Recover only a checkout whose journal identity or marker proves ownership."""

    evidence = evidence.expanduser()
    if not evidence.is_absolute():
        evidence = Path.cwd() / evidence
    parent = _real_directory(evidence.parent, "evidence parent")
    evidence = parent / evidence.name
    with _open_lock(evidence):
        payload = _load_recoverable_evidence(evidence)
        _assert_parent_identities(payload)
        state = payload["state"]
        destination = Path(payload["inputs"]["destination"])
        identity = _validate_identity(payload["destination_identity"], allow_none=True)
        if state == "committed":
            if identity is None or not _same_identity(destination, identity):
                raise StartError("committed destination identity no longer matches its evidence")
            return payload
        if state == "rolled_back":
            return payload
        has_owned_paths = bool(
            payload["created_paths"]
            or payload["destination_identity"] is not None
            or payload["quarantine_identity"] is not None
        )
        if state == "failed" and not has_owned_paths:
            return payload
        recovered_returncode = payload["returncode"] if state == "failed" else 0
        try:
            _rollback(payload, evidence)
        except BaseException as error:
            returncode = _failure_returncode(error)
            handled = isinstance(error, (CommandFailure, StartError, KeyboardInterrupt))
            still_owned = bool(
                payload["created_paths"]
                or payload["destination_identity"] is not None
                or payload["quarantine_identity"] is not None
            )
            cleanup_completed_on_interrupt = (
                isinstance(error, KeyboardInterrupt) and not still_owned
            )
            payload["state"] = (
                "rolled_back" if cleanup_completed_on_interrupt else "failed"
            )
            payload["returncode"] = returncode
            payload["next_safe_action"] = (
                "recover_start"
                if still_owned
                else (
                    "build_a_new_start_plan"
                    if cleanup_completed_on_interrupt
                    else "inspect_ownership_before_manual_recovery"
                )
            )
            _append_terminal_result(
                payload,
                "recover_start",
                returncode,
                f"{type(error).__name__}: {error}",
            )
            _durable_write(evidence, payload)
            if handled:
                return payload
            raise
        payload["state"] = "rolled_back"
        payload["returncode"] = recovered_returncode
        payload["created_paths"] = []
        payload["destination_identity"] = None
        payload["quarantine_identity"] = None
        payload["next_safe_action"] = "build_a_new_start_plan"
        _append_terminal_result(payload, "recover_start", recovered_returncode, "")
        _durable_write(evidence, payload)
        return payload

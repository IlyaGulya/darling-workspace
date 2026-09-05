#!/usr/bin/env python3
"""Preflight, stage, verify, and publish one Darling handoff transaction."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from generate_manifest import public_base, run_bounded, write_manifest
from handoff import bundle_filename, bundle_heads, private_branches, write_package

EXPECTED_PROJECTS_ENV = "DW_HANDOFF_EXPECTED_PROJECTS"
PUBLICATION_PATHS = (
    ".beads/issues.jsonl",
    "state/repos.tsv",
    "locked.xml",
    "base.xml",
    "handoff",
)


class HandoffError(RuntimeError):
    """The handoff cannot safely proceed."""


@dataclass(frozen=True)
class Branch:
    branch: str
    head: str
    upstream: str
    source_ref: str

    def as_dict(self) -> dict[str, str]:
        return {
            "branch": self.branch,
            "head": self.head,
            "upstream": self.upstream,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class Project:
    relative: str
    repo: Path
    head: str
    branch: str
    origin: str
    dirty: tuple[str, ...]
    base_revision: str
    branches: tuple[Branch, ...]
    exclusions: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    control: Path
    source: Path
    projects: tuple[Project, ...]
    allow_dirty: bool

    @property
    def bundle_count(self) -> int:
        return sum(bool(project.branches) for project in self.projects)


@dataclass(frozen=True)
class Unit:
    relative: str
    staged: Path
    destination: Path
    before: str | None
    after: str
    backup: Path
    displaced: Path


def _run_git(repo: Path, *args: str, required: bool = True) -> str:
    try:
        result = run_bounded(["git", "-C", str(repo), *args])
    except subprocess.TimeoutExpired as error:
        raise HandoffError(f"git timed out in {repo}: {' '.join(args)}") from error
    if result.overflow:
        raise HandoffError(f"git output limit exceeded in {repo}: {' '.join(args)}")
    if required and result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip() or "unknown error")[:4096]
        raise HandoffError(f"git failed in {repo}: {' '.join(args)}: {detail}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _submodule_state(source: Path, *, recursive: bool = True) -> list[tuple[str, str]]:
    args = ("--recursive",) if recursive else ()
    output = _run_git(source, "submodule", "status", *args)
    result: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        marker = line[0]
        fields = line[1:].split()
        if len(fields) < 2:
            raise HandoffError(f"cannot parse submodule status: {line}")
        result.append((fields[1], marker))
    return result


def _normalized_closure(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise HandoffError(f"{EXPECTED_PROJECTS_ENV} must be a JSON string array")
    normalized: list[str] = []
    for item in raw:
        path = Path(item)
        if item == ".":
            value = "."
        elif not item or path.is_absolute() or ".." in path.parts or str(path) != item:
            raise HandoffError(f"invalid project path in expected closure: {item!r}")
        else:
            value = item.rstrip("/")
        if value in normalized:
            raise HandoffError(f"duplicate project path in expected closure: {value}")
        normalized.append(value)
    if not normalized or normalized[0] != ".":
        raise HandoffError("expected project closure must begin with the Darling root (.)")
    return tuple(normalized)


def expected_closure(source: Path, states: list[tuple[str, str]]) -> tuple[str, ...]:
    encoded = os.environ.get(EXPECTED_PROJECTS_ENV)
    if encoded is None:
        incomplete = [path for path, marker in states if marker in {"-", "U"}]
        if incomplete:
            raise HandoffError(
                "direct handoff requires every recursive submodule to be initialized and "
                f"unconflicted; incomplete: {', '.join(incomplete)}"
            )
        return (".", *(path for path, _marker in states))
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise HandoffError(f"invalid {EXPECTED_PROJECTS_ENV}: {error}") from error
    return _normalized_closure(raw)


def _require_real_parent_chain(control: Path, destination: Path) -> None:
    if control.is_symlink() or not control.is_dir():
        raise HandoffError(f"control root is not a real directory: {control}")
    try:
        relative_parent = destination.parent.relative_to(control)
    except ValueError as error:
        raise HandoffError(f"publication destination escapes control root: {destination}") from error
    current = control
    for part in relative_parent.parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise HandoffError(f"publication parent is not a real directory: {current}")


def build_plan(control: Path, source: Path, allow_dirty: bool) -> Plan:
    control = control.resolve()
    source = source.resolve()
    beads_root = control / ".beads"
    if beads_root.is_symlink() or not beads_root.is_dir():
        raise HandoffError(f"Beads state is not a real directory: {beads_root}")
    for bead_path in beads_root.rglob("*"):
        if bead_path.is_symlink():
            raise HandoffError(f"Beads state contains an unsafe symlink: {bead_path}")
    for relative in PUBLICATION_PATHS:
        destination = control / relative
        _require_real_parent_chain(control, destination)
        if not destination.exists() and not destination.is_symlink():
            continue
        if relative == "handoff":
            valid = destination.is_dir() and not destination.is_symlink()
        else:
            valid = destination.is_file() and not destination.is_symlink()
        if not valid:
            raise HandoffError(f"invalid publication destination type: {destination}")
    if EXPECTED_PROJECTS_ENV in os.environ:
        closure = expected_closure(source, [])
        unexpected: list[str] = []
        for relative in closure:
            repo = source if relative == "." else source / relative
            _require_real_parent_chain(source, repo / ".git")
            top = _run_git(repo, "rev-parse", "--show-toplevel")
            if Path(top).resolve() != repo.resolve():
                raise HandoffError(f"expected Git worktree at {repo}, found top level {top}")
            for child, marker in _submodule_state(repo, recursive=False):
                child_path = (Path(relative) / child).as_posix()
                if marker == "U":
                    raise HandoffError(f"conflicted submodule: {child_path}")
                if child_path in closure:
                    continue
                child_repo = source / child_path
                child_top = _run_git(
                    child_repo, "rev-parse", "--show-toplevel", required=False
                )
                if marker != "-" or (
                    child_top and Path(child_top).resolve() == child_repo.resolve()
                ):
                    unexpected.append(child_path)
        if unexpected:
            raise HandoffError(
                "repository closure mismatch; unexpected initialized repositories: "
                + ", ".join(unexpected)
            )
    else:
        closure = expected_closure(source, _submodule_state(source))

    projects: list[Project] = []
    dirty_projects: list[str] = []
    filenames: set[str] = set()
    for relative in closure:
        repo = source if relative == "." else source / relative
        top = _run_git(repo, "rev-parse", "--show-toplevel")
        if Path(top).resolve() != repo.resolve():
            raise HandoffError(f"expected Git worktree at {repo}, found top level {top}")
        head = _run_git(repo, "rev-parse", "HEAD")
        branch = _run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", required=False)
        if not branch:
            branch = "DETACHED"
        remotes = _run_git(repo, "remote").splitlines()
        if "origin" in remotes:
            remote = "origin"
        elif len(remotes) == 1:
            remote = remotes[0]
        else:
            raise HandoffError(f"repository remote is ambiguous or missing: {repo}")
        origin = _run_git(repo, "remote", "get-url", remote)
        exclusions_for_status = [
            f":(exclude){Path(child).relative_to(relative).as_posix()}"
            for child in closure
            if child != relative and Path(child).is_relative_to(relative)
        ] if EXPECTED_PROJECTS_ENV in os.environ else []
        dirty = tuple(
            _run_git(
                repo, "status", "--porcelain", "--ignore-submodules=all",
                "--", ".", *exclusions_for_status,
            ).splitlines()
        )
        if dirty:
            dirty_projects.append(relative)
        branch_dicts = private_branches(
            repo, include_remote_only=relative == ".", remote=remote
        )
        branches = tuple(Branch(**record) for record in branch_dicts)
        exclusions: list[str] = []
        for default in ("main", "master"):
            base = _run_git(
                repo,
                "rev-parse",
                "--verify",
                f"refs/remotes/{remote}/{default}",
                required=False,
            )
            if base:
                exclusions.append(f"^{base}")
        base_revision = public_base(repo, head, remote=remote) or head
        if branches:
            filename = bundle_filename(relative)
            if filename in filenames:
                raise HandoffError(f"bundle filename collision for {relative}: {filename}")
            filenames.add(filename)
        projects.append(
            Project(
                relative=relative,
                repo=repo,
                head=head,
                branch=branch,
                origin=origin,
                dirty=dirty,
                base_revision=base_revision,
                branches=branches,
                exclusions=tuple(exclusions),
            )
        )
    if dirty_projects and not allow_dirty:
        raise HandoffError(
            "uncommitted file changes are not included; clean these repositories or use "
            f"--allow-dirty: {', '.join(dirty_projects)}"
        )
    return Plan(control, source, tuple(projects), allow_dirty)


def _publication_actions(plan: Plan) -> dict[str, list[str]]:
    """Describe exact prospective artifact changes without creating them."""
    actions = {"add": [], "replace": [], "remove": []}
    expected = [
        ".beads/issues.jsonl",
        "state/repos.tsv",
        "locked.xml",
        "base.xml",
        "handoff/manifest.json",
        *[
            f"handoff/{bundle_filename(project.relative)}"
            for project in plan.projects
            if project.branches
        ],
    ]
    for relative in expected:
        destination = plan.control / relative
        action = "replace" if destination.exists() or destination.is_symlink() else "add"
        actions[action].append(relative)
    handoff = plan.control / "handoff"
    if handoff.is_dir():
        expected_handoff = {
            Path(relative).name for relative in expected if relative.startswith("handoff/")
        }
        for path in sorted(handoff.iterdir(), key=lambda item: item.name):
            if path.name not in expected_handoff:
                actions["remove"].append(f"handoff/{path.name}")
    return actions


def plan_payload(plan: Plan) -> dict[str, Any]:
    dirty = [project.relative for project in plan.projects if project.dirty]
    next_action = "west dw handoff"
    if plan.allow_dirty:
        next_action += " --allow-dirty"
    return {
        "version": 1,
        "project_count": len(plan.projects),
        "bundle_count": plan.bundle_count,
        "dirty_override": plan.allow_dirty,
        "dirty_projects": dirty,
        "actions": _publication_actions(plan),
        "next_safe_action": next_action,
        "projects": [
            {
                "path": project.relative,
                "head": project.head,
                "branch": project.branch,
                "origin": project.origin,
                "dirty": bool(project.dirty),
                "private_ref_count": len(project.branches),
            }
            for project in plan.projects
        ],
    }


def print_plan(plan: Plan, as_json: bool) -> None:
    payload = plan_payload(plan)
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print("handoff plan")
    print(f"projects: {payload['project_count']}")
    print(f"bundles: {payload['bundle_count']}")
    print(f"dirty override: {'enabled' if plan.allow_dirty else 'disabled'}")
    for action in ("add", "replace", "remove"):
        values = payload["actions"][action]
        print(f"{action}: {', '.join(values) if values else '-'}")
    print(f"next safe action: {payload['next_safe_action']}")


def _write_repos(plan: Plan, output: Path) -> None:
    lines = ["path\thead\tbranch\torigin\tworktree"]
    for project in plan.projects:
        state = "dirty" if project.dirty else "clean"
        lines.append(
            "\t".join(
                (project.relative, project.head, project.branch, project.origin, state)
            )
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fault(label: str) -> None:
    if os.environ.get("DW_HANDOFF_FAIL_AT") == label:
        raise HandoffError(f"injected failure at {label}")
    if os.environ.get("DW_HANDOFF_INTERRUPT_AT") == label:
        raise KeyboardInterrupt
    if os.environ.get("DW_HANDOFF_CRASH_AT") == label:
        os._exit(86)

def stage(plan: Plan, scratch: Path) -> tuple[dict[str, str], list[dict[str, object]]]:
    beads = scratch / ".beads"
    shutil.copytree(plan.control / ".beads", beads, symlinks=True)
    _fault("stage:beads-copy")
    env = os.environ.copy()
    env["BEADS_DIR"] = str(beads)
    try:
        result = run_bounded(
            ["br", "sync", "--flush-only"],
            cwd=plan.control,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise HandoffError("br sync --flush-only timed out against staged Beads state") from error
    if result.overflow:
        raise HandoffError("br sync --flush-only output limit exceeded")
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip() or "unknown error")[:4096]
        raise HandoffError(f"br sync --flush-only failed against staged Beads state: {detail}")
    if not (beads / "issues.jsonl").is_file():
        raise HandoffError("staged br sync did not produce .beads/issues.jsonl")
    _fault("stage:beads-flush")

    _write_repos(plan, scratch / "state" / "repos.tsv")
    _fault("stage:repos")
    locked = [(item.relative, item.head, item.origin) for item in plan.projects]
    base = [(item.relative, item.base_revision, item.origin) for item in plan.projects]
    write_manifest(locked, scratch / "locked.xml")
    _fault("stage:locked")
    write_manifest(base, scratch / "base.xml")
    _fault("stage:base")
    package_projects = [
        (
            project.relative,
            project.repo,
            [branch.as_dict() for branch in project.branches],
            list(project.exclusions),
        )
        for project in plan.projects
    ]
    records = write_package(package_projects, scratch / "handoff")
    _fault("stage:handoff")
    _fsync_tree(scratch)
    hashes = validate_stage(plan, scratch, records)
    _fault("stage:validated")
    return hashes, records


def _xml_projects(path: Path) -> list[tuple[str, str]]:
    root = ET.parse(path).getroot()
    return [(item.attrib["path"], item.attrib["revision"]) for item in root.findall("project")]


def validate_stage(
    plan: Plan, scratch: Path, records: list[dict[str, object]]
) -> dict[str, str]:
    expected_locked = [
        ("darling" if p.relative == "." else f"darling/{p.relative}", p.head)
        for p in plan.projects
    ]
    expected_base = [
        ("darling" if p.relative == "." else f"darling/{p.relative}", p.base_revision)
        for p in plan.projects
    ]
    if _xml_projects(scratch / "locked.xml") != expected_locked:
        raise HandoffError("staged locked.xml project closure or revisions differ from plan")
    if _xml_projects(scratch / "base.xml") != expected_base:
        raise HandoffError("staged base.xml project closure or revisions differ from plan")

    expected_records = []
    expected_handoff_files = {"manifest.json"}
    for project in plan.projects:
        if not project.branches:
            continue
        filename = bundle_filename(project.relative)
        expected_handoff_files.add(filename)
        expected_records.append(
            {
                "path": project.relative,
                "bundle": filename,
                "branches": [branch.as_dict() for branch in project.branches],
            }
        )
        expected_heads = {
            branch.source_ref: branch.head for branch in project.branches
        }
        observed_heads = bundle_heads(scratch / "handoff" / filename)
        if observed_heads != expected_heads:
            raise HandoffError(f"staged bundle heads differ for {project.relative}")
    manifest_path = scratch / "handoff" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HandoffError(f"invalid staged handoff manifest: {error}") from error
    if manifest != {"version": 1, "projects": expected_records} or records != expected_records:
        raise HandoffError("staged handoff manifest differs from frozen plan")
    actual_handoff_files = {
        path.name for path in (scratch / "handoff").iterdir() if path.is_file()
    }
    if actual_handoff_files != expected_handoff_files:
        raise HandoffError(
            "staged handoff file closure differs: "
            f"expected {sorted(expected_handoff_files)}, observed {sorted(actual_handoff_files)}"
        )
    for relative in PUBLICATION_PATHS:
        path = scratch / relative
        if relative == "handoff":
            if not path.is_dir() or path.is_symlink():
                raise HandoffError("staged handoff is not a real directory")
        elif not path.is_file() or path.is_symlink():
            raise HandoffError(f"staged publication file is invalid: {relative}")
    return {relative: _snapshot(scratch / relative) for relative in PUBLICATION_PATHS}


def _snapshot(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        raise HandoffError(f"cannot snapshot missing path: {path}")
    records: list[tuple[str, str, int, str]] = []
    paths = [path]
    if path.is_dir() and not path.is_symlink():
        paths.extend(sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()))
    for item in paths:
        relative = "." if item == path else item.relative_to(path).as_posix()
        mode = stat.S_IMODE(item.lstat().st_mode)
        if item.is_symlink():
            records.append((relative, "symlink", mode, os.readlink(item)))
        elif item.is_dir():
            records.append((relative, "directory", mode, ""))
        elif item.is_file():
            digest = hashlib.sha256(item.read_bytes()).hexdigest()
            records.append((relative, "file", mode, digest))
        else:
            raise HandoffError(f"unsupported staged path type: {item}")
    encoded = json.dumps(records, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            _fsync_file(path)
        elif path.is_dir() and not path.is_symlink():
            directories.append(path)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_dir(path)
    _fsync_dir(root)


def _durable_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise HandoffError(f"unsafe handoff journal destination: {path}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        raise HandoffError(f"cannot create handoff journal safely: {error}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_backup(source: Path, destination: Path) -> None:
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise HandoffError(f"unsupported publication destination type: {source}")
    _fsync_tree(destination) if destination.is_dir() else _fsync_file(destination)


def _transaction_paths(control: Path) -> tuple[Path, Path, Path, Path]:
    state = control.parent / f".{control.name}.handoff-transaction"
    return state, state / "journal.json", state / "backups", state / "displaced"


def _validate_transaction_state(state: Path) -> None:
    try:
        metadata = state.lstat()
    except FileNotFoundError as error:
        raise HandoffError(f"handoff transaction state is missing: {state}") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or state.is_symlink()
        or metadata.st_uid != os.getuid()
        or mode & 0o077
    ):
        raise HandoffError(
            f"handoff transaction state must be an owner-only real directory: {state}"
        )


def _validate_scratch_path(control: Path, scratch: Path, *, allow_missing: bool) -> None:
    expected_prefix = f".{control.name}.handoff-stage-"
    if (
        scratch.parent != control.parent
        or not scratch.name.startswith(expected_prefix)
        or len(scratch.name) == len(expected_prefix)
    ):
        raise HandoffError(f"invalid handoff scratch path in journal: {scratch}")
    if not scratch.exists():
        if allow_missing:
            return
        raise HandoffError(f"handoff scratch path is missing: {scratch}")
    metadata = scratch.lstat()
    if scratch.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise HandoffError(f"handoff scratch path is not a real directory: {scratch}")


def _open_lock(path: Path):
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise HandoffError(f"handoff lock must be an owner-only regular file: {path}")
    return os.fdopen(descriptor, "a+")


def _journal_entries(
    journal: dict[str, Any], control: Path, state: Path
) -> list[dict[str, Any]]:
    if journal.get("version") != 1 or Path(journal.get("control", "")).resolve() != control:
        raise HandoffError("handoff recovery journal belongs to another control repository")
    entries = journal.get("entries")
    if (
        not isinstance(entries, list)
        or not all(isinstance(entry, dict) for entry in entries)
        or [entry.get("relative") for entry in entries] != list(PUBLICATION_PATHS)
    ):
        raise HandoffError("handoff recovery journal has an invalid publication closure")
    for index, entry in enumerate(entries):
        expected_backup = str(state / "backups" / str(index))
        expected_displaced = str(state / "displaced" / str(index))
        if entry.get("backup") != expected_backup or entry.get("displaced") != expected_displaced:
            raise HandoffError("handoff recovery journal contains an invalid transaction path")
    return entries


def _cleanup_transaction(
    control: Path, state: Path, journal_path: Path, scratch: Path | None
) -> None:
    if scratch is not None:
        _validate_scratch_path(control, scratch, allow_missing=True)
        if scratch.exists():
            shutil.rmtree(scratch)
            _fsync_dir(scratch.parent)
    for name in ("backups", "displaced"):
        path = state / name
        if path.exists():
            shutil.rmtree(path)
    journal_temporary = journal_path.with_name(journal_path.name + ".tmp")
    if journal_temporary.is_symlink() or journal_temporary.is_file():
        journal_temporary.unlink()
    elif journal_temporary.exists():
        raise HandoffError(f"unsafe temporary handoff journal: {journal_temporary}")
    if journal_path.exists():
        journal_path.unlink()
    _fsync_dir(state)


def _read_journal(control: Path, state: Path, journal_path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(journal_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise HandoffError(f"cannot open handoff recovery journal safely: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > 1024 * 1024
        ):
            raise HandoffError("handoff recovery journal is not a safe regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise HandoffError(f"cannot read handoff recovery journal: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _journal_entries(payload, control, state)
    if payload.get("state") not in {"prepared", "committed"}:
        raise HandoffError("handoff recovery journal has an invalid state")
    return payload


def _inspect_read_only_journal(control: Path, state: Path, journal_path: Path) -> None:
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    payload = _read_journal(control, state, journal_path)
    if payload["state"] == "prepared":
        raise HandoffError(
            "an interrupted handoff requires recovery; run west dw handoff without "
            "--dry-run/--json first"
        )


def _cleanup_orphans(control: Path, state: Path, journal_path: Path) -> None:
    if journal_path.exists() or journal_path.is_symlink():
        return
    changed = False
    for path in (state / "backups", state / "displaced"):
        if path.is_symlink() or path.is_file():
            path.unlink()
            changed = True
        elif path.is_dir():
            shutil.rmtree(path)
            changed = True
    journal_temporary = journal_path.with_name(journal_path.name + ".tmp")
    if journal_temporary.exists() or journal_temporary.is_symlink():
        journal_temporary.unlink()
        changed = True
    for path in control.parent.glob(f".{control.name}.handoff-stage-*"):
        if path.is_symlink() or path.is_file():
            path.unlink()
            changed = True
        elif path.is_dir():
            shutil.rmtree(path)
            changed = True
    if changed:
        _fsync_dir(state)
        _fsync_dir(control.parent)


def recover(control: Path, state: Path, journal_path: Path) -> None:
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    _validate_transaction_state(state)
    journal = _read_journal(control, state, journal_path)
    entries = _journal_entries(journal, control, state)
    scratch_text = journal.get("scratch")
    if not isinstance(scratch_text, str):
        raise HandoffError("handoff recovery journal has no valid scratch path")
    scratch = Path(scratch_text)
    _validate_scratch_path(control, scratch, allow_missing=True)
    if journal["state"] == "committed":
        try:
            _cleanup_transaction(control, state, journal_path, scratch)
        except OSError as error:
            print(f"warning: committed handoff cleanup failed: {error}", file=sys.stderr)
        return

    for entry in reversed(entries):
        destination = control / entry["relative"]
        _require_real_parent_chain(control, destination)
        index = PUBLICATION_PATHS.index(entry["relative"])
        backup = state / "backups" / str(index)
        existed = bool(entry["existed"])
        old = entry.get("before")
        new = entry["after"]
        observed = _snapshot(destination) if destination.exists() or destination.is_symlink() else None
        if observed == old:
            continue
        if observed is not None and observed != new:
            raise HandoffError(f"refusing to overwrite changed destination during recovery: {destination}")
        if observed is not None:
            _remove(destination)
        _require_real_parent_chain(control, destination)
        if destination.exists() or destination.is_symlink():
            raise HandoffError(f"new destination appeared during recovery: {destination}")
        if existed:
            if not backup.exists() and not backup.is_symlink():
                raise HandoffError(f"handoff recovery backup is missing: {backup}")
            if _snapshot(backup) != old:
                raise HandoffError(f"handoff recovery backup differs: {backup}")
            os.replace(backup, destination)
        _fsync_dir(destination.parent)
    _cleanup_transaction(control, state, journal_path, scratch)


def publish(plan: Plan, scratch: Path, hashes: dict[str, str], state: Path, journal: Path) -> None:
    backups = state / "backups"
    displaced = state / "displaced"
    if backups.exists() or displaced.exists():
        raise HandoffError("handoff transaction storage is not clean")
    try:
        backups.mkdir()
        displaced.mkdir()
        units: list[Unit] = []
        for index, relative in enumerate(PUBLICATION_PATHS):
            destination = plan.control / relative
            _require_real_parent_chain(plan.control, destination)
            staged = scratch / relative
            existed = destination.exists() or destination.is_symlink()
            before = _snapshot(destination) if existed else None
            backup = backups / str(index)
            if existed:
                _copy_backup(destination, backup)
            units.append(
                Unit(
                    relative,
                    staged,
                    destination,
                    before,
                    hashes[relative],
                    backup,
                    displaced / str(index),
                )
            )
        _fsync_tree(backups)
        payload: dict[str, Any] = {
            "version": 1,
            "state": "prepared",
            "control": str(plan.control),
            "scratch": str(scratch),
            "entries": [
                {
                    "relative": unit.relative,
                    "before": unit.before,
                    "after": unit.after,
                    "existed": unit.before is not None,
                    "backup": str(unit.backup),
                    "displaced": str(unit.displaced),
                }
                for unit in units
            ],
        }
        _durable_json(journal, payload)
        _fault("publish:prepared")
        for index, unit in enumerate(units):
            _fault(f"publish:before:{index}")
            _require_real_parent_chain(plan.control, unit.destination)
            observed = (
                _snapshot(unit.destination)
                if unit.destination.exists() or unit.destination.is_symlink()
                else None
            )
            if observed != unit.before:
                raise HandoffError(f"publication destination drifted: {unit.destination}")
            if unit.before is not None:
                os.replace(unit.destination, unit.displaced)
                _fsync_dir(unit.destination.parent)
                _fsync_dir(unit.displaced.parent)
                if _snapshot(unit.displaced) != unit.before:
                    if not unit.destination.exists() and unit.displaced.exists():
                        os.replace(unit.displaced, unit.destination)
                        _fsync_dir(unit.destination.parent)
                    raise HandoffError(f"displaced destination differs: {unit.destination}")
            _fault(f"publish:displaced:{index}")
            _require_real_parent_chain(plan.control, unit.destination)
            if unit.destination.exists() or unit.destination.is_symlink():
                raise HandoffError(
                    f"new destination appeared before publication: {unit.destination}"
                )
            os.replace(unit.staged, unit.destination)
            _fsync_dir(unit.destination.parent)
            if _snapshot(unit.destination) != unit.after:
                raise HandoffError(f"published destination differs from stage: {unit.destination}")
            _fault(f"publish:after:{index}")
        payload["state"] = "committed"
        _durable_json(journal, payload)
    except BaseException:
        if journal.exists():
            recover(plan.control, state, journal)
        else:
            if backups.exists():
                shutil.rmtree(backups)
            if displaced.exists():
                shutil.rmtree(displaced)
        raise

    # The journal's durable committed state is the commit point. POSIX cannot make
    # five compatibility paths visible in one rename, so every pre-commit
    # interruption rolls back, while cleanup after this point is warning-only.
    try:
        _fault("publish:committed")
        _cleanup_transaction(plan.control, state, journal, scratch)
    except BaseException as error:
        print(f"warning: committed handoff cleanup failed: {error}", file=sys.stderr)


def execute(control: Path, source: Path, allow_dirty: bool, dry_run: bool, as_json: bool) -> Plan:
    control = control.resolve()
    source = source.resolve()
    state, journal, _backups, _displaced = _transaction_paths(control)
    state_present = state.exists() or state.is_symlink()
    if state_present:
        _validate_transaction_state(state)
    if dry_run or as_json:
        _inspect_read_only_journal(control, state, journal)
        plan = build_plan(control, source, allow_dirty)
        print_plan(plan, as_json)
        return plan

    if not state_present:
        os.mkdir(state, 0o700)
    _validate_transaction_state(state)
    _fsync_dir(control.parent)
    lock_path = state / "lock"
    with _open_lock(lock_path) as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HandoffError("another handoff transaction holds the exclusive lock") from error
        _cleanup_orphans(control, state, journal)
        recover(control, state, journal)
        plan = build_plan(control, source, allow_dirty)
        scratch = Path(
            tempfile.mkdtemp(prefix=f".{control.name}.handoff-stage-", dir=control.parent)
        )
        committed = False
        try:
            hashes, records = stage(plan, scratch)
            if build_plan(control, source, allow_dirty) != plan:
                raise HandoffError("source repositories changed after the handoff plan was frozen")
            publish(plan, scratch, hashes, state, journal)
            committed = True
        finally:
            if not committed and scratch.exists() and not journal.exists():
                shutil.rmtree(scratch)
                _fsync_dir(scratch.parent)
        print(f"packed {len(records)} repositories into {control / 'handoff'}")
        dirty = [project.relative for project in plan.projects if project.dirty]
        if allow_dirty:
            print(f"dirty override: enabled ({len(dirty)} dirty repositories)")
        print(f"wrote {control / 'state' / 'repos.tsv'}")
        print(f"wrote {control / 'locked.xml'}")
        print(f"wrote {control / 'base.xml'}")
        return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-root", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    try:
        execute(args.control_root, args.source, args.allow_dirty, args.dry_run, args.json)
    except KeyboardInterrupt:
        print("handoff: interrupted; published paths were rolled back", file=sys.stderr)
        return 130
    except (HandoffError, OSError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"handoff: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

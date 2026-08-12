"""Fail-closed source inventory for runtime-prefix namespace writers.

This is deliberately a source contract (``coverage-tier: source``), not a
behavioural claim.  It binds every inventoried owner to an existing source
file and anchor, rejects a routable writer without an exact exclusive lease,
and scans the complete production forest for an unregistered mutation owner.
Python has no filesystem authority in the target design; this contract only
audits the current owners and records the migration gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DARLING = ROOT.parent / "darling"
REGISTRY_PATH = ROOT / "lifecycle" / "namespace-writer-inventory-v1.json"
AUDIT_CLASSIFICATIONS = {
    "build-or-generator-output",
    "caller-selected-library-output",
    "language-runtime-private-output",
    "non-filesystem-lexical-hit",
    "test-fixture-task-owned",
    "user-invoked-command-output",
    "workspace-task-owned-tooling",
}
AUDIT_RUNTIME_SCOPES = {
    "build-tree-only",
    "caller-owned-path",
    "language-runtime-owned-path",
    "no-filesystem-mutation",
    "task-owned-fixture",
    "user-command-owned-path",
    "workspace-task-root",
}
AUDIT_CLASSIFICATION_SCOPES = {
    "build-or-generator-output": "build-tree-only",
    "caller-selected-library-output": "caller-owned-path",
    "language-runtime-private-output": "language-runtime-owned-path",
    "non-filesystem-lexical-hit": "no-filesystem-mutation",
    "test-fixture-task-owned": "task-owned-fixture",
    "user-invoked-command-output": "user-command-owned-path",
    "workspace-task-owned-tooling": "workspace-task-root",
}
AUDIT_RELATIONS = {
    "nearest-cmake-ancestor",
    "source-forest-fallback",
    "workspace-runtime-source",
}
AUDIT_MUTATION_OPERATOR_PATTERNS = {
    "chmod": re.compile(r"(?<![A-Za-z0-9_.>:])(?:chmod|fchmod|fchmodat)\s*\("),
    "chown": re.compile(r"(?<![A-Za-z0-9_.>:])(?:chown|lchown|fchown|fchownat)\s*\("),
    "create": re.compile(r"(?<![A-Za-z0-9_.>:])(?:creat|mknod|mknodat|pidfile_open)\s*\("),
    "link": re.compile(r"(?<![A-Za-z0-9_.>:])(?:link|linkat|symlink|symlinkat)\s*\("),
    "mkdir": re.compile(r"(?<![A-Za-z0-9_.>:])(?:mkdir|mkdirat)\s*\(|\.mkdir\s*\("),
    "mount": re.compile(r"(?<![A-Za-z0-9_.>:])(?:mount|unmount)\s*\("),
    "open-create-or-truncate": re.compile(
        r"(?<![A-Za-z0-9_.>:])(?:open|openat)\s*\((?=[^;\n]{0,384}(?:O_CREAT|O_TRUNC))"
    ),
    "python-create": re.compile(r"\.(?:write_text|write_bytes|touch|symlink_to)\s*\("),
    "remove": re.compile(r"(?<![A-Za-z0-9_.>:])(?:unlink|unlinkat|rmdir)\s*\(|\.unlink\s*\("),
    "rename": re.compile(r"(?<![A-Za-z0-9_.>:])(?:rename|renameat|renameat2|renamex_np)\s*\("),
    "time-metadata": re.compile(r"(?<![A-Za-z0-9_.>:])utimensat\s*\("),
    "write-open": re.compile(
        r"(?<![A-Za-z0-9_.>:])fopen\s*\((?=[^;\n]{0,384},\s*[\"'](?:[^\"']*[wax+])[^\"']*[\"'])"
    ),
}


class InventoryError(AssertionError):
    """The source inventory is incomplete or internally unsafe."""


CMakeSourceGroup = tuple[Path, list[str], dict[str, list[str]]]
CMakeTargetGraph = tuple[
    dict[str, list[CMakeSourceGroup]],
    dict[str, list[CMakeSourceGroup]],
]


def _load_registry() -> dict:
    try:
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot load writer inventory: {error}") from error
    if not isinstance(payload, dict):
        raise InventoryError("writer inventory root must be an object")
    allowed = {
        "schema_version",
        "kind",
        "coverage_tier",
        "threat_model",
        "authority",
        "source_forest",
        "scan",
        "writers",
        "excluded_mutations",
        "migration_plan",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise InventoryError(f"unknown inventory fields: {sorted(unknown)}")
    if payload.get("schema_version") != 1:
        raise InventoryError("unsupported writer inventory schema")
    if payload.get("kind") != "rootless-namespace-writer-inventory":
        raise InventoryError("wrong writer inventory kind")
    if payload.get("coverage_tier") != "source":
        raise InventoryError("inventory must declare coverage-tier: source")
    threat = payload.get("threat_model")
    authority = payload.get("authority")
    scan = payload.get("scan")
    writers = payload.get("writers")
    if not isinstance(threat, dict) or not isinstance(authority, dict):
        raise InventoryError("threat_model and authority are required objects")
    if threat.get("lock_path") != ".lifecycle.lock":
        raise InventoryError("inventory lock path must be .lifecycle.lock")
    if authority.get("controller") != "Rust-owned lifecycle controller":
        raise InventoryError("writer authority must be Rust-owned")
    if authority.get("python") != "transport-and-orchestration-only":
        raise InventoryError("Python must remain transport/orchestration-only")
    forest = payload.get("source_forest")
    if (
        not isinstance(forest, dict)
        or forest.get("repository") != "darling"
        or forest.get("root") != "src"
        or not isinstance(forest.get("build_file"), str)
        or not isinstance(forest.get("closure_rule"), str)
    ):
        raise InventoryError("source_forest must anchor the Darling build forest")
    if not isinstance(scan, dict) or scan.get("required_paths_are_exhaustive") is not True:
        raise InventoryError("scan must explicitly declare exhaustive required paths")
    obsolete_prefilters = {
        "runtime_tokens",
        "source_runtime_tokens",
        "indirect_path_tokens",
        "mutation_window_lines",
    }
    if obsolete_prefilters & set(scan):
        raise InventoryError("runtime-token-first scan fields are forbidden")
    if (
        not isinstance(scan.get("translation_unit_extensions"), list)
        or not scan["translation_unit_extensions"]
        or not isinstance(scan.get("namespace_mutation_tokens"), list)
        or not scan["namespace_mutation_tokens"]
    ):
        raise InventoryError("mutation-first translation-unit grammar is required")
    universe = scan.get("universe")
    if (
        not isinstance(universe, dict)
        or universe.get("kind") != "production-forest-with-build-anchor"
        or not isinstance(universe.get("source_forest"), dict)
        or universe["source_forest"].get("repository") != "darling"
        or universe["source_forest"].get("root") != "src"
        or universe["source_forest"].get("build_file") != "src/CMakeLists.txt"
        or not isinstance(universe.get("runtime_roots"), list)
        or not isinstance(universe.get("rule"), str)
        or not universe["rule"].strip()
    ):
        raise InventoryError("scan.universe must bind discovery to the complete production forest")
    if universe["source_forest"] != {
        "repository": forest["repository"],
        "root": forest["root"],
        "build_file": forest["build_file"],
    }:
        raise InventoryError("scan.universe source forest must exactly match source_forest")
    if not isinstance(writers, list) or not writers:
        raise InventoryError("writer inventory is empty")
    return payload


def _repository_root(repository: str) -> Path:
    if repository == "darling-workspace":
        return ROOT
    if repository == "darling":
        return DARLING
    raise InventoryError(f"unknown repository in writer owner: {repository!r}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_mutation_operators(source: str) -> list[str]:
    return sorted(
        name for name, pattern in AUDIT_MUTATION_OPERATOR_PATTERNS.items() if pattern.search(source)
    )


def _audit_path(registry: dict) -> Path:
    reference = registry["scan"].get("audited_non_shared_candidates")
    if (
        not isinstance(reference, dict)
        or set(reference) != {"schema_version", "path", "rule"}
        or reference.get("schema_version") != 1
        or not isinstance(reference.get("rule"), str)
        or not reference["rule"].strip()
        or not isinstance(reference.get("path"), str)
    ):
        raise InventoryError("per-path audited candidate reference is malformed")
    relative = Path(reference["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise InventoryError("candidate audit path must be workspace-relative")
    path = ROOT / relative
    if not path.is_file() or path.is_symlink():
        raise InventoryError(f"candidate audit is not a retained regular file: {path}")
    return path


def _load_candidate_audit(registry: dict) -> dict:
    path = _audit_path(registry)
    try:
        audit = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot load candidate audit: {error}") from error
    if not isinstance(audit, dict) or set(audit) != {
        "schema_version",
        "kind",
        "coverage_tier",
        "count",
        "entries_digest",
        "entries",
    }:
        raise InventoryError("candidate audit root has unknown or missing fields")
    if (
        audit.get("schema_version") != 1
        or audit.get("kind") != "rootless-namespace-writer-candidate-audit"
        or audit.get("coverage_tier") != "source"
        or not isinstance(audit.get("entries"), list)
        or not isinstance(audit.get("count"), int)
        or not isinstance(audit.get("entries_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", audit["entries_digest"])
    ):
        raise InventoryError("candidate audit header is malformed")
    canonical = json.dumps(
        audit["entries"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if audit["count"] != len(audit["entries"]) or audit["entries_digest"] != hashlib.sha256(
        canonical
    ).hexdigest():
        raise InventoryError("candidate audit count/digest mismatch")
    return audit


def _validate_candidate_audit(
    registry: dict, *, audit_override: dict | None = None
) -> dict[str, dict]:
    audit = audit_override if audit_override is not None else _load_candidate_audit(registry)
    if not isinstance(audit, dict) or not isinstance(audit.get("entries"), list):
        raise InventoryError("candidate audit override is malformed")
    canonical = json.dumps(
        audit["entries"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if audit.get("count") != len(audit["entries"]) or audit.get(
        "entries_digest"
    ) != hashlib.sha256(canonical).hexdigest():
        raise InventoryError("candidate audit count/digest mismatch")
    entries: dict[str, dict] = {}
    for entry in audit["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "repository",
            "path",
            "sha256",
            "classification",
            "reason",
            "evidence",
        }:
            raise InventoryError("candidate audit entry has unknown or missing fields")
        repository = entry.get("repository")
        relative = entry.get("path")
        digest = entry.get("sha256")
        classification = entry.get("classification")
        reason = entry.get("reason")
        evidence = entry.get("evidence")
        if repository not in {"darling", "darling-workspace"} or not isinstance(relative, str):
            raise InventoryError("candidate audit entry has invalid repository/path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InventoryError(f"candidate audit entry has invalid SHA: {repository}:{relative}")
        if classification not in AUDIT_CLASSIFICATIONS:
            raise InventoryError(f"candidate audit classification is invalid: {repository}:{relative}")
        if not isinstance(reason, str) or not reason.strip() or relative not in reason:
            raise InventoryError(f"candidate audit reason is not path-specific: {repository}:{relative}")
        if not isinstance(evidence, dict) or set(evidence) != {
            "runtime_scope",
            "mutation_operators",
            "build_anchor",
        }:
            raise InventoryError(f"candidate audit evidence is malformed: {repository}:{relative}")
        runtime_scope = evidence.get("runtime_scope")
        operators = evidence.get("mutation_operators")
        anchor = evidence.get("build_anchor")
        if runtime_scope not in AUDIT_RUNTIME_SCOPES:
            raise InventoryError(f"candidate audit runtime scope is invalid: {repository}:{relative}")
        if runtime_scope != AUDIT_CLASSIFICATION_SCOPES[classification]:
            raise InventoryError(
                f"candidate audit classification/scope mismatch: {repository}:{relative}"
            )
        if (
            not isinstance(operators, list)
            or not operators
            or not all(isinstance(operator, str) and operator for operator in operators)
            or operators != sorted(set(operators))
        ):
            raise InventoryError(f"candidate audit mutation evidence is invalid: {repository}:{relative}")
        if not isinstance(anchor, dict) or set(anchor) != {
            "repository",
            "path",
            "sha256",
            "relation",
        }:
            raise InventoryError(f"candidate audit build anchor is malformed: {repository}:{relative}")
        if anchor.get("repository") not in {"darling", "darling-workspace"}:
            raise InventoryError(f"candidate audit build repository is invalid: {repository}:{relative}")
        anchor_path = anchor.get("path")
        anchor_digest = anchor.get("sha256")
        if (
            not isinstance(anchor_path, str)
            or not isinstance(anchor_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", anchor_digest)
            or anchor.get("relation") not in AUDIT_RELATIONS
        ):
            raise InventoryError(f"candidate audit build evidence is invalid: {repository}:{relative}")
        absolute = _repository_root(repository) / relative
        anchor_absolute = _repository_root(anchor["repository"]) / anchor_path
        if not absolute.is_file() or absolute.is_symlink() or _sha256_file(absolute) != digest:
            raise InventoryError(f"candidate audit source SHA mismatch: {repository}:{relative}")
        source = absolute.read_text(encoding="utf-8", errors="replace")
        if operators != _audit_mutation_operators(source):
            raise InventoryError(
                f"candidate audit mutation evidence mismatch: {repository}:{relative}"
            )
        if (
            not anchor_absolute.is_file()
            or anchor_absolute.is_symlink()
            or _sha256_file(anchor_absolute) != anchor_digest
        ):
            raise InventoryError(f"candidate audit build anchor SHA mismatch: {repository}:{relative}")
        key = f"{repository}:{relative}"
        if key in entries:
            raise InventoryError(f"duplicate candidate audit entry: {key}")
        entries[key] = entry
    return entries


def _owner_paths(registry: dict) -> dict[str, dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    ids: set[str] = set()
    for writer in registry["writers"]:
        if not isinstance(writer, dict):
            raise InventoryError("writer entry must be an object")
        writer_id = writer.get("id")
        if not isinstance(writer_id, str) or not writer_id:
            raise InventoryError("writer id is required")
        if writer_id in ids:
            raise InventoryError(f"duplicate writer id: {writer_id}")
        ids.add(writer_id)
        owner = writer.get("owner")
        if not isinstance(owner, dict):
            raise InventoryError(f"{writer_id}: owner is required")
        repository = owner.get("repository")
        paths = owner.get("paths")
        symbols = owner.get("symbols")
        if repository not in {"darling", "darling-workspace"}:
            raise InventoryError(f"{writer_id}: unsupported repository {repository!r}")
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
            raise InventoryError(f"{writer_id}: owner.paths must be a non-empty string list")
        if not isinstance(symbols, list) or not symbols or not all(isinstance(symbol, str) for symbol in symbols):
            raise InventoryError(f"{writer_id}: owner.symbols must be a non-empty string list")
        lock = writer.get("lock")
        if not isinstance(lock, dict):
            raise InventoryError(f"{writer_id}: lock is required")
        compatibility = writer.get("compatibility")
        if compatibility not in {
            "incompatible",
            "cohort-ready",
            "compatible",
            "product-only-exception",
        }:
            raise InventoryError(f"{writer_id}: invalid compatibility classification")
        _validate_lock_contract(writer_id, compatibility, lock)
        operation = writer.get("operation")
        targets = writer.get("targets")
        syscall_or_helper = writer.get("syscall_or_helper")
        phase = writer.get("lifecycle_phase")
        migration = writer.get("migration")
        if (
            not isinstance(operation, list)
            or not operation
            or not isinstance(targets, list)
            or not targets
            or not isinstance(syscall_or_helper, list)
            or not syscall_or_helper
            or not isinstance(phase, str)
            or not phase
            or not isinstance(migration, str)
            or not migration
        ):
            raise InventoryError(f"{writer_id}: operation/syscall/phase/migration are required")
        for path in paths:
            key = f"{repository}:{path}"
            # Several typed writer records may share one translation unit
            # (for example startup publication and stale repair).  Coverage is
            # path-based, while ownership remains symbol-based in the record.
            seen.setdefault(
                key,
                {"writer_id": writer_id, "repository": repository, "path": path},
            )
            absolute = _repository_root(repository) / path
            if not absolute.is_file():
                raise InventoryError(f"{writer_id}: owner path missing: {absolute}")
            source = absolute.read_text(encoding="utf-8", errors="replace")
            if not any(_symbol_anchor(source, symbol) for symbol in symbols):
                raise InventoryError(
                    f"{writer_id}: no declared symbol anchor found in {repository}:{path}"
                )
    return seen


def _symbol_anchor(source: str, symbol: str) -> bool:
    if "*" in symbol:
        return symbol.split("*", 1)[0] in source
    if symbol in source:
        return True
    # A C/C++ symbol can be qualified in the registry while the source uses
    # the unqualified definition, and Python anchors may be attribute names.
    tail = symbol.rsplit("::", 1)[-1]
    return bool(tail and re.search(rf"\b{re.escape(tail)}\b", source))


def _validate_lock_contract(writer_id: str, compatibility: str, lock: dict) -> None:
    if lock.get("required") is not True or lock.get("path") != ".lifecycle.lock":
        raise InventoryError(f"{writer_id}: exact .lifecycle.lock is mandatory")
    if not isinstance(lock.get("acquisition"), str) or not lock.get("acquisition"):
        raise InventoryError(f"{writer_id}: exact lock acquisition evidence is required")
    if not isinstance(lock.get("retained_fd"), bool):
        raise InventoryError(f"{writer_id}: retained_fd must be boolean")
    if lock.get("status") not in {"missing", "different-path", "exact-exclusive-flock"}:
        raise InventoryError(f"{writer_id}: invalid lock status")
    if compatibility in {"cohort-ready", "compatible"} and lock.get("status") != "exact-exclusive-flock":
        raise InventoryError(f"{writer_id}: routed writer lacks exact exclusive flock")
    if compatibility in {"cohort-ready", "compatible"} and not lock.get("retained_fd"):
        raise InventoryError(f"{writer_id}: routed writer must retain the lock FD")


def _runtime_scan_paths() -> list[tuple[str, str]]:
    """Return the finite production source roots covered by this inventory."""

    return [
        ("darling", "src/startup/darling.c"),
        ("darling", "src/shellspawn/shellspawn.c"),
        ("darling", "src/launchd/src/ipc.c"),
        ("darling", "src/launchd/src/core.c"),
        ("darling", "src/launchd/support/launchctl.c"),
        ("darling", "src/external/darlingserver/src/darlingserver.cpp"),
        ("darling", "src/external/darlingserver/src/server.cpp"),
        (
            "darling",
            "src/external/xnu/darling/src/libsystem_kernel/emulation/src/linux_premigration/vchroot_userspace.c",
        ),
        ("darling-workspace", "west_commands/test_prefix.py"),
        ("darling-workspace", "west_commands/prefix_repair.py"),
        ("darling-workspace", "west_commands/deploy_transaction.py"),
        ("darling-workspace", "west_commands/test_runtime_deploy.py"),
        ("darling-workspace", "west_commands/test_bootstrap.py"),
        ("darling-workspace", "west_commands/fresh_prefix.py"),
        ("darling-workspace", "west_commands/darling_build.py"),
        ("darling-workspace", "west_commands/guest_toolchain.py"),
        ("darling-workspace", "west_commands/darling_prefix_repair.py"),
    ]


def _excluded_keys(registry: dict) -> set[str]:
    """Validate exact-file, SHA-bound non-production mutation exclusions."""

    keys: set[str] = set()
    for entry in registry.get("excluded_mutations", []):
        if not isinstance(entry, dict) or set(entry) != {
            "repository",
            "path",
            "sha256",
            "reason",
        }:
            raise InventoryError("excluded_mutations entries require exact path/SHA/reason fields")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise InventoryError(f"exclusion {entry.get('path')!r} lacks an audit reason")
        repository = entry.get("repository")
        path = entry["path"]
        if repository not in {"darling", "darling-workspace"}:
            raise InventoryError(f"unknown exclusion repository: {repository!r}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InventoryError(f"exclusion SHA is invalid: {repository}:{path}")
        absolute = _repository_root(repository) / path
        if not absolute.is_file() or absolute.is_symlink():
            raise InventoryError(f"excluded path is not an exact regular file: {repository}:{path}")
        if _sha256_file(absolute) != digest:
            raise InventoryError(f"excluded source SHA mismatch: {repository}:{path}")
        key = f"{repository}:{path}"
        if key in keys:
            raise InventoryError(f"duplicate exclusion: {key}")
        keys.add(key)
    return keys


def _is_excluded(key: str, exclusions: set[str]) -> bool:
    """Return true only for an exact, content-bound exclusion."""

    return key in exclusions


def _derived_scan_roots(registry: dict) -> list[tuple[str, Path, Path]]:
    """Build the scan universe from source/build metadata, not owner roots."""

    universe = registry["scan"]["universe"]
    forest = universe["source_forest"]
    repository = forest["repository"]
    root = _repository_root(repository) / forest["root"]
    if not root.is_dir():
        raise InventoryError(f"production source forest is missing: {root}")
    roots: list[tuple[str, Path, Path]] = [(repository, root, _repository_root(repository))]
    for entry in universe["runtime_roots"]:
        if not isinstance(entry, dict):
            raise InventoryError("universe.runtime_roots entries must be objects")
        repo = entry.get("repository")
        relative = entry.get("path")
        if repo not in {"darling", "darling-workspace"} or not isinstance(relative, str):
            raise InventoryError("universe runtime root requires repository and path")
        absolute = _repository_root(repo) / relative
        if not absolute.is_dir():
            raise InventoryError(f"production runtime root is missing: {absolute}")
        roots.append((repo, absolute, _repository_root(repo)))
    return roots


def _candidate_files(
    root: Path,
    extensions: set[str],
    pattern: re.Pattern[str],
    *,
    engine: str = "auto",
) -> list[Path]:
    """Use rg as a bounded prefilter, with a deterministic pathlib fallback."""

    if engine not in {"auto", "rg", "python"}:
        raise InventoryError(f"unsupported candidate engine: {engine}")
    rg = shutil.which("rg")
    if engine != "python" and rg:
        args = [
            rg,
            "-l",
            "--no-messages",
            "--pcre2",
            "--hidden",
            "--no-ignore",
            "--glob",
            "!.git/**",
        ]
        if pattern.flags & re.IGNORECASE:
            args.append("--ignore-case")
        for extension in sorted(extensions):
            args.extend(["--glob", f"*{extension}"])
        args.extend(["-e", pattern.pattern, str(root)])
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode in (0, 1):
            return sorted({Path(line) for line in result.stdout.splitlines() if line})
        if engine == "rg":
            raise InventoryError(f"rg candidate scan failed: {result.stderr.strip()}")
    elif engine == "rg":
        raise InventoryError("rg candidate engine requested but rg is unavailable")
    candidates: list[Path] = []
    for absolute in root.rglob("*"):
        if (
            not absolute.is_file()
            or absolute.is_symlink()
            or absolute.suffix not in extensions
            or ".git" in absolute.parts
        ):
            continue
        try:
            source = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(source):
            candidates.append(absolute)
    return sorted(set(candidates))


def _namespace_mutation_pattern(registry: dict) -> re.Pattern[str]:
    """Compile the narrow, mutation-first shared-namespace grammar."""

    try:
        return re.compile(
            "(?:" + "|".join(registry["scan"]["namespace_mutation_tokens"]) + ")"
        )
    except (KeyError, TypeError, re.error) as error:
        raise InventoryError(f"invalid namespace mutation grammar: {error}") from error


def _discover_runtime_mutation_paths(
    registry: dict,
    *,
    extra_roots: list[tuple[str, Path]] | None = None,
) -> set[str]:
    """Discover translation units through mutation syntax, never path tokens.

    This is intentionally a bounded, source-only scan.  It is not a generic
    C parser: the registry names a narrow namespace-mutation grammar and file
    extensions, while the source/build closure anchors the universe.  Path
    strings, runtime-token spellings and capitalization do not decide whether
    a translation unit enters the candidate set.  Every discovered path must
    then be an owner or an explicit, reviewed fixture/read-only exclusion.
    """

    scan = registry["scan"]
    try:
        extensions = set(scan["translation_unit_extensions"])
        mutation_pattern = _namespace_mutation_pattern(registry)
    except (KeyError, TypeError, re.error) as error:
        raise InventoryError(f"invalid source scan rules: {error}") from error
    if not extensions or ".h" in extensions:
        raise InventoryError("source scan bounds are invalid")

    discovered: set[str] = set()
    roots = _derived_scan_roots(registry)

    for repository, absolute_root in extra_roots or []:
        if repository not in {"darling", "darling-workspace"} or not absolute_root.is_dir():
            raise InventoryError("extra production scan root is invalid")
        roots.append((repository, absolute_root, absolute_root.parent))

    for repository, absolute_root, relative_base in roots:
        for absolute in _candidate_files(absolute_root, extensions, mutation_pattern):
            path = str(absolute.relative_to(relative_base))
            discovered.add(f"{repository}:{path}")
    return discovered


def _candidate_engine_parity_contract(registry: dict) -> str:
    """Prove rg and the Python fallback select identical case variants."""

    if shutil.which("rg") is None:
        return "PYTHON_ONLY"
    pattern = _namespace_mutation_pattern(registry)
    extensions = set(registry["scan"]["translation_unit_extensions"])
    with tempfile.TemporaryDirectory(prefix="namespace-writer-parity-") as temporary:
        root = Path(temporary)
        fixtures = {
            "lower.c": "int f(void) { return unlink(options.pid_file); }\n",
            "upper.c": "int f(void) { return unlink(options.PID_FILE); }\n",
            "computed.c": (
                "int f(char *runtime_state) { return mkdir(runtime_state, 0700); }\n"
            ),
            "read_only.c": "int f(char *path) { return open(path, O_RDONLY); }\n",
        }
        for name, source in fixtures.items():
            (root / name).write_text(source, encoding="utf-8")
        # `rg` and pathlib must not diverge because a source happens to be
        # listed in a nested ignore file.
        (root / ".ignore").write_text("upper.c\n", encoding="utf-8")
        rg_candidates = {
            path.relative_to(root)
            for path in _candidate_files(root, extensions, pattern, engine="rg")
        }
        python_candidates = {
            path.relative_to(root)
            for path in _candidate_files(root, extensions, pattern, engine="python")
        }
        expected = {Path("lower.c"), Path("upper.c"), Path("computed.c")}
        if rg_candidates != python_candidates or rg_candidates != expected:
            raise InventoryError(
                "rg/Python candidate parity failed: "
                f"rg={sorted(map(str, rg_candidates))} "
                f"python={sorted(map(str, python_candidates))}"
            )

    # Exercise the reported production regression, not only synthetic case
    # folding.  Both engines must select the installed sshd translation unit.
    openssh_root = DARLING / "src/external/openssh/openssh"
    for engine in ("rg", "python"):
        candidates = _candidate_files(openssh_root, extensions, pattern, engine=engine)
        if openssh_root / "sshd.c" not in candidates:
            raise InventoryError(f"{engine} candidate scan missed OpenSSH sshd.c")
    return "RG_PYTHON"


def _validate_build_closure(registry: dict, owners: dict[str, dict[str, str]]) -> None:
    for entry in registry["scan"].get("build_closure", []):
        if not isinstance(entry, dict):
            raise InventoryError("build_closure entries must be objects")
        repository = entry.get("repository")
        cmake = entry.get("cmake")
        cmake_sha256 = entry.get("cmake_sha256")
        target = entry.get("target")
        sources = entry.get("sources")
        if (
            repository not in {"darling", "darling-workspace"}
            or not isinstance(cmake, str)
            or not isinstance(cmake_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", cmake_sha256)
            or not isinstance(target, str)
            or not target
            or not isinstance(sources, list)
            or not sources
        ):
            raise InventoryError("build_closure requires repository/cmake/target/sources")
        if "mutation_first" in entry and not isinstance(entry["mutation_first"], bool):
            raise InventoryError("build_closure mutation_first must be boolean")
        destination = entry.get("installed_destination")
        if destination is not None and (not isinstance(destination, str) or not destination.strip()):
            raise InventoryError("build_closure installed_destination must be a non-empty string")
        runtime_evidence = entry.get("runtime_evidence", [])
        if not isinstance(runtime_evidence, list) or not all(
            isinstance(evidence, dict)
            and set(evidence) == {"path", "sha256"}
            and isinstance(evidence["path"], str)
            and evidence["path"]
            and isinstance(evidence["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])
            for evidence in runtime_evidence
        ):
            raise InventoryError("build_closure runtime_evidence must be exact path/SHA records")
        cmake_path = _repository_root(repository) / cmake
        if not cmake_path.is_file():
            raise InventoryError(f"build closure CMake file missing: {cmake_path}")
        if _sha256_file(cmake_path) != cmake_sha256:
            raise InventoryError(f"build closure CMake SHA mismatch: {cmake_path}")
        cmake_text = cmake_path.read_text(encoding="utf-8", errors="replace")
        if target not in cmake_text:
            raise InventoryError(f"build closure target missing from CMake: {target}")
        if destination is not None and (
            "install" not in cmake_text or destination not in cmake_text
        ):
            raise InventoryError(f"build closure install destination missing from CMake: {target}")
        for source in sources:
            if not isinstance(source, str) or not source:
                raise InventoryError("build closure source must be a non-empty string")
            source_path = cmake_path.parent / source
            key = f"{repository}:{source_path.relative_to(_repository_root(repository))}"
            if not source_path.is_file() or key not in owners:
                raise InventoryError(f"build closure source is not inventoried: {key}")
            if Path(source).name not in cmake_text:
                raise InventoryError(f"build closure source missing from CMake: {source}")
            if entry.get("mutation_first") and not any(
                marker in source_path.read_text(encoding="utf-8", errors="replace")
                for marker in registry["scan"]["mutation_markers"]
            ):
                raise InventoryError(f"mutation-first build source has no mutation marker: {key}")
        for evidence in runtime_evidence:
            absolute = _repository_root(repository) / evidence["path"]
            if not absolute.is_file() or _sha256_file(absolute) != evidence["sha256"]:
                raise InventoryError(
                    f"build closure runtime evidence is missing or stale: {evidence['path']}"
                )


def _cmake_call_bodies(text: str, call: str) -> list[str]:
    """Return balanced bodies for the small CMake calls used by this audit."""

    bodies: list[str] = []
    # Production declarations are top-level CMake statements.  Anchoring at
    # the first non-whitespace token excludes commented-out calls without
    # damaging quoted ``#`` arguments or the parentheses they may contain.
    pattern = re.compile(rf"(?m)^[ \t]*{re.escape(call)}\s*\(")
    for match in pattern.finditer(text):
        opening = text.find("(", match.start())
        depth = 0
        for index in range(opening, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    bodies.append(text[opening + 1 : index])
                    break
        else:
            raise InventoryError(f"unterminated CMake {call}() call")
    return bodies


def _cmake_calls_in_order(text: str) -> list[tuple[str, str]]:
    """Return non-comment CMake calls in source order with balanced bodies."""

    calls: list[tuple[str, str]] = []
    pattern = re.compile(r"(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    cursor = 0
    while match := pattern.search(text, cursor):
        opening = text.find("(", match.start())
        depth = 0
        quoted = False
        escaped = False
        comment = False
        for index in range(opening, len(text)):
            character = text[index]
            if comment:
                if character == "\n":
                    comment = False
                continue
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
                continue
            if character == "#":
                comment = True
                continue
            if character == '"':
                quoted = True
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    calls.append(
                        (match.group(1).lower(), text[opening + 1 : index])
                    )
                    cursor = index + 1
                    break
        else:
            raise InventoryError(
                f"unterminated CMake {match.group(1)}() call"
            )
    return calls


def _top_level_cmake_calls(text: str, selected: set[str]) -> dict[str, list[str]]:
    """Collect selected calls only when their execution is unconditional."""

    found = {command: [] for command in selected}
    openers = {
        "if": "endif",
        "foreach": "endforeach",
        "while": "endwhile",
        "function": "endfunction",
        "macro": "endmacro",
        "block": "endblock",
    }
    closers = {closer: opener for opener, closer in openers.items()}
    stack: list[str] = []
    for command, body in _cmake_calls_in_order(text):
        if command in openers:
            stack.append(command)
            continue
        if command in closers:
            if not stack or stack[-1] != closers[command]:
                raise InventoryError("CMake control flow is unbalanced")
            stack.pop()
            continue
        if command in {"else", "elseif"}:
            if not stack or stack[-1] != "if":
                raise InventoryError("CMake conditional branch is unbalanced")
            continue
        if command in selected:
            if stack:
                raise InventoryError(
                    f"CMake {command}() uses unsupported conditional context"
                )
            found[command].append(body)
    if stack:
        raise InventoryError("CMake control flow is unterminated")
    return found


def _cmake_tokens(body: str) -> list[str]:
    return re.findall(r"[^\s()]+", re.sub(r"(?m)#.*$", "", body))


def _cmake_variables(text: str, call: str) -> dict[str, list[str]]:
    variables: dict[str, list[str]] = {}
    for assignment in _cmake_call_bodies(text, call):
        tokens = _cmake_tokens(assignment)
        if tokens:
            variables[tokens[0]] = tokens[1:]
    return variables


def _expand_cmake_tokens(tokens: list[str], variables: dict[str, list[str]]) -> list[str]:
    expanded: list[str] = []
    pending = list(tokens)
    expansions = 0
    while pending:
        token = pending.pop(0)
        variable = re.fullmatch(r"\$\{([^}]+)\}", token)
        if variable and variable.group(1) in variables:
            expansions += 1
            if expansions > 256:
                raise InventoryError("CMake variable expansion budget exceeded")
            pending[0:0] = variables[variable.group(1)]
            continue
        expanded.append(token.strip('"'))
    return expanded


def _service_target_policy(registry: dict) -> tuple[dict, set[str]]:
    policy = registry["scan"].get("installed_service_target_policy")
    if not isinstance(policy, dict) or set(policy) != {
        "plist_destination",
        "executable_call",
        "variable_call",
        "output_name_property",
        "object_library_calls",
        "target_sources_call",
        "target_expansion_budget",
        "source_token_budget",
        "mig_declared_source_inputs",
        "mig_output_proof",
        "generated_source_bindings",
        "required_classification",
        "rule",
        "required_mutation_sources",
        "excluded_test_plists",
    }:
        raise InventoryError("installed service target policy is missing or malformed")
    if (
        policy.get("plist_destination") != "LaunchDaemons"
        or policy.get("executable_call") != "add_darling_executable"
        or policy.get("variable_call") != "set"
        or policy.get("output_name_property") != "OUTPUT_NAME"
        or policy.get("object_library_calls")
        != ["add_darling_object_library", "add_library"]
        or policy.get("target_sources_call") != "target_sources"
        or not isinstance(policy.get("target_expansion_budget"), int)
        or not 1 <= policy["target_expansion_budget"] <= 256
        or not isinstance(policy.get("source_token_budget"), int)
        or not 1 <= policy["source_token_budget"] <= 16384
        or not isinstance(policy.get("generated_source_bindings"), list)
        or not policy["generated_source_bindings"]
        or policy.get("required_classification") != "typed-writer"
        or not isinstance(policy.get("rule"), str)
        or not policy["rule"].strip()
        or not isinstance(policy.get("required_mutation_sources"), list)
        or not policy["required_mutation_sources"]
        or not isinstance(policy.get("excluded_test_plists"), list)
    ):
        raise InventoryError("installed service target policy weakens the target-aware gate")
    _validate_mig_declared_inputs(policy)
    _validate_mig_output_proof_shape(policy)
    excluded: set[str] = set()
    for entry in policy["excluded_test_plists"]:
        if not isinstance(entry, dict) or set(entry) != {
            "repository",
            "path",
            "sha256",
            "reason",
        }:
            raise InventoryError("service test exclusion must have exact path/SHA/reason fields")
        repository = entry.get("repository")
        relative = entry.get("path")
        digest = entry.get("sha256")
        reason = entry.get("reason")
        if (
            repository != "darling"
            or not isinstance(relative, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise InventoryError("service test exclusion is malformed")
        path = DARLING / relative
        if (
            not path.is_file()
            or path.is_symlink()
            or _sha256_file(path) != digest
            or not any(part.lower() in {"test", "tests"} for part in path.parts)
        ):
            raise InventoryError(f"service test exclusion is stale or non-test: {relative}")
        key = f"darling:{relative}"
        if key in excluded:
            raise InventoryError(f"duplicate service test exclusion: {key}")
        excluded.add(key)
    _generated_source_bindings(policy)
    return policy, excluded


def _mig_generator_declared_inputs() -> set[str]:
    """Derive bounded static build-mig inputs; output replay is authoritative."""

    cmake_relative = "src/external/bootstrap_cmds/CMakeLists.txt"
    cmake = DARLING / cmake_relative
    text = cmake.read_text(encoding="utf-8", errors="replace")
    calls = _top_level_cmake_calls(
        text,
        {
            "set",
            "bison_target",
            "flex_target",
            "add_executable",
            "add_dependencies",
            "add_custom_command",
        },
    )
    source_assignments = [
        _cmake_tokens(body)[1:]
        for body in calls["set"]
        if _cmake_tokens(body) and _cmake_tokens(body)[0] == "mig_sources"
    ]
    if len(source_assignments) != 1 or not source_assignments[0]:
        raise InventoryError("bootstrap MIG source list is missing")
    mig_sources = source_assignments[0]

    bison_calls = calls["bison_target"]
    flex_calls = calls["flex_target"]
    if len(bison_calls) != 1 or len(flex_calls) != 1:
        raise InventoryError("bootstrap MIG lexer/parser declaration is ambiguous")
    bison_tokens = _cmake_tokens(bison_calls[0])
    flex_tokens = _cmake_tokens(flex_calls[0])
    if len(bison_tokens) < 3 or len(flex_tokens) < 3:
        raise InventoryError("bootstrap MIG lexer/parser declaration is malformed")
    generated_tokens = {
        f"${{BISON_{bison_tokens[0]}_OUTPUTS}}",
        f"${{FLEX_{flex_tokens[0]}_OUTPUTS}}",
    }
    if not generated_tokens.issubset(set(mig_sources)):
        raise InventoryError("migcom target omits declared lexer/parser output")

    executable_calls = calls["add_executable"]
    if not any(
        _cmake_tokens(body) == ["migcom", "${mig_sources}"]
        for body in executable_calls
    ):
        raise InventoryError("bootstrap migcom target is not bound to mig_sources")
    dependency_calls = calls["add_dependencies"]
    if not any(
        _cmake_tokens(body) == ["migexe", "migcom"] for body in dependency_calls
    ):
        raise InventoryError("bootstrap build-mig target is not bound to migcom")

    custom_commands = calls["add_custom_command"]
    required_literals = {
        "${CMAKE_CURRENT_SOURCE_DIR}/migcom.tproj/mig.sh",
        "${CMAKE_CURRENT_SOURCE_DIR}/darling/src/mig.awk",
        "migcom=${CMAKE_CURRENT_BINARY_DIR}/migcom",
        "${CMAKE_BINARY_DIR}/build-mig",
    }
    matching_commands = [
        body
        for body in custom_commands
        if required_literals.issubset(
            {token.strip('"') for token in _cmake_tokens(body)}
        )
    ]
    if len(matching_commands) != 1:
        raise InventoryError("bootstrap build-mig command relation is ambiguous")

    declared_inputs = {
        "cmake/mig.cmake",
        cmake_relative,
        "src/external/bootstrap_cmds/migcom.tproj/mig.sh",
        "src/external/bootstrap_cmds/darling/src/mig.awk",
        f"src/external/bootstrap_cmds/{bison_tokens[1]}",
        f"src/external/bootstrap_cmds/{flex_tokens[1]}",
    }
    source_root = cmake.parent
    source_paths: list[Path] = []
    for token in mig_sources:
        if token in generated_tokens:
            continue
        if "${" in token or "$<" in token:
            raise InventoryError(f"bootstrap MIG source is unresolved: {token}")
        source = source_root / token
        if not source.is_file():
            raise InventoryError(f"bootstrap MIG source is missing: {token}")
        source_paths.append(source)
        declared_inputs.add(source.relative_to(DARLING).as_posix())

    include_root = source_root / "migcom.tproj"
    pending = source_paths + [
        DARLING / path
        for path in declared_inputs
        if Path(path).suffix in {".l", ".y"}
    ]
    visited: set[Path] = set()
    while pending:
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        body = source.read_text(encoding="utf-8", errors="replace")
        for included in re.findall(
            r'(?m)^[ \t]*#[ \t]*include[ \t]+"([^"\n]+)"', body
        ):
            candidates = (source.parent / included, include_root / included)
            local = next((candidate for candidate in candidates if candidate.is_file()), None)
            if local is None:
                # parser.h is a declared Bison output, not a source input.
                if included == "parser.h":
                    continue
                raise InventoryError(
                    "bootstrap MIG quoted include is outside the bounded "
                    f"declared-input inventory: {included}"
                )
            relative = local.relative_to(DARLING).as_posix()
            if relative not in declared_inputs:
                declared_inputs.add(relative)
                pending.append(local)
    return declared_inputs


def _validate_mig_declared_inputs(policy: dict) -> None:
    """Require SHA bindings for CMake-declared generator inputs."""

    records = policy.get("mig_declared_source_inputs")
    if not isinstance(records, list) or not records:
        raise InventoryError("MIG declared source inputs are missing")
    bound: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise InventoryError("MIG declared source input is malformed")
        path = record.get("path")
        digest = record.get("sha256")
        if (
            not isinstance(path, str)
            or path in bound
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise InventoryError("MIG declared source input is malformed or duplicated")
        source = DARLING / path
        if not source.is_file() or source.is_symlink() or _sha256_file(source) != digest:
            raise InventoryError(f"MIG declared source input is stale: {path}")
        bound[path] = digest
    expected = _mig_generator_declared_inputs()
    if set(bound) != expected:
        raise InventoryError(
            "MIG declared inputs differ from the CMake-derived source set"
        )


def _mig_replay_environment() -> dict[str, str]:
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "SOURCE_DATE_EPOCH": "0",
        "TZ": "UTC",
    }


def _mig_full_configure_arguments() -> list[str]:
    return [
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_C_COMPILER=/usr/bin/clang",
        "-DCMAKE_CXX_COMPILER=/usr/bin/clang++",
        "-DDARLING_NO_CCACHE=ON",
        "-DDARLING_SKIP_DRIFT_GATE=ON",
        "-DDARLING_EUNION=ON",
        "-DDARLING_RING_TRANSPORT=ON",
    ]


def _mig_tool_specs() -> dict[str, tuple[str, ...]]:
    return {
        "bash": ("bash", "--version"),
        "bison": ("bison", "--version"),
        "clang": ("clang", "--version"),
        "clang++": ("clang++", "--version"),
        "cmake": ("cmake", "--version"),
        "flex": ("flex", "--version"),
        "ninja": ("ninja", "--version"),
        "awk": ("awk", "-W", "version"),
    }


def _bounded_capture(
    args: list[str], env: dict[str, str], *, timeout: int = 15, limit: int = 65536
) -> bytes:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise InventoryError(f"bounded MIG command timed out: {args[0]}") from error
    if len(result.stdout) > limit:
        raise InventoryError(f"bounded MIG command output exceeded limit: {args[0]}")
    if result.returncode != 0:
        output = result.stdout.decode("utf-8", errors="replace")
        raise InventoryError(f"MIG command failed: {args[0]}: {output[-4096:]}")
    return result.stdout


def _mig_tool_observations(env: dict[str, str]) -> list[dict[str, str]]:
    observations: list[dict[str, str]] = []
    for name, version_argv in _mig_tool_specs().items():
        executable = shutil.which(version_argv[0])
        if executable is None:
            raise InventoryError(f"MIG replay tool is unavailable: {name}")
        output = _bounded_capture(list(version_argv), env).decode(
            "utf-8", errors="strict"
        )
        version = next((line.strip() for line in output.splitlines() if line.strip()), "")
        if not version:
            raise InventoryError(f"MIG replay tool has no version evidence: {name}")
        observations.append(
            {
                "name": name,
                "path": executable,
                "sha256": _sha256_file(Path(executable)),
                "version": version,
            }
        )
    return observations


def _validate_mig_output_proof_shape(policy: dict) -> None:
    proof = policy.get("mig_output_proof")
    if not isinstance(proof, dict) or set(proof) != {
        "schema_version",
        "environment",
        "cmake_arguments",
        "normalization",
        "tools",
        "outputs",
    }:
        raise InventoryError("MIG output proof is missing or malformed")
    if (
        proof.get("schema_version") != 1
        or proof.get("environment") != _mig_replay_environment()
        or proof.get("cmake_arguments") != _mig_full_configure_arguments()
        or proof.get("normalization")
        != "replace-mig-banner-time-with-${SOURCE_DATE_EPOCH}"
    ):
        raise InventoryError("MIG output proof reproduction metadata is not exact")
    tools = proof.get("tools")
    if not isinstance(tools, list) or not tools:
        raise InventoryError("MIG output proof tool provenance is missing")
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or set(tool) != {"name", "path", "sha256", "version"}
            or not isinstance(tool.get("name"), str)
            or not isinstance(tool.get("path"), str)
            or not isinstance(tool.get("version"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", tool.get("sha256", ""))
        ):
            raise InventoryError("MIG output proof tool provenance is malformed")
    tool_names = [tool["name"] for tool in tools]
    if tool_names != list(_mig_tool_specs()) or len(set(tool_names)) != len(tool_names):
        raise InventoryError("MIG output proof tool inventory is not exact")
    outputs = proof.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise InventoryError("MIG output proof outputs are missing")
    for output in outputs:
        if (
            not isinstance(output, dict)
            or set(output)
            != {"cmake", "source", "sha256", "size", "mutation_operators"}
            or not isinstance(output.get("cmake"), str)
            or not isinstance(output.get("source"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", output.get("sha256", ""))
            or not isinstance(output.get("size"), int)
            or not 0 < output["size"] <= 4 * 1024 * 1024
            or not isinstance(output.get("mutation_operators"), list)
            or not all(
                isinstance(operator, str) and operator in AUDIT_MUTATION_OPERATOR_PATTERNS
                for operator in output["mutation_operators"]
            )
        ):
            raise InventoryError("MIG output proof output record is malformed")
    output_keys = [(output["cmake"], output["source"]) for output in outputs]
    if len(set(output_keys)) != len(output_keys):
        raise InventoryError("MIG output proof contains duplicate outputs")


def _run_logged_mig_command(
    args: list[str], env: dict[str, str], log: Path, *, timeout: int
) -> None:
    with log.open("wb") as stream:
        try:
            result = subprocess.run(
                args,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise InventoryError(f"MIG replay command timed out: {args[0]}") from error
    if log.stat().st_size > 1024 * 1024:
        raise InventoryError(f"MIG replay log exceeded limit: {args[0]}")
    if result.returncode != 0:
        output = log.read_bytes()[-16384:].decode("utf-8", errors="replace")
        raise InventoryError(f"MIG replay command failed: {args[0]}: {output}")


def _binding_output_path(build: Path, binding: dict) -> tuple[str, Path]:
    source = binding["source"]
    binary_prefix = "${CMAKE_CURRENT_BINARY_DIR}/"
    relative_source = source[len(binary_prefix) :] if source.startswith(binary_prefix) else source
    relative = Path(binding["cmake"]).parent / relative_source
    if relative.is_absolute() or ".." in relative.parts:
        raise InventoryError("MIG output escapes the configured build root")
    output = (build / relative).resolve()
    if build.resolve() not in output.parents:
        raise InventoryError("MIG output escapes the configured build root")
    return relative.as_posix(), output


def _canonical_mig_output(raw: bytes) -> bytes:
    banner = re.compile(
        rb"(?m)^(?P<prefix> \* stub generated )"
        rb"(?P<timestamp>(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
        rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
        rb"(?: [1-9]|[12][0-9]|3[01]) "
        rb"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-6][0-9] [0-9]{4})"
        rb"(?P<ending>\r?)$"
    )
    matches = list(banner.finditer(raw))
    if len(matches) != 1:
        raise InventoryError("materialized MIG output lacks one exact timestamp banner")
    match = matches[0]
    return (
        raw[: match.start("timestamp")]
        + b"${SOURCE_DATE_EPOCH}"
        + raw[match.end("timestamp") :]
    )


def _validated_mig_output(raw: bytes) -> tuple[bytes, list[str]]:
    raw_text = raw.decode("utf-8", errors="strict")
    raw_operators = _audit_mutation_operators(raw_text)
    if raw_operators:
        raise InventoryError(
            f"raw generated MIG output contains namespace mutations: {raw_operators}"
        )
    canonical = _canonical_mig_output(raw)
    canonical_text = canonical.decode("utf-8", errors="strict")
    canonical_operators = _audit_mutation_operators(canonical_text)
    if canonical_operators:
        raise InventoryError(
            "canonical generated MIG output contains namespace mutations: "
            f"{canonical_operators}"
        )
    return canonical, canonical_operators


def _extract_mig_command(
    full_build: Path,
    target: str,
    generator: Path,
    env: dict[str, str],
) -> list[str]:
    command_output = _bounded_capture(
        ["ninja", "-C", str(full_build), "-t", "commands", "-s", target],
        env,
        timeout=15,
        limit=262144,
    ).decode("utf-8", errors="strict")
    lines = [line for line in command_output.splitlines() if "/build-mig" in line]
    if len(lines) != 1:
        raise InventoryError(f"configured MIG output command is ambiguous: {target}")
    tokens = shlex.split(lines[0])
    indices = [index for index, token in enumerate(tokens) if Path(token).name == "build-mig"]
    if len(indices) != 1:
        raise InventoryError(f"configured MIG executable is ambiguous: {target}")
    start = indices[0]
    stop = next(
        (index for index in range(start + 1, len(tokens)) if tokens[index] in {";", "&&"}),
        len(tokens),
    )
    args = tokens[start:stop]
    args[0] = str(generator)
    if "-arch" not in args or "-target" not in args:
        raise InventoryError(f"configured MIG command lacks target identity: {target}")
    return args


def _materialize_mig_output_proof(
    bindings: dict[tuple[str, str], dict]
) -> tuple[list[dict[str, str]], list[dict]]:
    replay_env = os.environ.copy()
    for key in (
        "CC",
        "CXX",
        "CFLAGS",
        "CXXFLAGS",
        "LDFLAGS",
        "CMAKE_GENERATOR",
        "DESTDIR",
    ):
        replay_env.pop(key, None)
    replay_env.update(_mig_replay_environment())
    tools = _mig_tool_observations(replay_env)

    with tempfile.TemporaryDirectory(prefix="namespace-mig-output-proof-") as temporary:
        task_root = Path(temporary)
        generator_build = task_root / "generator-build"
        full_build = task_root / "full-build"
        source = DARLING.resolve()
        host_flags = " ".join(
            (
                "-DDARLING",
                f"-I{source / 'src/include'}",
                f"-I{source / 'basic-headers'}",
                "-Wno-nullability-completeness",
                "-Wno-deprecated-declarations",
                "-Wno-availability",
                "-Wno-expansion-to-defined",
                "-Wno-elaborated-enum-base",
                "-Wno-undef-prefix",
            )
        )
        _run_logged_mig_command(
            [
                "cmake",
                "-S",
                str(source / "src/external/bootstrap_cmds"),
                "-B",
                str(generator_build),
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_C_COMPILER=/usr/bin/clang",
                f"-DCMAKE_C_FLAGS={host_flags}",
            ],
            replay_env,
            task_root / "generator-configure.log",
            timeout=30,
        )
        _run_logged_mig_command(
            [
                "cmake",
                "--build",
                str(generator_build),
                "--target",
                "migexe",
                "-j2",
            ],
            replay_env,
            task_root / "generator-build.log",
            timeout=60,
        )
        generator = generator_build / "build-mig"
        if not generator.is_file() or not os.access(generator, os.X_OK):
            raise InventoryError("clean MIG generator build did not produce build-mig")

        _run_logged_mig_command(
            [
                "cmake",
                "-S",
                str(source),
                "-B",
                str(full_build),
                *_mig_full_configure_arguments(),
            ],
            replay_env,
            task_root / "full-configure.log",
            timeout=60,
        )

        output_records: list[dict] = []
        for key, binding in sorted(bindings.items()):
            target, output = _binding_output_path(full_build, binding)
            args = _extract_mig_command(full_build, target, generator, replay_env)
            for flag in ("-user", "-header", "-server", "-sheader", "-xtracemig"):
                try:
                    argument = Path(args[args.index(flag) + 1])
                except (ValueError, IndexError) as error:
                    raise InventoryError(
                        f"configured MIG command lacks {flag}: {target}"
                    ) from error
                argument.parent.mkdir(parents=True, exist_ok=True)
            _run_logged_mig_command(
                args,
                replay_env,
                task_root / f"generate-{len(output_records)}.log",
                timeout=30,
            )
            if not output.is_file() or output.is_symlink():
                raise InventoryError(f"configured MIG output was not materialized: {target}")
            size = output.stat().st_size
            if not 0 < size <= 4 * 1024 * 1024:
                raise InventoryError(f"configured MIG output exceeds size budget: {target}")
            raw = output.read_bytes()
            if len(raw) != size:
                raise InventoryError(f"configured MIG output changed during read: {target}")
            canonical, operators = _validated_mig_output(raw)
            output_records.append(
                {
                    "cmake": key[0],
                    "source": key[1],
                    "sha256": hashlib.sha256(canonical).hexdigest(),
                    "size": size,
                    "mutation_operators": operators,
                }
            )
    return tools, output_records


def _validate_materialized_mig_output_proof(
    policy: dict,
    bindings: dict[tuple[str, str], dict],
    tools: list[dict[str, str]],
    outputs: list[dict],
) -> None:
    _validate_mig_output_proof_shape(policy)
    proof = policy["mig_output_proof"]
    if proof["tools"] != tools:
        raise InventoryError("MIG output proof tool provenance mismatch")
    expected_keys = set(bindings)
    actual_keys = {(entry["cmake"], entry["source"]) for entry in outputs}
    declared_keys = {
        (entry["cmake"], entry["source"]) for entry in proof["outputs"]
    }
    if actual_keys != expected_keys or declared_keys != expected_keys:
        raise InventoryError("MIG output proof does not cover every generated binding")
    if proof["outputs"] != outputs:
        raise InventoryError("MIG materialized output proof mismatch")


def _mig_output_negative_contract(
    policy: dict,
    bindings: dict[tuple[str, str], dict],
    tools: list[dict[str, str]],
    outputs: list[dict],
) -> None:
    stale_output = json.loads(json.dumps(policy))
    stale_output["mig_output_proof"]["outputs"][0]["sha256"] = "0" * 64
    try:
        _validate_materialized_mig_output_proof(
            stale_output, bindings, tools, outputs
        )
    except InventoryError as error:
        assert "materialized output proof mismatch" in str(error)
    else:
        raise InventoryError("stale materialized MIG output unexpectedly accepted")

    stale_tool = json.loads(json.dumps(policy))
    stale_tool["mig_output_proof"]["tools"][0]["sha256"] = "0" * 64
    try:
        _validate_materialized_mig_output_proof(stale_tool, bindings, tools, outputs)
    except InventoryError as error:
        assert "tool provenance mismatch" in str(error)
    else:
        raise InventoryError("stale MIG replay tool unexpectedly accepted")

    missing_output = json.loads(json.dumps(policy))
    missing_output["mig_output_proof"]["outputs"].pop()
    try:
        _validate_materialized_mig_output_proof(
            missing_output, bindings, tools, outputs
        )
    except InventoryError as error:
        assert "does not cover every generated binding" in str(error)
    else:
        raise InventoryError("incomplete materialized MIG output set unexpectedly accepted")

    forged_mutation_scan = json.loads(json.dumps(policy))
    forged_mutation_scan["mig_output_proof"]["outputs"][0][
        "mutation_operators"
    ] = ["remove"]
    try:
        _validate_materialized_mig_output_proof(
            forged_mutation_scan, bindings, tools, outputs
        )
    except InventoryError as error:
        assert "materialized output proof mismatch" in str(error)
    else:
        raise InventoryError("forged MIG mutation scan unexpectedly accepted")

    banner_payload = (
        b"/*\n"
        b" * stub generated Sun Aug  9 14:36:53 2026 */ unlink(\"victim\"); /*\n"
        b" */\n"
    )
    try:
        _validated_mig_output(banner_payload)
    except InventoryError as error:
        assert "raw generated MIG output contains namespace mutations" in str(error)
    else:
        raise InventoryError("MIG banner-line mutation payload unexpectedly accepted")

    malformed_banner = b"/*\n * stub generated 2026-08-09 14:36:53\n */\n"
    try:
        _validated_mig_output(malformed_banner)
    except InventoryError as error:
        assert "lacks one exact timestamp banner" in str(error)
    else:
        raise InventoryError("non-ctime MIG banner unexpectedly accepted")


def _generated_source_bindings(policy: dict) -> dict[tuple[str, str], dict]:
    """Validate exact generator inputs for source tokens absent before configure."""

    _validate_mig_declared_inputs(policy)
    bindings: dict[tuple[str, str], dict] = {}
    for entry in policy.get("generated_source_bindings", []):
        if not isinstance(entry, dict) or set(entry) != {
            "cmake",
            "cmake_sha256",
            "source",
            "classification",
            "generator",
            "inputs",
            "reason",
        }:
            raise InventoryError("generated service source binding has malformed fields")
        cmake_relative = entry.get("cmake")
        source = entry.get("source")
        cmake_digest = entry.get("cmake_sha256")
        reason = entry.get("reason")
        if (
            not isinstance(cmake_relative, str)
            or not isinstance(source, str)
            or not source
            or entry.get("classification") != "sha-bound-generated-source"
            or not isinstance(cmake_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", cmake_digest)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise InventoryError("generated service source binding is malformed")
        cmake = DARLING / cmake_relative
        if not cmake.is_file() or _sha256_file(cmake) != cmake_digest:
            raise InventoryError(
                f"generated service source CMake anchor is stale: {cmake_relative}"
            )
        generator = entry.get("generator")
        if not isinstance(generator, dict) or set(generator) != {"path", "sha256"}:
            raise InventoryError("generated service source generator is malformed")
        if generator.get("path") != "cmake/mig.cmake":
            raise InventoryError("generated service source does not use the MIG generator")
        generator_path = DARLING / generator["path"]
        if (
            not generator_path.is_file()
            or not re.fullmatch(r"[0-9a-f]{64}", generator.get("sha256", ""))
            or _sha256_file(generator_path) != generator["sha256"]
        ):
            raise InventoryError(
                f"generated service source generator is stale: {generator.get('path')}"
            )
        inputs = entry.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != 1:
            raise InventoryError("generated service source inputs are missing")
        seen_inputs: set[str] = set()
        for generator_input in inputs:
            if not isinstance(generator_input, dict) or set(generator_input) != {
                "path",
                "sha256",
            }:
                raise InventoryError("generated service source input is malformed")
            input_relative = generator_input.get("path")
            input_digest = generator_input.get("sha256")
            if (
                not isinstance(input_relative, str)
                or input_relative in seen_inputs
                or not isinstance(input_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", input_digest)
            ):
                raise InventoryError("generated service source input is malformed or duplicated")
            input_path = DARLING / input_relative
            if not input_path.is_file() or _sha256_file(input_path) != input_digest:
                raise InventoryError(
                    f"generated service source input is stale: {input_relative}"
                )
            seen_inputs.add(input_relative)
        _validate_mig_generated_source(cmake, source, inputs[0]["path"])
        key = (cmake_relative, source)
        if key in bindings:
            raise InventoryError(f"duplicate generated service source binding: {key}")
        bindings[key] = entry
    return bindings


def _validate_mig_generated_source(
    declaring_cmake: Path,
    source_token: str,
    input_relative: str,
    repository_root: Path = DARLING,
) -> None:
    """Evaluate ordered, unconditional mig() calls and their suffix state."""

    binary_prefix = "${CMAKE_CURRENT_BINARY_DIR}/"
    if source_token.startswith(binary_prefix):
        normalized_output = source_token[len(binary_prefix) :]
    elif "${" not in source_token and "$<" not in source_token:
        normalized_output = source_token
    else:
        raise InventoryError("generated MIG source token is not a binary-relative output")
    text = declaring_cmake.read_text(encoding="utf-8", errors="replace")
    suffixes = {
        "MIG_USER_SOURCE_SUFFIX": "User.c",
        "MIG_SERVER_SOURCE_SUFFIX": "Server.c",
    }
    control_openers = {
        "if": "endif",
        "foreach": "endforeach",
        "while": "endwhile",
        "function": "endfunction",
        "macro": "endmacro",
        "block": "endblock",
    }
    control_closers = {closer: opener for opener, closer in control_openers.items()}
    control_stack: list[str] = []
    unsupported_output_state = {
        "MIG_ARCH",
        "MIG_MULTIARCH",
        "MIG_MULTIARCH_NO_SUFFIX",
    }
    expected_input = (repository_root / input_relative).resolve()
    matching_calls = 0
    for command, body in _cmake_calls_in_order(text):
        if unsupported_output_state.intersection(_cmake_tokens(body)):
            raise InventoryError("declaring MIG call uses unsupported architecture suffix state")
        if command in control_openers:
            control_stack.append(command)
            continue
        if command in control_closers:
            if not control_stack or control_stack[-1] != control_closers[command]:
                raise InventoryError("declaring CMake control flow is unbalanced")
            control_stack.pop()
            continue
        if command in {"else", "elseif"}:
            if not control_stack or control_stack[-1] != "if":
                raise InventoryError("declaring CMake conditional branch is unbalanced")
            continue
        if command in {"set", "unset"}:
            tokens = _cmake_tokens(body)
            if not tokens or tokens[0] not in suffixes:
                continue
            if control_stack:
                raise InventoryError(
                    "MIG suffix assignment uses unsupported conditional context"
                )
            if command == "unset":
                if len(tokens) != 1:
                    raise InventoryError("generated MIG suffix unset is not exact")
                suffixes[tokens[0]] = (
                    "User.c"
                    if tokens[0] == "MIG_USER_SOURCE_SUFFIX"
                    else "Server.c"
                )
                continue
            if (
                len(tokens) != 2
                or "${" in tokens[1]
                or "$<" in tokens[1]
                or not tokens[1].strip('"')
            ):
                raise InventoryError(
                    f"generated MIG suffix is not an exact scalar: {tokens[0]}"
                )
            suffixes[tokens[0]] = tokens[1].strip('"')
            continue
        if command != "mig":
            continue
        if control_stack:
            raise InventoryError("declaring mig() uses unsupported conditional context")
        tokens = _cmake_tokens(body)
        if not tokens:
            continue
        definition = tokens[0].strip('"')
        if "${" in definition or "$<" in definition or not definition.endswith(".defs"):
            raise InventoryError("declaring MIG call has an unresolved definition input")
        definition_path = (declaring_cmake.parent / definition).resolve()
        if definition_path != expected_input:
            continue
        relative_name = definition[: -len(".defs")]
        outputs = {
            f"{relative_name}{suffixes['MIG_USER_SOURCE_SUFFIX']}",
            f"{relative_name}{suffixes['MIG_SERVER_SOURCE_SUFFIX']}",
        }
        if normalized_output in outputs:
            matching_calls += 1
    if control_stack:
        raise InventoryError("declaring CMake control flow is unterminated")
    if matching_calls != 1:
        try:
            declaration = declaring_cmake.relative_to(repository_root)
        except ValueError:
            declaration = declaring_cmake
        raise InventoryError(
            "generated service source is not produced by one exact declaring mig(input): "
            f"{declaration}:{source_token}"
        )


def _installed_launchdaemon_plists(policy: dict) -> dict[str, Path]:
    """Resolve exact source plists from production CMake install() calls."""

    installed: dict[str, Path] = {}
    for cmake in (DARLING / "src").rglob("CMakeLists.txt"):
        text = cmake.read_text(encoding="utf-8", errors="replace")
        variables = _cmake_variables(text, policy["variable_call"])
        for body in _cmake_call_bodies(text, "install"):
            tokens = _expand_cmake_tokens(_cmake_tokens(body), variables)
            if not tokens or policy["plist_destination"] not in body:
                continue
            try:
                destination = tokens.index("DESTINATION")
            except ValueError as error:
                raise InventoryError(
                    f"LaunchDaemon install lacks DESTINATION: {cmake.relative_to(DARLING)}"
                ) from error
            mode = tokens[0]
            candidates: list[Path] = []
            if mode == "FILES":
                source_tokens = tokens[1:destination]
                if any("${" in token or "$<" in token for token in source_tokens):
                    raise InventoryError(
                        "LaunchDaemon FILES install has unresolved source: "
                        f"{cmake.relative_to(DARLING)}"
                    )
                candidates = [cmake.parent / token for token in source_tokens]
            elif mode == "DIRECTORY":
                for token in tokens[1:destination]:
                    if "${" in token or "$<" in token:
                        raise InventoryError(
                            "LaunchDaemon DIRECTORY install has unresolved source: "
                            f"{cmake.relative_to(DARLING)}"
                        )
                    directory = cmake.parent / token.rstrip("/")
                    if not directory.is_dir():
                        raise InventoryError(
                            "LaunchDaemon DIRECTORY install source is missing: "
                            f"{directory.relative_to(DARLING)}"
                        )
                    candidates.extend(directory.rglob("*.plist"))
            else:
                continue
            for plist in candidates:
                if plist.suffix != ".plist":
                    continue
                if not plist.is_file() or plist.is_symlink():
                    raise InventoryError(
                        "installed LaunchDaemon plist is missing or non-regular: "
                        f"{plist.relative_to(DARLING)}"
                    )
                relative = str(plist.relative_to(DARLING))
                key = f"darling:{relative}"
                previous = installed.setdefault(key, cmake)
                if previous != cmake:
                    raise InventoryError(f"LaunchDaemon plist has multiple install owners: {key}")
    return installed


def _darling_executable_targets(policy: dict) -> dict[str, list[tuple[Path, str]]]:
    targets: dict[str, list[tuple[Path, str]]] = {}
    for cmake in (DARLING / "src").rglob("CMakeLists.txt"):
        text = cmake.read_text(encoding="utf-8", errors="replace")
        for body in _cmake_call_bodies(text, policy["executable_call"]):
            tokens = _cmake_tokens(body)
            if not tokens:
                continue
            target = tokens[0]
            names = {target}
            for properties in _cmake_call_bodies(text, "set_target_properties"):
                property_tokens = _cmake_tokens(properties)
                if not property_tokens or property_tokens[0] != target:
                    continue
                try:
                    output_index = property_tokens.index(policy["output_name_property"])
                except ValueError:
                    continue
                if output_index + 1 < len(property_tokens):
                    names.add(property_tokens[output_index + 1].strip('"'))
            for name in names:
                candidate = (cmake, body)
                if candidate not in targets.setdefault(name, []):
                    targets[name].append(candidate)
    return targets


def _cmake_target_graph(
    policy: dict, cmake_root: Path | None = None
) -> CMakeTargetGraph:
    """Index object definitions and target_sources additions by target."""

    definitions: dict[str, list[CMakeSourceGroup]] = {}
    additions: dict[str, list[CMakeSourceGroup]] = {}
    root = cmake_root or DARLING / "src"
    for cmake in root.rglob("CMakeLists.txt"):
        text = cmake.read_text(encoding="utf-8", errors="replace")
        variables = _cmake_variables(text, policy["variable_call"])
        for call in policy["object_library_calls"]:
            for body in _cmake_call_bodies(text, call):
                tokens = _cmake_tokens(body)
                if not tokens:
                    continue
                if call == "add_library" and "OBJECT" not in tokens[1:]:
                    continue
                definitions.setdefault(tokens[0], []).append(
                    (cmake, tokens[1:], variables)
                )
        for body in _cmake_call_bodies(text, policy["target_sources_call"]):
            tokens = _cmake_tokens(body)
            if tokens:
                additions.setdefault(tokens[0], []).append(
                    (cmake, tokens[1:], variables)
                )
    return definitions, additions


def _target_source_paths(
    cmake: Path,
    body: str,
    policy: dict,
    target_graph: CMakeTargetGraph,
    generated_bindings: dict[tuple[str, str], dict] | None = None,
    used_generated_bindings: set[tuple[str, str]] | None = None,
) -> set[Path]:
    """Resolve direct and transitive object-library sources for one target."""

    definitions, additions = target_graph
    source_suffixes = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}
    sources: set[Path] = set()
    visited: set[str] = set()
    expanded_targets = 0
    consumed_tokens = 0
    bindings = (
        _generated_source_bindings(policy)
        if generated_bindings is None
        else generated_bindings
    )

    def generated_key(group_cmake: Path, token: str) -> tuple[str, str] | None:
        try:
            return (str(group_cmake.relative_to(DARLING)), token)
        except ValueError:
            return None

    def accept_generated(group_cmake: Path, token: str) -> bool:
        key = generated_key(group_cmake, token)
        if key is None or key not in bindings:
            return False
        if used_generated_bindings is not None:
            used_generated_bindings.add(key)
        return True

    def consume(group_cmake: Path, tokens: list[str], stack: tuple[str, ...]) -> None:
        nonlocal consumed_tokens
        for token in tokens:
            consumed_tokens += 1
            if consumed_tokens > policy["source_token_budget"]:
                raise InventoryError("installed service source-token budget exceeded")
            object_reference = re.fullmatch(r"\$<TARGET_OBJECTS:([^>]+)>", token)
            if object_reference:
                visit_object(object_reference.group(1), stack)
                continue
            if "TARGET_OBJECTS:" in token:
                raise InventoryError(
                    f"unsupported nested TARGET_OBJECTS expression in {group_cmake}: {token}"
                )
            if "${CMAKE_CURRENT_BINARY_DIR}" in token:
                if not accept_generated(group_cmake, token):
                    raise InventoryError(
                        "generated service source lacks an exact SHA-bound input: "
                        f"{group_cmake}:{token}"
                    )
                continue
            expanded_token = token.replace(
                "${CMAKE_CURRENT_SOURCE_DIR}", str(group_cmake.parent.resolve())
            ).replace(
                "${CMAKE_CURRENT_LIST_DIR}", str(group_cmake.parent.resolve())
            )
            if re.search(r"\$\{[^}]+\}", expanded_token):
                raise InventoryError(
                    f"unresolved CMake source variable in {group_cmake}: {token}"
                )
            if "$<" in expanded_token:
                raise InventoryError(
                    f"unsupported CMake source generator expression in {group_cmake}: {token}"
                )
            source_token = Path(expanded_token)
            source = (
                source_token
                if source_token.is_absolute()
                else group_cmake.parent / source_token
            )
            if source.suffix not in source_suffixes:
                continue
            if source.is_file():
                sources.add(source.resolve())
                continue
            if accept_generated(group_cmake, token):
                continue
            raise InventoryError(
                f"service target source is missing and unclassified: {group_cmake}:{token}"
            )

    def visit_object(target: str, stack: tuple[str, ...]) -> None:
        nonlocal expanded_targets
        if target in stack:
            raise InventoryError(
                f"CMake TARGET_OBJECTS cycle: {' -> '.join((*stack, target))}"
            )
        if target in visited:
            return
        target_definitions = definitions.get(target, [])
        if len(target_definitions) != 1:
            raise InventoryError(
                f"TARGET_OBJECTS dependency has {len(target_definitions)} definitions: {target}"
            )
        expanded_targets += 1
        if expanded_targets > policy["target_expansion_budget"]:
            raise InventoryError("installed service target-expansion budget exceeded")
        visited.add(target)
        nested_stack = (*stack, target)
        definition_cmake, definition_tokens, definition_variables = target_definitions[0]
        consume(
            definition_cmake,
            _expand_cmake_tokens(definition_tokens, definition_variables),
            nested_stack,
        )
        for addition_cmake, addition_tokens, addition_variables in additions.get(
            target, []
        ):
            consume(
                addition_cmake,
                _expand_cmake_tokens(addition_tokens, addition_variables),
                nested_stack,
            )

    text = cmake.read_text(encoding="utf-8", errors="replace")
    variables = _cmake_variables(text, policy["variable_call"])
    root_tokens = _expand_cmake_tokens(_cmake_tokens(body), variables)
    if not root_tokens:
        return set()
    root_target = root_tokens[0]
    consume(cmake, root_tokens[1:], (root_target,))
    for addition_cmake, addition_tokens, addition_variables in additions.get(
        root_target, []
    ):
        consume(
            addition_cmake,
            _expand_cmake_tokens(addition_tokens, addition_variables),
            (root_target,),
        )
    return sources


def _target_source_keys(
    cmake: Path,
    body: str,
    policy: dict,
    target_graph: CMakeTargetGraph,
    generated_bindings: dict[tuple[str, str], dict],
    used_generated_bindings: set[tuple[str, str]],
) -> set[str]:
    keys: set[str] = set()
    for source in _target_source_paths(
        cmake,
        body,
        policy,
        target_graph,
        generated_bindings,
        used_generated_bindings,
    ):
        try:
            relative = source.relative_to(DARLING)
        except ValueError as error:
            raise InventoryError(f"service target source is outside Darling: {source}") from error
        keys.add(f"darling:{relative}")
    return keys


def _validate_installed_service_target_coverage(
    registry: dict,
    owners: dict[str, dict[str, str]],
    discovered: set[str],
) -> tuple[int, set[str]]:
    """Bind mutation sources to installed daemon targets, not nearby CMake files."""

    policy, excluded_test_plists = _service_target_policy(registry)
    installed = _installed_launchdaemon_plists(policy)
    stale_exclusions = sorted(excluded_test_plists - set(installed))
    if stale_exclusions:
        raise InventoryError(f"service test exclusions are not installed plist fixtures: {stale_exclusions}")
    targets = _darling_executable_targets(policy)
    target_graph = _cmake_target_graph(policy)
    generated_bindings = _generated_source_bindings(policy)
    used_generated_bindings: set[tuple[str, str]] = set()
    service_targets: set[tuple[Path, str]] = set()
    unresolved: list[str] = []
    for key, _install_cmake in installed.items():
        if key in excluded_test_plists:
            continue
        _, relative = key.split(":", 1)
        plist = DARLING / relative
        try:
            payload = plistlib.loads(plist.read_bytes())
        except Exception as error:
            raise InventoryError(f"installed service plist is malformed: {relative}: {error}") from error
        if not isinstance(payload, dict):
            raise InventoryError(f"installed service plist root is not a dictionary: {relative}")
        program = payload.get("Program")
        arguments = payload.get("ProgramArguments")
        if not program and isinstance(arguments, list) and arguments:
            program = arguments[0]
        # Some installed launchd metadata supplies feature/logging policy but
        # no executable.  It has no source->target claim to validate here.
        if not isinstance(program, str) or not program:
            continue
        output_name = Path(program).name
        target_matches = targets.get(output_name, [])
        if not target_matches:
            unresolved.append(f"{relative}->{output_name}")
            continue
        if len(target_matches) != 1:
            raise InventoryError(
                f"installed service output name is ambiguous: {relative}->{output_name}"
            )
        service_targets.add(target_matches[0])
    if unresolved:
        raise InventoryError(f"installed service programs lack Darling targets: {sorted(unresolved)}")

    mutation_sources: set[str] = set()
    for cmake, body in service_targets:
        mutation_sources.update(
            _target_source_keys(
                cmake,
                body,
                policy,
                target_graph,
                generated_bindings,
                used_generated_bindings,
            )
            & discovered
        )
    stale_generated_bindings = sorted(set(generated_bindings) - used_generated_bindings)
    if stale_generated_bindings:
        raise InventoryError(
            f"generated service source bindings are stale: {stale_generated_bindings}"
        )
    missing = sorted(mutation_sources - set(owners))
    if missing:
        raise InventoryError(
            f"installed service target mutation sources are not typed writers: {missing}"
        )
    required_sources = policy["required_mutation_sources"]
    if len(required_sources) != len(set(required_sources)) or not all(
        isinstance(key, str) and key.startswith("darling:src/") for key in required_sources
    ):
        raise InventoryError("installed service regression sources are malformed or duplicated")
    absent_regressions = sorted(set(required_sources) - mutation_sources)
    if absent_regressions:
        raise InventoryError(
            "target-aware service regressions were not discovered: "
            f"{absent_regressions}"
        )
    return len(service_targets), mutation_sources


def _cmake_subdirectory_closure(registry: dict) -> set[Path]:
    forest = registry["source_forest"]
    root = _repository_root(forest["repository"]) / forest["root"]
    queue = [_repository_root(forest["repository"]) / forest["build_file"]]
    # The forest root is only the traversal origin.  It is not itself a
    # reachable build directory: every production child must be reached by a
    # concrete add_subdirectory() edge (or by an explicit conditional
    # build_closure entry).
    closure: set[Path] = set()
    visited: set[Path] = set()
    subdirectory_pattern = re.compile(r"(?m)^\s*add_subdirectory\s*\(\s*([^\s\)]+)")
    while queue:
        cmake = queue.pop()
        if cmake in visited or not cmake.is_file():
            continue
        visited.add(cmake)
        parent = cmake.parent
        text = cmake.read_text(encoding="utf-8", errors="replace")
        for token in subdirectory_pattern.findall(text):
            if token.startswith("${"):
                continue
            child = (parent / token).resolve()
            if not child.is_dir():
                continue
            closure.add(child)
            child_cmake = child / "CMakeLists.txt"
            if child_cmake.is_file():
                queue.append(child_cmake)
    return closure


def _validate_source_forest(registry: dict) -> None:
    closure = _cmake_subdirectory_closure(registry)
    source_root = (_repository_root("darling") / registry["source_forest"]["root"]).resolve()
    for root in registry["scan"]["production_roots"]:
        repository = root["repository"]
        relative = root["path"]
        absolute = (_repository_root(repository) / relative).resolve()
        if repository == "darling-workspace":
            if not absolute.is_dir():
                raise InventoryError(f"production grouping root is missing: {repository}:{relative}")
            continue
        if not absolute.is_dir() or (absolute != source_root and source_root not in absolute.parents):
            raise InventoryError(f"production grouping root is outside source forest: {repository}:{relative}")

    # CMake membership is checked for every pinned build/install target, not by
    # accepting arbitrary descendants of src.  Conditional targets (mDNS in
    # this branch) are allowed only with an explicit conditional membership
    # label and still require a real target/source declaration.
    for entry in registry["scan"].get("build_closure", []):
        repository = entry["repository"]
        cmake_parent = (_repository_root(repository) / entry["cmake"]).parent.resolve()
        membership = entry.get("membership", "reachable")
        if membership not in {"reachable", "conditional"}:
            raise InventoryError(f"invalid build membership: {entry.get('target')}")
        if repository != "darling":
            continue
        if membership == "reachable" and cmake_parent not in closure:
            raise InventoryError(
                f"build target is not reachable via add_subdirectory: {repository}:{entry['cmake']}"
            )
        if membership == "conditional" and (
            cmake_parent != source_root and source_root not in cmake_parent.parents
        ):
            raise InventoryError(f"conditional build target is outside source forest: {entry['cmake']}")


def _check_scan_coverage(
    registry: dict,
    owners: dict[str, dict[str, str]],
    *,
    extra_roots: list[tuple[str, Path]] | None = None,
    audit_override: dict | None = None,
) -> None:
    _validate_source_forest(registry)
    _validate_build_closure(registry, owners)
    # Owner coverage is checked against the complete universe.  The legacy
    # production_roots list remains a human grouping/index, never the source
    # of truth for discovery; otherwise an omitted root could self-assert
    # exhaustiveness.
    production_roots = [
        (repository, absolute_root)
        for repository, absolute_root, _ in _derived_scan_roots(registry)
    ]
    uncovered_owner_paths = []
    for key in sorted(owners):
        repository, path = key.split(":", 1)
        absolute = _repository_root(repository) / path
        if not any(
            repository == root_repository
            and (absolute == root_path or root_path in absolute.parents)
            for root_repository, root_path in production_roots
        ):
            uncovered_owner_paths.append(key)
    if uncovered_owner_paths:
        raise InventoryError(f"owner path outside complete production forest: {uncovered_owner_paths}")
    declared_group_roots = [
        (root["repository"], _repository_root(root["repository"]) / root["path"])
        for root in registry["scan"]["production_roots"]
    ]
    owners_only_reachable_from_derived_universe = [
        key
        for key, entry in owners.items()
        if not any(
            entry["repository"] == repository
            and (
                _repository_root(repository) / entry["path"] == path
                or path in (_repository_root(repository) / entry["path"]).parents
            )
            for repository, path in declared_group_roots
        )
    ]
    if not owners_only_reachable_from_derived_universe:
        raise InventoryError(
            "scan regression: every owner is reachable from the legacy grouping roots; "
            "discovery must exercise the complete production forest"
        )
    scan_paths = _runtime_scan_paths()
    scan_keys = {f"{repository}:{path}" for repository, path in scan_paths}
    owner_keys = set(owners)
    missing = sorted(scan_keys - owner_keys)
    if missing:
        raise InventoryError(f"unregistered production mutation paths: {missing}")
    production_discovered = _discover_runtime_mutation_paths(registry)
    _validate_installed_service_target_coverage(registry, owners, production_discovered)
    excluded = _excluded_keys(registry)
    stale_exclusions = sorted(excluded - production_discovered)
    if stale_exclusions:
        raise InventoryError(f"exclusions are not current mutation candidates: {stale_exclusions}")
    audit_entries = _validate_candidate_audit(registry, audit_override=audit_override)
    expected_audit_keys = {
        key
        for key in production_discovered
        if key not in owner_keys and not _is_excluded(key, excluded)
    }
    missing_audit = sorted(expected_audit_keys - set(audit_entries))
    unexpected_audit = sorted(set(audit_entries) - expected_audit_keys)
    if missing_audit:
        raise InventoryError(
            f"unregistered discovered mutation paths lack per-path audit: {missing_audit}"
        )
    if unexpected_audit:
        raise InventoryError(f"stale or foreign candidate audit entries: {unexpected_audit}")

    discovered = (
        _discover_runtime_mutation_paths(registry, extra_roots=extra_roots)
        if extra_roots
        else production_discovered
    )
    required_mutation_first = {
        "darling:src/external/libutil/pidfile.c",
        "darling:src/external/openssh/openssh/sshd.c",
        "darling:src/external/system_cmds/dynamic_pager.tproj/dynamic_pager.c",
        "darling:src/external/crontabs/newsyslog/newsyslog.c",
    }
    missing_mutation_first = sorted(required_mutation_first - discovered)
    if missing_mutation_first:
        raise InventoryError(
            f"mutation-first production regressions were not discovered: {missing_mutation_first}"
        )
    unregistered = sorted(
        key
        for key in discovered
        if key not in owner_keys
        and not _is_excluded(key, excluded)
        and key not in audit_entries
    )
    if unregistered:
        raise InventoryError(f"unregistered discovered mutation paths: {unregistered}")
    if not discovered:
        raise InventoryError("source scanner discovered no runtime mutation paths")
    # The XNU syscall family has one writer record per concrete translation
    # unit, so it is intentionally larger than the small set of core roots
    # above.  Those paths are still covered by the registry's exhaustive owner
    # list and by the existence/anchor checks in _owner_paths().

    # Every finite scan source must still contain a mutation hook.  This keeps
    # the registry from silently drifting to a list of names with no source
    # evidence after a refactor.
    markers = tuple(registry["scan"]["mutation_markers"])
    for key in sorted(owners):
        repository, path = key.split(":", 1)
        source = (_repository_root(repository) / path).read_text(encoding="utf-8", errors="replace")
        if (
            not any(marker in source for marker in markers)
            and "vchroot_" not in source
            and "repair_prefix_prerequisites" not in source
            and "cleanup_rootless_runtime_sockets" not in source
        ):
            raise InventoryError(f"scan source has no mutation marker: {repository}:{path}")


def _reseal_audit(audit: dict) -> None:
    audit["count"] = len(audit["entries"])
    canonical = json.dumps(
        audit["entries"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    audit["entries_digest"] = hashlib.sha256(canonical).hexdigest()


def _negative_contract(registry: dict, owners: dict[str, dict[str, str]]) -> None:
    """Exercise the fail-closed path with an unregistered synthetic writer."""

    with tempfile.TemporaryDirectory(prefix="namespace-writer-negative-") as temporary:
        fake_path = Path(temporary) / "unregistered-runtime-writer.c"
        fake_path.write_text(
            'int bad(void) { return unlink(".init.pid"); }\n',
            encoding="utf-8",
        )
        indirect_path = Path(temporary) / "unregistered-indirect-writer.c"
        indirect_path.write_text(
            '#define PID_FILE "/var/run/indirect.pid"\n'
            + "/* deliberately separate path declaration and mutation */\n" * 20
            + 'int indirect(void) { return open(PID_FILE, O_CREAT | O_WRONLY, 0600); }\n',
            encoding="utf-8",
        )
        computed_path = Path(temporary) / "unregistered-computed-writer.c"
        computed_path.write_text(
            '#include <stdlib.h>\n'
            '#include <stdio.h>\n'
            '#include <sys/stat.h>\n'
            'int computed(void) {\n'
            '  char destination[256];\n'
            '  snprintf(destination, sizeof(destination), "%s/%s", getenv("ROOT"), "leaf");\n'
            '  mkdir(destination, 0700);\n'
            '  return unlink(destination);\n'
            '}\n',
            encoding="utf-8",
        )
        try:
            _check_scan_coverage(
                registry,
                extra_roots=[("darling-workspace", fake_path.parent)],
                owners=owners,
            )
        except InventoryError as error:
            message = str(error)
            assert "unregistered discovered mutation paths" in message
            assert "unregistered-runtime-writer.c" in message
            assert "unregistered-indirect-writer.c" in message
            assert "unregistered-computed-writer.c" in message
        else:
            raise InventoryError("negative fixture unexpectedly passed scan coverage")

        # RED model for the exact false-negative class: the installed service
        # has no direct mutation, while its TARGET_OBJECTS dependency receives
        # the only mutation-bearing source through target_sources().  A
        # shallow executable-body scan sees no writer; the production resolver
        # must reach writer.c through both graph edges.
        fixture_root = Path(temporary) / "object-service-fixture"
        service_dir = fixture_root / "service"
        object_dir = fixture_root / "objects"
        service_dir.mkdir(parents=True)
        object_dir.mkdir(parents=True)
        service_source = service_dir / "service.c"
        path_writer = service_dir / "path-writer.cxx"
        empty_object = object_dir / "empty.c"
        mutating_object = object_dir / "writer.c"
        service_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        path_writer.write_text(
            'int path_writer(void) { return mkdir("path-runtime", 0700); }\n',
            encoding="utf-8",
        )
        empty_object.write_text("int helper(void) { return 0; }\n", encoding="utf-8")
        mutating_object.write_text(
            'int writer(void) { return mkdir("runtime", 0700); }\n',
            encoding="utf-8",
        )
        (service_dir / "fixture.plist").write_bytes(
            plistlib.dumps({"Program": "/usr/sbin/fixture_service"})
        )
        service_cmake = service_dir / "CMakeLists.txt"
        service_cmake.write_text(
            "add_darling_executable(fixture_service\n"
            "  service.c\n"
            "  $<TARGET_OBJECTS:fixture_objects>\n"
            ")\n"
            "add_darling_executable(path_fixture\n"
            "  ${CMAKE_CURRENT_SOURCE_DIR}/path-writer.cxx\n"
            ")\n"
            "add_darling_executable(unknown_fixture ${UNKNOWN_SOURCES})\n"
            "add_darling_executable(generator_fixture "
            "$<IF:$<BOOL:1>,path-writer.cxx,service.c>)\n"
            "add_darling_executable(binary_fixture "
            "${CMAKE_CURRENT_BINARY_DIR}/generated.c)\n"
            "install(FILES fixture.plist "
            "DESTINATION libexec/darling/System/Library/LaunchDaemons)\n",
            encoding="utf-8",
        )
        (object_dir / "CMakeLists.txt").write_text(
            "add_darling_object_library(fixture_objects empty.c)\n"
            "target_sources(fixture_objects PRIVATE writer.c)\n"
            "add_darling_object_library(cycle_a $<TARGET_OBJECTS:cycle_b>)\n"
            "add_darling_object_library(cycle_b $<TARGET_OBJECTS:cycle_a>)\n",
            encoding="utf-8",
        )
        fixture_policy, _ = _service_target_policy(registry)
        executable_bodies = _cmake_call_bodies(
            service_cmake.read_text(encoding="utf-8"),
            fixture_policy["executable_call"],
        )
        bodies_by_target = {
            _cmake_tokens(executable_body)[0]: executable_body
            for executable_body in executable_bodies
            if _cmake_tokens(executable_body)
        }
        if set(bodies_by_target) != {
            "fixture_service",
            "path_fixture",
            "unknown_fixture",
            "generator_fixture",
            "binary_fixture",
        }:
            raise InventoryError("object-service RED fixture executable is malformed")
        graph = _cmake_target_graph(fixture_policy, fixture_root)
        cxx_candidates = _candidate_files(
            service_dir,
            set(registry["scan"]["translation_unit_extensions"]),
            _namespace_mutation_pattern(registry),
            engine="python",
        )
        if cxx_candidates != [path_writer]:
            raise InventoryError("mutating .cxx source was not discovered")
        fixture_body = bodies_by_target["fixture_service"]
        resolved = _target_source_paths(
            service_cmake, fixture_body, fixture_policy, graph
        )
        direct_tokens = _expand_cmake_tokens(
            _cmake_tokens(fixture_body)[1:], {}
        )
        shallow = {
            (service_dir / token).resolve()
            for token in direct_tokens
            if (service_dir / token).suffix in {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}
            and (service_dir / token).is_file()
        }
        resolved_mutations = {
            source
            for source in resolved
            if _audit_mutation_operators(source.read_text(encoding="utf-8"))
        }
        if shallow != {service_source.resolve()} or any(
            _audit_mutation_operators(source.read_text(encoding="utf-8"))
            for source in shallow
        ):
            raise InventoryError("object-service RED arm unexpectedly has a direct writer")
        if resolved_mutations != {mutating_object.resolve()} or empty_object.resolve() not in resolved:
            raise InventoryError("object-service transitive mutation source was not resolved")
        path_resolved = _target_source_paths(
            service_cmake, bodies_by_target["path_fixture"], fixture_policy, graph
        )
        if path_resolved != {path_writer.resolve()} or not _audit_mutation_operators(
            path_writer.read_text(encoding="utf-8")
        ):
            raise InventoryError("embedded CMAKE_CURRENT_SOURCE_DIR writer was not resolved")
        try:
            _target_source_paths(
                service_cmake, bodies_by_target["unknown_fixture"], fixture_policy, graph
            )
        except InventoryError as error:
            assert "unresolved CMake source variable" in str(error)
        else:
            raise InventoryError("unknown CMake source variable unexpectedly accepted")
        try:
            _target_source_paths(
                service_cmake,
                bodies_by_target["generator_fixture"],
                fixture_policy,
                graph,
            )
        except InventoryError as error:
            assert "unsupported CMake source generator expression" in str(error)
        else:
            raise InventoryError("unsupported CMake generator expression unexpectedly accepted")
        try:
            _target_source_paths(
                service_cmake, bodies_by_target["binary_fixture"], fixture_policy, graph
            )
        except InventoryError as error:
            assert "lacks an exact SHA-bound input" in str(error)
        else:
            raise InventoryError("unbound generated service source unexpectedly accepted")
        stale_generated_policy = json.loads(json.dumps(fixture_policy))
        stale_generated_policy["generated_source_bindings"][0]["inputs"][0][
            "sha256"
        ] = "0" * 64
        try:
            _generated_source_bindings(stale_generated_policy)
        except InventoryError as error:
            assert "generated service source input is stale" in str(error)
        else:
            raise InventoryError("stale generated service input unexpectedly accepted")
        forged_generator_policy = json.loads(json.dumps(fixture_policy))
        forged_generator_policy["generated_source_bindings"][0]["generator"] = dict(
            forged_generator_policy["generated_source_bindings"][0]["inputs"][0]
        )
        try:
            _generated_source_bindings(forged_generator_policy)
        except InventoryError as error:
            assert "does not use the MIG generator" in str(error)
        else:
            raise InventoryError("unrelated valid generator unexpectedly accepted")
        unrelated_input_policy = json.loads(json.dumps(fixture_policy))
        unrelated_input_policy["generated_source_bindings"][0]["inputs"] = json.loads(
            json.dumps(
                unrelated_input_policy["generated_source_bindings"][2]["inputs"]
            )
        )
        try:
            _generated_source_bindings(unrelated_input_policy)
        except InventoryError as error:
            assert "not produced by one exact declaring mig(input)" in str(error)
        else:
            raise InventoryError("unrelated valid MIG input unexpectedly accepted")
        unrelated_output_policy = json.loads(json.dumps(fixture_policy))
        unrelated_output_policy["generated_source_bindings"][0][
            "source"
        ] = "${CMAKE_CURRENT_BINARY_DIR}/unrelatedServer.c"
        try:
            _generated_source_bindings(unrelated_output_policy)
        except InventoryError as error:
            assert "not produced by one exact declaring mig(input)" in str(error)
        else:
            raise InventoryError("unrelated generated output unexpectedly accepted")
        unrelated_closure_policy = json.loads(json.dumps(fixture_policy))
        unrelated_closure = DARLING / "CMakeLists.txt"
        unrelated_closure_policy["mig_declared_source_inputs"][0] = {
            "path": "CMakeLists.txt",
            "sha256": _sha256_file(unrelated_closure),
        }
        try:
            _generated_source_bindings(unrelated_closure_policy)
        except InventoryError as error:
            assert "differ from the CMake-derived source set" in str(error)
        else:
            raise InventoryError("unrelated valid MIG closure source unexpectedly accepted")

        mig_fixture_root = Path(temporary) / "mig-order"
        mig_fixture_root.mkdir()
        (mig_fixture_root / "x.defs").write_text("subsystem x 1;\n", encoding="utf-8")
        late_suffix_cmake = mig_fixture_root / "late.cmake"
        late_suffix_cmake.write_text(
            "mig(x.defs)\n"
            "set(MIG_SERVER_SOURCE_SUFFIX Late.cpp)\n",
            encoding="utf-8",
        )
        try:
            _validate_mig_generated_source(
                late_suffix_cmake,
                "${CMAKE_CURRENT_BINARY_DIR}/xLate.cpp",
                "x.defs",
                mig_fixture_root,
            )
        except InventoryError as error:
            assert "not produced by one exact declaring mig(input)" in str(error)
        else:
            raise InventoryError("late MIG suffix assignment unexpectedly applied retroactively")

        conditional_mig_cmake = mig_fixture_root / "conditional.cmake"
        conditional_mig_cmake.write_text(
            "if(FALSE)\n"
            "  mig(x.defs)\n"
            "endif()\n",
            encoding="utf-8",
        )
        try:
            _validate_mig_generated_source(
                conditional_mig_cmake,
                "${CMAKE_CURRENT_BINARY_DIR}/xServer.c",
                "x.defs",
                mig_fixture_root,
            )
        except InventoryError as error:
            assert "unsupported conditional context" in str(error)
        else:
            raise InventoryError("conditional MIG invocation unexpectedly accepted")
        cycle_body = "cycle_service $<TARGET_OBJECTS:cycle_a>"
        try:
            _target_source_paths(service_cmake, cycle_body, fixture_policy, graph)
        except InventoryError as error:
            assert "TARGET_OBJECTS cycle" in str(error)
        else:
            raise InventoryError("object-service cycle unexpectedly accepted")
        target_budget_policy = json.loads(json.dumps(fixture_policy))
        target_budget_policy["target_expansion_budget"] = 1
        try:
            _target_source_paths(service_cmake, cycle_body, target_budget_policy, graph)
        except InventoryError as error:
            assert "target-expansion budget" in str(error)
        else:
            raise InventoryError("object-service target budget unexpectedly bypassed")
        token_budget_policy = json.loads(json.dumps(fixture_policy))
        token_budget_policy["source_token_budget"] = 1
        try:
            _target_source_paths(
                service_cmake, fixture_body, token_budget_policy, graph
            )
        except InventoryError as error:
            assert "source-token budget" in str(error)
        else:
            raise InventoryError("object-service source-token budget unexpectedly bypassed")

    # The build anchor must be a real add_subdirectory edge.  The source root
    # itself (and an arbitrary umbrella subtree) is not accepted merely
    # because it is a descendant of ``src``.
    bad_registry = json.loads(json.dumps(registry))
    bad_registry["scan"]["build_closure"].append(
        {
            "repository": "darling",
            "cmake": "src/external/network_cmds/mnc.tproj/CMakeLists.txt",
            "target": "mnc",
            "sources": ["mnc_main.c"],
            "membership": "reachable",
        }
    )
    try:
        _validate_source_forest(bad_registry)
    except InventoryError as error:
        assert "not reachable via add_subdirectory" in str(error)
    else:
        raise InventoryError("unreachable CMake subtree unexpectedly accepted")

    service_policy, _ = _service_target_policy(registry)
    installed_plists = _installed_launchdaemon_plists(service_policy)
    if _cmake_call_bodies(
        "#install(FILES disabled.plist DESTINATION LaunchDaemons)", "install"
    ):
        raise InventoryError("commented CMake install unexpectedly treated as production")
    for false_install in (
        "darling:src/external/system_cmds/dynamic_pager.tproj/"
        "com.apple.dynamic_pager.plist",
        "darling:src/external/mDNSResponder/mDNSMacOSX/FeatureFlags/"
        "mDNSResponder.plist",
    ):
        if false_install in installed_plists:
            raise InventoryError(
                f"non-installed same-name/commented plist unexpectedly accepted: {false_install}"
            )

    missing_cups_owner = dict(owners)
    missing_cups_owner.pop("darling:src/external/cups/cups/scheduler/conf.c")
    try:
        _check_scan_coverage(registry, missing_cups_owner)
    except InventoryError as error:
        assert "installed service target mutation sources are not typed writers" in str(error)
        assert "cups/cups/scheduler/conf.c" in str(error)
    else:
        raise InventoryError("untyped installed cupsd source unexpectedly passed")

    audit = _load_candidate_audit(registry)
    stale_source_audit = json.loads(json.dumps(audit))
    stale_source_audit["entries"][0]["sha256"] = "0" * 64
    _reseal_audit(stale_source_audit)
    try:
        _check_scan_coverage(registry, owners, audit_override=stale_source_audit)
    except InventoryError as error:
        assert "candidate audit source SHA mismatch" in str(error)
    else:
        raise InventoryError("stale per-path candidate SHA unexpectedly passed")

    stale_anchor_audit = json.loads(json.dumps(audit))
    stale_anchor_audit["entries"][0]["evidence"]["build_anchor"]["sha256"] = "0" * 64
    _reseal_audit(stale_anchor_audit)
    try:
        _check_scan_coverage(registry, owners, audit_override=stale_anchor_audit)
    except InventoryError as error:
        assert "candidate audit build anchor SHA mismatch" in str(error)
    else:
        raise InventoryError("stale candidate build/runtime anchor unexpectedly passed")

    missing_entry_audit = json.loads(json.dumps(audit))
    missing_entry_audit["entries"].pop()
    _reseal_audit(missing_entry_audit)
    try:
        _check_scan_coverage(registry, owners, audit_override=missing_entry_audit)
    except InventoryError as error:
        assert "lack per-path audit" in str(error)
    else:
        raise InventoryError("missing per-path candidate audit unexpectedly passed")

    foreign_entry_audit = json.loads(json.dumps(audit))
    owner_key = "darling-workspace:west_commands/darling_build.py"
    assert owner_key in owners
    owner_repository, owner_path = owner_key.split(":", 1)
    foreign = json.loads(json.dumps(foreign_entry_audit["entries"][0]))
    owner_absolute = _repository_root(owner_repository) / owner_path
    foreign.update(
        {
            "repository": owner_repository,
            "path": owner_path,
            "sha256": _sha256_file(owner_absolute),
            "reason": f"negative foreign entry for {owner_path}",
        }
    )
    foreign["evidence"]["mutation_operators"] = _audit_mutation_operators(
        owner_absolute.read_text(encoding="utf-8", errors="replace")
    )
    foreign["evidence"]["build_anchor"] = {
        "repository": owner_repository,
        "path": owner_path,
        "sha256": _sha256_file(owner_absolute),
        "relation": "workspace-runtime-source",
    }
    foreign_entry_audit["entries"].append(foreign)
    _reseal_audit(foreign_entry_audit)
    try:
        _check_scan_coverage(registry, owners, audit_override=foreign_entry_audit)
    except InventoryError as error:
        assert "stale or foreign candidate audit entries" in str(error)
    else:
        raise InventoryError("foreign per-path candidate audit unexpectedly passed")

    stale_exclusion = json.loads(json.dumps(registry))
    stale_exclusion["excluded_mutations"][0]["sha256"] = "0" * 64
    try:
        _excluded_keys(stale_exclusion)
    except InventoryError as error:
        assert "excluded source SHA mismatch" in str(error)
    else:
        raise InventoryError("stale exclusion SHA unexpectedly passed")

    subtree_exclusion = json.loads(json.dumps(registry))
    subtree_exclusion["excluded_mutations"][0]["path"] = "src/external/curl/curl/tests/server"
    try:
        _excluded_keys(subtree_exclusion)
    except InventoryError as error:
        assert "not an exact regular file" in str(error)
    else:
        raise InventoryError("subtree exclusion unexpectedly passed")

    for compatibility in ("cohort-ready", "compatible"):
        for status, retained_fd in (("missing", False), ("exact-exclusive-flock", False)):
            try:
                _validate_lock_contract(
                    f"negative-{compatibility}-writer-{status}",
                    compatibility,
                    {
                        "required": True,
                        "path": ".lifecycle.lock",
                        "status": status,
                        "acquisition": "none",
                        "retained_fd": retained_fd,
                    },
                )
            except InventoryError as error:
                assert "exact exclusive flock" in str(error) or "retain" in str(error)
            else:
                raise InventoryError("negative lock fixture unexpectedly accepted")


def _digest_sources(owners: dict[str, dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for key in sorted(owners):
        entry = owners[key]
        absolute = _repository_root(entry["repository"]) / entry["path"]
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(absolute.read_bytes()).digest())
    return digest.hexdigest()


def _digest_candidate_sources(keys: set[str]) -> str:
    digest = hashlib.sha256()
    for key in sorted(keys):
        repository, path = key.split(":", 1)
        absolute = _repository_root(repository) / path
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(absolute.read_bytes()).digest())
    return digest.hexdigest()


def main() -> None:
    registry = _load_registry()
    owners = _owner_paths(registry)
    _check_scan_coverage(registry, owners)
    service_policy, _ = _service_target_policy(registry)
    generated_source_bindings = _generated_source_bindings(service_policy)
    mig_tools, mig_outputs = _materialize_mig_output_proof(
        generated_source_bindings
    )
    _validate_materialized_mig_output_proof(
        service_policy, generated_source_bindings, mig_tools, mig_outputs
    )
    _mig_output_negative_contract(
        service_policy, generated_source_bindings, mig_tools, mig_outputs
    )
    _negative_contract(registry, owners)
    candidate_parity = _candidate_engine_parity_contract(registry)

    incompatible = sum(
        writer.get("compatibility") == "incompatible" for writer in registry["writers"]
    )
    cohort_ready = sum(
        writer.get("compatibility") == "cohort-ready" for writer in registry["writers"]
    )
    compatible = sum(
        writer.get("compatibility") == "compatible" for writer in registry["writers"]
    )
    assert cohort_ready == 6, "only the reviewed first cohort and its transport may be cohort-ready"
    assert compatible == 0, "global production routing must remain disabled"
    assert incompatible + cohort_ready == len(registry["writers"]), (
        "all non-cohort writers must remain incompatible"
    )
    source_digest = _digest_sources(owners)
    discovered = _discover_runtime_mutation_paths(registry)
    service_targets, service_sources = _validate_installed_service_target_coverage(
        registry, owners, discovered
    )
    mig_declared_inputs = _mig_generator_declared_inputs()
    exclusions = _excluded_keys(registry)
    excluded_hits = {key for key in discovered if _is_excluded(key, exclusions)}
    audit_entries = _validate_candidate_audit(registry)
    candidate_digest = _digest_candidate_sources(set(audit_entries))
    audit_digest = _sha256_file(_audit_path(registry))
    registry_digest = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    contract_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    print(
        "NAMESPACE_WRITER_INVENTORY_VALID "
        f"coverage-tier=source writers={len(registry['writers'])} "
        f"paths={len(owners)} discovered={len(discovered)} "
        f"service_targets={service_targets} service_mutation_sources={len(service_sources)} "
        f"generated_source_bindings={len(generated_source_bindings)} "
        f"mig_declared_inputs={len(mig_declared_inputs)} mig_outputs={len(mig_outputs)} "
        f"excluded_hits={len(excluded_hits)} audited_non_shared={len(audit_entries)} "
        f"incompatible={incompatible} cohort_ready={cohort_ready} "
        f"compatible={compatible} candidate_digest={candidate_digest} "
        f"audit_digest={audit_digest} source_digest={source_digest} "
        f"registry_digest={registry_digest} contract_digest={contract_digest} "
        f"negatives=35 candidate_parity={candidate_parity} routing=DEFERRED"
    )


if __name__ == "__main__":
    main()

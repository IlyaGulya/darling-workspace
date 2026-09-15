"""Identity-keyed snapshots of the stock Homebrew stack inside a guest prefix.

The stock acceptance replays rebuild cmake and wget from source inside the
guest -- measured at 88 minutes for one ``brew reinstall --keep-tmp
--build-from-source cmake wget`` on 2026-09-14 -- and a fresh prefix cannot run
those phases at all, because the replay needs the staged ``west-homebrew-lz4-*``
work directory and an installed Cellar that a previous run left behind. The
resulting prefix state is a pure function of a small identity:

* the pinned brew and homebrew-core revisions and the pinned input digests;
* the resolved formula versions the Cellar actually holds;
* the guest toolchain (CommandLineTools) identity the framework verifies;
* the deployed Darling runtime identity (patch stack digests and revisions);
* the layout schema of this snapshot format and the host architecture.

Nothing cached that state, so every fresh prefix paid for it again. This module
archives the guest-visible prefix subtree that resource staging and the source
builds produce (the ``usr/local`` tree plus the staged work directory) into a
registered ``.west-test`` state root, and restores it for a matching identity.

Strictness over speed. A capture publishes through a temporary path and only
then writes the completion marker, so a killed capture is never consumed. A
restore verifies the recorded per-file sha256 manifest and the formula source
receipts it recorded before anything reaches the prefix. Any mismatch falls
back to the from-source staging path instead of a widened match. The store is
bounded with least-recently-used eviction enforced *before* a snapshot is
added, and reports its own hit, miss, eviction and byte accounting.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import tarfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .test_store import (
        canonical_digest,
        entry_bytes,
        entry_reusable,
        locked_entry,
        marker_path as _shared_marker_path,
        parse_bound,
        prune_entries,
        read_marker as _shared_read_marker,
        read_stats,
        record_event,
        store_disabled,
        store_root as _shared_store_root,
        sync_directory,
        touch_entry,
        write_marker as _shared_write_marker,
    )
except ImportError:
    from test_store import (
        canonical_digest,
        entry_bytes,
        entry_reusable,
        locked_entry,
        marker_path as _shared_marker_path,
        parse_bound,
        prune_entries,
        read_marker as _shared_read_marker,
        read_stats,
        record_event,
        store_disabled,
        store_root as _shared_store_root,
        sync_directory,
        touch_entry,
        write_marker as _shared_write_marker,
    )

try:
    from .guest_toolchain import (
        COMMAND_LINE_TOOLS_RECEIPT,
        REVIEWED_COMMAND_LINE_TOOLS_SHA256,
        SELECTED_COMMAND_LINE_TOOLS_ID,
        SELECTED_COMMAND_LINE_TOOLS_SHA256,
    )
    from .test_homebrew import BREW_COMMIT, CORE_COMMIT, INPUTS, RUBY_VERSION
except ImportError:
    from guest_toolchain import (
        COMMAND_LINE_TOOLS_RECEIPT,
        REVIEWED_COMMAND_LINE_TOOLS_SHA256,
        SELECTED_COMMAND_LINE_TOOLS_ID,
        SELECTED_COMMAND_LINE_TOOLS_SHA256,
    )
    from test_homebrew import BREW_COMMIT, CORE_COMMIT, INPUTS, RUBY_VERSION


SCHEMA = 1
KIND = "west-stock-stack"
MARKER_NAME = ".west-stock-stack.json"
# The layout schema this snapshot format describes: which guest-visible paths
# are archived and how the manifest is written. A change to either invalidates
# older snapshots instead of restoring them into a layout they do not describe.
LAYOUT_SCHEMA = 1
DEFAULT_MAX_BYTES = 12 * 1024**3
ARCHIVE_NAME = "snapshot.tar"
ENTRY_DIRECTORY = "stack"
TEMP_PREFIX = ".capture-"
RESTORE_PREFIX = ".west-stock-restore-"
WORK_DIRECTORY_GLOB = "west-homebrew-lz4-*"
STORE_SWITCH = "WEST_STOCK_STACK_CACHE"
STORE_BOUND = "WEST_STOCK_STACK_CACHE_MAX_BYTES"
# ``WEST_STOCK_STACK_CACHE=off`` disables the store for a whole run. This switch
# is narrower: it refuses only the restore, so an invocation whose subject is the
# from-source build still stages and still captures. A test declaration sets it,
# because the acceptance claim for that test is the build, not the restored
# result of an earlier one.
RESTORE_SWITCH = "WEST_STOCK_STACK_RESTORE"
# Build scratch, guest logs and brew's download cache are not stack state: the
# replay re-stages pinned tarballs into the cache, and the directories only have
# to exist. Archiving them would archive hours of build trees.
WORK_EMPTY_DIRECTORIES = ("tmp", "logs", "cache/downloads")
# The formulas the stock replays build from source. A capture requires a source
# receipt for every one of them that is installed.
STACK_FORMULAS = ("cmake", "wget", "lz4")
# ``usr/local`` is the guest Homebrew tree. ``libexec`` holds the immutable
# lower runtime template, which staging verifies for drift and never rewrites.
CAPTURED_LOCAL_ROOT = "usr/local"


def _digest_file(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def store_root(manifest_repo: Path, environ: Mapping[str, str]) -> Path | None:
    """Return the stock-stack snapshot store, or ``None`` when it is disabled.

    ``WEST_STOCK_STACK_CACHE_DIR`` names the store explicitly, which is how a CI
    tier pins it. Otherwise the store lives under the manifest repository, so it
    is a registered ``.west-test`` state root rather than an unowned tree.
    ``WEST_STOCK_STACK_CACHE=off`` is the acceptance-grade kill switch: a run
    that has to build the stack from source disables the cache and says so.
    """

    return _shared_store_root(
        manifest_repo,
        environ,
        switch=STORE_SWITCH,
        directory=".west-test",
        default_name="stock-stack-cache",
    )


def store_max_bytes(environ: Mapping[str, str]) -> int:
    """Return the configured store bound in bytes."""

    return parse_bound(environ, STORE_BOUND, DEFAULT_MAX_BYTES)


def store_restore_disabled(environ: Mapping[str, str]) -> bool:
    """Return whether this invocation must stage instead of restoring.

    The store stays enabled, so the staged stack is still captured.
    """

    return store_disabled(environ, RESTORE_SWITCH)


def pinned_inputs() -> dict[str, Any]:
    """Return the pinned brew, Ruby and formula inputs staging consumes."""

    return {
        "brew-commit": BREW_COMMIT,
        "homebrew-core-commit": CORE_COMMIT,
        "portable-ruby-version": RUBY_VERSION,
        "inputs": {spec.filename: spec.sha256 for spec in INPUTS},
    }


def guest_toolchain_identity(
    prefix: Path, environ: Mapping[str, str]
) -> dict[str, Any]:
    """Return the guest toolchain identity the framework already verifies.

    The framework authenticates a fixed CommandLineTools package set (or the
    locally supplied CLT13.2 package) and records the receipt it verified under
    the prefix. That receipt, plus the canonical compiler link prefix repair
    establishes, is the toolchain this snapshot was built against.
    """

    receipt = prefix / COMMAND_LINE_TOOLS_RECEIPT
    canonical = prefix / "Library/Developer/CommandLineTools"
    link_target: str | None = None
    if canonical.is_symlink():
        link_target = os.readlink(canonical)
    return {
        "selected": environ.get("DARLING_CLT_PACKAGE") or SELECTED_COMMAND_LINE_TOOLS_ID,
        "selected-sha256": SELECTED_COMMAND_LINE_TOOLS_SHA256,
        "reviewed-packages": dict(REVIEWED_COMMAND_LINE_TOOLS_SHA256),
        "receipt-sha256": _digest_file(receipt),
        "canonical-link": link_target,
        "canonical-compiler": (canonical / "usr/bin/clang").is_file(),
    }


def runtime_identity_digest(
    *,
    prefix: Path,
    manifest_repo: Path,
    topdir: Path,
    profile_name: str | None,
) -> str | None:
    """Return the deployed runtime identity digest, or ``None`` when unknown.

    The prefix's retained runtime marker is the authoritative record: a
    ``guest-runtime-script`` invocation does not carry the profile name, and the
    deployed provider in the prefix is what the stack actually runs against. A
    caller that names a profile the prefix does not retain gets nothing, so a
    mismatch can never reuse another provider's stack. ``None`` means the run
    cannot be keyed, and no snapshot is reused.
    """

    marker = prefix / ".west-runtime-profile.json"
    if marker.is_file() and not marker.is_symlink():
        try:
            record = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            record = None
        if isinstance(record, dict) and record.get("schema") == 2:
            fingerprint = record.get("fingerprint")
            if isinstance(fingerprint, dict) and fingerprint:
                if profile_name is not None and record.get("profile") != profile_name:
                    return None
                return canonical_digest(fingerprint)
    if not profile_name:
        return None
    try:
        try:
            from .test_runtime import load_ctest_runtime_profiles
            from .test_runtime_identity import runtime_identity
        except ImportError:
            from test_runtime import load_ctest_runtime_profiles
            from test_runtime_identity import runtime_identity
        definitions = load_ctest_runtime_profiles(
            manifest_repo / "testkit/runtime-profiles.yml"
        )
        definition = definitions.get(profile_name)
        if not isinstance(definition, dict):
            return None
        launcher = prefix / "bin/darling"
        if not launcher.is_file():
            return None
        return canonical_digest(
            runtime_identity(
                topdir=topdir,
                manifest_repo=manifest_repo,
                profile_name=profile_name,
                definition=definition,
                launcher=launcher,
            )
        )
    except (OSError, ValueError, ImportError):
        return None


def retained_profile_name(prefix: Path) -> str | None:
    """Return the profile name a prefix's retained marker records, if any.

    The marker names the profile that provisioned the prefix, which is the
    bootstrap provider. A deployed profile never matches it, so a caller asking
    the prefix for its runtime identity must ask under the retained name rather
    than the profile the test deploys: naming the deployed profile is what made
    every test that consumes the stock stack unkeyable.
    """

    marker = prefix / ".west-runtime-profile.json"
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        record = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("schema") != 2:
        return None
    name = record.get("profile")
    return name if isinstance(name, str) and name else None


def stack_request(
    *,
    prefix: Path,
    manifest_repo: Path,
    topdir: Path,
    profile_name: str | None,
    environ: Mapping[str, str],
) -> dict[str, Any] | None:
    """Return the identity inputs that are knowable before the stack is staged.

    Resolved formula versions are not part of the request: they are an output of
    the pinned revisions and are recorded in the snapshot itself. The versions
    this host can vouch for without a prefix -- the pinned lz4 and portable Ruby
    the resource stages -- are declared, and a restore refuses a snapshot whose
    recorded versions disagree with them. ``profile_name`` is a cross-check when
    the caller has one; the prefix's retained runtime marker is authoritative.
    """

    runtime = runtime_identity_digest(
        prefix=prefix,
        manifest_repo=manifest_repo,
        topdir=topdir,
        profile_name=profile_name,
    )
    if runtime is None:
        return None
    return {
        "layout": LAYOUT_SCHEMA,
        "arch": platform.machine(),
        "pinned": pinned_inputs(),
        # The only stack formula whose version this host can vouch for without a
        # prefix is the pinned lz4 the resource stages. The rest are resolved by
        # the pinned revisions and recorded in the snapshot.
        "declared-formulas": {"lz4": ["1.10.0"]},
        "guest-toolchain": guest_toolchain_identity(prefix, environ),
        "runtime": runtime,
    }


def resolved_formulas(prefix: Path) -> dict[str, list[str]]:
    """Return the installed formula versions recorded in the prefix Cellar."""

    cellar = prefix / CAPTURED_LOCAL_ROOT / "Cellar"
    formulas: dict[str, list[str]] = {}
    if cellar.is_symlink() or not cellar.is_dir():
        return formulas
    for formula in sorted(cellar.iterdir()):
        if formula.is_symlink() or not formula.is_dir():
            continue
        versions = sorted(
            entry.name
            for entry in formula.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        )
        if versions:
            formulas[formula.name] = versions
    return formulas


def request_key(request: Mapping[str, Any]) -> str:
    """Return the cache key for the identity inputs known before staging."""

    return canonical_digest({"schema": SCHEMA, "kind": KIND, "request": True, **request})


def identity_key(identity: Mapping[str, Any]) -> str:
    """Return the cache key for one fully resolved stack identity."""

    return canonical_digest({"schema": SCHEMA, "kind": KIND, **identity})


def stack_identity(
    request: Mapping[str, Any], formulas: Mapping[str, list[str]]
) -> dict[str, Any]:
    """Return the resolved identity of one captured stack."""

    return {
        **request,
        "formulas": {name: list(versions) for name, versions in formulas.items()},
    }


def entry_path(store: Path, key: str) -> Path:
    return store / ENTRY_DIRECTORY / key


def marker_path(entry: Path) -> Path:
    return _shared_marker_path(entry, name=MARKER_NAME)


def read_marker(entry: Path, key: str) -> dict[str, Any] | None:
    """Return the stock-stack completion marker of ``entry`` for ``key``."""

    return _shared_read_marker(entry, kind=KIND, key=key, name=MARKER_NAME)


def write_marker(entry: Path, key: str, **fields: Any) -> None:
    """Record that ``entry`` completes ``key`` for this snapshot format."""

    _shared_write_marker(entry, kind=KIND, key=key, name=MARKER_NAME, **fields)


def _declared_versions_satisfied(
    request: Mapping[str, Any], identity: Mapping[str, Any]
) -> bool:
    declared = request.get("declared-formulas")
    if not isinstance(declared, dict):
        return True
    recorded = identity.get("formulas")
    if not isinstance(recorded, dict):
        return False
    for name, versions in declared.items():
        declared_versions = versions if isinstance(versions, list) else [versions]
        installed = recorded.get(name)
        if not isinstance(installed, list) or not set(declared_versions) <= set(installed):
            return False
    return True


def read_entry(
    store: Path, request: Mapping[str, Any]
) -> tuple[Path, dict[str, Any]] | None:
    """Return the reusable entry for ``request``, or ``None``.

    Reuse requires the completion marker for exactly this request, a marker that
    still describes its own identity, and recorded formula versions that agree
    with what the request vouched for. An entry that fails any of those is never
    consumed: a partial or tampered capture is rebuilt instead.
    """

    key = request_key(request)
    entry = entry_path(store, key)

    def validate(marker: dict[str, Any]) -> bool:
        identity = marker.get("identity")
        if not isinstance(identity, dict):
            return False
        if marker.get("request") != request:
            return False
        if marker.get("identity_key") != identity_key(identity):
            return False
        if marker.get("layout") != LAYOUT_SCHEMA or marker.get("format") != "tar":
            return False
        if not isinstance(marker.get("archive"), str):
            return False
        if not isinstance(marker.get("members"), dict):
            return False
        if not isinstance(marker.get("roots"), list) or not marker.get("roots"):
            return False
        if not isinstance(marker.get("work"), str):
            return False
        return _declared_versions_satisfied(request, identity)

    if not entry_reusable(
        entry, kind=KIND, key=key, name=MARKER_NAME, validate=validate
    ):
        return None
    marker = _shared_read_marker(entry, kind=KIND, key=key, name=MARKER_NAME)
    if marker is None:
        return None
    return entry, marker


def snapshot_count(store: Path) -> int:
    """Return how many snapshots the store currently holds."""

    directory = store / ENTRY_DIRECTORY
    if directory.is_symlink() or not directory.is_dir():
        return 0
    return sum(
        1
        for entry in directory.iterdir()
        if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".")
    )


def prune_store(
    store: Path, max_bytes: int, *, protect: Sequence[Path] = ()
) -> dict[str, Any]:
    """Evict least-recently-used snapshots until the store fits ``max_bytes``.

    The bound is enforced before a new snapshot is published, so the store never
    holds more than the configured bound at rest.
    """

    return prune_entries(store, [ENTRY_DIRECTORY], max_bytes, protect=protect)


def _work_empty_directories(root: str) -> tuple[str, ...]:
    if not PurePosixPath(root).name.startswith("west-homebrew-lz4-"):
        return ()
    return tuple(f"{root}/{name}" for name in WORK_EMPTY_DIRECTORIES)


def _captured_roots(prefix: Path, work_directory: Path) -> list[str]:
    roots: list[str] = []
    local = prefix / CAPTURED_LOCAL_ROOT
    if local.is_dir() and not local.is_symlink():
        roots.extend(
            f"{CAPTURED_LOCAL_ROOT}/{child.name}" for child in sorted(local.iterdir())
        )
    roots.append(work_directory.relative_to(prefix).as_posix())
    return roots


def _relative_members(prefix: Path, roots: Sequence[str]) -> list[tuple[str, Path]]:
    """Return ``(archive name, host path)`` for every captured member."""

    members: list[tuple[str, Path]] = []
    empty = {candidate for root in roots for candidate in _work_empty_directories(root)}

    def walk(name: str, path: Path) -> None:
        members.append((name, path))
        if path.is_symlink() or not path.is_dir() or name in empty:
            return
        for child in sorted(path.iterdir()):
            walk(f"{name}/{child.name}", child)

    for root in roots:
        base = prefix / root
        if not (base.exists() or base.is_symlink()):
            continue
        walk(root, base)
    return members


def capture_tree(prefix: Path, roots: Sequence[str], archive: Path) -> dict[str, Any]:
    """Archive ``roots`` (prefix-relative) into ``archive``; return the manifest."""

    members: dict[str, dict[str, Any]] = {}
    with tarfile.open(archive, "w", format=tarfile.GNU_FORMAT) as tar:
        for name, path in _relative_members(prefix, roots):
            info = tar.gettarinfo(str(path), arcname=name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if info.islnk():
                # A hard link is captured as a second copy of the bytes, so the
                # manifest describes every restored file on its own.
                replacement = tarfile.TarInfo(name)
                replacement.mode = info.mode
                replacement.mtime = info.mtime
                replacement.size = path.stat().st_size
                info = replacement
            if info.isreg():
                with path.open("rb") as stream:
                    tar.addfile(info, stream)
                members[name] = {
                    "kind": "file",
                    "mode": info.mode,
                    "size": info.size,
                    "sha256": _digest_file(path),
                }
            elif info.isdir():
                tar.addfile(info)
                members[name] = {"kind": "directory", "mode": info.mode}
            elif info.issym():
                tar.addfile(info)
                members[name] = {"kind": "symlink", "target": os.readlink(path)}
            else:
                raise ValueError(f"stock stack cache: unsupported capture member: {path}")
    return members


def _safe_member(name: str) -> str:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or str(path) != name.rstrip("/")
    ):
        raise ValueError(f"stock stack cache: unsafe archive member: {name!r}")
    return str(path)


def verify_tree(root: Path, members: Mapping[str, Any]) -> list[str]:
    """Return the problems that stop ``root`` from satisfying ``members``."""

    problems: list[str] = []
    for name, recorded in members.items():
        path = root / name
        kind = recorded.get("kind")
        if kind == "symlink":
            if not path.is_symlink():
                problems.append(f"{name}: expected symlink")
            elif os.readlink(path) != recorded.get("target"):
                problems.append(f"{name}: symlink target differs")
            continue
        if path.is_symlink() or not path.exists():
            problems.append(f"{name}: missing {kind}")
            continue
        if kind == "directory":
            if not path.is_dir():
                problems.append(f"{name}: expected directory")
            continue
        if not path.is_file():
            problems.append(f"{name}: expected regular file")
            continue
        if recorded.get("sha256") != _digest_file(path):
            problems.append(f"{name}: sha256 mismatch")
    return problems


def extract_tree(
    archive: Path, destination: Path, members: Mapping[str, Any]
) -> list[str]:
    """Extract ``archive`` under ``destination`` and verify the manifest.

    The archive is validated structurally before anything is written, and the
    manifest is verified against what was written. Returns the problems found so
    the caller can reject a snapshot instead of consuming it. Nothing is written
    outside ``destination``.

    Symlink targets are preserved verbatim. A guest prefix is a guest root: brew
    links ``/usr/local/etc/openssl@3/cert.pem`` to a guest-absolute
    ``/usr/local/etc/ca-certificates/cert.pem``, which only the guest resolves,
    and rewriting it would corrupt the restored stack. What extraction must
    guarantee instead is that no member is ever written *through* a symlink, so
    a snapshot that puts a member beneath one is refused before extraction.
    """

    problems: list[str] = []
    seen: list[str] = []
    directories: list[str] = []
    regular: list[tuple[str, tarfile.TarInfo]] = []
    symlinks: list[tuple[str, str]] = []
    with tarfile.open(archive, "r") as tar:
        for info in tar.getmembers():
            name = _safe_member(info.name)
            seen.append(name)
            if info.isdir():
                directories.append(name)
            elif info.isreg():
                regular.append((name, info))
            elif info.issym():
                if not info.linkname:
                    problems.append(f"{name}: empty symlink target")
                symlinks.append((name, info.linkname))
            else:
                problems.append(f"{name}: unsupported archive member type")
        if sorted(seen) != sorted(str(name) for name in members):
            problems.append("archive membership differs from the recorded manifest")
        link_names = {name for name, _target in symlinks}
        for name in seen:
            parents = {
                str(parent)
                for parent in PurePosixPath(name).parents
                if str(parent) != "."
            }
            if parents & link_names:
                problems.append(f"{name}: archive member beneath a symlink")
        if problems:
            return problems
        for name in directories:
            (destination / name).mkdir(parents=True, exist_ok=True)
        for name, info in regular:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(info) as source:
                if source is None:
                    problems.append(f"{name}: unreadable archive member")
                    continue
                with target.open("wb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(block)
            target.chmod(info.mode & 0o777)
    for name, linkname in symlinks:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_file():
            target.unlink()
        target.symlink_to(linkname)
    problems.extend(verify_tree(destination, members))
    return problems


def source_receipts(prefix: Path, members: Mapping[str, Any]) -> dict[str, str]:
    """Return the source receipts a completed stack must still carry.

    Every stack formula that is installed must be installed from source: a
    poured bottle would make this a different stack from the one the replay
    rebuilds. The recorded digest proves the receipt survived the round trip.
    """

    receipts: dict[str, str] = {}
    cellar = prefix / CAPTURED_LOCAL_ROOT / "Cellar"
    for formula in STACK_FORMULAS:
        directory = cellar / formula
        if directory.is_symlink() or not directory.is_dir():
            continue
        for version in sorted(
            entry.name for entry in directory.iterdir() if entry.is_dir()
        ):
            receipt = directory / version / "INSTALL_RECEIPT.json"
            name = receipt.relative_to(prefix).as_posix()
            if name not in members:
                raise ValueError(
                    f"stock stack cache: {formula} receipt is not captured: {receipt}"
                )
            try:
                data = json.loads(receipt.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"stock stack cache: unreadable {formula} receipt: {error}"
                ) from error
            if not isinstance(data, dict) or data.get("poured_from_bottle") is not False:
                raise ValueError(
                    f"stock stack cache: {formula} {version} was not built from source"
                )
            receipts[name] = _digest_file(receipt)
    return receipts


def _receipt_problems(root: Path, marker: Mapping[str, Any]) -> list[str]:
    receipts = marker.get("receipts")
    if not isinstance(receipts, dict) or not receipts:
        return ["snapshot records no source receipts"]
    problems = []
    for name, digest in receipts.items():
        relative = _safe_member(str(name))
        if _digest_file(root / relative) != digest:
            problems.append(f"{relative}: source receipt missing or changed")
    return problems


def completed_work_directory(prefix: Path) -> Path | None:
    """Return the completed staged resource work directory, or ``None``.

    A capture is only attempted for a complete resource state: exactly one work
    directory under the prefix whose staged inputs agree with the pinned inputs
    and which carries both round trip markers the resource script writes.
    """

    temporary = prefix / "private/var/tmp"
    if temporary.is_symlink() or not temporary.is_dir():
        return None
    pinned = pinned_inputs()
    candidates: list[Path] = []
    for inputs in sorted(temporary.glob(f"{WORK_DIRECTORY_GLOB}/inputs.json")):
        directory = inputs.parent
        if inputs.is_symlink() or not inputs.is_file():
            continue
        try:
            recorded = json.loads(inputs.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(recorded, dict):
            continue
        if recorded.get("homebrew-core-commit") != pinned["homebrew-core-commit"]:
            continue
        staged = {
            entry.get("filename"): entry.get("sha256")
            for entry in recorded.get("inputs", [])
            if isinstance(entry, dict)
        }
        if staged != pinned["inputs"]:
            continue
        if not (directory / "install-roundtrip.ok").is_file():
            continue
        if not (directory / "reuse-roundtrip.ok").is_file():
            continue
        candidates.append(directory)
    if len(candidates) != 1:
        return None
    return candidates[0]


def capture(
    store: Path | None,
    prefix: Path,
    *,
    request: Mapping[str, Any] | None,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Capture a completed stack, or report why nothing was captured.

    Publishing order matters. The payload and its completion marker are built
    under a hidden temporary name, the bound is enforced against the measured
    size of that complete entry, and only then is the entry renamed into place.
    A killed capture therefore leaves either nothing or a hidden temporary
    directory that no reader ever consumes; it never leaves a visible entry
    without a marker, and the store is never over its bound once the capture
    returns.
    """

    if store is None:
        return {"captured": False, "reason": "disabled"}
    if request is None:
        return {"captured": False, "reason": "unknown-identity"}
    work = completed_work_directory(prefix)
    if work is None:
        return {"captured": False, "reason": "incomplete-resource"}
    max_bytes = store_max_bytes(environ)
    key = request_key(request)
    entry = entry_path(store, key)
    entry.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    # The temporary entry lives beside the bucket, not inside it: the bucket is
    # what the shared LRU prune scans, and an in-progress capture must never be
    # counted or evicted as if it were a published snapshot.
    store.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = store / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700)
    try:
        roots = _captured_roots(prefix, work)
        members = capture_tree(prefix, roots, temporary / ARCHIVE_NAME)
        receipts = source_receipts(prefix, members)
        formulas = resolved_formulas(prefix)
        identity = stack_identity(request, formulas)
        write_marker(
            temporary,
            key,
            layout=LAYOUT_SCHEMA,
            format="tar",
            archive=ARCHIVE_NAME,
            request=dict(request),
            identity=identity,
            identity_key=identity_key(identity),
            roots=roots,
            work=work.relative_to(prefix).as_posix(),
            members=members,
            receipts=receipts,
        )
        payload = entry_bytes(temporary)
        budget = max_bytes - payload
        if budget < 1:
            return {
                "captured": False,
                "reason": "snapshot-exceeds-bound",
                "bytes": payload,
                "bound": max_bytes,
            }
        # Enforce the bound before publishing: the pruned store plus this
        # complete entry has to fit, and the entry this capture replaces is
        # kept so a refused capture cannot destroy the snapshot it was
        # rebuilding.
        pruned = prune_store(store, budget, protect=[entry])
        with locked_entry(entry):
            if entry.is_symlink():
                entry.unlink()
            elif entry.exists():
                shutil.rmtree(entry)
            os.replace(temporary, entry)
            sync_directory(entry.parent)
        touch_entry(entry)
    except ValueError as error:
        # The prefix is not a complete stack for this identity -- a formula
        # being reinstalled, an unreadable receipt, an unsafe member. Capturing
        # it would publish a stack the replays must not consume.
        record_event(store, "capture_incomplete")
        return {"captured": False, "reason": "incomplete-stack", "error": str(error)}
    except OSError as error:
        # The observed prefix can change under a capture (a live guest, a full
        # disk). A cache never fails the run it is observing: it declines.
        record_event(store, "capture_failed")
        return {"captured": False, "reason": "capture-failed", "error": str(error)}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    record_event(store, "captures")
    record_event(store, "bytes_captured", payload)
    return {
        "captured": True,
        "key": key,
        "entry": entry,
        "bytes": payload,
        "work": work,
        "formulas": formulas,
        "evicted": pruned["evicted"],
    }


def restore(
    store: Path | None,
    prefix: Path,
    *,
    request: Mapping[str, Any] | None,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Restore a matching snapshot into ``prefix``, or report why it was not.

    Every failure is a miss that falls back to staging: an entry that is not
    reusable, a corrupt archive, a file whose digest does not match, a lost
    formula receipt, or a prefix that already holds paths the snapshot writes.
    """

    if store is None:
        return {"restored": False, "reason": "disabled"}
    if request is None:
        return {"restored": False, "reason": "unknown-identity"}
    found = read_entry(store, request)
    if found is None:
        record_event(store, "misses")
        return {"restored": False, "reason": "no-entry"}
    entry, marker = found
    members = marker["members"]
    roots = [str(name) for name in marker["roots"]]
    destinations = [prefix / name for name in roots]
    conflicting = [path for path in destinations if path.exists() or path.is_symlink()]
    if conflicting:
        record_event(store, "restore_rejected")
        record_event(store, "misses")
        return {
            "restored": False,
            "reason": "prefix-not-fresh",
            "problems": [str(path) for path in conflicting],
        }
    temporary = prefix / "private/var/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    staging = temporary / f"{RESTORE_PREFIX}{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        try:
            problems = extract_tree(entry / str(marker["archive"]), staging, members)
            problems.extend(_receipt_problems(staging, marker))
        except (ValueError, tarfile.TarError, OSError) as error:
            # An unreadable or malformed archive is a rejection, never a crash
            # in the middle of a run that can simply rebuild the stack.
            problems = [f"{marker.get('archive')}: {error}"]
        if problems:
            record_event(store, "restore_rejected")
            record_event(store, "misses")
            return {"restored": False, "reason": "rejected", "problems": problems}
        for name, destination in zip(roots, destinations):
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / name, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    restored_bytes = sum(
        int(recorded.get("size", 0))
        for recorded in members.values()
        if isinstance(recorded, dict)
    )
    work = prefix / str(marker["work"])
    record_event(store, "hits")
    record_event(store, "bytes_restored", restored_bytes)
    touch_entry(entry)
    return {
        "restored": True,
        "key": marker.get("key"),
        "entry": entry,
        "work": work,
        "bytes": restored_bytes,
        "bound": store_max_bytes(environ),
    }


def store_report(store: Path | None, environ: Mapping[str, str]) -> str:
    """Return one line describing the cache state for the run output."""

    if store is None:
        return f"stock stack cache: disabled by {STORE_SWITCH}"
    return (
        f"stock stack cache: enabled store={store} "
        f"bound={store_max_bytes(environ)} entries={snapshot_count(store)}"
    )


def cache_report(store: Path | None, environ: Mapping[str, str]) -> str:
    """Return the store line plus its byte and hit, miss, eviction accounting."""

    if store is None:
        return store_report(store, environ)
    stats = read_stats(store)
    return (
        f"{store_report(store, environ)} bytes={entry_bytes(store)} "
        f"captured={stats.value('captures')} hits={stats.value('hits')} "
        f"misses={stats.value('misses')} "
        f"evicted={stats.value('entries_evicted')} "
        f"rejected={stats.value('restore_rejected')} "
        f"bytes_captured={stats.value('bytes_captured')} "
        f"bytes_restored={stats.value('bytes_restored')} "
        f"bytes_evicted={stats.value('bytes_evicted')}"
    )


def _guest_path(prefix: Path, path: Path) -> str:
    return "/" + path.relative_to(prefix).as_posix()


@contextlib.contextmanager
def stock_stack_context(
    command: Any,
    invocation: Mapping[str, Any],
    env: Mapping[str, str] | None,
) -> Iterator[dict[str, str] | None]:
    """Restore-or-stage the stock Homebrew resource for one test invocation.

    Resource setup restores a matching snapshot and skips staging entirely. A
    miss, a rejection or a disabled cache stages the resource exactly as before
    and captures the resulting stack at teardown, when the state is complete.

    A miss on a prefix that already holds an installation is not staged: staging
    refuses that prefix by design, and the stack that is already there is what a
    replay phase consumes. It is adopted instead, and captured so the next run
    can restore it. A test whose subject is the from-source build itself declares
    ``WEST_STOCK_STACK_RESTORE=off`` and always stages, because a snapshot must
    never stand in for the build that test is measuring.
    """

    try:
        from .test_homebrew import conflicting_homebrew, homebrew_lz4_context
    except ImportError:
        from test_homebrew import conflicting_homebrew, homebrew_lz4_context

    environment = dict(os.environ if env is None else env)
    # A declared environment participates in cache decisions so a test can opt
    # out or point at another store without discarding the operator's global
    # switch: it overlays the host environment instead of replacing it.
    cache_environment = {**os.environ, **dict(env or {})}
    log = getattr(command, "inf", print)
    prefix_text = environment.get("DPREFIX")
    if not prefix_text:
        raise ValueError("homebrew-lz4 requires West DPREFIX and DARLING_LAUNCHER")
    prefix = Path(prefix_text)
    manifest_repo = Path(
        getattr(getattr(command, "manifest", None), "repo_abspath", Path.cwd())
    )
    topdir = Path(getattr(command, "topdir", manifest_repo))
    store = store_root(manifest_repo, cache_environment)
    request = None
    if store is not None and prefix.is_dir():
        request = stack_request(
            prefix=prefix,
            manifest_repo=manifest_repo,
            topdir=topdir,
            # A metadata test carries no runtime_profile here, so the prefix's
            # retained fingerprint is the identity. A caller that does name a
            # deployed profile gets no request, because the marker records the
            # bootstrap provider and the two never match.
            profile_name=invocation.get("runtime_profile"),
            environ=cache_environment,
        )

    @contextlib.contextmanager
    def staged_stack() -> Iterator[dict[str, str] | None]:
        with homebrew_lz4_context(environment) as staged:
            yield staged
        try:
            outcome = capture(store, prefix, request=request, environ=cache_environment)
        except Exception as error:  # noqa: BLE001 - the observed run already succeeded
            log(f"stock stack cache: not captured reason=error {error!r}")
            return
        if outcome["captured"]:
            log(
                f"stock stack cache: captured key={str(outcome['key'])[:12]} "
                f"bytes={outcome['bytes']} {cache_report(store, cache_environment)}"
            )
        else:
            log(
                f"stock stack cache: not captured reason={outcome['reason']} "
                f"{outcome.get('error', '')} {cache_report(store, cache_environment)}"
            )

    if store is None:
        log(store_report(store, cache_environment))
    elif request is None:
        log(
            "stock stack cache: no deployed-runtime identity for "
            f"profile={invocation.get('runtime_profile')!r}; staging from source"
        )
    else:
        log(cache_report(store, cache_environment))
    if request is None:
        with staged_stack() as staged:
            yield staged
        return
    if store_restore_disabled(cache_environment):
        log(
            "stock stack cache: restore disabled by declaration; staging from source "
            "so this run measures the build it declares"
        )
        with staged_stack() as staged:
            yield staged
        return
    try:
        restored = restore(store, prefix, request=request, environ=cache_environment)
    except Exception as error:  # noqa: BLE001 - a cache never fails the run it serves
        log(f"stock stack cache: restore error {error!r}; staging from source")
        restored = {"restored": False, "reason": "error"}
    if restored["restored"]:
        work = restored["work"]
        log(
            f"stock stack cache: hit key={str(restored['key'])[:12]} "
            f"work={work} bytes={restored['bytes']} {cache_report(store, cache_environment)}"
        )
        restored_env = dict(environment)
        restored_env["DARLING_HOMEBREW_LZ4_WORK"] = _guest_path(prefix, work)
        restored_env["DARLING_HOMEBREW_LZ4_INPUTS"] = str(work / "inputs.json")
        restored_env["DARLING_HOMEBREW_LZ4_RESTORED"] = "1"
        yield restored_env
        return
    log(
        f"stock stack cache: miss reason={restored['reason']} "
        f"{cache_report(store, cache_environment)}"
    )
    conflict = conflicting_homebrew(prefix) if prefix.is_dir() else None
    if conflict is not None:
        # Staging would refuse this prefix, and the stack it already holds is the
        # one the replay phase is about to consume. Adopt it and capture it.
        work = completed_work_directory(prefix)
        adopted = dict(environment)
        if work is not None:
            adopted["DARLING_HOMEBREW_LZ4_WORK"] = _guest_path(prefix, work)
            adopted["DARLING_HOMEBREW_LZ4_INPUTS"] = str(work / "inputs.json")
        log(
            f"stock stack cache: adopting the installation already present "
            f"({conflict}); not staging"
        )
        try:
            outcome = capture(store, prefix, request=request, environ=cache_environment)
        except Exception as error:  # noqa: BLE001 - the observed run already succeeded
            log(f"stock stack cache: not captured reason=error {error!r}")
        else:
            if outcome["captured"]:
                log(
                    f"stock stack cache: captured key={str(outcome['key'])[:12]} "
                    f"bytes={outcome['bytes']} {cache_report(store, cache_environment)}"
                )
            else:
                log(
                    f"stock stack cache: not captured reason={outcome['reason']} "
                    f"{outcome.get('error', '')} {cache_report(store, cache_environment)}"
                )
        yield adopted
        return
    with staged_stack() as staged:
        yield staged

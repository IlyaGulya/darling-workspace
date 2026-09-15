#!/usr/bin/env python3
"""Prove the stock-stack snapshot cache restores identity-matched stacks.

The cache only pays for itself if its mechanics are real: a capture has to
archive a prefix subtree, a restore has to reproduce it byte for byte, and every
identity input has to force a miss. This contract exercises exactly that on a
synthetic prefix, with real tar archiving and extraction rather than mocks, plus
the store bound, the accounting and the kill switch.

Two deliberately broken arms must fail the same exercise, and the contract
reports that failure instead of hiding it:

* ``WEST_STOCK_STACK_CONTRACT_ARM=broken`` -- capture publishes the payload but
  never writes the completion marker, the pre-change behaviour in which a
  partially published snapshot could be consumed;
* ``WEST_STOCK_STACK_CONTRACT_ARM=disabled`` -- the cache is switched off, the
  pre-change behaviour in which nothing is captured at all.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from pathlib import PurePosixPath
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import test_stock_stack_cache as cache  # noqa: E402

PROFILE = "stock-stack-contract"
FINGERPRINT = {
    "schema": 2,
    "profile": PROFILE,
    "source-profile": "fixture",
    "source-lock-sha256": "a" * 64,
    "source-commits": {"darling": "b" * 40},
    "patchsets": [{"profile": "fixture", "sha256": "c" * 64, "patches": []}],
    "runtime-manifest-sha256": "d" * 64,
    "runtime-profile-definition-sha256": "e" * 64,
    "launcher-sha256": "f" * 64,
}
CLT_RECEIPT = {"package": "Command_Line_Tools_for_Xcode_13.2", "sha256": "1" * 64}
FORMULAS = (("lz4", "1.10.0"), ("cmake", "3.31.6"), ("wget", "1.25.0"))
WORK_TOKEN = "fixture0001"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def tree_bytes(path: Path) -> int:
    """Return the on-disk size of a store entry, as the bound accounts for it."""

    total = 0
    for candidate in path.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        total += candidate.stat().st_size
    return total


def build_prefix(
    root: Path, *, work_token: str = WORK_TOKEN, ballast: int = 0, stack: bool = True
) -> Path:
    """Build a small stand-in for a bootstrapped prefix, with the stack staged."""

    prefix = root / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "darling").write_text("#!/bin/sh\nexit 0\n")
    write_json(
        prefix / ".west-runtime-profile.json",
        {"schema": 2, "profile": PROFILE, "source-profile": "fixture", "fingerprint": FINGERPRINT},
    )
    write_json(prefix / ".west-command-line-tools.json", CLT_RECEIPT)
    developer = prefix / "Library/Developer"
    (developer / "CommandLineTools.apple-clt-13.2/usr/bin").mkdir(parents=True)
    (developer / "CommandLineTools.apple-clt-13.2/usr/bin/clang").write_text("clang\n")
    (developer / "CommandLineTools").symlink_to("CommandLineTools.apple-clt-13.2")
    if stack:
        materialize_stack(prefix, work_token=work_token, ballast=ballast)
    return prefix


def materialize_stack(prefix: Path, *, work_token: str, ballast: int = 0) -> Path:
    """Stage the pinned resource and install the source-built stack into a prefix."""

    brew = prefix / "usr/local/Homebrew/bin/brew"
    brew.parent.mkdir(parents=True)
    brew.write_text("#!/bin/sh\necho fixture brew\n")
    brew.chmod(0o755)
    for name, version in FORMULAS:
        version_dir = prefix / f"usr/local/Cellar/{name}/{version}"
        (version_dir / "bin").mkdir(parents=True)
        write_json(
            version_dir / "INSTALL_RECEIPT.json",
            {
                "formula": name,
                "poured_from_bottle": False,
                "built_as_bottle": False,
                "time": 1,
            },
        )
        binary = version_dir / "bin" / name
        binary.write_text(f"#!/bin/sh\necho {name} {version}\n")
        binary.chmod(0o755)
    (prefix / "usr/local/opt").mkdir()
    (prefix / "usr/local/opt/lz4").symlink_to("../Cellar/lz4/1.10.0")
    (prefix / "usr/local/bin").mkdir()
    (prefix / "usr/local/bin/brew").symlink_to("../Homebrew/bin/brew")
    (prefix / "usr/local/bin/lz4").symlink_to("../Cellar/lz4/1.10.0/bin/lz4")
    # brew links a guest-absolute path: inside the guest this resolves to the
    # guest's own /usr/local, and the prefix layout makes it correct again.
    (prefix / "usr/local/etc/ca-certificates").mkdir(parents=True)
    (prefix / "usr/local/etc/ca-certificates/cert.pem").write_text("certificates\n")
    (prefix / "usr/local/etc/openssl@3").mkdir()
    (prefix / "usr/local/etc/openssl@3/cert.pem").symlink_to(
        "/usr/local/etc/ca-certificates/cert.pem"
    )
    if ballast:
        shares = prefix / "usr/local/share"
        shares.mkdir()
        (shares / "ballast.bin").write_bytes(b"B" * ballast)

    work = prefix / "private/var/tmp" / f"west-homebrew-lz4-{work_token}"
    (work / "home").mkdir(parents=True)
    write_json(
        work / "inputs.json",
        {
            "brew-commit": cache.BREW_COMMIT,
            "homebrew-core-commit": cache.CORE_COMMIT,
            "inputs": [vars(spec) for spec in cache.INPUTS],
            "lower-template-sha256": "0" * 64,
        },
    )
    (work / "install-roundtrip.ok").write_text("install\n")
    (work / "reuse-roundtrip.ok").write_text("reuse\n")
    (work / "original.bin").write_bytes(bytes(range(256)) * 4)
    # Build scratch, guest logs and the brew download cache are not stack state.
    (work / "tmp/build").mkdir(parents=True)
    (work / "tmp/build/scratch.o").write_bytes(b"scratch" * 1024)
    (work / "logs/lz4").mkdir(parents=True)
    (work / "logs/lz4/01.build").write_text("guest log\n")
    (work / "cache/downloads").mkdir(parents=True)
    (work / "cache/downloads" / "lz4-1.10.0.tar.gz").write_bytes(b"tarball" * 512)
    return work


def synthetic_request(root: Path, prefix: Path) -> dict:
    request = cache.stack_request(
        prefix=prefix,
        manifest_repo=root / "manifest",
        topdir=root,
        profile_name=PROFILE,
        environ={},
    )
    assert request is not None, (
        "a prefix with a retained runtime marker and a launcher must yield an identity"
    )
    return request


@contextlib.contextmanager
def suppressed_completion_marker(module):
    """RED arm: publish the payload but never write the completion marker."""

    original = module.write_marker

    def skip(*_args, **_kwargs):
        return None

    module.write_marker = skip
    try:
        yield
    finally:
        module.write_marker = original


def rewrite_member(archive: Path, name: str, payload: bytes) -> None:
    """Replace one archive member's bytes, leaving the manifest stale."""

    replacement = archive.with_suffix(".rewritten")
    with tarfile.open(archive, "r") as source, tarfile.open(replacement, "w") as target:
        members = source.getmembers()
        for info in members:
            if info.isreg():
                with source.extractfile(info) as data:
                    body = data.read() if data is not None else b""
                if info.name == name:
                    body = payload
                info.size = len(body)
                target.addfile(info, io.BytesIO(body))
            else:
                target.addfile(info)
    replacement.replace(archive)


def exercise(
    module,
    workspace: Path,
    *,
    environ: dict,
    suppress_marker: bool,
) -> None:
    manifest = workspace / "manifest"
    manifest.mkdir(parents=True)
    store = module.store_root(manifest, environ)
    assert store is not None, (
        "a completed capture must publish a snapshot into an enabled store"
    )

    first_prefix = build_prefix(workspace / "first")
    request = synthetic_request(workspace, first_prefix)
    assert synthetic_request(workspace, first_prefix) == request, (
        "the same prefix state must produce the same identity inputs"
    )
    assert not store.exists(), "resolving the store must not create it"

    # Identity: stable for the same inputs, and every input invalidates it.
    identity = module.stack_identity(request, {"lz4": ["1.10.0"]})
    base = module.identity_key(identity)
    assert module.identity_key(module.stack_identity(request, {"lz4": ["1.10.0"]})) == base
    assert module.identity_key(module.stack_identity(request, {"lz4": ["1.10.1"]})) != base, (
        "a resolved formula version must invalidate the snapshot identity"
    )
    assert module.identity_key(module.stack_identity(request, {"lz4": ["1.10.0"], "wget": ["1.25.0"]})) != base, (
        "the formula set must invalidate the snapshot identity"
    )
    request_key = module.request_key(request)
    mutations = {
        "layout": {"layout": module.LAYOUT_SCHEMA + 1},
        "arch": {"arch": "aarch64"},
        "homebrew-core commit": {
            "pinned": {**request["pinned"], "homebrew-core-commit": "0" * 40}
        },
        "brew commit": {"pinned": {**request["pinned"], "brew-commit": "1" * 40}},
        "input digest": {
            "pinned": {
                **request["pinned"],
                "inputs": {**request["pinned"]["inputs"], "lz4-1.10.0.tar.gz": "2" * 64},
            }
        },
        "portable ruby version": {
            "pinned": {**request["pinned"], "portable-ruby-version": "4.0.5"}
        },
        "declared formula version": {"declared-formulas": {"lz4": ["1.10.2"]}},
        "guest toolchain": {
            "guest-toolchain": {**request["guest-toolchain"], "receipt-sha256": "3" * 64}
        },
        "runtime": {"runtime": "4" * 64},
    }
    for label, mutation in mutations.items():
        candidate = {**request, **mutation}
        assert module.request_key(candidate) != request_key, (
            f"a changed {label} must invalidate the snapshot request"
        )
        assert module.identity_key(module.stack_identity(candidate, {"lz4": ["1.10.0"]})) != base, (
            f"a changed {label} must invalidate the snapshot identity"
        )
    # The snapshot schema itself is part of the key, so a format change cannot
    # restore an older layout.
    original_schema = module.SCHEMA
    try:
        module.SCHEMA = original_schema + 1
        assert module.identity_key(identity) != base, (
            "a snapshot schema change must invalidate the identity"
        )
        assert module.request_key(request) != request_key, (
            "a snapshot schema change must invalidate the request"
        )
    finally:
        module.SCHEMA = original_schema
    assert module.request_key(request) == request_key
    # A prefix that retains a provider is keyed by it, even when the caller has
    # no profile name -- a guest-runtime-script invocation carries none.
    nameless = cache.stack_request(
        prefix=first_prefix,
        manifest_repo=manifest,
        topdir=workspace,
        profile_name=None,
        environ={},
    )
    assert nameless is not None, (
        "a prefix retaining a runtime provider must key without a named profile"
    )
    assert nameless["runtime"] == request["runtime"]
    # A caller naming a different provider than the prefix retains reuses
    # nothing at all.
    assert (
        cache.stack_request(
            prefix=first_prefix,
            manifest_repo=manifest,
            topdir=workspace,
            profile_name="some-other-provider",
            environ={},
        )
        is None
    ), "a named profile the prefix does not retain must not be keyed"
    # A prefix with no retained marker and no resolvable definition cannot be
    # keyed, so its stack is never reused.
    adrift = build_prefix(workspace / "adrift", stack=False)
    (adrift / ".west-runtime-profile.json").unlink()
    assert (
        cache.stack_request(
            prefix=adrift,
            manifest_repo=manifest,
            topdir=workspace,
            profile_name=None,
            environ={},
        )
        is None
    ), "an unknown deployed runtime must not be keyed"

    # Capture: archive the synthetic stack and publish it atomically.
    context = (
        suppressed_completion_marker(module) if suppress_marker else contextlib.nullcontext()
    )
    with context:
        outcome = module.capture(store, first_prefix, request=request, environ=environ)
    assert outcome["captured"], outcome
    first_bytes = outcome["bytes"]
    entry = module.entry_path(store, module.request_key(request))
    assert (entry / module.ARCHIVE_NAME).is_file(), "a capture must publish its archive"
    marker = module.read_marker(entry, module.request_key(request))
    assert marker is not None, (
        "a capture must publish a completion marker naming this identity"
    )
    assert marker["identity"]["formulas"] == {
        "lz4": ["1.10.0"],
        "cmake": ["3.31.6"],
        "wget": ["1.25.0"],
    }, marker["identity"]
    assert marker["identity_key"] == module.identity_key(marker["identity"])
    members = marker["members"]
    work_name = f"private/var/tmp/west-homebrew-lz4-{WORK_TOKEN}"
    assert marker["work"] == work_name
    for name in (
        "usr/local/Cellar/lz4/1.10.0/INSTALL_RECEIPT.json",
        "usr/local/Cellar/cmake/3.31.6/INSTALL_RECEIPT.json",
        "usr/local/Cellar/wget/1.25.0/INSTALL_RECEIPT.json",
        f"{work_name}/inputs.json",
        f"{work_name}/reuse-roundtrip.ok",
    ):
        assert name in members, f"{name} must be archived"
        assert len(members[name]["sha256"]) == 64, members[name]
    assert members["usr/local/opt/lz4"]["kind"] == "symlink"
    assert members[f"{work_name}/cache/downloads"]["kind"] == "directory"
    assert f"{work_name}/cache/downloads/lz4-1.10.0.tar.gz" not in members, (
        "brew's download cache is not stack state"
    )
    assert f"{work_name}/tmp/build/scratch.o" not in members, (
        "guest build scratch is not stack state"
    )
    assert marker["receipts"], "a capture must record its source receipts"
    assert not list(store.glob(f"{module.TEMP_PREFIX}*")), (
        "publishing a snapshot must not leave its temporary entry behind"
    )
    assert module.read_entry(store, request) is not None, (
        "a captured snapshot must be reusable for its own identity"
    )

    # A partial capture is never consumed.
    module.marker_path(entry).unlink()
    assert module.read_entry(store, request) is None
    refused = module.restore(store, workspace / "unused", request=request, environ=environ)
    assert refused["restored"] is False and refused["reason"] == "no-entry", refused
    with context:
        module.capture(store, first_prefix, request=request, environ=environ)
    assert module.read_entry(store, request) is not None

    # A stack in flux is not a complete state and is never captured. Observed
    # for real: a live crash-reproduction run was reinstalling wget, so its
    # version directory existed without a receipt while the resource work
    # directory still carried both round trip markers.
    flux = build_prefix(workspace / "flux")
    (flux / "usr/local/Cellar/wget/1.25.0/INSTALL_RECEIPT.json").unlink()
    flux_request = {**request, "runtime": "8" * 64}
    refused = module.capture(store, flux, request=flux_request, environ=environ)
    assert refused["captured"] is False and refused["reason"] == "incomplete-stack", refused
    assert module.read_entry(store, flux_request) is None
    assert module.read_stats(store).value("capture_incomplete") >= 1
    assert not list(store.glob(f"{module.TEMP_PREFIX}*")), (
        "a refused capture must not leave a temporary entry behind"
    )

    # A resource that never finished its round trips is not captured either.
    unfinished = build_prefix(workspace / "unfinished")
    (unfinished / f"private/var/tmp/west-homebrew-lz4-{WORK_TOKEN}/reuse-roundtrip.ok").unlink()
    refused = module.capture(
        store, unfinished, request={**request, "runtime": "9" * 64}, environ=environ
    )
    assert refused["captured"] is False and refused["reason"] == "incomplete-resource", (
        refused
    )

    # Extraction never writes a member through a symlink, whatever the archive
    # claims: a snapshot that puts one beneath a link is refused, not merged.
    hostile = workspace / "hostile.tar"
    under_link = {
        "usr/local/opt/lz4": {"kind": "symlink", "target": ".."},
        "usr/local/opt/lz4/bin/lz4": {"kind": "file", "mode": 0o755, "size": 3,
                                      "sha256": "0" * 64},
    }
    with tarfile.open(hostile, "w") as tar:
        link = tarfile.TarInfo("usr/local/opt/lz4")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../.."
        tar.addfile(link)
        payload = tarfile.TarInfo("usr/local/opt/lz4/bin/lz4")
        payload.size = 3
        tar.addfile(payload, io.BytesIO(b"bad"))
    target = workspace / "hostile-extract"
    target.mkdir()
    problems = cache.extract_tree(hostile, target, under_link)
    assert any("beneath a symlink" in problem for problem in problems), problems

    # Restore into a fresh prefix and verify the stack came back byte for byte.
    fresh = workspace / "fresh"
    fresh.mkdir()
    restored = module.restore(store, fresh, request=request, environ=environ)
    assert restored["restored"], restored
    assert restored["work"] == fresh / work_name, restored["work"]
    assert restored["bytes"] > 0 and restored["bound"] == module.store_max_bytes(environ)
    for name, version in FORMULAS:
        receipt = fresh / f"usr/local/Cellar/{name}/{version}/INSTALL_RECEIPT.json"
        assert json.loads(receipt.read_text())["poured_from_bottle"] is False
        binary = fresh / f"usr/local/Cellar/{name}/{version}/bin/{name}"
        assert binary.is_file() and binary.stat().st_mode & 0o111, binary
    assert (fresh / "usr/local/opt/lz4").is_symlink()
    assert os.readlink(fresh / "usr/local/opt/lz4") == "../Cellar/lz4/1.10.0"
    assert os.readlink(fresh / "usr/local/bin/brew") == "../Homebrew/bin/brew"
    guest_link = fresh / "usr/local/etc/openssl@3/cert.pem"
    assert guest_link.is_symlink() and os.readlink(guest_link) == (
        "/usr/local/etc/ca-certificates/cert.pem"
    ), (
        "a guest-absolute symlink must be restored verbatim, not rewritten"
    )
    assert (fresh / "usr/local/etc/ca-certificates/cert.pem").read_text() == "certificates\n"
    # The rule this fixture exists for: an absolute target is guest-absolute,
    # so a host-side "escaping symlink" test would corrupt a real stack.
    assert PurePosixPath(os.readlink(guest_link)).is_absolute()
    assert (fresh / work_name / "reuse-roundtrip.ok").is_file()
    assert (fresh / work_name / "original.bin").read_bytes() == (
        first_prefix / work_name / "original.bin"
    ).read_bytes()
    assert (fresh / work_name / "cache/downloads").is_dir(), (
        "the restored work directory keeps the directories staging needs"
    )
    assert not (fresh / work_name / "cache/downloads/lz4-1.10.0.tar.gz").exists()
    assert not list(fresh.glob("private/var/tmp/.west-stock-restore-*")), (
        "a restore must not leave its staging directory behind"
    )
    stats = module.read_stats(store)
    assert stats.value("hits") == 1 and stats.value("captures") == 2, stats.counters
    assert stats.value("bytes_restored") > 0

    # A prefix that already holds the snapshot's paths is left alone.
    occupied = module.restore(store, fresh, request=request, environ=environ)
    assert occupied["restored"] is False and occupied["reason"] == "prefix-not-fresh"
    assert module.read_stats(store).value("restore_rejected") >= 1

    # Corruption: a changed member body must be refused, not consumed.
    archive = entry / module.ARCHIVE_NAME
    original_archive = archive.read_bytes()
    rewrite_member(archive, f"{work_name}/original.bin", b"tampered\n")
    corrupt = workspace / "corrupt"
    corrupt.mkdir()
    rejected = module.restore(store, corrupt, request=request, environ=environ)
    assert rejected["restored"] is False and rejected["reason"] == "rejected", rejected
    assert any("sha256" in problem for problem in rejected["problems"]), rejected["problems"]
    archive.write_bytes(original_archive)
    assert module.restore(store, corrupt, request=request, environ=environ)["restored"], (
        "restoring the repair must succeed on the pristine archive"
    )
    # A truncated archive is a rejection, not a crash.
    archive.write_bytes(original_archive[: len(original_archive) // 2])
    truncated = workspace / "truncated"
    truncated.mkdir()
    assert module.restore(store, truncated, request=request, environ=environ)["restored"] is False
    archive.write_bytes(original_archive)

    # A lost source receipt is refused even when the manifest still matches.
    marker_file = module.marker_path(entry)
    intact_marker = marker_file.read_bytes()
    tampered = json.loads(intact_marker)
    receipt_name = next(iter(tampered["receipts"]))
    tampered["receipts"][receipt_name] = "9" * 64
    marker_file.write_text(json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    missing_receipt = workspace / "missing-receipt"
    missing_receipt.mkdir()
    rejected = module.restore(store, missing_receipt, request=request, environ=environ)
    assert rejected["restored"] is False and rejected["reason"] == "rejected", rejected
    assert any("source receipt" in problem for problem in rejected["problems"]), rejected
    marker_file.write_bytes(intact_marker)
    shutil.rmtree(missing_receipt)

    # A different identity never reaches this entry.
    other_runtime = {**request, "runtime": "5" * 64}
    other = workspace / "other-identity"
    other.mkdir()
    missed = module.restore(store, other, request=other_runtime, environ=environ)
    assert missed["restored"] is False and missed["reason"] == "no-entry", missed
    assert module.read_stats(store).value("misses") >= 1

    # A snapshot that does not fit the bound is refused before it is published.
    tiny = {**environ, "WEST_STOCK_STACK_CACHE_MAX_BYTES": "1"}
    oversized = module.capture(store, first_prefix, request=request, environ=tiny)
    assert oversized["captured"] is False and oversized["reason"] == "snapshot-exceeds-bound", (
        oversized
    )

    # Grow the store with a genuinely different, larger stack.
    second_prefix = build_prefix(
        workspace / "second", work_token="fixture0002", ballast=1 << 20
    )
    second_request = {
        **synthetic_request(workspace, second_prefix),
        "runtime": "6" * 64,
    }
    second_key = module.request_key(second_request)
    assert second_key != request_key
    roomy = {**environ, "WEST_STOCK_STACK_CACHE_MAX_BYTES": str(8 << 20)}
    added = module.capture(store, second_prefix, request=second_request, environ=roomy)
    assert added["captured"] and added["evicted"] == [], added
    second_entry = module.entry_path(store, second_key)
    assert added["bytes"] > first_bytes, (
        "the second stack must be the larger one so eviction is observable"
    )

    # The bound is enforced before the new snapshot is added: a bound equal to
    # both payloads admits a third stack only by dropping the least recently
    # used one, and afterwards the store provably fits without further pruning.
    bound = tree_bytes(entry) + tree_bytes(second_entry)
    bounded = {**environ, "WEST_STOCK_STACK_CACHE_MAX_BYTES": str(bound)}
    stale = time.time() - 600
    os.utime(entry, (stale, stale))
    third_prefix = build_prefix(workspace / "third", work_token="fixture0003")
    third_request = {
        **synthetic_request(workspace, third_prefix),
        "runtime": "7" * 64,
    }
    third_key = module.request_key(third_request)
    assert third_key not in (request_key, second_key)
    added_third = module.capture(store, third_prefix, request=third_request, environ=bounded)
    assert added_third["captured"], added_third
    assert added_third["evicted"] == [f"{module.ENTRY_DIRECTORY}/{request_key}"], (
        added_third
    )
    assert not entry.exists(), "the least recently used snapshot must be evicted"
    assert second_entry.is_dir(), "the snapshot just added must survive"
    assert module.prune_store(store, bound)["evicted"] == [], (
        "the bound must be enforced before the snapshot is added, not after"
    )
    evicted_stats = module.read_stats(store)
    assert evicted_stats.value("entries_evicted") >= 1
    assert evicted_stats.value("bytes_evicted") >= first_bytes
    assert module.read_entry(store, request) is None, (
        "an evicted snapshot must miss instead of being partially restored"
    )

    report = module.cache_report(store, bounded)
    for field in (
        "store=",
        "bound=",
        "bytes=",
        "hits=",
        "misses=",
        "evicted=",
        "captured=",
        "rejected=",
        "bytes_captured=",
        "bytes_restored=",
    ):
        assert field in report, report
    assert "disabled" in module.store_report(None, bounded)

    # The kill switch disables the cache and says so in the run output.
    assert module.store_root(manifest, {"WEST_STOCK_STACK_CACHE": "off"}) is None
    assert module.store_root(manifest, {"WEST_STOCK_STACK_CACHE": "0"}) is None
    assert module.capture(None, first_prefix, request=request, environ=environ)["reason"] == "disabled"
    assert module.restore(None, first_prefix, request=request, environ=environ)["reason"] == "disabled"
    assert "disabled" in module.store_report(None, environ)
    assert module.store_report(None, environ) in module.cache_report(None, environ)
    for bad in ("relative/store",):
        try:
            module.store_root(manifest, {"WEST_STOCK_STACK_CACHE_DIR": bad})
        except ValueError:
            continue
        raise AssertionError("a relative store root must be rejected")
    assert module.store_max_bytes({}) == module.DEFAULT_MAX_BYTES
    for bad in ("many", "0", "-1"):
        try:
            module.store_max_bytes({"WEST_STOCK_STACK_CACHE_MAX_BYTES": bad})
        except ValueError:
            continue
        raise AssertionError(f"a bound of {bad!r} must be rejected")

    exercise_provider(module, workspace, environ)


class Command:
    """Minimal stand-in for the command object a resource provider receives."""

    def __init__(self, manifest: Path) -> None:
        self.manifest = SimpleNamespace(repo_abspath=str(manifest))
        self.topdir = manifest
        self.lines: list[str] = []

    def inf(self, message: str) -> None:
        self.lines.append(str(message))


@contextlib.contextmanager
def applied_environ(values: dict):
    """Apply the cache environment the provider reads from ``os.environ``."""

    keys = (
        "WEST_STOCK_STACK_CACHE",
        "WEST_STOCK_STACK_CACHE_DIR",
        "WEST_STOCK_STACK_CACHE_MAX_BYTES",
    )
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    os.environ.update({key: str(value) for key, value in values.items() if key in keys})
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def exercise_provider(module, workspace: Path, environ: dict) -> None:
    """Prove resource setup restores-or-stages and teardown captures.

    The provider registry, the restore-or-stage decision, the archive, the
    marker, the bound and the accounting are the real implementations. Only the
    staging step itself is replaced, because it downloads pinned inputs and
    builds inside a guest; the double performs the host-visible effects a
    completed run leaves behind.
    """

    import test_homebrew  # noqa: PLC0415
    import test_resources  # noqa: PLC0415

    assert test_resources.active_resource_provider_names(
        {"requires_resources": ["homebrew-lz4"]}
    ) == ["homebrew-lz4"], "the homebrew-lz4 resource must stay registered"

    manifest = workspace / "provider-manifest"
    manifest.mkdir()
    store_env = {
        **environ,
        "WEST_STOCK_STACK_CACHE_DIR": str(workspace / "provider-store"),
    }
    staging_calls: list[dict] = []

    @contextlib.contextmanager
    def staging_double(environment):
        staging_calls.append(dict(environment))
        prefix = Path(environment["DPREFIX"])
        # The real context refuses a prefix that is not fresh. Reproducing that
        # refusal keeps a contract arm from passing by staging over an
        # installation the production path would have rejected.
        existing = prefix / "usr/local/Homebrew"
        if existing.exists() or existing.is_symlink():
            raise ValueError(f"homebrew-lz4 requires a fresh installation: {existing}")
        work = materialize_stack(prefix, work_token="staged0001")
        yield {
            **environment,
            "DARLING_HOMEBREW_LZ4_WORK": "/" + work.relative_to(prefix).as_posix(),
            "DARLING_HOMEBREW_LZ4_INPUTS": str(work / "inputs.json"),
        }

    original = test_homebrew.homebrew_lz4_context
    test_homebrew.homebrew_lz4_context = staging_double
    # A guest-runtime-script invocation carries no runtime profile; the prefix's
    # retained provider is the identity the cache keys on.
    invocation = {
        "name": "stock_stack_provider_contract",
        "requires_resources": ["homebrew-lz4"],
    }
    try:
        with applied_environ(store_env):
            store = module.store_root(manifest, os.environ)
            assert store is not None

            cold = build_prefix(workspace / "cold", stack=False)
            cold_command = Command(manifest)
            cold_env = {
                "DPREFIX": str(cold),
                "DARLING_LAUNCHER": str(cold / "bin/darling"),
            }
            with test_resources.resource_context(
                cold_command, invocation, cold_env
            ) as staged:
                assert staged is not None and "DARLING_HOMEBREW_LZ4_WORK" in staged
                assert "DARLING_HOMEBREW_LZ4_RESTORED" not in staged, (
                    "a cold run must stage the resource"
                )
            assert len(staging_calls) == 1
            cold_request = synthetic_request(workspace, cold)
            assert module.read_entry(store, cold_request) is not None, (
                "resource teardown must capture the completed stack"
            )
            assert module.read_stats(store).value("captures") == 1
            assert any("captured" in line for line in cold_command.lines), cold_command.lines
            assert any("enabled" in line for line in cold_command.lines), cold_command.lines

            warm = build_prefix(workspace / "warm", stack=False)
            warm_command = Command(manifest)
            warm_env = {
                "DPREFIX": str(warm),
                "DARLING_LAUNCHER": str(warm / "bin/darling"),
            }
            with test_resources.resource_context(
                warm_command, invocation, warm_env
            ) as restored:
                assert restored is not None
                assert restored.get("DARLING_HOMEBREW_LZ4_RESTORED") == "1", restored
                assert restored["DARLING_HOMEBREW_LZ4_WORK"].endswith(
                    "west-homebrew-lz4-staged0001"
                ), restored
                restored_work = warm / "private/var/tmp/west-homebrew-lz4-staged0001"
                assert (restored_work / "reuse-roundtrip.ok").is_file()
                assert (warm / "usr/local/Cellar/lz4/1.10.0/bin/lz4").is_file()
                assert (warm / "usr/local/Homebrew/bin/brew").is_file()
            assert len(staging_calls) == 1, "a cache hit must not stage the resource"
            assert any("hit" in line for line in warm_command.lines), warm_command.lines
            assert module.read_stats(store).value("hits") == 1

            # A run that names a provider the prefix does not retain must build
            # from source instead of restoring another provider's stack.
            mismatched = build_prefix(workspace / "mismatched", stack=False)
            mismatch_command = Command(manifest)
            staging_before = len(staging_calls)
            with test_resources.resource_context(
                mismatch_command,
                {**invocation, "runtime_profile": "some-other-provider"},
                {
                    "DPREFIX": str(mismatched),
                    "DARLING_LAUNCHER": str(mismatched / "bin/darling"),
                },
            ) as staged:
                assert staged is not None
                assert "DARLING_HOMEBREW_LZ4_RESTORED" not in staged, (
                    "a mismatched runtime profile must not restore"
                )
            assert len(staging_calls) == staging_before + 1
            assert any("no deployed-runtime identity" in line for line in mismatch_command.lines), (
                mismatch_command.lines
            )

            # A prefix that already holds an installation is adopted, not staged
            # over: staging refuses that prefix by design, and the stack already
            # there is the one a replay phase consumes. It is captured so the
            # next run can restore it.
            adopted = build_prefix(workspace / "adopted", stack=True)
            # A different deployed-runtime identity so the restore misses: this arm
            # is about what happens when a snapshot cannot serve the invocation.
            write_json(
                adopted / ".west-runtime-profile.json",
                {
                    "schema": 2,
                    "profile": PROFILE,
                    "source-profile": "fixture",
                    "fingerprint": {**FINGERPRINT, "runtime-manifest-sha256": "f" * 64},
                },
            )
            adopted_command = Command(manifest)
            staging_before = len(staging_calls)
            captures_before = module.read_stats(store).value("captures")
            with test_resources.resource_context(
                adopted_command,
                invocation,
                {
                    "DPREFIX": str(adopted),
                    "DARLING_LAUNCHER": str(adopted / "bin/darling"),
                },
            ) as adopted_env:
                assert adopted_env is not None
                assert "DARLING_HOMEBREW_LZ4_RESTORED" not in adopted_env, (
                    "an adopted stack was not restored, so nothing was skipped"
                )
                assert adopted_env["DARLING_HOMEBREW_LZ4_WORK"].endswith(
                    f"west-homebrew-lz4-{WORK_TOKEN}"
                ), adopted_env
            assert len(staging_calls) == staging_before, (
                "an installation already present must be adopted, never staged over"
            )
            assert any("adopting" in line for line in adopted_command.lines), (
                adopted_command.lines
            )
            assert module.read_stats(store).value("captures") == captures_before + 1, (
                "adopting a complete stack must capture it for the next run"
            )

            # A test whose subject is the from-source build refuses the restore:
            # a snapshot must never stand in for the build it measures, while the
            # store stays enabled so the staged stack is still captured.
            declared = build_prefix(workspace / "declared", stack=False)
            declared_command = Command(manifest)
            staging_before = len(staging_calls)
            with test_resources.resource_context(
                declared_command,
                invocation,
                {
                    "DPREFIX": str(declared),
                    "DARLING_LAUNCHER": str(declared / "bin/darling"),
                    "WEST_STOCK_STACK_RESTORE": "off",
                },
            ) as declared_env:
                assert declared_env is not None
                assert "DARLING_HOMEBREW_LZ4_RESTORED" not in declared_env, (
                    "a declaration that refuses the restore must stage instead"
                )
            assert len(staging_calls) == staging_before + 1, (
                "the restore refusal must be honored before the snapshot is read"
            )
            assert any(
                "restore disabled by declaration" in line for line in declared_command.lines
            ), declared_command.lines

            staging_before = len(staging_calls)
            os.environ["WEST_STOCK_STACK_CACHE"] = "off"
            skipped = build_prefix(workspace / "skipped", stack=False)
            off_command = Command(manifest)
            captures = module.read_stats(store).value("captures")
            with test_resources.resource_context(
                off_command,
                invocation,
                {
                    "DPREFIX": str(skipped),
                    "DARLING_LAUNCHER": str(skipped / "bin/darling"),
                },
            ) as staged:
                assert staged is not None
                assert "DARLING_HOMEBREW_LZ4_RESTORED" not in staged, (
                    "the kill switch must build from source"
                )
            assert len(staging_calls) == staging_before + 1, (
                "the kill switch must stage the resource"
            )
            assert any("disabled" in line for line in off_command.lines), off_command.lines
            assert module.read_stats(store).value("captures") == captures, (
                "a disabled cache must not capture"
            )
    finally:
        test_homebrew.homebrew_lz4_context = original


def run(arm: str) -> int:
    with tempfile.TemporaryDirectory(prefix="stock-stack-cache-contract-") as raw:
        workspace = Path(raw)
        if arm == "green":
            exercise(
                cache,
                workspace,
                environ={"WEST_STOCK_STACK_CACHE_DIR": str(workspace / "store")},
                suppress_marker=False,
            )
        elif arm == "broken":
            try:
                exercise(
                    cache,
                    workspace,
                    environ={"WEST_STOCK_STACK_CACHE_DIR": str(workspace / "store")},
                    suppress_marker=True,
                )
            except AssertionError as error:
                print(
                    "RED arm (capture without a completion marker) failed as "
                    f"designed: {error}"
                )
                return 0
            print("RED arm unexpectedly passed")
            return 1
        elif arm == "adopt-ignored":
            # Staging over an installation that is already present: the adopt path
            # exists because staging refuses that prefix by design.
            import test_homebrew  # noqa: PLC0415

            original = test_homebrew.conflicting_homebrew
            test_homebrew.conflicting_homebrew = lambda prefix: None
            try:
                exercise(
                    cache,
                    workspace,
                    environ={"WEST_STOCK_STACK_CACHE_DIR": str(workspace / "store")},
                    suppress_marker=False,
                )
            except (AssertionError, ValueError) as error:
                print(
                    "RED arm (installation already present staged over) failed as "
                    f"designed: {error}"
                )
                return 0
            finally:
                test_homebrew.conflicting_homebrew = original
            print("RED arm unexpectedly passed")
            return 1
        elif arm == "restore-declaration-ignored":
            # Reading only the host environment: a test that declares it must
            # build from source would be served a snapshot instead.
            original = cache.store_restore_disabled
            cache.store_restore_disabled = lambda environ: False
            try:
                exercise(
                    cache,
                    workspace,
                    environ={"WEST_STOCK_STACK_CACHE_DIR": str(workspace / "store")},
                    suppress_marker=False,
                )
            except AssertionError as error:
                print(
                    "RED arm (declaration to build from source ignored) failed as "
                    f"designed: {error}"
                )
                return 0
            finally:
                cache.store_restore_disabled = original
            print("RED arm unexpectedly passed")
            return 1
        elif arm == "disabled":
            try:
                exercise(
                    cache,
                    workspace,
                    environ={
                        "WEST_STOCK_STACK_CACHE_DIR": str(workspace / "store"),
                        "WEST_STOCK_STACK_CACHE": "off",
                    },
                    suppress_marker=False,
                )
            except AssertionError as error:
                print(f"RED arm (cache disabled) failed as designed: {error}")
                return 0
            print("RED arm unexpectedly passed")
            return 1
        else:
            raise SystemExit(f"unknown stock-stack cache contract arm: {arm}")
    print("PASS stock-stack-cache-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(os.environ.get("WEST_STOCK_STACK_CONTRACT_ARM", "green")))

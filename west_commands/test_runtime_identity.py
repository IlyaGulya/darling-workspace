"""Content identity for a retained runtime provider."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from patch_stack_lock_first import profile_dependency_chain

# Runtime profile source selection: `source-mode: manifest` takes the product source from the current West
# manifest's pins instead of the legacy patch/profile stack. The constant lives in this leaf module so both the
# identity writer and the runtime planner can use it without an import cycle.
MANIFEST_SOURCE_MODE = "manifest"


def _sha256_file(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _git_head(repo: Path) -> str | None:
    if not repo.is_dir():
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    return revision or None


def _patch_records(path: Path) -> list[dict[str, str | None]]:
    if not path.is_file() or path.is_symlink():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    records = []
    for patch in data.get("patches", []) or []:
        if not isinstance(patch, dict):
            continue
        records.append(
            {
                key: str(patch[key]) if patch.get(key) is not None else None
                for key in ("module", "path", "source-base", "source-commit", "sha256sum")
            }
        )
    return records


def runtime_identity(
    *,
    topdir: Path,
    manifest_repo: Path,
    profile_name: str,
    definition: dict[str, Any],
    launcher: Path,
) -> dict[str, Any]:
    """Return the inputs that make a retained runtime deployment meaningful."""

    source_modules = definition.get("source-modules", [])
    source_commits = {
        str(module): _git_head(topdir / str(module))
        for module in source_modules
    }
    # A manifest-native provider applies NO patchset, so it records no patchset. Asking the patch layer for the
    # dependency chain of a source mode it does not use is how the sentinel name reached a patch-profile loader and
    # failed the bootstrap with "profile metadata path must be real and contained".
    source_mode = definition.get("source-mode")
    patchsets = []
    if source_mode != MANIFEST_SOURCE_MODE:
        for profile in profile_dependency_chain(manifest_repo, definition["source-profile"]):
            path = manifest_repo / "patches" / profile / "patches.yml"
            patchsets.append({
                "profile": profile,
                "sha256": _sha256_file(path),
                "patches": _patch_records(path),
            })
    runtime_manifest = manifest_repo / "testkit/runtime-profiles.yml"
    lock = manifest_repo / "west.lock.yml"
    if not lock.is_file():
        lock = manifest_repo / "west.yml"
    identity = {
        "schema": 2,
        "profile": profile_name,
        "source-profile": definition.get("source-profile"),
        "source-lock-sha256": _sha256_file(lock),
        "source-commits": source_commits,
        "patchsets": patchsets,
        "runtime-manifest-sha256": _sha256_file(runtime_manifest),
        "runtime-profile-definition-sha256": _canonical_sha256(definition),
        "launcher-sha256": _sha256_file(launcher),
    }
    if source_mode == MANIFEST_SOURCE_MODE:
        # The evidence a manifest-native runtime needs: which workspace revision and provider produced it, what each
        # Ring switch was set to at build time (so a RING claim can never rest on the provider's NAME), and what the
        # manifest resolved to at that moment.
        identity["source-mode"] = MANIFEST_SOURCE_MODE
        identity["source-profile"] = None
        identity["manifest-commit"] = _git_head(manifest_repo)
        identity["ring-defines"] = {
            key: definition.get("cmake-defines", {}).get(key)
            for key in ("DARLING_RING_TRANSPORT", "DSERVER_RING_TRANSPORT")
        }
    return identity

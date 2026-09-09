"""Content identity for a retained runtime provider."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from patch_stack_lock_first import profile_dependency_chain


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
    patchsets = []
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
    return {
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

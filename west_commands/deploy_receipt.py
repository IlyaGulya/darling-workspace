"""Durable development deployment receipt.

The manifest-driven workflow makes a ``darling-workspace`` commit the product
revision (see ``docs/adr/0001-workspace-manifest-is-the-product.md``). The
post-deploy doctor must therefore verify the deployed bytes against the build
that actually produced them, not against a historical known-good md5 set: a
deliberate development deployment is supposed to differ from the historical
baseline, and comparing it to that baseline rolls back correct work.

A receipt is written by every deploy path that retains its result. It records:

* the workspace identity (manifest commit and dirty state),
* the source revisions of the components whose artifacts were built,
* the build directory, and
* one row per deployed file: destination, deployed sha256, build source and the
  built source sha256.

The doctor's default (``current``) receipt mode verifies ``sha256(deployed)``
against the recorded digest for each row, and reports a row as a problem when
the build artifact has moved on since the deploy (the deployed bytes are then
stale relative to the current build). Historical ``deploy-baseline.md5``
comparison remains available as an explicit regression mode.

This module deliberately owns no orchestration: it is a small, read/write
receipt used by the existing deploy paths and doctor checks.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

try:
    from .deploy_transaction import sha256_file
except ImportError:
    from deploy_transaction import sha256_file

RECEIPT_FILENAME = ".darling-deploy-receipt.json"
SCHEMA_VERSION = 1

# Components recorded by default. These are the worktrees whose sources feed a
# Darling runtime closure; each is recorded only when it is a live Git
# worktree, so a partial checkout still produces an honest receipt.
DEFAULT_COMPONENT_PATHS = (
    "darling",
    "darling/src/external/darlingserver",
    "darling/src/external/xnu",
    "darling/src/external/dyld",
    "darling/src/external/libsystem",
)


def receipt_path(prefix: Path) -> Path:
    """Return the receipt location for a prefix."""
    return Path(prefix) / RECEIPT_FILENAME


def _git(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _worktree_identity(path: Path) -> dict[str, Any] | None:
    if not (path / ".git").exists():
        return None
    revision = _git(path, "rev-parse", "HEAD")
    if revision is None:
        return None
    status = _git(path, "status", "--porcelain")
    return {
        "revision": revision,
        "dirty": bool(status),
    }


def workspace_identity(manifest_repo: Path) -> dict[str, Any]:
    """Record the product revision: the manifest commit plus its dirty state."""
    manifest_repo = Path(manifest_repo)
    commit = _git(manifest_repo, "rev-parse", "HEAD")
    status = _git(manifest_repo, "status", "--porcelain")
    return {
        "manifest_commit": commit,
        "dirty": bool(status) if status is not None else None,
    }


def component_identity(
    topdir: Path,
    relative_paths: Iterable[str] = DEFAULT_COMPONENT_PATHS,
) -> list[dict[str, Any]]:
    """Record the HEAD of each component worktree that exists under ``topdir``."""
    components: list[dict[str, Any]] = []
    for relative in relative_paths:
        path = Path(topdir) / relative
        identity = _worktree_identity(path)
        if identity is None:
            continue
        components.append({"path": relative, **identity})
    return components


def build_receipt(
    *,
    manifest_repo: Path,
    topdir: Path,
    build_dir: Path,
    prefix: Path,
    deployed: Iterable[tuple[Path, Path]],
    component_paths: Iterable[str] = DEFAULT_COMPONENT_PATHS,
) -> dict[str, Any]:
    """Build a receipt from deployed ``(source, destination)`` pairs."""
    build_dir = Path(build_dir)
    artifacts: list[dict[str, Any]] = []
    for source, destination in deployed:
        source = Path(source)
        destination = Path(destination)
        source_digest = sha256_file(source) if source.is_file() else None
        artifacts.append(
            {
                "destination": str(destination),
                "deployed_sha256": sha256_file(destination)
                if destination.is_file()
                else None,
                "source": str(source),
                "source_sha256": source_digest,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace": workspace_identity(manifest_repo),
        "components": component_identity(topdir, component_paths),
        "build_dir": str(build_dir),
        "prefix": str(prefix),
        "artifacts": artifacts,
    }


def write_receipt(prefix: Path, receipt: dict[str, Any]) -> Path:
    """Atomically write a receipt under ``prefix``."""
    path = receipt_path(prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return path


def read_receipt(prefix: Path) -> dict[str, Any] | None:
    """Read a receipt from ``prefix``; ``None`` when absent, raise when malformed."""
    path = receipt_path(prefix)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"deployment receipt is not an object: {path}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"deployment receipt schema {payload.get('schema_version')!r} "
            f"is not supported (expected {SCHEMA_VERSION}): {path}"
        )
    return payload


class ReceiptProblem:
    """One doctor-actionable mismatch found while verifying a receipt."""

    __slots__ = ("label", "message")

    def __init__(self, label: str, message: str) -> None:
        self.label = label
        self.message = message


def verify_receipt(receipt: dict[str, Any]) -> tuple[list[ReceiptProblem], list[str]]:
    """Verify deployed bytes against the receipt.

    Returns ``(problems, notes)``. A problem means the deployed state does not
    match the recorded deployment; a note is informational.
    """
    problems: list[ReceiptProblem] = []
    notes: list[str] = []
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        problems.append(
            ReceiptProblem("deployment receipt", "receipt records no deployed artifacts")
        )
        return problems, notes
    for row in artifacts:
        if not isinstance(row, dict):
            problems.append(
                ReceiptProblem("deployment receipt", f"malformed artifact row: {row!r}")
            )
            continue
        destination = Path(str(row.get("destination", "")))
        deployed_expected = row.get("deployed_sha256")
        label = f"deployed {destination.name or destination}"
        if not destination.is_file():
            problems.append(
                ReceiptProblem(label, f"missing at {destination} (recorded by the receipt)")
            )
            continue
        deployed_actual = sha256_file(destination)
        if deployed_expected is None:
            problems.append(
                ReceiptProblem(
                    label,
                    f"receipt records no deployed digest for {destination}",
                )
            )
            continue
        if deployed_actual != deployed_expected:
            problems.append(
                ReceiptProblem(
                    label,
                    f"deployed sha256 {deployed_actual[:12]} != receipt "
                    f"{str(deployed_expected)[:12]} ({destination})",
                )
            )
            continue
        source = row.get("source")
        source_expected = row.get("source_sha256")
        if source and source_expected:
            source_path = Path(str(source))
            if not source_path.is_file():
                notes.append(
                    f"{label}: build source absent ({source}); deploy receipt accepted"
                )
            elif sha256_file(source_path) != source_expected:
                problems.append(
                    ReceiptProblem(
                        label,
                        f"build artifact changed since deploy ({source}); redeploy "
                        "or rebuild the receipt",
                    )
                )
            else:
                notes.append(f"{label} matches deploy receipt")
        else:
            notes.append(f"{label} matches deploy receipt (no build source recorded)")
    return problems, notes

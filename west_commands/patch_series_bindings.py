"""Which immutable bindings still describe the series an export just rewrote.

``west patch export`` refreshes the patch artifact and ``patches.yml``. Three
further things bind that artifact, and the export cannot refresh any of them:
the schema-v2 lock named by the profile's mapping needs a create-only tag
published to the mirror first, the migration receipt's row restates the lock,
and the profile compositions derive the trees the export may have changed.

What the export can do is say so while the change is still in hand, instead of
leaving the mismatch to be found later by three unrelated gates: the receipt
contract, the tier's registry contract, and finally a profile materialization.
Nothing here rewrites a binding - a lock that cannot be published and a
composition that cannot be replayed are exactly the values that must not be
guessed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

SOURCE_REF_PREFIX = "refs/tags/patch-stack/v1/sources/"
PROFILES_REGISTRY = "lock-first-profiles-v1.yml"
MIGRATION_RECEIPT = "migration-inventory-v1.yml"
COMMIT_LINE = re.compile(rb"^From [0-9a-f]{40} ", re.MULTILINE)


def _load(path: Path) -> Any:
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text())


def _mapping_for(locks_root: Path, profile: str) -> dict[str, Any] | None:
    registry = _load(locks_root / PROFILES_REGISTRY)
    if not isinstance(registry, dict):
        return None
    for entry in registry.get("profiles", []):
        if entry.get("profile") == profile:
            mapping = _load(locks_root / str(entry.get("mapping")))
            return mapping if isinstance(mapping, dict) else None
    return None


def _dependent_profiles(locks_root: Path, profile: str) -> list[str]:
    """Every profile whose chain consumes this one, transitively."""
    compositions: dict[str, list[str]] = {}
    for path in sorted(locks_root.glob("*-profile-composition-*.yml")):
        document = _load(path)
        if not isinstance(document, dict):
            continue
        owner = document.get("profile") or path.name.split("-profile-composition-")[0]
        compositions[str(owner)] = [
            str(item.get("profile")) for item in document.get("prerequisites", [])
        ]
    dependents = [
        owner for owner, prerequisites in compositions.items() if profile in prerequisites
    ]
    reachable: list[str] = []
    frontier = list(dependents)
    while frontier:
        current = frontier.pop(0)
        if current in reachable:
            continue
        reachable.append(current)
        frontier.extend(
            owner for owner, prerequisites in compositions.items()
            if current in prerequisites and owner not in reachable
        )
    return reachable


def binding_report(
    locks_root: Path,
    *,
    profile: str,
    module: str,
    patch: str,
    commit: str,
    exported: bytes,
) -> list[str]:
    """Lines naming every binding file that still describes the old series."""
    mapping = _mapping_for(locks_root, profile)
    if mapping is None:
        return []
    row = next(
        (
            item for item in mapping.get("series", [])
            if item.get("module") == module and item.get("patch") == patch
        ),
        None,
    )
    if row is None:
        return []

    count = len(COMMIT_LINE.findall(exported))
    stale: list[str] = []
    lock_path = locks_root / str(row.get("lock"))
    lock = _load(lock_path)
    if isinstance(lock, dict) and lock.get("source_commit") != commit:
        mirror = lock.get("mirror") if isinstance(lock.get("mirror"), dict) else {}
        url = mirror.get("url") or "<mirror>"
        stale.append(
            f"{lock_path.name}: still records source_commit {lock.get('source_commit')}, "
            f"but {patch} now exports {commit} ({count} commit(s))"
        )
        stale.append(
            f"  publish it create-only: git push {url} {commit}:{SOURCE_REF_PREFIX}{commit}"
        )
        stale.append(
            "  then refresh the lock (schema-v2, mirror.source_oid/source_ref, "
            "source_commit, ordered_commits, expected_tree) and its row in "
            f"{MIGRATION_RECEIPT} - neither has a refresh command today"
        )

    receipt = _load(locks_root / MIGRATION_RECEIPT)
    if isinstance(receipt, dict):
        relative = f"locks/patch-stack/{lock_path.name}"
        for stack in receipt.get("stacks", []):
            if stack.get("lock") != relative:
                continue
            if stack.get("source_commit") != commit or stack.get("commit_count") != count:
                stale.append(
                    f"{MIGRATION_RECEIPT}: row for {relative} records "
                    f"source_commit {stack.get('source_commit')} with "
                    f"commit_count {stack.get('commit_count')}, not {commit} with {count}"
                )
            break

    if stale:
        dependents = _dependent_profiles(locks_root, profile)
        stale.append(
            f"  reissue the compositions this moves: scripts/generate_profile_composition.py "
            f"--profile {profile}"
            + (f" (its dependents: {', '.join(dependents)})" if dependents else "")
        )
    return stale

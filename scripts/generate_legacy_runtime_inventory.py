#!/usr/bin/env python3
"""Generate locks/patch-stack/legacy-runtime-profile-inventory-v1.json.

The registry is a census of the three legacy-runtime profiles (homebrew, perf,
arch): how many immutable series each profile replays, how many ordered
commits those series carry, how the series group into modules, and the typed
lock-first batch identity that binds them.  Every one of those fields is a
function of the tree, so refreshing them by hand is how the registry drifted
(dar-developer-tooling-ux-tcwe.27).  This script derives them instead.

Derivation inputs (all read from the tree, never from the registry):

  locks/patch-stack/immutable-oracle-profiles-v1.yml
      the legacy-runtime profile set, its order, oracle mode and typed mapping
  patches/<profile>/patches.yml
      the declared series list, module grouping and declared base profile
  locks/patch-stack/lock-first-profiles-v1.yml
      profile -> typed mapping registry
  locks/patch-stack/lock-first-series-*.yml
      the typed batch identity (batch_id, expected_count) and lock per series
  locks/patch-stack/<profile>-profile-composition-v2.yml
      the bound profile composition: prerequisite chain, mapping digest,
      module order, and per-series canonical lock
  locks/patch-stack/<series lock>.yml
      the ordered commit census per series
  locks/patch-stack/migration-inventory-v1.yml
      the canonical lock inventory; its hosted object-closure vocabulary

Reviewed inputs (kept from the registry, because a generator cannot invent
publication state, and checked against the derivation):

  source.canonical_lock_inventory, source.migration_report
      pointers to the lock inventory and migration report
  closure.schema_v2_lock_coverage, closure.immutable_ref_closure,
  closure.clean_odb_evidence
      publication notes.  The hosted/publication-pending split inside them is
      a publication fact that lives on the hosting side, not in this tree, so
      it stays reviewed; the notes are re-rendered around the derived census
      and their arithmetic is required to close over it.  When the census
      moves, the check fails naming the note and the split must be reviewed.

Usage:

  python3 -B scripts/generate_legacy_runtime_inventory.py
      rewrite the registry from the current tree (no-op when already current)
  python3 -B scripts/generate_legacy_runtime_inventory.py --check
      fail, naming every disagreeing field, when the registry disagrees
  python3 -B scripts/generate_legacy_runtime_inventory.py --registry PATH
      operate on another registry path (used to exercise drift detection)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))
sys.path.insert(0, str(ROOT / "scripts"))
from registry_derivation import DerivationError, Target, run  # noqa: E402
import patch_stack_lock_first  # noqa: E402
import patch_stack_materialize  # noqa: E402

LOCKS = ROOT / "locks" / "patch-stack"
DEFAULT_REGISTRY = LOCKS / "legacy-runtime-profile-inventory-v1.json"
ORACLE_PROFILES = LOCKS / "immutable-oracle-profiles-v1.yml"
MIGRATION_INVENTORY = LOCKS / "migration-inventory-v1.yml"
MIGRATION_REPORT = ROOT / "docs" / "patch-stack-canonical-migration-report.md"

_COVERAGE = re.compile(
    r"^(?P<hosted>\d+)/(?P<hosted_again>\d+) hosted plus "
    r"(?P<pending>\d+) publication-pending append-only series$"
)
_REF_CLOSURE = re.compile(
    r"^(?P<closure>[a-z0-9_]+) plus (?P<pending>\d+) publication-pending append-only series$"
)
_EVIDENCE = re.compile(r"^(?P<hosted>\d+) hosted series independently verified(?P<tail>;.*)$")


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DerivationError(f"{path.relative_to(ROOT)}: cannot read derivation input: {error}") from error


def _hosted_closure_vocabulary() -> str:
    """Return the one object-closure token the canonical lock inventory uses for hosted series."""

    inventory = _load_yaml(MIGRATION_INVENTORY)
    stacks = inventory.get("stacks") if isinstance(inventory, dict) else None
    if not isinstance(stacks, list) or not stacks:
        raise DerivationError(
            f"{MIGRATION_INVENTORY.relative_to(ROOT)}: canonical lock inventory has no stacks"
        )
    tokens = sorted(
        {
            stack["object_closure"]
            for stack in stacks
            if isinstance(stack, dict) and stack.get("classification") == "ALREADY_MIGRATED"
        }
    )
    if len(tokens) != 1:
        raise DerivationError(
            "locks/patch-stack/migration-inventory-v1.yml: hosted series object-closure "
            f"vocabulary is not unique: {tokens}"
        )
    return tokens[0]


def _profile_chain() -> list[str]:
    """Return the legacy-runtime profiles in the order the oracle registry declares."""

    oracle = _load_yaml(ORACLE_PROFILES)
    entries = oracle.get("profiles") if isinstance(oracle, dict) else None
    if oracle.get("schema_version") != 1 or not isinstance(entries, list) or not entries:
        raise DerivationError(
            f"{ORACLE_PROFILES.relative_to(ROOT)}: immutable-oracle profile registry is malformed"
        )
    profiles = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"profile", "oracle_mode", "mapping"}:
            raise DerivationError(
                f"{ORACLE_PROFILES.relative_to(ROOT)}: immutable-oracle profile entry is malformed"
            )
        if entry["oracle_mode"] != "immutable-cherry-pick-oracle":
            raise DerivationError(
                f"{ORACLE_PROFILES.relative_to(ROOT)}: {entry['profile']}: unexpected oracle mode"
            )
        profiles.append(entry["profile"])
    return profiles


def _profile_rows(profile: str) -> dict[str, Any]:
    """Derive one profile census from its patches, mapping, composition and locks."""

    metadata = _load_yaml(ROOT / "patches" / profile / "patches.yml")
    patches = metadata.get("patches") if isinstance(metadata, dict) else None
    if not isinstance(patches, list) or not patches:
        raise DerivationError(f"patches/{profile}/patches.yml: profile declares no patches")
    mapping_path = patch_stack_lock_first.mapping_for_profile(profile)
    plan = patch_stack_lock_first.plan(profile, patches)
    composition = plan.batch.get("profile_composition")
    if not isinstance(composition, dict):
        raise DerivationError(
            f"patches/{profile}/patches.yml: the typed lock-first mapping declares no bound profile "
            "composition, so the profile census cannot be derived"
        )
    prerequisites = composition["prerequisites"]
    base_profile = prerequisites[0]["profile"] if prerequisites else None
    declared_base = metadata.get("base-profile")
    if declared_base != base_profile:
        raise DerivationError(
            f"patches/{profile}/patches.yml: base-profile {declared_base!r} differs from the bound "
            f"composition prerequisite {base_profile!r}"
        )
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for patch in patches:
        grouped.setdefault(patch["module"], []).append(patch)
    ordered_commits = dict.fromkeys(grouped, 0)
    for record in plan:
        lock = patch_stack_materialize.load_lock(Path(record["lock_path"]))
        ordered_commits[record["module"]] += len(lock["ordered_commits"])
    if [module["module"] for module in composition["modules"]] != list(grouped):
        raise DerivationError(
            f"locks/patch-stack/{composition['path']}: module order differs from the profile's "
            "grouped series"
        )
    if plan.batch["expected_count"] != len(patches):
        raise DerivationError(f"patches/{profile}/patches.yml: typed batch count differs from series")
    return {
        "profile": profile,
        "base_profile": base_profile,
        "mapping": mapping_path.name,
        "batch_id": plan.batch["batch_id"],
        "series_count": len(patches),
        "ordered_commit_count": sum(ordered_commits.values()),
        "modules": [
            {
                "path": module,
                "series_count": len(module_patches),
                "ordered_commit_count": ordered_commits[module],
            }
            for module, module_patches in grouped.items()
        ],
    }


def _publication_notes(current: dict[str, Any], total_series: int) -> dict[str, str]:
    """Re-render the reviewed publication notes around the derived census.

    The hosted/publication-pending split is a hosting-side fact, so it is read
    from the registry rather than invented here.  Its arithmetic, however, is
    not allowed to drift away from the census this file is a census of.
    """

    closure = current.get("closure")
    if not isinstance(closure, dict):
        raise DerivationError("closure: registry has no closure object")
    coverage = closure.get("schema_v2_lock_coverage")
    ref = closure.get("immutable_ref_closure")
    evidence = closure.get("clean_odb_evidence")
    for field, value in (
        ("closure.schema_v2_lock_coverage", coverage),
        ("closure.immutable_ref_closure", ref),
        ("closure.clean_odb_evidence", evidence),
    ):
        if not isinstance(value, str):
            raise DerivationError(f"{field}: registry publication note is missing")
    match = _COVERAGE.match(coverage)
    if match is None:
        raise DerivationError(
            "closure.schema_v2_lock_coverage: expected '<hosted>/<hosted> hosted plus "
            f"<pending> publication-pending append-only series', found {coverage!r}"
        )
    hosted = int(match["hosted"])
    pending = int(match["pending"])
    if hosted != int(match["hosted_again"]):
        raise DerivationError(
            f"closure.schema_v2_lock_coverage: hosted numerator/denominator disagree: {coverage!r}"
        )
    if hosted + pending != total_series:
        raise DerivationError(
            f"closure.schema_v2_lock_coverage: reviewed split {hosted}+{pending} does not close "
            f"over the derived total_series {total_series}; review the publication split"
        )
    ref_match = _REF_CLOSURE.match(ref)
    if ref_match is None:
        raise DerivationError(
            "closure.immutable_ref_closure: expected '<object_closure> plus <pending> "
            f"publication-pending append-only series', found {ref!r}"
        )
    if int(ref_match["pending"]) != pending:
        raise DerivationError(
            "closure.immutable_ref_closure: publication-pending count differs from "
            "closure.schema_v2_lock_coverage"
        )
    vocabulary = _hosted_closure_vocabulary()
    if ref_match["closure"] != vocabulary:
        raise DerivationError(
            "closure.immutable_ref_closure: hosted closure token "
            f"{ref_match['closure']!r} differs from the canonical lock inventory vocabulary "
            f"{vocabulary!r}"
        )
    evidence_match = _EVIDENCE.match(evidence)
    if evidence_match is None:
        raise DerivationError(
            "closure.clean_odb_evidence: expected '<hosted> hosted series independently verified;...', "
            f"found {evidence!r}"
        )
    if int(evidence_match["hosted"]) != hosted:
        raise DerivationError(
            "closure.clean_odb_evidence: verified count differs from closure.schema_v2_lock_coverage"
        )
    return {
        "schema_v2_lock_coverage": (
            f"{hosted}/{hosted} hosted plus {pending} publication-pending append-only series"
        ),
        "immutable_ref_closure": (
            f"{vocabulary} plus {pending} publication-pending append-only series"
        ),
        "clean_odb_evidence": (
            f"{hosted} hosted series independently verified{evidence_match['tail']}"
        ),
    }


def derive(current: dict[str, Any]) -> dict[str, Any]:
    """Derive the whole registry, taking reviewed publication notes from ``current``."""

    source = current.get("source")
    if not isinstance(source, dict) or set(source) != {
        "profiles",
        "canonical_lock_inventory",
        "migration_report",
    }:
        raise DerivationError("source: registry source block is missing or malformed")
    canonical = source["canonical_lock_inventory"]
    report = source["migration_report"]
    if canonical != "locks/patch-stack/migration-inventory-v1.yml":
        raise DerivationError(
            f"source.canonical_lock_inventory: {canonical!r} is not the canonical lock inventory"
        )
    if not (ROOT / canonical).is_file():
        raise DerivationError(f"source.canonical_lock_inventory: {canonical} is not a file")
    if not (ROOT / report).is_file():
        raise DerivationError(f"source.migration_report: {report} is not a file")
    profiles = [_profile_rows(profile) for profile in _profile_chain()]
    total_series = sum(row["series_count"] for row in profiles)
    total_commits = sum(row["ordered_commit_count"] for row in profiles)
    notes = _publication_notes(current, total_series)
    return {
        "schema_version": 1,
        "source": {
            "profiles": [f"patches/{row['profile']}/patches.yml" for row in profiles],
            "canonical_lock_inventory": canonical,
            "migration_report": report,
        },
        "profiles": profiles,
        "closure": {
            "total_series": total_series,
            "total_ordered_commits": total_commits,
            **notes,
        },
    }


def render(document: dict[str, Any]) -> str:
    """Render the registry in its checked-in layout."""

    lines = ["{", '  "schema_version": 1,', '  "source": {', '    "profiles": [']
    for index, path in enumerate(document["source"]["profiles"]):
        comma = "," if index < len(document["source"]["profiles"]) - 1 else ""
        lines.append(f'      "{path}"{comma}')
    lines.append("    ],")
    lines.append(f'    "canonical_lock_inventory": "{document["source"]["canonical_lock_inventory"]}",')
    lines.append(f'    "migration_report": "{document["source"]["migration_report"]}"')
    lines.append("  },")
    lines.append('  "profiles": [')
    for index, row in enumerate(document["profiles"]):
        lines.append("    {")
        lines.append(f'      "profile": "{row["profile"]}",')
        lines.append(f'      "base_profile": {json.dumps(row["base_profile"])},')
        lines.append(f'      "mapping": "{row["mapping"]}",')
        lines.append(f'      "batch_id": "{row["batch_id"]}",')
        lines.append(f'      "series_count": {row["series_count"]},')
        lines.append(f'      "ordered_commit_count": {row["ordered_commit_count"]},')
        lines.append('      "modules": [')
        for position, module in enumerate(row["modules"]):
            comma = "," if position < len(row["modules"]) - 1 else ""
            lines.append(
                f'        {{"path": "{module["path"]}", "series_count": {module["series_count"]}, '
                f'"ordered_commit_count": {module["ordered_commit_count"]}}}{comma}'
            )
        lines.append("      ]")
        lines.append("    }," if index < len(document["profiles"]) - 1 else "    }")
    lines.append("  ],")
    lines.append('  "closure": {')
    lines.append(f'    "total_series": {document["closure"]["total_series"]},')
    lines.append(f'    "total_ordered_commits": {document["closure"]["total_ordered_commits"]},')
    lines.append(f'    "schema_v2_lock_coverage": {json.dumps(document["closure"]["schema_v2_lock_coverage"])},')
    lines.append(f'    "immutable_ref_closure": {json.dumps(document["closure"]["immutable_ref_closure"])},')
    lines.append(f'    "clean_odb_evidence": {json.dumps(document["closure"]["clean_odb_evidence"])}')
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def load_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DerivationError(f"{path}: cannot read registry: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DerivationError(f"{path}: registry must be a schema_version 1 object")
    return payload


def target(registry_path: Path) -> Target:
    return Target(
        label="locks/patch-stack/legacy-runtime-profile-inventory-v1.json",
        path=registry_path,
        load=load_registry,
        derive=derive,
        render=render,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero instead of rewriting the registry",
    )
    args = parser.parse_args(argv)
    return run(
        [target(args.registry.resolve())],
        check=args.check,
        heading="legacy-runtime inventory derivation",
    )


if __name__ == "__main__":
    raise SystemExit(main())

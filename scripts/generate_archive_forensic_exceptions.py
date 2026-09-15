#!/usr/bin/env python3
"""Generate locks/patch-stack/archive-forensic-exceptions-v1.yml.

An archive-forensic exception records a legacy patch archive whose declared
preimage is not reproducible from a clean ODB, so the profile materializes the
series from its immutable lock instead of the archive.  The exception itself
is a reviewed judgement; everything it *describes* is derivable and is derived
here:

  artifact          patches/<profile>/<patch> as patches/<profile>/patches.yml declares it
  lock              the canonical lock the profile's lock-first mapping binds
  declared_base     the archive's declared source-base from patches/<profile>/patches.yml
  declared_source   the archive's declared source-commit from the same entry
  missing_blob      the preimage blob of the archive's own index line for index_path
  index_preimage    the same preimage, recorded as the archive spells it
  authority         the profile's oracle mode from immutable-oracle-profiles-v1.yml

Reviewed inputs, kept from the registry: the exception subject (profile,
patch), the index_path whose preimage is unreproducible, and the
classification that names the pathology.  A subject that no longer needs an
exception - because the archive's declared source became the canonical lock
source - is refused rather than carried.

Usage:

  python3 -B scripts/generate_archive_forensic_exceptions.py
      rewrite the registry from the current tree (no-op when already current)
  python3 -B scripts/generate_archive_forensic_exceptions.py --check
      fail, naming every disagreeing field, when the registry disagrees
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))
sys.path.insert(0, str(ROOT / "scripts"))
import patch_stack_lock_first  # noqa: E402
import patch_stack_materialize  # noqa: E402
from registry_derivation import DerivationError, Target, run  # noqa: E402

LOCKS = ROOT / "locks" / "patch-stack"
DEFAULT_REGISTRY = LOCKS / "archive-forensic-exceptions-v1.yml"
ORACLE_PROFILES = LOCKS / "immutable-oracle-profiles-v1.yml"

CLASSIFICATION = "LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE"
_FIELDS = (
    "profile",
    "patch",
    "artifact",
    "classification",
    "lock",
    "missing_blob",
    "index_path",
    "index_preimage",
    "declared_base",
    "declared_source",
    "authority",
)
_INDEX_LINE = re.compile(r"^index (?P<preimage>[0-9a-f]{40})\.\.(?P<postimage>[0-9a-f]{40})(?: |$)")
_DIFF_HEADER = re.compile(r"^diff --git a/(?P<left>.+) b/(?P<right>.+)$")


def load_registry(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DerivationError(f"{path}: cannot read registry: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "exceptions"}:
        raise DerivationError(f"{path}: registry must hold exactly schema_version and exceptions")
    if payload["schema_version"] != 1 or not isinstance(payload["exceptions"], list):
        raise DerivationError(f"{path}: registry schema_version/exceptions are malformed")
    return payload


def _archive_preimages(archive: Path) -> dict[str, str]:
    """Return the preimage blob the archive declares per patched path."""

    try:
        text = archive.read_text(encoding="utf-8")
    except OSError as error:
        raise DerivationError(f"{archive}: cannot read patch archive: {error}") from error
    preimages: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        header = _DIFF_HEADER.match(line)
        if header is not None:
            if header["left"] != header["right"]:
                raise DerivationError(f"{archive}: diff header sides differ: {line}")
            current = header["right"]
            continue
        index = _INDEX_LINE.match(line)
        if index is not None and current is not None:
            preimages.setdefault(current, index["preimage"])
    return preimages


def _oracle_modes() -> dict[str, str]:
    registry = yaml.safe_load(ORACLE_PROFILES.read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise DerivationError(f"{ORACLE_PROFILES}: immutable-oracle registry is malformed")
    return {entry["profile"]: entry["oracle_mode"] for entry in registry["profiles"]}


def derive(current: dict[str, Any]) -> dict[str, Any]:
    """Derive every exception field that the tree can decide."""

    modes = _oracle_modes()
    profiles: dict[str, dict[str, Any]] = {}
    plans: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    exceptions: list[dict[str, Any]] = []
    for entry in current["exceptions"]:
        if not isinstance(entry, dict) or tuple(entry) != _FIELDS:
            raise DerivationError(
                f"exceptions[]: exception entry must hold exactly {list(_FIELDS)}, found "
                f"{list(entry) if isinstance(entry, dict) else entry!r}"
            )
        profile = entry["profile"]
        patch = entry["patch"]
        index_path = entry["index_path"]
        if entry["classification"] != CLASSIFICATION:
            raise DerivationError(
                f"exceptions[{profile}:{patch}].classification: {entry['classification']!r} is not "
                f"the supported {CLASSIFICATION!r}"
            )
        if profile not in modes:
            raise DerivationError(f"exceptions[{profile}:{patch}].profile: profile is not a pinned profile")
        if profile not in profiles:
            metadata = yaml.safe_load((ROOT / "patches" / profile / "patches.yml").read_text(encoding="utf-8"))
            profiles[profile] = metadata
            plan = patch_stack_lock_first.plan(profile, metadata["patches"])
            plans[profile] = {(record["module"], record["patch"]): record for record in plan}
        matches = [key for key in plans[profile] if key[1] == patch]
        if len(matches) != 1:
            raise DerivationError(
                f"exceptions[{profile}:{patch}].patch: not bound exactly once by the profile's "
                "lock-first mapping"
            )
        record = plans[profile][matches[0]]
        declared = {(item["module"], item["path"]): item for item in profiles[profile]["patches"]}[
            record["module"], patch
        ]
        lock_path = Path(record["lock_path"])
        lock = patch_stack_materialize.load_lock(lock_path)
        if lock["source_commit"] == declared.get("source-commit"):
            raise DerivationError(
                f"exceptions[{profile}:{patch}]: the canonical lock source is the archive's declared "
                "source; the forensic exception is obsolete"
            )
        artifact = (ROOT / "patches" / profile / patch).relative_to(ROOT)
        preimages = _archive_preimages(ROOT / artifact)
        if index_path not in preimages:
            raise DerivationError(
                f"exceptions[{profile}:{patch}].index_path: {index_path} has no index line in {artifact}"
            )
        preimage = preimages[index_path]
        exceptions.append(
            {
                "profile": profile,
                "patch": patch,
                "artifact": artifact.as_posix(),
                "classification": CLASSIFICATION,
                "lock": lock_path.name,
                "missing_blob": preimage,
                "index_path": index_path,
                "index_preimage": preimage,
                "declared_base": declared.get("source-base"),
                "declared_source": declared.get("source-commit"),
                "authority": modes[profile],
            }
        )
    return {"schema_version": 1, "exceptions": exceptions}


def render(document: dict[str, Any]) -> str:
    """Render the registry in its checked-in layout."""

    lines = ["schema_version: 1", "exceptions:"]
    for entry in document["exceptions"]:
        for field in (
            "profile",
            "patch",
            "artifact",
            "classification",
            "lock",
            "missing_blob",
            "index_path",
            "index_preimage",
            "declared_base",
            "declared_source",
            "authority",
        ):
            value = entry[field]
            if not isinstance(value, str) or not value:
                raise DerivationError(f"exceptions[{entry['profile']}:{entry['patch']}].{field}: empty")
            prefix = "  - " if field == "profile" else "    "
            lines.append(f"{prefix}{field}: {value}")
    return "\n".join(lines) + "\n"


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
        [
            Target(
                label="locks/patch-stack/archive-forensic-exceptions-v1.yml",
                path=args.registry.resolve(),
                load=load_registry,
                derive=derive,
                render=render,
            )
        ],
        check=args.check,
        heading="archive forensic exceptions derivation",
    )


if __name__ == "__main__":
    raise SystemExit(main())

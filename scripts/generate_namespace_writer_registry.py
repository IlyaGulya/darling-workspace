#!/usr/bin/env python3
"""Generate the namespace-writer registries from the darling source tree.

Two checked-in registries describe the same audit:

  lifecycle/namespace-writer-inventory-v1.json
      the reviewed threat model, scan grammar, typed writers and exclusions,
      plus every SHA-256 binding those claims rest on
  lifecycle/namespace-writer-candidate-audit-v1.json
      one reviewed classification per discovered mutation candidate that is
      neither a typed writer nor an exact exclusion

What this script derives, from the tree and from the contract that already
owns the scan grammar
(tests/west_test_contracts/namespace_writer_inventory_contract.py):

  inventory
      every digest binding: excluded-source digests, build-closure CMake and
      runtime-evidence digests, installed-service fixture digests, generated
      service source bindings (CMake anchor, MIG generator, declared inputs)
      and the declared MIG source inputs.  The reviewed layout is preserved
      byte for byte; only the derived scalars are spliced.
  candidate audit
      the entry set (discovered mutation candidates minus typed writers and
      exact exclusions), each candidate's source digest, its mutation
      operators, and its build anchor (repository, path, digest, relation).
      The audit is re-emitted in the layout its contract validates.

Reviewed inputs, never invented here:

  * the writer records, scan grammar, prose and exclusion reasons in the
    inventory
  * the per-candidate classification and reason in the candidate audit.  A
    newly discovered candidate with no reviewed classification is refused,
    naming the path, so the classification map stays an input to this script
    rather than an output of it
  * the MIG replay output digests.  Those are derived by re-running the MIG
    generator (cmake configure, build, per-target mig); the contract replays
    them in tests/run-namespace-writer-inventory-contract.sh.  Pass
    --mig-proof to refresh and compare them here as well

Usage:

  python3 -B scripts/generate_namespace_writer_registry.py
      rewrite both registries from the current tree (no-op when current)
  python3 -B scripts/generate_namespace_writer_registry.py --check
      fail, naming every disagreeing field, when a registry disagrees
  python3 -B scripts/generate_namespace_writer_registry.py --check --mig-proof
      also replay and compare the MIG output proof (needs cmake/ninja/clang)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from registry_derivation import DerivationError, diff, run  # noqa: E402

CONTRACT_PATH = ROOT / "tests" / "west_test_contracts" / "namespace_writer_inventory_contract.py"
DEFAULT_INVENTORY = ROOT / "lifecycle" / "namespace-writer-inventory-v1.json"

AUDIT_KIND = "rootless-namespace-writer-candidate-audit"
AUDIT_TIER = "source"
_WORKSPACE_RELATION = "workspace-runtime-source"
_CMAKE_RELATION = "nearest-cmake-ancestor"
_FALLBACK_RELATION = "source-forest-fallback"


def _contract() -> Any:
    """Load the contract module that owns the scan grammar and its validators."""

    spec = importlib.util.spec_from_file_location("namespace_writer_inventory_contract", CONTRACT_PATH)
    if spec is None or spec.loader is None:
        raise DerivationError(f"{CONTRACT_PATH}: cannot load the scan contract")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DerivationError(f"{path}: cannot read registry: {error}") from error


def _dotted(path: tuple[Any, ...]) -> str:
    out = ""
    for step in path:
        out = f"{out}[{step}]" if isinstance(step, int) else (f"{out}.{step}" if out else str(step))
    return out


def _sha256(contract: Any, repository: str, relative: str) -> str:
    absolute = contract._repository_root(repository) / relative
    if not absolute.is_file() or absolute.is_symlink():
        raise DerivationError(f"{repository}:{relative}: derived digest source is not a regular file")
    return contract._sha256_file(absolute)


# ----------------------------------------------------------------- inventory


def inventory_derivation(
    contract: Any, registry: dict[str, Any], *, mig_proof: bool
) -> dict[tuple[Any, ...], Any]:
    """Return every derived scalar of the inventory, addressed by JSON path."""

    policy_path = ("scan", "installed_service_target_policy")
    proof_path = policy_path + ("mig_output_proof",)
    derived: dict[tuple[Any, ...], Any] = {}
    for index, entry in enumerate(registry["excluded_mutations"]):
        derived[("excluded_mutations", index, "sha256")] = _sha256(
            contract, entry["repository"], entry["path"]
        )
    scan = registry["scan"]
    for index, entry in enumerate(scan["build_closure"]):
        derived[("scan", "build_closure", index, "cmake_sha256")] = _sha256(
            contract, entry["repository"], entry["cmake"]
        )
        for position, evidence in enumerate(entry.get("runtime_evidence", [])):
            derived[("scan", "build_closure", index, "runtime_evidence", position, "sha256")] = (
                _sha256(contract, entry["repository"], evidence["path"])
            )
    policy = scan["installed_service_target_policy"]
    for index, entry in enumerate(policy["excluded_test_plists"]):
        derived[policy_path + ("excluded_test_plists", index, "sha256")] = _sha256(
            contract, entry["repository"], entry["path"]
        )
    for index, entry in enumerate(policy["mig_declared_source_inputs"]):
        derived[policy_path + ("mig_declared_source_inputs", index, "sha256")] = _sha256(
            contract, "darling", entry["path"]
        )
    for index, entry in enumerate(policy["generated_source_bindings"]):
        base = policy_path + ("generated_source_bindings", index)
        derived[base + ("cmake_sha256",)] = _sha256(contract, "darling", entry["cmake"])
        derived[base + ("generator", "sha256")] = _sha256(
            contract, "darling", entry["generator"]["path"]
        )
        for position, generator_input in enumerate(entry["inputs"]):
            derived[base + ("inputs", position, "sha256")] = _sha256(
                contract, "darling", generator_input["path"]
            )
    if mig_proof:
        # The MIG proof (tool provenance and generated-output digests) is the one
        # derivation that needs a real configure/build, so it runs under the
        # contract's own replay and environment.  Without --mig-proof those
        # fields are left to tests/run-namespace-writer-inventory-contract.sh,
        # which replays them and fails on drift.
        bindings = contract._generated_source_bindings(policy)
        tools, outputs = contract._materialize_mig_output_proof(bindings)
        for index, tool in enumerate(tools):
            for field in ("name", "path", "sha256", "version"):
                derived[proof_path + ("tools", index, field)] = tool[field]
        for index, output in enumerate(outputs):
            base = proof_path + ("outputs", index)
            for field in ("cmake", "source", "sha256", "size"):
                derived[base + (field,)] = output[field]
            # The MIG proof records no mutation operators by construction, so
            # this list is compared but never rewritten: a non-empty one is a
            # finding to review, not drift to splice.
            derived[base + ("mutation_operators",)] = output["mutation_operators"]
    return derived


def _apply(document: Any, derived: dict[tuple[Any, ...], Any]) -> Any:
    """Return the document with every derived scalar replaced, for field diffing."""

    updated = deepcopy(document)
    for path, value in derived.items():
        cursor: Any = updated
        for step in path[:-1]:
            cursor = cursor[step]
        cursor[path[-1]] = value
    return updated


def _scalar_spans(text: str) -> dict[tuple[Any, ...], tuple[int, int]]:
    """Map every scalar value's JSON path to its span in the registry text."""

    spans: dict[tuple[Any, ...], tuple[int, int]] = {}
    length = len(text)

    def skip(index: int) -> int:
        while index < length and text[index] in " \n\r\t":
            index += 1
        return index

    def string_end(index: int) -> int:
        index += 1
        while index < length:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == '"':
                return index + 1
            index += 1
        raise DerivationError("registry text has an unterminated string")

    def value(index: int, path: tuple[Any, ...]) -> int:
        index = skip(index)
        character = text[index]
        if character == "{":
            index = skip(index + 1)
            if text[index] == "}":
                return index + 1
            while True:
                index = skip(index)
                key_start = index
                index = string_end(index)
                key = json.loads(text[key_start:index])
                index = skip(index)
                if text[index] != ":":
                    raise DerivationError("registry text has a malformed object member")
                index = value(index + 1, path + (key,))
                index = skip(index)
                if text[index] == ",":
                    index += 1
                    continue
                if text[index] == "}":
                    return index + 1
                raise DerivationError("registry text has a malformed object")
        if character == "[":
            position = 0
            index = skip(index + 1)
            if text[index] == "]":
                return index + 1
            while True:
                index = value(index, path + (position,))
                position += 1
                index = skip(index)
                if text[index] == ",":
                    index = skip(index + 1)
                    continue
                if text[index] == "]":
                    return index + 1
                raise DerivationError("registry text has a malformed array")
        start = index
        if character == '"':
            index = string_end(index)
        else:
            while index < length and text[index] not in ",}]":
                index += 1
            while index > start and text[index - 1] in " \n\r\t":
                index -= 1
        spans[path] = (start, index)
        return index

    value(skip(0), ())
    return spans


def _value_at(document: Any, path: tuple[Any, ...]) -> Any:
    """Return the value at a JSON path, or a sentinel when it is absent."""

    cursor = document
    for step in path:
        try:
            cursor = cursor[step]
        except (KeyError, IndexError, TypeError):
            return _ABSENT
    return cursor


_ABSENT = object()


def _splice(text: str, derived: dict[tuple[Any, ...], Any], document: Any) -> str:
    """Replace the text span of every derived scalar whose rendering differs.

    Derived values that are containers rather than scalars (the MIG proof's
    mutation-operator lists) cannot be spliced into the reviewed layout; they
    are only compared, and a change is refused for review instead of being
    rewritten behind the reviewer's back.
    """

    spans = _scalar_spans(text)
    edits: list[tuple[int, int, str]] = []
    for path, value in derived.items():
        span = spans.get(path)
        if span is None:
            if _value_at(document, path) == value:
                continue
            raise DerivationError(
                f"{_dotted(path)}: derived value changed but is not a scalar this generator can "
                "splice into the reviewed layout; review the registry before regenerating"
            )
        rendered = json.dumps(value, ensure_ascii=False)
        if text[span[0] : span[1]] != rendered:
            edits.append((span[0], span[1], rendered))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


class InventoryTarget:
    """The inventory: derived scalars spliced into its reviewed layout."""

    label = "lifecycle/namespace-writer-inventory-v1.json"

    def __init__(self, path: Path, contract: Any, *, mig_proof: bool) -> None:
        self.path = path
        self.contract = contract
        self.mig_proof = mig_proof

    def evaluate(self) -> tuple[list[str], str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise DerivationError(f"{self.label}: cannot read registry: {error}") from error
        current = load_json(self.path)
        derived = inventory_derivation(self.contract, current, mig_proof=self.mig_proof)
        moved = diff(_apply(current, derived), current)
        rendered = _splice(text, derived, current)
        if not moved and rendered != text:
            moved = [
                f"{self.label}: derived values are current but the checked-in layout differs from "
                "the generator's rendering"
            ]
        return moved, rendered


# ----------------------------------------------------------- candidate audit


def _build_anchor(
    contract: Any, repository: str, relative: str, forest: dict[str, Any]
) -> dict[str, str]:
    """Derive the build anchor of one candidate source file."""

    if repository != forest["repository"]:
        return {
            "repository": repository,
            "path": relative,
            "sha256": _sha256(contract, repository, relative),
            "relation": _WORKSPACE_RELATION,
        }
    root = contract._repository_root(repository)
    for candidate in (PurePosixPath(relative).parent, *PurePosixPath(relative).parent.parents):
        anchor = (candidate / "CMakeLists.txt").as_posix()
        if (root / anchor).is_file():
            return {
                "repository": repository,
                "path": anchor,
                "sha256": _sha256(contract, repository, anchor),
                "relation": _CMAKE_RELATION,
            }
    fallback = forest["build_file"]
    if not (root / fallback).is_file():
        raise DerivationError(
            f"{repository}:{relative}: no CMake ancestor and the source forest build file is missing"
        )
    return {
        "repository": repository,
        "path": fallback,
        "sha256": _sha256(contract, repository, fallback),
        "relation": _FALLBACK_RELATION,
    }


def audit_derivation(contract: Any, inventory: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    """Derive the candidate audit, keeping the reviewed classifications as input."""

    if not isinstance(audit, dict) or not isinstance(audit.get("entries"), list):
        raise DerivationError("candidate audit: registry has no entries list")
    owners = contract._owner_paths(inventory)
    exclusions = {
        f"{entry['repository']}:{entry['path']}" for entry in inventory["excluded_mutations"]
    }
    discovered = contract._discover_runtime_mutation_paths(inventory)
    expected = sorted(
        (key for key in discovered if key not in owners and key not in exclusions),
        key=lambda key: tuple(key.split(":", 1)),
    )
    reviewed: dict[tuple[str, str], tuple[str, str]] = {}
    for entry in audit["entries"]:
        if not isinstance(entry, dict) or not {"repository", "path", "classification", "reason"} <= set(entry):
            raise DerivationError("candidate audit: entry lacks repository/path/classification/reason")
        key = (entry["repository"], entry["path"])
        if key in reviewed:
            raise DerivationError(f"candidate audit: duplicate entry {key[0]}:{key[1]}")
        reviewed[key] = (entry["classification"], entry["reason"])
    unclassified = [key for key in expected if tuple(key.split(":", 1)) not in reviewed]
    if unclassified:
        raise DerivationError(
            "candidate audit: discovered candidates have no reviewed classification: "
            f"{unclassified}; review and classify them before regenerating"
        )
    forest = inventory["source_forest"]
    entries = []
    for key in expected:
        repository, relative = key.split(":", 1)
        classification, reason = reviewed[(repository, relative)]
        if classification not in contract.AUDIT_CLASSIFICATIONS:
            raise DerivationError(
                f"candidate audit[{key}].classification: {classification!r} is not a reviewed class"
            )
        if relative not in reason:
            raise DerivationError(
                f"candidate audit[{key}].reason: the reviewed reason does not name the candidate path"
            )
        source = (contract._repository_root(repository) / relative).read_text(
            encoding="utf-8", errors="replace"
        )
        entries.append(
            {
                "repository": repository,
                "path": relative,
                "sha256": _sha256(contract, repository, relative),
                "classification": classification,
                "reason": reason,
                "evidence": {
                    "runtime_scope": contract.AUDIT_CLASSIFICATION_SCOPES[classification],
                    "mutation_operators": contract._audit_mutation_operators(source),
                    "build_anchor": _build_anchor(contract, repository, relative, forest),
                },
            }
        )
    canonical = json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "kind": AUDIT_KIND,
        "coverage_tier": AUDIT_TIER,
        "count": len(entries),
        "entries_digest": hashlib.sha256(canonical).hexdigest(),
        "entries": entries,
    }


def render_audit(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


class AuditTarget:
    """The candidate audit: re-emitted in the layout its contract validates."""

    def __init__(self, path: Path, contract: Any, inventory: dict[str, Any]) -> None:
        self.label = f"lifecycle/{path.name}"
        self.path = path
        self.contract = contract
        self.inventory = inventory

    def evaluate(self) -> tuple[list[str], str]:
        current = load_json(self.path)
        document = audit_derivation(self.contract, self.inventory, current)
        return diff(document, current), render_audit(document)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--audit", type=Path, help="candidate audit path (default: the inventory's reference)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero instead of rewriting the registries",
    )
    parser.add_argument(
        "--mig-proof",
        action="store_true",
        help="also replay and compare the MIG output proof (needs cmake/ninja/clang)",
    )
    args = parser.parse_args(argv)
    contract = _contract()
    inventory_path = args.inventory.resolve()
    inventory = load_json(inventory_path)
    audit_path = (
        args.audit.resolve()
        if args.audit is not None
        else ROOT / inventory["scan"]["audited_non_shared_candidates"]["path"]
    )
    return run(
        [
            InventoryTarget(inventory_path, contract, mig_proof=args.mig_proof),
            AuditTarget(audit_path, contract, inventory),
        ],
        check=args.check,
        heading="namespace-writer registry derivation",
    )


if __name__ == "__main__":
    raise SystemExit(main())

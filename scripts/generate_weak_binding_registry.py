#!/usr/bin/env python3
"""Generate lifecycle/weak-binding-registry-v1.json from the Darling source forest.

The registry is the reviewed census of every definition in the forest that can
satisfy a guest dynamic binding while doing nothing -- the defect class ADR-0004
names, measured on 2026-10-10 when libsystem_kernel exported an empty weak
`os_unfair_lock_lock`/`os_unfair_lock_unlock` and every guest lock became a
no-op.

What this script derives, from the tree and from the contract that already owns
the scan grammar (tests/west_test_contracts/weak_binding_registry_contract.py):

  * the candidate set: every weak-attributed file-scope non-static definition,
    plus every explicitly default-visible definition whose whole body is empty,
    `return 0;`, `return -ENOSYS;` or `return NULL;`
  * each entry's discovery label (weak-linkage / placeholder-body) and the
    measured character of its body (void / silent / loud / none)
  * the SHA-256 binding of every anchored source file and of every strong side
  * the candidate, classified and retired counts, and the digest over the
    entries

What it never invents, because these are reviewed data rather than derivation
output:

  * the status, origin, kind, body character, caller-dependency note and
    retirement record of each entry
  * the strong side's location when the entry has one
  * any entry at all: a discovered candidate with no reviewed classification is
    refused by name, so the classification map stays an input to this script
    rather than an output of it, and a classified entry that the tree no longer
    contains is refused too, because a silent disappearance is the other half of
    the same failure

Usage:

  python3 -B scripts/generate_weak_binding_registry.py
      rewrite the registry from the current tree (no-op when current)
  python3 -B scripts/generate_weak_binding_registry.py --check
      fail, naming every disagreeing entry or field, when the tree and the
      registry disagree; writes nothing
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from registry_derivation import DerivationError, diff, run  # noqa: E402

CONTRACT_PATH = ROOT / "tests" / "west_test_contracts" / "weak_binding_registry_contract.py"
DEFAULT_REGISTRY = ROOT / "lifecycle" / "weak-binding-registry-v1.json"


def _contract() -> Any:
    """Load the contract module that owns the scan grammar and its validators."""

    spec = importlib.util.spec_from_file_location("weak_binding_registry_contract", CONTRACT_PATH)
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


def _sha256(contract: Any, repository: str, relative: str) -> str:
    absolute = contract._repository_root(repository) / relative
    if not absolute.is_file() or absolute.is_symlink():
        raise DerivationError(f"{repository}:{relative}: derived digest source is not a regular file")
    return contract._sha256_file(absolute)


def _dotted(path: tuple[Any, ...]) -> str:
    out = ""
    for step in path:
        out = f"{out}[{step}]" if isinstance(step, int) else (f"{out}.{step}" if out else str(step))
    return out


def _apply(document: Any, derived: dict[tuple[Any, ...], Any]) -> Any:
    """Return the document with every derived scalar replaced, for field diffing."""

    updated = json.loads(json.dumps(document))
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

    Only scalars are spliced: the reviewed layout of the registry -- its prose,
    the order of its fields, the order of its entries -- is preserved byte for
    byte, and a derived value that is not a scalar is refused for review rather
    than rewritten behind the reviewer's back.
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


def derivation(
    contract: Any, registry: dict[str, Any], discovered: dict[tuple[str, str], dict[str, Any]]
) -> dict[tuple[Any, ...], Any]:
    """Return every derived scalar of the registry, addressed by JSON path."""

    derived: dict[tuple[Any, ...], Any] = {}
    classified = retired = 0
    for index, entry in enumerate(registry["entries"]):
        base = ("entries", index)
        derived[base + ("sha256",)] = _sha256(contract, entry["repository"], entry["path"])
        strong_side = entry["strong_side"]
        if isinstance(strong_side, dict):
            derived[base + ("strong_side", "sha256")] = _sha256(
                contract, strong_side["repository"], strong_side["path"]
            )
        if entry["status"] == "classified":
            classified += 1
            found = discovered.get((entry["path"], entry["name"]))
            if found is None:
                raise DerivationError(
                    f"{entry['path']}:{entry['name']}: no candidate to derive from; the "
                    "classification is stale and must be reviewed before the registry is rewritten"
                )
            derived[base + ("discovery",)] = found["discovery"]
            derived[base + ("fault_mode",)] = found["fault_mode"]
            # A discovered candidate defines the name; the import shape is the
            # retired entry's reviewed record of what the removal left behind.
            derived[base + ("image_role",)] = "defined"
        else:
            retired += 1
            derived[base + ("discovery",)] = None
            derived[base + ("fault_mode",)] = None
    derived[("counts", "candidate_count")] = len(discovered)
    derived[("counts", "classified_count")] = classified
    derived[("counts", "retired_count")] = retired
    # The entries digest covers the entries as they will be written, so it is
    # computed over the document with the derived scalars already applied.
    derived[("entries_digest",)] = contract.entries_digest(_apply(registry, derived)["entries"])
    return derived


class RegistryTarget:
    """The registry: derived scalars spliced into the reviewed layout."""

    label = "lifecycle/weak-binding-registry-v1.json"

    def __init__(self, path: Path, contract: Any) -> None:
        self.path = path
        self.contract = contract

    @staticmethod
    def _labelled(current: dict[str, Any], moved: list[str]) -> list[str]:
        """Name the entry each ``entries[i]`` disagreement belongs to."""

        labels = {
            f"entries[{index}]": f"{entry.get('path')}:{entry.get('name')}"
            for index, entry in enumerate(current.get("entries", []))
        }
        labelled: list[str] = []
        for line in moved:
            head = line.split(":", 1)[0].split(".", 1)[0]
            label = labels.get(head)
            labelled.append(f"{label}: {line}" if label else line)
        return labelled

    def evaluate(self) -> tuple[list[str], str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise DerivationError(f"{self.label}: cannot read registry: {error}") from error
        current = load_json(self.path)
        refused = self.contract.classification_problems(current)
        if refused:
            raise DerivationError(
                "the registry cannot be regenerated while its classifications are wrong:\n  "
                + "\n  ".join(refused)
            )
        discovered = self.contract.discover()
        values = derivation(self.contract, current, discovered)
        updated = _apply(current, values)
        moved = self._labelled(current, diff(updated, current))
        rendered = _splice(text, values, current)
        if not moved and rendered != text:
            moved = [
                f"{self.label}: derived values are current but the checked-in layout differs from "
                "the generator's rendering"
            ]
        return moved, rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero instead of rewriting the registry",
    )
    args = parser.parse_args(argv)
    contract = _contract()
    return run(
        [RegistryTarget(args.registry.resolve(), contract)],
        check=args.check,
        heading="weak-binding registry derivation",
    )


if __name__ == "__main__":
    raise SystemExit(main())

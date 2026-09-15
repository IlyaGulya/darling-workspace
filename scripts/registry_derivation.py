#!/usr/bin/env python3
"""Shared plumbing for the checked-in registry generators.

A registry generator derives its document from the tree and either compares it
to the checked-in file (``--check``, drift is red) or rewrites the file.  This
module holds the pieces every generator needs: the one exception type, a
field-addressed structural diff, and the two driver modes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class DerivationError(AssertionError):
    """A registry disagrees with the derivation inputs, or an input is invalid."""


class EvaluableTarget(Protocol):
    """A registry a generator can compare against its own derivation."""

    label: str
    path: Path

    def evaluate(self) -> tuple[list[str], str]:
        """Return the disagreeing fields and the rendered derivation."""


@dataclass(frozen=True)
class Target:
    """One checked-in registry, its reader, its derivation and its renderer."""

    label: str
    path: Path
    load: Callable[[Path], Any]
    derive: Callable[[Any], Any]
    render: Callable[[Any], str]

    def evaluate(self) -> tuple[list[str], str]:
        """Return the disagreeing fields and the rendered derivation."""

        try:
            current = self.load(self.path)
            on_disk = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise DerivationError(f"{self.label}: cannot read registry: {error}") from error
        document = self.derive(current)
        moved = diff(document, current)
        rendered = self.render(document)
        if not moved and on_disk != rendered:
            moved = [
                f"{self.label}: values agree but the checked-in layout differs from the "
                "generator layout"
            ]
        return moved, rendered


def diff(expected: Any, observed: Any, path: str = "") -> list[str]:
    """Return one line per leaf that differs between two documents."""

    if isinstance(expected, dict) and isinstance(observed, dict):
        if list(expected) != list(observed):
            return [f"{path or '<root>'}: fields {list(observed)} differ from {list(expected)}"]
        lines: list[str] = []
        for key in expected:
            lines.extend(diff(expected[key], observed[key], f"{path}.{key}" if path else key))
        return lines
    if isinstance(expected, list) and isinstance(observed, list):
        lines: list[str] = []
        if len(expected) != len(observed):
            lines.append(f"{path}: length registry={len(observed)} derived={len(expected)}")
            lines.extend(_unpaired(expected, observed, path, side="derived"))
            lines.extend(_unpaired(observed, expected, path, side="registry"))
        lines.extend(
            line
            for index in range(min(len(expected), len(observed)))
            if expected[index] != observed[index]
            for line in diff(expected[index], observed[index], f"{path}[{index}]")
        )
        return lines
    if expected != observed:
        return [f"{path}: registry={observed!r} derived={expected!r}"]
    return []


_IDENTIFIERS = ("path", "id", "repository", "profile", "patch", "cmake", "source", "name")


def _unpaired(longer: list[Any], shorter: list[Any], path: str, *, side: str) -> list[str]:
    """Name every entry that exists only on the given side."""

    lines: list[str] = []
    for index in range(len(shorter), len(longer)):
        item = longer[index]
        identity = next((item[key] for key in _IDENTIFIERS if isinstance(item, dict) and key in item), None)
        suffix = f".{_identifier_key(item)}" if identity is not None else ""
        lines.append(f"{path}[{index}]{suffix}: {identity if identity is not None else item!r} only in {side}")
    return lines


def _identifier_key(item: Any) -> str:
    return next((key for key in _IDENTIFIERS if isinstance(item, dict) and key in item), "")


def run(targets: list[EvaluableTarget], *, check: bool, heading: str) -> int:
    """Compare or refresh every target; return the process exit status."""

    evaluations: list[tuple[EvaluableTarget, list[str], str]] = []
    for target in targets:
        try:
            moved, rendered = target.evaluate()
        except (AssertionError, RuntimeError, OSError, ValueError, KeyError) as error:
            print(f"{heading}: FAIL\n  {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        evaluations.append((target, moved, rendered))
    if check:
        failures = [line for _, moved, _ in evaluations for line in moved]
        if failures:
            print(f"{heading}: FAIL", file=sys.stderr)
            for line in failures:
                print(f"  {line}", file=sys.stderr)
            return 1
        for target, _, _ in evaluations:
            print(f"{target.label}: derivation PASS")
        return 0
    for target, moved, rendered in evaluations:
        if target.path.read_text(encoding="utf-8") != rendered:
            target.path.write_text(rendered, encoding="utf-8")
        for line in moved:
            print(f"{target.label}: refreshed {line}")
        print(f"{target.label}: current")
    return 0

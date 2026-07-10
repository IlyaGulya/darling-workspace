"""Pure patch-metadata selection for ``west test``."""

from __future__ import annotations

from typing import Callable


class MetadataSelection:
    """Selected tests plus patches that need a test/exception decision."""

    def __init__(self, selected: list[tuple[dict, dict]], missing: list[dict], found_patch: bool):
        self.selected = selected
        self.missing = missing
        self.found_patch = found_patch


def select_metadata_tests(
    profile: dict,
    *,
    patch_path: str | None,
    bead: str | None,
    env: str | None,
    diag: str | None,
    red_only: bool,
    resolved_diag: Callable[[dict], str],
) -> MetadataSelection:
    """Select normalized metadata without depending on a West command object."""

    selected = []
    missing = []
    found_patch = False
    for patch in profile.get("patches", []):
        if patch_path and patch["path"] != patch_path:
            continue
        found_patch = True
        if bead and patch.get("bead") != bead:
            continue
        all_tests = [test for test in (patch.get("tests") or []) if not test.get("blocked")]
        tests = all_tests
        if red_only:
            tests = [test for test in tests if test.get("red")]
        if env:
            tests = [test for test in tests if test.get("env") == env]
        if diag:
            tests = [test for test in tests if resolved_diag(test) == diag]
        selected.extend((patch, test) for test in tests)
        if not tests and not all_tests and not patch.get("test-exception"):
            missing.append(patch)
    return MetadataSelection(selected, missing, found_patch)

"""Pure patch-metadata selection for ``west test``."""

from __future__ import annotations

import re
from typing import Callable

from test_ctest import is_ctest_binding


class MetadataSelection:
    """Selected tests plus patches that need a test/exception decision."""

    def __init__(self, selected: list[tuple[dict, dict]], missing: list[dict], found_patch: bool, blocked: list[tuple[dict, dict]]):
        self.selected = selected
        self.missing = missing
        self.found_patch = found_patch
        self.blocked = blocked


def select_metadata_tests(
    profile: dict,
    *,
    patch_path: str | None,
    bead: str | None,
    env: str | None,
    diag: str | None,
    label: str | None,
    red_only: bool,
    resolved_diag: Callable[[dict], str],
    validation_group: str | None = None,
    defer_ctest: bool = False,
) -> MetadataSelection:
    """Select normalized metadata without depending on a West command object."""

    if validation_group is not None and validation_group not in {"homebrew"}:
        raise ValueError(f"unknown guest Mach-O validation group: {validation_group}")

    selected = []
    missing = []
    blocked = []
    found_patch = False
    for patch in profile.get("patches", []):
        if patch_path and patch["path"] != patch_path:
            continue
        found_patch = True
        if bead and patch.get("bead") != bead:
            continue
        all_tests = [test for test in (patch.get("tests") or []) if not test.get("blocked")]
        tests = patch.get("tests") or []
        if red_only:
            tests = [test for test in tests if test.get("red")]
        if env:
            tests = [test for test in tests if (defer_ctest and is_ctest_binding(test)) or test.get("env") == env]
        if validation_group:
            tests = [
                test
                for test in tests
                if test.get("runner") == "guest-macho-fixture"
                and test.get("validation-group") == validation_group
            ]
        if diag:
            tests = [test for test in tests if (defer_ctest and is_ctest_binding(test)) or resolved_diag(test) == diag]
        if label:
            matcher = re.compile(label)
            tests = [
                test
                for test in tests
                if (defer_ctest and is_ctest_binding(test))
                or any(matcher.search(item) for item in metadata_test_labels(patch, test, resolved_diag))
            ]
        blocked.extend((patch, test) for test in tests if test.get("blocked"))
        selected.extend((patch, test) for test in tests if not test.get("blocked"))
        if not all_tests and not patch.get("test-exception"):
            missing.append(patch)
    return MetadataSelection(selected, missing, found_patch, blocked)


def metadata_test_labels(
    patch: dict, test: dict, resolved_diag: Callable[[dict], str]
) -> set[str]:
    labels = {
        f"env:{test.get('env', 'host')}",
        f"diag:{resolved_diag(test)}",
    }
    if test.get("name"):
        labels.add(f"name:{test['name']}")
    if test.get("validation-group"):
        labels.add(f"guest-macho-group:{test['validation-group']}")
    if patch.get("bead"):
        labels.add(f"bead:{patch['bead']}")
    modules = test.get("submodules") or [patch.get("module")]
    if isinstance(modules, str):
        modules = [modules]
    for module in modules:
        if module:
            labels.add(f"submod:{module}")
            labels.add(f"submod:{str(module).rsplit('/', 1)[-1]}")
    explicit = test.get("labels") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    labels.update(str(item) for item in explicit)
    labels.update(test.get("_ctest", {}).get("labels", []))
    for axis in ("smoke", "fuzz", "stress"):
        if test.get(axis):
            labels.add(f"{axis}:true")
    return labels


def select_metadata_tests_for_command(
    command,
    profile: str,
    patch_path: str | None,
    bead: str | None,
    env: str | None,
    diag: str | None,
    label: str | None,
    red_only: bool,
    validation_group: str | None = None,
):
    selection = select_metadata_tests(
        command._load_profile(profile),
        patch_path=patch_path,
        bead=bead,
        env=env,
        diag=diag,
        label=label,
        validation_group=validation_group,
        red_only=red_only,
        resolved_diag=command._resolved_diag,
        defer_ctest=True,
    )
    if patch_path and not selection.found_patch:
        command.die(f"{profile}: patch not found or has no selected tests: {patch_path}")
    for patch, test in selection.blocked:
        if env and test.get("env") and test["env"] != env:
            continue
        identity = test.get("ctest-name") or test.get("name") or test.get("ctest-label")
        reason = test.get("note") or patch.get("publication-blocker") or "explicitly blocked in metadata"
        command.inf(f"{patch['path']}: {identity} BLOCKED: {reason}")
    return selection.selected, selection.missing

"""Pure source-independent workload selection for ``west test``."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from test_ctest import is_ctest_binding


class MetadataSelection:
    """Selected and explicitly blocked workload invocations."""

    def __init__(self, selected: list[tuple[dict, dict]], blocked: list[tuple[dict, dict]]):
        self.selected = selected
        self.blocked = blocked


def _resolved_diag(test: dict) -> str:
    return str(test.get("diag", "bare"))


def workload_owner(test: dict) -> dict:
    """Adapt a flat declaration to the structured runners' owner/test API.

    ``path`` is an invocation label, not a patch or source path.
    """

    owner = {"id": test["id"], "path": test["id"], "module": test["module"]}
    if test.get("bead"):
        owner["bead"] = test["bead"]
    return owner


def select_workload_tests(
    data: dict,
    *,
    workload: str | Sequence[str] | None = None,
    bead: str | None = None,
    submodule: str | None = None,
    env: str | None = None,
    diag: str | None = None,
    label: str | None = None,
    validation_group: str | None = None,
    resolved_diag: Callable[[dict], str] = _resolved_diag,
    defer_ctest: bool = False,
) -> MetadataSelection:
    """Select stable ids or named groups in their declared execution order.

    Alias overlap selects each declaration once. Distinct declarations remain
    distinct even when they have the same display name or command.
    """

    if validation_group is not None and validation_group not in {"homebrew"}:
        raise ValueError(f"unknown guest Mach-O validation group: {validation_group}")
    by_id = {test["id"]: test for test in data["workloads"]}
    requested = [workload] if isinstance(workload, str) else workload
    entries = data["workloads"]
    if requested is not None:
        wanted = []
        for name in requested:
            if name in by_id:
                members = [name]
            elif name in data.get("aliases", {}):
                members = data["aliases"][name]
            else:
                raise ValueError(f"unknown workload: {name}")
            for member in members:
                if member not in wanted:
                    wanted.append(member)
        entries = [by_id[identifier] for identifier in wanted]
    selected = []
    blocked = []
    for test in entries:
        choice = select_metadata_tests(
            workload_owner(test), [test], bead=bead, submodule=submodule,
            env=env, diag=diag, label=label, validation_group=validation_group,
            resolved_diag=resolved_diag, defer_ctest=defer_ctest,
        )
        selected.extend(choice.selected)
        blocked.extend(choice.blocked)
    return MetadataSelection(selected, blocked)


def select_metadata_tests(
    owner: dict,
    tests: Sequence[dict],
    *,
    bead: str | None = None,
    submodule: str | None = None,
    env: str | None = None,
    diag: str | None = None,
    label: str | None = None,
    validation_group: str | None = None,
    resolved_diag: Callable[[dict], str] = _resolved_diag,
    defer_ctest: bool = False,
) -> MetadataSelection:
    """Apply selectors to declarations or resolved CTest registration variants."""

    if validation_group is not None and validation_group not in {"homebrew"}:
        raise ValueError(f"unknown guest Mach-O validation group: {validation_group}")
    matcher = re.compile(label) if label else None
    selected = []
    blocked = []
    for test in tests:
        if bead and test.get("bead", owner.get("bead")) != bead:
            continue
        labels = metadata_test_labels(owner, test, resolved_diag)
        if submodule and f"submod:{submodule}" not in labels:
            continue
        deferred = defer_ctest and is_ctest_binding(test)
        if env and not deferred and test.get("env") != env:
            continue
        if diag and not deferred and resolved_diag(test) != diag:
            continue
        if matcher and not deferred and not any(matcher.search(item) for item in labels):
            continue
        if validation_group and (
            test.get("runner") != "guest-macho-fixture"
            or test.get("validation-group") != validation_group
        ):
            continue
        (blocked if test.get("blocked") else selected).append((owner, test))
    return MetadataSelection(selected, blocked)


def metadata_test_labels(
    owner: dict, test: dict, resolved_diag: Callable[[dict], str]
) -> set[str]:
    labels = {
        f"env:{test.get('env', 'host')}",
        f"diag:{resolved_diag(test)}",
    }
    if test.get("id"):
        labels.add(f"workload:{test['id']}")
    if test.get("name"):
        labels.add(f"name:{test['name']}")
    if test.get("validation-group"):
        labels.add(f"guest-macho-group:{test['validation-group']}")
    bead = test.get("bead", owner.get("bead"))
    if bead:
        labels.add(f"bead:{bead}")
    modules = test.get("submodules") or [test.get("module", owner.get("module"))]
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


def metadata_invocation_identity(
    invocation: Mapping[str, Any], test: Mapping[str, Any]
) -> tuple[Any, ...]:
    """Keep distinct declarations even when their commands coincide."""

    return (
        test.get("id"),
        invocation.get("key"),
        str(test.get("env", "host")),
        str(invocation.get("diag") or ""),
    )


def metadata_selection_plan(
    names: Sequence[str], bundle_root: object
) -> list[str]:
    """Return the lines stating what a metadata selection will run, in order.

    A test that is selected but produces no evidence is indistinguishable from
    one that never ran unless the run says what it selected and where a guarded
    or forensic diagnostic's bundle would go. The value is the debug bundle
    root, not the runtime evidence store, and the line names it as such: two
    roots that both called themselves the evidence root is how a scoped-looking
    run came to collect the wrong one.
    """

    listed = ", ".join(names) if names else "none"
    return [
        f"metadata selection: {len(names)} test(s), in execution order: {listed}",
        f"  diagnostic bundle root for these tests: {bundle_root}",
    ]


def metadata_test_outcome(
    name: str, returncode: int, bundle: object | None
) -> str:
    """Return the closing line for one executed metadata test.

    Not every diagnostic mode writes a bundle: a guarded fixture prints its
    verdict in the job log, so a missing bundle must not read as "did not run".
    """

    state = "passed" if returncode == 0 else f"failed rc={returncode}"
    if bundle is None:
        return f"  {name}: {state}; no bundle (diagnostics stay in the job log)"
    return f"  {name}: {state}; bundle {bundle}"


def metadata_run_summary(counts: Mapping[str, int]) -> str:
    """Return the closing summary of what a metadata selection did."""

    duplicate = counts.get("duplicate", 0)
    verdict = counts.get("verdict", 0)
    return (
        f"metadata selection summary: executed {counts.get('executed', 0)} "
        f"({counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed), "
        f"skipped {duplicate + verdict} (duplicate invocation {duplicate}, "
        f"reused verdict {verdict}) of {counts.get('selected', 0)} selected"
    )

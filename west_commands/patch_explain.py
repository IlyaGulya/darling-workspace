"""Read-only projection of typed patch-stack state for humans and tools."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

import patch_stack_materialize
import patch_stack_profile_composition

SCHEMA_VERSION = 1
DEFAULT_ROW_CAP = 8
_CLASSIFICATIONS = ("matched", "missing", "mismatched", "unavailable")


class ExplainError(RuntimeError):
    pass


def shell_command(*arguments: str) -> str:
    """Return a directly reproducible, shell-quoted command."""
    return shlex.join(arguments)


def full_details_command(operation: str, profile: str) -> str:
    return shell_command("west", "patch", operation, "--profile", profile, "--full")


def bounded_lines(
    operation: str,
    profile: str,
    summary: str,
    findings: Sequence[str],
    *,
    full: bool = False,
    details_command: str | None = None,
    cap: int = DEFAULT_ROW_CAP,
) -> list[str]:
    """Format bounded default output and an exact full-details instruction."""
    if cap < 1:
        raise ValueError("row cap must be positive")
    rows = list(findings if full else findings[:cap])
    lines = [summary]
    omitted = len(findings) - len(rows)
    if not full:
        lines.append(
            f"full details: {details_command or full_details_command(operation, profile)}"
        )
    lines.extend(rows)
    if omitted:
        lines.append(f"omitted: {omitted} finding(s)")
    return lines


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_NO_LAZY_FETCH"] = "1"
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ExplainError(f"cannot run local Git in {repo}: {error}") from error


def _missing_revision(result: subprocess.CompletedProcess[str]) -> bool:
    diagnostic = result.stderr.lower()
    return result.returncode in (1, 128) and any(
        marker in diagnostic
        for marker in (
            "needed a single revision",
            "unknown revision",
            "not a valid object name",
            "ambiguous argument",
            "bad object",
            "invalid object",
            "unable to read tree",
            "could not fetch",
            "promisor",
        )
    )


def _tree(repo: Path, revision: str) -> str | None:
    result = _git(repo, "rev-parse", "--verify", f"{revision}^{{tree}}")
    if result.returncode == 0:
        return result.stdout.strip()
    if _missing_revision(result):
        return None
    raise ExplainError(
        f"git rev-parse {revision}^{{tree}} failed ({result.returncode}): "
        f"{result.stderr.strip()}"
    )


def repo_available(repo: Path) -> bool:
    if not repo.is_dir():
        return False
    result = _git(repo, "rev-parse", "--git-dir")
    if result.returncode == 0:
        return True
    if result.returncode in (1, 128):
        return False
    raise ExplainError(
        f"git rev-parse --git-dir failed ({result.returncode}): {result.stderr.strip()}"
    )


def error_result(
    operation: str,
    profile: str | None,
    message: str,
    *,
    exit_code: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "profile": profile,
        "status": "error",
        "error": {"message": message},
        "exit_code": exit_code,
    }


def _branch_oid(repo: Path, branch: str) -> str | None:
    ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
    result = _git(repo, "show-ref", "--verify", ref)
    if result.returncode == 0:
        fields = result.stdout.split()
        if len(fields) == 2 and fields[1] == ref:
            return fields[0]
        raise ExplainError(f"git show-ref returned malformed output for {ref}")
    if result.returncode == 1 or (
        result.returncode == 128
        and "not a valid ref" in result.stderr.lower()
    ):
        return None
    raise ExplainError(
        f"git show-ref --verify {ref} failed ({result.returncode}): "
        f"{result.stderr.strip()}"
    )


def inspect_integration(
    module: str,
    repo: Path | None,
    expected_tree: str,
    all_expected: Mapping[str, str],
    repos: Mapping[str, Path | None],
    branch: str,
    *,
    inherited_children: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    state = {
        "applied_tree": None,
        "classification": "unavailable",
        "detail": "repository is locally unavailable",
        "unavailable_module": module,
        "recovery": "west_update",
    }
    if repo is None or not repo_available(repo):
        return state
    oid = _branch_oid(repo, branch)
    if oid is None:
        return {
            **state,
            "classification": "missing",
            "detail": f"{branch} is locally unavailable",
            "unavailable_module": None,
            "recovery": "patch_apply",
        }
    applied_tree = _tree(repo, oid)
    if applied_tree is None:
        return {
            **state,
            "detail": f"{branch} exists but its commit/tree is locally unreadable",
            "unavailable_module": None,
            "recovery": "patch_apply",
        }
    state["applied_tree"] = applied_tree
    state["unavailable_module"] = None
    state["recovery"] = None
    if module != "darling":
        if applied_tree == expected_tree:
            state.update(
                classification="matched",
                detail="typed integration tree matches",
            )
        else:
            state.update(
                classification="mismatched",
                detail=(
                    f"integration tree {applied_tree} differs from typed "
                    f"profile final tree {expected_tree}"
                ),
            )
        return state

    difference = _git(repo, "diff-tree", "--raw", "-r", expected_tree, applied_tree)
    if difference.returncode:
        if _missing_revision(difference):
            state["detail"] = "typed or applied integration tree is locally unreadable"
            return state
        raise ExplainError(
            f"git diff-tree failed ({difference.returncode}): "
            f"{difference.stderr.strip()}"
        )
    expected_children = dict(inherited_children or {})
    expected_children.update(all_expected)
    children = {
        name for name in expected_children if name.startswith("darling/")
    }
    relative_children = {
        str(Path(name).relative_to("darling")): name for name in children
    }
    for line in filter(None, difference.stdout.splitlines()):
        fields = line.split("\t", 1)
        if len(fields) != 2:
            raise ExplainError("darling integration diff is malformed")
        meta, path = fields
        modes = meta.split()[:2]
        if modes:
            modes[0] = modes[0].lstrip(":")
        if path not in relative_children or modes != ["160000", "160000"]:
            state.update(
                classification="mismatched",
                detail="darling integration changed non-gitlink content",
            )
            return state
    for child in sorted(children):
        child_repo = repos.get(child)
        if child_repo is None or not repo_available(child_repo):
            state.update(
                detail=f"dependency repository is locally unavailable: {child}",
                unavailable_module=child,
                recovery="west_update",
            )
            return state
        if child in all_expected:
            child_oid = _branch_oid(child_repo, branch)
        else:
            relative = Path(child).relative_to("darling")
            recorded = _git(repo, "rev-parse", "--verify", f"{oid}:{relative}")
            if recorded.returncode and not _missing_revision(recorded):
                raise ExplainError(f"cannot read dependency gitlink {child}: {recorded.stderr.strip()}")
            child_oid = recorded.stdout.strip() if recorded.returncode == 0 else None
        child_tree = _tree(child_repo, child_oid) if child_oid is not None else None
        if child_tree is None:
            state.update(
                detail=f"dependency integration ref/tree is locally unavailable: {child}",
                unavailable_module=None,
                recovery="patch_apply",
            )
            return state
        if child_tree != expected_children[child]:
            state.update(
                classification="mismatched",
                detail=f"darling integration child {child} differs from typed final tree",
            )
            return state
    state.update(classification="matched", detail="typed integration tree matches")
    return state


def transitive_dependency_order(
    composition: Mapping[str, Any],
    load_dependency,
) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(current: Mapping[str, Any]) -> None:
        for dependency in current.get("prerequisites", []):
            profile = dependency["profile"]
            if profile in seen:
                continue
            if profile in visiting:
                raise ExplainError("typed profile dependency cycle")
            visiting.add(profile)
            child = load_dependency(dependency)
            if child.get("profile") != profile:
                raise ExplainError(
                    f"typed prerequisite composition differs for {profile}"
                )
            visit(child)
            visiting.remove(profile)
            seen.add(profile)
            order.append(profile)

    visit(composition)
    return order


def _selected_entries(
    entries: Sequence[dict[str, str]],
    *,
    module: str | None,
    series: str | None,
) -> list[dict[str, str]]:
    if module is not None and series is not None:
        raise ExplainError("--module and --series are mutually exclusive")
    selected = [
        entry
        for entry in entries
        if (module is None or entry["module"] == module)
        and (series is None or entry["patch"] == series)
    ]
    if module is not None and not selected:
        raise ExplainError(f"unknown module selector: {module}")
    if series is not None and not selected:
        raise ExplainError(f"unknown series selector: {series}")
    return selected


def explain(
    profile: str,
    plan: Sequence[dict[str, str]],
    repos: Mapping[str, Path | None],
    *,
    module: str | None = None,
    series: str | None = None,
) -> dict[str, Any]:
    """Inspect a typed plan without fetching, materializing, or changing refs."""
    composition = getattr(plan, "composition", None)
    if not isinstance(composition, dict):
        raise ExplainError(f"{profile}: typed profile composition is required")
    entries = _selected_entries(plan, module=module, series=series)
    expected_finals = composition["integration_finals"]
    locks = {
        (entry["module"], entry["patch"]): patch_stack_materialize.load_lock(
            Path(entry["lock_path"])
        )
        for entry in plan
    }
    boundary_offsets: dict[tuple[str, str], int] = {}
    by_module: dict[str, list[dict[str, str]]] = {}
    for entry in plan:
        by_module.setdefault(entry["module"], []).append(entry)
    for name, module_entries in by_module.items():
        trailing = (
            1
            if name == "darling"
            and any(child.startswith("darling/") for child in expected_finals)
            else 0
        )
        for entry in reversed(module_entries):
            key = (name, entry["patch"])
            boundary_offsets[key] = trailing
            trailing += len(locks[key]["ordered_commits"])
    branch = f"integration/{profile}"
    repo_states = {
        name: inspect_integration(
            name,
            repos.get(name),
            expected_finals[name],
            expected_finals,
            repos,
            branch,
            inherited_children=composition.get("inherited_children", {}),
        )
        for name in dict.fromkeys(entry["module"] for entry in entries)
    }

    rows: list[dict[str, Any]] = []
    selected_keys = {(entry["module"], entry["patch"]) for entry in entries}
    global_order = {
        (entry["module"], entry["patch"]): index
        for index, entry in enumerate(plan, start=1)
    }
    module_order: dict[str, int] = {}
    for entry in plan:
        module_order[entry["module"]] = module_order.get(entry["module"], 0) + 1
        key = (entry["module"], entry["patch"])
        if key not in selected_keys:
            continue
        lock = locks[key]
        state = repo_states[entry["module"]]
        base_tree = source_tree = applied_tree = None
        repo = repos.get(entry["module"])
        if repo is not None and state["applied_tree"] is not None:
            applied_tree = _tree(repo, f"{branch}~{boundary_offsets[key]}")
        if state["classification"] in {"missing", "unavailable"} and applied_tree is None:
            classification = state["classification"]
            detail = state["detail"]
        elif applied_tree is None:
            classification = "unavailable"
            detail = "applied series boundary tree is locally unreadable"
        elif applied_tree == composition["boundaries"][key]:
            classification = "matched"
            detail = "typed series boundary tree matches"
        else:
            classification = "mismatched"
            detail = "applied series boundary tree differs from typed expected tree"
        if repo is not None and repo_available(repo):
            base_tree = _tree(repo, lock["upstream"]["base_commit"])
            source_tree = _tree(repo, lock["source_commit"])
            if (base_tree is None or source_tree is None) and classification == "matched":
                classification = "unavailable"
                detail = "one or more immutable commit objects are locally unavailable"
        rows.append(
            {
                "order": global_order[key],
                "module_order": module_order[entry["module"]],
                "module": entry["module"],
                "series": entry["patch"],
                "lock": entry["lock_path"],
                "base_commit": lock["upstream"]["base_commit"],
                "base_tree": base_tree,
                "source_commit": lock["source_commit"],
                "source_tree": source_tree,
                "starting_tree": composition["starts"][entry["module"]]["tree"],
                "expected_tree": composition["boundaries"][key],
                "expected_integration_tree": expected_finals[entry["module"]],
                "applied_tree": applied_tree,
                "applied_integration_tree": state["applied_tree"],
                "integration_classification": state["classification"],
                "integration_detail": state["detail"],
                "unavailable_module": state["unavailable_module"],
                "integration_recovery": state["recovery"],
                "classification": classification,
                "detail": detail,
            }
        )

    counts = {
        name: sum(row["classification"] == name for row in rows)
        for name in _CLASSIFICATIONS
    }
    integration_states = {
        module_name: repo_states[module_name]["classification"]
        for module_name in repo_states
    }
    integration_counts = {
        name: sum(state == name for state in integration_states.values())
        for name in _CLASSIFICATIONS
    }
    unavailable_target = next(
        (
            row["unavailable_module"]
            for row in rows
            if row["integration_classification"] == "unavailable"
            and row["integration_recovery"] == "west_update"
            and row["unavailable_module"] is not None
        ),
        None,
    )
    if unavailable_target is not None:
        recommendation = shell_command("west", "update", unavailable_target)
    elif (
        counts["missing"]
        or counts["mismatched"]
        or counts["unavailable"]
        or integration_counts["missing"]
        or integration_counts["mismatched"]
        or integration_counts["unavailable"]
    ):
        recommendation = shell_command(
            "west", "patch", "apply", "--profile", profile, "--lock-first"
        )
    else:
        recommendation = shell_command(
            "west", "patch", "status", "--profile", profile, "--strict"
        )
    lock_root = Path(plan[0]["lock_path"]).parent
    try:
        dependency_order = transitive_dependency_order(
            composition,
            lambda dependency: patch_stack_profile_composition.load(
                lock_root / dependency["composition"]
            ),
        )
    except (
        KeyError,
        OSError,
        patch_stack_profile_composition.ProfileCompositionError,
    ) as error:
        raise ExplainError(f"cannot read typed prerequisite order: {error}") from error
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "patch_explain",
        "profile": profile,
        "selector": {"module": module, "series": series},
        "dependency_order": dependency_order,
        "dependencies": [dict(item) for item in composition["prerequisites"]],
        "summary": {
            "total": len(rows),
            **counts,
            "integration": integration_counts,
        },
        "series": rows,
        "exit_code": 0,
        "recommended_command": recommendation,
    }


def human_lines(report: dict[str, Any], *, full: bool = False) -> list[str]:
    summary = report["summary"]
    dependencies = " -> ".join(report["dependency_order"]) or "none"
    integration = summary["integration"]
    headline = (
        f"patch explain: {summary['matched']} matched, {summary['missing']} missing, "
        f"{summary['mismatched']} mismatched, {summary['unavailable']} unavailable "
        f"(of {summary['total']}); integration: {integration['matched']} matched, "
        f"{integration['missing']} missing, {integration['mismatched']} mismatched, "
        f"{integration['unavailable']} unavailable; dependencies: {dependencies}"
    )
    rows = []
    for item in report["series"]:
        if (
            not full
            and item["classification"] == "matched"
            and item["integration_classification"] == "matched"
        ):
            continue
        row = (
            f"{item['classification'].upper():11} {item['order']:>3} "
            f"{item['module']} {item['series']}: {item['detail']}; "
            f"integration={item['integration_classification']} "
            f"({item['integration_detail']})"
        )
        if full:
            row += (
                f"; base_commit={item['base_commit']} base_tree={item['base_tree'] or '-'}"
                f" source_commit={item['source_commit']} source_tree={item['source_tree'] or '-'}"
                f" starting_tree={item['starting_tree']} expected_tree={item['expected_tree']}"
                f" expected_integration_tree={item['expected_integration_tree']}"
                f" applied_tree={item['applied_tree'] or '-'}"
                f" applied_integration_tree={item['applied_integration_tree'] or '-'}"
            )
        rows.append(row)
    command = ["west", "patch", "explain", "--profile", report["profile"]]
    selector = report["selector"]
    if selector["module"] is not None:
        command.extend(("--module", selector["module"]))
    elif selector["series"] is not None:
        command.extend(("--series", selector["series"]))
    command.append("--full")
    lines = bounded_lines(
        "explain",
        report["profile"],
        headline,
        rows,
        full=full,
        details_command=shell_command(*command),
    )
    lines.append(f"recommended next command: {report['recommended_command']}")
    return lines

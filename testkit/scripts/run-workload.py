#!/usr/bin/env python3
"""Register flat workloads with CTest and delegate execution to West's runner."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_ctest import installed_requirement_problems, is_ctest_binding
from test_manifest import load_workloads
from test_selection import metadata_test_labels, select_workload_tests
from test_prefix import validate_borrowed_prefix_lease


def cmake_argument(value: object) -> str:
    """Quote arbitrary labels and paths without CMake variable expansion."""
    value = str(value)
    separator = "="
    while f"]{separator}]" in value:
        separator += "="
    return f"[{separator}[{value}]{separator}]"


def ctest_timeout(test: dict) -> int:
    """Bound the whole bridge without preempting stage verdicts or capture."""
    deadline = int(test.get("timeout-seconds", 600))
    runner = test.get("runner")
    stages = 1
    if runner in {"guest-c-fixture", "guest-macho-fixture", "darling-cmake-target-fixture"}:
        # Upload/setup, compilation, and execution have independent deadlines.
        stages = 3
    elif runner == "source-build-fixture":
        stages = max(1, len(test.get("build-commands", [])) + len(test.get("run-commands", [])))
    diag = test.get("diag", "bare")
    capture_grace = 300 if diag == "forensic" else 15 if diag == "guarded" else 0
    lifecycle_grace = 0
    if test.get("env") == "darling":
        # Admission and final shutdown each use the owner's bounded attempts.
        shutdown = int(os.environ.get("WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS", "15"))
        attempts = max(1, int(os.environ.get("WEST_TEST_SHUTDOWN_ATTEMPTS", "2")))
        # Prefix readiness (15s), upload/cleanup stages (10s each), plus the
        # guest runner's 15s executor allowance must finish before CTest stops it.
        lifecycle_grace = 2 * attempts * shutdown + 15 + 10 + 10 + 15
    return stages * deadline + capture_grace + lifecycle_grace


def registration_lines(catalog: Path, executor: str = "", bundle_root: str = "") -> list[str]:
    lines = ["# Generated from flat workload declarations; do not edit."]
    for test in load_workloads(catalog)["workloads"]:
        if is_ctest_binding(test):
            continue
        owner = {"path": test["id"], "module": test["module"], "bead": test.get("bead")}
        environment = test.get("env", "host")
        name = f"{environment}/workload/{test['id']}"
        labels = sorted(metadata_test_labels(owner, test, lambda item: item.get("diag", "bare")))
        command = [sys.executable, str(Path(__file__).resolve()), "--catalog", str(catalog),
                   "--workload", test["id"]]
        if executor:
            command += ["--executor", executor]
        if bundle_root:
            command += ["--bundle-root", bundle_root]
        lines.append("add_test(NAME " + cmake_argument(name) + " COMMAND " +
                     " ".join(map(cmake_argument, command)) + ")")
        properties = {"LABELS": ";".join(labels), "TIMEOUT": ctest_timeout(test),
                      "WORKING_DIRECTORY": str(ROOT.parent), "SKIP_RETURN_CODE": 77}
        lines.append(f'if(DARLING_SRC)\n  set_property(TEST {cmake_argument(name)} APPEND PROPERTY ENVIRONMENT "DARLING_SOURCE_ROOT=${{DARLING_SRC}}")\nendif()')
        if environment == "darling":
            properties.update(RESOURCE_LOCK="darling-prefix", RUN_SERIAL="TRUE")
            lines.append(f'if(DARLING_TEST_PREFIX)\n  set_property(TEST {cmake_argument(name)} APPEND PROPERTY ENVIRONMENT "DPREFIX=${{DARLING_TEST_PREFIX}}")\nendif()')
        lines.append("set_tests_properties(" + cmake_argument(name) + " PROPERTIES " +
                     " ".join(f"{key} {cmake_argument(value)}" for key, value in properties.items()) + ")")
    return lines


def run_workload(args: argparse.Namespace) -> int:
    catalog = load_workloads(args.catalog)
    declarations = [test for test in catalog["workloads"] if test["id"] == args.workload]
    if len(declarations) != 1 or is_ctest_binding(declarations[0]):
        raise SystemExit("CTest workload bridge requires exactly one non-CTest declaration")
    if declarations[0].get("blocked"):
        raise SystemExit(f"workload {args.workload} is blocked: {declarations[0].get('note', 'unavailable')}")
    if declarations[0].get("env") == "macos" and sys.platform != "darwin":
        print(f"SKIP: {args.workload} requires a native macOS execution host")
        return 77
    from west.manifest import Manifest
    from test import DarlingTest

    manifest = Manifest.from_topdir()
    runner = DarlingTest.__new__(DarlingTest)
    runner.manifest = manifest
    runner.topdir = str(manifest.topdir)
    runner.inf = print
    runner.err = runner.wrn = lambda message: print(message, file=sys.stderr)
    runner.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    runner._prefix = os.environ.get("DPREFIX") or os.environ.get("DARLING_PREFIX")
    runner._runtime_root = os.environ.get("DARLING_RUNTIME_ROOT")
    runner._runtime = None
    source_root = os.environ.get("DARLING_SOURCE_ROOT")
    runner._source_root = Path(source_root).expanduser().resolve() if source_root else None
    runner._executor = runner._resolve_executor(args.executor)
    state = Path(manifest.repo_abspath).parent / ".west-test"
    runner._bundle_root = args.bundle_root or str(state / "bundles")
    runner._runtime_evidence_root = state / "runtime-evidence"
    runner._keep_prefix_running = os.environ.get("WEST_TEST_KEEP_PREFIX_RUNNING") == "1"
    runner._prefix_cleanup_failed = False
    selection = select_workload_tests(catalog, workload=args.workload,
                                      resolved_diag=runner._resolved_diag)
    if selection.blocked:
        runner.die(f"workload {args.workload} is blocked: {selection.blocked[0][1].get('note', 'unavailable')}")
    if len(selection.selected) != 1 or is_ctest_binding(selection.selected[0][1]):
        runner.die("CTest workload bridge requires exactly one non-CTest declaration")
    try:
        borrowed = validate_borrowed_prefix_lease(runner._prefix) if os.environ.get("WEST_TEST_PREFIX_LEASE") else None
    except (OSError, ValueError) as error:
        runner.die(f"invalid inherited prefix lease: {error}")
    runner._prefix_lease_descriptor = borrowed
    declaration = selection.selected[0][1]
    if declaration.get("requires-build-features") or declaration.get("requires-installed-paths"):
        problems = installed_requirement_problems(declaration, runner._installed_runtime())
        if problems:
            for problem in problems:
                print(f"SKIP: {problem}")
            return 77
    if declaration.get("runner") == "diagnostic":
        if declaration.get("diagnostic") != "exact-capture":
            runner.die(f"unsupported workload diagnostic: {declaration.get('diagnostic')}")
        if not runner._prefix:
            runner.die("exact-capture requires DPREFIX and an installed Darling runtime")
        from test_diagnostics import run_exact_capture
        # This diagnostic owns its lease, or borrows the outer CTest lease.
        return run_exact_capture(runner)
    guest = runner._metadata_needs_prefix(selection.selected)
    if guest:
        if not runner._prefix:
            runner.die("guest workload requires DPREFIX and an installed Darling runtime")
        runner._installed_runtime()
    # Borrowing the suite lease preserves exclusion, not a previous case's server.
    # Only the suite's explicit warm-prefix option skips ordinary reset/shutdown.
    with runner._prefix_resource_context(guest):
        rc = runner._run_metadata_tests(selection.selected, False, [])
    return 1 if getattr(runner, "_prefix_cleanup_failed", False) else rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--generate", type=Path)
    action.add_argument("--workload")
    parser.add_argument("--executor")
    parser.add_argument("--bundle-root")
    args = parser.parse_args()
    if args.generate:
        args.generate.write_text("\n".join(registration_lines(args.catalog, args.executor or "", args.bundle_root or "")) + "\n")
        return 0
    return run_workload(args)


if __name__ == "__main__":
    raise SystemExit(main())

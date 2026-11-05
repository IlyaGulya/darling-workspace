"""Darling workspace test orchestrator.

`west test` is a thin layer over CTest, in the same spirit as gVisor's Bazel
test targets and Wine's winetest: the runner sits ON TOP of the build system,
it does not reinvent discovery/parallelism/JUnit/WILL_FAIL. CTest owns those.

This command adds the three things CTest does not give for free in this repo:

  --changed   map changed submodules (from the west manifest + git diff) to the
              `submod:<name>` CTest labels, so a quick local cycle runs only the
              tests a PR could affect.
  --submodule PATH
              map an explicit West project path/name to `submod:<name>`.
  --bead ID   run the regression(s) attached to an issue (label `bead:<id>`),
              turning the beads graph into a live regression set.
  --executor  the darling-debug-runner binary used by the guarded/forensic
              diagnosis tiers, so a hang becomes a captured, timed-out failure
              instead of a stall (the tier is set per-test in add_compat_test).

Named workloads are source-independent declarations. CTest remains the suite
selection authority; West adds structured resources and owned diagnostics.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from shlex import quote, join as shell_join

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefix_repair import (
    cleanup_prefix_mounts,
    darling_init_pid_is_usable,
)
from test_ctest import (
    CtestSelectionMixin,
    ctest_command,
    ctest_selector_label_args,
    ctest_runtime_group_passthrough,
    is_ctest_binding,
    ctest_reference_args,
    ctest_index_args,
    installed_requirement_problems,
)
from test_selection import (
    metadata_invocation_identity,
    metadata_run_summary,
    metadata_selection_plan,
    metadata_test_outcome,
)
from test_dispatch import dispatch_fixture_runner
from test_cmake import CmakeFixtureMixin
from test_descriptor_transport import (
    DescriptorTraceError,
    window_specs as descriptor_trace_window_specs,
)
from test_execution import process_output_text, run_bounded
from fresh_prefix import create_fresh_prefix, remove_fresh_prefix
from runtime_retention import leased_runtime, runtime_use
from lane import (
    SCOPE_ACTIVE_ENV,
    LaneError,
    claim_scope,
    env_argv,
    lane_env,
    lane_path,
    lane_root,
    read_record,
    record_problems,
    require_scope,
    scope_description,
    scope_unit,
    scope_wrap,
    validate_lane_name,
    west_command_argv,
)
from test_guest_execution import (
    failure_phase_from_output,
    guest_c_fixture_prerequisite_problems,
    # Kept in this namespace on purpose: focused contracts patch and read the
    # facade's ``test.run_guest_argv`` rather than the owning module.
    run_guest_argv,
    run_guest_argv_fixture,
    run_guest_command_fixture,
)
try:
    from .test_guest_macho import run_guest_macho_fixture
except ImportError:  # Loaded as a West extension module, not a package.
    from test_guest_macho import run_guest_macho_fixture
try:
    from .test_guest_c import failure_phase_from_debug_bundle, run_guest_c_fixture
except ImportError:  # Loaded as a West extension module, not a package.
    from test_guest_c import failure_phase_from_debug_bundle, run_guest_c_fixture
from runtime_install import resolve_runtime
from test_manifest import load_workloads
from test_selection import select_workload_tests
from test_prefix import (
    cleanup_rootless_runtime_sockets,
    PrefixLifecycleMixin,
    PrefixLifecycleOwner,
    remove_stale_init_pid,
    remove_stale_server_socket,
    RootlessRuntimeSocketCleanupResult,
)
from test_resources import resource_context
from test_results import InvocationResult
from test_runtime_evidence import RuntimeEvidenceStore
import test_runtime_cache
import test_stock_stack_cache
import test_verdict_cache
from guest_macho_validation import (
    add_cli_arguments,
    capture_invocation,
    finalize_guest_macho_evidence,
    validate_cli_selection,
    validate_selected_group,
)
from test_worktrees import prunable_west_temp_worktrees, prune_stale_west_temp_worktrees
from test_store import (
    STATE_ROOT_ENV,
    durable_subdir,
    inside_state_root,
    scratch_owner,
    state_root,
    state_subdir,
)


# A debug bundle is a timestamp-named directory, <YYYYMMDDTHHMMSSZ>-<name>.
# The GC bundle pass selects by this shape and by nothing else: selecting by
# age and count over every directory under the root made unrelated state
# eligible, including a west dev job directory and another workstream's
# experiment root.
BUNDLE_NAME = re.compile(r"^[0-9]{8}T[0-9]{6}Z-")


class DarlingTest(
    PrefixLifecycleMixin,
    CtestSelectionMixin,
    CmakeFixtureMixin,
    WestCommand,
):
    def __init__(self):
        super().__init__(
            "test",
            "Run Darling regression/compat tests (changed-only, by bead, or full)",
            "Discover and run compat tests via ctest with changed/bead targeting",
            accepts_unknown_args=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument(
                    "--changed",
                    action="store_true",
                    help="run only tests labelled for submodules changed vs upstream",
                )
        parser.add_argument(
                    "--bead",
                    metavar="ID",
                    help="run tests attached to a bead (label bead:<ID>)",
                )
        parser.add_argument(
                    "--submodule",
                    action="append",
                    default=[],
                    metavar="PATH",
                    help="run CTest-backed tests labelled for a West project path/name",
                )
        parser.add_argument(
                    "--env",
                    choices=("host", "darling", "macos"),
                    help="restrict to one environment",
                )
        parser.add_argument(
                    "--prefix",
                    metavar="PATH",
                    help="writable guest-state directory, separate from --runtime-root",
                )
        parser.add_argument(
                    "--fresh-prefix-from",
                    metavar="BASELINE",
                    help="copy BASELINE into an isolated disposable prefix for this test run",
                )
        parser.add_argument(
                    "--lane",
                    metavar="NAME",
                    help=(
                        "run inside isolation lane NAME: its own copy-on-write prefix, "
                        "state root, temporary directories and cgroup ownership"
                    ),
                )
        parser.add_argument(
                    "--keep-prefix-running",
                    action="store_true",
                    help="do not shut down a Darling prefix after prefix-backed metadata tests",
                )
        parser.add_argument(
                    "--executor",
                    metavar="PATH",
                    help="darling-debug-runner binary for guarded/forensic tiers",
                )
        parser.add_argument(
                    "--diag",
                    choices=("bare", "guarded", "forensic"),
                    help="restrict to one diagnosis tier; matches the RESOLVED tier, so "
                    "guarded/forensic that fell back to bare (no executor) count as bare",
                )
        parser.add_argument(
                    "--label",
                    metavar="REGEX",
                    help="restrict metadata/CTest labels (e.g. 'name:case' or 'macos:15')",
                )
        add_cli_arguments(parser)
        parser.add_argument(
                    "--fuzz",
                    action="store_true",
                    help="restrict CTest suite selection to tests labelled fuzz:*",
                )
        parser.add_argument(
                    "--stress",
                    action="store_true",
                    help="restrict CTest suite selection to tests labelled stress:*",
                )
        parser.add_argument(
                    "--list",
                    action="store_true",
                    help="list selected tests and exit (no run)",
                )
        parser.add_argument(
                    "--ctest-timeout-seconds",
                    type=int,
                    default=3600,
                    metavar="SECONDS",
                    help="outer deadline for one selected CTest invocation (default 3600)",
                )
        parser.add_argument(
                    "--gc",
                    action="store_true",
                    help="prune owned debug bundles and proof scratch, plus explicitly opted-in caches, and exit",
                )
        parser.add_argument(
                    "--cleanup-prefix",
                    action="store_true",
                    help="shutdown and verify one prefix, then exit without running tests",
                )
        parser.add_argument(
                    "--keep-last",
                    type=int,
                    default=20,
                    metavar="N",
                    help="bundles to keep when pruning (default 20)",
                )
        parser.add_argument(
                    "--max-bundle-mb",
                    type=int,
                    default=64,
                    metavar="MB",
                    help="drop bundles larger than this when pruning (default 64)",
                )
        parser.add_argument(
                    "--bundle-root",
                    metavar="DIR",
                    help="debug bundle directory (default DW_STATE_ROOT/bundles, "
                    "otherwise ~/work/darling-debug)",
                )
        parser.add_argument(
                    "--dry-run",
                    action="store_true",
                    help="with --gc, show what would be pruned without deleting",
                )
        parser.add_argument(
                    "--proof-scratch-root",
                    metavar="DIR",
                    help="with --gc, directory to scan for stale owned runtime/proof scratch "
                    "and guest runner output (default DW_STATE_ROOT/scratch, "
                    f"otherwise {tempfile.gettempdir()}). Runtime evidence units are scoped "
                    "separately by --runtime-evidence-root",
                )
        parser.add_argument(
                    "--runtime-evidence-root",
                    metavar="DIR",
                    help="durable root for retained runtime evidence units "
                    "(default DW_STATE_ROOT/evidence, otherwise .west-test/runtime-evidence); "
                    "with --gc --gc-runtime-evidence, the root that pass collects",
                )
        parser.add_argument(
                    "--runtime-evidence",
                    choices=("list", "show", "replay"),
                    help="list, inspect, or validate a retained runtime evidence unit",
                )
        parser.add_argument(
                    "--runtime-evidence-id",
                    metavar="ID",
                    help="unit name or unique trailing ID for --runtime-evidence show/replay",
                )
        parser.add_argument(
                    "--gc-runtime-evidence",
                    action="store_true",
                    help="with --gc, explicitly prune durable runtime evidence units using the proof scratch age/count limits",
                )
        parser.add_argument(
                    "--gc-runtime-build-cache",
                    action="store_true",
                    help="with --gc, discard all idle owned runtime build-cache entries, "
                    "including source worktrees and legacy builds (independent of scratch retention)",
                )
        parser.add_argument(
                    "--runtime-build-cache-root",
                    metavar="DIR",
                    help="with --gc --gc-runtime-build-cache, cache to maintain "
                    "(default WEST_RUNTIME_BUILD_CACHE_DIR, otherwise .west-test/runtime-build-cache)",
                )
        parser.add_argument(
                    "--proof-scratch-max-age-hours",
                    type=float,
                    default=24.0,
                    metavar="HOURS",
                    help="with --gc, prune stale owned test/proof scratch directories older than this "
                    "(default 24)",
                )
        parser.add_argument(
                    "--proof-scratch-keep-last",
                    type=int,
                    default=2,
                    metavar="N",
                    help="with --gc, keep at most N newest owned test/proof scratch directories "
                    "regardless of age (default 2)",
                )
        parser.add_argument('--runtime-root', metavar='ROOT', help='normal Darling product installation root, separate from guest state')
        parser.add_argument('--source-root', metavar='SOURCE', help='current Darling checkout used for source-owned test inputs (not runtime selection)')
        parser.add_argument('--workload', metavar='NAME', help='run a named source-independent workload')
        return parser

    # --- helpers ------------------------------------------------------------

    def _testkit_dir(self) -> Path:
        return Path(self.manifest.repo_abspath) / "testkit"
    
    def _installed_runtime(self):
        runtime = getattr(self, '_runtime', None)
        if runtime is None:
            try:
                root = getattr(self, '_runtime_root', None)
                guards = getattr(self, '_runtime_guards', None)
                runtime = guards.enter_context(leased_runtime(root)) if guards is not None else resolve_runtime(root)
            except (OSError, ValueError, RuntimeError) as error:
                self.die(str(error))
            self._runtime = runtime
        return runtime
    
    
    def _display_invocation(self, invocation):
        if invocation.get('diag','bare') == 'bare' or any(invocation.get(field) for field in ('guest_c_fixture','guest_command_fixture','guest_argv_fixture','guest_macho_fixture','darling_cmake_target_fixture')):
            return invocation['display']
        return shell_join(self._debug_runner_args(invocation, display_only=True))
    
    
    def _runtime_diagnostic_output(self, invocation):
        return ''.join(Path(path).read_text(errors='replace') for path in dict.fromkeys(invocation.get('_host_trace_paths', ())) if Path(path).is_file())
    

    def _runtime_evidence_store(self) -> RuntimeEvidenceStore:
        configured = getattr(self, "_runtime_evidence_root", None)
        manifest = getattr(self, "manifest", None)
        default_workspace = getattr(self, "topdir", Path.cwd())
        workspace = Path(
            getattr(manifest, "repo_abspath", default_workspace)
        )
        root = (
            Path(configured)
            if configured
            else workspace / ".west-test/runtime-evidence"
        )
        if not root.is_absolute():
            root = workspace / root
        return RuntimeEvidenceStore(root)

    


    def _resolve_executor(self, explicit: str | None) -> str | None:
        if explicit:
            return str(Path(explicit).expanduser())
        path = shutil.which("darling-debug-runner")
        if path:
            return path
        project = self._projects().get("darling-debug-runner")
        if project is None:
            return None
        repo = project
        candidates = [
            repo / "target" / "release" / "darling-debug-runner",
            repo / "target" / "debug" / "darling-debug-runner",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    @staticmethod
    def _resolved_diag(test) -> str:
        diag = test.get("diag")
        if diag:
            return diag
        return "guarded" if test.get("env") == "darling" else "bare"

    def _projects(self) -> dict[str, Path]:
        projects: dict[str, Path] = {}
        for project in self.manifest.projects:
            projects[project.name] = Path(project.abspath)
            projects[project.path] = Path(project.abspath)
        return projects

    

    def _project_path(self, ref: str) -> Path:
        source_root = getattr(self, "_source_root", None)
        module = ref
        for project in self.manifest.projects:
            if ref in {project.name, project.path}:
                module = project.path
                break
        if source_root and (module == "darling" or module.startswith("darling/")):
            relative = Path(module).relative_to("darling")
            return Path(source_root) / relative
        projects = self._projects()
        if ref in projects:
            return projects[ref]
        path = Path(self.topdir) / ref
        if path.exists():
            return path
        self.die(f"unknown West project or path: {ref}")

    def _test_invocation(self, patch, test):
            """Resolve structured workload metadata to a concrete local invocation.
    
            `command` is intentionally still supported as an escape hatch, but the
            common cases should be structured so west owns how tests are launched.
            """
            source_env = test.get("source-env")
            source_module = test.get("source-module", patch["module"])
            source_names = list(dict.fromkeys([*test.get("source-envs", []), *([source_env] if source_env else [])]))
            if source_names:
                source_root = str(self._project_path(source_module))
                test = {**test, "env-vars": {**test.get("env-vars", {}), **{name: source_root for name in source_names}}}
            if test.get("command"):
                return {
                    "key": f"shell:{test['command']}",
                    "display": test["command"],
                    "cwd": Path(self.topdir),
                    "args": test["command"],
                    "shell": True,
                    "env": {**os.environ, **{str(k):str(v) for k,v in test.get("env-vars",{}).items()}},
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                    "source_env": source_env,
                    "source_module": source_module,
                }
            if is_ctest_binding(test):
                selection = test.get("_ctest", {})
                build = Path(selection.get("build", self._testkit_dir() / "build"))
                ctest_args = (
                    ctest_command(build, passthrough=ctest_index_args([selection["index"]]))
                    if selection else ctest_command(build, label_args=ctest_reference_args(test))
                )
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                return {
                    "key": f"ctest:{patch['path']}:{test.get('name')}:{build}:{selection.get('index')}",
                    "display": shell_join(ctest_args),
                    "cwd": Path(self.topdir),
                    "args": ctest_args,
                    "shell": False,
                    "env": env,
                    "ctest_label": test.get("ctest-label"),
                    "ctest_name": selection.get("name", test.get("ctest-name")),
                    "ctest_build": selection.get("build"),
                    # Preserved when a source-override arm repoints `ctest_build` at
                    # a bad/fixed source tree's build directory: the registration
                    # identity compares each WORKING_DIRECTORY relative to the build
                    # tree that produced it.
                    "ctest_selection_build": selection.get("build"),
                    "ctest_index": selection.get("index"),
                    "ctest_directory": selection.get("directory"),
                    "ctest_env": test.get("env"),
                    "ctest_source_override": test.get("ctest-source-override"),
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                    "source_env": source_env,
                    "source_module": source_module,
                }
    
            runner = test.get("runner", "script" if test.get("script") else None)
            if runner == "west-build":
                target = test["target"]
                args = [
                    "west",
                    "darling-build",
                    "--force",
                    "--skip-doctor",
                    "--targets",
                    target,
                ]
                return {
                    "key": " ".join(args),
                    "display": " ".join(args),
                    "cwd": Path(self.topdir),
                    "args": args,
                    "shell": False,
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                    "source_env": source_env,
                    "source_module": source_module,
                }
            if runner in {
                "script",
                "source-contract-script",
                "self-contract-script",
                "guest-runtime-script",
            }:
                repo = test.get("repo", patch["module"])
                script = test["script"]
                script_args = [str(arg) for arg in test.get("args", [])]
                args = [str(Path(script)), *script_args]
                prefix = ""
                if test.get("env-vars"):
                    prefix = " ".join(
                        f"{quote(str(key))}={quote(str(value))}"
                        for key, value in test["env-vars"].items()
                    ) + " "
                display_args = " ".join(quote(arg) for arg in args)
                if runner in {
                    "source-contract-script",
                    "self-contract-script",
                    "guest-runtime-script",
                }:
                    display = f"cd {quote(repo)} && <{runner}> {prefix}{display_args}"
                else:
                    display = f"cd {quote(repo)} && {prefix}{display_args}"
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                cwd = self._project_path(repo)
                script_path = cwd / script
                return {
                    "key": display,
                    "display": display,
                    "cwd": cwd,
                    "script_path": script_path,
                    "repo": repo,
                    "script": script,
                    "args": args,
                    "shell": False,
                    "runner": runner,
                    "prefix_controller": test.get("prefix-controller", False),
                    "env": env,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                    "source_env": source_env,
                    "source_module": source_module,
                    "host_trace_files": list(test.get("host-trace-files", [])),
                    "host_temp_files": list(test.get("host-temp-files", [])),
                    "host_trace_oracle": bool(test.get("host-trace-oracle", False)),
                }
            if runner == "python":
                repo = test.get("repo", patch["module"])
                script = test["script"]
                script_args = [str(arg) for arg in test.get("args", [])]
                args = ["python3", str(Path(script)), *script_args]
                prefix = ""
                if test.get("env-vars"):
                    prefix = " ".join(
                        f"{quote(str(key))}={quote(str(value))}"
                        for key, value in test["env-vars"].items()
                    ) + " "
                display = f"cd {quote(repo)} && {prefix}{' '.join(quote(arg) for arg in args)}"
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                cwd = self._project_path(repo)
                script_path = cwd / script
                return {
                    "key": display,
                    "display": display,
                    "cwd": cwd,
                    "script_path": script_path,
                    "repo": repo,
                    "script": script,
                    "args": args,
                    "shell": False,
                    "prefix_controller": test.get("prefix-controller", False),
                    "env": env,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                    "source_env": source_env,
                    "source_module": source_module,
                }
            if runner == "c-fixture":
                repo = test.get("repo", patch["module"])
                script = test["script"]
                cwd = self._project_path(repo)
                script_path = cwd / script
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                cc = str(test.get("cc", os.environ.get("CC", "cc")))
                output = f"<temp>/{Path(script).stem}"
                display_parts = [quote(cc), *[quote(str(flag)) for flag in test.get("compile-flags", [])]]
                for include_dir in test.get("fixture-include-dirs", []):
                    display_parts.extend(["-I", quote(str(include_dir))])
                for include_dir in test.get("include-dirs", []):
                    display_parts.extend(["-I", quote(str(include_dir))])
                if test.get("stub-headers") or test.get("generated-headers"):
                    display_parts.extend(["-I", "<generated-stubs>"])
                for source_file in test.get("source-files", []):
                    display_parts.append(quote(str(source_file)))
                display_parts.extend([quote(script), "-o", quote(output)])
                display = f"cd {quote(repo)} && {' '.join(display_parts)} && {quote(output)}"
                return {
                    "key": (
                        f"c-fixture:{repo}:{script}:"
                        f"{repr(test.get('compile-flags', []))}:"
                        f"{repr(test.get('source-files', []))}:"
                        f"{repr(test.get('include-dirs', []))}:"
                        f"{repr(test.get('fixture-include-dirs', []))}:"
                        f"{repr(test.get('stub-headers', []))}:"
                        f"{repr(sorted((test.get('generated-headers') or {}).keys()))}:"
                        f"{repr(test.get('source-root-module', ''))}"
                    ),
                    "display": display,
                    "cwd": cwd,
                    "script_path": script_path,
                    "repo": repo,
                    "script": script,
                    "args": None,
                    "shell": False,
                    "env": env,
                    "c_fixture": True,
                    "cc": cc,
                    "include_dirs": [str(item) for item in test.get("include-dirs", [])],
                    "fixture_include_dirs": [
                        str(item) for item in test.get("fixture-include-dirs", [])
                    ],
                    "stub_headers": [str(item) for item in test.get("stub-headers", [])],
                    "generated_headers": {
                        str(path): str(content)
                        for path, content in (test.get("generated-headers") or {}).items()
                    },
                    "source_files": [str(item) for item in test.get("source-files", [])],
                    "compile_flags": [str(item) for item in test.get("compile-flags", [])],
                    "source_root_env": source_env,
                    "source_root_module": str(test.get("source-root-module", "")),
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                    "host_trace_files": list(test.get("host-trace-files", [])),
                }
            if runner == "object-symbol-fixture":
                repo = test.get("repo", patch["module"])
                cwd = self._project_path(repo)
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                cc = str(test.get("cc", os.environ.get("CC", "cc")))
                source_file = str(test["source-file"])
                display_parts = [
                    quote(cc),
                    "-c",
                    *[quote(str(flag)) for flag in test.get("compile-flags", [])],
                ]
                for include_dir in test.get("fixture-include-dirs", []):
                    display_parts.extend(["-I", quote(str(include_dir))])
                for include_dir in test.get("include-dirs", []):
                    display_parts.extend(["-I", quote(str(include_dir))])
                display_parts.extend([quote(source_file), "-o", "<temp>/<variant>.o", "&&", "nm", "-u", "<temp>/<variant>.o"])
                if any(
                    check.get("present-defined-symbols") or check.get("absent-defined-symbols")
                    for check in test.get("symbol-checks", [])
                ):
                    display_parts.extend(["&&", "nm", "-g", "<temp>/<variant>.o"])
                display = f"cd {quote(repo)} && {' '.join(display_parts)}"
                return {
                    "key": (
                        f"object-symbol-fixture:{repo}:{source_file}:"
                        f"{repr(test.get('compile-flags', []))}:"
                        f"{repr(test.get('include-dirs', []))}:"
                        f"{repr(test.get('fixture-include-dirs', []))}:"
                        f"{repr(test.get('symbol-checks', []))}"
                    ),
                    "display": display,
                    "cwd": cwd,
                    "args": None,
                    "shell": False,
                    "env": env,
                    "object_symbol_fixture": True,
                    "cc": cc,
                    "source_file": source_file,
                    "include_dirs": [str(item) for item in test.get("include-dirs", [])],
                    "fixture_include_dirs": [
                        str(item) for item in test.get("fixture-include-dirs", [])
                    ],
                    "compile_flags": [str(item) for item in test.get("compile-flags", [])],
                    "symbol_checks": [
                        {
                            "name": str(check.get("name", f"check-{index}")),
                            "compile_flags": [str(item) for item in check.get("compile-flags", [])],
                            "present_undefined_symbols": [
                                str(item) for item in check.get("present-undefined-symbols", [])
                            ],
                            "absent_undefined_symbols": [
                                str(item) for item in check.get("absent-undefined-symbols", [])
                            ],
                            "present_defined_symbols": [
                                str(item) for item in check.get("present-defined-symbols", [])
                            ],
                            "absent_defined_symbols": [
                                str(item) for item in check.get("absent-defined-symbols", [])
                            ],
                        }
                        for index, check in enumerate(test.get("symbol-checks", []))
                    ],
                    "source_root_env": source_env,
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
            if runner == "source-build-fixture":
                repo = test.get("repo", patch["module"])
                script = test["script"]
                cwd = self._project_path(repo)
                script_path = cwd / script
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                build_commands = [str(item) for item in test.get("build-commands", [])]
                run_commands = [str(item) for item in test.get("run-commands", [])]
                display_steps = [
                    "<archive-source>",
                    *build_commands,
                    *run_commands,
                ]
                display = f"cd {quote(repo)} && " + " && ".join(display_steps)
                return {
                    "key": f"source-build-fixture:{repo}:{script}",
                    "display": display,
                    "cwd": cwd,
                    "script_path": script_path,
                    "repo": repo,
                    "script": script,
                    "args": None,
                    "shell": False,
                    "env": env,
                    "source_build_fixture": True,
                    "build_commands": build_commands,
                    "run_commands": run_commands,
                    "source_root_env": source_env,
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
            if runner == "source-script-fixture":
                repo = test.get("repo", patch["module"])
                cwd = self._project_path(repo)
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                source_script = str(test["source-script"])
                cases = [
                    {
                        "name": str(case.get("name", f"case-{index}")),
                        "args": [str(arg) for arg in case.get("args", [])],
                        "stdout": None if case.get("stdout") is None else str(case.get("stdout")),
                        "returncode": int(case.get("returncode", 0)),
                    }
                    for index, case in enumerate(test.get("cases", []))
                ]
                display = (
                    f"cd {quote(repo)} && "
                    f"<source-script-fixture> {quote(source_script)} "
                    f"({len(cases)} case(s))"
                )
                return {
                    "key": f"source-script-fixture:{repo}:{source_script}:{repr(cases)}",
                    "display": display,
                    "cwd": cwd,
                    "args": None,
                    "shell": False,
                    "env": env,
                    "source_script_fixture": True,
                    "prefix_controller": test.get("prefix-controller", False),
                    "source_script": source_script,
                    "cases": cases,
                    "source_root_env": source_env,
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
            if runner == "cmake-configure-fixture":
                repo = test.get("repo", patch["module"])
                cwd = self._project_path(repo)
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                configure_args = [str(arg) for arg in test.get("configure-args", [])]
                fake_tools = {
                    str(name): {
                        "stdout": str(spec.get("stdout", "")),
                        "stderr": str(spec.get("stderr", "")),
                        "returncode": int(spec.get("returncode", 0)),
                        "log_args": bool(spec.get("log-args", False)),
                    }
                    for name, spec in (test.get("fake-tools") or {}).items()
                }
                display = (
                    f"cd {quote(repo)} && <cmake-configure-fixture> "
                    f"cmake -S <source> -B <temp>/build "
                    f"{shell_join(configure_args)}"
                )
                return {
                    "key": (
                        f"cmake-configure-fixture:{repo}:"
                        f"{repr(configure_args)}:{repr(fake_tools)}"
                    ),
                    "display": display,
                    "cwd": cwd,
                    "args": None,
                    "shell": False,
                    "env": env,
                    "cmake_configure_fixture": True,
                    "configure_args": configure_args,
                    "fake_tools": fake_tools,
                    "marker_files": [
                        {
                            "path": str(marker["path"]),
                            "content": str(marker.get("content", "")),
                        }
                        for marker in test.get("marker-files", [])
                    ],
                    "expect": test.get("expect", {}),
                    "source_root_env": source_env,
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
            if runner == "darling-cmake-target-fixture":
                repo = test.get("repo", patch["module"])
                cwd = self._project_path(repo)
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                target = str(test["target"])
                source_dir = str(test.get("source-dir", "source"))
                cmake_args = [str(arg) for arg in test.get("cmake-args", [])]
                build_args = [str(arg) for arg in test.get("build-args", [])]
                ctest_label = test.get("ctest-label")
                run_binary = str(test.get("run-binary", f"{source_dir}/{target}"))
                diag = self._resolved_diag(test)
                if ctest_label:
                    ctest_step = (
                        f"ctest --test-dir <temp>/build --output-on-failure -L "
                        f"{quote(str(ctest_label))}"
                    )
                    final_step = (
                        f"<darling-debug-runner> run -- {ctest_step}"
                        if diag != "bare"
                        else ctest_step
                    )
                else:
                    final_step = f"<temp>/build/{quote(run_binary)}"
                display = (
                    f"cd {quote(repo)} && <darling-cmake-target-fixture> "
                    f"cmake -S <superproject> -B <temp>/build "
                    f"{shell_join(cmake_args)} && "
                    f"cmake --build <temp>/build --target {quote(target)} "
                    f"{shell_join(build_args)} && "
                    f"{final_step}"
                )
                return {
                    "key": (
                        f"darling-cmake-target-fixture:{repo}:{target}:"
                        f"{source_dir}:{run_binary}:{ctest_label}:"
                        f"{repr(test.get('fixture-files', []))}:"
                        f"{repr(cmake_args)}:{repr(build_args)}:"
                        f"{repr(test.get('required-compile-options', []))}"
                    ),
                    "display": display,
                    "cwd": cwd,
                    "args": None,
                    "shell": False,
                    "env": env,
                    "darling_cmake_target_fixture": True,
                    "target": target,
                    "source_dir": source_dir,
                    "run_binary": run_binary,
                    "ctest_label": str(ctest_label) if ctest_label else None,
                    "fixture_files": [str(item) for item in test.get("fixture-files", [])],
                    "cmake_args": cmake_args,
                    "build_args": build_args,
                    "fallback_executable_sources": [
                        str(item) for item in test.get("fallback-executable-sources", [])
                    ],
                    "fallback_include_dirs": [
                        str(item) for item in test.get("fallback-include-dirs", [])
                    ],
                    "fallback_link_libraries": [
                        str(item) for item in test.get("fallback-link-libraries", ["crypto44"])
                    ],
                    "required_compile_options": [
                        {
                            "source": str(check["source"]),
                            "options": [str(item) for item in check.get("options", [])],
                        }
                        for check in test.get("required-compile-options", [])
                    ],
                    "source_root_env": source_env,
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": list(test.get("requires", [])),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": diag,
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
            if runner == "guest-c-fixture":
                repo = test.get("repo", patch["module"])
                script = test["script"]
                cwd = self._project_path(repo)
                script_path = cwd / script
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                resources = set(test.get("requires", []))
                resources.add("darling-prefix")
                name = test.get("name", Path(script).stem)
                guest_cc = str(
                    test.get(
                        "guest-cc",
                        os.environ.get(
                            "DARLING_GUEST_CC",
                            "/Library/Developer/CommandLineTools/usr/bin/clang",
                        ),
                    )
                )
                guest_cflags = str(
                    test.get(
                        "guest-cflags",
                        os.environ.get(
                            "DARLING_GUEST_CFLAGS",
                            "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
                        ),
                    )
                )
                compile_flags = [str(item) for item in test.get("compile-flags", [])]
                link_flags = [str(item) for item in test.get("link-flags", [])]
                run_args = [str(item) for item in test.get("run-args", [])]
                guest_env_vars = {
                    str(k): str(v) for k, v in test.get("guest-env-vars", {}).items()
                }
                host_trace_oracle = bool(test.get("host-trace-oracle", False))
                host_stat_deltas = list(test.get("host-stat-deltas", []))
                ok_marker = test.get("ok-marker")
                if not ok_marker and not host_trace_oracle:
                    self.die(f"{patch['path']}: guest-c-fixture needs ok-marker")
                dserver_path = self._project_path("darling/src/external/darlingserver")
                host_stat_tool = "darling-stat"
                if dserver_path is not None:
                    host_stat_tool = str(dserver_path / "tools/darling-stat")
                display = (
                    f"cd {quote(repo)} && <upload> {quote(script)} && "
                    f"darling shell {quote(guest_cc)} {guest_cflags} "
                    f"{shell_join(compile_flags)} -o /tmp/{quote(name)} /tmp/{quote(name)}.c "
                    f"{shell_join(link_flags)} && darling shell /tmp/{quote(name)} "
                    f"{shell_join(run_args)}"
                )
                return {
                    "key": (
                        f"guest-c-fixture:{repo}:{script}:{repr(host_stat_deltas)}:"
                        f"{repr(test.get('descriptor-trace'))}"
                    ),
                    "display": display,
                    "cwd": cwd,
                    "script_path": script_path,
                    "repo": repo,
                    "script": script,
                    "args": None,
                    "shell": False,
                    "runner": "guest-c-fixture",
                    "env": env,
                    "guest_c_fixture": True,
                    "guest_cc": guest_cc,
                    "guest_cflags": guest_cflags,
                    "guest_prelude": str(test.get("guest-prelude", "")),
                    "guest_env_vars": guest_env_vars,
                    "compile_flags": compile_flags,
                    "link_flags": link_flags,
                    "run_args": run_args,
                    "ok_marker": str(ok_marker or ""),
                    "host_trace_files": list(test.get("host-trace-files", [])),
                    "host_temp_files": list(test.get("host-temp-files", [])),
                    "host_stat_deltas": host_stat_deltas,
                    "host_stat_tool": host_stat_tool,
                    "descriptor_trace": test.get("descriptor-trace") or {},
                    "eunion_template_files": list(test.get("eunion-template-files", [])),
                    "eunion_template_symlinks": list(test.get("eunion-template-symlinks", [])),
                    "eunion_upper_files": list(test.get("eunion-upper-files", [])),
                    "eunion_cleanup_dirs": list(test.get("eunion-cleanup-dirs", [])),
                    "eunion_forbid_template_paths": list(
                        test.get("eunion-forbid-template-paths", [])
                    ),
                    "eunion_require_upper_paths": list(
                        test.get("eunion-require-upper-paths", [])
                    ),
                    "eunion_verify_template_files_after": bool(
                        test.get("eunion-verify-template-files-after", False)
                    ),
                    "host_trace_oracle": host_trace_oracle,
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": sorted(resources),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": name,
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
            if runner == "guest-command-fixture":
                repo = test.get("repo", patch["module"])
                cwd = self._project_path(repo)
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                resources = set(test.get("requires", []))
                resources.add("darling-prefix")
                guest_command = str(test["guest-command"])
                guest_env_vars = {
                    str(k): str(v) for k, v in test.get("guest-env-vars", {}).items()
                }
                expect = test.get("expect", {})
                display = f"cd {quote(repo)} && darling shell /bin/bash --login -c {quote(guest_command)}"
                return {
                    "key": (
                        f"guest-command-fixture:{repo}:{guest_command}:"
                        f"{repr(test.get('guest-env-vars', {}))}:"
                        f"{repr(test.get('dcc-cache', {}))}:"
                        f"{repr(expect)}"
                    ),
                    "display": display,
                    "cwd": cwd,
                    "args": None,
                    "shell": False,
                    "runner": "guest-command-fixture",
                    "env": env,
                    "guest_command_fixture": True,
                    "guest_command": guest_command,
                    "guest_env_vars": guest_env_vars,
                    "expect": expect,
                    "dcc_cache": test.get("dcc-cache"),
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": sorted(resources),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
            if runner == "guest-argv-fixture":
                repo = test.get("repo", patch["module"])
                cwd = self._project_path(repo)
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                resources = set(test.get("requires", []))
                resources.add("darling-prefix")
                guest_argv = tuple(str(arg) for arg in test["guest-argv"])
                expect = test.get("expect", {})
                display = f"cd {quote(repo)} && darling exec {' '.join(quote(arg) for arg in guest_argv)}"
                return {
                    "key": f"guest-argv-fixture:{repo}:{repr(guest_argv)}:{repr(expect)}",
                    "display": display,
                    "cwd": cwd,
                    "args": None,
                    "shell": False,
                    "runner": "guest-argv-fixture",
                    "env": env,
                    "guest_argv_fixture": True,
                    "guest_argv": guest_argv,
                    "expect": expect,
                    "source_env": source_env,
                    "source_module": source_module,
                    "requires_resources": sorted(resources),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                    "host_trace_files": list(test.get("host-trace-files", [])),
                }
    
            if runner == "guest-macho-fixture":
                repo = test.get("repo", patch["module"])
                repo_root = self._project_path(repo)
                cwd = repo_root
                env = None
                if test.get("env-vars"):
                    env = os.environ.copy()
                    env.update({str(k): str(v) for k, v in test["env-vars"].items()})
                resources = set(test.get("requires", []))
                resources.add("darling-prefix")
                return {
                    "key": f"guest-macho-fixture:{repo}:{test['corpus']}:{test['fixture']}",
                    "display": (
                        f"cd {quote(repo)} && <guest-macho-fixture> "
                        f"{quote(str(test['corpus']))} {quote(str(test['fixture']))}"
                    ),
                    "cwd": cwd,
                    "repo_root": repo_root,
                    "args": None,
                    "shell": False,
                    "runner": "guest-macho-fixture",
                    "env": env,
                    "guest_macho_fixture": True,
                    "corpus": str(test["corpus"]),
                    "fixture": str(test["fixture"]),
                    "validation_group": test.get("validation-group"),
                    "requires_resources": sorted(resources),
                    "requires_env": list(test.get("requires-env", [])),
                    "diag": self._resolved_diag(test),
                    "name": test.get("name", patch["path"]),
                    "timeout_seconds": int(test.get("timeout-seconds", 600)),
                }
    
            self.die(f"{patch['path']}: unsupported test runner {runner!r}")

    def _metadata_verdict_store(self) -> Path | None:
        """Return the verdict reuse store, or ``None`` when it is switched off."""

        return test_verdict_cache.cache_root(
            Path(self.manifest.repo_abspath), os.environ
        )

    @staticmethod
    def _metadata_invocation_needs_guest(test, invocation) -> bool:
            """Return whether one invocation runs inside a deployed guest runtime."""
    
            if test.get("runs") == "guest" or test.get("env") == "darling":
                return True
            return bool(
                set(invocation.get("requires_resources", ()))
                & {"darling-prefix", "darling-eunion-prefix"}
            )

    def _metadata_verdict_skip_reason(self, test, invocation) -> str | None:
        """Return why this invocation must execute, or ``None`` when it may reuse.

        Two reasons a guest invocation is executed rather than served from an
        earlier verdict:

        * Host work costs seconds. A stale verdict standing in for a cheap check
          would hide exactly the flake that check exists to expose, so it is
          executed and counted as ``verdict_host``.
        * A test that consumes the stock stack IS the from-source measurement:
          its subject is the build, and the receipt checks that prove the build
          cannot distinguish a freshly built keg from one that was restored or
          skipped. Reusing its verdict would make the acceptance claim vacuous,
          so it always runs and is counted as ``verdict_source``.
        """

        if test_verdict_cache.STACK_RESOURCE in set(
            invocation.get("requires_resources", ())
        ):
            return "verdict_source"
        if not self._metadata_invocation_needs_guest(test, invocation):
            return "verdict_host"
        return None

    def _metadata_verdict_cacheable(self, test, invocation) -> bool:
        """Return whether this invocation may be served from the verdict cache."""

        return self._metadata_verdict_skip_reason(test, invocation) is None

    def _metadata_runtime_identity(self, test, owner):
        runtime = self._installed_runtime()
        return None, runtime.identity
    

    def _metadata_verdict_identity(self, owner, test, invocation):
        if test.get('validation-group') or test.get('verify-clean-shutdown'):
            return None
        needs_guest = self._metadata_invocation_needs_guest(test, invocation)
        runtime = None
        if needs_guest:
            if not getattr(self, '_prefix', None):
                return None
            _, runtime = self._metadata_runtime_identity(test, owner)
            if runtime is None:
                return None
        stack = None
        if test_verdict_cache.STACK_RESOURCE in set(invocation.get('requires_resources', ())):
            stack = test_stock_stack_cache.stack_request(
                prefix=Path(self._prefix), environ=os.environ,
                runtime=self._installed_runtime(),
            )
            if stack is None:
                return None
        return test_verdict_cache.verdict_identity(
            test=test, invocation=invocation, runtime=runtime, stack=stack,
            toolchain=None if not self._prefix else test_stock_stack_cache.guest_toolchain_identity(Path(self._prefix), os.environ),
            environ=os.environ,
        )
    

    def _run_metadata_tests(self, tests, list_only: bool, unknown: list[str]) -> int:
            if unknown:
                self.die("metadata command tests do not accept raw ctest passthrough arguments")
            verdict_store: Path | None = None
            if not list_only:
                verdict_store = self._metadata_verdict_store()
                if verdict_store is None and test_verdict_cache.reuse_disabled(os.environ):
                    self.inf(
                        "  verdict reuse disabled by WEST_TEST_VERDICT_CACHE: every "
                        "selected test is executed"
                    )
            rc = 0
            counts = {
                "selected": len(tests), "executed": 0, "passed": 0, "failed": 0,
                "duplicate": 0, "verdict": 0,
            }
            if not list_only:
                for line in metadata_selection_plan(
                    [test.get("name", "-") for _, test in tests], self._debug_bundle_root()
                ):
                    self.inf(line)
            seen_invocations: set[tuple] = set()
            for patch, test in tests:
                name = test.get("name", "-")
                env = test.get("env", "-")
                diag = self._resolved_diag(test)
                kind = test.get("kind", "-")
                red = "red" if test.get("red") else "non-red"
                invocation = self._test_invocation(patch, test)
                self.inf(
                    f"{patch['path']}: {name} [{red}, env:{env}, diag:{diag}, kind:{kind}]"
                )
                self.inf(f"  {self._display_invocation(invocation)}")
                if test.get("requires-build-features") or test.get("requires-installed-paths"):
                    self.inf("  installed artifact requirements: " + json.dumps({
                        "build-features": test.get("requires-build-features", {}),
                        "paths": test.get("requires-installed-paths", []),
                    }, sort_keys=True))
                if test.get("_ctest"):
                    self.inf(
                        f"  registration: {test['_ctest']['name']}; "
                        
                        f"resources: {', '.join(invocation.get('requires_resources', [])) or 'none'}"
                    )
                if list_only:
                    continue
                script_path = invocation.get("script_path")
                if script_path is not None and not script_path.is_file():
                    self.die(f"{patch['path']}: test script not found: {script_path}")
                missing_env = self._missing_requirements(invocation)
                if missing_env:
                    self.die(
                        f"{patch['path']}: missing required environment for {test.get('name', '-')}: "
                        f"{', '.join(missing_env)}"
                    )
                identity = (patch["path"], metadata_invocation_identity(invocation, test))
                if identity in seen_invocations:
                    counts["duplicate"] += 1
                    self.inf(f"  skipped duplicate invocation already run")
                    continue
                seen_invocations.add(identity)
                verdict_key = None
                verdict_identity = None
                if verdict_store is not None:
                    if not self._metadata_verdict_cacheable(test, invocation):
                        # Executed rather than reused, and counted by reason: see
                        # _metadata_verdict_skip_reason for why each one must run.
                        test_verdict_cache.record_event(
                            verdict_store,
                            self._metadata_verdict_skip_reason(test, invocation)
                            or "verdict_host",
                        )
                    else:
                        verdict_identity = self._metadata_verdict_identity(
                            patch, test, invocation
                        )
                        if verdict_identity is None:
                            test_verdict_cache.record_event(verdict_store, "verdict_unkeyed")
                        else:
                            verdict_key = test_verdict_cache.identity_key(verdict_identity)
                            cached = test_verdict_cache.read_verdict(
                                verdict_store, verdict_key
                            )
                            if cached is not None:
                                self.inf(
                                    f"  verdict {test_verdict_cache.reuse_summary(cached)} "
                                    f"[{verdict_key}]; the guest program is not "
                                    "re-executed for this run"
                                )
                                test_verdict_cache.record_event(
                                    verdict_store, "verdict_hits"
                                )
                                counts["verdict"] += 1
                                continue
                            test_verdict_cache.record_event(verdict_store, "verdict_misses")
                started = time.time()
                monotonic_started = time.monotonic()
                exec_env = self._execution_env(invocation)
                with self._ctest_source_override_context(invocation) as run_invocation:
                    with self._resource_context(run_invocation, exec_env) as resource_env:
                        if getattr(self, '_guest_macho_evidence_dir', None):
                            result_rc = capture_invocation(self, run_invocation, resource_env, self._guest_macho_evidence_dir)
                        else:
                            result_rc = self._run_invocation(run_invocation, env=resource_env)
                if result_rc == 0 and test.get('verify-clean-shutdown'):
                    if not self._verify_prefix_idle():
                        result_rc = 1
                bundle = self._latest_debug_bundle(invocation, since=started)
                counts["executed"] += 1
                counts["passed" if result_rc == 0 else "failed"] += 1
                self.inf(metadata_test_outcome(invocation["name"], result_rc, bundle))
                if verdict_key is not None and result_rc == 0:
                    test_verdict_cache.record_verdict(
                        verdict_store,
                        verdict_key,
                        identity=verdict_identity,
                        duration_seconds=time.monotonic() - monotonic_started,
                        ok_marker=invocation.get("ok_marker") or None,
                        bundle=None if bundle is None else str(bundle),
                        guest_stdout_sha256=test_verdict_cache.bundle_stdout_digest(bundle),
                        provenance={"workload": patch["path"], "test": invocation["name"]},
                    )
                if result_rc:
                    rc = result_rc if rc == 0 else rc
            if verdict_store is not None:
                pruned = test_verdict_cache.prune(
                    verdict_store, test_verdict_cache.max_bytes(os.environ)
                )
                if pruned["evicted"]:
                    self.inf(
                        f"  verdict cache evicted {len(pruned['evicted'])} entry(s), "
                        f"{pruned['evicted_bytes']} bytes"
                    )
                self.inf(f"  verdict reuse {test_verdict_cache.report(verdict_store)}")
            if not list_only:
                self.inf(metadata_run_summary(counts))
            return rc

    def _metadata_needs_prefix(self, tests) -> bool:
        return any(self._metadata_invocation_needs_guest(test, self._test_invocation(owner,test)) for owner,test in tests)
    

    

    

    def _wrapped_args(self, invocation) -> list[str]:
        if invocation.get("ctest_label") or invocation.get("ctest_name"):
            return self._ctest_label_args(invocation)
        if invocation["shell"]:
            return ["/bin/bash", "-lc", invocation["args"]]
        return [str(arg) for arg in invocation["args"]]

    def _debug_runner_timeout_seconds(self, invocation) -> int:
        """Bound the executor without cutting off its post-timeout capture."""
        timeout = int(invocation.get("debug_timeout_seconds", invocation.get("timeout_seconds", 600)))
        grace = 300 if invocation.get("diag") == "forensic" else 15
        return timeout + grace

    def _debug_runner_args(self, invocation, *, env=None, display_only: bool = False) -> list[str]:
        diag = invocation.get("diag", "bare")
        if diag == "bare":
            return self._wrapped_args(invocation)
        executor = getattr(self, "_executor", None)
        if not executor:
            if display_only:
                executor = "<darling-debug-runner>"
            else:
                self.die(
                    f"{invocation['name']}: diag:{diag} requires darling-debug-runner. "
                    "Build the west project with `cargo build --release` in "
                    "`darling-debug-runner`, install it on PATH, or pass --executor."
                )
        name = f"west-test-{invocation['name']}"
        args = [
            executor,
            "run",
            "--name",
            name,
            "--bundle-root",
            str(getattr(self, "_bundle_root", "~/work/darling-debug")),
            "--timeout-seconds",
            str(invocation.get("debug_timeout_seconds", invocation.get("timeout_seconds", 600))),
        ]
        cwd = invocation.get("cwd")
        if cwd is not None:
            args.extend(["--cwd", str(cwd)])
        resources = set(invocation.get("requires_resources", []))
        if resources & {"darling-prefix", "darling-eunion-prefix"}:
            run_env = env if env is not None else self._execution_env(invocation)
            run_env = run_env or {}
            prefix = run_env.get("DPREFIX") or getattr(self, "_prefix", None)
            launcher = (
                run_env.get("DARLING_LAUNCHER")
                or run_env.get("DARLING")
                or self._resolve_darling_launcher(str(prefix) if prefix else None)
            )
            if not prefix or not launcher:
                if not display_only:
                    self.die(f"{invocation['name']}: guarded prefix cleanup requires its prefix and launcher")
                prefix = prefix or "<darling-prefix>"
                launcher = launcher or "<darling-launcher>"
            shutdown = ["env", f"DPREFIX={prefix}", f"DARLING_PREFIX={prefix}"]
            shutdown.extend([str(launcher), "shutdown"])
            close_lease = (
                'if [ -n "${WEST_TEST_PREFIX_LEASE:-}" ]; then '
                'case "${WEST_TEST_PREFIX_LEASE_FD:-8}" in ""|*[!0-9]*) exit 2;; esac; '
                '[ "${WEST_TEST_PREFIX_LEASE_FD:-8}" -ge 3 ] || exit 2; '
                'eval "exec ${WEST_TEST_PREFIX_LEASE_FD:-8}>&-"; fi; '
                'unset WEST_TEST_PREFIX_LEASE WEST_TEST_PREFIX_LEASE_FD; '
            )
            args.extend(["--terminate-command", close_lease + "exec " + shell_join(shutdown)])
            if diag == "forensic":
                args.extend(["--capture-prefix", str(prefix)])
        if diag == "forensic":
            args.extend(["--capture-exact", "--capture-tree"])
        args.append("--")
        args.extend(
            invocation["args"] if display_only and (invocation.get("ctest_label") or invocation.get("ctest_name"))
            else self._wrapped_args(invocation)
        )
        return args

    def _debug_bundle_root(self) -> Path:
        return Path(os.path.expanduser(str(getattr(self, "_bundle_root", "~/work/darling-debug"))))

    def _latest_debug_bundle(self, invocation, *, since: float) -> Path | None:
        root = self._debug_bundle_root()
        if not root.is_dir():
            return None
        suffix = re.sub(r"[^A-Za-z0-9._-]", "_", f"west-test-{invocation['name']}")
        candidates = [
            path
            for path in root.iterdir()
            if path.is_dir()
            and path.name.endswith(suffix)
            and path.stat().st_mtime >= since - 1
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)


    def _run_invocation(self, invocation, env=None) -> int:
        return dispatch_fixture_runner(
            invocation,
            env,
            runners=(
                ("guest_c_fixture", self._run_guest_c_fixture),
                ("guest_macho_fixture", self._run_guest_macho_fixture),
                ("guest_command_fixture", self._run_guest_command_fixture),
                ("guest_argv_fixture", self._run_guest_argv_fixture),
                ("c_fixture", self._run_c_fixture),
                ("object_symbol_fixture", self._run_object_symbol_fixture),
                ("source_build_fixture", self._run_source_build_fixture),
                ("source_script_fixture", self._run_source_script_fixture),
                ("cmake_configure_fixture", self._run_cmake_configure_fixture),
                ("darling_cmake_target_fixture", self._run_darling_cmake_target_fixture),
            ),
            fallback=self._run_command_invocation,
        )

    def _controller_invocation(self, invocation, args, env):
        if not invocation.get("prefix_controller") and invocation.get("ctest_env") != "darling":
            return args, env, ()
        descriptor = getattr(self, "_prefix_lease_descriptor", None)
        if descriptor is None:
            self.die(f"{invocation['name']}: host prefix controller requires an active prefix lease")
        prefix = str(self._prefix)
        environment = self._installed_runtime().environment(prefix, env)
        environment["WEST_TEST_PREFIX_LEASE"] = prefix
        environment["WEST_TEST_PREFIX_LEASE_FD"] = str(descriptor)
        controller = [
            sys.executable, "-B", str(Path(__file__).with_name("test_prefix.py")),
            "--prefix", prefix, "--", *args,
        ]
        return controller, environment, (descriptor,)

    def _run_command_invocation(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        args, run_env, pass_descriptors = self._controller_invocation(
            invocation, self._debug_runner_args(invocation, env=run_env), run_env,
        )
        result = run_bounded(
            args,
            cwd=invocation["cwd"],
            env=run_env,
            timeout_seconds=self._debug_runner_timeout_seconds(invocation),
            pass_fds=pass_descriptors,
        )
        if result.timed_out:
            self.err(
                f"{invocation['name']}: timed out after "
                f"{invocation.get('timeout_seconds', 600)}s"
            )
        rc = result.returncode
        if rc:
            self._record_failure_phase(
                invocation,
                "ctest" if invocation.get("ctest_label") or invocation.get("ctest_name") else "script",
            )
            return rc
        trace_rc = self._check_host_traces(invocation, run_env)
        if trace_rc:
            self._record_failure_phase(invocation, "run")
        return trace_rc

    def _record_failure_phase(self, invocation, phase: str) -> None:
        """Record and report the runner stage that made an invocation fail."""

        self._failure_phase = phase
        print(f"WEST_TEST_FAILURE_PHASE={phase}", file=sys.stderr)

    # --- process seams ------------------------------------------------------
    #
    # Focused contracts replace ``west_commands.test.run_bounded`` and
    # ``west_commands.test.run_guest_argv`` to intercept execution.  Methods
    # that moved to a mixin therefore call these facades instead of the
    # imported function, so the module the contract patches stays the module
    # that resolves the call.

    def _run_bounded(self, *args, **kwargs):
        return run_bounded(*args, **kwargs)

    def _run_guest_argv(self, *args, **kwargs):
        return run_guest_argv(*args, **kwargs)

    def _run_invocation_captured(self, invocation, env=None) -> InvocationResult:
        """Run an invocation and return its output and structured failure phase."""
        prior_phase = getattr(self, "_failure_phase", None)
        self._failure_phase = None
        started_at = time.time()
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as output:
            stdout_fd = os.dup(1)
            stderr_fd = os.dup(2)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(output.fileno(), 1)
                os.dup2(output.fileno(), 2)
                rc = self._run_invocation(invocation, env=env)
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                os.dup2(stdout_fd, 1)
                os.dup2(stderr_fd, 2)
                os.close(stdout_fd)
                os.close(stderr_fd)
            output.seek(0)
            result = InvocationResult(rc, output.read(), self._failure_phase)
            derived_phase = failure_phase_from_output(result.output)
            if (
                result.returncode
                and derived_phase is not None
                and result.failure_phase in {None, "script", "ctest"}
            ):
                result.failure_phase = derived_phase
            if result.returncode and result.failure_phase in {None, "script", "ctest"}:
                bundle = self._latest_debug_bundle(invocation, since=started_at)
                if bundle is not None:
                    bundle_phase = failure_phase_from_debug_bundle(
                        f"BUNDLE={bundle}\n"
                    )
                    if bundle_phase is not None:
                        result.failure_phase = bundle_phase
        self._failure_phase = prior_phase
        return result

    @contextmanager
    def _host_trace_context(self, invocation, env):
        traces = invocation.get("host_trace_files", [])
        if not traces:
            yield env
            return
        prefix = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: host-trace-files need DPREFIX")
        trace_env = dict(env or os.environ.copy())
        trace_paths = []
        for index, trace in enumerate(traces):
            if not isinstance(trace, dict):
                self.die(f"{invocation['name']}: host-trace-files entries must be mappings")
            env_name = str(trace.get("env", ""))
            rel_path = str(trace.get("prefix-relative-path", ""))
            if not rel_path:
                self.die(
                    f"{invocation['name']}: host-trace-files[{index}] needs prefix-relative-path"
                )
            if rel_path.startswith("/") or ".." in Path(rel_path).parts:
                self.die(
                    f"{invocation['name']}: host-trace-files[{index}] path must "
                    "be prefix-relative"
                )
            trace_path = Path(prefix) / rel_path
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                trace_path.unlink()
            except FileNotFoundError:
                pass
            if env_name:
                trace_env[env_name] = str(trace_path)
            trace_paths.append(trace_path)
        invocation["_host_trace_paths"] = trace_paths
        yield trace_env

    @contextmanager
    def _host_temp_context(self, invocation, env):
        temp_files = invocation.get("host_temp_files", [])
        if not temp_files:
            yield env
            return
        prefix = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: host-temp-files need DPREFIX")
        temp_env = dict(env or os.environ.copy())
        temp_paths = []
        for index, temp_file in enumerate(temp_files):
            if not isinstance(temp_file, dict):
                self.die(f"{invocation['name']}: host-temp-files entries must be mappings")
            env_name = str(temp_file.get("env", ""))
            rel_path = str(temp_file.get("prefix-relative-path", ""))
            guest_path = temp_file.get("guest-path", False)
            if not env_name or not rel_path:
                self.die(
                    f"{invocation['name']}: host-temp-files[{index}] needs env "
                    "and prefix-relative-path"
                )
            if rel_path.startswith("/") or ".." in Path(rel_path).parts:
                self.die(
                    f"{invocation['name']}: host-temp-files[{index}] path must "
                    "be prefix-relative"
                )
            if not isinstance(guest_path, bool):
                self.die(
                    f"{invocation['name']}: host-temp-files[{index}] guest-path must be a boolean"
                )
            temp_path = Path(prefix) / rel_path
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            if "contents" in temp_file and temp_file["contents"] is not None:
                temp_path.write_text(str(temp_file["contents"]))
            temp_env[env_name] = f"/{rel_path}" if guest_path else str(temp_path)
            temp_paths.append(temp_path)
        invocation["_host_temp_paths"] = temp_paths
        try:
            yield temp_env
        finally:
            for temp_path in temp_paths:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def _check_host_traces(self, invocation, env) -> int:
        traces = invocation.get("host_trace_files", [])
        if not traces:
            return 0
        for index, trace in enumerate(traces):
            trace_path = invocation.get("_host_trace_paths", [])[index]
            if not trace_path.is_file():
                self.err(f"  missing host trace file: {trace_path}")
                return 1
            content = trace_path.read_text(errors="replace")
            print(content, end="" if content.endswith("\n") else "\n")
            for expected in [str(item) for item in trace.get("contains", [])]:
                if expected not in content:
                    self.err(f"  missing host trace content in {trace_path}: {expected}")
                    return 1
        return 0

    def _run_c_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: c-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        source_root = invocation["cwd"]
        source_root_module = invocation.get("source_root_module")
        if source_root_module:
            source_root = self._project_path(source_root_module)
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        with tempfile.TemporaryDirectory(prefix=f"west-c-fixture-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            stub_root = tempdir / "include"
            for header in invocation.get("stub_headers", []):
                header_path = stub_root / header
                header_path.parent.mkdir(parents=True, exist_ok=True)
                header_path.write_text("\n")
            for header, content in invocation.get("generated_headers", {}).items():
                header_path = stub_root / header
                header_path.parent.mkdir(parents=True, exist_ok=True)
                header_path.write_text(content)
            binary = tempdir / Path(invocation["script_path"]).stem
            args = [
                invocation.get("cc", "cc"),
                *invocation.get("compile_flags", []),
                "-I",
                str(stub_root),
            ]
            for include_dir in invocation.get("fixture_include_dirs", []):
                include_path = Path(include_dir)
                if not include_path.is_absolute():
                    include_path = invocation["cwd"] / include_path
                args.extend(["-I", str(include_path)])
            for include_dir in invocation.get("include_dirs", []):
                include_path = Path(include_dir)
                if not include_path.is_absolute():
                    include_path = source_root / include_path
                args.extend(["-I", str(include_path)])
            for source_file in invocation.get("source_files", []):
                source_path = Path(source_file)
                if not source_path.is_absolute():
                    source_path = source_root / source_path
                args.append(str(source_path))
            args.extend([str(invocation["script_path"]), "-o", str(binary)])
            compile_result = run_bounded(
                args,
                cwd=invocation["cwd"],
                env=run_env,
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if compile_result.timed_out:
                self.err(f"{invocation['name']}: compile timed out")
            if compile_result.returncode:
                self._record_failure_phase(invocation, "compile")
                return compile_result.returncode
            run_result = run_bounded(
                [str(binary)],
                cwd=invocation["cwd"],
                env=run_env,
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if run_result.timed_out:
                self.err(f"{invocation['name']}: test binary timed out")
            if run_result.returncode:
                self._record_failure_phase(invocation, "run")
            return run_result.returncode

    def _run_object_symbol_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: object-symbol-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        source_root = invocation["cwd"]
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        source_path = Path(invocation["source_file"])
        if not source_path.is_absolute():
            source_path = source_root / source_path
        with tempfile.TemporaryDirectory(prefix=f"west-object-symbol-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            for check in invocation.get("symbol_checks", []):
                object_path = tempdir / f"{check['name']}.o"
                args = [
                    invocation.get("cc", "cc"),
                    "-c",
                    *invocation.get("compile_flags", []),
                    *check.get("compile_flags", []),
                ]
                for include_dir in invocation.get("fixture_include_dirs", []):
                    include_path = Path(include_dir)
                    if not include_path.is_absolute():
                        include_path = invocation["cwd"] / include_path
                    args.extend(["-I", str(include_path)])
                for include_dir in invocation.get("include_dirs", []):
                    include_path = Path(include_dir)
                    if not include_path.is_absolute():
                        include_path = source_root / include_path
                    args.extend(["-I", str(include_path)])
                args.extend([str(source_path), "-o", str(object_path)])
                compile_result = run_bounded(
                    args,
                    cwd=invocation["cwd"],
                    env=run_env,
                    timeout_seconds=int(invocation.get("timeout_seconds", 600)),
                )
                if compile_result.timed_out:
                    self.err(f"{invocation['name']}:{check['name']}: compile timed out")
                if compile_result.returncode:
                    self._record_failure_phase(invocation, "compile")
                    return compile_result.returncode
                nm = run_bounded(
                    ["nm", "-u", str(object_path)],
                    cwd=invocation["cwd"],
                    env=run_env,
                    timeout_seconds=int(invocation.get("timeout_seconds", 600)),
                    capture_output=True,
                )
                if nm.timed_out:
                    self.err(f"{invocation['name']}:{check['name']}: nm timed out")
                if nm.returncode:
                    sys.stderr.write(nm.stdout)
                    sys.stderr.write(nm.stderr)
                    self._record_failure_phase(invocation, "inspect")
                    return nm.returncode
                symbols = {
                    line.split()[-1]
                    for line in nm.stdout.splitlines()
                    if line.split()
                }
                for symbol in check.get("present_undefined_symbols", []):
                    if symbol not in symbols:
                        self.err(f"{invocation['name']}:{check['name']}: missing undefined symbol {symbol}")
                        self._record_failure_phase(invocation, "inspect")
                        return 1
                for symbol in check.get("absent_undefined_symbols", []):
                    if symbol in symbols:
                        self.err(f"{invocation['name']}:{check['name']}: unexpected undefined symbol {symbol}")
                        self._record_failure_phase(invocation, "inspect")
                        return 1
                if check.get("present_defined_symbols") or check.get("absent_defined_symbols"):
                    defined_nm = run_bounded(
                        ["nm", "-g", str(object_path)],
                        cwd=invocation["cwd"],
                        env=run_env,
                        timeout_seconds=int(invocation.get("timeout_seconds", 600)),
                        capture_output=True,
                    )
                    if defined_nm.timed_out:
                        self.err(f"{invocation['name']}:{check['name']}: nm timed out")
                    if defined_nm.returncode:
                        sys.stderr.write(defined_nm.stdout)
                        sys.stderr.write(defined_nm.stderr)
                        self._record_failure_phase(invocation, "inspect")
                        return defined_nm.returncode
                    defined_symbols = set()
                    for line in defined_nm.stdout.splitlines():
                        parts = line.split()
                        if not parts:
                            continue
                        if parts[0] == "U":
                            continue
                        if len(parts) >= 3:
                            defined_symbols.add(parts[-1])
                    for symbol in check.get("present_defined_symbols", []):
                        if symbol not in defined_symbols:
                            self.err(f"{invocation['name']}:{check['name']}: missing defined symbol {symbol}")
                            self._record_failure_phase(invocation, "inspect")
                            return 1
                    for symbol in check.get("absent_defined_symbols", []):
                        if symbol in defined_symbols:
                            self.err(f"{invocation['name']}:{check['name']}: unexpected defined symbol {symbol}")
                            return 1
        return 0

    def _run_source_script_fixture(self, invocation, env=None) -> int:
        if invocation.get("diag", "bare") != "bare":
            self.die(f"{invocation['name']}: source-script-fixture currently supports diag:bare only")
        run_env = env if env is not None else invocation.get("env")
        source_root = invocation["cwd"]
        source_root_env = invocation.get("source_root_env")
        if source_root_env and run_env and run_env.get(source_root_env):
            source_root = Path(run_env[source_root_env])
        script_path = source_root / invocation["source_script"]
        if not script_path.is_file():
            self.err(f"{invocation['name']}: source script not found: {script_path}")
            self._record_failure_phase(invocation, "setup")
            return 1

        timeout_seconds = int(invocation.get("timeout_seconds", 600))
        for case in invocation.get("cases", []):
            if os.access(script_path, os.X_OK):
                args = [str(script_path), *case.get("args", [])]
            else:
                args = ["sh", str(script_path), *case.get("args", [])]
            args, case_env, pass_descriptors = self._controller_invocation(invocation, args, run_env)
            result = run_bounded(
                args,
                cwd=source_root,
                env=case_env,
                timeout_seconds=timeout_seconds,
                capture_output=True,
                pass_fds=pass_descriptors,
            )
            if result.timed_out:
                self.err(
                    f"{invocation['name']}:{case['name']}: timed out after "
                    f"{timeout_seconds}s"
                )
                self._record_failure_phase(invocation, "script")
                return 124
            expected_rc = case.get("returncode", 0)
            if result.returncode != expected_rc:
                sys.stderr.write(result.stdout)
                sys.stderr.write(result.stderr)
                self._record_failure_phase(invocation, "script")
                self.err(
                    f"{invocation['name']}:{case['name']}: rc {result.returncode}, "
                    f"want {expected_rc}"
                )
                return 1
            expected_stdout = case.get("stdout")
            if expected_stdout is not None and result.stdout != expected_stdout:
                sys.stderr.write(result.stderr)
                self._record_failure_phase(invocation, "script")
                self.err(
                    f"{invocation['name']}:{case['name']}: stdout "
                    f"{result.stdout!r}, want {expected_stdout!r}"
                )
                return 1
        return 0

    @contextmanager
    def _descriptor_trace_context(self, invocation, env):
        declaration = invocation.get("descriptor_trace")
        if not declaration:
            yield env
            return
        try:
            specs = descriptor_trace_window_specs(declaration)
        except DescriptorTraceError as error:
            self.die(f"{invocation['name']}: descriptor-trace {error}")
        if shutil.which("strace") is None:
            self.die(
                f"{invocation['name']}: descriptor-trace requires strace on the host"
            )
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(invocation["name"]))
        trace_dir = Path(invocation["cwd"]) / ".west-test" / "descriptor-trace" / name
        if trace_dir.exists():
            shutil.rmtree(trace_dir)
        trace_dir.mkdir(parents=True)
        trace_env = dict(env or os.environ.copy())
        trace_env["WEST_DESCRIPTOR_TRACE_DIR"] = str(trace_dir)
        invocation["_descriptor_trace_dir"] = trace_dir
        invocation["_descriptor_trace_specs"] = specs
        yield trace_env

    @contextmanager
    def _host_stat_context(self, invocation, env):
        deltas = invocation.get("host_stat_deltas", [])
        if not deltas:
            yield env
            return
        prefix = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
        if not prefix:
            self.die(f"{invocation['name']}: host-stat-deltas need DPREFIX")
        tool = Path(str(invocation.get("host_stat_tool", "darling-stat")))
        if not tool.is_absolute():
            resolved = shutil.which(str(tool))
            if resolved:
                tool = Path(resolved)
        if not tool.is_file() or not os.access(tool, os.X_OK):
            self.die(f"{invocation['name']}: missing darling stat tool: {tool}")
        invocation["_host_stat_tool"] = str(tool)
        yield env

    def _run_guest_c_fixture(self, invocation, env=None) -> int:
        return run_guest_c_fixture(self, invocation, env)

    def _run_guest_macho_fixture(self, invocation, env=None) -> int:
        return run_guest_macho_fixture(self, invocation, env)

    def _run_guest_command_fixture(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        if not run_env:
            run_env = self._execution_env(invocation)
        if not run_env:
            run_env = os.environ.copy()
        return run_guest_command_fixture(
            invocation,
            env=run_env,
            prefix=getattr(self, "_prefix", None),
            resolve_launcher=self._resolve_darling_launcher,
            die=self.die,
            err=self.err,
            record_failure_phase=self._record_failure_phase,
        )

    def _run_guest_argv_fixture(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        if not run_env:
            run_env = self._execution_env(invocation)
        if not run_env:
            run_env = os.environ.copy()
        return run_guest_argv_fixture(
            invocation,
            env=run_env,
            prefix=getattr(self, "_prefix", None),
            resolve_launcher=self._resolve_darling_launcher,
            die=self.die,
            err=self.err,
            record_failure_phase=self._record_failure_phase,
        )

    def _execution_env(self, invocation):
        merged = os.environ.copy()
        merged.update(invocation.get('env') or {})
        manifest_root = getattr(getattr(self, "manifest", None), "repo_abspath", None)
        if manifest_root:
            merged["DARLING_WORKSPACE_ROOT"] = str(manifest_root)
        if getattr(self, "_source_root", None):
            merged["DARLING_SOURCE_ROOT"] = str(self._source_root)
        source_env = invocation.get('source_env')
        if source_env and not merged.get(source_env):
            source = self._project_path(invocation['source_module'])
            merged[source_env] = str(source)
        if set(invocation.get('requires_resources', ())) & {'darling-prefix','darling-eunion-prefix'}:
            if self._prefix:
                merged = self._installed_runtime().environment(self._prefix, merged)
        return merged
    

    @staticmethod
    def _bootstrap_prefix_advice(prefix: Path, *, guest_toolchain: bool) -> str:
        toolchain = ' --guest-toolchain darling-command-line-tools' if guest_toolchain else ''
        return f'provision the selected installation and guest toolchain: west darling-bootstrap --build-dir BUILD --runtime-root ROOT --prefix {quote(str(prefix))}{toolchain}'
    

    def _missing_requirements(self, invocation):
        missing = [name for name in invocation.get('requires_env', ()) if not os.environ.get(name)]
        resources = set(invocation.get('requires_resources', ()))
        if resources & {'darling-prefix','darling-eunion-prefix'}:
            if not self._prefix:
                missing.append('darling-prefix (--prefix or DPREFIX)')
            else:
                self._installed_runtime()
                if invocation.get('guest_c_fixture'):
                    problems = self._guest_c_fixture_prerequisite_problems(Path(self._prefix), invocation.get('guest_cc',''), invocation.get('guest_cflags',''))
                    if problems:
                        missing.extend(problems)
                        missing.append(self._bootstrap_prefix_advice(Path(self._prefix), guest_toolchain=True))
        return missing
    


    def _guest_c_fixture_prerequisite_problems(
        self,
        prefix: Path,
        guest_cc: str,
        guest_cflags: str,
    ) -> list[str]:
        return guest_c_fixture_prerequisite_problems(
            prefix, guest_cc, guest_cflags, system_root=self._installed_runtime().system_root
        )


    @contextmanager
    def _resource_context(self, invocation, env):
        with resource_context(self, invocation, env) as resource_env:
            yield resource_env

    @contextmanager
    def _dcc_cache_context(self, invocation, env):
            spec = invocation.get("dcc_cache")
            if spec is None:
                yield
                return
            if not isinstance(spec, dict):
                self.die(f"{invocation['name']}: dcc-cache must be a mapping")
            prefix_text = (env or {}).get("DPREFIX") or getattr(self, "_prefix", None)
            if not prefix_text:
                self.die(f"{invocation['name']}: dcc-cache needs DPREFIX")
            prefix = Path(prefix_text)
            source_module = str(spec.get("source-module", "darling/src/external/darlingserver"))
            tools_dir_name = str(spec.get("tools-dir", "tools/closure-cache"))
            builder_name = str(spec.get("builder", "dcc5-builder.c"))
            list_name = str(spec.get("closure-list", "closure-list.txt"))
            install_root_mode = str(spec.get("install-root", "guest-visible"))
            guest_env_name = str(spec.get("env", "DARLING_DYLD_DCC2_PATH"))
            enable_env_name = str(spec.get("enable-env", "DARLING_DYLD_DCC2"))
            if install_root_mode not in {"guest-visible", "base", "prefix"}:
                self.die(
                    f"{invocation['name']}: dcc-cache install-root must be "
                    "guest-visible, base, or prefix"
                )
            if not guest_env_name or not guest_env_name.isidentifier():
                self.die(f"{invocation['name']}: dcc-cache env must be a shell variable name")
            if enable_env_name and not enable_env_name.isidentifier():
                self.die(f"{invocation['name']}: dcc-cache enable-env must be a shell variable name")
    
            old_guest_env = dict(invocation.get("guest_env_vars", {}))
            work_rel = Path("private/var/tmp") / f"west-dcc-cache-{os.getpid()}-{int(time.time() * 1000)}"
            host_dir = prefix / work_rel
            guest_dir = "/" + str(work_rel)
            install_root = self._dcc_install_root(prefix, env, install_root_mode)
            with tempfile.TemporaryDirectory(prefix=f"west-dcc-cache-{invocation['name']}-") as temp:
                tempdir = Path(temp)
                source_root = self._dcc_cache_source_root(
                    invocation,
                    env,
                    source_module,
                    tools_dir_name,
                    tempdir,
                )
                tools_dir = source_root / tools_dir_name
                builder_source = tools_dir / builder_name
                closure_list = tools_dir / list_name
                if not builder_source.is_file():
                    self.die(f"{invocation['name']}: DCC builder not found: {builder_source}")
                if not closure_list.is_file():
                    self.die(f"{invocation['name']}: DCC closure list not found: {closure_list}")
                builder = tempdir / Path(builder_name).stem
                host_cache = host_dir / "system-closure.dcc6"
                guest_cache = f"{guest_dir}/system-closure.dcc6"
                try:
                    host_dir.mkdir(parents=True, exist_ok=False)
                    compile_args = ["gcc", "-O2", "-o", str(builder), str(builder_source)]
                    self.inf(f"  DCC cache builder: {' '.join(quote(str(arg)) for arg in compile_args)}")
                    self._run_dcc_cache_command(invocation, "compile", compile_args, tools_dir)
                    build_args = [
                        str(builder),
                        str(install_root),
                        str(closure_list),
                        str(host_cache),
                    ]
                    self.inf(
                        f"  DCC cache build: {host_cache} "
                        f"(install-root={install_root})"
                    )
                    self._run_dcc_cache_command(invocation, "build", build_args, tools_dir)
                    if spec.get("stale"):
                        self._make_dcc_cache_stale(host_cache)
                    guest_env = dict(old_guest_env)
                    if enable_env_name:
                        guest_env[enable_env_name] = str(spec.get("enable-value", "1"))
                    guest_env[guest_env_name] = guest_cache
                    if spec.get("soft"):
                        guest_env["DARLING_DYLD_DCC2_SOFT"] = "1"
                    invocation["guest_env_vars"] = guest_env
                    yield
                finally:
                    invocation["guest_env_vars"] = old_guest_env
                    shutil.rmtree(host_dir, ignore_errors=True)

    def _run_dcc_cache_command(self, invocation, stage: str, args, cwd: Path) -> None:
        """Run bounded host-side DCC preparation and preserve its failure tail."""

        timeout_seconds = int(invocation.get("timeout_seconds", 600))
        result = run_bounded(
            args,
            cwd=cwd,
            env=None,
            timeout_seconds=timeout_seconds,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        if result.timed_out:
            self.err(
                f"{invocation['name']}: DCC cache {stage} timed out after "
                f"{timeout_seconds}s"
            )
        self._dump_command_tail(f"DCC cache {stage}", result)
        self._record_failure_phase(invocation, "setup")
        self.die(f"{invocation['name']}: DCC cache {stage} failed with rc {result.returncode}")

    def _dcc_cache_source_root(self, invocation, env, source_module: str, tools_dir_name: str, tempdir: Path) -> Path:
        return self._project_path(source_module)
    

    def _dcc_install_root(self, prefix: Path, env, mode: str) -> Path:
        return prefix if mode == 'prefix' else self._installed_runtime().system_root
    

    def _make_dcc_cache_stale(self, cache_path: Path) -> None:
        """Mutate the first image's recorded src_size so reader validation rejects it."""
        header_size = 424
        first_image_src_size_offset = header_size + 256 + 16 + 8 + 8
        with cache_path.open("r+b") as handle:
            handle.seek(first_image_src_size_offset)
            raw = handle.read(8)
            if len(raw) != 8:
                self.die(f"DCC cache too small to stale-mutate: {cache_path}")
            value = int.from_bytes(raw, "little", signed=False)
            handle.seek(first_image_src_size_offset)
            handle.write((value + 1).to_bytes(8, "little", signed=False))

    def _mkdirs_for_fixture(self, target: Path, root: Path) -> list[Path]:
        root = root.resolve()
        to_create = []
        current = target
        while current != root and root in current.resolve().parents:
            if current.exists():
                break
            to_create.append(current)
            current = current.parent
        for path in reversed(to_create):
            path.mkdir()
        return list(reversed(to_create))

    

    def _dump_command_tail(self, label: str, result) -> None:
        output = process_output_text(result)
        if output:
            self.err(f'{label} output:\n' + '\n'.join(output.splitlines()[-160:]))
    



    

    

    def _shutdown_runtime_prefix(self, prefix: Path, *, extra_env=None) -> bool:
        return self._prefix_lifecycle_owner().shutdown(prefix, extra_env=extra_env)
    

    def _finalize_prefix_shutdown(self, prefix: Path) -> bool:
        return self._prefix_lifecycle_owner().finalize(prefix)

    

    

    

    

    

    

    

    

    

    

    

    

    

    

    

    

    

    

    

    def _shutdown_test_prefix(self) -> bool:
            prefix = getattr(self, "_prefix", None)
            if not prefix:
                return True
            return self._prefix_lifecycle_owner().shutdown(
                prefix, keep_running=getattr(self, "_keep_prefix_running", False)
            )

    

    

    def _prefix_lifecycle_owner(self) -> PrefixLifecycleOwner:
        return PrefixLifecycleOwner(
            resolve_launcher=self._resolve_darling_launcher,
            prefix_env=self._darling_prefix_env,
            cleanup_mounts=cleanup_prefix_mounts,
            init_pid_is_usable=darling_init_pid_is_usable,
            inf=self.inf,
            err=getattr(self, "err", lambda _message: None),
            wrn=getattr(self, "wrn", lambda _message: None),
            process_entries=self._ps_entries,
        )

    def _cleanup_prefix_mounts(self, prefix: Path) -> bool:
        result = cleanup_prefix_mounts(prefix)
        for message in result.changed:
            self.inf(f"cleanup Darling prefix mount: {message}")
        for message in result.problems:
            self.err(f"leftover Darling prefix mount for {prefix}: {message}")
        return result.success

    def _remove_stale_init_pid(self, prefix: Path) -> None:
        remove_stale_init_pid(prefix, pid_is_usable=darling_init_pid_is_usable)

    def _remove_stale_server_socket(self, prefix: Path) -> bool:
        return remove_stale_server_socket(prefix)

    def _cleanup_rootless_runtime_sockets(
        self, prefix: Path
    ) -> RootlessRuntimeSocketCleanupResult:
        return cleanup_rootless_runtime_sockets(prefix)

    @contextmanager
    def _prefix_resource_context(self, enabled: bool):
        prefix = getattr(self, "_prefix", None)
        if not enabled or not prefix:
            yield
            return

        with self._prefix_lifecycle_owner().locked(os.path.expanduser(str(prefix))) as descriptor:
            previous_descriptor = getattr(self, "_prefix_lease_descriptor", None)
            self._prefix_lease_descriptor = descriptor
            self._prefix_cleanup_failed = False
            try:
                if not self._shutdown_test_prefix():
                    self._prefix_cleanup_failed = True
                    self.die(
                        f"could not reset Darling prefix before test run: {prefix}"
                    )
                yield descriptor
            finally:
                try:
                    if not self._shutdown_test_prefix():
                        self._prefix_cleanup_failed = True
                finally:
                    self._prefix_lease_descriptor = previous_descriptor

    def _changed_submodules(self) -> list[str]:
        """Submodules whose checkout differs from their manifest revision.

        Prefer West's local manifest-rev ref when available. It records the
        exact revision selected by the manifest, regardless of whether the
        manifest used a branch name or SHA. Dirty worktrees are always selected.
        """
        changed: list[str] = []
        for project in self.manifest.projects:
            if not self.manifest.is_active(project):
                continue
            path = Path(self.topdir) / project.path
            if path == Path(self.manifest.repo_abspath):
                continue
            if not (path / ".git").exists():
                continue
            label_name = Path(project.path).name
            if self._worktree_dirty(path, parent=project.name == "darling"):
                changed.append(label_name)
                continue
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            manifest_rev = subprocess.run(
                ["git", "rev-parse", "--verify", "manifest-rev^{commit}"],
                cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            if not manifest_rev and project.revision:
                manifest_rev = subprocess.run(
                    ["git", "rev-parse", "--verify", f"{project.revision}^{{commit}}"],
                    cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            if head and manifest_rev and head != manifest_rev:
                changed.append(label_name)
        return changed

    @staticmethod
    def _clear_ctest_failure_record(build: Path) -> None:
        """Discard CTest's prior-run failure list before a new invocation.

        CTest does not clear ``LastTestsFailed.log`` after a later green run.
        Leaving it in place makes a fresh successful selection look failed to
        humans and to any diagnostic tooling that inspects the build tree.
        """

        (build / "Testing" / "Temporary" / "LastTestsFailed.log").unlink(
            missing_ok=True
        )

    @staticmethod
    def _dir_size(path: Path) -> int:
        return sum(
            entry.stat().st_size
            for entry in path.rglob("*")
            if entry.is_file() and not entry.is_symlink()
        )

    @staticmethod
    def _format_size(size: int) -> str:
        """Format a byte count compactly for cleanup diagnostics."""

        for unit, scale in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
            if size >= scale:
                return f"{size / scale:.1f}{unit}"
        return f"{size}B"

    def _gc_bundles(
        self, root: Path, keep_last: int, max_mb: int, dry_run: bool = False
    ) -> None:
        """Prune debug bundles so the dir cannot balloon (we saw 7.4G/980).

        Drop any bundle over max_mb (forensic cores/rpctrace), then keep only
        the newest keep_last of the rest. Bundles are timestamp-named dirs.
        Non-directory entries (stray files) are left untouched.
        """
        root = root.expanduser()
        if not root.is_dir():
            self.inf(f"no bundle dir at {root}")
            return
        # The name predicate is the whole safety property of this pass. Without
        # it the pass treats every directory under the root as a bundle and
        # selects by age and count, which is how a west dev job state directory
        # and another workstream's experiment root became eligible for deletion:
        # one that appeared after a dry run was counted into the "over count"
        # set and removed without ever being planned.
        bundles: list[Path] = []
        left_alone = 0
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if not BUNDLE_NAME.match(entry.name):
                left_alone += 1
                self.inf(f"left alone (not a west-test bundle): {entry.name}")
                continue
            bundles.append(entry)
        bundles.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        cap = max_mb * 1024 * 1024
        freed = 0
        kept = 0
        verb = "would prune" if dry_run else "pruned"
        for bundle in bundles:
            size = self._dir_size(bundle)
            over_cap = size > cap
            over_count = kept >= keep_last
            if over_cap or over_count:
                why = "size" if over_cap else "count"
                freed += size
                self.inf(f"{verb} ({why}, {size // (1024 * 1024)}M): {bundle.name}")
                if not dry_run:
                    shutil.rmtree(bundle, ignore_errors=True)
            else:
                kept += 1
        action = "would free" if dry_run else "freed"
        self.inf(
            f"gc: kept {kept}, {action} {freed // (1024 * 1024)}M from {root}"
        )
        if left_alone:
            self.inf(
                f"gc: left alone {left_alone} entries under {root} that are "
                "not west-test bundles"
            )

    def _gc_runtime_proof_scratch(
        self,
        root: Path,
        max_age_hours: float,
        keep_last: int,
        dry_run: bool = False,
    ) -> None:
        root = root.expanduser()
        if max_age_hours < 0:
            self.die("--proof-scratch-max-age-hours must be >= 0")
        if keep_last < 0:
            self.die("--proof-scratch-keep-last must be >= 0")
        if not root.is_dir():
            self.inf(f"no proof scratch root at {root}")
            return
        cutoff = time.time() - (max_age_hours * 3600)
        patterns = (
            "west-red-proof-runtime-*",
            "west-green-proof-runtime-*",
            "west-red-proof-source-*",
            "west-green-proof-source-*",
            "west-red-proof-deploy-*",
            "west-ctest-runtime-*",
            "west-runtime-*",
        )
        candidates = sorted(
            {
                path
                for pattern in patterns
                for path in root.glob(pattern)
                if path.is_dir() and not path.is_symlink()
            },
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        # A name is not ownership. Only a directory whose creator wrote the
        # ownership marker before filling it is selected; the rest are reported
        # with the size they hold, because deleting a name-match that belongs to
        # someone else is the failure this rule exists to prevent, and staying
        # silent about it would hide the disk instead of reclaiming it.
        all_scratch_dirs = []
        for path in candidates:
            if scratch_owner(path) is None:
                self.inf(
                    "left alone (scratch without an ownership marker): "
                    f"{path} ({self._format_size(self._dir_size(path))})"
                )
            else:
                all_scratch_dirs.append(path)
        freed = 0
        retained = 0
        pruned = 0
        verb = "would prune" if dry_run else "pruned"
        now = time.time()
        for index, scratch in enumerate(all_scratch_dirs):
            size = self._dir_size(scratch)
            age_hours = max(0.0, (now - scratch.stat().st_mtime) / 3600)
            over_count = index >= keep_last
            stale = scratch.stat().st_mtime <= cutoff
            if over_count or stale:
                if over_count and stale:
                    reason = "count+age"
                elif over_count:
                    reason = "count"
                else:
                    reason = "age"
                freed += size
                pruned += 1
                self.inf(
                    f"{verb} proof scratch ({reason}, {self._format_size(size)}, "
                    f"age {age_hours:.1f}h): {scratch}"
                )
                if not dry_run:
                    shutil.rmtree(scratch, ignore_errors=True)
            else:
                retained += 1
                self.inf(
                    f"retained proof scratch (newest, {self._format_size(size)}, "
                    f"age {age_hours:.1f}h): {scratch}"
                )
        action = "would free" if dry_run else "freed"
        self.inf(
            "proof-scratch gc: "
            f"retained {retained}, {verb} {pruned} dir(s), "
            f"{action} {self._format_size(freed)} from {root}"
        )

    def _gc_guest_runner_output(
        self,
        root: Path,
        max_age_hours: float,
        dry_run: bool = False,
    ) -> None:
        """Prune stale local output files left by pre-cleanup guest C runners.

        The guest runner now unlinks its output on every exit path.  This pass
        only repairs historical files, so it uses the same age threshold as
        runtime scratch and deliberately ignores directories, symlinks, and
        fresh output that may belong to a still-running test.
        """

        root = root.expanduser()
        if max_age_hours < 0:
            self.die("--proof-scratch-max-age-hours must be >= 0")
        if not root.is_dir():
            self.inf(f"no guest runner output root at {root}")
            return
        cutoff = time.time() - (max_age_hours * 3600)
        outputs = sorted(
            (
                path
                for path in root.glob("west-ctest-guest-c.*")
                if path.is_file()
                and not path.is_symlink()
                and path.stat().st_mtime <= cutoff
            ),
            key=lambda path: path.stat().st_mtime,
        )
        # The same rule as the scratch pass: a file name is not ownership. These
        # are outputs of a runner that now unlinks its own output on every exit
        # path, so anything matching here is either historical or someone else's,
        # and both are reported rather than removed. Reclaiming the space is an
        # operator decision with the path and size in hand.
        for output in outputs:
            self.inf(
                f"left alone (guest runner output, {output.stat().st_size}B): {output}"
            )
        self.inf(
            f"guest-runner gc: left alone {len(outputs)} file(s) under {root}"
        )

    def _gc_west_temp_worktree_registrations(self, *, dry_run: bool) -> None:
        """Prune stale west temp worktree registrations, reporting them first.

        The plan and the real run select with the same predicate and print the
        same lines. The real run used to perform this mutation without the plan
        mentioning it at all, which is the difference this pass exists to
        remove: a plan that omits an effect is not a plan of that effect.
        """
        repos = [
            Path(project.abspath)
            for project in getattr(self.manifest, "projects", [])
            if getattr(project, "name", None) != "manifest"
        ]
        if dry_run:
            count = 0
            for repo in repos:
                for entry in prunable_west_temp_worktrees(repo):
                    count += 1
                    self.inf(
                        f"would prune stale west temp worktree registration in {repo}: {entry}"
                    )
            self.inf(f"stale-worktree gc: would prune {count} registration(s)")
            return
        pruned = prune_stale_west_temp_worktrees(repos)
        for entry in pruned:
            self.inf(f"pruned stale west temp worktree registration: {entry}")
        self.inf(f"stale-worktree gc: pruned {len(pruned)} registration(s)")

    # --- entrypoint ---------------------------------------------------------

    @contextmanager
    def _fresh_prefix_context(self, args):
        baseline = getattr(args, "fresh_prefix_from", None)
        if not baseline:
            yield
            return
        if args.prefix:
            self.die("--fresh-prefix-from cannot be combined with --prefix")
        fresh_root = durable_subdir(self.topdir, "prefixes")
        fresh_root.mkdir(parents=True, exist_ok=True)
        result = create_fresh_prefix(
            baseline, temp_root=fresh_root,
            runtime_root=getattr(args, "runtime_root", None),
        )
        for message in result.changed:
            self.inf(f"fresh prefix: {message}")
        if not result.success or result.path is None:
            self.die("fresh prefix: " + "; ".join(result.problems))
        args.prefix = str(result.path)
        try:
            yield
        finally:
            cleanup = remove_fresh_prefix(result.path, temp_root=fresh_root)
            for message in cleanup.changed:
                self.inf(f"fresh prefix: {message}")
            for message in cleanup.problems:
                self.err(f"fresh prefix: {message}")
            if not cleanup.success:
                self._prefix_cleanup_failed = True

    @contextmanager
    def _lane_context(self, args):
        """Run this invocation inside one isolation lane when one is named.

        This is a mode of the existing runner, not a second runner: the command
        line, the test selection and the acceptance semantics are unchanged.
        What changes is *where* the run happens - the lane's own copy-on-write
        prefix, state root, temporary directory and runtime directory - and
        *who owns its processes*: the whole tree joins the lane's own
        `systemd --user` scope, so stopping one lane cannot reach another.
        """

        name = getattr(args, "lane", None)
        if not name:
            yield
            return
        if args.prefix or getattr(args, "fresh_prefix_from", None):
            self.die(
                "--lane cannot be combined with --prefix or --fresh-prefix-from"
            )
        environ = dict(os.environ)
        root = lane_root(environ, Path(self.topdir).resolve())
        try:
            lane = lane_path(root, validate_lane_name(name))
        except LaneError as error:
            self.die(str(error))
        try:
            record = read_record(lane)
            if record is None:
                self.die(
                    f"no lane '{name}' under {root}; create it with "
                    f"'west dev lane create {name} (--base <prefix> | --parent <frozen-lane>)'"
                )
            problems = record_problems(lane, record)
            if problems:
                self.die(f"lane '{name}' is not usable: " + "; ".join(problems))
            if record.get("managed_run") is not None:
                from managed_run import authorize_lane_execution
                authorize_lane_execution(lane, record)
            environment = lane_env(lane, record)
        except LaneError as error:
            self.die(str(error))
        unit = scope_unit(lane)
        if environ.get(SCOPE_ACTIVE_ENV) != str(lane):
            # Fail closed. A lane run without its own cgroup is a run whose cleanup can miss a guest
            # that scrubbed its environment, and `--lane` exists for parallel agents.
            try:
                require_scope()
                claim = claim_scope(unit, lane=lane)
            except LaneError as error:
                self.die(str(error))
            for message in claim.changed:
                self.inf(f"lane '{name}': {message}")
            environment[SCOPE_ACTIVE_ENV] = str(lane)
            argv = scope_wrap(
                unit,
                env_argv(environment, [*west_command_argv(), *sys.argv[1:]]),
                description=scope_description(lane),
            )
            self.inf(f"lane '{name}': re-entering under cgroup {unit}")
            try:
                os.execvp(argv[0], argv)
            except OSError as error:
                self.die(f"cannot re-enter lane '{name}' under {unit}: {error}")

        for key, value in environment.items():
            os.environ[key] = value
        # The lane's temporary directory must be seen by every later lookup,
        # including one that already cached the host default.
        tempfile.tempdir = None
        args.prefix = environment["DPREFIX"]
        self.inf(
            f"lane '{name}': prefix={args.prefix} state_root={environment['DW_STATE_ROOT']}"
        )
        yield

    @contextmanager
    def _runtime_use_context(self, args):
        selected = getattr(args, "runtime_root", None) or os.environ.get("DARLING_RUNTIME_ROOT")
        with ExitStack() as guards:
            if selected and not getattr(args, "gc", False):
                try:
                    guards.enter_context(runtime_use(Path(selected)))
                except (OSError, ValueError) as error:
                    self.die(str(error))
            self._runtime_guards = None if getattr(args, "gc", False) else guards
            try:
                yield
            finally:
                self._runtime_guards = None

    def do_run(self, args, unknown):
        try:
            with self._lane_context(args):
                with self._runtime_use_context(args), self._fresh_prefix_context(args):
                    result = self._do_run(args, unknown)
        except SystemExit as error:
            if not error.code and getattr(self, "_prefix_cleanup_failed", False):
                raise SystemExit(1) from error
            raise
        if getattr(self, "_prefix_cleanup_failed", False):
            raise SystemExit(1)
        return result

    def _do_run(self, args, unknown):
        self._prefix = self._resolve_prefix(args)
        self._runtime_root = args.runtime_root
        self._runtime = None
        source = getattr(args, "source_root", None) or os.environ.get("DARLING_SOURCE_ROOT")
        if source:
            self._source_root = Path(source).expanduser().resolve()
            if not (self._source_root / "CMakeLists.txt").is_file():
                self.die(f"not a Darling source checkout: {self._source_root}")
        else:
            projects = getattr(getattr(self, "manifest", None), "projects", [])
            current = next((project.abspath for project in projects if project.path == "darling"), None)
            self._source_root = Path(current) if current else None
        if self._source_root:
            self.inf(f"test source checkout: {self._source_root}")
        self._executor = self._resolve_executor(args.executor)
        self._keep_prefix_running = args.keep_prefix_running
        if not args.bundle_root:
            args.bundle_root = str(state_subdir('bundles') or Path('~/work/darling-debug').expanduser())
        if not args.proof_scratch_root:
            args.proof_scratch_root = str(state_subdir('scratch') or tempfile.gettempdir())
        if not args.runtime_evidence_root:
            args.runtime_evidence_root = str(state_subdir('evidence') or '.west-test/runtime-evidence')
        self._bundle_root = str(Path(args.bundle_root).expanduser())
        self._runtime_evidence_root = Path(args.runtime_evidence_root).expanduser()
        if args.ctest_timeout_seconds <= 0:
            self.die('--ctest-timeout-seconds must be > 0')
        evidence_action = getattr(args, "runtime_evidence", None)
        evidence_id = getattr(args, "runtime_evidence_id", None)
        if evidence_action:
            store = self._runtime_evidence_store()
            if evidence_action == "list":
                if evidence_id:
                    self.die("--runtime-evidence list does not accept --runtime-evidence-id")
                for entry in store.entries():
                    manifest = store.manifest(entry)
                    diagnostics = manifest.get("diagnostics", [])
                    cause = (
                        diagnostics[-1].get("summary", "-")
                        if diagnostics and isinstance(diagnostics[-1], dict)
                        else manifest.get("failure", {}).get("message", "-")
                    )
                    self.inf(
                        f"{entry.name}\t{manifest.get('label', '-')}\t"
                        f"{cause}"
                    )
            else:
                if not evidence_id:
                    self.die(
                        f"--runtime-evidence {evidence_action} requires --runtime-evidence-id"
                    )
                payload = (
                    store.manifest(store.resolve(evidence_id))
                    if evidence_action == "show"
                    else store.replay_report(evidence_id)
                )
                self.inf(json.dumps(payload, indent=2, sort_keys=True))
            return
        if evidence_id:
            self.die("--runtime-evidence-id requires --runtime-evidence show or replay")
        
        if args.dry_run and not args.gc:
            # --dry-run only plans --gc. Everywhere else it was silently ignored
            # and the selected tests ran, which is how a second prefix-backed run
            # was started while one was already in flight.
            self.die(
                "--dry-run only plans what --gc would prune; without --gc it would "
                "start the selected run. Pass --gc to plan a prune, or drop "
                "--dry-run to run the selection."
            )
        if getattr(args, "gc_runtime_build_cache", False) and not args.gc:
            self.die("--gc-runtime-build-cache requires --gc")
        if getattr(args, "runtime_build_cache_root", None) and not (
            args.gc and getattr(args, "gc_runtime_build_cache", False)
        ):
            self.die("--runtime-build-cache-root requires --gc --gc-runtime-build-cache")
        
        if getattr(args, "cleanup_prefix", False):
            incompatible = []
            if args.workload or args.env or args.label or args.changed or args.bead or args.submodule:
                incompatible.append("test selection")
            if args.list:
                incompatible.append("metadata/list mode")
            if args.keep_prefix_running:
                incompatible.append("keep-running mode")
            if incompatible:
                self.die(
                    "--cleanup-prefix is a lifecycle operation; do not combine it with "
                    + ", ".join(incompatible)
                )
            if not self._prefix:
                self.die("--cleanup-prefix requires --prefix or DPREFIX")
            with self._prefix_lifecycle_owner().locked(self._prefix):
                if not self._shutdown_test_prefix():
                    self.die(f"could not cleanly shutdown Darling prefix: {self._prefix}")
            return
        
        if args.gc:
            # A maintenance pass acts only inside its own root. With a declared
            # state root, a flag that points elsewhere is refused rather than
            # obeyed: reaching another lane's state is the failure the shared
            # roots produced, and it is silent until something is already gone.
            declared = state_root()
            maintenance_roots = [
                ("bundle", args.bundle_root),
                ("proof scratch", args.proof_scratch_root),
                ("runtime evidence", self._runtime_evidence_root),
            ]
            runtime_cache_root = None
            if getattr(args, "gc_runtime_build_cache", False):
                try:
                    runtime_cache_root = test_runtime_cache.maintenance_root(
                        Path(self.manifest.repo_abspath),
                        os.environ,
                        getattr(args, "runtime_build_cache_root", None),
                    )
                except ValueError as error:
                    self.die(str(error))
                maintenance_roots.append(("runtime build cache", runtime_cache_root))
            for label, root in maintenance_roots:
                if not inside_state_root(Path(root)):
                    self.die(
                        f"{label} root {Path(root).expanduser()} is outside the declared "
                        f"state root {declared} ({STATE_ROOT_ENV}); a maintenance pass "
                        "only acts inside its own root"
                    )
            if runtime_cache_root is not None:
                try:
                    result = test_runtime_cache.gc(
                        runtime_cache_root,
                        dry_run=args.dry_run,
                        progress=self.inf,
                    )
                except (OSError, ValueError, RuntimeError) as error:
                    self.die(f"runtime build cache GC failed: {error}")
                if result["errors"]:
                    self.die("runtime build cache GC failed; see per-entry diagnostics above")
            self._gc_bundles(
                Path(args.bundle_root), args.keep_last, args.max_bundle_mb,
                dry_run=args.dry_run,
            )
            self._gc_runtime_proof_scratch(
                Path(args.proof_scratch_root),
                args.proof_scratch_max_age_hours,
                args.proof_scratch_keep_last,
                dry_run=args.dry_run,
            )
            # Source-proof scratch can contain detached Git worktrees. The plan
            # reports the registrations the real run would prune, so the two
            # agree; only the real run removes them.
            self._gc_west_temp_worktree_registrations(dry_run=args.dry_run)
            self._gc_guest_runner_output(
                Path(args.proof_scratch_root),
                args.proof_scratch_max_age_hours,
                dry_run=args.dry_run,
            )
            if getattr(args, "gc_runtime_evidence", False):
                evidence_store = self._runtime_evidence_store()
                entries = evidence_store.gc(
                    max_age_hours=args.proof_scratch_max_age_hours,
                    keep_last=args.proof_scratch_keep_last,
                    dry_run=args.dry_run,
                    progress=self.inf,
                )
                verb = "would prune" if args.dry_run else "pruned"
                for entry in entries:
                    self.inf(f"{verb} runtime evidence: {entry}")
                # A name is not ownership. A published-name directory with no
                # manifest and no unit marker is reported with the size it holds
                # instead of being deleted or silently ignored.
                for entry in evidence_store.unowned_units():
                    self.inf(
                        "left alone (runtime evidence with no manifest or unit marker): "
                        f"{entry} ({self._format_size(self._dir_size(entry))})"
                    )
                # Runtime evidence GC can remove the last directory reference
                # to a source worktree. Prune its now-stale Git metadata too.
                self._gc_west_temp_worktree_registrations(dry_run=args.dry_run)
            return
        
        if getattr(args, "gc_runtime_evidence", False):
            self.die("--gc-runtime-evidence requires --gc")
        try:
            validate_cli_selection(args)
        except ValueError as error:
            self.die(str(error))
        if args.workload or args.guest_macho_validation_group:
            if args.changed or args.submodule or args.fuzz or args.stress:
                self.die('--workload cannot be combined with CTest source/fuzz/stress selectors')
            try:
                catalogue = load_workloads(self._testkit_dir() / 'workloads.yml')
                selection = select_workload_tests(
                    catalogue, workload=args.workload, bead=args.bead, env=args.env,
                    diag=args.diag, label=args.label,
                    validation_group=args.guest_macho_validation_group,
                    resolved_diag=self._resolved_diag, defer_ctest=True,
                )
            except (OSError, ValueError) as error:
                self.die(str(error))
            selected = selection.selected
            if not selected:
                self.die('workload selectors matched no applicable declarations')
            if not args.list:
                for owner, test in selected:
                    if test.get("requires-build-features") or test.get("requires-installed-paths"):
                        problems = installed_requirement_problems(test, self._installed_runtime())
                        if problems:
                            self.die(f"{owner['path']}: installed artifact prerequisites: " + "; ".join(problems))
            if args.guest_macho_validation_group:
                try:
                    validate_selected_group(selected, args.guest_macho_validation_group)
                except ValueError as error:
                    self.die(str(error))
                self._guest_macho_evidence_dir = Path(args.guest_macho_evidence_dir) if args.guest_macho_evidence_dir else None
            diagnostics = [test for _, test in selected if test.get('runner') == 'diagnostic']
            if diagnostics:
                if len(selected) != 1 or unknown or args.keep_prefix_running:
                    self.die('exact-capture requires an exclusive workload selection and owned shutdown')
                if args.list:
                    self.inf('exact-capture: live guest image, matching core, registers and prefix cleanup')
                    return
                if not self._prefix:
                    self.die('exact-capture requires --prefix or DPREFIX')
                from test_diagnostics import run_exact_capture
                raise SystemExit(run_exact_capture(self))
            with self._metadata_ctest_selection(selected, env=args.env, diag=args.diag, label=args.label) as selected:
                needs_prefix = not args.list and self._metadata_needs_prefix(selected)
                with self._prefix_resource_context(needs_prefix):
                    result = self._run_metadata_tests(selected, args.list, unknown)
                if getattr(self, '_guest_macho_evidence_dir', None):
                    result |= finalize_guest_macho_evidence(self._guest_macho_evidence_dir, args.guest_macho_validation_group, [test['fixture'] for _, test in selected])
                if getattr(self, '_prefix_cleanup_failed', False):
                    result = result or 1
                raise SystemExit(result)
        testkit = self._testkit_dir()
        if not testkit.exists():
            self.die(f'no testkit at {testkit}')
        launcher = self._resolve_darling_launcher(self._prefix)
        if args.env == 'darling' and not args.list:
            if not self._prefix:
                self.die('env:darling requires --prefix or DPREFIX, separate from --runtime-root')
            if not launcher:
                self.die('env:darling requires a normal installed Darling runtime; pass --runtime-root or DARLING_RUNTIME_ROOT')
        build = self._configure_and_build(
            testkit, self._executor, darling_launcher=launcher, prefix=self._prefix,
            bundle_root=self._bundle_root, compile_tests=not args.list,
        )
        changed = self._changed_submodules() if args.changed else None
        if args.changed and not changed:
            self.inf('no changed submodules; nothing selected by --changed')
            return
        label_args = ctest_selector_label_args(
            bead=args.bead, env=None, diag=None, label=args.label, fuzz=args.fuzz,
            stress=args.stress, changed_submodules=changed, submodules=args.submodule,
        )
        groups = self._selected_ctest_groups(build, label_args, unknown, env=args.env, diag=args.diag)
        needs_prefix = not args.list and any(group['darling'] for group in groups)
        if needs_prefix and not self._prefix:
            self.die('selected guest CTest cases require --prefix or DPREFIX')
        try:
            passthrough = ctest_runtime_group_passthrough(unknown)
        except ValueError as error:
            self.die(f'invalid CTest passthrough selection: {error}')
        with self._prefix_resource_context(needs_prefix) as lease_descriptor:
            if not args.list:
                self._clear_ctest_failure_record(build)
            rc = 0
            for group in groups:
                command = ctest_command(build, list_only=args.list, passthrough=[*passthrough, *ctest_index_args(group['indices'])])
                self.inf(f"running: {shell_join(command)}")
                runtime_env = self._installed_runtime().environment(self._prefix) if group['darling'] and not args.list else None
                if runtime_env is not None:
                    runtime_env["WEST_TEST_PREFIX_LEASE"] = str(self._prefix)
                    runtime_env["WEST_TEST_PREFIX_LEASE_FD"] = str(lease_descriptor)
                    runtime_env["WEST_TEST_KEEP_PREFIX_RUNNING"] = "1" if self._keep_prefix_running else "0"
                if self._source_root:
                    runtime_env = dict(os.environ if runtime_env is None else runtime_env)
                    runtime_env["DARLING_SOURCE_ROOT"] = str(self._source_root)
                result = run_bounded(
                    command, cwd=Path(self.topdir), env=runtime_env,
                    timeout_seconds=int(args.ctest_timeout_seconds),
                    pass_fds=(lease_descriptor,) if lease_descriptor is not None and group['darling'] else (),
                )
                if result.timed_out:
                    self.err(f'CTest selection timed out after {args.ctest_timeout_seconds}s')
                rc = rc or result.returncode
        if getattr(self, '_prefix_cleanup_failed', False):
            rc = rc or 1
        raise SystemExit(rc)
    

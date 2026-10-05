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

Patch metadata can point at local scripts/build targets or at CTest labels.
CTest remains the execution backend for suite-style tests; west owns patch
selection, profile materialization, resource provisioning, and diagnostics.
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
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from shlex import quote, join as shell_join

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefix_repair import (
    cleanup_prefix_mounts,
    darling_init_pid_is_usable,
    eunion_prefix_prerequisite_problems,
    guest_c_fixture_prerequisite_problems,
    prefix_boot_prerequisite_problems,
    repair_prefix_boot_prerequisites,
)
from test_ctest import (
    CtestSelectionMixin,
    ctest_command,
    ctest_selector_label_args,
    ctest_runtime_group_passthrough,
    ctest_uses_prefix,
    is_ctest_binding,
    ctest_reference_args,
    ctest_index_args,
)
from test_selection import (
    metadata_invocation_identity,
    metadata_run_summary,
    metadata_selection_plan,
    metadata_test_outcome,
    select_metadata_tests_for_command,
)
from test_dispatch import dispatch_fixture_runner
from test_cmake import archive_git_tree_to, CmakeFixtureMixin
from test_descriptor_transport import (
    DescriptorTraceError,
    window_specs as descriptor_trace_window_specs,
)
from test_execution import process_output_text, run_bounded
from fresh_prefix import create_fresh_prefix, remove_fresh_prefix
from test_guest_execution import (
    failure_phase_from_output,
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
from guest_toolchain import (
    COMMAND_LINE_TOOLS_RESOURCE,
    ensure_command_line_tools,
    GuestToolchainError,
    require_guest_toolchain_provisioning_allowed,
)
try:
    from .test_guest_c import failure_phase_from_debug_bundle, run_guest_c_fixture
except ImportError:  # Loaded as a West extension module, not a package.
    from test_guest_c import failure_phase_from_debug_bundle, run_guest_c_fixture
from profile_catalog import (
    BOOTSTRAP_RUNTIME_PROFILE_OPTION,
    PATCH_PROFILE_KIND,
    PROFILE_OPTION,
    RUNTIME_PROFILE_KIND,
    WITH_RUNTIME_PROFILE_OPTION,
    add_profile_argument,
)
from test_profile import ProfileOperationsMixin
from test_prefix import (
    cleanup_rootless_runtime_sockets,
    PrefixLifecycleMixin,
    PrefixLifecycleOwner,
    remove_stale_init_pid,
    remove_stale_server_socket,
    RootlessRuntimeSocketCleanupResult,
)
from test_resources import resource_context
from test_runtime_proof import ProofObservation, RedOracle, RuntimeProofStateMachine
from test_runtime_deploy import RuntimeDeploymentService
from test_results import InvocationResult, RuntimeBuildFailure, RuntimeRedProven
from test_runtime_build import RuntimeBuildService
from test_runtime_evidence import RuntimeEvidenceStore
from test_runtime_source import RuntimeSourceMaterializer
from test_runtime_identity import runtime_identity
import test_stock_stack_cache
import test_verdict_cache
from guest_macho_validation import (
    add_cli_arguments,
    capture_invocation,
    finalize_guest_macho_evidence,
    validate_cli_selection,
    validate_selected_group,
)
from test_runtime import (
    applicability_preflight_advice,
    compose_ctest_runtime_profiles,
    MANIFEST_SOURCE_MODE,
    merge_runtime_cmake_define_overrides,
    parse_runtime_cmake_define_overrides,
    preflight_retry_allowed,
    RuntimePlanMixin,
)
from test_worktrees import prunable_west_temp_worktrees, prune_stale_west_temp_worktrees
from test_store import (
    STATE_ROOT_ENV,
    inside_state_root,
    scratch_owner,
    state_root,
    state_subdir,
)
from test_bootstrap import (
    BootstrapRuntimeProfileMixin,
    RuntimeProfileDeployment,
    RuntimeProviderFailure,
    # Kept in this namespace on purpose: focused contracts read the facade's
    # ``test.RETAINED_RUNTIME_PROFILE_MARKER`` to build a retained prefix.
    RETAINED_RUNTIME_PROFILE_MARKER,
    bootstrap_syscall_stall_summary,
    bootstrap_trace_fatal_signal,
)


# A debug bundle is a timestamp-named directory, <YYYYMMDDTHHMMSSZ>-<name>.
# The GC bundle pass selects by this shape and by nothing else: selecting by
# age and count over every directory under the root made unrelated state
# eligible, including a west dev job directory and another workstream's
# experiment root.
BUNDLE_NAME = re.compile(r"^[0-9]{8}T[0-9]{6}Z-")


class DarlingTest(
    ProfileOperationsMixin,
    BootstrapRuntimeProfileMixin,
    PrefixLifecycleMixin,
    CtestSelectionMixin,
    CmakeFixtureMixin,
    RuntimePlanMixin,
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
        parser.add_argument("--diagnostic", choices=("exact-capture",),
                            help="run a managed live diagnostic acceptance scenario")
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
        add_profile_argument(
            parser,
            PROFILE_OPTION,
            PATCH_PROFILE_KIND,
            metavar="NAME",
            help="run tests declared by a patch profile's patches.yml metadata",
        )
        parser.add_argument(
            "--patch",
            metavar="PATH",
            help="run tests declared for one patch path in patches.yml metadata",
        )
        parser.add_argument(
            "--red-only",
            action="store_true",
            help="with --profile/--patch, select only tests marked red: true",
        )
        parser.add_argument(
            "--prove-red",
            action="store_true",
            help="with --profile/--patch, run RED proof mode; normal runs still expect GREEN on current checkout",
        )
        parser.add_argument(
            "--red-audit",
            action="store_true",
            help="with --profile, list patches missing tests or test-exception",
        )
        parser.add_argument(
            "--env",
            choices=("host", "darling", "macos"),
            help="restrict to one environment",
        )
        parser.add_argument(
            "--prefix",
            metavar="PATH",
            help="Darling prefix for guest tests; accepts PATH or existing:PATH",
        )
        parser.add_argument(
            "--prefix-profile",
            metavar="NAME",
            help="named Darling prefix shortcut (homebrew -> ~/work/darling-prefix-homebrew-test)",
        )
        parser.add_argument(
            "--fresh-prefix-from",
            metavar="BASELINE",
            help="copy BASELINE into an isolated disposable prefix for this test run",
        )
        add_profile_argument(
            parser,
            WITH_RUNTIME_PROFILE_OPTION,
            RUNTIME_PROFILE_KIND,
            action="append",
            default=[],
            metavar="NAME",
            help="add a declared CTest guest runtime provider without changing test selection; useful for reproducing artifact interactions",
        )
        parser.add_argument(
            "--runtime-cmake-define",
            action="append",
            default=[],
            metavar="NAME=VALUE",
            help="override one feature CMake definition for a disposable runtime deployment",
        )
        add_profile_argument(
            parser,
            BOOTSTRAP_RUNTIME_PROFILE_OPTION,
            RUNTIME_PROFILE_KIND,
            metavar="NAME",
            help="with --prefix, --prefix-profile, or DPREFIX: build and retain one declared runtime provider as the selected prefix baseline, then prove it with a bounded guest smoke",
        )
        parser.add_argument(
            "--reuse-prefix-runtime",
            action="store_true",
            help="with --profile and --prefix, run metadata guest tests against the provider retained by an earlier bootstrap",
        )
        parser.add_argument(
            "--bootstrap-syscall-trace",
            metavar="DIR",
            help="save strace -ff output for a bounded runtime bootstrap in DIR",
        )
        parser.add_argument(
            "--bootstrap-stack-sample",
            metavar="DIR",
            help="with --bootstrap-runtime-profile, save a low-overhead perf stack sample for the bounded guest smoke in DIR",
        )
        parser.add_argument(
            "--bootstrap-timeout-seconds",
            type=int,
            metavar="SECONDS",
            help="override the bounded runtime-bootstrap deadline (E-UNION default: 15; maximum: 600)",
        )
        parser.add_argument(
            "--runtime-build-timeout-seconds",
            type=int,
            metavar="SECONDS",
            help="override the per-phase runtime source/build deadline (useful for bounded diagnostics)",
        )
        parser.add_argument(
            "--bootstrap-executable",
            metavar="GUEST_PATH",
            help="with --bootstrap-runtime-profile, run one absolute guest executable instead of the default login-shell verdict",
        )
        parser.add_argument(
            "--keep-prefix-running",
            action="store_true",
            help="do not shut down a Darling prefix after prefix-backed metadata tests",
        )
        parser.add_argument(
            "--no-overlayfs",
            action="store_true",
            help="run Darling prefix tests with DARLING_NOOVERLAYFS=1",
        )
        parser.add_argument(
            "--materialize-profile",
            action="store_true",
            help="run profile metadata tests from temporary worktrees built from manifest revisions plus patch files",
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
            help="prune old debug bundles (keep-last + size cap) and exit",
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
            help="with --gc, directory to scan for stale runtime, source-proof, and "
            "deploy-proof scratch plus guest runner output (default DW_STATE_ROOT/scratch, "
            f"otherwise {tempfile.gettempdir()}). Runtime evidence units are scoped "
            "separately by --runtime-evidence-root",
        )
        parser.add_argument(
            "--runtime-evidence-root",
            metavar="DIR",
            help="durable root for failed runtime source/build evidence units "
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
            "--proof-scratch-max-age-hours",
            type=float,
            default=24.0,
            metavar="HOURS",
            help="with --gc, prune stale west runtime/source-profile scratch dirs older than this "
            "(default 24)",
        )
        parser.add_argument(
            "--proof-scratch-keep-last",
            type=int,
            default=2,
            metavar="N",
            help="with --gc, keep at most N newest west runtime/source-profile scratch dirs "
            "regardless of age (default 2)",
        )
        return parser

    # --- helpers ------------------------------------------------------------

    def _testkit_dir(self) -> Path:
        return Path(self.manifest.repo_abspath) / "testkit"

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

    def _require_runtime_scratch_space(self, deployment_name: str) -> None:
        configured_minimum = os.environ.get("WEST_RUNTIME_MIN_FREE_BYTES", str(8 * 1024**3))
        try:
            minimum = int(configured_minimum)
        except ValueError:
            self.die(
                "WEST_RUNTIME_MIN_FREE_BYTES must be an integer number of bytes "
                "greater than or equal to 0"
            )
        if minimum < 0:
            self.die("WEST_RUNTIME_MIN_FREE_BYTES must be >= 0")
        scratch_root = Path(tempfile.gettempdir())
        available = shutil.disk_usage(scratch_root).free
        if available < minimum:
            self.die(
                f"Runtime deployment {deployment_name} needs at least {minimum} free bytes "
                f"under {scratch_root}, but only {available} are available; "
                "run west test --gc or free disk space before materializing the runtime source forest"
            )

    @staticmethod
    def _prematerialized_runtime_profile_is_verified(
        source_root: Path,
        workspace_root: Path,
        source_profile: str,
    ) -> bool:
        marker = workspace_root / "tier-workspace-index.json"
        if not marker.is_file() or marker.is_symlink():
            return False
        try:
            index = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return (
            index.get("schema_version") == 1
            and index.get("kind") == "west-acceptance-tier-workspace"
            and index.get("profile") == source_profile
            and source_root.parent == workspace_root
        )

    def _preflight_runtime_profile_stack(
        self, source_profile: str, deployment_name: str
    ) -> None:
        """Verify every layer of a runtime source stack before materializing it.

        Runtime forests reconstruct a profile from manifest revisions, applying
        its base profiles in order.  A broken intermediate layer otherwise
        fails only after the runner has made a large disposable forest (and may
        be mistaken for a runtime RED result).  Reuse ``west patch verify`` as
        the single authority for patch integrity and applicability, and cache
        successful stacks for the current invocation.
        """

        verified = getattr(self, "_verified_runtime_profile_stacks", set())
        if source_profile in verified:
            return
        try:
            stack = self._profile_stack(source_profile)
        except SystemExit:
            raise
        except Exception as error:
            self.die(
                f"Runtime deployment {deployment_name} cannot resolve source profile "
                f"{source_profile!r}: {error}"
            )

        timeout_seconds = getattr(self, "_runtime_build_timeout_seconds", None) or 300
        for profile in stack:
            self.inf(
                f"  runtime profile preflight: {profile} "
                f"for {deployment_name}"
            )
            attempt = 0
            while True:
                result = run_bounded(
                    [
                        "west",
                        "patch",
                        "verify",
                        "--profile",
                        profile,
                        "--applicability-only",
                    ],
                    cwd=Path(self.topdir),
                    env=None,
                    timeout_seconds=timeout_seconds,
                    capture_output=True,
                )
                output = "\n".join(
                    stream.rstrip("\n")
                    for stream in (result.stdout, result.stderr)
                    if stream
                )
                if not result.returncode or not preflight_retry_allowed(
                    attempt, output
                ):
                    break
                attempt += 1
                self.err(
                    f"  runtime profile preflight: {profile} could not reach the "
                    "mirror; retrying once"
                )
            if result.returncode:
                self._dump_command_tail(
                    f"Runtime profile {profile} preflight", result
                )
                if result.timed_out:
                    self.die(
                        f"Runtime deployment {deployment_name}: source profile "
                        f"{profile!r} applicability preflight timed out after "
                        f"{timeout_seconds}s. Inspect the verifier output or increase "
                        "--runtime-build-timeout-seconds; source validity was not "
                        "determined; this is not a runtime test result."
                    )
                self.die(
                    f"Runtime deployment {deployment_name} cannot materialize "
                    f"source profile stack {source_profile!r}: {profile!r} failed "
                    "patch applicability preflight. "
                    + applicability_preflight_advice(profile, output)
                )
        verified.add(source_profile)
        self._verified_runtime_profile_stacks = verified

    def _resolve_darling_launcher(self, prefix: str | None) -> str | None:
        if prefix:
            candidate = Path(prefix).expanduser() / "bin" / "darling"
            if candidate.exists():
                return str(candidate)
            # An explicit prefix is a runtime identity, not just an artifact
            # directory. Falling back to another prefix's launcher silently
            # mixes launcher and DPREFIX, which can make a broken named prefix
            # appear usable for one test lifecycle.
            return None
        if os.environ.get("DARLING"):
            return os.environ["DARLING"]
        if os.environ.get("DARLING_LAUNCHER"):
            return os.environ["DARLING_LAUNCHER"]
        candidate = Path("~/work/darling-prefix/bin/darling").expanduser()
        if candidate.exists():
            return str(candidate)
        return None

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

    def _manifest_revision(self, ref: str) -> str:
        for project in self.manifest.projects:
            if ref in {project.name, project.path}:
                revision = project.revision
                repo = Path(project.abspath)
                if not revision or subprocess.run(
                    ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                    cwd=repo,
                    check=False,
                ).returncode != 0:
                    self.die(
                        f"{ref}: manifest revision {revision or '<empty>'} "
                        f"is not available; run west update {project.name}"
                    )
                return revision
        self.die(f"unknown West project: {ref}")

    def _project_path(self, ref: str) -> Path:
        overrides = getattr(self, "_project_overrides", {})
        if ref in overrides:
            return overrides[ref]
        projects = self._projects()
        if ref in projects:
            return projects[ref]
        path = Path(self.topdir) / ref
        if path.exists():
            return path
        self.die(f"unknown West project or path: {ref}")

    def _test_invocation(self, patch, test):
        """Resolve structured patch metadata to a concrete local invocation.

        `command` is intentionally still supported as an escape hatch, but the
        common cases should be structured so west owns how tests are launched.
        """
        proof = test.get("red-proof") if isinstance(test.get("red-proof"), dict) else {}
        source_env = test.get("source-env") or proof.get("source-env")
        source_module = proof.get("source-module", patch["module"])
        if test.get("command"):
            return {
                "key": f"shell:{test['command']}",
                "display": test["command"],
                "cwd": Path(self.topdir),
                "args": test["command"],
                "shell": True,
                "requires_profile": test.get("requires-profile"),
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
                "ctest_index": selection.get("index"),
                "ctest_directory": selection.get("directory"),
                "ctest_env": test.get("env"),
                "ctest_source_override": test.get("ctest-source-override"),
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
                "source_env": source_env,
                "source_module": source_module,
            }
        if runner in {
            "script",
            "source-contract-script",
            "source-profile-script",
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
                "source-profile-script",
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
                "env": env,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
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
                "env": env,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
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
                "source_script": source_script,
                "cases": cases,
                "source_root_env": source_env,
                "source_env": source_env,
                "source_module": source_module,
                "requires_resources": list(test.get("requires", [])),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
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
                "requires_profile": test.get("requires-profile"),
                "diag": self._resolved_diag(test),
                "name": test.get("name", patch["path"]),
                "timeout_seconds": int(test.get("timeout-seconds", 600)),
                "host_trace_files": list(test.get("host-trace-files", [])),
            }

        if runner == "guest-macho-fixture":
            if test.get("runtime-profile"):
                self.die(
                    f"{patch['path']}: guest-macho-fixture cannot declare a runtime-profile"
                )
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
                "runtime_profile": test.get("runtime-profile"),
                "source_profile": test.get("source-profile"),
                "validation_group": test.get("validation-group"),
                "requires_resources": sorted(resources),
                "requires_env": list(test.get("requires-env", [])),
                "requires_profile": test.get("requires-profile"),
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

        if test.get("runtime-profile") or test.get("_ctest", {}).get("profiles"):
            return True
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

    def _metadata_runtime_identity(self, test, patch):
        """Return the runtime identity one metadata test runs against.

        A test that deploys a typed runtime provider is keyed on the same
        identity the runtime reuse cache computes for that provider, plus the
        proof document the deployment builds from - an override that changes the
        built runtime has to change the key. A guest test without a provider is
        keyed on the runtime the prefix retains; when the prefix identifies no
        runtime the test cannot be keyed, because two prefixes are then not
        known to be the same runtime.
        """

        profiles = list(dict.fromkeys([
            *([test["runtime-profile"]] if test.get("runtime-profile") else []),
            *test.get("_ctest", {}).get("profiles", []),
        ]))
        prefix_text = getattr(self, "_prefix", None)
        if profiles:
            try:
                definition = compose_ctest_runtime_profiles(
                    self._ctest_runtime_profile_definitions(), profiles
                )
            except ValueError:
                return None, None
            return (
                definition,
                {
                    "profile": definition["name"],
                    "identity": runtime_identity(
                        topdir=Path(self.topdir),
                        manifest_repo=Path(self.manifest.repo_abspath),
                        profile_name=definition["name"],
                        definition=definition,
                        launcher=Path(prefix_text) / "bin" / "darling",
                    ),
                    "proof": self._runtime_profile_proof(
                        definition,
                        omit_patch=False,
                        patch=patch,
                        label_prefix=f"metadata verdict {patch['path']}",
                    ),
                },
            )
        if not prefix_text:
            return None, None
        digest = test_stock_stack_cache.runtime_identity_digest(
            prefix=Path(prefix_text),
            manifest_repo=Path(self.manifest.repo_abspath),
            topdir=Path(self.topdir),
            profile_name=None,
        )
        if digest is None:
            return None, None
        return None, {"profile": None, "identity-digest": digest}

    def _metadata_verdict_identity(self, patch, test, invocation):
        """Return the identity that decides whether this verdict may be reused.

        ``None`` means the verdict cannot be keyed and the test is executed for
        real. That is the honest answer for a test whose result depends on state
        no identity input describes: a RED arm is an experiment about flake, a
        guest Mach-O validation group publishes evidence this run has to
        produce, a clean-shutdown test observes live prefix state, and a guest
        test whose prefix identifies no runtime has no runtime identity to key
        on.
        """

        if (
            test.get("validation-group")
            or test.get("verify-clean-shutdown")
            or test.get("red")
            or test.get("red-proof")
        ):
            return None
        needs_guest = self._metadata_invocation_needs_guest(test, invocation)
        if needs_guest and not getattr(self, "_prefix", None):
            return None
        if needs_guest:
            definition, runtime = self._metadata_runtime_identity(test, patch)
            if runtime is None:
                return None
        else:
            definition, runtime = None, None
        stack = None
        if test_verdict_cache.STACK_RESOURCE in set(
            invocation.get("requires_resources", ())
        ):
            # Ask under the name the prefix retains. The marker names the profile
            # that provisioned the prefix, and a deployed ring profile never
            # matches it, so naming the deployed profile returned no identity and
            # every test that consumes the stock stack was unkeyable - the exact
            # tests the reuse exists for. A prefix that retains nothing falls back
            # to the deployed name, which computes the identity from the source.
            retained = test_stock_stack_cache.retained_profile_name(
                Path(getattr(self, "_prefix"))
            )
            stack = test_stock_stack_cache.stack_request(
                prefix=Path(getattr(self, "_prefix")),
                manifest_repo=Path(self.manifest.repo_abspath),
                topdir=Path(self.topdir),
                profile_name=retained
                or (None if definition is None else definition["name"]),
                environ=os.environ,
            )
            if stack is None:
                return None
        prefix_text = getattr(self, "_prefix", None)
        return test_verdict_cache.verdict_identity(
            test=test,
            invocation=invocation,
            runtime=runtime,
            stack=stack,
            toolchain=(
                None
                if not prefix_text
                else test_stock_stack_cache.guest_toolchain_identity(
                    Path(prefix_text), os.environ
                )
            ),
            environ=os.environ,
        )

    def _run_metadata_tests(self, tests, list_only: bool, unknown: list[str]) -> int:
        if unknown:
            self.die("metadata command tests do not accept raw ctest passthrough arguments")
        self._prune_stale_west_temp_worktrees()
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
            if test.get("_ctest"):
                self.inf(
                    f"  registration: {test['_ctest']['name']}; "
                    f"runtime profiles: {', '.join(test['_ctest']['profiles']) or 'none'}; "
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
            identity = metadata_invocation_identity(invocation, test)
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
            with self._required_profile_context(patch, invocation):
                with self._metadata_runtime_profile_context(patch, test) as deployment:
                    runtime_env = deployment.env if deployment is not None else None
                    runtime_invocation = self._with_runtime_diagnostics(
                        invocation, deployment
                    )
                    proof = test.get("red-proof")
                    if (
                        isinstance(proof, dict)
                        and proof.get("mode") == "guest-runtime-deploy"
                        and not test.get("runtime-profile")
                    ):
                        result_rc = self._run_guest_runtime_deploy_green(patch, proof, invocation)
                    else:
                        exec_env = self._runtime_profile_execution_env(
                            runtime_invocation, runtime_env
                        )
                        with self._ctest_source_override_context(runtime_invocation) as run_invocation:
                            with self._resource_context(run_invocation, exec_env) as resource_env:
                                if getattr(self, "_guest_macho_evidence_dir", None):
                                    result_rc = capture_invocation(
                                        self, run_invocation, resource_env,
                                        self._guest_macho_evidence_dir,
                                    )
                                else:
                                    result_rc = self._run_invocation(
                                        run_invocation, env=resource_env
                                    )
                    if result_rc == 0 and test.get("verify-clean-shutdown"):
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
                    provenance={"patch": patch["path"], "test": invocation["name"]},
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
        for patch, test in tests:
            if test.get("runtime-profile"):
                return True
            invocation = self._test_invocation(patch, test)
            resources = set(invocation.get("requires_resources", []))
            if resources & {"darling-prefix", "darling-eunion-prefix"}:
                return True
        return False

    def _prune_stale_west_temp_worktrees(self) -> None:
        projects = getattr(self.manifest, "projects", [])
        repos = [
            Path(project.abspath)
            for project in projects
            if getattr(project, "name", None) != "manifest"
        ]
        pruned = prune_stale_west_temp_worktrees(repos)
        if pruned:
            self.inf(
                f"  pruned stale west temp worktree metadata: {len(pruned)} entry(s)"
            )

    def _metadata_needs_profile_worktree(self, tests) -> bool:
        for patch, test in tests:
            invocation = self._test_invocation(patch, test)
            required = invocation.get("requires_profile")
            if required and not self._profile_is_applied(required):
                return True
            # A source-bound host test must execute against the selected patch
            # profile. Without a temporary profile worktree its source env
            # resolves to whatever branch happens to be checked out locally.
            active_profile = getattr(self, "_active_profile", None)
            source_module = invocation.get("source_module")
            source_project = self._project_path(source_module) if source_module else None
            if (
                invocation.get("source_env")
                and active_profile
                and source_module in self._profile_stack_modules(active_profile)
                and source_project != Path(self.manifest.repo_abspath)
                and not self._profile_is_applied(active_profile)
            ):
                return True
            script_path = invocation.get("script_path")
            if script_path is not None and not script_path.is_file():
                return True
        return False

    @contextmanager
    def _metadata_runtime_profile_context(
        self, patch, test, *, omit_patch=False, red_proof=None
    ):
        """Temporarily deploy the typed runtime provider declared by metadata."""

        profiles = list(dict.fromkeys([
            *([test["runtime-profile"]] if test.get("runtime-profile") else []),
            *test.get("_ctest", {}).get("profiles", []),
        ]))
        if not profiles:
            yield None
            return
        if getattr(self, "_reuse_prefix_runtime", False):
            if len(profiles) != 1:
                self.die("--reuse-prefix-runtime requires exactly one selected runtime profile")
            if omit_patch:
                self.die(
                    "--reuse-prefix-runtime cannot be used for RED proofs; "
                    "RED requires an isolated runtime deployment"
                )
            yield self._retained_runtime_profile(profiles[0])
            return
        label = f"metadata {patch['path']}:{test.get('name', patch['path'])}"
        deployment_args = {
            "label_prefix": label,
            "retain_deployment": False,
            "patch": patch,
            "omit_patch": omit_patch,
        }
        if red_proof is not None:
            deployment_args["red_proof"] = red_proof
        with self._runtime_profile_deployment_context(
            profiles,
            **deployment_args,
        ) as deployment:
            yield deployment

    @staticmethod
    def _with_runtime_diagnostics(invocation, deployment):
        """Bind provider-owned trace files to the invocation they diagnose."""

        if deployment is None or not deployment.diagnostic_trace_paths:
            return invocation
        configured = dict(invocation)
        configured["_runtime_diagnostic_trace_paths"] = deployment.diagnostic_trace_paths
        return configured

    def _runtime_profile_execution_env(self, invocation, runtime_env):
        """Combine runner defaults with the selected provider's launcher contract."""
        env = self._execution_env(invocation)
        if runtime_env is None:
            return env
        merged = dict(env or {})
        merged.update(runtime_env)
        return merged

    def _bootstrap_diagnostics_enabled(self) -> bool:
        return (
            getattr(self, "_bootstrap_syscall_trace", None) is not None
            or getattr(self, "_bootstrap_stack_sample", None) is not None
        )

    def _runtime_profile_proof(
        self, definition, *, omit_patch, patch, label_prefix, red_proof=None
    ) -> dict:
        """Return the deployment inputs that decide which runtime is built.

        The deployment builds a runtime from this document and the verdict cache
        keys on it, so an input that reaches one of them and not the other would
        let a verdict recorded against one runtime be reused for another. The
        document is built here once and both callers use it.
        """

        try:
            cmake_defines = merge_runtime_cmake_define_overrides(
                definition.get("cmake-defines", {}),
                getattr(self, "_runtime_cmake_define_overrides", {}),
            )
        except ValueError as error:
            self.die(f"invalid runtime CMake override: {error}")
        proof = {
            "source-modules": definition["source-modules"],
            "runtime-artifacts": definition["runtime-artifacts"],
            "cmake-defines": cmake_defines,
        }
        if definition.get("compiler-launcher") is not None:
            proof["compiler-launcher"] = definition["compiler-launcher"]
        if definition.get("runtime-mode") is not None:
            proof["runtime-mode"] = definition["runtime-mode"]
        if omit_patch:
            proof["bad-profile"] = "current-minus-patch"
            if isinstance(red_proof, dict):
                for key in ("source-revision", "current-minus-skip-patches"):
                    if key in red_proof:
                        proof[key] = red_proof[key]
        proof["launcher-env"] = {
            key: str(value)
            for key, value in definition.get("launcher-env", {}).items()
        }
        if omit_patch and patch is None:
            self.die(
                f"{label_prefix} runtime profile cannot omit a patch "
                "without patch metadata"
            )
        return proof

    @contextmanager
    def _runtime_profile_deployment_context(
        self,
        profiles: list[str],
        *,
        label_prefix: str,
        retain_deployment: bool,
        provision_guest_toolchain: bool = True,
        patch=None,
        omit_patch=False,
        red_proof=None,
    ):
        """Materialize, build, and deploy a declared runtime provider.

        CTest uses the default transactional form and restores the prefix after
        each selected group.  Explicit prefix bootstrap uses the same provider
        plan but commits its deployment only after its caller's guest smoke
        succeeds.  Keeping both paths here prevents bootstrap from becoming a
        second, ad-hoc build/deploy implementation.
        """

        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{label_prefix} runtime profile requires a Darling prefix")
        try:
            definition = compose_ctest_runtime_profiles(
                self._ctest_runtime_profile_definitions(), profiles
            )
        except ValueError as error:
            self.die(f"invalid CTest runtime profile selection: {error}")
        assert definition is not None
        profile_name = definition["name"]
        source_profile = definition["source-profile"]
        manifest_source = definition.get("source-mode") == MANIFEST_SOURCE_MODE
        proof = self._runtime_profile_proof(
            definition,
            omit_patch=omit_patch,
            red_proof=red_proof,
            patch=patch,
            label_prefix=label_prefix,
        )
        launcher_env = {
            key: str(value)
            for key, value in definition.get("launcher-env", {}).items()
        }
        anchor = patch or {
            "path": f"{label_prefix} runtime profile {profile_name}",
            "module": definition["source-module"],
        }
        previous_profile = getattr(self, "_active_profile", None)
        prematerialized_source: Path | None = None
        prematerialized_profile_verified = False
        raw_prematerialized_source = os.environ.get(
            "WEST_PREMATERIALIZED_RUNTIME_SOURCE_ROOT"
        )
        if raw_prematerialized_source:
            prematerialized_source = Path(raw_prematerialized_source)
            raw_workspace_lock = os.environ.get("WEST_MATERIALIZED_WORKSPACE_LOCK")
            workspace_root = (
                Path(raw_workspace_lock).parent if raw_workspace_lock else None
            )
            if omit_patch:
                self.die(
                    f"{label_prefix} cannot use a prematerialized source for RED"
                )
            if (
                workspace_root is None
                or not workspace_root.is_absolute()
                or workspace_root not in prematerialized_source.parents
                or not prematerialized_source.is_absolute()
                or prematerialized_source.is_symlink()
                or not prematerialized_source.is_dir()
                or not (prematerialized_source / ".git").exists()
            ):
                self.die(
                    f"{label_prefix} prematerialized runtime source is invalid: "
                    f"{prematerialized_source}"
                )
            prematerialized_profile_verified = (
                self._prematerialized_runtime_profile_is_verified(
                    prematerialized_source,
                    workspace_root,
                    source_profile,
                )
            )
        else:
            self._require_runtime_scratch_space(
                f"{label_prefix} profile {profile_name}"
            )
        if manifest_source:
            # A manifest-native provider never touches the patch/profile stack: no `west patch verify`, no lock-first
            # plan, no immutable-mirror probe, no disposable patched forest. Its source precondition is instead that
            # the workspace IS the manifest's product source, checked in _manifest_runtime_source_root before any
            # build. Skipping this call is the migration invariant: a manifest provider must succeed with no
            # source-bundles/ directory at all.
            self.inf(
                f"  runtime source mode: manifest (no patch/profile preflight) "
                f"for {label_prefix} profile {profile_name}"
            )
        elif prematerialized_profile_verified:
            self.inf(
                f"  runtime profile preflight reuse: {source_profile} "
                f"for {label_prefix} profile {profile_name}"
            )
        else:
            self._preflight_runtime_profile_stack(
                source_profile, f"{label_prefix} profile {profile_name}"
            )
        evidence_store = self._runtime_evidence_store()
        evidence = evidence_store.start(
            f"{label_prefix} runtime profile {profile_name}",
            {"provider": profile_name, "source-profile": source_profile}
            if not manifest_source
            else {
                "provider": profile_name,
                "source-mode": MANIFEST_SOURCE_MODE,
                "source-module": definition["source-module"],
            },
        )
        scratch = evidence.directory
        evidence_failure = None
        previous_evidence = getattr(self, "_active_runtime_evidence", None)
        self._active_runtime_evidence = evidence
        self._active_profile = None if manifest_source else source_profile
        try:
            self.inf(f"{label_prefix} runtime profile: {profile_name} ({source_profile})")
            reuse_plan = None
            if manifest_source:
                # The workspace IS the source: no reuse store (a cached build tree is exactly the class of stale
                # artifact this mode must not serve) and no patched forest.
                source_context = self._manifest_runtime_source_root(
                    definition, anchor, evidence
                )
            elif prematerialized_source is not None:
                self.inf(
                    f"{label_prefix} prematerialized runtime source: "
                    f"{prematerialized_source}"
                )
                source_context = nullcontext(prematerialized_source)
            else:
                reuse_plan = self._runtime_reuse_plan(
                    profile_name=profile_name,
                    definition=definition,
                    proof=proof,
                    patch=anchor,
                    prefix_text=prefix_text,
                    omit_patch=omit_patch,
                )
                if reuse_plan is not None:
                    self.inf(
                        f"  runtime reuse plan {label_prefix} {profile_name}: "
                        f"{reuse_plan.report()}"
                    )
                    self._bound_runtime_reuse_store(reuse_plan)
                source_context = self._guest_runtime_source_forest(
                    anchor,
                    proof,
                    omit_patch=omit_patch,
                    root=(
                        reuse_plan.source_entry
                        if reuse_plan is not None
                        else evidence.source_root
                    ),
                    evidence_session=evidence,
                    reuse_key=reuse_plan.source_key if reuse_plan is not None else None,
                )
            with source_context as source_root:
                build_root = self._runtime_red_build_artifacts(
                    source_root,
                    proof,
                    Path(prefix_text),
                    Path(scratch),
                    label=f"{label_prefix} {profile_name}",
                    cache=reuse_plan.build_cache if reuse_plan is not None else None,
                    on_reuse=reuse_plan.record_build if reuse_plan is not None else None,
                )
                if reuse_plan is not None:
                    self.inf(
                        f"  runtime {label_prefix} {profile_name} "
                        f"{reuse_plan.report()}"
                    )
                with self._runtime_red_deployed_artifacts(
                    proof,
                    build_root,
                    Path(prefix_text),
                    label=f"{label_prefix} {profile_name}",
                    restore_deployment=not retain_deployment,
                ):
                    runtime_env = os.environ.copy()
                    diagnostic_trace_paths: tuple[Path, ...] = ()
                    runtime_env.update(self._darling_prefix_env(prefix_text))
                    runtime_env.update(launcher_env)
                    runtime_launcher = Path(prefix_text) / "bin" / "darling"
                    if not runtime_launcher.is_file():
                        self.die(
                            f"{label_prefix} runtime profile {profile_name} did not deploy "
                            f"a launcher at {runtime_launcher}"
                        )
                    guest_toolchain = definition.get("guest-toolchain")
                    if (
                        provision_guest_toolchain
                        and guest_toolchain == COMMAND_LINE_TOOLS_RESOURCE
                    ):
                        try:
                            require_guest_toolchain_provisioning_allowed()
                        except GuestToolchainError as error:
                            self.die(f"{label_prefix}: {error}")
                        try:
                            self._ensure_guest_toolchain(
                                Path(prefix_text), runtime_launcher, runtime_env
                            )
                        except GuestToolchainError as error:
                            if (
                                omit_patch
                                and isinstance(red_proof, dict)
                                and red_proof.get("provider-under-test") is True
                                and error.kind == "install"
                            ):
                                raise RuntimeProviderFailure(
                                    str(error), kind=error.kind
                                ) from error
                            self.die(
                                f"guest toolchain {COMMAND_LINE_TOOLS_RESOURCE} failed: "
                                f"{error}"
                            )
                    runtime_env["DARLING"] = str(runtime_launcher)
                    runtime_env["DARLING_LAUNCHER"] = str(runtime_launcher)
                    if definition.get("bootstrap") == "rootless-no-mount":
                        if self._bootstrap_diagnostics_enabled():
                            server_trace = (
                                Path(prefix_text)
                                / "private/var/log/dserver-rpc-trace.log"
                            )
                            server_trace.parent.mkdir(parents=True, exist_ok=True)
                            server_trace.unlink(missing_ok=True)
                            runtime_env["DSERVER_TEST_TRACE_FILE"] = str(server_trace)
                            diagnostic_trace_paths = (server_trace,)
                    yield RuntimeProfileDeployment(
                        name=profile_name,
                        prefix=Path(prefix_text),
                        build_root=build_root,
                        env=runtime_env,
                        diagnostic_trace_paths=diagnostic_trace_paths,
                    )
        except BaseException as error:
            evidence_failure = error
            raise
        finally:
            self._active_profile = previous_profile
            self._active_runtime_evidence = previous_evidence
            retained = evidence_store.finish(evidence, evidence_failure)
            if retained is not None:
                self.err(f"preserved failed {label_prefix} runtime evidence: {retained}")

    def _ensure_guest_toolchain(
        self, prefix: Path, launcher: Path, env: dict[str, str]
    ) -> None:
        """Materialize profile-declared guest tools before CTest or smoke."""

        toolchain_env = dict(env)
        toolchain_env.update(self._darling_prefix_env(prefix))
        toolchain_env["DARLING"] = str(launcher)
        toolchain_env["DARLING_LAUNCHER"] = str(launcher)
        changed = ensure_command_line_tools(
            prefix=prefix,
            launcher=str(launcher),
            cwd=Path(self.topdir),
            env=toolchain_env,
            timeout_seconds=int(
                os.environ.get("WEST_GUEST_TOOLCHAIN_TIMEOUT_SECONDS", "900")
            ),
            log=self.inf,
        )
        for item in changed:
            self.inf(f"guest toolchain: {item}")

    def _bad_revision(self, patch, proof=None) -> str:
        if isinstance(proof, dict) and proof.get("source-revision"):
            return str(proof["source-revision"])
        if patch.get("source-base"):
            return patch["source-base"]
        source_commit = patch.get("source-commit")
        if not source_commit:
            self.die(f"{patch['path']}: source-base proof needs source-base or source-commit")
        return f"{source_commit}^"

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
            shutdown.extend(
                f"{key}={run_env[key]}"
                for key in ("DARLING_RUNTIME_MODE", "DARLING_ROOTLESS", "DARLING_NOOVERLAYFS", "DARLING_EUNION")
                if key in run_env
            )
            shutdown.extend([str(launcher), "shutdown"])
            args.extend(["--terminate-command", "exec " + " ".join(quote(arg) for arg in shutdown)])
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

    def _debug_bundle_output(self, bundle: Path) -> str:
        parts = []
        for name in ("stdout.log", "stderr.log", "exit-status.txt"):
            path = bundle / name
            if path.is_file():
                parts.append(path.read_text(errors="replace"))
        return "".join(parts)

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

    def _run_command_invocation(self, invocation, env=None) -> int:
        run_env = env if env is not None else invocation.get("env")
        result = run_bounded(
            self._debug_runner_args(invocation, env=run_env),
            cwd=invocation["cwd"],
            env=run_env,
            timeout_seconds=self._debug_runner_timeout_seconds(invocation),
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
            if not env_name or not rel_path:
                self.die(
                    f"{invocation['name']}: host-trace-files[{index}] needs env "
                    "and prefix-relative-path"
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
            result = run_bounded(
                args,
                cwd=source_root,
                env=run_env,
                timeout_seconds=timeout_seconds,
                capture_output=True,
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

    def _execution_env(self, invocation) -> dict[str, str] | None:
        env = invocation.get("env")
        resources = set(invocation.get("requires_resources", []))
        needs_prefix = bool(resources & {"darling-prefix", "darling-eunion-prefix"})
        source_env = invocation.get("source_env")
        if not needs_prefix and not source_env:
            return env
        merged = os.environ.copy()
        if env:
            merged.update(env)
        if source_env and not merged.get(source_env):
            source_root = self._project_path(invocation.get("source_module"))
            if source_root is not None:
                merged[source_env] = str(source_root)
        if not needs_prefix:
            return merged
        prefix = getattr(self, "_prefix", None)
        if not prefix:
            return merged
        merged.update(self._darling_prefix_env(prefix))
        if "darling-eunion-prefix" in resources:
            merged["DARLING_EUNION"] = "1"
        launcher = self._resolve_darling_launcher(prefix)
        if launcher:
            merged["DARLING"] = launcher
            merged["DARLING_LAUNCHER"] = launcher
        return merged

    @staticmethod
    def _bootstrap_prefix_advice(prefix: Path, *, guest_toolchain: bool) -> str:
        """Name the bootstrap-only invocation that makes *prefix* usable.

        ``--prefix`` resolves an existing prefix; it neither creates nor
        provisions one, and prefix-backed work fails until the operator runs
        the bootstrap provider. The baseline provider creates the launcher, and
        the provisioning provider additionally installs the reviewed guest
        CommandLineTools that guest C compilation requires.
        """

        profile = (
            "homebrew-guest-toolchain-provisioning"
            if guest_toolchain
            else "homebrew-rootless-bootstrap-minimal"
        )
        return (
            f"bootstrap this prefix first: west test --prefix {prefix} "
            f"--bootstrap-runtime-profile {profile}"
        )

    def _missing_requirements(self, invocation) -> list[str]:
        resources = set(invocation.get("requires_resources", []))
        missing = [
            env_name
            for env_name in invocation.get("requires_env", [])
            if not os.environ.get(env_name)
        ]
        if (
            resources & {"darling-prefix", "darling-eunion-prefix"}
            and not getattr(self, "_prefix", None)
        ):
            missing.append("darling-prefix (--prefix, --prefix-profile, or DPREFIX)")
        if resources & {"darling-prefix", "darling-eunion-prefix"}:
            prefix = getattr(self, "_prefix", None)
            prefix_path = Path(prefix).expanduser() if prefix else None
            needs_guest_toolchain = bool(invocation.get("guest_c_fixture"))
            launcher = self._resolve_darling_launcher(prefix)
            if not launcher:
                detail = (
                    "darling-launcher (DARLING, DARLING_LAUNCHER, "
                    "prefix/bin/darling, or ~/work/darling-prefix/bin/darling)"
                )
                if prefix_path is not None:
                    detail += (
                        f"; {prefix_path} is not bootstrapped: "
                        + self._bootstrap_prefix_advice(
                            prefix_path, guest_toolchain=needs_guest_toolchain
                        )
                    )
                missing.append(detail)
            if prefix_path is not None and needs_guest_toolchain:
                toolchain_problems = self._guest_c_fixture_prerequisite_problems(
                    prefix_path,
                    invocation.get("guest_cc", ""),
                    invocation.get("guest_cflags", ""),
                )
                if toolchain_problems:
                    missing.extend(toolchain_problems)
                    if launcher:
                        # A missing launcher already named the same command.
                        missing.append(
                            self._bootstrap_prefix_advice(
                                prefix_path, guest_toolchain=True
                            )
                        )
        return missing

    def _prefix_boot_prerequisite_problems(self, prefix: Path) -> list[str]:
        return prefix_boot_prerequisite_problems(prefix)

    def _guest_c_fixture_prerequisite_problems(
        self,
        prefix: Path,
        guest_cc: str,
        guest_cflags: str,
    ) -> list[str]:
        return guest_c_fixture_prerequisite_problems(prefix, guest_cc, guest_cflags)

    def _eunion_prefix_prerequisite_problems(self, prefix: Path) -> list[str]:
        return eunion_prefix_prerequisite_problems(prefix)

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
        source_ref = spec.get("source-ref")
        install_root_mode = str(spec.get("install-root", "guest-visible"))
        guest_env_name = str(spec.get("env", "DARLING_DYLD_DCC2_PATH"))
        enable_env_name = str(spec.get("enable-env", "DARLING_DYLD_DCC2"))
        if source_ref is not None and (not isinstance(source_ref, str) or not source_ref):
            self.die(f"{invocation['name']}: dcc-cache source-ref must be a non-empty string")
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
        host_dir = prefix / "libexec/darling" / work_rel
        guest_dir = "/" + str(work_rel)
        install_root = self._dcc_install_root(prefix, env, install_root_mode)
        with tempfile.TemporaryDirectory(prefix=f"west-dcc-cache-{invocation['name']}-") as temp:
            tempdir = Path(temp)
            source_root = self._dcc_cache_source_root(
                invocation,
                env,
                source_module,
                source_ref,
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

    def _dcc_cache_source_root(
        self,
        invocation,
        env,
        source_module: str,
        source_ref: str | None,
        tools_dir_name: str,
        tempdir: Path,
    ) -> Path:
        if source_ref:
            source_root = tempdir / "source"
            source_root.mkdir()
            repo = self._project_path(source_module)
            if repo is None:
                self.die(f"{invocation['name']}: unknown dcc-cache source module {source_module}")
            result = archive_git_tree_to(
                repo,
                source_root,
                revision=source_ref,
                paths=[tools_dir_name],
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if result.returncode:
                streams = (result.stdout, result.stderr)
                detail = "".join(
                    stream.decode(errors="replace") if isinstance(stream, bytes) else stream
                    for stream in streams
                    if stream
                )
                if detail:
                    sys.stderr.write(detail)
                self.die(
                    f"{invocation['name']}: failed to materialize DCC cache "
                    f"tools from {source_module}@{source_ref}"
                )
            return source_root

        runtime_source_root = (env or {}).get("WEST_RUNTIME_SOURCE_ROOT")
        if runtime_source_root:
            module_path = Path(source_module)
            try:
                rel = module_path.relative_to("darling")
            except ValueError:
                rel = module_path
            return Path(runtime_source_root) / rel
        return self._project_path(source_module)

    def _dcc_install_root(self, prefix: Path, env, mode: str) -> Path:
        if mode == "base":
            return prefix / "libexec/darling"
        if mode == "prefix":
            return prefix
        run_env = dict(getattr(self, "_prefix_env", {}))
        if env:
            run_env.update(env)
        if run_env.get("DARLING_NOOVERLAYFS") == "1":
            return prefix
        return prefix / "libexec/darling"

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

    def _check_requires_profile(self, patch, invocation) -> None:
        required = invocation.get("requires_profile")
        if not required:
            return
        if required in getattr(self, "_worktree_materialized_profiles", set()):
            return
        if self._profile_is_applied(required):
            return
        if getattr(self, "_materialize_profile", False):
            return
        self.die(
            f"{patch['path']}: test requires materialized patch profile {required!r}; "
            f"current checkout is not fully on integration/{required}. "
            f"Run `west patch apply --profile {required}` first, or pass "
            "`west test --materialize-profile` to switch temporarily."
        )

    @contextmanager
    def _required_profile_context(self, patch, invocation):
        required = invocation.get("requires_profile")
        if required in getattr(self, "_worktree_materialized_profiles", set()):
            yield
            return
        if not required or self._profile_is_applied(required):
            yield
            return
        if not getattr(self, "_materialize_profile", False):
            self._check_requires_profile(patch, invocation)
            yield
            return
        self.inf(f"{patch['path']}: temporarily materializing profile {required!r}")
        with self._profile_checkout(required):
            yield

    @contextmanager
    def _selected_profile_context(self, profile: str, *, list_only: bool = False):
        if list_only or not getattr(self, "_materialize_profile", False):
            yield
            return
        self.inf(f"temporarily materializing selected profile {profile!r} in worktrees")
        active = set(getattr(self, "_worktree_materialized_profiles", set()))
        active.add(profile)
        previous = getattr(self, "_worktree_materialized_profiles", set())
        self._worktree_materialized_profiles = active
        try:
            with self._profile_worktree_checkout(profile):
                yield
        finally:
            self._worktree_materialized_profiles = previous

    def _reject_guest_source_base_red_proof(self, patch) -> None:
        self.die(
            f"{patch['path']}: guest-c-fixture cannot use source-base RED proof "
            "because it would run against the already deployed Darling prefix. "
            "Use a GREEN-only guest gate or add an isolated bad/fixed deploy runner."
        )

    def _runtime_source_materializer(self) -> RuntimeSourceMaterializer:
        """Return the domain owner for disposable runtime source trees."""

        return RuntimeSourceMaterializer(self)

    # Compatibility facades for focused contracts.  Source selection and Git
    # mutations are implemented by RuntimeSourceMaterializer.
    def _red_source_patch_path(self, path: str) -> Path:
        return self._runtime_source_materializer().red_source_patch_path(path)

    def _project_manifest_path(self, ref: str) -> Path:
        return self._runtime_source_materializer().project_manifest_path(ref)

    def _apply_profile_module_patches(
        self, profile: str, module: str, target: Path, *, skip_patch_paths=None
    ) -> None:
        self._runtime_source_materializer().apply_profile_module_patches(
            profile, module, target, skip_patch_paths=skip_patch_paths
        )

    def _commit_is_ancestor(self, repo: Path, commit: str) -> bool:
        return RuntimeSourceMaterializer.commit_is_ancestor(repo, commit)

    def _commit_has_equivalent_patch(self, repo: Path, commit: str) -> bool:
        return RuntimeSourceMaterializer.commit_has_equivalent_patch(repo, commit)

    def _active_runtime_profile(self, patch) -> str:
        return self._runtime_source_materializer().active_runtime_profile(patch)

    def _apply_current_minus_profile(self, patch, proof, module: str, target: Path) -> None:
        self._runtime_source_materializer().apply_current_minus_profile(
            patch, proof, module, target
        )

    def _apply_full_runtime_profile(self, patch, module: str, target: Path) -> None:
        self._runtime_source_materializer().apply_full_runtime_profile(patch, module, target)

    def _remove_path_for_materialize(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def _has_symlink_parent(self, path: Path, stop: Path) -> bool:
        current = path.parent
        while current != stop and stop in current.parents:
            if current.is_symlink():
                return True
            current = current.parent
        return False

    @contextmanager
    def _source_base_green_source_tree(self, patch, module: str):
        """Materialize the fixed/profile source tree for source-base proofs."""
        with self._runtime_source_materializer().source_base_green_source_tree(
            patch, module
        ) as tree:
            yield tree


    @contextmanager
    def _materialize_source_base_green_tree(self, patch, module: str):
        """Create one disposable fixed source tree for a source-base proof."""
        with self._runtime_source_materializer().materialize_source_base_green_tree(
            patch, module
        ) as tree:
            yield tree







    def _dump_command_tail(self, label: str, result) -> None:
        RuntimeBuildService(self).dump_command_tail(label, result)
        return



    def _runtime_replace_file(self, src: Path, dst: Path) -> None:
        from deploy_transaction import DeploymentTransaction
        DeploymentTransaction._replace_file(src, dst)

    def _emit_bootstrap_heartbeat(
        self, prefix: Path, target: str, elapsed: float
    ) -> None:
        """Publish live guest progress before cleanup destroys runtime state."""

        self.inf(
            f"prefix bootstrap heartbeat: guest {target} still running "
            f"({elapsed:.0f}s)"
        )
        for line in self._bootstrap_runtime_state(prefix).splitlines():
            self.inf(f"  {line}")

    def _shutdown_runtime_prefix(
        self, prefix: Path, *, extra_env: dict[str, str] | None = None
    ) -> bool:
        self._load_retained_prefix_env(Path(prefix))
        return self._prefix_lifecycle_owner().shutdown(prefix, extra_env=extra_env)

    def _finalize_prefix_shutdown(self, prefix: Path) -> bool:
        return self._prefix_lifecycle_owner().finalize(prefix)

    def _invocation_from_runtime_source(self, invocation, source_root: Path):
        repo = invocation.get("repo")
        script = invocation.get("script")
        if not repo or not script:
            return invocation

        repo_path = Path(repo)
        if repo_path == Path("darling"):
            runtime_cwd = source_root
        else:
            try:
                runtime_cwd = source_root / repo_path.relative_to("darling")
            except ValueError:
                return invocation

        runtime_invocation = dict(invocation)
        runtime_invocation["cwd"] = runtime_cwd
        runtime_invocation["script_path"] = runtime_cwd / script
        return runtime_invocation

    def _invocation_from_source_profile(self, invocation, source_root: Path):
        """Run a profile-owned test script from a materialized source tree."""
        script = invocation.get("script")
        if not script:
            return invocation
        profile_invocation = dict(invocation)
        profile_invocation["cwd"] = source_root
        profile_invocation["script_path"] = source_root / script
        return profile_invocation

    def _guest_runtime_red_invocation(self, patch, proof, invocation):
        red_runner = proof.get("red-runner")
        if red_runner is None:
            return invocation
        if not isinstance(red_runner, dict):
            self.die(f"{patch['path']}: red-proof.red-runner must be a mapping")
        red_test = dict(red_runner)
        red_test.setdefault("name", f"{invocation['name']}_red")
        red_test.setdefault("diag", invocation.get("diag", "bare"))
        red_test.setdefault("timeout-seconds", invocation.get("timeout_seconds", 600))
        resources = set(red_test.get("requires", []))
        inherited_resources = set(invocation.get("requires_resources", []))
        prefix_resources = inherited_resources & {"darling-prefix", "darling-eunion-prefix"}
        resources.update(prefix_resources or {"darling-prefix"})
        red_test["requires"] = sorted(resources)
        return self._test_invocation(patch, red_test)

    def _run_guest_runtime_deploy_green(self, patch, proof, invocation) -> int:
        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{patch['path']}: guest-runtime-deploy needs a Darling prefix")
        prefix = Path(prefix_text)
        self._require_runtime_scratch_space(
            f"{patch['path']}: {invocation['name']} GREEN"
        )
        self._preflight_runtime_profile_stack(
            self._active_runtime_profile(patch),
            f"{patch['path']}: {invocation['name']} GREEN",
        )
        evidence_store = self._runtime_evidence_store()
        evidence = evidence_store.start(
            f"{patch['path']}: {invocation['name']} GREEN runtime",
            {
                "patch": patch["path"],
                "phase": "green",
                "source-profile": self._active_runtime_profile(patch),
            },
        )
        evidence_failure = None
        try:
            green_env = self._execution_env(invocation)
            if green_env is None:
                green_env = os.environ.copy()
            else:
                green_env = dict(green_env)
            # A runtime test's resources describe its whole logical run, not
            # merely its final script. In particular, clear any host-temp
            # readiness marker before the costly source/build/deploy phase.
            with self._resource_context(invocation, green_env) as resource_env:
                scratch_root = evidence.directory
                with self._guest_runtime_source_forest(
                    patch,
                    proof,
                    omit_patch=False,
                    root=evidence.source_root,
                    evidence_session=evidence,
                ) as source_root:
                    build_root = self._runtime_red_build_artifacts(
                        source_root,
                        proof,
                        prefix,
                        scratch_root,
                        label="GREEN",
                    )
                    with self._runtime_red_deployed_artifacts(
                        proof,
                        build_root,
                        prefix,
                        label="GREEN",
                        lifecycle_env=resource_env,
                    ):
                        resource_env["WEST_RUNTIME_SOURCE_ROOT"] = str(source_root)
                        runtime_invocation = self._invocation_from_runtime_source(invocation, source_root)
                        green_started_at = time.time()
                        green_rc = self._run_invocation(runtime_invocation, env=resource_env)
                    if green_rc != 0:
                        evidence.preserve(RuntimeError(f"guest GREEN returned {green_rc}"))
                        return green_rc
                    if not self._check_guest_runtime_green_success(
                        runtime_invocation,
                        since=green_started_at,
                    ):
                        evidence.preserve(RuntimeError("guest GREEN completion oracle failed"))
                        return 1
                    return 0
        except BaseException as error:
            evidence_failure = error
            raise
        finally:
            retained = evidence_store.finish(evidence, evidence_failure)
            if retained is not None:
                self.err(f"preserved failed GREEN runtime evidence: {retained}")

    def _check_guest_runtime_green_success(self, invocation, *, since: float) -> bool:
        if invocation.get("host_trace_oracle"):
            return True
        ok_marker = invocation.get("ok_marker")
        if not ok_marker:
            return True
        bundle = self._latest_debug_bundle(invocation, since=since)
        if bundle is None:
            self.err(
                f"{invocation['name']}: GREEN output requested, "
                f"but no recent debug bundle was found under {self._debug_bundle_root()}"
            )
            return False
        output = self._debug_bundle_output(bundle)
        if str(ok_marker) not in output:
            self.err(f"{invocation['name']}: GREEN output missing {ok_marker!r} in {bundle}")
            return False
        if "ORACLE_RC=0" not in output:
            self.err(f"{invocation['name']}: GREEN output missing 'ORACLE_RC=0' in {bundle}")
            return False
        return True

    def _check_guest_runtime_red_failure(
        self,
        proof,
        invocation,
        *,
        since: float,
        captured_output: str | None = None,
    ) -> bool:
        contains, lacks = self._red_output_expectations(proof)
        if not contains and not lacks:
            return True

        output = self._guest_runtime_red_output(
            invocation, since=since, captured_output=captured_output
        )
        if output is None:
            self.err(
                f"{invocation['name']}: RED failure output requested, "
                f"but no recent debug bundle was found under {self._debug_bundle_root()}"
            )
            return False
        return self._check_red_output_expectations(
            proof, invocation, output, where="in captured RED evidence"
        )

    def _guest_runtime_red_output(
        self, invocation, *, since: float, captured_output: str | None = None
    ) -> str | None:
        observed_output = (captured_output or "") + self._runtime_diagnostic_output(invocation)
        bundle = self._latest_debug_bundle(invocation, since=since)
        if bundle is None:
            return observed_output or None
        return self._debug_bundle_output(bundle) + observed_output

    def _red_output_expectations(self, proof) -> tuple[list[str], list[str]]:
        contains = proof.get("expect-output-contains", [])
        lacks = proof.get("expect-output-lacks", [])
        if isinstance(contains, str):
            contains = [contains]
        if isinstance(lacks, str):
            lacks = [lacks]
        return [str(item) for item in contains], [str(item) for item in lacks]

    def _red_failure_phases(self, proof) -> list[str]:
        phases = proof.get("expect-failure-phase", [])
        if isinstance(phases, str):
            phases = [phases]
        return [str(phase) for phase in phases]

    def _check_red_failure_phase(self, proof, invocation, observed: str | None) -> bool:
        phases = self._red_failure_phases(proof)
        if not phases:
            return True
        if observed in phases:
            return True
        self.err(
            f"{invocation['name']}: RED failed in phase "
            f"{observed or '<unclassified>'}, want "
            f"{', '.join(phases)}"
        )
        return False

    def _check_red_output_expectations(self, proof, invocation, output: str, *, where: str) -> bool:
        contains, lacks = self._red_output_expectations(proof)
        for needle in contains:
            if needle not in output:
                self.err(
                    f"{invocation['name']}: RED failure output missing {needle!r} "
                    f"{where}"
                )
                return False
        for needle in lacks:
            if needle in output:
                self.err(
                    f"{invocation['name']}: RED failure output unexpectedly contains "
                    f"{needle!r} {where}"
                )
                return False
        return True

    def _guest_runtime_red_has_positive_reason(self, proof) -> bool:
        contains = proof.get("expect-output-contains")
        if isinstance(contains, str):
            return bool(contains)
        return isinstance(contains, list) and any(
            isinstance(item, str) and item for item in contains
        )

    def _run_metadata_runtime_profile_proof(self, patch, test, proof, invocation) -> int:
        """Prove RED and GREEN through the metadata-declared runtime provider."""
        if proof.get("source-patches") or proof.get("prepare-fixture-before-deploy"):
            self.die(
                f"{patch['path']}: runtime-profile guest-runtime-deploy does not support "
                "source-patches or prepare-fixture-before-deploy"
            )

        red_invocation = self._guest_runtime_red_invocation(patch, proof, invocation)
        machine = RuntimeProofStateMachine(
            name=red_invocation["name"],
            oracle=RedOracle.from_manifest(proof),
            error=self.err,
        )
        original_prefix_text = getattr(self, "_prefix", None)
        if proof.get("clean-prefix") is True and not original_prefix_text:
            self.die(f"{patch['path']}: runtime-profile RED proof needs a prefix")
        runtime_deployment = RuntimeDeploymentService(self)
        isolated_prefix = None
        cleanup_error = None
        lifecycle_env = self._execution_env(red_invocation) or {}
        try:
            if proof.get("clean-prefix") is True:
                isolated_prefix = runtime_deployment.create_empty_prefix(
                    Path(original_prefix_text)
                )
                self._prefix = str(isolated_prefix.prefix)
            with self._metadata_runtime_profile_context(
                patch, test, omit_patch=True, red_proof=proof
            ) as deployment:
                runtime_env = deployment.env if deployment is not None else None
                runtime_invocation = self._with_runtime_diagnostics(
                    red_invocation, deployment
                )
                red_env = self._runtime_profile_execution_env(
                    runtime_invocation, runtime_env
                )
                with self._ctest_source_override_context(runtime_invocation) as run_invocation:
                    with self._resource_context(run_invocation, red_env) as resource_env:
                        red_started_at = time.time()
                        red_result = self._run_invocation_captured(
                            run_invocation, env=resource_env
                        )
                observation_output = red_result.output
                diagnostic_output = self._guest_runtime_red_output(
                    run_invocation,
                    since=red_started_at,
                    captured_output=red_result.output,
                )
                if diagnostic_output is None and machine.oracle.output_contains:
                    self.err(
                        f"{run_invocation['name']}: RED domain output is unavailable"
                    )
                    return 1
                observation = ProofObservation(
                    red_result.returncode,
                    diagnostic_output or observation_output,
                    red_result.failure_phase,
                )
        except RuntimeProviderFailure as failure:
            if proof.get("provider-under-test") is not True:
                self.die(
                    f"{patch['path']}: provider failure requires "
                    "red-proof.provider-under-test: true"
                )
            if failure.kind in {"download", "cache", "setup"}:
                self.die(
                    f"{patch['path']}: provider RED cannot accept {failure.kind} failure: "
                    f"{failure}"
                )
            observation = ProofObservation(1, str(failure), "provider")
        finally:
            if isolated_prefix is not None:
                self._prefix = original_prefix_text
                if not runtime_deployment.cleanup_empty_prefix(
                    isolated_prefix, lifecycle_env=lifecycle_env
                ):
                    cleanup_error = RuntimeError(
                        "guest-runtime-deploy isolated RED prefix cleanup failed"
                    )

        if cleanup_error is not None:
            return 1

        if not machine.validate_red(observation):
            return 1
        self.inf(
            f"  RED runtime provider failed as expected (rc={observation.returncode})"
        )

        def run_green() -> int:
            self.inf("  GREEN runtime provider")
            with self._metadata_runtime_profile_context(patch, test) as deployment:
                runtime_env = deployment.env if deployment is not None else None
                runtime_invocation = self._with_runtime_diagnostics(invocation, deployment)
                green_env = self._runtime_profile_execution_env(runtime_invocation, runtime_env)
                with self._ctest_source_override_context(runtime_invocation) as run_invocation:
                    with self._resource_context(run_invocation, green_env) as resource_env:
                        return self._run_invocation(run_invocation, env=resource_env)

        return machine.run_green(run_green)

    def _run_guest_runtime_deploy_proof(self, patch, proof, invocation) -> int:
        if (
            not invocation.get("guest_c_fixture")
            and not invocation.get("guest_command_fixture")
            and not invocation.get("guest_argv_fixture")
            and not invocation.get("ctest_label")
            and not invocation.get("ctest_name")
            and invocation.get("runner") not in {"script", "guest-runtime-script"}
        ):
            self.die(
                f"{patch['path']}: guest-runtime-deploy requires guest-c-fixture, "
                "guest-command-fixture, guest-argv-fixture, CTest, script, or guest-runtime-script"
            )
        if invocation.get("ctest_label") or invocation.get("ctest_name"):
            if invocation.get("ctest_env") != "darling":
                self.die(f"{patch['path']}: runtime CTest proof requires a Darling registration")
            if not {"darling-prefix", "darling-eunion-prefix"} & set(invocation.get("requires_resources", [])):
                self.die(f"{patch['path']}: runtime CTest proof requires darling-prefix")
        if not self._guest_runtime_red_has_positive_reason(proof):
            self.die(
                f"{patch['path']}: {invocation['name']} guest-runtime-deploy "
                "RED proof needs expect-output-contains"
            )
        if invocation.get("runner") in {"script", "guest-runtime-script"}:
            resources = set(invocation.get("requires_resources", []))
            if not resources & {"darling-prefix", "darling-eunion-prefix"}:
                self.die(
                    f"{patch['path']}: guest-runtime-deploy script runner requires "
                    "darling-prefix"
                )
        missing_env = self._missing_requirements(invocation)
        if missing_env:
            self.die(
                f"{patch['path']}: missing required environment for {invocation['name']}: "
                f"{', '.join(missing_env)}"
            )
        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(f"{patch['path']}: guest-runtime-deploy needs a Darling prefix")
        prefix = Path(prefix_text)
        machine = RuntimeProofStateMachine(
            name=invocation["name"],
            oracle=RedOracle.from_manifest(proof),
            error=self.err,
        )
        self._require_runtime_scratch_space(
            f"{patch['path']}: {invocation['name']} RED"
        )
        self._preflight_runtime_profile_stack(
            self._active_runtime_profile(patch),
            f"{patch['path']}: {invocation['name']} RED",
        )
        evidence_store = self._runtime_evidence_store()
        evidence = evidence_store.start(
            f"{patch['path']}: {invocation['name']} RED runtime",
            {
                "patch": patch["path"],
                "phase": "red",
                "source-profile": self._active_runtime_profile(patch),
            },
        )
        evidence_failure = None
        runtime_deployment = RuntimeDeploymentService(self)
        original_prefix = prefix
        original_prefix_text = self._prefix
        isolated_prefix = None
        lifecycle_env = self._execution_env(invocation) or {}
        try:
            if proof.get("clean-prefix") is True:
                isolated_prefix = runtime_deployment.create_empty_prefix(prefix)
                prefix = isolated_prefix.prefix
                self._prefix = str(prefix)
            scratch_root = evidence.directory
            with self._guest_runtime_source_forest(
                patch,
                proof,
                omit_patch=True,
                root=evidence.source_root,
                evidence_session=evidence,
            ) as source_root:
                try:
                    build_root = self._runtime_red_build_artifacts(
                        source_root,
                        proof,
                        prefix,
                        scratch_root,
                        label="RED",
                        allow_failure=True,
                    )
                except RuntimeBuildFailure as failure:
                    if not machine.validate_red(
                        ProofObservation(
                            failure.result.returncode,
                            process_output_text(failure.result),
                            failure.phase,
                        )
                    ):
                        evidence.preserve(RuntimeError("RED runtime build failed for an unexpected reason"))
                        return 1
                    self.inf(
                        "  RED runtime build failed as expected "
                        f"(phase={failure.phase}, rc={failure.result.returncode})"
                    )
                    raise RuntimeRedProven()
                fixture_id = f"{invocation['name']}.{os.getpid()}.{int(time.time() * 1000)}"
                if invocation.get("guest_c_fixture") and proof.get("prepare-fixture-before-deploy"):
                    prepare_env = self._execution_env(invocation)
                    if prepare_env is None:
                        prepare_env = os.environ.copy()
                    else:
                        prepare_env = dict(prepare_env)
                    prepare_env["WEST_RUNTIME_SOURCE_ROOT"] = str(source_root)
                    prepare_env["WEST_GUEST_C_FIXTURE_ID"] = fixture_id
                    prepare_env["WEST_GUEST_C_FIXTURE_PREPARE_ONLY"] = "1"
                    prepare_invocation = self._invocation_from_runtime_source(invocation, source_root)
                    self.inf("  RED prepare guest fixture before bad runtime deploy")
                    with self._resource_context(prepare_invocation, prepare_env) as resource_env:
                        prepare_rc = self._run_invocation(prepare_invocation, env=resource_env)
                    if prepare_rc != 0:
                        evidence.preserve(RuntimeError(f"RED guest fixture preparation returned {prepare_rc}"))
                        return prepare_rc
                red_invocation = self._guest_runtime_red_invocation(patch, proof, invocation)
                lifecycle_env = self._execution_env(red_invocation) or {}
                with self._runtime_red_deployed_artifacts(
                    proof,
                    build_root,
                    prefix,
                    label="RED",
                    lifecycle_env=lifecycle_env,
                ):
                    bad_env = self._execution_env(red_invocation)
                    if bad_env is None:
                        bad_env = os.environ.copy()
                    else:
                        bad_env = dict(bad_env)
                    bad_env["WEST_RUNTIME_SOURCE_ROOT"] = str(source_root)
                    if red_invocation.get("guest_c_fixture") and proof.get("prepare-fixture-before-deploy"):
                        bad_env["WEST_GUEST_C_FIXTURE_ID"] = fixture_id
                        bad_env["WEST_GUEST_C_FIXTURE_RUN_ONLY"] = "1"
                    runtime_invocation = self._invocation_from_runtime_source(red_invocation, source_root)
                    with self._resource_context(runtime_invocation, bad_env) as resource_env:
                        red_started_at = time.time()
                        bad_result = self._run_invocation_captured(
                            runtime_invocation,
                            env=resource_env,
                        )
                    red_output = self._guest_runtime_red_output(
                        runtime_invocation,
                        since=red_started_at,
                        captured_output=bad_result.output,
                    )
                    if red_output is None and machine.oracle.output_contains:
                        self.err(f"{runtime_invocation['name']}: RED domain output is unavailable")
                        evidence.preserve(RuntimeError("RED runtime output is unavailable"))
                        return 1
                    if not machine.validate_red(
                        ProofObservation(
                            bad_result.returncode,
                            red_output or bad_result.output,
                            bad_result.failure_phase,
                        )
                    ):
                        evidence.preserve(RuntimeError("RED runtime output missed its domain oracle"))
                        return 1
                    self.inf(f"  RED runtime failed as expected (rc={bad_result.returncode})")
        except RuntimeRedProven:
            pass
        except BaseException as error:
            evidence_failure = error
            raise
        finally:
            cleanup_error = None
            if isolated_prefix is not None:
                self._prefix = original_prefix_text
                prefix = original_prefix
                if not runtime_deployment.cleanup_empty_prefix(
                    isolated_prefix, lifecycle_env=lifecycle_env
                ):
                    cleanup_error = RuntimeError(
                        "guest-runtime-deploy isolated RED prefix cleanup failed"
                    )
            if cleanup_error is not None and evidence_failure is None:
                evidence_failure = cleanup_error
                raise cleanup_error
            retained = evidence_store.finish(evidence, evidence_failure)
            if retained is not None:
                self.err(f"preserved failed RED runtime evidence: {retained}")
        def run_green() -> int:
            self.inf("  GREEN profile runtime")
            if not self._shutdown_runtime_prefix(prefix):
                self.die(
                    f"guest-runtime-deploy could not clean Darling prefix before GREEN runtime: {prefix}"
                )
            return self._run_guest_runtime_deploy_green(patch, proof, invocation)

        return machine.run_green(run_green)

    def _run_source_base_proof(self, patch, proof, invocation) -> int:
        if invocation["shell"]:
            self.die(f"{patch['path']}: source-base proof requires a structured runner")
        if invocation.get("guest_c_fixture"):
            self._reject_guest_source_base_red_proof(patch)
        source_env = proof.get("source-env")
        if not source_env:
            self.die(f"{patch['path']}: source-base proof needs red-proof.source-env")
        module = proof.get("source-module", patch["module"])

        def ctest_invocation_for(source_root: Path, build_root: Path):
            override = invocation.get("ctest_source_override")
            if not override:
                return proof_invocation
            configured = dict(proof_invocation)
            configured["ctest_build"] = self._configure_and_build(
                self._testkit_dir(),
                self._executor,
                darling_launcher=self._resolve_darling_launcher(self._prefix),
                prefix=self._prefix,
                bundle_root=str(getattr(self, "_bundle_root", "")),
                build_dir=build_root,
                cmake_defines=self._ctest_cmake_defines(
                    proof_invocation, source_override=override, source_root=source_root
                ),
            )
            return configured

        with self._source_base_green_source_tree(patch, module) as green_source:
            if green_source is not None:
                green_source_env = green_source
                self.inf(f"  GREEN profile source tree: {source_env}={green_source_env}")
            else:
                green_source_env = self._project_path(module)
                self.inf(f"  GREEN current tree: {source_env}={green_source_env}")

            if invocation.get("runner") == "source-profile-script":
                proof_invocation = self._invocation_from_source_profile(invocation, green_source_env)
            else:
                proof_invocation = invocation

            script_path = proof_invocation.get("script_path")
            if script_path is not None and not script_path.is_file():
                self.die(f"{patch['path']}: test script not found: {script_path}")

            bad_revision = self._bad_revision(patch, proof)
            with self._runtime_source_materializer().bad_source_tree(
                module, bad_revision
            ) as worktree:
                with tempfile.TemporaryDirectory(prefix="west-red-proof-ctest-") as temp:
                    bad_env = os.environ.copy()
                    exec_env = self._execution_env(proof_invocation)
                    if exec_env:
                        bad_env.update(exec_env)
                    bad_env[source_env] = str(worktree)
                    red_invocation = ctest_invocation_for(
                        worktree, Path(temp) / "ctest-build"
                    )
                    self.inf(f"  RED source tree: {bad_revision} via {source_env}={worktree}")
                    try:
                        bad_result = self._run_invocation_captured(
                            red_invocation, env=bad_env
                        )
                    except SystemExit as error:
                        if not red_invocation.get("ctest_label"):
                            raise
                        bad_result = InvocationResult(
                            1, str(error), "ctest-discovery"
                        )
                    if bad_result.returncode == 0:
                        self.err("  RED proof failed: source-base run unexpectedly passed")
                        return 1
                    if not self._check_red_failure_phase(
                        proof,
                        proof_invocation,
                        bad_result.failure_phase,
                    ):
                        return 1
                    if not self._check_red_output_expectations(
                        proof,
                        proof_invocation,
                        bad_result.output,
                        where="in source-base RED output",
                    ):
                        return 1
                    self.inf(f"  RED path failed as expected (rc={bad_result.returncode})")

            green_env = self._execution_env(invocation)
            if green_env is None:
                green_env = os.environ.copy()
            else:
                green_env = dict(green_env)
            if source_env:
                green_env[source_env] = str(green_source_env)
            with tempfile.TemporaryDirectory(prefix="west-green-proof-ctest-") as temp:
                green_invocation = ctest_invocation_for(
                    Path(green_source_env), Path(temp) / "build"
                )
                return self._run_invocation(green_invocation, env=green_env)

    def _run_red_proofs(self, tests, list_only: bool, unknown: list[str]) -> int:
        """Run the proof that a regression test really distinguishes old/bad behavior.

        A normal metadata test run always expects GREEN on the current checkout.
        RED proof is an explicit second mode. `mode: self` means the
        test binary/script contains its own bad-path oracle
        (for example, run an old algorithm and require that it fails, then run
        the fixed algorithm and require that it passes). `mode: source-base`
        keeps the current test asset and points it at a bad/source-base worktree
        through an explicit source-root environment variable.
        """
        if unknown:
            self.die("metadata RED proofs do not accept raw ctest passthrough arguments")
        rc = 0
        seen_invocations: set[tuple] = set()
        self._prune_stale_west_temp_worktrees()
        with ExitStack() as source_base_green_stack:
            self._source_base_green_stack = source_base_green_stack
            self._source_base_green_cache = {}
            try:
                for patch, test in tests:
                    proof = test.get("red-proof")
                    name = test.get("name", "-")
                    if not proof:
                        self.die(
                            f"{patch['path']}: {name} is marked red but has no red-proof metadata"
                        )
                    mode = proof.get("mode") if isinstance(proof, dict) else proof
                    invocation = self._test_invocation(patch, test)
                    self.inf(f"{patch['path']}: {name} RED proof [{mode}]")
                    self.inf(f"  {self._display_invocation(invocation)}")
                    if mode == "guest-runtime-deploy" and isinstance(proof, dict):
                        self.inf(f"  {self._display_guest_runtime_deploy_plan(proof)}")
                    if list_only:
                        continue
                    if mode not in {"self", "source-base", "guest-runtime-deploy"}:
                        self.die(
                            f"{patch['path']}: RED proof mode {mode!r} is not implemented; "
                            "use mode: self, source-base, or guest-runtime-deploy"
                        )
                    if not self._red_failure_phases(proof):
                        self.die(
                            f"{patch['path']}: {name} RED proof needs expect-failure-phase"
                        )
                    if (
                        mode in {"self", "guest-runtime-deploy"}
                        and not self._guest_runtime_red_has_positive_reason(proof)
                    ):
                        self.die(
                            f"{patch['path']}: {name} RED proof needs expect-output-contains"
                        )
                    if mode == "source-base" and invocation.get("guest_c_fixture"):
                        self._reject_guest_source_base_red_proof(patch)
                    script_path = invocation.get("script_path")
                    if script_path is not None and not script_path.is_file():
                        self.die(f"{patch['path']}: test script not found: {script_path}")
                    missing_env = self._missing_requirements(invocation)
                    if missing_env:
                        self.die(
                            f"{patch['path']}: missing required environment for {name}: "
                            f"{', '.join(missing_env)}"
                        )
                    identity = metadata_invocation_identity(invocation, test)
                    invocation_key = (
                        (patch["path"], *identity)
                        if mode == "source-base"
                        else identity
                    )
                    if invocation_key in seen_invocations:
                        self.inf("  skipped duplicate invocation already run")
                        continue
                    seen_invocations.add(invocation_key)
                    with self._required_profile_context(patch, invocation):
                        if mode == "source-base":
                            result_rc = self._run_source_base_proof(patch, proof, invocation)
                        elif mode == "guest-runtime-deploy":
                            if test.get("runtime-profile"):
                                result_rc = self._run_metadata_runtime_profile_proof(
                                    patch, test, proof, invocation
                                )
                            else:
                                result_rc = self._run_guest_runtime_deploy_proof(
                                    patch, proof, invocation
                                )
                        else:
                            exec_env = self._execution_env(invocation)
                            with self._resource_context(invocation, exec_env):
                                self_result = self._run_invocation_captured(invocation, env=exec_env)
                            if self_result.returncode:
                                result_rc = self_result.returncode
                            elif not self._check_red_failure_phase(proof, invocation, "self"):
                                result_rc = 1
                            elif not self._check_red_output_expectations(
                                proof,
                                invocation,
                                self_result.output,
                                where="in self-contained RED output",
                            ):
                                result_rc = 1
                            else:
                                self.inf("  self-contained RED arm observed as expected")
                                result_rc = 0
                    if result_rc:
                        rc = result_rc
            finally:
                del self._source_base_green_cache
                del self._source_base_green_stack
        return rc

    def _reject_unsupported_red_proof_models(self, tests) -> None:
        for patch, test in tests:
            proof = test.get("red-proof")
            if not isinstance(proof, dict) or proof.get("mode") != "source-base":
                continue
            if test.get("runner") == "guest-c-fixture":
                self._reject_guest_source_base_red_proof(patch)

    def _check_red_proof_requirements(self, tests) -> None:
        for patch, test in tests:
            invocation = self._test_invocation(patch, test)
            missing = self._missing_requirements(invocation)
            if missing:
                self.die(
                    f"{patch['path']}: missing required environment for "
                    f"{test.get('name', '-')}: {', '.join(missing)}"
                )

    def _red_proof_audit(self, tests) -> list[str]:
        """Return manifest gaps that would make a RED result ambiguous."""
        missing = []
        for patch, test in tests:
            proof = test.get("red-proof")
            if not isinstance(proof, dict):
                continue
            mode = proof.get("mode")
            if mode not in {"self", "source-base", "guest-runtime-deploy"}:
                missing.append(
                    f"{patch['path']}: {test.get('name', '-')} has unsupported RED mode {mode!r}"
                )
                continue
            if not self._red_failure_phases(proof):
                missing.append(
                    f"{patch['path']}: {test.get('name', '-')} RED proof needs expect-failure-phase"
                )
            if mode in {"self", "guest-runtime-deploy"} and not self._guest_runtime_red_has_positive_reason(proof):
                missing.append(
                    f"{patch['path']}: {test.get('name', '-')} "
                    "RED proof needs expect-output-contains"
                )
        return missing

    def _shutdown_test_prefix(self) -> bool:
        prefix = getattr(self, "_prefix", None)
        if not prefix:
            return True
        self._load_retained_prefix_env(Path(prefix))
        return self._prefix_lifecycle_owner().shutdown(
            Path(prefix), keep_running=getattr(self, "_keep_prefix_running", False)
        )

    # Provider retention stays in the facade: its identity check is defined by
    # ``test_bootstrap``, which owns the marker written into the prefix.
    def _load_retained_prefix_env(self, prefix: Path) -> None:
        """Restore provider flags before a separate process resets a prefix."""

        marker_path = prefix / RETAINED_RUNTIME_PROFILE_MARKER
        try:
            marker = json.loads(marker_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(marker, dict) or marker.get("schema") != 2:
            return
        profile_name = marker.get("profile")
        if not isinstance(profile_name, str) or not profile_name:
            return
        definition = self._ctest_runtime_profile_definitions().get(profile_name)
        if definition is None or marker.get("source-profile") != definition.get("source-profile"):
            return
        self._prefix_env.update(
            {key: str(value) for key, value in definition.get("launcher-env", {}).items()}
        )

    def _retained_runtime_profile(self, profile_name: str) -> RuntimeProfileDeployment:
        """Use a provider retained by ``--bootstrap-runtime-profile``.

        The marker is an identity check, not a source snapshot. It prevents a
        follow-up metadata run from silently using a prefix provisioned for a
        different runtime profile.
        """

        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die("--reuse-prefix-runtime requires --prefix or DPREFIX")
        prefix = Path(prefix_text)
        marker_path = prefix / RETAINED_RUNTIME_PROFILE_MARKER
        try:
            marker = json.loads(marker_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            self.die(
                "--reuse-prefix-runtime needs a retained provider marker at "
                f"{marker_path}: {error}; run --bootstrap-runtime-profile first"
            )
        definition = self._ctest_runtime_profile_definitions().get(profile_name)
        if definition is None:
            self.die(f"unknown retained runtime profile: {profile_name}")
        launcher = prefix / "bin" / "darling"
        if not launcher.is_file():
            self.die(
                "--reuse-prefix-runtime retained prefix has no launcher: "
                f"{launcher}"
            )
        expected_fingerprint = runtime_identity(
            topdir=Path(self.topdir),
            manifest_repo=Path(self.manifest.repo_abspath),
            profile_name=profile_name,
            definition=definition,
            launcher=launcher,
        )
        if (
            not isinstance(marker, dict)
            or marker.get("schema") != 2
            or marker.get("profile") != profile_name
            or marker.get("source-profile") != definition.get("source-profile")
            or marker.get("fingerprint") != expected_fingerprint
        ):
            actual = marker.get("profile") if isinstance(marker, dict) else None
            self.die(
                "--reuse-prefix-runtime retained provider fingerprint mismatch: selected "
                f"{profile_name!r}, retained {actual!r}; bootstrap the selected profile again"
            )
        runtime_env = os.environ.copy()
        runtime_env.update(self._darling_prefix_env(prefix))
        runtime_env.update(
            {key: str(value) for key, value in definition.get("launcher-env", {}).items()}
        )
        runtime_env["DARLING"] = str(launcher)
        runtime_env["DARLING_LAUNCHER"] = str(launcher)
        return RuntimeProfileDeployment(
            name=profile_name,
            prefix=prefix,
            build_root=prefix,
            env=runtime_env,
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

    def _kill_dserver_for_prefix(self, prefix: Path) -> None:
        self._prefix_lifecycle_owner()._kill_server(prefix)

    @contextmanager
    def _prefix_resource_context(self, enabled: bool):
        prefix = getattr(self, "_prefix", None)
        if not enabled or not prefix:
            yield
            return

        with self._prefix_lifecycle_owner().locked(Path(prefix).expanduser()):
            self._prefix_cleanup_failed = False
            try:
                if not self._shutdown_test_prefix():
                    self._prefix_cleanup_failed = True
                    self.die(
                        f"could not reset Darling prefix before test run: {prefix}"
                    )
                yield
            finally:
                if not self._shutdown_test_prefix():
                    self._prefix_cleanup_failed = True

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
        if args.prefix or args.prefix_profile:
            self.die("--fresh-prefix-from cannot be combined with --prefix or --prefix-profile")
        result = create_fresh_prefix(Path(baseline))
        for message in result.changed:
            self.inf(f"fresh prefix: {message}")
        if not result.success or result.path is None:
            self.die("fresh prefix: " + "; ".join(result.problems))
        args.prefix = str(result.path)
        try:
            yield
        finally:
            cleanup = remove_fresh_prefix(result.path)
            for message in cleanup.changed:
                self.inf(f"fresh prefix: {message}")
            for message in cleanup.problems:
                self.err(f"fresh prefix: {message}")
            if not cleanup.success:
                self._prefix_cleanup_failed = True

    def do_run(self, args, unknown):
        with self._fresh_prefix_context(args):
            return self._do_run(args, unknown)

    def _do_run(self, args, unknown):
        self._prefix = self._resolve_prefix(args)
        self._executor = self._resolve_executor(args.executor)
        # One owned state root per task or lane. When DW_STATE_ROOT is declared,
        # the defaults for bundles, proof scratch and runtime evidence live
        # beneath it, so two concurrent lanes cannot write into each other's
        # state and a cleanup cannot reach outside its own root. Undeclared
        # keeps the previous defaults, so existing callers are unchanged.
        if not getattr(args, "bundle_root", None):
            args.bundle_root = str(
                state_subdir("bundles") or Path("~/work/darling-debug").expanduser()
            )
        if not getattr(args, "proof_scratch_root", None):
            args.proof_scratch_root = str(state_subdir("scratch") or tempfile.gettempdir())
        if not getattr(args, "runtime_evidence_root", None):
            args.runtime_evidence_root = str(
                state_subdir("evidence") or ".west-test/runtime-evidence"
            )
        self._bundle_root = str(Path(args.bundle_root).expanduser())
        self._runtime_evidence_root = Path(
            getattr(args, "runtime_evidence_root", ".west-test/runtime-evidence")
        ).expanduser()
        self._materialize_profile = args.materialize_profile
        self._keep_prefix_running = args.keep_prefix_running
        self._reuse_prefix_runtime = bool(getattr(args, "reuse_prefix_runtime", False))
        self._bootstrap_syscall_trace = None
        self._bootstrap_stack_sample = None
        self._bootstrap_timeout_seconds = None
        self._runtime_build_timeout_seconds = None
        try:
            self._runtime_cmake_define_overrides = parse_runtime_cmake_define_overrides(
                getattr(args, "runtime_cmake_define", [])
            )
        except ValueError as error:
            self.die(f"invalid --runtime-cmake-define: {error}")

        if args.ctest_timeout_seconds <= 0:
            self.die("--ctest-timeout-seconds must be > 0")
        bootstrap_timeout_seconds = getattr(args, "bootstrap_timeout_seconds", None)
        if bootstrap_timeout_seconds is not None:
            if not 1 <= bootstrap_timeout_seconds <= 600:
                self.die("--bootstrap-timeout-seconds must be between 1 and 600")
            self._bootstrap_timeout_seconds = bootstrap_timeout_seconds
        runtime_build_timeout_seconds = getattr(args, "runtime_build_timeout_seconds", None)
        if runtime_build_timeout_seconds is not None:
            if runtime_build_timeout_seconds <= 0:
                self.die("--runtime-build-timeout-seconds must be > 0")
            self._runtime_build_timeout_seconds = runtime_build_timeout_seconds

        if getattr(args, "diagnostic", None):
            if (args.profile or args.patch or args.bead or args.list or args.prove_red
                    or args.keep_prefix_running or args.bootstrap_runtime_profile or unknown):
                self.die("--diagnostic cannot be combined with test selection, bootstrap, or keep-running")
            if len(args.with_runtime_profile) != 1:
                self.die("--diagnostic requires one --with-runtime-profile retained provider")
            from test_diagnostics import run_exact_capture
            raise SystemExit(run_exact_capture(self, args.with_runtime_profile[0]))

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

        if getattr(args, "cleanup_prefix", False):
            incompatible = []
            if args.profile or args.patch or args.env or args.label or args.changed:
                incompatible.append("test selection")
            if args.prove_red or args.red_only or args.red_audit or args.list:
                incompatible.append("metadata/list mode")
            if incompatible:
                self.die(
                    "--cleanup-prefix is a lifecycle operation; do not combine it with "
                    + ", ".join(incompatible)
                )
            if not self._prefix:
                self.die("--cleanup-prefix requires --prefix, --prefix-profile, or DPREFIX")
            if not self._shutdown_test_prefix():
                self.die(f"could not cleanly shutdown Darling prefix: {self._prefix}")
            return

        if args.gc:
            # A maintenance pass acts only inside its own root. With a declared
            # state root, a flag that points elsewhere is refused rather than
            # obeyed: reaching another lane's state is the failure the shared
            # roots produced, and it is silent until something is already gone.
            declared = state_root()
            for label, root in (
                ("bundle", args.bundle_root),
                ("proof scratch", args.proof_scratch_root),
                ("runtime evidence", self._runtime_evidence_root),
            ):
                if not inside_state_root(Path(root)):
                    self.die(
                        f"{label} root {Path(root).expanduser()} is outside the declared "
                        f"state root {declared} ({STATE_ROOT_ENV}); a maintenance pass "
                        "only acts inside its own root"
                    )
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
        if args.red_audit:
            profile = args.profile or "homebrew"
            selected, missing = select_metadata_tests_for_command(
                self,
                profile, args.patch, args.bead, args.env, args.diag, args.label,
                red_only=False, validation_group=args.guest_macho_validation_group
            )
            missing_reasons = self._red_proof_audit(selected)
            for patch in missing:
                self.inf(f"MISSING {patch['path']} [{patch.get('bead', '-')}]")
            for message in missing_reasons:
                self.inf(f"RED-REASON-MISSING {message}")
            self.inf(f"red-audit: {len(missing)} patch(es) missing tests/exception")
            self.inf(
                "red-audit: "
                f"{len(missing_reasons)} RED proof contract gap(s)"
            )
            if missing or missing_reasons:
                self.die("red-audit failed")
            return

        if args.patch and not args.profile:
            if args.prefix_profile:
                self.die(
                    "--patch selects patch metadata and requires --profile; "
                    "--prefix-profile selects only a Darling prefix "
                    "(for example: --profile homebrew --prefix-profile homebrew)"
                )
            self.die("--patch requires --profile")
        if args.profile and args.submodule:
            self.die("--submodule selects CTest suite tests; use --patch/--profile for patch metadata")
        if args.profile and (args.fuzz or args.stress):
            self.die("--fuzz/--stress select CTest suite tests; use --patch/--profile for patch metadata")
        bootstrap_runtime_profile = getattr(args, "bootstrap_runtime_profile", None)
        reuse_prefix_runtime = bool(getattr(args, "reuse_prefix_runtime", False))
        bootstrap_executable = getattr(args, "bootstrap_executable", None)
        bootstrap_syscall_trace = getattr(args, "bootstrap_syscall_trace", None)
        bootstrap_stack_sample = getattr(args, "bootstrap_stack_sample", None)
        if bootstrap_syscall_trace and bootstrap_stack_sample:
            self.die(
                "--bootstrap-syscall-trace and --bootstrap-stack-sample are mutually exclusive"
            )
        if bootstrap_stack_sample and not bootstrap_runtime_profile and not args.profile:
            self.die(
                "--bootstrap-stack-sample requires --bootstrap-runtime-profile "
                "or a metadata --profile selection with a runtime-profile"
            )
        if bootstrap_syscall_trace and not bootstrap_runtime_profile and not args.profile:
            self.die(
                "--bootstrap-syscall-trace requires --bootstrap-runtime-profile "
                "or a metadata --profile selection with a runtime-profile"
            )
        if bootstrap_runtime_profile:
            incompatible = []
            if args.profile or args.patch or args.prove_red or args.red_only or args.red_audit:
                incompatible.append("patch metadata selection")
            if args.changed or args.bead or args.submodule or args.label or args.fuzz or args.stress:
                incompatible.append("CTest selection")
            if args.list or args.with_runtime_profile or reuse_prefix_runtime or unknown:
                incompatible.append("CTest execution options")
            if incompatible:
                self.die(
                    "--bootstrap-runtime-profile is a prefix provisioning operation; "
                    "do not combine it with " + ", ".join(incompatible)
                )
            if bootstrap_executable and not Path(bootstrap_executable).is_absolute():
                self.die("--bootstrap-executable must be an absolute guest path")
            self._bootstrap_syscall_trace = Path(bootstrap_syscall_trace) if bootstrap_syscall_trace else None
            self._bootstrap_stack_sample = Path(bootstrap_stack_sample) if bootstrap_stack_sample else None
            try:
                self._bootstrap_runtime_profile(
                    bootstrap_runtime_profile, executable=bootstrap_executable
                )
                if getattr(self, "_prefix_cleanup_failed", False):
                    raise SystemExit(1)
                return
            finally:
                self._bootstrap_syscall_trace = None
                self._bootstrap_stack_sample = None
                self._bootstrap_timeout_seconds = None
                self._runtime_build_timeout_seconds = None

        if reuse_prefix_runtime and not args.profile:
            self.die("--reuse-prefix-runtime requires --profile metadata selection")
        if reuse_prefix_runtime and (args.prove_red or args.red_only or args.red_audit):
            self.die(
                "--reuse-prefix-runtime cannot be combined with RED or red-audit modes"
            )

        if bootstrap_executable:
            self.die("--bootstrap-executable requires --bootstrap-runtime-profile")

        if args.profile:
            selected, missing = select_metadata_tests_for_command(
                self,
                args.profile, args.patch, args.bead, args.env, args.diag, args.label,
                args.red_only, validation_group=args.guest_macho_validation_group
            )
            if args.guest_macho_validation_group:
                try:
                    validate_selected_group(
                        selected, args.guest_macho_validation_group
                    )
                except ValueError as error:
                    self.die(str(error))
                self._guest_macho_evidence_dir = (
                    Path(args.guest_macho_evidence_dir)
                    if args.guest_macho_evidence_dir
                    else None
                )
            if bootstrap_syscall_trace and not bootstrap_runtime_profile:
                if args.list or not any(test.get("runtime-profile") for _, test in selected):
                    self.die(
                        "--bootstrap-syscall-trace needs a selected metadata test "
                        "with runtime-profile"
                    )
                self._bootstrap_syscall_trace = Path(bootstrap_syscall_trace)
            if bootstrap_stack_sample and not bootstrap_runtime_profile:
                if args.list or not any(test.get("runtime-profile") for _, test in selected):
                    self.die(
                        "--bootstrap-stack-sample needs a selected metadata test "
                        "with runtime-profile"
                    )
                self._bootstrap_stack_sample = Path(bootstrap_stack_sample)
            if args.prove_red:
                selected = [
                    (patch, test)
                    for patch, test in selected
                    if test.get("red") or test.get("red-proof")
                ]
                if not selected:
                    self.die("no red-proof tests selected from patch metadata")
                self._reject_unsupported_red_proof_models(selected)
                if not args.list:
                    self._check_red_proof_requirements(selected)
            materialize_was_requested = self._materialize_profile
            previous_active_profile = getattr(self, "_active_profile", None)
            self._active_profile = args.profile
            if (
                selected
                and (not args.list or any(is_ctest_binding(test) for _, test in selected))
                and not self._materialize_profile
                and not self._profile_is_applied(args.profile)
                and self._metadata_needs_profile_worktree(selected)
            ):
                self.inf(
                    f"{args.profile}: selected tests need the profile checkout; "
                    "temporarily materializing profile in worktrees"
                )
                self._materialize_profile = True
            try:
                with self._selected_profile_context(
                    args.profile,
                    list_only=args.list and not any(is_ctest_binding(test) for _, test in selected),
                ), self._metadata_ctest_selection(
                    selected, env=args.env, diag=args.diag, label=args.label,
                    additional_profiles=args.with_runtime_profile,
                ) as selected:
                    if missing:
                        for patch in missing:
                            self.inf(f"missing test metadata: {patch['path']} [{patch.get('bead', '-')}]")
                    if selected:
                        needs_prefix = self._metadata_needs_prefix(selected) and not args.list
                        if args.prove_red:
                            needs_prefix = self._metadata_needs_prefix(selected) and not args.list
                            with self._prefix_resource_context(needs_prefix):
                                result = self._run_red_proofs(selected, args.list, unknown)
                            if getattr(self, "_prefix_cleanup_failed", False):
                                result = result or 1
                            raise SystemExit(result)
                        with self._prefix_resource_context(needs_prefix):
                            result = self._run_metadata_tests(selected, args.list, unknown)
                        if getattr(self, "_guest_macho_evidence_dir", None):
                            result |= finalize_guest_macho_evidence(self._guest_macho_evidence_dir, args.guest_macho_validation_group, [test["fixture"] for _, test in selected])
                        if getattr(self, "_prefix_cleanup_failed", False):
                            result = result or 1
                        raise SystemExit(result)
                    if args.list:
                        return
                    self.die("no tests selected from patch metadata")
            finally:
                self._materialize_profile = materialize_was_requested
                self._active_profile = previous_active_profile
                self._bootstrap_syscall_trace = None
                self._bootstrap_stack_sample = None
                self._bootstrap_timeout_seconds = None
                self._runtime_build_timeout_seconds = None

        testkit = self._testkit_dir()
        if not testkit.exists():
            self.die(f"no testkit at {testkit}")

        launcher = self._resolve_darling_launcher(self._prefix)
        if args.env == "darling" and not launcher and not args.list:
            if self._prefix:
                prefix_path = Path(self._prefix).expanduser()
                self.die(
                    "env:darling CTest runs need the selected prefix launcher: "
                    f"{prefix_path / 'bin' / 'darling'}; {prefix_path} is not "
                    "bootstrapped: "
                    + self._bootstrap_prefix_advice(
                        prefix_path, guest_toolchain=True
                    )
                )
            self.die(
                "env:darling CTest runs need a Darling launcher; pass --prefix, "
                "set DARLING/DARLING_LAUNCHER, or install ~/work/darling-prefix/bin/darling"
            )
        build = self._configure_and_build(
            testkit,
            self._executor,
            darling_launcher=launcher,
            prefix=self._prefix,
            bundle_root=str(getattr(self, "_bundle_root", "")),
            compile_tests=not args.list,
        )

        changed = None
        if args.changed:
            changed = self._changed_submodules()
            if not changed:
                self.inf("no changed submodules; nothing selected by --changed")
                return
            self.inf(f"changed submodules: {', '.join(changed)}")
        # CTest -L selectors are ANDed per flag; changed submodules use one
        # alternation label regex to select any touched submodule.
        label_args = ctest_selector_label_args(
            bead=args.bead,
            env=None,
            diag=None,
            label=args.label,
            fuzz=args.fuzz,
            stress=args.stress,
            changed_submodules=changed,
            submodules=args.submodule,
        )

        runtime_groups = self._selected_ctest_runtime_groups(
            build, label_args, unknown, args.with_runtime_profile, env=args.env, diag=args.diag
        )
        needs_prefix = (
            not args.list and (
                ctest_uses_prefix(env=args.env, list_only=False)
                or any(group["profiles"] for group in runtime_groups)
            )
        )
        if args.list:
            for group in runtime_groups:
                self.inf(
                    f"selected registrations: {', '.join(group['tests'])}; "
                    f"runtime profiles: {', '.join(group['profiles']) or 'none'}"
                )
        commands: list[tuple[list[str], list[str]]] = []
        for group in runtime_groups:
            try:
                group_passthrough = ctest_runtime_group_passthrough(unknown)
            except ValueError as error:
                self.die(f"invalid CTest passthrough selection: {error}")
            group_ctest = ctest_command(
                build,
                list_only=args.list,
                passthrough=[
                    *group_passthrough,
                    *ctest_index_args(group["indices"]),
                ],
            )
            commands.append((group_ctest, [] if args.list else group["profiles"]))
        with self._prefix_resource_context(needs_prefix):
            if not args.list:
                self._clear_ctest_failure_record(build)
            rc = 0
            for command, profiles in commands:
                profile_text = ", ".join(profiles) if profiles else "no runtime deployment"
                self.inf(f"running ({profile_text}): {' '.join(command)}")
                with self._ctest_runtime_profile_context(profiles) as runtime_env:
                    result = run_bounded(
                        command,
                        cwd=Path(self.topdir),
                        env=runtime_env,
                        timeout_seconds=int(args.ctest_timeout_seconds),
                    )
                if result.timed_out:
                    self.err(
                        "CTest selection timed out after "
                        f"{args.ctest_timeout_seconds}s"
                    )
                rc = rc or result.returncode
        if getattr(self, "_prefix_cleanup_failed", False):
            rc = rc or 1
        raise SystemExit(rc)

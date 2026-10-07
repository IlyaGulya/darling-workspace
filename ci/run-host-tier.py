#!/usr/bin/env python3
"""Run independent host-tier contracts with bounded fail-fast concurrency."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = (
    "tests/run-process-control-slot-ownership-contract.sh",
    "tests/run-truthful-nofile-contract.sh",
    "tests/run-dtape-kqchan-modify-context-contract.sh",
    "tests/run-west-patch-stack-materialize-contract.sh",
    "tests/run-west-patch-stack-lock-first-contract.sh",
    "tests/run-profile-composition-dependency-contract.sh",
    "tests/run-west-patch-stack-default-cutover-contract.sh",
    "tests/run-west-patch-stack-retirement-policy-contract.sh",
    "tests/run-west-patch-stack-runtime-source-contract.sh",
    "tests/run-patch-stack-immutable-oracle-contract.sh",
    "tests/run-west-patch-stack-export-contract.sh",
    "tests/run-patch-stack-lock-first-hosted-workflow-contract.sh",
    "tests/run-patch-stack-migration-inventory-contract.sh",
    "tests/run-west-test-ctest-backend-contract.sh",
    "tests/run-west-test-runtime-cache-contract.sh",
    "tests/run-west-test-stock-stack-cache-contract.sh",
    "tests/run-west-test-stock-replay-iterations-contract.sh",
    "tests/run-parallel-load-contract.sh",
    "tests/run-west-test-verdict-cache-contract.sh",
    "tests/run-west-test-applicability-preflight-diagnosis-contract.sh",
    "tests/run-west-test-metadata-run-report-contract.sh",
    "tests/run-west-test-dry-run-contract.sh",
    "tests/run-rootless-debug-cleanup-contract.sh",
    "tests/run-darling-boot-harness-ownership-contract.sh",
    "tests/run-west-test-runtime-source-reuse-contract.sh",
    "tests/run-west-test-metadata-invocation-identity-contract.sh",
    # Recovered: these contracts existed and passed but were listed by no tier,
    # so nothing ran them. Registered after running each one on this checkout.
    "tests/run-check-python-syntax-contract.sh",
    "tests/run-handoff-transaction-contract.sh",
    "tests/run-rootless-diagnostics-contract.sh",
    "tests/run-rootless-shutdown-consumer-contract.sh",
    "tests/run-rtk-exit-status-contract.sh",
    "tests/run-west-darling-build-contract.sh",
    "tests/run-west-deploy-transaction-contract.sh",
    "tests/run-west-dev-output-contract.sh",
    "tests/run-west-doctor-output-contract.sh",
    "tests/run-west-doctor-prefix-contract.sh",
    "tests/run-west-doctor-receipt-contract.sh",
    "tests/run-west-dw-beads-alias-contract.sh",
    "tests/run-west-extension-help-contract.sh",
    "tests/run-west-job-contract.sh",
    "tests/run-west-patch-explain-contract.sh",
    "tests/run-west-patch-export-preflight-contract.sh",
    "tests/run-west-patch-stack-preflight-contract.sh",
    "tests/run-west-prefix-repair-contract.sh",
    "tests/run-west-profile-discovery-contract.sh",
    "tests/run-west-source-worktree-contract.sh",
    "tests/run-west-test-ctest-lifecycle-contract.sh",
    "tests/run-west-test-descriptor-transport-contract.sh",
    "tests/run-west-test-dispatch-contract.sh",
    "tests/run-west-test-execution-contract.sh",
    "tests/run-west-test-expect-failure-contract.sh",
    "tests/run-west-test-manifest-contract.sh",
    "tests/run-west-test-resource-provider-contract.sh",
    "tests/run-west-test-results-contract.sh",
    "tests/run-west-test-runtime-red-contract.sh",
    "tests/run-west-test-stacked-omission-contract.sh",
    "tests/run-west-test-worktree-cleanup-contract.sh",
    "tests/run-west-update-parallel-contract.sh",
    "tests/run-patch-stack-mutation-contract.sh",
    "tests/run-lifecycle-explorer-contract.sh",
    "tests/run-lifecycle-fuzz-contract.sh",
    "tests/run-lifecycle-operation-boundary-contract.sh",
    "tests/run-dwdiag-runner-tests-contract.sh",
    "tests/run-manifest-source-mode-contract.sh",
    "tests/run-darling-bootstrap-contract.sh",
    "tests/run-lifecycle-trace-contract.sh",
    # Documented in AGENTS.md as focused contracts but invoked by no entrypoint.
    # run-west-test-testkit-contract.sh is the root of a family: the guest-macho,
    # guest-toolchain, darling-c-test, runtime-build and macho-corpus contracts
    # are only named by it, so registering it makes all of them reachable again.
    "tests/run-west-patch-verify-contract.sh",
    "tests/run-west-test-testkit-contract.sh",
    "tests/run-west-test-add-compat-cmake-contract.sh",
    "tests/run-west-test-gc-contract.sh",
    "tests/run-west-test-guarded-timeout-contract.sh",
    "tests/run-west-test-guest-command-contract.sh",
    "tests/run-west-test-prefix-cleanup-contract.sh",
    "tests/run-clt-provenance-contract.sh",
    "tests/run-rootless-cleanup-contract.sh",
    "tests/run-rootless-prefix-contract.sh",
    # Repaired after they were found failing: the two inventory registries were
    # stale against the tree and the archive-forensic contract had frozen on a
    # superseded lock. All three pass now, so they belong in the sweep.
    "tests/run-legacy-runtime-inventory-contract.sh",
    "tests/run-namespace-writer-inventory-contract.sh",
    "tests/run-perf-archive-forensic-contract.sh",
    # Proves every declared test is selectable by an invocation the census can
    # name. Host-only and read-only: it reads fixture metadata in a temp dir and
    # never starts a prefix.
    "tests/run-test-selection-census-contract.sh",
    # Proves the checked-in registries derive from the tree, and that a drift in
    # any of them fails naming the disagreeing field.
    "tests/run-registry-derivation-contract.sh",
    # Proves the profile-composition derivation reports a drift against the field
    # that disagrees and refuses to rewrite a file whose values are current. A
    # stale receipt is caught end to end by the profile materialization the tier
    # already runs, which is how the drift this derivation fixes was found.
    "tests/run-profile-composition-derivation-contract.sh",
    # Proves apply and clean name every module that is out of state in one run,
    # and that collecting them mutates nothing.
    "tests/run-patch-apply-module-state-contract.sh",
    # Proves the export says which immutable bindings a series change left
    # behind, instead of leaving them to three gates that run much later.
    "tests/run-patch-series-bindings-contract.sh",
    # Proves the tier's own wiring: which commands it builds, in which order,
    # and with which arguments. It stubs every registered contract in a mirror
    # repo instead of driving the real tier, so it is cheap enough to run inside
    # the tier it checks - the registered names are read out of the runner
    # itself, so a contract added to the tier is stubbed rather than executed.
    "tests/run-ci-test-tiers-contract.sh",
)

# Contracts the tier runs as explicit commands rather than CONTRACTS entries,
# because they need this interpreter (a python module carries no exec bit) or
# because they must stay uncached. Each entry is
# (name, workspace-relative path, cacheable).
EXPLICIT_CONTRACTS = (
    # Uncached on purpose: cached host evidence must not bypass a current
    # metadata and applicability review.
    ("native-inventory", "tests/run-native-inventory-contract.sh", False),
    ("native-artifact", "tests/west_test_contracts/native_artifact_contract.py", True),
    ("native-transport", "tests/west_test_contracts/native_transport_contract.py", True),
    # Recovered: these contracts passed on this checkout and no entrypoint ran
    # them. Each was run before it was registered here. They need the
    # interpreter that runs this tier, which carries PyYAML, as the shell
    # contracts that call python3 already assume.
    ("dev-check", "tests/west_test_contracts/dev_check_contract.py", True),
    ("dev-start", "tests/west_test_contracts/dev_start_contract.py", True),
    ("dev-status", "tests/west_test_contracts/dev_status_contract.py", True),
    ("fresh-prefix", "tests/west_test_contracts/fresh_prefix_contract.py", True),
    ("host-tier", "tests/west_test_contracts/host_tier_contract.py", True),
    ("patch-check-exit", "tests/west_test_contracts/patch_check_exit_contract.py", True),
    (
        "perf-profile-integration",
        "tests/west_test_contracts/perf_profile_integration_contract.py",
        True,
    ),
    ("runtime-proof-state", "tests/west_test_contracts/runtime_proof_state_contract.py", True),
    ("runtime-source-cache", "tests/west_test_contracts/runtime_source_cache_contract.py", True),
    ("source-search", "tests/west_test_contracts/source_search_contract.py", True),
    # The applicability review commands and the prefix bootstrap guidance are
    # contracts added when those two defects were repaired; both run through
    # this interpreter.
    (
        "native-applicability-review",
        "tests/run-native-applicability-review-contract.sh",
        True,
    ),
    (
        "prefix-bootstrap-guidance",
        "tests/west_test_contracts/prefix_bootstrap_guidance_contract.py",
        True,
    ),
    # Extracted from west_commands/test.py when it came back inside its facade
    # budget; each covers the logic that moved, so nothing moved uncovered.
    ("ctest-selection-argv", "tests/run-west-test-ctest-selection-argv-contract.sh", True),
    ("prefix-lifecycle-helpers", "tests/run-west-test-prefix-lifecycle-helpers-contract.sh", True),
    ("runtime-plan-helpers", "tests/run-west-test-runtime-plan-helpers-contract.sh", True),
    ("cmake-fixture-backend", "tests/run-west-test-cmake-fixture-backend-contract.sh", True),
    # The facade budget is met again, so this guard belongs in the sweep rather
    # than in the exclusion list.
    (
        "test-facade-ownership",
        "tests/west_test_contracts/test_facade_ownership_contract.py",
        True,
    ),
    # Recovered from a chain nothing executed: these eleven modules were named
    # only from inside tests/run-west-test-metadata-contract.sh, which no
    # entrypoint ran (its one caller passed --transport-gate-probe and returned
    # before the body), so two of them had rotted to AttributeError unseen.
    # They are registered individually here because the chain as a whole cannot
    # complete on a workstation without mirror access.
    ("metadata-selection", "tests/west_test_contracts/selection_contract.py", True),
    (
        "metadata-display",
        "tests/west_test_contracts/metadata_display_contract.py",
        True,
    ),
    (
        "metadata-runtime-profile",
        "tests/west_test_contracts/metadata_runtime_profile_contract.py",
        True,
    ),
    (
        "metadata-runtime-profile-red",
        "tests/west_test_contracts/metadata_runtime_profile_red_contract.py",
        True,
    ),
    (
        "metadata-source-profile",
        "tests/west_test_contracts/metadata_source_profile_contract.py",
        True,
    ),
    (
        "runtime-profile-current-minus",
        "tests/west_test_contracts/runtime_profile_current_minus_contract.py",
        True,
    ),
    (
        "runtime-evidence",
        "tests/west_test_contracts/runtime_evidence_contract.py",
        True,
    ),
    (
        "host-trace-failure-phase",
        "tests/west_test_contracts/host_trace_failure_phase_contract.py",
        True,
    ),
    (
        "guest-trace-failure-phase",
        "tests/west_test_contracts/guest_trace_failure_phase_contract.py",
        True,
    ),
    ("eunion-boot", "tests/west_test_contracts/eunion_boot_contract.py", True),
    ("eunion-prereq", "tests/west_test_contracts/eunion_prereq_contract.py", True),
)

# Contracts deliberately kept out of the tier. Each entry states the reason,
# and the census below fails the tier if a contract is in neither this mapping
# nor CONTRACTS: an unaccounted contract silently proves nothing.
EXCLUDED_CONTRACTS = {
    "tests/run-dtape-kqchan-modify-runtime-contract.sh":
        "guest-runtime gate: it host-builds a prebuilt guest Mach-O fixture and executes it in a "
        "booted Darling prefix (DPREFIX) whose darlingserver is under test, then asserts the "
        "server's own kqchan modify debug lines. The host tier has no prefix lifecycle and no guest "
        "runtime; run it with DARLING_BUILD_DIR and DPREFIX from the prefix-backed lane",
    "tests/run-dtape-kqchan-fill-context-contract.sh":
        "compiles the real XNU-flavoured duct-tape objects the kqchan read/fill path runs on "
        "(duct-tape/src/kqchan.c, duct-tape/xnu ipc_pset.c, ipc_mqueue.c, mach_msg.c), which needs "
        "a configured product build dir for its generated headers and its recorded compile "
        "commands. It is a deterministic host contract (two contexts K != R) but build-dir backed: "
        "run it with DARLING_BUILD_DIR=<configured build> from the prefix-backed lane. The barer "
        "host tier has no product build",
    "tests/run-ios-unfair-lock-contract.sh":
        "guest-runtime gate: it host-builds a guest Mach-O fixture (the same recorded-command replay "
        "the DTAPE runtime contracts use) and executes it in a booted Darling prefix whose "
        "libsystem_platform is under test, then requires the fixture's own pass marker. It reproduces "
        "the unfair-lock trap seen in Apple's ld during T4 without the Apple toolchain. The host tier "
        "has no product build, no prefix lifecycle and no guest runtime; run it with "
        "DARLING_BUILD_DIR=<configured build> and DPREFIX from the prefix-backed lane",
    "tests/run-ios-toolchain-contract.sh":
        "guest-runtime gate: it executes the REAL Apple clang from an Xcode tree inside a booted "
        "Darling prefix (DPREFIX) and compiles arm64 iPhoneOS objects against that Xcode's "
        "iPhoneOS SDK, then asserts the Mach-O architecture, platform and SDK metadata. It needs "
        "XCODE_APP, a prefix lifecycle and guest runtime that the host tier does not have; run it "
        "from the prefix-backed lane with DPREFIX and XCODE_APP set",
    "tests/run-dar-dles-plane-fairness-contract.sh":
        "guest-runtime gate: it executes the prebuilt guest Mach-O ring_mach_msg_test fixture "
        "(pthread_live mode) in a booted Darling prefix whose darlingserver is under test and "
        "requires bounded management-plane progress under continuous ring load - every "
        "pthread_create returns and every worker starts and joins at the requested simultaneous "
        "live-thread counts. The host tier has no prefix lifecycle and no guest runtime; run it "
        "with DPREFIX from the prefix-backed lane",
    "tests/run-dtape-kqchan-fill-runtime-contract.sh":
        "guest-runtime gate: it host-builds a prebuilt guest Mach-O fixture that registers "
        "EVFILT_MACHPORT and receives a message through the kqchan read path, executes it in a "
        "booted Darling prefix (DPREFIX), and asserts the server's own read-path evidence. The host "
        "tier has no prefix lifecycle and no guest runtime; run it with DARLING_BUILD_DIR and "
        "DPREFIX from the prefix-backed lane",
    "tests/run-west-test-metadata-contract.sh":
        "cannot complete without the immutable mirror: its west steps materialize profiles whose "
        "lock-first batches fetch refs/tags/patch-stack/* from the mirror. That fetch is bounded "
        "and non-interactive now, so it fails closed with a named unreachable-mirror error rather "
        "than hanging, but a workstation with no mirror access still cannot run the chain to the "
        "end. Its eleven python contracts are registered individually in EXPLICIT_CONTRACTS so "
        "they execute in the tier; this runner is what drives the remaining west and "
        "west patch check steps where the mirror is reachable",
    "tests/run-objc4-macro-contract.sh":
        "requires OBJC4_MACRO_CONTRACT_CANDIDATE, a reviewed objc4 source tree supplied by the operator",
    "tests/run-lifecycle-real-kernel-contract.sh":
        "runs a privileged cgroup-v2 fixture (bounded sudo) and needs an interpreter with os.pidfd_open",
    "tests/run-guest-syscall-trace-contract.sh":
        "guest-runtime gate: it drives booted-prefix workload runs through scripts/dwdiag (--prefix/"
        "DPREFIX) to prove the guest Darwin-syscall tracer preserves the guest, actually traces, and "
        "does not trace its own output. The host tier has no prefix lifecycle and no guest runtime, so "
        "it is exercised by the prefix-backed lane that owns the Ring/plane work instead",
    "tests/west_test_contracts/a0_typed_wake_fault_hook_contract.py":
        "argparse tool, not a self-running contract: it needs --source, the materialized "
        "darlingserver source root an operator selects",
    "tests/west_test_contracts/native_bundle_contract.py":
        "argparse tool, not a self-running contract: it needs --work-dir and a real Mac, "
        "which docs/test-infra.md names as its evidence boundary",
    "tests/west_test_contracts/v6_publication_closure_contract.py":
        "argparse tool, not a self-running contract: it needs --closure and --lock to name "
        "the publication closure and revisions under review",
}


def _contract_files(tests_dir: Path) -> dict[str, Path]:
    """Map the name of every contract file the census covers to its path.

    Two classes: shell runners and the framework contracts they call, which no
    runner has to name because the tier runs them through the interpreter.
    """
    files = {path.name: path for path in tests_dir.glob("run-*contract*.sh")}
    files.update(
        {path.name: path for path in (tests_dir / "west_test_contracts").glob("*contract*.py")}
    )
    return files


def _contract_family(name: str) -> str | None:
    """Return the family a contract file belongs to, or None.

    tests/run-<family>-contract.sh runs
    tests/west_test_contracts/<family>_contract.py, so a decision that keeps a
    runner out of the tier keeps the module behind it out too.
    """
    runner = re.fullmatch(r"run-(.+)-contract\.sh", name)
    if runner is not None:
        return runner.group(1).replace("-", "_")
    module = re.fullmatch(r"(.+)_contract\.py", name)
    if module is not None:
        return module.group(1)
    return None


# Flags that turn a contract invocation into a probe: a probe prints a marker
# and returns before the contract body runs.
PROBE_FLAGS = (
    "--transport-gate-probe",
    "--self-contract-probe",
    "--metadata-display-contract-probe",
    "--probe",
)


def _is_assignment_only(line: str) -> bool:
    """Whether a shell line assigns variables and then runs nothing.

    Environment prefixes are not assignments in this sense: a line that starts
    with ``PYTHONDONTWRITEBYTECODE=1 python3 -B ...`` runs python, while
    ``contract="$repo/tests/run-x-contract.sh"`` only stores a path for a later
    call somewhere else. Only the second is a naming that proves nothing.
    """
    rest = line.strip()
    while True:
        match = re.match(
            r"(export\s+)?[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s*",
            rest,
        )
        if match is None:
            break
        rest = rest[match.end() :]
    return rest == "" or rest.startswith("#")


def _invokes(text: str, name: str) -> bool:
    """Whether text names a contract on a line that is an actual invocation.

    The census credits a naming edge only when it can see an invocation of the
    contract by path. Naming it in a variable assignment, or only on a line that
    passes a probe flag, does not mean the contract runs: a probe returns before
    the body. tests/run-west-job-contract.sh showed exactly that failure - it
    assigned the metadata chain to a variable and called it only with
    --transport-gate-probe, so the chain and the eleven contracts it drives were
    counted as covered while nothing executed them.
    """
    for line in text.splitlines():
        if name not in line:
            continue
        stripped = line.strip()
        # A comment never invokes anything. Without this the census could be
        # satisfied by writing a contract's name in a comment - which is how a
        # comment in this very file kept the metadata chain "covered" while the
        # tightened rule was being written.
        if stripped.startswith("#"):
            continue
        if _is_assignment_only(line):
            continue
        if any(flag in line for flag in PROBE_FLAGS):
            continue
        return True
    return False


def unaccounted_contracts(tests_dir: Path) -> list[str]:
    """Return contract files that no entrypoint accounts for.

    A contract counts as accounted for when this tier registers it, when
    EXCLUDED_CONTRACTS lists it or the runner of its family with a reason, when
    CI or patch metadata names it, or when a contract this tier runs names it:
    a registered contract that invokes another contract runs that one too.
    A mention in an excluded contract does not count, since that contract does
    not run.
    """
    workspace = tests_dir.parent
    contracts = _contract_files(tests_dir)
    excluded = {Path(contract).name for contract in EXCLUDED_CONTRACTS}
    accounted = (
        {Path(contract).name for contract in CONTRACTS}
        | {Path(path).name for _, path, _ in EXPLICIT_CONTRACTS}
        | excluded
    )
    excluded_families = {
        family for family in map(_contract_family, excluded) if family is not None
    }
    for name in contracts:
        family = _contract_family(name)
        if family is not None and family in excluded_families:
            accounted.add(name)
    sources = list((workspace / "ci").rglob("*.py"))
    sources += list((workspace / "ci").rglob("*.sh"))
    sources += list((workspace / ".github" / "workflows").glob("*"))
    # Only real patch declarations count. Tests synthesize temporary profiles
    # under patches/__* while they run; a fixture is not a declaration, and one
    # of those leftovers was crediting the metadata chain for a while.
    sources += [
        path
        for path in (workspace / "patches").glob("*/patches.yml")
        if not path.parent.name.startswith("__")
    ]
    sources += [workspace / contract for contract in CONTRACTS]
    sources += [workspace / path for _, path, _ in EXPLICIT_CONTRACTS]
    scanned: set[Path] = set()
    while sources:
        source = sources.pop()
        try:
            identity = source.resolve()
        except OSError:
            continue
        if identity in scanned:
            continue
        scanned.add(identity)
        try:
            text = source.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for name, path in contracts.items():
            if name in accounted:
                continue
            if _invokes(text, name):
                accounted.add(name)
                sources.append(path)
    return sorted(name for name in contracts if name not in accounted)


class HostCommand(NamedTuple):
    name: str
    argv: list[str]
    cacheable: bool
    # How many of the tier's worker slots this command holds while it runs. A
    # command that drives other contracts or a nested tier costs more than a
    # single slot, because a flat pool of cpu_count workers lets several of
    # them oversubscribe the machine at once. Load failures that come from
    # scheduling are indistinguishable from regressions to whoever reads the
    # result, which is why the budget is weighted rather than counted in
    # processes.
    weight: int = 1


# Commands that hold more than one slot. Every entry states the measured reason,
# because a weight nobody can check is worse than no weight at all: a command
# that drives N other runners makes N times the process load of a leaf contract.
#
# Measured on this checkout by counting the runner/tier invocations in the
# command's own file:
#   run-west-test-metadata-contract.sh        42  (drives ~40 west steps and 11 contracts)
#   run-ci-test-tiers-contract.sh             42  (drives the tier itself)
#   run-west-test-testkit-contract.sh         11  (drives five sibling contracts)
#   tests/west_test_contracts/dev_check_contract.py  34 subprocess sites, and it
#                                                  failed in-tier at both 8 and 4 workers
#   tests/west_test_contracts/ctest_backend_contract.py  9 subprocess sites
#   run-west-job-contract.sh                   1  (drives a contract and supervises a job)
# The two profile sweeps in main() each materialize a profile and run a whole
# host metadata sweep, so they are weighted too. run-ci-test-tiers-contract.sh
# counts 42 references to other runners in its source but is not weighted: it
# stubs them in a mirror repository, so it costs one slot. The remaining
# 42-invocation command, run-west-test-metadata-contract.sh, is not in this
# table because the tier does not run it yet, and a weight keyed on a command
# that does not exist is a declaration that silently does nothing.
CONTRACT_WEIGHTS: dict[str, int] = {
    "run-west-test-testkit-contract": 4,
    "dev-check": 2,
    "run-west-test-ctest-backend-contract": 2,
    "run-west-job-contract": 2,
    "homebrew-host-metadata": 3,
    "wget-residual-host-metadata": 3,
}


class WeightedSlots:
    """A slot budget whose permits are taken and returned in weighted amounts.

    ``threading.Semaphore.acquire`` takes exactly one permit and only its
    ``blocking``/``timeout`` arguments can be passed, so a command that costs N
    slots needs its own counter: acquiring N at once is what keeps a command
    driving other runners from being scheduled beside a peer.
    """

    def __init__(self, total: int) -> None:
        self._condition = threading.Condition()
        self._free = total

    def acquire(self, cost: int) -> None:
        with self._condition:
            while self._free < cost:
                self._condition.wait()
            self._free -= cost

    def release(self, cost: int) -> None:
        with self._condition:
            self._free += cost
            self._condition.notify_all()


def _validate_weights(commands: list[HostCommand]) -> list[str]:
    """Return weight-table keys that name no command.

    A weight keyed on a name no command has would be silently ignored, and the
    command it was meant to slow down would keep oversubscribing the machine.
    """
    names = {command.name for command in commands}
    return sorted(name for name in CONTRACT_WEIGHTS if name not in names)


def _weight_for(name: str) -> int:
    return CONTRACT_WEIGHTS.get(name, 1)


def _explicit_command(name: str, path: str, cacheable: bool) -> HostCommand:
    """Build the command that runs a workspace-relative contract path."""
    argv = [str(ROOT / path)]
    if path.endswith(".py"):
        argv = [sys.executable, "-B", *argv]
    return HostCommand(name, argv, cacheable, _weight_for(name))


def _worker_count(command_count: int) -> int:
    default = min(8, max(1, os.cpu_count() or 1), command_count)
    value = os.environ.get("DARLING_HOST_TIER_WORKERS", str(default))
    try:
        workers = int(value)
    except ValueError as error:
        raise SystemExit("DARLING_HOST_TIER_WORKERS must be an integer") from error
    if not 1 <= workers <= command_count:
        raise SystemExit(
            f"DARLING_HOST_TIER_WORKERS must be between 1 and {command_count}"
        )
    return workers


def _cache_marker(
    root: Path,
    cache_key: str,
    command: HostCommand,
) -> Path:
    identity = hashlib.sha256(
        json.dumps(
            {"cache_key": cache_key, "name": command.name, "argv": command.argv},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return root / f"{identity}.json"


def _read_cache_marker(
    marker: Path,
    cache_key: str,
    command: HostCommand,
) -> bool:
    if not marker.exists():
        return False
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError(f"host tier cache marker is not a regular file: {marker}")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"host tier cache marker is invalid: {marker}: {error}") from error
    expected = {
        "schema_version": 1,
        "kind": "west-host-tier-result",
        "cache_key": cache_key,
        "name": command.name,
        "argv": command.argv,
        "returncode": 0,
    }
    if value != expected:
        raise RuntimeError(f"host tier cache marker identity differs: {marker}")
    return True


def _write_cache_marker(
    marker: Path,
    cache_key: str,
    command: HostCommand,
) -> None:
    value = {
        "schema_version": 1,
        "kind": "west-host-tier-result",
        "cache_key": cache_key,
        "name": command.name,
        "argv": command.argv,
        "returncode": 0,
    }
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(marker)


def run_commands(
    commands: list[HostCommand],
    *,
    workers: int,
    cache_root: Path | None,
    cache_key: str | None,
) -> int:
    if not 1 <= workers <= len(commands):
        raise ValueError("host tier worker count is outside the command range")
    if (cache_root is None) != (cache_key is None):
        raise ValueError("host tier cache root and key must be configured together")
    if cache_key is not None and re.fullmatch(r"[0-9a-f]{64}", cache_key) is None:
        raise ValueError("host tier cache key must be a lowercase SHA-256")
    if cache_root is not None:
        cache_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if cache_root.is_symlink() or not cache_root.is_dir():
            raise ValueError("host tier cache root must be a real directory")

    stop = threading.Event()
    state_lock = threading.Lock()
    output_lock = threading.Lock()
    active: set[subprocess.Popen[bytes]] = set()
    slots = WeightedSlots(workers)
    print(
        "host tier slots: "
        f"workers={workers} commands={len(commands)} "
        f"weighted_load={sum(command.weight for command in commands)}",
        flush=True,
    )

    def signal_processes(
        processes: tuple[subprocess.Popen[bytes], ...],
        sig: int,
    ) -> None:
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass

    def execute(command: HostCommand) -> tuple[HostCommand, int]:
        if stop.is_set():
            return command, 0
        started = time.monotonic_ns()
        cache_state = "disabled"
        lock_descriptor: int | None = None
        marker: Path | None = None
        try:
            if command.cacheable and cache_root is not None and cache_key is not None:
                marker = _cache_marker(cache_root, cache_key, command)
                lock_path = marker.with_suffix(".lock")
                lock_descriptor = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                if _read_cache_marker(marker, cache_key, command):
                    cache_state = "hit"
                    returncode = 0
                    return command, returncode
                cache_state = "miss"
            # The slots bound process load, not concurrency: a command that
            # drives other runners holds as many slots as it costs, so the
            # tier cannot run several of them beside each other by accident.
            cost = min(command.weight, workers)
            slots.acquire(cost)
            try:
                if stop.is_set():
                    return command, 0
                process = subprocess.Popen(command.argv, cwd=ROOT, start_new_session=True)
                with state_lock:
                    active.add(process)
                    cancelled = stop.is_set()
                if cancelled:
                    signal_processes((process,), signal.SIGTERM)
                returncode = process.wait()
                with state_lock:
                    active.discard(process)
            finally:
                slots.release(cost)
            if returncode:
                stop.set()
            elif marker is not None and cache_key is not None:
                _write_cache_marker(marker, cache_key, command)
            return command, returncode
        finally:
            if lock_descriptor is not None:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)
            elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
            with output_lock:
                print(
                    f"host tier command complete: {command.name} "
                    f"elapsed_ms={elapsed_ms} cache={cache_state}",
                    flush=True,
                )

    failure: tuple[HostCommand, int] | None = None
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Light commands are submitted first: a heavy command that has to
            # wait for slots would otherwise hold a worker thread while light
            # commands queue behind it, which costs wall time and buys nothing.
            ordered = sorted(commands, key=lambda command: command.weight)
            futures = [pool.submit(execute, command) for command in ordered]
            for future in as_completed(futures):
                command, returncode = future.result()
                if returncode and failure is None:
                    failure = command, returncode
                    with state_lock:
                        processes = tuple(active)
                    signal_processes(processes, signal.SIGTERM)
    except KeyboardInterrupt:
        stop.set()
        with state_lock:
            processes = tuple(active)
        signal_processes(processes, signal.SIGINT)
        return 130

    if failure is not None:
        command, returncode = failure
        print(
            f"host tier command failed ({returncode}): {' '.join(command.argv)}",
            file=sys.stderr,
        )
        return returncode if 0 < returncode < 256 else 1
    return 0


def build_commands(argv: list[str], *, prematerialized: str | None) -> list[HostCommand]:
    """Build the host tier's command list.

    Kept separate from ``main`` so a contract can check the weight table against
    the commands the tier actually runs: a weight keyed on a name that no
    command has would be silently ignored.
    """
    commands = [
        HostCommand(
            Path(contract).stem,
            [str(ROOT / contract)],
            True,
            _weight_for(Path(contract).stem),
        )
        for contract in CONTRACTS
    ]
    commands += [
        _explicit_command(name, path, cacheable)
        for name, path, cacheable in EXPLICIT_CONTRACTS
    ]
    profile_materialization = [] if prematerialized == "homebrew" else ["--materialize-profile"]
    commands.append(
        HostCommand(
            "homebrew-host-metadata",
            [
                "west",
                "test",
                "--profile",
                "homebrew",
                "--env",
                "host",
                *profile_materialization,
                *argv,
            ],
            False,
            _weight_for("homebrew-host-metadata"),
        )
    )
    # The E-UNION host suites assert behaviour that only the wget-residual chain
    # provides, so they are declared in that profile and run here. Measured at
    # about 100 seconds end to end, most of it materialization.
    wget_materialization = (
        [] if prematerialized == "wget-residual" else ["--materialize-profile"]
    )
    commands.append(
        HostCommand(
            "wget-residual-host-metadata",
            [
                "west",
                "test",
                "--profile",
                "wget-residual",
                "--env",
                "host",
                *wget_materialization,
                *argv,
            ],
            False,
            _weight_for("wget-residual-host-metadata"),
        )
    )
    return commands


def main() -> int:
    unaccounted = unaccounted_contracts(ROOT / "tests")
    if unaccounted:
        print(
            "host tier contract census failed: these contracts are in neither CONTRACTS nor "
            "EXCLUDED_CONTRACTS, so nothing runs them:\n  " + "\n  ".join(unaccounted),
            file=sys.stderr,
        )
        return 2
    commands = build_commands(
        sys.argv[1:], prematerialized=os.environ.get("WEST_PREMATERIALIZED_PROFILE")
    )
    unknown_weights = _validate_weights(commands)
    if unknown_weights:
        print(
            "host tier weight table names commands that do not exist: "
            + ", ".join(unknown_weights),
            file=sys.stderr,
        )
        return 2
    raw_cache_root = os.environ.get("WEST_HOST_CONTRACT_CACHE_DIR")
    raw_cache_key = os.environ.get("WEST_HOST_CONTRACT_CACHE_KEY")
    return run_commands(
        commands,
        workers=_worker_count(len(commands)),
        cache_root=Path(raw_cache_root) if raw_cache_root else None,
        cache_key=raw_cache_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())

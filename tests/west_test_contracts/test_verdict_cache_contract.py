"""Prove metadata verdict reuse keys on identity and never serves what it cannot justify.

A reused verdict is only defensible while the identity is conservative and the
store never presents a cached pass as a fresh one. Both are asserted here: every
declared input must move the key, an entry that is not demonstrably complete for
exactly this identity must be a miss, a non-zero verdict must never be served,
the store must be bounded and must state its own accounting, and the kill switch
must force a real execution and say so.

The last section runs the real metadata loop (``DarlingTest._run_metadata_tests``
with the real ``_test_invocation``) against a host test, so the behaviour the
acceptance run depends on - a second identical run reports cached and does not
execute, a changed asset or a changed runtime profile executes again - is proved
on the loop that dispatches tests, not on a copy of it. The fixture runner and
the prefix machinery are stubbed, so no Darling prefix is needed or started.

Every check is also run against a named deliberately broken arm. ``--arm <name>``
(or ``WEST_TEST_VERDICT_CONTRACT_ARM=<name>``) applies one defect and runs the
checks, which must fail: that is the RED arm of this contract. Without an arm the
contract proves each named defect is caught by the check that names it, so a
future edit cannot quietly make the checks insensitive.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import types
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ``west_commands/test.py`` is a West extension module, so it imports ``west``
# even when it is only being inspected from the host tier.
west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    def die(self, message):
        raise SystemExit(message)


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

sys.path.insert(0, str(ROOT / "west_commands"))

import test_stock_stack_cache  # noqa: E402
import test_verdict_cache as verdicts  # noqa: E402
from test_store import canonical_digest  # noqa: E402
from test import DarlingTest  # noqa: E402

ARM_ENV = "WEST_TEST_VERDICT_CONTRACT_ARM"
DEFAULT = object()

ASSET = "tests/shmem_ring_fd_ownership_guest.c"
DECLARED_NAME = "comparison_on_ring_fd_lifetime"

GUEST_TEST = {
    "name": DECLARED_NAME,
    "kind": "guest",
    "coverage-tier": "runtime",
    "runs": "guest",
    "diag": "guarded",
    "runner": "guest-c-fixture",
    "repo": "darling-workspace",
    "script": ASSET,
    "ok-marker": "RING_FD_INHERITANCE_OK",
    "compile-flags": ["-std=gnu11", "-Wall", "-Werror"],
    "guest-env-vars": {"RING_FD_MODE": "inherit"},
    "requires": ["darling-prefix", "homebrew-lz4"],
    "runtime-profile": "homebrew-ring-on",
    "timeout-seconds": 120,
}

RUNTIME = {
    "profile": "homebrew-ring-on",
    "identity": {
        "schema": 2,
        "profile": "homebrew-ring-on",
        "source-profile": "ring-comparison",
        "source-lock-sha256": "a" * 64,
        "source-commits": {"darling": "b" * 40, "darlingserver": "c" * 40},
        "patchsets": [
            {
                "profile": "ring-comparison",
                "sha256": "d" * 64,
                "patches": [
                    {
                        "module": "darlingserver",
                        "path": "ring-comparison/darlingserver/ring.patch",
                        "source-base": "e" * 40,
                        "source-commit": "f" * 40,
                        "sha256sum": "0" * 64,
                    }
                ],
            }
        ],
        "runtime-manifest-sha256": "1" * 64,
        "runtime-profile-definition-sha256": "2" * 64,
        "launcher-sha256": "3" * 64,
    },
    "proof": {
        "source-modules": ["darling"],
        "runtime-artifacts": [{"build-targets": ["darlingserver"]}],
        "cmake-defines": {"DARLING_RING_TRANSPORT": "ON"},
        "launcher-env": {"DARLING_RING_TRANSPORT": "1"},
    },
}

STACK = {
    "layout": 1,
    "arch": "x86_64",
    "pinned": {
        "brew-commit": "a" * 40,
        "homebrew-core-commit": "b" * 40,
        "portable-ruby-version": "3.3.0",
        "inputs": {"brew.tar.gz": "c" * 64},
    },
    "declared-formulas": {"lz4": ["1.10.0"]},
    "guest-toolchain": {"selected": "clt13.2", "receipt-sha256": "d" * 64},
    "runtime": "e" * 64,
}

TOOLCHAIN = {
    "selected": "CommandLineTools13.2",
    "selected-sha256": "1" * 64,
    "reviewed-packages": {"CommandLineTools13.2": "2" * 64},
    "receipt-sha256": "3" * 64,
    "canonical-link": "Library/Developer/CommandLineTools",
    "canonical-compiler": True,
}

ENVIRON = {"WEST_STOCK_WGET_ITERATIONS": "12", "WEST_STOCK_REPLAY_PHASE": "install"}


def _mutated(value, path, replacement):
    """Return a deep copy of ``value`` with ``path`` replaced."""

    document = copy.deepcopy(value)
    target = document
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = replacement
    return document


@dataclass
class Scenario:
    """One guest test and the identities it is keyed on."""

    work: Path
    store: Path
    test: dict
    invocation: dict
    runtime: dict
    stack: dict
    toolchain: dict
    environ: dict

    def identity(
        self,
        *,
        test=DEFAULT,
        invocation=DEFAULT,
        runtime=DEFAULT,
        stack=DEFAULT,
        toolchain=DEFAULT,
        environ=DEFAULT,
        arch=DEFAULT,
    ):
        return verdicts.verdict_identity(
            test=self.test if test is DEFAULT else test,
            invocation=self.invocation if invocation is DEFAULT else invocation,
            runtime=self.runtime if runtime is DEFAULT else runtime,
            stack=self.stack if stack is DEFAULT else stack,
            toolchain=self.toolchain if toolchain is DEFAULT else toolchain,
            environ=self.environ if environ is DEFAULT else environ,
            arch="x86_64" if arch is DEFAULT else arch,
        )

    def key(self, **overrides) -> str:
        return verdicts.identity_key(self.identity(**overrides))


def build_scenario(work: Path) -> Scenario:
    """Create one project holding the declared test asset."""

    project = work / "darling-workspace"
    tests = project / "tests"
    tests.mkdir(parents=True)
    asset = tests / Path(ASSET).name
    asset.write_text("int main(void) { return 0; }\n")
    manifest = work / "manifest"
    manifest.mkdir()
    return Scenario(
        work=work,
        store=manifest / ".west-test" / "test-verdict-cache",
        test=dict(GUEST_TEST),
        invocation={
            "key": f"guest-c-fixture:darling-workspace:{ASSET}:[]:{{}}",
            "runner": "guest-c-fixture",
            "args": None,
            "shell": False,
            "cwd": str(project),
            "script_path": str(asset),
            "repo": "darling-workspace",
            "script": ASSET,
            "ok_marker": "RING_FD_INHERITANCE_OK",
            "timeout_seconds": 120,
            "diag": "guarded",
            "requires_resources": ["darling-prefix", "homebrew-lz4"],
            "name": DECLARED_NAME,
        },
        runtime=copy.deepcopy(RUNTIME),
        stack=copy.deepcopy(STACK),
        toolchain=copy.deepcopy(TOOLCHAIN),
        environ=dict(ENVIRON),
    )


def check_stable_key(work: Path) -> None:
    """The same inputs must key the same, wherever the run root happens to be."""

    scenario = build_scenario(work)
    key = scenario.key()
    assert key == scenario.key(), "identical inputs must produce a stable key"
    assert len(key) == 64 and key == key.lower(), key

    document = scenario.identity()
    declared = {
        "schema",
        "asset",
        "declaration",
        "runner",
        "runtime",
        "stack",
        "workload",
        "toolchain",
        "arch",
    }
    missing = declared - set(document)
    assert not missing, f"the identity document must declare {sorted(missing)}"
    assert document["runtime"] is not None and document["stack"] is not None

    # The asset is content-addressed, not path-addressed: the same declared
    # asset under another run root is the same asset, which is what makes a
    # repeat run of the same test hit instead of missing on a per-run path.
    moved = work / "moved" / "darling-workspace"
    shutil.copytree(scenario.work / "darling-workspace", moved)
    relocated = dict(
        scenario.invocation, cwd=str(moved), script_path=str(moved / ASSET)
    )
    assert scenario.key(invocation=relocated) == key, (
        "the same asset bytes under another run root must keep the key"
    )
    (moved / ASSET).write_text("int main(void) { return 1; }\n")
    assert scenario.key(invocation=relocated) != key, (
        "different asset bytes must not share a key"
    )


def check_invalidation(work: Path) -> None:
    """Every declared input must move the key, or a stale verdict is reused."""

    scenario = build_scenario(work)
    key = scenario.key()
    asset = scenario.work / "darling-workspace" / ASSET
    original = asset.read_text()

    def change_asset() -> dict:
        asset.write_text(original + "\n/* variant */\n")
        return {}

    def add_fixture_source() -> dict:
        header = scenario.work / "darling-workspace" / "tests" / "ring-extra.h"
        header.write_text("#define RING_EXTRA 1\n")
        return {
            "invocation": dict(
                scenario.invocation, source_files=["tests/ring-extra.h"]
            )
        }

    inv = scenario.invocation
    test = scenario.test
    mutations = (
        ("the test asset bytes", change_asset),
        ("an additional fixture source", add_fixture_source),
        (
            "the declared ok-marker",
            lambda: {"test": dict(test, **{"ok-marker": "RING_FD_OTHER_OK"})},
        ),
        (
            "the declared compile flags",
            lambda: {"test": dict(test, **{"compile-flags": ["-std=gnu11"]})},
        ),
        (
            "the declared guest environment",
            lambda: {"test": dict(test, **{"guest-env-vars": {"RING_FD_MODE": "legacy"}})},
        ),
        ("the runner arguments", lambda: {"invocation": dict(inv, args=["--mode", "bare"])}),
        ("the runner timeout", lambda: {"invocation": dict(inv, timeout_seconds=121)}),
        (
            "the expected marker the runner checks",
            lambda: {"invocation": dict(inv, ok_marker="RING_FD_OTHER_OK")},
        ),
        (
            "a source revision in the runtime identity",
            lambda: {
                "runtime": _mutated(
                    RUNTIME, ["identity", "source-commits", "darlingserver"], "9" * 40
                )
            },
        ),
        (
            "a patchset digest in the runtime identity",
            lambda: {
                "runtime": _mutated(RUNTIME, ["identity", "patchsets", 0, "sha256"], "8" * 64)
            },
        ),
        (
            "a runtime build define",
            lambda: {
                "runtime": _mutated(
                    RUNTIME, ["proof", "cmake-defines", "DARLING_RING_TRANSPORT"], "OFF"
                )
            },
        ),
        (
            "the stock stack snapshot identity",
            lambda: {"stack": _mutated(STACK, ["pinned", "brew-commit"], "7" * 40)},
        ),
        (
            "the resolved stack formulas",
            lambda: {"stack": _mutated(STACK, ["declared-formulas"], {"lz4": ["1.10.1"]})},
        ),
        (
            "the stock stack runtime digest",
            lambda: {"stack": _mutated(STACK, ["runtime"], "6" * 64)},
        ),
        (
            "the guest toolchain identity",
            lambda: {"toolchain": _mutated(TOOLCHAIN, ["receipt-sha256"], "5" * 64)},
        ),
        (
            "a shortened workload",
            lambda: {"environ": dict(ENVIRON, WEST_STOCK_WGET_ITERATIONS="2")},
        ),
        (
            "a different workload phase",
            lambda: {"environ": dict(ENVIRON, WEST_STOCK_REPLAY_PHASE="wget-repeat")},
        ),
        ("the host architecture", lambda: {"arch": "aarch64"}),
    )
    try:
        for label, mutate in mutations:
            overrides = mutate()
            try:
                changed = scenario.key(**overrides)
                assert changed != key, (
                    f"{label} must invalidate the verdict key"
                )
                assert scenario.key(**overrides) == changed, (
                    f"{label} must produce a stable key on recomputation"
                )
            finally:
                asset.write_text(original)
            assert scenario.key() == key, (
                f"undoing {label} must restore the baseline key"
            )
    finally:
        asset.write_text(original)

    # The workload variables are read from the effective environment, so an
    # operator who shortens or splits the run cannot inherit the acceptance
    # verdict, and an unset variable is recorded as such rather than dropped.
    ambient = verdicts.workload_parameters({"WEST_STOCK_WGET_ITERATIONS": "12"}, {})
    assert ambient["WEST_STOCK_WGET_ITERATIONS"] == "12", ambient
    effective = verdicts.workload_parameters(
        {"WEST_STOCK_WGET_ITERATIONS": "12"},
        {"env": {"WEST_STOCK_WGET_ITERATIONS": "2"}},
    )
    assert effective["WEST_STOCK_WGET_ITERATIONS"] == "2", effective
    split = verdicts.workload_parameters(
        {}, {"env": {"WEST_GUEST_C_FIXTURE_RUN_ONLY": "1"}}
    )
    assert split["WEST_GUEST_C_FIXTURE_RUN_ONLY"] == "1", split
    assert split["WEST_STOCK_WGET_ITERATIONS"] is None, split


def check_unkeyable(work: Path) -> None:
    """An identity that cannot be computed must be a miss, never a partial key."""

    scenario = build_scenario(work)
    assert scenario.identity() is not None
    # A CTest binding resolves no test asset bytes on the host.
    assert (
        scenario.identity(
            invocation={
                "key": "ctest:build:index",
                "runner": "ctest",
                "args": ["ctest", "-I", "3,3"],
                "timeout_seconds": 600,
            }
        )
        is None
    ), "an invocation without asset bytes must be unkeyable"
    # An asset whose bytes cannot be read is not an unchanged asset.
    assert (
        scenario.identity(
            invocation=dict(
                scenario.invocation,
                script_path=str(scenario.work / "darling-workspace" / "tests" / "gone.c"),
            )
        )
        is None
    ), "an unreadable asset must be unkeyable"


def check_marker_required(work: Path) -> None:
    """Only a completed entry for exactly this identity may be reused."""

    scenario = build_scenario(work)
    store = work / "store"
    key = scenario.key()
    entry = verdicts.entry_path(store, key)
    entry.mkdir(parents=True)
    (entry / "result.json").write_text('{"verdict": 0}')
    assert verdicts.read_verdict(store, key) is None, (
        "an entry without a completion marker must not be reused"
    )

    verdicts.write_entry(
        store,
        key,
        identity=scenario.identity(),
        verdict=0,
        duration_seconds=1.5,
        recorded_at="2026-09-15T07:00:00Z",
        ok_marker="RING_FD_INHERITANCE_OK",
        bundle="/home/operator/darling-debug/20260915T065958Z-west-test-ring",
        guest_stdout_sha256="a" * 64,
        provenance={"patch": "darling/x.patch", "test": DECLARED_NAME},
    )
    marker = verdicts.read_verdict(store, key)
    assert marker is not None, "a completed entry for this identity must be reused"
    assert marker["verdict"] == 0
    assert marker["recorded-at"] == "2026-09-15T07:00:00Z"
    assert marker["duration-seconds"] == 1.5
    assert marker["ok-marker"] == "RING_FD_INHERITANCE_OK"
    assert marker["bundle"].endswith("20260915T065958Z-west-test-ring")
    assert marker["guest-stdout-sha256"] == "a" * 64
    assert verdicts.read_verdict(store, "f" * 64) is None, (
        "another identity must not reuse this entry"
    )

    good = verdicts.marker_path(entry).read_text()
    damaged = (
        ("a corrupt marker", "{ not json"),
        ("a marker for another identity", good.replace(key, "f" * 64)),
        (
            "a marker without an identity document",
            json.dumps({**json.loads(good), "identity": {}}),
        ),
        (
            "a marker without a recorded-at stamp",
            json.dumps({**json.loads(good), "recorded-at": ""}),
        ),
        (
            "a marker with a malformed stdout digest",
            json.dumps({**json.loads(good), "guest-stdout-sha256": "short"}),
        ),
        (
            "a marker that disagrees with its own identity",
            json.dumps({**json.loads(good), "identity-digest": "f" * 64}),
        ),
        (
            "a marker from another schema",
            json.dumps({**json.loads(good), "schema": verdicts.SCHEMA + 1}),
        ),
    )
    try:
        for label, content in damaged:
            verdicts.marker_path(entry).write_text(content)
            assert verdicts.read_verdict(store, key) is None, (
                f"{label} must not be reused"
            )
    finally:
        verdicts.marker_path(entry).write_text(good)
    assert verdicts.read_verdict(store, key) is not None, (
        "restoring the marker must restore reuse"
    )

    # Publishing an entry under an identity it does not describe is refused:
    # otherwise a later read would serve a verdict for the wrong experiment.
    try:
        verdicts.write_entry(
            store, "0" * 64, identity=scenario.identity(), verdict=0, duration_seconds=1
        )
    except ValueError:
        pass
    else:
        raise AssertionError("an entry must not be published under a foreign key")


def check_nonzero_never_reused(work: Path) -> None:
    """A failure is not evidence about the identity, so it is never reused."""

    scenario = build_scenario(work)
    store = work / "store"
    key = scenario.key()
    verdicts.write_entry(
        store,
        key,
        identity=scenario.identity(),
        verdict=1,
        duration_seconds=2.0,
        recorded_at="2026-09-15T07:10:00Z",
        provenance={"patch": "darling/x.patch", "test": DECLARED_NAME},
    )
    assert verdicts.read_verdict(store, key) is None, (
        "a non-zero verdict must never be reused"
    )
    verdicts.write_entry(
        store,
        key,
        identity=scenario.identity(),
        verdict=0,
        duration_seconds=0.5,
        recorded_at="2026-09-15T07:12:00Z",
    )
    marker = verdicts.read_verdict(store, key)
    assert marker is not None and marker["verdict"] == 0, (
        "a later passing run must replace the failing record"
    )
    assert marker["recorded-at"] == "2026-09-15T07:12:00Z", marker


def check_bound_and_accounting(work: Path) -> None:
    """The store is bounded by least-recent use and states its own accounting."""

    manifest = work / "manifest"
    manifest.mkdir(parents=True)
    assert verdicts.cache_root(manifest, {}) == manifest / ".west-test" / "test-verdict-cache"
    assert not (manifest / ".west-test").exists(), (
        "resolving the store must not create it"
    )
    explicit = work / "elsewhere"
    assert (
        verdicts.cache_root(manifest, {"WEST_TEST_VERDICT_CACHE_DIR": str(explicit)})
        == explicit
    )

    assert verdicts.max_bytes({}) == verdicts.DEFAULT_MAX_BYTES
    assert verdicts.max_bytes({"WEST_TEST_VERDICT_CACHE_MAX_BYTES": "4096"}) == 4096
    assert verdicts.max_bytes({"WEST_TEST_VERDICT_CACHE_MAX_BYTES": "  "}) == (
        verdicts.DEFAULT_MAX_BYTES
    ), "an empty bound means the configured default, not zero"
    for bad in ("lots", "0", "-5"):
        try:
            verdicts.max_bytes({"WEST_TEST_VERDICT_CACHE_MAX_BYTES": bad})
        except ValueError:
            continue
        raise AssertionError(f"a bound of {bad!r} must be rejected")

    scenario = build_scenario(work / "scenario")
    store = work / "store"
    keys = []
    for index in range(3):
        identity = scenario.identity(
            environ=dict(ENVIRON, WEST_STOCK_WGET_ITERATIONS=str(10 + index))
        )
        key = verdicts.identity_key(identity)
        verdicts.write_entry(
            store,
            key,
            identity=identity,
            verdict=0,
            duration_seconds=float(index),
            recorded_at=f"2026-09-15T07:0{index}:00Z",
        )
        (verdicts.entry_path(store, key) / "payload.bin").write_bytes(b"v" * 4096)
        keys.append(key)
    sizes = {
        key: sum(
            path.stat().st_size
            for path in verdicts.entry_path(store, key).rglob("*")
            if path.is_file()
        )
        for key in keys
    }
    now = time.time()
    for offset, key in enumerate(keys):
        stamp = now - 600 + offset
        os.utime(verdicts.entry_path(store, key), (stamp, stamp))

    bound = sizes[keys[1]] + sizes[keys[2]]
    result = verdicts.prune(
        store, bound, protect=[verdicts.entry_path(store, keys[2])]
    )
    assert result["evicted"] == [f"verdict/{keys[0]}"], result
    assert result["evicted_bytes"] == sizes[keys[0]], result
    assert not verdicts.entry_path(store, keys[0]).exists()
    assert verdicts.read_verdict(store, keys[1]) is not None
    assert verdicts.read_verdict(store, keys[2]) is not None, (
        "a protected entry must survive the bound"
    )
    assert verdicts.read_stats(store).value("entries_evicted") == 1
    assert verdicts.read_stats(store).value("bytes_evicted") == sizes[keys[0]]

    # A reused verdict counts as recently used, so the bound evicts cold entries
    # rather than the entry the current run is serving.
    coldest, warm = verdicts.entry_path(store, keys[1]), verdicts.entry_path(store, keys[2])
    stamp = time.time() - 900
    os.utime(coldest, (stamp, stamp))
    os.utime(warm, (stamp, stamp))
    verdicts.read_verdict(store, keys[2])
    result = verdicts.prune(store, sizes[keys[2]])
    assert result["evicted"] == [f"verdict/{keys[1]}"], result
    assert verdicts.read_verdict(store, keys[2]) is not None

    verdicts.record_event(store, "verdict_hits")
    verdicts.record_event(store, "verdict_misses", 2)
    verdicts.record_event(store, "verdict_unkeyed")
    stats = verdicts.read_stats(store)
    assert stats.value("verdict_hits") == 1 and stats.value("verdict_misses") == 2
    assert stats.value("verdict_unkeyed") == 1
    assert stats.rate("verdict_hits", "verdict_misses") == 33.3, stats.counters
    report = verdicts.report(store)
    assert "1hit/2miss" in report, report
    assert "unkeyed=1" in report, report
    assert "skipped-host=0" in report, report
    assert str(store) in report, report

    try:
        verdicts.prune(store, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("a non-positive store bound must be rejected")


def check_kill_switch(work: Path) -> None:
    """The kill switch forces fresh execution and the store stays disabled."""

    manifest = work / "manifest"
    manifest.mkdir(parents=True)
    for value in ("off", "0", "no", "false", "", "OFF", " off "):
        assert verdicts.reuse_disabled({"WEST_TEST_VERDICT_CACHE": value}), value
        assert (
            verdicts.cache_root(manifest, {"WEST_TEST_VERDICT_CACHE": value}) is None
        ), value
    for value in ("1", "on", "yes", "true"):
        assert not verdicts.reuse_disabled({"WEST_TEST_VERDICT_CACHE": value}), value
        assert verdicts.cache_root(
            manifest, {"WEST_TEST_VERDICT_CACHE": value}
        ) == manifest / ".west-test" / "test-verdict-cache"
    assert not verdicts.reuse_disabled({}), "an unset switch must leave reuse on"
    try:
        verdicts.cache_root(manifest, {"WEST_TEST_VERDICT_CACHE_DIR": "relative/path"})
    except ValueError:
        pass
    else:
        raise AssertionError("a relative store root must be rejected")


HOST_PATCH = {"path": "notes/host-verdict.patch", "module": "notes"}
HOST_SCRIPT = "tests/host_verdict_smoke.sh"


def host_test(**overrides) -> dict:
    test = {
        "name": "host_verdict_smoke",
        "kind": "host",
        "env": "host",
        "runner": "script",
        "repo": "notes",
        "script": HOST_SCRIPT,
        "args": ["--mode", "smoke"],
        "timeout-seconds": 60,
    }
    test.update(overrides)
    return test


class Harness(DarlingTest):
    """Run the real metadata loop with the fixture runner and prefix stubbed."""

    def __init__(self, work: Path, *, result_rc: int = 0, prefix: Path | None = None):
        self.topdir = str(work)
        self.manifest = types.SimpleNamespace(
            repo_abspath=str(work / "manifest"), projects=[]
        )
        self._project_overrides = {
            name: work / name
            for name in ("notes", "darling-workspace", "darling")
        }
        self._prefix = None if prefix is None else str(prefix)
        self._bundle_root = str(work / "darling-debug")
        self.output: list[str] = []
        self.executions = 0
        self.result_rc = result_rc

    def inf(self, message):
        self.output.append(str(message))

    def err(self, message):
        self.output.append(str(message))

    def die(self, message):
        raise SystemExit(f"harness died: {message}")

    def _run_invocation(self, invocation, env=None):
        self.executions += 1
        return self.result_rc

    def output_mentions(self, needle: str) -> bool:
        return any(needle.lower() in line.lower() for line in self.output)

    def run(self, tests) -> int:
        return self._run_metadata_tests(list(tests), False, [])


@contextlib.contextmanager
def with_environ(values: dict):
    """Set environment values for one block, restoring them afterwards."""

    previous = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def kill_switch(value: str = "off"):
    return with_environ({"WEST_TEST_VERDICT_CACHE": value})


GUEST_PATCH = {"path": "darling/homebrew-prefix-tooling.patch", "module": "darling"}

PROFILE_DEFINITION = """\
runtime-profiles:
  homebrew-ring-on:
    source-profile: ring-comparison
    source-module: darling
    source-modules:
    - darling
    cmake-defines:
      DARLING_RING_TRANSPORT: true
    launcher-env:
      DARLING_ROOTLESS: '1'
    runtime-artifacts:
    - module: darling
      build-targets: [darlingserver]
      deploy: [usr/libexec/darling/darlingserver]
"""

PATCHSET = """\
version: 1
description: fabricated profile for the verdict cache contract
patches:
  - path: darling/ring-fd-ownership.patch
    module: darling
    source-branch: fix/ring-fd-ownership
    source-base: 9980737fcbfdd37f24118ae6b2ad26046f157f1a
    source-commit: 9ef131b2de45be69cdac0fb8c491a73e670b2e9e
    sha256sum: 64b3f523f70ca7427c3c961a5da72632685536345e9329307824720179ac4d45
"""


def _commit(repo: Path, message: str) -> str:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=contract@example.invalid",
            "-c",
            "user.name=verdict-contract",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            message,
        ],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def prepare_guest_workspace(work: Path, harness=Harness) -> Harness:
    """Create the project, prefix and metadata a guest runtime identity reads."""

    manifest = work / "manifest"
    (manifest / "testkit").mkdir(parents=True)
    (manifest / "testkit" / "runtime-profiles.yml").write_text(PROFILE_DEFINITION)
    profile = manifest / "patches" / "ring-comparison"
    profile.mkdir(parents=True)
    (profile / "patches.yml").write_text(PATCHSET)
    (manifest / "west.yml").write_text("manifest:\n  version: 0.13\n")

    project = work / "darling-workspace"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / Path(ASSET).name).write_text("int main(void) { return 0; }\n")

    dserver = work / "darling" / "src" / "external" / "darlingserver"
    (dserver / "tools").mkdir(parents=True)
    (dserver / "tools" / "darling-stat").write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=work / "darling",
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _commit(work / "darling", "baseline")

    prefix = work / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "darling").write_text("#!/bin/sh\nexit 0\n")
    return harness(work, prefix=prefix)


def check_guest_runtime_identity(work: Path) -> None:
    """A guest test is keyed on the runtime, the stock stack and the toolchain."""

    harness = prepare_guest_workspace(work)
    test = dict(GUEST_TEST)
    invocation = harness._test_invocation(GUEST_PATCH, test)
    identity = harness._metadata_verdict_identity(GUEST_PATCH, test, invocation)
    assert identity is not None, "a guest test with a declared provider must be keyable"
    # A real prefix retains the profile that provisioned it, which is never the
    # profile the test deploys; the identity has to survive that or every test
    # that consumes the stock stack is uncacheable. This is the shape that broke
    # reuse: the marker names the bootstrap provider, the test names the ring
    # profile, and asking under the deployed name yields no identity at all.
    retained = work / "retained-prefix"
    (retained / "bin").mkdir(parents=True)
    (retained / "bin" / "darling").write_text("#!/bin/sh\nexit 0\n")
    fingerprint = {"launcher-sha256": "e" * 64, "patchsets": []}
    (retained / ".west-runtime-profile.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "profile": "homebrew-lz4-source",
                "source-profile": "wget-residual",
                "fingerprint": fingerprint,
            }
        )
    )
    retained_harness = Harness(work, prefix=retained)
    retained_test = dict(GUEST_TEST)
    retained_invocation = retained_harness._test_invocation(GUEST_PATCH, retained_test)
    retained_identity = retained_harness._metadata_verdict_identity(
        GUEST_PATCH, retained_test, retained_invocation
    )
    assert retained_identity is not None, (
        "a prefix that retains its bootstrap provider must still key the stack"
    )
    assert retained_identity["stack"] is not None, retained_identity
    assert retained_identity["stack"]["runtime"] == canonical_digest(fingerprint), (
        "the stack identity must be the retained fingerprint"
    )
    assert (
        test_stock_stack_cache.stack_request(
            prefix=retained,
            manifest_repo=work / "manifest",
            topdir=work,
            profile_name="homebrew-ring-on",
            environ={},
        )
        is None
    ), "naming the deployed profile must not resolve: that is the trap this covers"
    runtime = identity["runtime"]
    assert runtime["profile"] == "homebrew-ring-on", runtime
    fingerprints = runtime["identity"]
    patchset_file = work / "manifest" / "patches" / "ring-comparison" / "patches.yml"
    assert fingerprints["patchsets"][0]["profile"] == "ring-comparison", fingerprints
    assert fingerprints["patchsets"][0]["sha256"] == hashlib.sha256(
        patchset_file.read_bytes()
    ).hexdigest(), "the runtime identity must cover the patchset digest"
    assert fingerprints["patchsets"][0]["patches"][0]["source-commit"] == (
        "9ef131b2de45be69cdac0fb8c491a73e670b2e9e"
    ), fingerprints
    assert fingerprints["source-commits"] == {
        "darling": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=work / "darling",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    }, "the runtime identity must cover the source revisions"
    assert fingerprints["launcher-sha256"] == hashlib.sha256(
        (work / "prefix" / "bin" / "darling").read_bytes()
    ).hexdigest(), fingerprints
    assert runtime.get("proof", {}).get("cmake-defines") == {
        "DARLING_RING_TRANSPORT": True
    }, ("the runtime identity must carry the deployment proof", runtime.get("proof"))

    # The stack and toolchain components are the identities the stock stack
    # cache already computes, not a second notion of the guest state.
    assert identity["stack"] == test_stock_stack_cache.stack_request(
        prefix=work / "prefix",
        manifest_repo=work / "manifest",
        topdir=work,
        profile_name="homebrew-ring-on",
        environ=os.environ,
    ), identity["stack"]
    assert identity["toolchain"] == test_stock_stack_cache.guest_toolchain_identity(
        work / "prefix", os.environ
    )

    key = verdicts.identity_key(identity)

    def rekey() -> str:
        current = harness._metadata_verdict_identity(
            GUEST_PATCH,
            test,
            harness._test_invocation(GUEST_PATCH, test),
        )
        assert current is not None
        return verdicts.identity_key(current)

    # A changed patchset (a different patch revision in the runtime's source
    # profile) must not reuse the verdict recorded against the old one.
    patchset_file.write_text(PATCHSET.replace("9ef131b2", "11111111"))
    assert rekey() != key, "a changed runtime patchset must invalidate the verdict"
    patchset_file.write_text(PATCHSET)
    assert rekey() == key, "restoring the patchset must restore the key"

    # A changed provider definition (a different runtime) must not either.
    definition_file = work / "manifest" / "testkit" / "runtime-profiles.yml"
    definition_file.write_text(
        PROFILE_DEFINITION.replace(
            "DARLING_RING_TRANSPORT: true", "DARLING_RING_TRANSPORT: false"
        )
    )
    assert rekey() != key, "a changed runtime profile must invalidate the verdict"
    definition_file.write_text(PROFILE_DEFINITION)
    assert rekey() == key, "restoring the definition must restore the key"

    # A CLI CMake override builds a different runtime through the same profile,
    # so it is part of the identity the deployment and the cache share.
    harness._runtime_cmake_define_overrides = {"DARLING_EXTRA": "1"}
    assert rekey() != key, "a runtime CMake override must invalidate the verdict"
    harness._runtime_cmake_define_overrides = {}
    assert rekey() == key

    # A guest test whose prefix identifies no runtime cannot be keyed: two
    # prefixes are then not known to be the same runtime.
    plain = {name: value for name, value in test.items() if name != "runtime-profile"}
    plain_invocation = harness._test_invocation(GUEST_PATCH, plain)
    assert (
        harness._metadata_verdict_identity(GUEST_PATCH, plain, plain_invocation) is None
    ), "a guest test without an identified runtime must be unkeyable"

    # A retained provider marker names that runtime, which is what makes the
    # same test keyable on a prefix that boots one.
    marker = work / "prefix" / ".west-runtime-profile.json"
    marker.write_text(
        json.dumps(
            {
                "schema": 2,
                "profile": "homebrew-ring-on",
                "source-profile": "ring-comparison",
                "fingerprint": fingerprints,
            }
        )
    )
    retained = harness._metadata_verdict_identity(
        GUEST_PATCH, plain, plain_invocation
    )
    assert retained is not None, "a retained provider must key the guest test"
    assert retained["runtime"]["profile"] is None, retained["runtime"]
    assert retained["runtime"]["identity-digest"] is not None
    marker.unlink()

    # Finally, an advancing source revision is a different runtime.
    _commit(work / "darling", "advance")
    assert rekey() != key, "a new source revision must invalidate the verdict"


class GuestHarness(Harness):
    """The real loop with the runtime deployment and resources stubbed.

    Nothing here starts a prefix or a guest stack: the runtime deployment and
    the stock stack resource are replaced by their identity, which the verdict
    cache keys on and which the identity checks assert directly.
    """

    @contextlib.contextmanager
    def _metadata_runtime_profile_context(self, patch, test):
        yield None

    @contextlib.contextmanager
    def _resource_context(self, invocation, env):
        yield env

    def _guest_c_fixture_prerequisite_problems(self, prefix, guest_cc, guest_cflags):
        return []


def check_workload_class(work: Path) -> None:
    """A cached pass names the workload it was observed under."""

    assert verdicts.workload_class({}) == verdicts.ACCEPTANCE_WORKLOAD
    assert verdicts.workload_class(
        {"WEST_STOCK_WGET_ITERATIONS": "12"}
    ) == verdicts.ACCEPTANCE_WORKLOAD, (
        "the acceptance repetition count is the acceptance workload"
    )
    for values in (
        {"WEST_STOCK_WGET_ITERATIONS": "2"},
        {"WEST_STOCK_WGET_ITERATIONS": "lots"},
        {"WEST_GUEST_C_FIXTURE_PREPARE_ONLY": "1"},
        {"WEST_GUEST_C_FIXTURE_RUN_ONLY": "1"},
    ):
        assert verdicts.workload_class(values) == verdicts.REDUCED_WORKLOAD, values
    # The phase selects which experiment runs, so it is not a shortening knob.
    assert verdicts.workload_class(
        {"WEST_STOCK_REPLAY_PHASE": "wget-repeat"}
    ) == verdicts.ACCEPTANCE_WORKLOAD

    scenario = build_scenario(work)
    acceptance = scenario.identity(environ=dict(ENVIRON))
    shortened = scenario.identity(
        environ=dict(ENVIRON, WEST_STOCK_WGET_ITERATIONS="2")
    )
    assert acceptance["workload"]["class"] == verdicts.ACCEPTANCE_WORKLOAD
    assert shortened["workload"]["class"] == verdicts.REDUCED_WORKLOAD
    reduced_key = verdicts.identity_key(shortened)
    assert reduced_key != verdicts.identity_key(acceptance), (
        "a shortened run must not key like the acceptance run"
    )
    assert reduced_key != scenario.key(
        environ=dict(ENVIRON, WEST_STOCK_WGET_ITERATIONS="3")
    ), "two different shortened workloads must not share a verdict"

    # The class travels with the entry and is reported when the entry is reused.
    store = work / "store"
    verdicts.write_entry(
        store,
        reduced_key,
        identity=shortened,
        verdict=0,
        duration_seconds=3.0,
        recorded_at="2026-09-15T07:20:00Z",
    )
    marker = verdicts.read_verdict(store, reduced_key)
    assert marker is not None
    assert marker["identity"]["workload"]["class"] == verdicts.REDUCED_WORKLOAD, marker
    summary = verdicts.reuse_summary(marker)
    assert verdicts.REDUCED_WORKLOAD in summary, summary
    assert "2026-09-15T07:20:00Z" in summary, summary
    assert "3.0s" in summary, summary


def check_loop_guest(work: Path) -> None:
    """The metadata loop reuses a guest verdict and executes on every change."""

    prepare_guest_workspace(work, GuestHarness)
    test = dict(GUEST_TEST)
    tests = [(GUEST_PATCH, test)]
    store = work / "manifest" / ".west-test" / "test-verdict-cache"
    script = work / "darling-workspace" / "tests" / Path(ASSET).name
    bundle = work / "darling-debug" / f"20260915T070000Z-west-test-{DECLARED_NAME}"
    bundle.mkdir(parents=True)
    (bundle / "stdout.log").write_text("RING_FD_INHERITANCE_OK\n")

    first = GuestHarness(work, prefix=work / "prefix")
    assert first.run(tests) == 0, first.output
    assert first.executions == 1, "the first guest run must execute the test"
    entries = list((store / "verdict").iterdir())
    assert len(entries) == 1, entries
    key = entries[0].name
    marker = verdicts.read_verdict(store, key)
    assert marker is not None and marker["verdict"] == 0
    identity = marker["identity"]
    assert (identity.get("asset") or [{}])[0].get("sha256") == hashlib.sha256(
        script.read_bytes()
    ).hexdigest(), identity
    assert identity.get("stack") is not None, (
        "a test that consumes homebrew-lz4 must key on the stock stack identity"
    )
    assert (identity.get("runtime") or {}).get("profile") == "homebrew-ring-on", identity
    assert (identity.get("workload") or {}).get("class") == (
        verdicts.ACCEPTANCE_WORKLOAD
    ), identity
    assert marker["ok-marker"] == "RING_FD_INHERITANCE_OK", marker
    assert marker["bundle"] == str(bundle), marker
    assert marker["guest-stdout-sha256"] == hashlib.sha256(
        b"RING_FD_INHERITANCE_OK\n"
    ).hexdigest(), marker

    second = GuestHarness(work, prefix=work / "prefix")
    assert second.run(tests) == 0, second.output
    assert second.executions == 0, "an unchanged guest test must not execute again"
    announced = "\n".join(second.output)
    assert key in announced, "a reused verdict must name its identity digest"
    assert marker["recorded-at"] in announced, (
        "a reused verdict must name the original run time"
    )
    assert verdicts.ACCEPTANCE_WORKLOAD in announced, (
        "a reused verdict must name the workload class it was observed under"
    )

    # A changed test asset is a different experiment, and the old verdict stays
    # addressable for the identity it describes.
    original = script.read_text()
    script.write_text(original + "\n/* variant */\n")
    changed = GuestHarness(work, prefix=work / "prefix")
    assert changed.run(tests) == 0, changed.output
    assert changed.executions == 1, "a changed test asset must execute again"
    again = GuestHarness(work, prefix=work / "prefix")
    assert again.run(tests) == 0, again.output
    assert again.executions == 0, "the changed asset must be recorded and reused"
    assert len(list((store / "verdict").iterdir())) == 2, entries
    script.write_text(original)
    assert verdicts.read_verdict(store, key) is not None, (
        "the first verdict must survive a second identity"
    )

    # A changed runtime profile is a different runtime.
    definition_file = work / "manifest" / "testkit" / "runtime-profiles.yml"
    definition_file.write_text(
        PROFILE_DEFINITION.replace(
            "DARLING_RING_TRANSPORT: true", "DARLING_RING_TRANSPORT: false"
        )
    )
    reprofiled = GuestHarness(work, prefix=work / "prefix")
    assert reprofiled.run(tests) == 0, reprofiled.output
    assert reprofiled.executions == 1, "a changed runtime profile must execute again"
    definition_file.write_text(PROFILE_DEFINITION)
    restored = GuestHarness(work, prefix=work / "prefix")
    assert restored.run(tests) == 0, restored.output
    assert restored.executions == 0, "restoring the profile must restore reuse"

    # The workload is part of the identity in both directions: a shortened run
    # must not inherit the acceptance verdict, and the acceptance run must not
    # inherit a shortened one.
    with with_environ({"WEST_STOCK_WGET_ITERATIONS": "2"}):
        shortened = GuestHarness(work, prefix=work / "prefix")
        assert shortened.run(tests) == 0, shortened.output
        assert shortened.executions == 1, (
            "a shortened workload must execute instead of reusing the acceptance verdict"
        )
    shortened_entry = [
        entry
        for entry in (store / "verdict").iterdir()
        if (
            (verdicts.read_verdict(store, entry.name) or {}).get("identity", {})
            .get("workload", {})
            .get("class")
            == verdicts.REDUCED_WORKLOAD
        )
    ]
    assert shortened_entry, "a shortened run must record the workload class it ran"
    summary = verdicts.reuse_summary(verdicts.read_verdict(store, shortened_entry[0].name))
    assert verdicts.REDUCED_WORKLOAD in summary, summary
    acceptance = GuestHarness(work, prefix=work / "prefix")
    assert acceptance.run(tests) == 0, acceptance.output
    assert acceptance.executions == 0, (
        "the acceptance workload must reuse its own verdict, not the shortened one"
    )
    assert verdicts.ACCEPTANCE_WORKLOAD in "\n".join(acceptance.output)

    with kill_switch():
        forced = GuestHarness(work, prefix=work / "prefix")
        assert forced.run(tests) == 0, forced.output
        assert forced.executions == 1, "the kill switch must force a fresh run"
        assert forced.output_mentions("WEST_TEST_VERDICT_CACHE"), forced.output
    resumed = GuestHarness(work, prefix=work / "prefix")
    assert resumed.run(tests) == 0, resumed.output
    assert resumed.executions == 0, "reuse must resume when the switch is off"
    assert "verdict reuse" in "\n".join(resumed.output), resumed.output

    # A failing run publishes nothing, so the next attempt executes again.
    failing = {**GUEST_TEST, "name": "comparison_guest_failure"}
    probe = GuestHarness(work, prefix=work / "prefix")
    failure_identity = probe._metadata_verdict_identity(
        GUEST_PATCH, failing, probe._test_invocation(GUEST_PATCH, failing)
    )
    assert failure_identity is not None
    failure_key = verdicts.identity_key(failure_identity)
    assert verdicts.read_verdict(store, failure_key) is None
    broken = GuestHarness(work, prefix=work / "prefix", result_rc=1)
    assert broken.run([(GUEST_PATCH, failing)]) == 1 and broken.executions == 1
    retried = GuestHarness(work, prefix=work / "prefix", result_rc=1)
    assert retried.run([(GUEST_PATCH, failing)]) == 1
    assert retried.executions == 1, "a failing guest test must be executed again"
    assert verdicts.read_verdict(store, failure_key) is None, (
        "a failing run must not publish a verdict"
    )

    # A test whose verdict is an experiment about flake, or evidence that this
    # run has to produce, is executed every time instead of reused.
    for label, name, extra in (
        ("a RED arm", "comparison_guest_red", {"red": True}),
        (
            "a guest Mach-O validation group",
            "comparison_guest_group",
            {"validation-group": "homebrew"},
        ),
    ):
        declarations = [(GUEST_PATCH, {**GUEST_TEST, **extra, "name": name})]
        executed = GuestHarness(work, prefix=work / "prefix")
        assert executed.run(declarations) == 0, (label, executed.output)
        assert executed.executions == 1, label
        repeated = GuestHarness(work, prefix=work / "prefix")
        assert repeated.run(declarations) == 0, (label, repeated.output)
        assert repeated.executions == 1, f"{label} must not be reused"
    assert verdicts.read_stats(store).value("verdict_unkeyed") >= 2, (
        "an unkeyable test must be counted, not silently cached"
    )

    # A clean-shutdown test observes live prefix state, which no identity input
    # describes, so it is unkeyable as well.
    clean = {**GUEST_TEST, "verify-clean-shutdown": True}
    assert (
        probe._metadata_verdict_identity(
            GUEST_PATCH, clean, probe._test_invocation(GUEST_PATCH, clean)
        )
        is None
    ), "a clean-shutdown test must be unkeyable"


def check_host_never_reused(work: Path) -> None:
    """A host invocation always executes, however often it is selected."""

    notes = work / "notes" / "tests"
    notes.mkdir(parents=True)
    (notes / Path(HOST_SCRIPT).name).write_text("#!/bin/sh\necho host verdict smoke\n")
    tests = [(HOST_PATCH, host_test())]
    store = work / "manifest" / ".west-test" / "test-verdict-cache"

    first = Harness(work)
    assert first.run(tests) == 0, first.output
    assert first.executions == 1, "a host test must execute"
    second = Harness(work)
    assert second.run(tests) == 0, second.output
    assert second.executions == 1, (
        "a host test must execute again: a cached pass on seconds of work would "
        "hide the flake the check exists to expose"
    )
    assert not (store / "verdict").exists(), (
        "a host invocation must not publish a verdict"
    )
    stats = verdicts.read_stats(store)
    assert stats.value("verdict_host") == 2, stats.counters
    assert stats.value("verdict_misses") == 0, (
        f"a deliberately uncached host run is not a miss: {stats.counters}"
    )
    assert stats.value("verdict_hits") == 0, stats.counters
    report = "\n".join(second.output)
    assert "skipped-host=2" in report, report
    assert "0hit/0miss" in report, report


def run_checks(work: Path) -> None:
    check_stable_key(work / "stable")
    check_invalidation(work / "invalidation")
    check_unkeyable(work / "unkeyable")
    check_marker_required(work / "marker")
    check_nonzero_never_reused(work / "nonzero")
    check_bound_and_accounting(work / "bound")
    check_kill_switch(work / "switch")
    check_workload_class(work / "workload")
    check_guest_runtime_identity(work / "guest")
    check_loop_guest(work / "guest-loop")
    check_host_never_reused(work / "host-loop")


# --------------------------------------------------------------------------
# Deliberately broken arms. Each one names the check that must catch it.
# --------------------------------------------------------------------------


def _drop(name: str):
    original = verdicts.verdict_identity

    def patched(**kwargs):
        document = original(**kwargs)
        if document is None:
            return None
        document = dict(document)
        document.pop(name, None)
        return document

    return patched


def _read_ignoring_marker(store: Path, key: str):
    entry = verdicts.entry_path(store, key)
    if not entry.is_dir():
        return None
    marker = verdicts.marker_path(entry)
    if marker.is_file():
        try:
            return json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    return {"verdict": 0, "recorded-at": "unknown", "duration-seconds": 0.0, "key": key}


def _read_reusing_failures(store: Path, key: str):
    entry = verdicts.entry_path(store, key)
    if not entry.is_dir():
        return None
    marker = verdicts.marker_path(entry)
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _prune_nothing(store, max_bytes=verdicts.DEFAULT_MAX_BYTES, *, protect=()):
    return {"evicted": [], "evicted_bytes": 0, "bytes": 0, "max_bytes": max_bytes}


def _never_disabled(environ):
    return False


def _always_root(manifest_repo, environ):
    return Path(manifest_repo) / ".west-test" / "test-verdict-cache"


def _always_acceptance_workload(values):
    return verdicts.ACCEPTANCE_WORKLOAD


def _cache_every_invocation(self, test, invocation):
    return True


_metadata_runtime_identity = DarlingTest._metadata_runtime_identity
_metadata_verdict_identity = DarlingTest._metadata_verdict_identity


def _runtime_without_proof(self, test, patch):
    definition, runtime = _metadata_runtime_identity(self, test, patch)
    if runtime is None:
        return definition, runtime
    return definition, {name: value for name, value in runtime.items() if name != "proof"}


def _key_guest_without_runtime(self, patch, test, invocation):
    identity = _metadata_verdict_identity(self, patch, test, invocation)
    if identity is not None:
        return identity
    return verdicts.verdict_identity(
        test=test,
        invocation=invocation,
        runtime=None,
        stack=None,
        toolchain=None,
        environ=os.environ,
    )


# Each arm names one deliberate defect and the check that has to catch it. The
# owner prefixes are the modules the checks go through, so an arm cannot quietly
# patch a copy of the function the run does not call.
OWNERS = {"verdicts": verdicts, "test": DarlingTest}

ARMS = {
    "asset-not-keyed": ({"verdicts.verdict_identity": _drop("asset")}, check_invalidation),
    "declaration-not-keyed": (
        {"verdicts.verdict_identity": _drop("declaration")},
        check_invalidation,
    ),
    "runner-not-keyed": (
        {"verdicts.verdict_identity": _drop("runner")},
        check_invalidation,
    ),
    "runtime-not-keyed": (
        {"verdicts.verdict_identity": _drop("runtime")},
        check_invalidation,
    ),
    "stack-not-keyed": ({"verdicts.verdict_identity": _drop("stack")}, check_invalidation),
    "workload-not-keyed": (
        {"verdicts.verdict_identity": _drop("workload")},
        check_invalidation,
    ),
    "toolchain-not-keyed": (
        {"verdicts.verdict_identity": _drop("toolchain")},
        check_invalidation,
    ),
    "arch-not-keyed": ({"verdicts.verdict_identity": _drop("arch")}, check_invalidation),
    "marker-not-required": (
        {"verdicts.read_verdict": _read_ignoring_marker},
        check_marker_required,
    ),
    "nonzero-verdict-reused": (
        {"verdicts.read_verdict": _read_reusing_failures},
        check_nonzero_never_reused,
    ),
    "bound-ignored": ({"verdicts.prune": _prune_nothing}, check_bound_and_accounting),
    "kill-switch-ignored": (
        {"verdicts.reuse_disabled": _never_disabled, "verdicts.cache_root": _always_root},
        check_kill_switch,
    ),
    "runtime-proof-not-keyed": (
        {"test._metadata_runtime_identity": _runtime_without_proof},
        check_guest_runtime_identity,
    ),
    "guest-keyed-without-runtime": (
        {"test._metadata_verdict_identity": _key_guest_without_runtime},
        check_guest_runtime_identity,
    ),
    "host-verdict-cached": (
        {"test._metadata_verdict_cacheable": _cache_every_invocation},
        check_host_never_reused,
    ),
    "workload-class-mislabeled": (
        {"verdicts.workload_class": _always_acceptance_workload},
        check_workload_class,
    ),
}


@contextlib.contextmanager
def armed(arm: str):
    """Apply one named deliberate defect for the duration of the block."""

    replacements, _check = ARMS[arm]
    undo = []
    for target, function in replacements.items():
        owner_name, attribute = target.split(".", 1)
        owner = OWNERS[owner_name]
        undo.append((owner, attribute, getattr(owner, attribute)))
        setattr(owner, attribute, function)
    try:
        yield
    finally:
        for owner, attribute, original in undo:
            setattr(owner, attribute, original)


def main() -> int:
    arm = os.environ.get(ARM_ENV) or (sys.argv[1] if len(sys.argv) > 1 else None)
    with tempfile.TemporaryDirectory(prefix="west-verdict-cache-contract-") as raw:
        if arm is not None:
            if arm not in ARMS:
                raise SystemExit(
                    f"unknown RED arm {arm!r}; known arms: {', '.join(sorted(ARMS))}"
                )
            try:
                with armed(arm):
                    run_checks(Path(raw) / "red")
            except Exception as error:  # noqa: BLE001 - reported as the arm's verdict
                raised_in = traceback.extract_tb(error.__traceback__)[-1].name
                print(
                    f"RED arm {arm!r} failed as designed in {raised_in}: "
                    f"{type(error).__name__}: {error}"
                )
                return 1
            print(
                f"RED arm {arm!r} was not detected: the contract is insensitive to it"
            )
            return 1
        run_checks(Path(raw) / "green")
        for name in sorted(ARMS):
            check = ARMS[name][1]
            with tempfile.TemporaryDirectory(prefix="west-verdict-arm-") as probe:
                base = Path(probe)
                check(base / "clean")
                try:
                    with armed(name):
                        check(base / "armed")
                except Exception as error:  # noqa: BLE001 - the arm must be caught
                    print(f"arm {name}: {check.__name__} caught it: {error}")
                    continue
                raise AssertionError(
                    f"the contract is insensitive to arm {name!r}: "
                    f"{check.__name__} still passed"
                )

    print("PASS test-verdict-cache-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

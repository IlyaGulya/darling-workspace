"""Ensure CTest guest selections own the Darling prefix lifecycle."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import west_commands.test as test_module
from west_commands.test import DarlingTest
from west_commands.test_execution import ProcessResult


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "prefix"
    prefix.mkdir()
    context_test = DarlingTest.__new__(DarlingTest)
    context_test._prefix = str(prefix)
    context_test._keep_prefix_running = False
    context_test._prefix_cleanup_failed = False
    context_test.inf = lambda _message: None
    shutdowns = []
    context_test._shutdown_test_prefix = lambda: (shutdowns.append("shutdown") or True)
    with context_test._prefix_resource_context(True):
        assert shutdowns == ["shutdown"], shutdowns
    assert shutdowns == ["shutdown", "shutdown"], shutdowns


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    build = root / "build"
    build.mkdir()
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.wrn = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._resolve_prefix = lambda _args: str(root / "prefix")
    test._resolve_executor = lambda _executor: None
    test._resolve_darling_launcher = lambda _prefix: "/fake/darling"
    test._testkit_dir = lambda: root
    test._ctest_runtime_profile_definitions = lambda: {
        "extra": {
            "source-profile": "homebrew",
            "source-module": "darling/src/external/xnu",
            "source-modules": ["darling"],
            "runtime-artifacts": [{
                "build-targets": ["system_kernel"],
                "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
            }],
        }
    }
    test._configure_and_build = lambda *_args, **_kwargs: build
    test._prefix_cleanup_failed = False
    stale_failure_record = build / "Testing" / "Temporary" / "LastTestsFailed.log"
    stale_failure_record.parent.mkdir(parents=True)
    stale_failure_record.write_text("darling/stale_failure\n")
    lifecycle = []

    @contextmanager
    def prefix_context(enabled):
        lifecycle.append(enabled)
        yield

    test._prefix_resource_context = prefix_context
    runtime_contexts = []

    @contextmanager
    def runtime_context(profiles):
        runtime_contexts.append(profiles)
        yield

    test._ctest_runtime_profile_context = runtime_context
    recorded = []
    original = test_module.run_bounded
    def bounded(args, **kwargs):
        recorded.append((args, kwargs))
        if "--show-only=json-v1" in args:
            return ProcessResult(0, stdout=json.dumps({"tests": [{
                "name": "darling/extra",
                "properties": [{"name": "LABELS", "value": [
                    "env:darling", "runtime-profile:extra",
                ]}],
            }]}))
        assert not stale_failure_record.exists(), stale_failure_record
        return ProcessResult(0)

    test_module.run_bounded = bounded
    try:
        args = SimpleNamespace(
            bundle_root=str(root / "bundles"),
            materialize_profile=False,
            keep_prefix_running=False,
            ctest_timeout_seconds=17,
            gc=False,
            red_audit=False,
            profile=None,
            patch=None,
            submodule=[],
            fuzz=False,
            stress=False,
            list=False,
            env="darling",
            changed=False,
            bead=None,
            diag=None,
            label=None,
            executor=None,
            red_only=False,
            prove_red=False,
            with_runtime_profile=["extra"],
        )
        try:
            test.do_run(args, [])
        except SystemExit as exc:
            assert exc.code == 0, exc.code
        else:
            raise AssertionError("do_run did not exit")
    finally:
        test_module.run_bounded = original

    assert lifecycle == [True], lifecycle
    assert runtime_contexts == [["extra"]], runtime_contexts
    assert len(recorded) == 2, recorded
    assert "--show-only=json-v1" in recorded[0][0], recorded
    assert recorded[1][0][0] == "ctest", recorded
    assert recorded[1][1]["timeout_seconds"] == 17, recorded
    assert not stale_failure_record.exists(), stale_failure_record


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    test = DarlingTest.__new__(DarlingTest)
    test._prefix = str(root / "prefix")
    test._active_profile = None
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew": {
            "source-profile": "homebrew",
            "source-module": "darling/src/external/xnu",
            "source-modules": [
                "darling",
                "darling/src/external/darlingserver",
                "darling/src/external/xnu",
            ],
            "runtime-artifacts": [{"build-targets": ["system_kernel"]}],
        }
    }
    events = []

    @contextmanager
    def source_forest(anchor, proof, *, omit_patch):
        assert test._active_profile == "homebrew"
        assert anchor["module"] == "darling/src/external/xnu"
        assert proof["runtime-artifacts"][0]["build-targets"] == ["system_kernel"]
        assert not omit_patch
        events.append("source")
        yield root / "source"

    @contextmanager
    def deployed(proof, build_root, prefix, *, label):
        assert proof["source-modules"] == [
            "darling",
            "darling/src/external/darlingserver",
            "darling/src/external/xnu",
        ]
        assert build_root == root / "build"
        assert prefix == root / "prefix"
        assert label == "CTest homebrew"
        events.append("deploy")
        yield
        events.append("restore")

    test._guest_runtime_source_forest = source_forest
    test._runtime_red_build_artifacts = lambda *_args, **_kwargs: (events.append("build") or root / "build")
    test._runtime_red_deployed_artifacts = deployed
    with test._ctest_runtime_profile_context(["homebrew"]):
        assert events == ["source", "build", "deploy"], events
    assert events == ["source", "build", "deploy", "restore"], events
    assert test._active_profile is None


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    scratch = root / "preserved-scratch"
    test = DarlingTest.__new__(DarlingTest)
    test._prefix = str(root / "prefix")
    test._active_profile = None
    errors = []
    test.inf = lambda _message: None
    test.err = errors.append
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew": {
            "source-profile": "homebrew",
            "source-module": "darling/src/external/xnu",
            "source-modules": [
                "darling",
                "darling/src/external/darlingserver",
                "darling/src/external/xnu",
            ],
            "runtime-artifacts": [{"build-targets": ["system_kernel"]}],
        }
    }

    @contextmanager
    def source_forest(_anchor, _proof, *, omit_patch):
        assert not omit_patch
        yield root / "source"

    test._guest_runtime_source_forest = source_forest
    test._runtime_red_build_artifacts = lambda *_args, **_kwargs: test.die("build failed")
    original_mkdtemp = test_module.tempfile.mkdtemp

    def make_scratch(**_kwargs):
        scratch.mkdir()
        return str(scratch)

    test_module.tempfile.mkdtemp = make_scratch
    try:
        try:
            with test._ctest_runtime_profile_context(["homebrew"]):
                raise AssertionError("runtime build failure unexpectedly yielded")
        except SystemExit as exc:
            assert str(exc) == "build failed", exc
    finally:
        test_module.tempfile.mkdtemp = original_mkdtemp
    assert scratch.is_dir(), scratch
    assert errors == [f"preserving failed CTest runtime scratch for inspection: {scratch}"], errors


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    old_output = root / "west-ctest-guest-c.old"
    fresh_output = root / "west-ctest-guest-c.fresh"
    output_dir = root / "west-ctest-guest-c.directory"
    unrelated = root / "unrelated"
    old_output.write_text("old\n")
    fresh_output.write_text("fresh\n")
    output_dir.mkdir()
    unrelated.write_text("keep\n")
    old_time = time.time() - 7200
    os.utime(old_output, (old_time, old_time))

    test = DarlingTest.__new__(DarlingTest)
    messages = []
    test.inf = messages.append
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._gc_guest_runner_output(root, max_age_hours=1)

    assert not old_output.exists(), old_output
    assert fresh_output.exists(), fresh_output
    assert output_dir.is_dir(), output_dir
    assert unrelated.exists(), unrelated
    assert any("guest-runner gc: pruned 1 file(s)" in message for message in messages), messages

print("PASS west-test-ctest-lifecycle-contract")

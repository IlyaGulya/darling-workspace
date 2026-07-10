"""Ensure CTest guest selections own the Darling prefix lifecycle."""

from __future__ import annotations

import sys
import tempfile
import types
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
    test._configure_and_build = lambda *_args, **_kwargs: build
    test._prefix_cleanup_failed = False
    lifecycle = []

    @contextmanager
    def prefix_context(enabled):
        lifecycle.append(enabled)
        yield

    test._prefix_resource_context = prefix_context
    recorded = []
    original = test_module.run_bounded
    test_module.run_bounded = lambda args, **kwargs: (
        recorded.append((args, kwargs)) or ProcessResult(0)
    )
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
    assert recorded and recorded[0][0][0] == "ctest", recorded
    assert recorded[0][1]["timeout_seconds"] == 17, recorded

print("PASS west-test-ctest-lifecycle-contract")

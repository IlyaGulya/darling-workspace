"""Ensure CTest guest selections own the Darling prefix lifecycle."""

from __future__ import annotations

import os
import subprocess
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
    root = Path(temp)
    prefix = root / "selected prefix ' $(printf wrong)"
    prefix.mkdir(parents=True)
    (root / "installed" / "bin").mkdir(parents=True)
    launcher = root / "installed" / "bin" / "darling ' owner"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,sys\n"
        "prefix=pathlib.Path(os.environ['DPREFIX'])\n"
        "if sys.argv[1:] != ['shutdown']: sys.exit(64)\n"
        "with open(prefix/'shutdown.fifo','wb',buffering=0) as endpoint:\n"
        "    endpoint.write(b'shutdown')\n"
    )
    launcher.chmod(0o755)
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys; os.mkfifo(sys.argv[1]); "
            "endpoint=open(sys.argv[1],'rb',buffering=0); "
            "sys.exit(23 if endpoint.read(8)==b'shutdown' else 42)",
            str(prefix / "shutdown.fifo"),
        ]
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            if (prefix / "shutdown.fifo").exists():
                break
            assert worker.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        debug_test = DarlingTest.__new__(DarlingTest)
        debug_test._executor = "/tmp/darling-debug-runner"
        debug_args = debug_test._debug_runner_args(
            {
                "name": "prefix_owner",
                "diag": "guarded",
                "cwd": root,
                "args": ["runtime.sh"],
                "shell": False,
                "requires_resources": ["darling-prefix"],
            },
            env={
                "DPREFIX": str(prefix),
                "DARLING_LAUNCHER": str(launcher),
                "DARLING": "/not-the-selected-prefix/darling",
            },
        )
        shutdown = debug_args[debug_args.index("--terminate-command") + 1]
        subprocess.run(["/bin/bash", "-lc", shutdown], cwd=root, check=True, timeout=3)
        assert worker.wait(timeout=2) == 23
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait()


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    empty_selection_test = DarlingTest.__new__(DarlingTest)
    empty_selection_test.topdir = str(root)
    empty_selection_test._ctest_build = root / "build"
    empty_selection_test._ctest_build.mkdir()
    empty_selection_test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    empty_selection_test._dump_command_tail = lambda *_args: None
    original = test_module.run_bounded
    test_module.run_bounded = lambda *_args, **_kwargs: ProcessResult(
        0, stdout=json.dumps({"tests": []})
    )
    try:
        try:
            empty_selection_test._ctest_label_args({"ctest_label": "missing"})
            raise AssertionError("empty CTest label selection unexpectedly passed")
        except SystemExit:
            pass
    finally:
        test_module.run_bounded = original



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

    # A file name is not ownership. The guest runner unlinks its own output on
    # every exit path, so anything matching here is either historical or belongs
    # to someone else: the pass reports each one with its size and deletes none
    # of them. Reclaiming the space is an operator decision made with the path
    # in hand, which is why this contract asserts survival rather than removal.
    assert old_output.exists(), old_output
    assert fresh_output.exists(), fresh_output
    assert output_dir.is_dir(), output_dir
    assert unrelated.exists(), unrelated

for workload_rc, final_rc in ((0, 1), (7, 7)):
    teardown_command = DarlingTest.__new__(DarlingTest)
    teardown_command._prefix_cleanup_failed = False
    @contextmanager
    def lane_context(_args):
        yield
    @contextmanager
    def failed_fresh_cleanup(_args):
        try:
            yield
        finally:
            teardown_command._prefix_cleanup_failed = True
    teardown_command._lane_context = lane_context
    teardown_command._fresh_prefix_context = failed_fresh_cleanup
    teardown_command._do_run = lambda *_args: (_ for _ in ()).throw(SystemExit(workload_rc))
    try:
        teardown_command.do_run(SimpleNamespace(), [])
    except SystemExit as error:
        assert error.code == final_rc, error.code
    else:
        raise AssertionError("fresh-prefix cleanup failure produced a clean verdict")

print("PASS west-test-ctest-lifecycle-contract")

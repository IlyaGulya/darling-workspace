import ctypes
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))
from west_commands.test import DarlingTest

RUNNER = Path(os.environ.get(
    "DARLING_DEBUG_RUNNER_TEST_BINARY",
    ROOT.parent / "darling-debug-runner/target/release/darling-debug-runner",
))


def forensic_capture_contract(root):
    started = root / "capture-started"
    completed = root / "capture-completed"
    hook = root / "capture.py"
    hook.write_text(
        "import time\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).touch()\n"
        "time.sleep(17)\n"
        f"Path({str(completed)!r}).write_text('capture complete\\n')\n"
    )
    executor = root / "capture-executor"
    capture_command = "exec " + shlex.join([sys.executable, str(hook)])
    # Replace machine-dependent GDB capture with a deterministic slow hook;
    # DarlingTest still selects its real forensic execution path.
    executor.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "separator = args.index('--')\n"
        "options = [arg for arg in args[:separator]\n"
        "           if arg not in {'--capture-gdb', '--capture-tree'}]\n"
        f"options += ['--poll-seconds', '1', '--capture-command', {capture_command!r}]\n"
        f"os.execv({str(RUNNER)!r}, [{str(RUNNER)!r}, *options, *args[separator:]])\n"
    )
    executor.chmod(0o755)
    runner = DarlingTest()
    runner.topdir = str(root)
    runner._executor = str(executor)
    runner._bundle_root = root / "forensic-bundles"
    runner.err = lambda *args, **kwargs: print(*args, file=sys.stderr)
    invocation = {
        "name": "forensic-capture-completion",
        "cwd": root,
        "diag": "forensic",
        "timeout_seconds": 1,
        "shell": False,
        "args": [sys.executable, "-c", "import time; time.sleep(30)"],
    }

    # The executor gives its payload a separate session. Adopt its children
    # so the RED host timeout cannot leave that payload or the hook orphaned.
    libc = ctypes.CDLL(None, use_errno=True)
    previous_subreaper = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")

    def deadline_expired(signum, frame):
        raise TimeoutError("forensic capture contract exceeded 45 seconds")

    previous_alarm = signal.signal(signal.SIGALRM, deadline_expired)
    signal.alarm(45)
    try:
        result = runner._run_invocation(invocation)
        assert result != 0, "timed-out payload unexpectedly succeeded"
        assert started.is_file(), "forensic capture hook never started"
        assert completed.is_file(), "host deadline killed unfinished forensic capture"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        try:
            deadline = time.monotonic() + 5
            children_path = Path(f"/proc/self/task/{os.getpid()}/children")
            while True:
                children = children_path.read_text().split()
                if not children:
                    break
                for child in children:
                    try:
                        os.kill(int(child), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    while os.waitpid(-1, os.WNOHANG)[0]:
                        pass
                except ChildProcessError:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("forensic fixture children were not reaped")
                time.sleep(0.01)
        finally:
            if libc.prctl(36, previous_subreaper.value, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "restoring child subreaper")


def main():
    if not RUNNER.is_file():
        raise SystemExit(f"missing guarded executor: {RUNNER}")
    with tempfile.TemporaryDirectory(prefix="guarded-ctest-contract-") as temp:
        root = Path(temp)
        build = root / "build"
        bundles = root / "bundles"
        (root / "hang.c").write_text(
            "#include <unistd.h>\nint main(void) { sleep(30); return 0; }\n"
        )
        (root / "failure.c").write_text(
            '#include <stdio.h>\nint main(void) {\n'
            '  puts("GUARDED_DOMAIN_FAILURE");\n'
            '  fputs("WEST_TEST_FAILURE_PHASE=run\\n", stderr);\n'
            '  return 23;\n}\n'
        )
        (root / "CMakeLists.txt").write_text(f"""
cmake_minimum_required(VERSION 3.13)
project(guarded_timeout_contract C)
include(CTest)
include("{ROOT}/testkit/cmake/AddCompatTest.cmake")
add_compat_test(NAME guarded_timeout_contract SOURCE "{root}/hang.c"
  ENVS host BEAD dar-contract DIAG guarded TIMEOUT 1)
add_compat_test(NAME guarded_output_contract SOURCE "{root}/failure.c"
  ENVS host BEAD dar-contract DIAG guarded TIMEOUT 10)
""")
        subprocess.run([
            "cmake", "-S", str(root), "-B", str(build), "-G", "Ninja",
            f"-DDARLING_TEST_EXECUTOR={RUNNER}",
            f"-DDARLING_TEST_BUNDLE_ROOT={bundles}",
        ], check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(["cmake", "--build", str(build)], check=True,
                       capture_output=True, text=True, timeout=30)
        runner = DarlingTest()
        runner.topdir = str(root)
        runner._executor = str(RUNNER)
        runner._bundle_root = bundles
        invocation = {
            "name": "xnu/guarded-output.patch",
            "ctest_name": "host/guarded_output_contract",
            "ctest_build": build,
            "cwd": root,
            "diag": "guarded",
            "timeout_seconds": 15,
        }
        started = time.time()
        failed = runner._run_invocation_captured(invocation)
        output = runner._guest_runtime_red_output(
            invocation, since=started, captured_output=failed.output
        )
        assert failed.returncode != 0, output
        assert failed.failure_phase == "run", (failed.failure_phase, output)
        assert output is not None and "GUARDED_DOMAIN_FAILURE" in output, output
        assert "WEST_TEST_FAILURE_PHASE=run" in output, output

        timed_out = subprocess.run([
            "ctest", "--test-dir", str(build), "--output-on-failure",
            "-R", "^host/guarded_timeout_contract$",
        ], capture_output=True, text=True, timeout=20)
        output = timed_out.stdout + timed_out.stderr
        assert timed_out.returncode != 0, output
        assert "RESULT=timeout" in output, output
        timeout_bundles = list(bundles.glob("*/timeout.txt"))
        assert len(timeout_bundles) == 1, output
        bundle = timeout_bundles[0].parent
        assert (bundle / "cleanup-status.txt").is_file(), output
        assert (bundle / "guarded-tree").is_dir(), output
        pid = int((bundle / "pid.txt").read_text())
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
        except FileNotFoundError:
            pass
        else:
            assert os.fsencode(root) not in command, (
                f"timed-out fixture is still alive: {pid}"
            )
        forensic_capture_contract(root)
    print("PASS west-test-guarded-timeout-contract")


if __name__ == "__main__":
    main()

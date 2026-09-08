import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(os.environ.get(
    "DARLING_DEBUG_RUNNER_TEST_BINARY",
    ROOT.parent / "darling-debug-runner/target/release/darling-debug-runner",
))


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
        failed = subprocess.run([
            "ctest", "--test-dir", str(build), "--output-on-failure",
            "-R", "^host/guarded_output_contract$",
        ], capture_output=True, text=True, timeout=20)
        output = failed.stdout + failed.stderr
        assert failed.returncode != 0, output
        assert "GUARDED_DOMAIN_FAILURE" in output, output
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
    print("PASS west-test-guarded-timeout-contract")


if __name__ == "__main__":
    main()

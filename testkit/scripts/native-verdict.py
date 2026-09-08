#!/usr/bin/env python3
"""A native command's exit status plus its source-owned output oracle."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def infrastructure_error(test_name, phase, error):
    print(f"NATIVE_INFRASTRUCTURE_ERROR: {error}", file=sys.stderr)
    results = os.environ.get("DARLING_NATIVE_RESULTS_DIR")
    if results:
        temporary = None
        try:
            directory = Path(results) / "infrastructure"
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="native-helper-",
                suffix=".tmp", dir=directory, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump({
                    "schema_version": 1,
                    "kind": "native_helper_infrastructure_error",
                    "test_name": test_name,
                    "phase": phase,
                    "error": str(error),
                }, stream)
                stream.write("\n")
            os.replace(temporary, temporary.with_suffix(".json"))
            temporary = None
        except OSError as diagnostic_error:
            print(f"Cannot retain native infrastructure diagnostic: {diagnostic_error}",
                  file=sys.stderr)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return 125


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ok-marker-file", type=Path)
    parser.add_argument("--failure-marker-file", type=Path)
    parser.add_argument("--ctest-root", required=True)
    parser.add_argument("--test-name", required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing command")
    phase = "marker"
    try:
        ok = args.ok_marker_file.read_bytes().removesuffix(b"\n") if args.ok_marker_file else None
        red = args.failure_marker_file.read_bytes().removesuffix(b"\n") if args.failure_marker_file else None
        phase = "launch"
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        return infrastructure_error(args.test_name, phase, exc)
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.buffer.flush()
    if result.returncode < 0:
        # Preserve CTest's exception semantics: WILL_FAIL cannot invert a signal.
        signum = -result.returncode
        if signum not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        return 125
    if red is not None and result.returncode:
        # Only CTest owns skip policy, including caller-added properties.
        try:
            ctest = [os.environ.get("DARLING_NATIVE_CTEST", "ctest"),
                     "--test-dir", os.environ.get("DARLING_NATIVE_CTEST_ROOT", args.ctest_root),
                     "--show-only=json-v1"]
            if args.config:
                ctest += ["-C", args.config]
            query = subprocess.run(
                ctest,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            # CTest may canonicalize its --test-dir while an including runner
            # used a symlink spelling (notably /tmp versus /private/tmp on macOS).
            # These are the same registration, not distinct command contracts.
            def canonical_arguments(values):
                values = list(values)
                path_options = {"--ctest-root", "--ok-marker-file", "--failure-marker-file"}
                for index, value in enumerate(values):
                    if value == "--":
                        if index + 1 < len(values):
                            values[index + 1] = os.path.realpath(values[index + 1])
                        break  # Product argv must remain byte-for-byte intact.
                    if index and values[index - 1] in path_options:
                        values[index] = os.path.realpath(value)
                return values

            helper = os.path.realpath(sys.argv[0])
            own_arguments = canonical_arguments(sys.argv[1:])
            candidates = []
            for test in json.loads(query.stdout)["tests"]:
                argv = test.get("command", [])
                if test["name"] != args.test_name:
                    continue
                for index, value in enumerate(argv):
                    if os.path.isabs(value) and os.path.realpath(value) == helper:
                        if canonical_arguments(argv[index + 1:]) == own_arguments:
                            candidates.append(test)
                        break
            if len(candidates) != 1:
                raise ValueError("cannot uniquely locate native verdict CTest registration")
            props = {p["name"]: p["value"] for p in candidates[0]["properties"]}
            if result.returncode == props.get("SKIP_RETURN_CODE"):
                return result.returncode
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
            return infrastructure_error(args.test_name, "discovery", exc)
    if red is not None:
        if result.returncode == 0 or red not in result.stdout:
            print("Native command did not produce the expected failure", file=sys.stderr)
            return 1
        return 0
    if result.returncode:
        return result.returncode
    if ok is not None and ok not in result.stdout.splitlines():
        print("Native command exited successfully without the exact OK_MARKER line", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Real-Darwin acceptance of build-tree and relocatable native CTest contracts.

Run with --work-dir NEW_DIRECTORY. Keeps the bundle and diagnostics so the same
artifact can be replayed through the SSH transport; never rebuild it for SSH.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "macos/pass": "pass",
    "macos/missing-marker": "fail",
    "macos/wrong-marker": "fail",
    "macos/expected-nonzero": "pass",
    "macos/wrong-negative": "fail",
    "macos/signal": "fail",
    "macos/timeout": "fail",
    "macos/skip": "skip",
    "macos/property-fail": "fail",
}


def verdicts(path: Path) -> dict[str, str]:
    cases = ET.parse(path).findall(".//testcase")
    return {
        case.attrib["name"]: (
            "skip" if case.find("skipped") is not None else
            "fail" if case.find("failure") is not None or case.find("error") is not None else
            "pass"
        ) for case in cases
    }


def run(work: Path, name: str, command: list[str], *, expected: int = 0, env=None):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, timeout=180)
    (work / f"{name}.log").write_text(result.stdout)
    if result.returncode != expected:
        raise AssertionError(f"{name}: rc={result.returncode}, expected {expected}\n{result.stdout}")
    return result


def prepare(work: Path):
    source = work / "source with spaces"
    source.mkdir()
    resources = source / "resources"
    resources.mkdir()
    (resources / "payload.dat").write_bytes(b"resource\x00\xff\n")
    (source / "probe.c").write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>

int main(int argc, char **argv) {
    const char *value = getenv("NATIVE_CONTRACT_VALUE");
    if (argc != 3 || strcmp(argv[2], "space \"quote\" $literal") ||
        !value || strcmp(value, "semi;colon")) {
        fputs("BAD_ARGUMENT_OR_ENVIRONMENT\n", stderr); return 10;
    }
    const unsigned char expected[] = {'r','e','s','o','u','r','c','e',0,255,10};
    unsigned char actual[sizeof(expected) + 1];
    FILE *resource = fopen("payload.dat", "rb");
    if (!resource) { perror("payload.dat"); return 11; }
    size_t count = fread(actual, 1, sizeof(actual), resource);
    fclose(resource);
    if (count != sizeof(expected) || memcmp(actual, expected, sizeof(expected))) return 12;
    const char *mode = argv[1];
    if (!strcmp(mode, "skip")) { puts("API_UNSUPPORTED"); return 77; }
    if (!strcmp(mode, "wrong-negative")) { puts("WRONG_FAILURE"); return 7; }
    if (!strcmp(mode, "expected-nonzero") || !strcmp(mode, "signal") || !strcmp(mode, "timeout")) {
        puts("EXPECTED; FAILURE [marker]."); fflush(stdout);
        if (!strcmp(mode, "signal")) { raise(SIGTERM); return 13; }
        if (!strcmp(mode, "timeout")) { for (;;) pause(); }
        return 137; /* Explicit normal exit, NOT termination by SIGKILL. */
    }
    if (!strcmp(mode, "missing-marker")) puts("NO_SUCCESS_MARKER");
    else if (!strcmp(mode, "wrong-marker")) puts("prefix VALUE; [OK]. suffix");
    else puts("VALUE; [OK].");
    return 0;
}
''')
    generator = ROOT / "testkit/cmake/AddCompatTest.cmake"
    (source / "CMakeLists.txt").write_text(fr'''
cmake_minimum_required(VERSION 3.20)
project(native_bundle_contract C)
include(CTest)
include("{generator}")
foreach(mode pass missing-marker wrong-marker expected-nonzero wrong-negative signal timeout skip property-fail)
  if(mode MATCHES "^(expected-nonzero|wrong-negative|signal|timeout|skip)$")
    set(oracle EXPECT_FAILURE_MARKER "EXPECTED; FAILURE [marker].")
  else()
    set(oracle OK_MARKER "VALUE; [OK].")
  endif()
  add_compat_test(NAME "${{mode}}" SOURCE "${{CMAKE_CURRENT_SOURCE_DIR}}/probe.c"
    ENVS macos INSTALL TIMEOUT 5 RESOURCES "${{CMAKE_CURRENT_SOURCE_DIR}}/resources"
    ARGS "${{mode}}" [=[space "quote" $literal]=] ${{oracle}})
  set_tests_properties("macos/${{mode}}" PROPERTIES
    WORKING_DIRECTORY "${{CMAKE_CURRENT_SOURCE_DIR}}/resources"
    ENVIRONMENT [=[NATIVE_CONTRACT_VALUE=semi\;colon]=])
endforeach()
set_tests_properties(macos/skip PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(macos/timeout PROPERTIES TIMEOUT 1)
set_tests_properties(macos/property-fail PROPERTIES FAIL_REGULAR_EXPRESSION "VALUE")
darling_install_native_bundle()
''')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    source = prepare(work)
    if args.prepare_only:
        print(source)
        return
    if platform.system() != "Darwin":
        raise SystemExit("native-bundle-contract requires a real Darwin compiler and runtime")
    build = work / "build with spaces"
    bundle = work / "installed bundle"
    run(work, "configure", ["cmake", "-S", str(source), "-B", str(build),
                            f"-DCMAKE_INSTALL_PREFIX={bundle}"])
    run(work, "build", ["cmake", "--build", str(build), "--parallel", "4"])
    build_junit = work / "build-junit.xml"
    started = time.monotonic()
    run(work, "build-ctest", ["ctest", "--test-dir", str(build), "--output-on-failure",
                              "--output-junit", str(build_junit)], expected=8)
    assert time.monotonic() - started < 30, "source-owned timeout was not enforced"
    assert verdicts(build_junit) == EXPECTED, verdicts(build_junit)
    run(work, "install", ["cmake", "--install", str(build)])
    # Prove relocation without the source tree, build tree or original install path.
    shutil.rmtree(source)
    shutil.rmtree(build)
    relocated = work / "relocated bundle"
    bundle.rename(relocated)
    environment = dict(os.environ, DARLING_NATIVE_RESULTS_DIR=str(work / "local-results"))
    run(work, "installed-ctest", [str(ROOT / "ci/run-macos-installed-tests.sh"), str(relocated)],
        expected=1, env=environment)
    installed_junit = work / "local-results/ctest-junit.xml"
    assert verdicts(installed_junit) == EXPECTED, verdicts(installed_junit)
    local_report = json.loads((work / "local-results/execution.json").read_text())
    original_digest = local_report["bundle_sha256"]
    cases = {case["name"]: case for case in local_report["case_results"]}
    assert cases["macos/signal"]["failure_kind"] == "signal"
    assert cases["macos/timeout"]["failure_kind"] == "timeout"
    for name, selector, status, expected_cases in (
        ("positive", "^macos/(pass|expected-nonzero)$", "passed",
         {"macos/pass": "pass", "macos/expected-nonzero": "pass"}),
        ("skipped", "^macos/skip$", "skipped", {"macos/skip": "skip"}),
    ):
        results = work / f"{name}-results"
        run(work, name, [str(ROOT / "ci/run-macos-installed-tests.sh"), str(relocated),
                         "-R", selector],
            env=dict(os.environ, DARLING_NATIVE_RESULTS_DIR=str(results)))
        assert verdicts(results / "ctest-junit.xml") == expected_cases
        report = json.loads((results / "execution.json").read_text())
        assert report["status"] == status
        assert report["bundle_sha256"] == original_digest, "executing CTest mutated the bundle"
    run(work, "archive", [str(ROOT / "ci/run-test-tier.sh"), "macos-archive",
                          str(relocated), str(work / "oracle.tar")])
    damaged = work / "damaged bundle"
    shutil.copytree(relocated, damaged, symlinks=True)
    marker = damaged / "libexec/west-test-markers/expected-nonzero.red"
    marker.unlink()
    damaged_results = work / "damaged-results"
    run(work, "missing-infrastructure-asset",
        [str(ROOT / "ci/run-macos-installed-tests.sh"), str(damaged), "-R", "^macos/expected-nonzero$"],
        expected=2, env=dict(os.environ, DARLING_NATIVE_RESULTS_DIR=str(damaged_results)))
    assert json.loads((damaged_results / "execution.json").read_text())["status"] == "infrastructure_error"
    (work / "acceptance.json").write_text(json.dumps({
        "platform": platform.platform(), "machine": platform.machine(),
        "build_tree_verdicts": verdicts(build_junit),
        "installed_verdicts": verdicts(installed_junit),
        "bundle": str(relocated),
    }, indent=2) + "\n")
    print("PASS native-bundle-contract: relocation, argv/env/resources, exact markers, "
          "negative oracle, normal exit 137, signal rejection, timeout and formal skip")


if __name__ == "__main__":
    main()

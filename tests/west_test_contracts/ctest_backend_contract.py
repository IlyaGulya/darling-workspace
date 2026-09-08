import sys
import subprocess
import tempfile
from pathlib import Path
import json
import os
import shutil
import xml.etree.ElementTree as ET
import yaml

from west_extension_help_contract import _copy_manifest_fixture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))
from test_cmake import archive_git_tree_to, archive_source_to


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    source = root / "source"
    destination = root / "copy"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "west-test@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "West test"], cwd=source, check=True)
    (source / "fixture.txt").write_text("ARCHIVE_COPY_OK\n")
    subprocess.run(["git", "add", "fixture.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=source, check=True)

    assert archive_source_to(source, destination, timeout_seconds=5) == 0
    assert (destination / "fixture.txt").read_text() == "ARCHIVE_COPY_OK\n"

    (source / "tools").mkdir()
    (source / "tools" / "closure.txt").write_text("DCC_ARCHIVE_OK\n")
    subprocess.run(["git", "add", "tools/closure.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "tool fixture"], cwd=source, check=True)
    subset = archive_git_tree_to(
        source,
        root / "subset",
        paths=["tools"],
        timeout_seconds=5,
    )
    assert subset.returncode == 0, subset
    assert (root / "subset/tools/closure.txt").read_text() == "DCC_ARCHIVE_OK\n"

# Exercise the real West loader and CTest, not rendered command templates.
with tempfile.TemporaryDirectory(prefix="ctest-selection-contract-") as temp:
    top, manifest, _ = _copy_manifest_fixture(Path(temp), Path(shutil.which("git")))
    testkit = manifest / "testkit"
    testkit.mkdir()
    shutil.copy2(ROOT / "testkit/runtime-profiles.yml", testkit / "runtime-profiles.yml")
    definitions = yaml.safe_load((testkit / "runtime-profiles.yml").read_text())["runtime-profiles"]
    provider = next(name for name, definition in definitions.items()
                    if definition.get("purpose", "runtime") == "runtime")
    built = top / "product-built"
    executed = top / "host-executed"
    unrelated = top / "unrelated-executed"
    upstream_executed = top / "upstream-executed"
    upstream_name = "testsuite/System/Library/Frameworks/Example.framework/test_variable"
    (testkit / "CMakeLists.txt").write_text(f"""
cmake_minimum_required(VERSION 3.16)
project(selection_contract NONE)
enable_testing()
add_custom_target(product ALL COMMAND "${{CMAKE_COMMAND}}" -E touch "{built}")
add_test(NAME host/scenario COMMAND "${{CMAKE_COMMAND}}" -E touch "{executed}")
set_tests_properties(host/scenario PROPERTIES LABELS "env:host;scenario:shared;diag:bare;bead:shared;submod:example")
add_test(NAME macos/scenario COMMAND "${{CMAKE_COMMAND}}" -E false)
set_tests_properties(macos/scenario PROPERTIES LABELS "env:macos;scenario:shared;diag:guarded;bead:shared;submod:example")
add_test(NAME darling/scenario COMMAND "${{CMAKE_COMMAND}}" -E false)
set_tests_properties(darling/scenario PROPERTIES LABELS "env:darling;scenario:shared;diag:bare;bead:shared;runtime-profile:{provider}")
add_test(NAME host/unrelated COMMAND "${{CMAKE_COMMAND}}" -E touch "{unrelated}")
set_tests_properties(host/unrelated PROPERTIES LABELS "env:host;scenario:host-only;bead:shared-extra;submod:example-extra")
add_test(NAME "{upstream_name}" COMMAND "${{CMAKE_COMMAND}}" -E touch "{upstream_executed}")
add_test(NAME "{upstream_name.replace('.framework', 'Xframework')}" COMMAND "${{CMAKE_COMMAND}}" -E touch "{unrelated}")
add_test(NAME host/skip COMMAND sh -c "exit 77")
set_tests_properties(host/skip PROPERTIES LABELS "env:host;scenario:skip" SKIP_RETURN_CODE 77)
""")
    profile = manifest / "patches/selection/patches.yml"
    profile.parent.mkdir(parents=True)
    shared_patch = {
        "path": "shared.patch", "module": "darling", "bead": "shared",
        "tests": [{"name": "shared-binding", "ctest": "^scenario:shared$"}],
    }
    host_patch = {
        "path": "host.patch", "module": "darling", "bead": "host-only",
        "tests": [{"name": "host-binding", "ctest": "^scenario:host-only$", "runs": "host"}],
    }
    profile.write_text(json.dumps({"patches": [shared_patch, host_patch]}))
    environment = dict(os.environ)
    for variable in ("WEST_PREMATERIALIZED_PROFILE", "WEST_MATERIALIZED_WORKSPACE_LOCK", "DPREFIX"):
        environment.pop(variable, None)

    def west_test(*arguments, succeeds=True):
        result = subprocess.run(
            ["west", "test", *arguments], cwd=top, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        if succeeds:
            assert result.returncode == 0, result.stdout
        else:
            assert result.returncode != 0, result.stdout
        return result.stdout

    # Profile, patch, Bead, metadata-name and CTest-label selectors converge.
    for selectors in (
        (),
        ("--patch", "shared.patch"),
        ("--bead", "shared"),
        ("--label", "^name:shared-binding$"),
        ("--label", "^scenario:shared$"),
        ("--diag", "guarded"),
    ):
        output = west_test("--profile", "selection", "--env", "macos", "--list", *selectors)
        assert "macos/scenario" in output, output
        assert "host/scenario" not in output and "darling/scenario" not in output, output
        assert provider not in output, "native selection inherited a Darling runtime"
        assert not built.exists() and not executed.exists() and not unrelated.exists()

    # Both selection planes expose the same real guest prerequisite, without booting.
    for selectors in (("--profile", "selection"), ("--label", "^scenario:shared$")):
        output = west_test(*selectors, "--env", "darling", "--list")
        assert "darling/scenario" in output and provider in output, output
        assert not built.exists() and not executed.exists()
    output = west_test("--bead", "shared", "--submodule", "example", "--env", "macos", "--list")
    for selector in (("--bead", "shared"), ("--submodule", "example")):
        scoped = west_test(*selector, "--env", "host", "--list")
        assert "host/scenario" in scoped and "host/unrelated" not in scoped, scoped
    assert "macos/scenario" in output and "host/scenario" not in output, output

    output = west_test("--profile", "selection", "--patch", "shared.patch", "--env", "host", "--list")
    assert "host/scenario" in output and not built.exists(), output
    west_test("--profile", "selection", "--patch", "shared.patch", "--env", "host")
    assert executed.exists(), "listed host registration was not executed"
    assert not unrelated.exists(), "execution escaped the frozen CTest selection"
    executed.unlink()
    built.unlink()

    # Exact upstream names need neither name labels nor a renamed layout.
    profile.write_text(json.dumps({"patches": [{
        "path": "upstream.patch", "module": "darling", "bead": "upstream",
        "tests": [{"name": "upstream-binding", "ctest-name": upstream_name}],
    }]}))
    native_host = "macos" if sys.platform == "darwin" else "host"
    output = west_test("--profile", "selection", "--env", native_host, "--list")
    assert upstream_name in output and not built.exists(), output
    west_test("--profile", "selection", "--env", native_host)
    assert upstream_executed.exists() and not unrelated.exists()
    upstream_executed.unlink()
    output = west_test("--env", native_host, "--list", "--",
                       "-R", "Example[.]framework/test_variable$")
    assert upstream_name in output, output
    west_test("--env", native_host, "--", "-R", "Example[.]framework/test_variable$")
    assert upstream_executed.exists() and not unrelated.exists()
    built.unlink()

    # Identical third-party names in different suite directories must not escape scope.
    for directory, marker in (("first", executed), ("second", unrelated)):
        child = testkit / directory
        child.mkdir()
        (child / "CMakeLists.txt").write_text(
            f'add_test(NAME collision COMMAND "${{CMAKE_COMMAND}}" -E touch "{marker}")\n'
            f'set_tests_properties(collision PROPERTIES LABELS "env:host;scope:{directory}")\n'
        )
    with (testkit / "CMakeLists.txt").open("a") as cmake:
        cmake.write("add_subdirectory(first)\nadd_subdirectory(second)\n")
    profile.write_text(json.dumps({"patches": [{
        **shared_patch, "tests": [{"name": "scoped", "ctest-name": "collision", "ctest": "^scope:first$"}],
    }]}))
    for selectors in (("--profile", "selection"), ("--label", "^scope:first$")):
        west_test(*selectors, "--env", "host")
        assert executed.exists() and not unrelated.exists(), "same-named case escaped its source-suite scope"
        executed.unlink()
    # Replay disjoint indices, without implicitly selecting the range between them.
    selection_junit = top / "selection.xml"
    west_test("--env", "host", "--label", "^(scenario:shared|scope:first)$",
              "--", "--output-junit", str(selection_junit))
    assert {case.attrib["name"] for case in ET.parse(selection_junit).findall(".//testcase")} == {
        "host/scenario", "collision",
    }
    assert executed.exists() and not unrelated.exists()
    executed.unlink()
    built.unlink()

    # Missing references, unsupported variants and accidental empty selections fail closed.
    profile.write_text(json.dumps({"patches": [shared_patch, host_patch]}))
    for selectors in (
        ("--profile", "selection", "--patch", "absent.patch"),
        ("--profile", "selection", "--bead", "absent-bead"),
        ("--profile", "selection", "--patch", "host.patch", "--env", "macos"),
        ("--profile", "selection", "--label", "nonexistent-label"),
        ("--env", "macos", "--label", "nonexistent-label"),
    ):
        west_test(*selectors, "--list", succeeds=False)
        assert not built.exists() and not executed.exists() and not unrelated.exists()
    profile.write_text(json.dumps({"patches": [{
        **shared_patch, "tests": [{"name": "dangling-reference", "ctest-name": "missing/test"}],
    }]}))
    output = west_test("--profile", "selection", "--list", succeeds=False)
    assert "missing/test" in output and not built.exists(), output

    # A guest fixture cannot become a native runner by changing its environment label.
    profile.write_text(json.dumps({"patches": [{
        **shared_patch, "tests": [{"name": "mislabelled", "runner": "guest-c-fixture",
                                  "runs": "macos", "script": "unused.c"}],
    }]}))
    west_test("--profile", "selection", "--env", "macos", "--list", succeeds=False)
    assert not built.exists() and not executed.exists()
    for invalid_binding in (
        {"ctest-name": upstream_name, "command": "true"},
        {"ctest": "^scenario:shared$", "runtime-profile": provider},
    ):
        profile.write_text(json.dumps({"patches": [{
            **shared_patch, "tests": [{"name": "invalid-binding", **invalid_binding}],
        }]}))
        west_test("--profile", "selection", "--env", "macos", "--list", succeeds=False)
        assert not built.exists() and not executed.exists()

    # Preserve CTest's formal skip status; no text-based pseudo-protocol.
    junit = top / "skip.xml"
    west_test("--env", "host", "--label", "^scenario:skip$", "--", "--output-junit", str(junit))
    cases = ET.parse(junit).findall(".//testcase")
    assert {case.attrib["name"] for case in cases} == {"host/skip"}
    assert cases[0].find("skipped") is not None

    # An unavailable runtime must be reported, not silently look inapplicable.
    profile.write_text(json.dumps({"patches": [{
        **shared_patch, "tests": [{"ctest-name": "host/scenario", "runs": "host",
                                  "blocked": True, "note": "runtime ownership unresolved"}],
    }]}))
    output = west_test("--profile", "selection", "--env", "host", "--list", succeeds=False)
    assert "host/scenario BLOCKED:" in output and "runtime ownership unresolved" in output, output
    assert not executed.exists(), "blocked registration was executed"

# Execute the source-driven CTest transport with a host shell adapter. This
# proves runner phase classification, not Darwin or Darling compatibility.
from west_commands.test import DarlingTest
from west_commands.test_runtime_proof import ProofObservation, RedOracle, RuntimeProofStateMachine
from west_commands.test_manifest import _default_red_failure_phase

with tempfile.TemporaryDirectory(prefix="ctest-runtime-phase-") as temp:
    root = Path(temp)
    prefix = root / "prefix"
    (prefix / "private/var/tmp").mkdir(parents=True)
    launcher = root / "launcher"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "assert sys.argv[1] == 'shell'\n"
        "args = sys.argv[2:]\n"
        "args[-1] = args[-1].replace('/private/var/tmp', os.environ['DPREFIX'] + '/private/var/tmp')\n"
        "sys.exit(subprocess.call(args))\n"
    )
    launcher.chmod(0o755)
    marker = "CTEST_RUNTIME_SEMANTIC_BROKEN"
    sources = {
        "semantic": f'#include <stdio.h>\nint main(void) {{ puts("{marker}"); return 1; }}\n',
        "compile": f"#error {marker}\n",
        "timeout": f'#include <stdio.h>\n#include <unistd.h>\nint main(void) {{ puts("{marker}"); fflush(stdout); sleep(30); }}\n',
    }
    cmake = ["cmake_minimum_required(VERSION 3.16)", "project(runtime_phases NONE)", "enable_testing()"]
    for name, source in sources.items():
        (root / f"{name}.c").write_text(source)
        cmake.append(
            f'add_test(NAME {name} COMMAND "{ROOT}/testkit/scripts/run-darling-c-test.sh" '
            f'--name {name} --source "{root}/{name}.c" --launcher "{launcher}" --cc cc --cflags "")'
        )
    (root / "CMakeLists.txt").write_text("\n".join(cmake) + "\n")
    build = root / "build"
    subprocess.run(["cmake", "-S", str(root), "-B", str(build), "-G", "Ninja"],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    runner = DarlingTest()
    runner.topdir = str(root)
    runner._bundle_root = root
    environment = dict(os.environ, DPREFIX=str(prefix), DARLING_GUEST_TIMEOUT_SECONDS="1")
    declaration = {"ctest-name": "semantic", "red-proof": {"mode": "guest-runtime-deploy",
                    "expect-output-contains": [marker]}}
    _default_red_failure_phase(declaration)
    for name, expected_phase in (("semantic", "run"), ("compile", "compile"), ("timeout", "timeout")):
        invocation = {"name": name, "ctest_name": name, "ctest_build": build,
                      "cwd": root, "diag": "bare", "timeout_seconds": 15}
        observed = runner._run_invocation_captured(invocation, env=environment)
        assert observed.returncode != 0 and marker in observed.output, observed
        assert observed.failure_phase == expected_phase, observed
        machine = RuntimeProofStateMachine(
            name=name, oracle=RedOracle.from_manifest(declaration["red-proof"]),
            error=lambda _message: None,
        )
        accepted = machine.validate_red(ProofObservation(
            observed.returncode, observed.output, observed.failure_phase))
        assert accepted == (name == "semantic"), (name, observed)

print("PASS ctest-discovery-contract")

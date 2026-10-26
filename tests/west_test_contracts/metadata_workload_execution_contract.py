"""Exercise current-source structured workloads without source reconstruction."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
west = types.ModuleType("west")
commands = types.ModuleType("west.commands")
commands.WestCommand = type("WestCommand", (), {})
sys.modules.setdefault("west", west)
sys.modules.setdefault("west.commands", commands)
from west_commands.test import DarlingTest


def host(root: Path):
    command = DarlingTest.__new__(DarlingTest)
    command.topdir = str(root)
    command._project_path = lambda _module: root
    command._prefix = None
    command.inf = command.err = command.wrn = lambda _message: None
    command.die = lambda message: (_ for _ in ()).throw(AssertionError(message))
    return command


with tempfile.TemporaryDirectory(prefix="west-workload-execution-") as temporary:
    root = Path(temporary)
    selected_source = root / "derived-source"
    source_command = DarlingTest.__new__(DarlingTest)
    source_command.topdir = str(root)
    source_command._source_root = selected_source
    source_command.manifest = types.SimpleNamespace(projects=[
        types.SimpleNamespace(name="darling", path="darling", abspath=str(root / "canonical")),
        types.SimpleNamespace(name="xnu", path="darling/src/external/xnu", abspath=str(root / "canonical/src/external/xnu")),
        types.SimpleNamespace(name="support", path="support", abspath=str(root / "support")),
    ])
    assert source_command._project_path("darling") == selected_source
    assert source_command._project_path("xnu") == selected_source / "src/external/xnu"
    assert source_command._project_path("darling/src/external/xnu") == selected_source / "src/external/xnu"
    assert source_command._project_path("support") == root / "support"
    command = host(root)
    owner = {"path": "compile-contract", "module": "fixture"}
    (root / "include").mkdir()
    (root / "include/value.h").write_text("#define FIXTURE_VALUE 17\n")
    (root / "helper.c").write_text("int helper(void) { return 23; }\n")
    (root / "fixture.c").write_text(
        '#include <value.h>\n#include <generated.h>\n#include <empty.h>\n'
        'int helper(void);\nint main(void) { return FIXTURE_VALUE + helper() == EXPECTED ? 0 : 1; }\n'
    )
    declaration = {
        "name": "compile-contract", "runner": "c-fixture", "repo": "fixture",
        "script": "fixture.c", "source-files": ["helper.c"], "include-dirs": ["include"],
        "generated-headers": {"generated.h": "#define EXPECTED 40\n"},
        "stub-headers": ["empty.h"], "compile-flags": ["-std=c11", "-Wall", "-Werror"],
        "source-env": "FIXTURE_SOURCE", "diag": "bare", "timeout-seconds": 15,
    }
    invocation = command._test_invocation(owner, declaration)
    environment = command._execution_env(invocation)
    assert environment["FIXTURE_SOURCE"] == str(root)
    assert command._run_invocation(invocation, environment) == 0
    (root / "helper.c").write_text("int helper(void) { return 24; }\n")
    assert command._run_invocation(invocation, environment) != 0
    assert command._failure_phase == "run"

    script = root / "cases"
    script.write_text(
        "#!/usr/bin/env python3\nimport os,pathlib,sys\n"
        "assert os.environ['FIXTURE_SOURCE']==str(pathlib.Path.cwd())\n"
        "with open('observed','a') as out: out.write(sys.argv[1]+'\\n')\n"
        "print(sys.argv[1])\n"
    )
    script.chmod(0o755)
    cases = {
        "name": "source-cases", "runner": "source-script-fixture", "repo": "fixture",
        "source-script": "cases", "source-env": "FIXTURE_SOURCE", "diag": "bare",
        "cases": [{"name": "first", "args": ["first"], "stdout": "first\n"},
                  {"name": "second", "args": ["second"], "stdout": "second\n"}],
    }
    invocation = command._test_invocation(owner, cases)
    assert command._run_invocation(invocation, command._execution_env(invocation)) == 0
    assert (root / "observed").read_text() == "first\nsecond\n"
    invocation["cases"][1]["stdout"] = "incorrect\n"
    assert command._run_invocation(invocation, command._execution_env(invocation)) != 0
    assert command._failure_phase == "script"

    (root / "symbols.c").write_text(
        "extern int dependency(void); int exported(void) { return dependency(); }\n"
    )
    symbols = {
        "name": "symbols", "runner": "object-symbol-fixture", "repo": "fixture",
        "source-file": "symbols.c", "diag": "bare", "timeout-seconds": 15,
        "symbol-checks": [{"name": "default", "present-undefined-symbols": ["dependency"],
                           "present-defined-symbols": ["exported"],
                           "absent-undefined-symbols": ["not_present"]}],
    }
    invocation = command._test_invocation(owner, symbols)
    assert command._run_invocation(invocation, os.environ.copy()) == 0
    invocation["symbol_checks"][0]["present_undefined_symbols"] = ["missing_dependency"]
    assert command._run_invocation(invocation, os.environ.copy()) != 0
    assert command._failure_phase == "inspect"

print("PASS metadata-workload-execution-contract")

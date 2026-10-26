"""Contract for the CTest selection/argv logic that moved out of ``test.py``.

``CtestSelectionMixin`` owns CTest argument vectors and exact discovery, with
host and guest groups separated only for prefix lifecycle ownership.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.test import DarlingTest  # noqa: E402
from west_commands.test_execution import ProcessResult  # noqa: E402



def new_test(**attributes) -> DarlingTest:
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = "/workspace"
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.wrn = lambda _message: None
    test._dump_command_tail = lambda *_args, **_kwargs: None
    test._prefix_env = {}
    failures: list[str] = []

    def die(message: str):
        failures.append(message)
        raise SystemExit(message)

    test.die = die
    test._failures = failures
    for name, value in attributes.items():
        setattr(test, name, value)
    return test


def expect_die(call, needle: str) -> None:
    try:
        call()
    except SystemExit as exc:
        assert needle in str(exc), (needle, str(exc))
        return
    raise AssertionError(f"expected the selection to be refused with {needle!r}")


# --- _ctest_cmake_defines -------------------------------------------------
test = new_test()
test._project_path = lambda ref: Path("/projects") / ref
assert test._ctest_cmake_defines({"source_module": "darling"}) == {}
assert test._ctest_cmake_defines(
    {"source_module": "darling"}, source_override="DARLING_SOURCE_ROOT"
) == {"DARLING_SOURCE_ROOT": "/projects/darling"}
assert test._ctest_cmake_defines(
    {"source_module": "darling"}, source_override="X", source_root=Path("/explicit")
) == {"X": "/explicit"}
assert test._ctest_cmake_defines({"ctest_label": "eunion-host"}) == {
    "DARLING_ENABLE_EUNION_HOST_SUITE": "ON"
}

# --- _ensure_ctest_build --------------------------------------------------
built: list[tuple[str, list[str]]] = []
build_dir = Path("/tmp/west-ctest-selection-build")
test = new_test(
    _configure_and_build=lambda *_args, **_kwargs: build_dir,
    _run_testkit_build_command=lambda stage, args: built.append((stage, list(args))),
)
test._testkit_dir = lambda: Path("/workspace/testkit")
test._executor = None
assert test._ensure_ctest_build() == build_dir
assert test._ensure_ctest_build() == build_dir, "the configured build must be reused"
assert test._ensure_ctest_build({"ctest_build": str(build_dir)}) == build_dir
assert built == [
    ("build", ["ninja", "-C", str(build_dir)])
], "a source-bound invocation must build its own tree once"
assert test._ensure_ctest_build({"ctest_build": str(build_dir)}) == build_dir
assert len(built) == 1, "an already compiled source-bound build must not rebuild"

# --- _ctest_catalogue -----------------------------------------------------
test = new_test()
test._run_bounded = lambda *_args, **_kwargs: ProcessResult(
    0, stdout=json.dumps({"tests": [{"name": "darling/one"}]})
)
assert test._ctest_catalogue(build_dir) == [{"name": "darling/one"}]

test = new_test()
test._run_bounded = lambda *_args, **_kwargs: ProcessResult(1, stderr="no ctest")
expect_die(lambda: test._ctest_catalogue(build_dir), "could not discover CTest suite")

test = new_test()
test._run_bounded = lambda *_args, **_kwargs: ProcessResult(0, stdout="not json")
expect_die(lambda: test._ctest_catalogue(build_dir), "invalid CTest catalogue")

# --- _ctest_label_args ----------------------------------------------------
discovery_args: list[list[str]] = []


def discovery(stdout: str, returncode: int = 0):
    def run(args, **_kwargs):
        discovery_args.append(list(args))
        return ProcessResult(returncode, stdout=stdout)

    return run


test = new_test(
    _ensure_ctest_build=lambda _invocation=None: build_dir,
    _run_bounded=discovery(
        json.dumps(
            {
                "tests": [
                    {
                        "name": "darling/one",
                        "properties": [
                            {"name": "WORKING_DIRECTORY", "value": "/build/tests"}
                        ],
                    }
                ]
            }
        )
    ),
)
args = test._ctest_label_args(
    {"ctest_index": 7, "ctest_name": "darling/one", "ctest_directory": "/build/tests"}
)
assert args[:4] == [
    "ctest",
    "--test-dir",
    str(build_dir),
    "--output-on-failure",
], args
assert args[4] == "-I" and args[5].split(",")[0] == "7", args
assert discovery_args[-1] == [
    "ctest",
    "--test-dir",
    str(build_dir),
    "--show-only=json-v1",
    *args[4:],
], "discovery must ask CTest itself for the composed selection"

args = test._ctest_label_args({"ctest_name": "darling/one"})
assert args[4:] == ["-R", "^(darling/one)$"], args

args = test._ctest_label_args({"ctest_label": "bead:dar-1"})
assert args[4:] == ["-L", "bead:dar-1"], args

test = new_test(
    _ensure_ctest_build=lambda _invocation=None: build_dir,
    _run_bounded=discovery(json.dumps({"tests": []})),
)
expect_die(
    lambda: test._ctest_label_args({"ctest_label": "bead:dar-1"}),
    "refusing a false GREEN",
)

test = new_test(
    _ensure_ctest_build=lambda _invocation=None: build_dir,
    _run_bounded=discovery(json.dumps({"tests": [{"name": "darling/other", "properties": []}]})),
)
expect_die(
    lambda: test._ctest_label_args(
        {"ctest_index": 7, "ctest_name": "darling/one", "ctest_directory": "/build/tests"}
    ),
    "CTest registration changed after discovery",
)

# --- _selected_ctest_groups -----------------------------------------------
test = new_test(
    _ctest_catalogue=lambda _build: [],
    _resolved_diag=lambda variant: variant.get("diag") or "bare",
)


def selection(stdout: str, returncode: int = 0):
    return lambda *_args, **_kwargs: ProcessResult(returncode, stdout=stdout)


test._run_bounded = selection(json.dumps({"tests": []}))
expect_die(
    lambda: test._selected_ctest_groups(build_dir, ["-L", "x"], []),
    "refusing an empty selection",
)

registration = {
    "name": "darling/extra",
    "properties": [
        {
            "name": "LABELS",
            "value": ["env:darling"],
        }
    ],
}
test._run_bounded = selection(json.dumps({"tests": [registration]}))
groups = test._selected_ctest_groups(build_dir, [], [])
assert [group["darling"] for group in groups] == [True], groups
assert [group["tests"] for group in groups] == [["darling/extra"]], groups
assert [
    group["indices"] for group in groups
] == [[1]], "the CTest catalogue index must reach the runtime group"

test._run_bounded = selection(json.dumps({"tests": [registration]}))
expect_die(
    lambda: test._selected_ctest_groups(build_dir, [], [], env="host"),
    "matched no applicable registrations",
)

test = new_test(
    _ctest_catalogue=lambda _build: [],
    _resolved_diag=lambda variant: "bare",
    _run_bounded=selection(json.dumps({"tests": [registration]}), returncode=1),
)
expect_die(
    lambda: test._selected_ctest_groups(build_dir, [], []),
    "could not discover CTest selection",
)

# Host and guest registrations share one installation; only lifecycle differs.
host = {"name": "host/extra", "properties": [{"name": "LABELS", "value": ["env:host"]}]}
guest = {"name": "darling/other", "properties": [{"name": "LABELS", "value": ["env:darling"]}]}
test._run_bounded = selection(json.dumps({"tests": [registration, host, guest]}))
assert test._selected_ctest_groups(build_dir, [], []) == [
    {"darling": False, "tests": ["host/extra"], "indices": [2]},
    {"darling": True, "tests": ["darling/extra", "darling/other"], "indices": [1, 3]},
]

# Non-CTest workload bindings are passed through without mutating runtime env.
test = new_test(_prefix_env={"EXISTING": "yes"})
binding = ({"path": "workload"}, {"name": "t", "runner": "script"})
with test._metadata_ctest_selection([binding], env=None, diag=None, label=None) as resolved:
    assert resolved == [binding]
    assert test._prefix_env == {"EXISTING": "yes"}

# Flat structured runners join CTest without recursively registering references.
import runpy
import tempfile
import yaml

bridge = runpy.run_path(str(ROOT / "testkit/scripts/run-workload.py"))
with tempfile.TemporaryDirectory() as temporary:
    catalog = Path(temporary) / "workloads.yml"
    catalog.write_text(yaml.safe_dump({"version": 1, "workloads": [
        {"id": "host-model", "module": "darling/src/external/xnu",
         "bead": "dar-contract", "runner": "python", "script": "model.py",
         "timeout-seconds": 19, "env": "host", "diag": "bare"},
        {"id": "guest-oracle", "module": "darling", "env": "darling",
         "runner": "guest-c-fixture", "script": "oracle.c", "diag": "guarded",
         "timeout-seconds": 37},
        {"id": "capture", "module": "darling-workspace", "env": "darling",
         "runner": "diagnostic", "diagnostic": "exact-capture", "diag": "forensic",
         "timeout-seconds": 15},
        {"id": "native-oracle", "module": "darling", "env": "macos",
         "runner": "python", "script": "native.py"},
        {"id": "reference", "module": "darling", "ctest-name": "existing"},
    ]}))
    generated = "\n".join(bridge["registration_lines"](catalog, "/executor", "/bundles"))
    assert "host/workload/host-model" in generated
    assert "darling/workload/guest-oracle" in generated
    assert "reference" not in generated and "existing" not in generated
    for expected in ("env:host", "diag:bare", "bead:dar-contract", "submod:xnu",
                     "name:host-model", "TIMEOUT [=[19]=]",
                     "RESOURCE_LOCK [=[darling-prefix]=]", "RUN_SERIAL [=[TRUE]=]",
                     "--executor", "/executor", "--bundle-root", "/bundles"):
        assert expected in generated, (expected, generated)
    declarations = bridge["load_workloads"](catalog)["workloads"]
    guest = next(test for test in declarations if test["id"] == "guest-oracle")
    capture = next(test for test in declarations if test["id"] == "capture")
    assert guest["timeout-seconds"] == 37 and capture["timeout-seconds"] == 15
    assert bridge["ctest_timeout"](guest) >= 3 * 37 + 15 + 60
    assert bridge["ctest_timeout"](capture) >= 15 + 300 + 60
    assert f'TIMEOUT [=[{bridge["ctest_timeout"](capture)}]=]' in generated
    assert 'APPEND PROPERTY ENVIRONMENT "DARLING_SOURCE_ROOT=${DARLING_SRC}"' in generated
    if sys.platform != "darwin":
        assert "SKIP_RETURN_CODE [=[77]=]" in generated
        args = types.SimpleNamespace(catalog=catalog, workload="native-oracle")
        assert bridge["run_workload"](args) == 77
    quoted = bridge["cmake_argument"]("path ]=] ${not_a_variable}")
    assert quoted == "[==[path ]=] ${not_a_variable}]==]"

# Applicability comes only from installed artifact facts, never a mutable cache.
requirements = bridge["installed_requirement_problems"]
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    installed = root / "installation"
    (installed / "libexec").mkdir(parents=True)
    (installed / "libexec/oracle").write_text("fixture artifact")
    build = root / "mutable-build"
    build.mkdir()
    (build / "CMakeCache.txt").write_text("RING:BOOL=OFF\n")
    runtime = types.SimpleNamespace(root=installed, identity={"build": {
        "directory": str(build), "configuration": {"RING": "YES", "FLAVOR": "release"}}})
    declaration = {"id": "artifact-oracle", "requires-build-features": {
        "RING": "ON", "FLAVOR": "release"}, "requires-installed-paths": ["libexec/oracle"]}
    assert requirements(declaration, runtime) == []
    runtime.identity["build"]["configuration"]["RING"] = "OFF"
    problems = requirements(declaration, runtime)
    assert len(problems) == 1 and "requires installed CMake RING=ON" in problems[0]
    assert "selected RING=OFF" in problems[0]
    runtime.identity["build"]["configuration"]["RING"] = True
    assert requirements(declaration, runtime) == []
    runtime.identity["build"]["configuration"].pop("RING")
    assert "selected RING=unknown" in requirements(declaration, runtime)[0]
    runtime.identity = None
    unknown = requirements(declaration, runtime)
    assert len(unknown) == 2 and all("=unknown" in problem for problem in unknown)
    runtime.identity = {"build": {"configuration": {"RING": "ON", "FLAVOR": "debug"}}}
    assert "selected FLAVOR=debug" in requirements(declaration, runtime)[0]
    runtime.identity["build"]["configuration"]["FLAVOR"] = "release"
    (installed / "libexec/oracle").unlink()
    missing = requirements(declaration, runtime)
    assert len(missing) == 1 and "requires installed path libexec/oracle" in missing[0]
    assert "is missing libexec/oracle" in missing[0]
    (installed / "libexec/oracle").symlink_to("intentionally-missing-oracle-target")
    assert requirements(declaration, runtime) == []

print("PASS ctest-selection-argv-contract")

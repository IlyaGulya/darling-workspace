"""Contract for the CTest selection/argv logic that moved out of ``test.py``.

``CtestSelectionMixin`` owns the composition of CTest argument vectors, the
discovery rules that decide whether a selection is real, and the launcher
environment a selected runtime profile contributes.  Every assertion here is
about that observable behaviour, so a change to the moved logic fails this
contract.
"""

from __future__ import annotations

import json
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

from west_commands.test import DarlingTest  # noqa: E402
from west_commands.test_execution import ProcessResult  # noqa: E402

PROFILE_DEFINITION = {
    "source-profile": "homebrew",
    "source-module": "darling/src/external/xnu",
    "source-modules": ["darling"],
    "runtime-artifacts": [
        {
            "build-targets": ["system_kernel"],
            "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
        }
    ],
}


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

# --- _selected_ctest_runtime_groups ---------------------------------------
test = new_test(
    _ctest_runtime_profile_definitions=lambda: {"extra": dict(PROFILE_DEFINITION)},
    _ctest_catalogue=lambda _build: [],
    _resolved_diag=lambda variant: variant.get("diag") or "bare",
)


def selection(stdout: str, returncode: int = 0):
    return lambda *_args, **_kwargs: ProcessResult(returncode, stdout=stdout)


test._run_bounded = selection(json.dumps({"tests": []}))
expect_die(
    lambda: test._selected_ctest_runtime_groups(build_dir, ["-L", "x"], [], []),
    "refusing an empty selection",
)

registration = {
    "name": "darling/extra",
    "properties": [
        {
            "name": "LABELS",
            "value": ["env:darling", "runtime-profile:extra"],
        }
    ],
}
test._run_bounded = selection(json.dumps({"tests": [registration]}))
groups = test._selected_ctest_runtime_groups(build_dir, [], [], [])
assert [group["profiles"] for group in groups] == [["extra"]], groups
assert [group["tests"] for group in groups] == [["darling/extra"]], groups
assert [
    group["indices"] for group in groups
] == [[1]], "the CTest catalogue index must reach the runtime group"

test._run_bounded = selection(json.dumps({"tests": [registration]}))
expect_die(
    lambda: test._selected_ctest_runtime_groups(build_dir, [], [], [], env="host"),
    "matched no applicable registrations",
)

test = new_test(
    _ctest_runtime_profile_definitions=lambda: {"extra": dict(PROFILE_DEFINITION)},
    _ctest_catalogue=lambda _build: [],
    _resolved_diag=lambda variant: "bare",
    _run_bounded=selection(json.dumps({"tests": [registration]}), returncode=1),
)
expect_die(
    lambda: test._selected_ctest_runtime_groups(build_dir, [], [], []),
    "could not discover CTest runtime profiles",
)

# --- _metadata_ctest_selection --------------------------------------------
# A selected runtime profile contributes its launcher environment for exactly
# the window the selection is used, and the previous environment is restored.
test = new_test(
    _ctest_runtime_profile_definitions=lambda: {
        "extra": {**PROFILE_DEFINITION, "launcher-env": {"DARLING_NOOVERLAYFS": "1"}}
    },
    _prefix_env={"EXISTING": "yes"},
)
with test._metadata_ctest_selection(
    [({"path": "p.patch"}, {"name": "t", "runtime-profile": "extra"})],
    env=None,
    diag=None,
    label=None,
    additional_profiles=[],
) as resolved:
    assert resolved == [({"path": "p.patch"}, {"name": "t", "runtime-profile": "extra"})]
    assert test._prefix_env == {"EXISTING": "yes", "DARLING_NOOVERLAYFS": "1"}, test._prefix_env
assert test._prefix_env == {"EXISTING": "yes"}, "the previous launcher environment must return"

test = new_test(
    _ctest_runtime_profile_definitions=lambda: {"extra": dict(PROFILE_DEFINITION)},
)
expect_die(
    lambda: test._metadata_ctest_selection(
        [({"path": "p.patch"}, {"name": "t", "runtime-profile": "missing"})],
        env=None,
        diag=None,
        label=None,
        additional_profiles=[],
    ).__enter__(),
    "unknown runtime profile: missing",
)

test = new_test(
    _ctest_runtime_profile_definitions=lambda: {
        "one": {**PROFILE_DEFINITION, "launcher-env": {"A": "1"}},
        "two": {**PROFILE_DEFINITION, "launcher-env": {"A": "2"}},
    },
)
expect_die(
    lambda: test._metadata_ctest_selection(
        [
            (
                {"path": "p.patch"},
                {"name": "t", "runtime-profile": "one", "_ctest": {"profiles": ["two"]}},
            )
        ],
        env=None,
        diag=None,
        label=None,
        additional_profiles=[],
    ).__enter__(),
    "conflict on launcher environment A",
)

# --- _ctest_runtime_profile_context ---------------------------------------
test = new_test(_darling_prefix_env=lambda prefix: {"DPREFIX": str(prefix), **test._prefix_env})
test._prefix = tempfile.mkdtemp(prefix="west-ctest-profile-")
test._resolve_darling_launcher = lambda prefix: f"{prefix}/bin/darling"
with test._ctest_runtime_profile_context([]) as runtime_env:
    assert runtime_env["DPREFIX"] == test._prefix
    assert runtime_env["DARLING"] == f"{test._prefix}/bin/darling"
    assert runtime_env["DARLING_LAUNCHER"] == f"{test._prefix}/bin/darling"

deployments: list[tuple[list[str], bool]] = []


@contextmanager
def deployment_context(profiles, *, label_prefix, retain_deployment):
    deployments.append((list(profiles), retain_deployment))
    yield SimpleNamespace(env={"DEPLOYED": "1"})


test = new_test(_runtime_profile_deployment_context=deployment_context)
with test._ctest_runtime_profile_context(["extra"]) as runtime_env:
    assert runtime_env == {"DEPLOYED": "1"}
assert deployments == [(["extra"], False)], deployments

print("PASS ctest-selection-argv-contract")

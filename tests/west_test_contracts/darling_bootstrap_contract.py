"""Prove the direct bootstrap entrypoint is explicit: a deploy, never a source selection.

``west darling-bootstrap`` exists to take the profile indirection out of building a prefix. The source state
is the West manifest's pins, the build variant is the configured build dir, and the only thing this path may
read is an explicit deploy plan. This contract pins the properties that keep that true, because the failure
it prevents is the one the migration is about: an indirection that quietly comes back through data, and a
build dir for a different variant that gets deployed as if it were the accepted one.

What it proves:

A. the checked-in plan declares the accepted Ring variant -- the three build targets that produce the
   closure, the typed runtime mode, the launcher environment and one component per closure resource -- and
   the proof handed to the deploy half carries exactly those values;
B. a plan that names a source selection (source-mode/source-profile/source-modules/patch/lock/revision/
   materialize, and the same keys spelled otherwise) is REFUSED, naming the key, so the patch/lock/materializer
   indirection cannot be smuggled back in as data;
C. a malformed plan is refused instead of partially deployed: wrong schema, no targets, an artifact with
   neither a resource nor a deploy path, an unknown key, a non-positive deadline, a non-list deploy;
D. the cmake-define gate is a real gate: a cache that carries the variant passes, and every entry a different
   variant is missing or has inverted is reported by name -- including a boolean expectation against OFF;
E. the plan's Ring defines are the same values the manifest-native Ring provider declares, so the direct path
   cannot drift into a variant other than the one that was accepted.

The command module is imported with only its west base class stubbed: west is a `uv` tool here, not an
importable package, and the subject is the plan/define logic rather than the CLI wiring.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))


def _stub_west_command_base() -> None:
    """Make `from west.commands import WestCommand` importable without a west install."""

    if "west.commands" in sys.modules:
        return
    package = types.ModuleType("west")
    package.__path__ = []  # type: ignore[attr-defined]
    commands = types.ModuleType("west.commands")

    class WestCommand:
        def __init__(self, *args, **kwargs):
            pass

        def inf(self, *_args, **_kwargs):
            pass

        def wrn(self, *_args, **_kwargs):
            pass

        def err(self, *_args, **_kwargs):
            pass

        def die(self, message, *_args, **_kwargs):
            raise SystemExit(message)

    commands.WestCommand = WestCommand  # type: ignore[attr-defined]
    package.commands = commands  # type: ignore[attr-defined]
    sys.modules["west"] = package
    sys.modules["west.commands"] = commands


_stub_west_command_base()

import darling_bootstrap  # noqa: E402
import test_runtime  # noqa: E402

PLAN = ROOT / "testkit/darling-bootstrap.yml"
PROFILES = ROOT / "testkit/runtime-profiles.yml"
SOURCE_SELECTION_KEYS = (
    "source-mode",
    "source-profile",
    "source-modules",
    "patch",
    "lock",
    "revision",
    "materialize",
)


def _write_plan(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yml", prefix="darling-bootstrap-", delete=False
    )
    handle.write(text)
    handle.close()
    return Path(handle.name)


def _refusal(text: str) -> str:
    path = _write_plan(text)
    try:
        darling_bootstrap.load_bootstrap_plan(path)
    except ValueError as error:
        return str(error)
    raise AssertionError(f"plan was accepted: {text!r}")


def check_a_plan_declares_the_accepted_variant() -> None:
    plan = darling_bootstrap.load_bootstrap_plan(PLAN)
    assert plan.all_build_targets() == [
        "rootless_bootstrap",
        "rootless_toolchain",
        "rack_region_generation",
    ], plan.all_build_targets()
    assert plan.runtime_mode == "rootless-eunion", plan.runtime_mode
    assert plan.launcher_env.get("DARLING_ROOTLESS") == "1", plan.launcher_env
    assert plan.launcher_env.get("DARLING_ROOTLESS_SHELLSPAWN_READY_TIMEOUT_MS") == "60000"
    assert plan.launcher_env.get("DARLING_SERVER_MODE") == "balanced"
    resources = {
        artifact.get("resource")
        for artifact in plan.runtime_artifacts
        if artifact.get("resource")
    }
    assert resources == {"rootless-bootstrap", "rootless-toolchain"}, resources
    proof = plan.proof()
    assert proof["runtime-mode"] == "rootless-eunion"
    assert proof["runtime-artifacts"] == plan.runtime_artifacts
    assert proof["launcher-env"] == plan.launcher_env
    assert plan.smoke_timeout_seconds and plan.smoke_timeout_seconds > 0
    assert darling_bootstrap.SMOKE_MARKER in plan.smoke_script, plan.smoke_script
    print("A ok: the checked-in plan declares the accepted Ring variant and its closure")


def check_b_a_source_selection_is_refused() -> None:
    baseline = PLAN.read_text()
    for key in SOURCE_SELECTION_KEYS:
        message = _refusal(baseline + f"\n{key}: smuggled\n")
        assert key in message, (key, message)
        assert "source selection" in message, (key, message)
    # The same indirection under its own spelling is still the same indirection.
    message = _refusal(baseline + "\nprofiles: [manifest-ring-on]\n")
    assert "profiles" in message, message
    print("B ok: a plan naming a source selection is refused, and the refusal names the key")


def check_c_a_malformed_plan_is_refused() -> None:
    artifact = "{module: m, resource: rootless-bootstrap}"
    cases = {
        "not a mapping": "- one\n- two\n",
        "wrong schema": f"schema: 2\nbuild-targets: [x]\nruntime-artifacts: [{artifact}]\n",
        "no build targets": f"schema: 1\nbuild-targets: []\nruntime-artifacts: [{artifact}]\n",
        "artifact without resource or deploy": "schema: 1\nbuild-targets: [x]\nruntime-artifacts: [{module: m}]\n",
        "artifact without module": "schema: 1\nbuild-targets: [x]\nruntime-artifacts: [{resource: rootless-bootstrap}]\n",
        "unknown key": f"schema: 1\nbuild-targets: [x]\nwidget: 3\nruntime-artifacts: [{artifact}]\n",
        "non-positive deadline": f"schema: 1\nbuild-targets: [x]\nsmoke-timeout-seconds: 0\nruntime-artifacts: [{artifact}]\n",
        "deploy is not a list": "schema: 1\nbuild-targets: [x]\nruntime-artifacts: [{module: m, deploy: nope}]\n",
        "no artifacts": "schema: 1\nbuild-targets: [x]\nruntime-artifacts: []\n",
    }
    for name, text in cases.items():
        message = _refusal(text)
        assert message, name
    print(f"C ok: {len(cases)} malformed plans are refused instead of partially deployed")


def check_d_the_configure_gate_is_real() -> None:
    plan = darling_bootstrap.load_bootstrap_plan(PLAN)

    def cache(text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix="CMakeCache.txt", prefix="darling-bootstrap-", delete=False
        )
        handle.write(text)
        handle.close()
        return Path(handle.name)

    matching = "".join(
        f"{name}:BOOL={'ON' if value is True else 'OFF'}\n"
        for name, value in plan.cmake_defines.items()
    )
    assert darling_bootstrap.compare_cmake_defines(cache(matching), plan.cmake_defines) == []
    other = matching.replace("DARLING_RING_TRANSPORT:BOOL=ON", "DARLING_RING_TRANSPORT:BOOL=OFF")
    other = other.replace("DARLING_SKIP_DRIFT_GATE:BOOL=ON\n", "")
    problems = darling_bootstrap.compare_cmake_defines(cache(other), plan.cmake_defines)
    joined = "; ".join(problems)
    assert "DARLING_RING_TRANSPORT" in joined and "'OFF'" in joined, joined
    assert "DARLING_SKIP_DRIFT_GATE" in joined and "absent" in joined, joined
    # A boolean expectation must not accept the value a different variant would leave behind.
    inverted = darling_bootstrap.compare_cmake_defines(
        cache("DARLING_EUNION:BOOL=OFF\n"), {"DARLING_EUNION": True}
    )
    assert inverted and "DARLING_EUNION" in inverted[0], inverted
    print("D ok: the define gate accepts the variant and reports every entry a wrong one lacks")


def check_e_ring_defines_match_the_manifest_provider() -> None:
    plan = darling_bootstrap.load_bootstrap_plan(PLAN)
    definitions = test_runtime.load_ctest_runtime_profiles(PROFILES)
    reference = definitions["manifest-ring-on"]["cmake-defines"]
    assert plan.cmake_defines == reference, (plan.cmake_defines, reference)
    print(
        "E ok: the plan's Ring defines are the values the manifest-native Ring provider declares"
    )


def check_f_deploy_set_and_environment_match_the_manifest_provider() -> None:
    """The direct path replaces the profile LOOKUP, not the accepted runtime.

    If the plan's closure or environment drifts from the accepted Ring provider, this entrypoint would
    bootstrap something other than the runtime that was accepted, and no receipt would notice -- the receipt
    records what was deployed, not what was intended.
    """

    plan = darling_bootstrap.load_bootstrap_plan(PLAN)
    definitions = test_runtime.load_ctest_runtime_profiles(PROFILES)
    reference = definitions["manifest-ring-on"]
    assert plan.runtime_artifacts == reference["runtime-artifacts"], (
        plan.runtime_artifacts,
        reference["runtime-artifacts"],
    )
    assert plan.launcher_env == {
        key: str(value) for key, value in reference["launcher-env"].items()
    }, (plan.launcher_env, reference["launcher-env"])
    assert plan.runtime_mode == reference["runtime-mode"], (
        plan.runtime_mode,
        reference["runtime-mode"],
    )
    print(
        "F ok: the plan's deploy set, launcher environment and runtime mode are the accepted provider's"
    )


def check_g_the_user_namespace_precondition_is_named() -> None:
    """A host that refuses unprivileged user namespaces must be named, not timed out.

    Measured: such a host turned every bootstrap -- including a prefix that had booted earlier the same
    day -- into a shellspawn readiness timeout whose cause was only in the kernel log.
    """

    handle = tempfile.NamedTemporaryFile("w", suffix="sysctl", prefix="userns-", delete=False)
    handle.write("1\n")
    handle.close()
    problem = darling_bootstrap.unprivileged_userns_problem(
        probe=lambda: (1, "unshare: write failed /proc/self/uid_map: Operation not permitted"),
        sysctl_path=Path(handle.name),
    )
    assert problem is not None and "user namespaces are unavailable" in problem, problem
    assert "apparmor_restrict_unprivileged_userns=1" in problem, problem
    assert "mount and PID namespaces" in problem, problem
    assert (
        darling_bootstrap.unprivileged_userns_problem(
            probe=lambda: (0, ""), sysctl_path=Path(handle.name)
        )
        is None
    )
    # A permissive sysctl must not turn a failing probe into success.
    permissive = tempfile.NamedTemporaryFile("w", suffix="sysctl", prefix="userns-", delete=False)
    permissive.write("0\n")
    permissive.close()
    problem = darling_bootstrap.unprivileged_userns_problem(
        probe=lambda: (1, "unshare: Operation not permitted"),
        sysctl_path=Path(permissive.name),
    )
    assert problem is not None and "unavailable" in problem, problem
    print("G ok: an unusable-namespace host is named up front instead of timing out")


def check_h_the_inherited_nofile_is_recorded() -> None:
    """A run must name the limit it passes on, and must not fail when the host cannot report one."""

    soft, hard = darling_bootstrap.inherited_nofile_limits(
        getrlimit=lambda _resource_id: (4096, 1048576)
    )
    assert (soft, hard) == (4096, 1048576), (soft, hard)

    def refuse(_resource_id):
        raise OSError("no such limit")

    assert darling_bootstrap.inherited_nofile_limits(getrlimit=refuse) == (None, None)
    print("H ok: the inherited NOFILE soft/hard is recorded, and an unreadable one is not fatal")


def check_i_the_install_prefix_is_part_of_the_configuration() -> None:
    """The launcher bakes INSTALL_PREFIX, so a build dir for another install root cannot boot this prefix.

    Measured: a build dir left at the default /usr/local produced a launcher that exec'd
    /usr/local/bin/darlingserver, got ENOENT and printed "Failed to start darlingserver", after which the
    launcher polled shellspawn.sock for its whole timeout with no server, no launchd and no shellspawn
    running. The reader and the documented configure line are pinned here; the gate itself runs before any
    deploy.
    """

    handle = tempfile.NamedTemporaryFile("w", suffix="CMakeCache.txt", prefix="install-", delete=False)
    handle.write(
        "# For build in /scratch/build\n"
        "CMAKE_INSTALL_PREFIX:PATH=/usr/local\n"
        "DARLING_EUNION:BOOL=ON\n"
    )
    handle.close()
    cache = Path(handle.name)
    assert darling_bootstrap.build_install_prefix(cache) == "/usr/local"

    missing = tempfile.NamedTemporaryFile("w", suffix="CMakeCache.txt", prefix="install-", delete=False)
    missing.write("DARLING_EUNION:BOOL=ON\n")
    missing.close()
    assert darling_bootstrap.build_install_prefix(Path(missing.name)) is None

    plan = darling_bootstrap.load_bootstrap_plan(PLAN)
    line = plan.cmake_configure_line(Path("/topdir"), Path("/prefix/here"))
    assert "-DCMAKE_INSTALL_PREFIX=/prefix/here" in line, line
    print("I ok: the install prefix is read, and the documented configure line names it")


def main() -> int:
    check_a_plan_declares_the_accepted_variant()
    check_b_a_source_selection_is_refused()
    check_c_a_malformed_plan_is_refused()
    check_d_the_configure_gate_is_real()
    check_e_ring_defines_match_the_manifest_provider()
    check_f_deploy_set_and_environment_match_the_manifest_provider()
    check_g_the_user_namespace_precondition_is_named()
    check_h_the_inherited_nofile_is_recorded()
    check_i_the_install_prefix_is_part_of_the_configuration()
    print("PASS darling-bootstrap-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

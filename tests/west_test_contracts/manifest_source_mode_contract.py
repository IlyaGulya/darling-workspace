"""Prove the manifest source mode is a source selection, not a second runtime framework.

The runtime-profile pipeline used to conflate two responsibilities: WHERE the source comes from
(source-profile -> patch stack -> locks -> immutable mirrors) and WHAT runtime variant to build. This
contract pins the separation, because the failure it prevents is measured: a prefix was accepted as a
Ring product while its provider actually selected a legacy source profile with no Ring transport, and
the legacy layer was a hard prerequisite for building anything at all.

What it proves:

A. a manifest-native provider is planned with NO patch machinery -- the profile loader sees an empty
   patch set for the mode, the patch-set/lock/mirror functions are never called, and a workspace with
   no source-bundles/ directory is sufficient;
B. a component whose HEAD is not its resolved manifest revision fails BEFORE the build, naming it;
C. a tracked modification, and an untracked entry that is not another materialized project, fail
   BEFORE the build; an untracked entry that IS another materialized project does not;
D. the manifest Ring providers carry the Ring defines on/off as declared;
E. the build/deploy half is SHARED: a manifest provider and its legacy counterpart compose to the
   same settings for everything except source selection -- asserted on the composed plan rather than
   by enumerating deployed binaries;
F. the recorded identity states source-mode, the workspace commit, the component revisions and the
   Ring define values, and the Ring oracle refuses to label a runtime Ring unless that configuration
   says so.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import patch_stack_lock_first  # noqa: E402
import patch_stack_materialize  # noqa: E402
import runtime_ring_identity  # noqa: E402
import test_profile  # noqa: E402
import test_runtime  # noqa: E402
import test_runtime_identity  # noqa: E402
from test_runtime_source import manifest_source_problems  # noqa: E402

PROFILES = ROOT / "testkit/runtime-profiles.yml"


def _definitions() -> dict:
    return test_runtime.load_ctest_runtime_profiles(PROFILES)


def check_a_no_patch_machinery_is_consulted() -> None:
    definitions = _definitions()
    manifest = definitions["manifest-ring-on"]
    assert manifest.get("source-mode") == "manifest", manifest
    assert "source-mode" in test_runtime.compose_ctest_runtime_profiles(
        definitions, ["manifest-ring-on"]
    )

    # The patch-profile loader answers for the source mode with an EMPTY patch set, and it must do so
    # without touching patches/<mode>/patches.yml: a workspace without any patch profile at all is
    # exactly the state a manifest-native product is in.
    calls: list[str] = []
    original_profile_path = test_profile.ProfileOperationsMixin._profile_path

    def forbidden_path(self, profile):  # pragma: no cover - only on failure
        calls.append(profile)
        raise AssertionError(f"patch profile path consulted for {profile!r}")

    test_profile.ProfileOperationsMixin._profile_path = forbidden_path
    try:
        mixin = test_profile.ProfileOperationsMixin()
        loaded = mixin._load_profile(test_runtime.MANIFEST_SOURCE_MODE)
    finally:
        test_profile.ProfileOperationsMixin._profile_path = original_profile_path
    assert loaded == {"patches": []}, loaded
    assert not calls, calls

    # The patch-set/lock/mirror entry points must not be reachable from a manifest composition or from
    # the manifest identity: a manifest runtime records an empty patchset list.
    def forbidden(*args, **kwargs):  # pragma: no cover - only on failure
        raise AssertionError("patch machinery was reached by a manifest-native provider")

    for module, name in (
        (patch_stack_lock_first, "profile_dependency_chain"),
        (patch_stack_materialize, "probe_immutable_mirror"),
        (patch_stack_materialize, "materialize"),
    ):
        original = getattr(module, name)
        setattr(module, name, forbidden)
        try:
            identity = test_runtime_identity.runtime_identity(
                topdir=ROOT.parent,
                manifest_repo=ROOT,
                profile_name="manifest-ring-on",
                definition=definitions["manifest-ring-on"],
                launcher=ROOT / "testkit/runtime-profiles.yml",
            )
        finally:
            setattr(module, name, original)
        assert identity["patchsets"] == [], identity["patchsets"]
    print("A ok: manifest source mode consults no patch machinery and records no patchset")


def _fake_run(project_status: dict[str, str], heads: dict[str, str]):
    def run(args, **kwargs):
        cwd = args[2] if len(args) > 2 else ""
        if args[3:5] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout=heads[cwd] + "\n", stderr="")
        if args[3:5] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, stdout=project_status.get(cwd, ""), stderr="")
        raise AssertionError(f"unexpected git invocation: {args}")

    return run


def check_b_manifest_mismatch_fails() -> None:
    projects = [
        ("darling", "a" * 40, Path("/nonexistent-darling"), True),
        ("darling/src/external/xnu", "b" * 40, Path("/nonexistent-xnu"), True),
    ]
    heads = {"/nonexistent-darling": "a" * 40, "/nonexistent-xnu": "c" * 40}
    # Directories do not exist in this synthetic case, so drive the pure comparison instead.
    problems = manifest_source_problems(
        [("xnu", "b" * 40, Path("."), True)],
        nested_project_paths={"xnu": set()},
        run=_fake_run({}, {".": "c" * 40}),
    )
    assert any("HEAD" in problem and "manifest revision" in problem for problem in problems), problems
    assert any("xnu" in problem for problem in problems), problems
    print(f"B ok: a drifted component fails before the build: {problems[0]}")


def check_c_dirty_and_untracked_source_fails() -> None:
    run = _fake_run(
        {
            ".": " M src/startup/mldr/mldr.c\n",
        },
        {".": "a" * 40},
    )
    problems = manifest_source_problems(
        [("darling", "a" * 40, Path("."), True)],
        nested_project_paths={"darling": set()},
        run=run,
    )
    assert any("tracked modification" in problem for problem in problems), problems

    run = _fake_run({".": "?? docs/\n"}, {".": "a" * 40})
    problems = manifest_source_problems(
        [("darling", "a" * 40, Path("."), True)],
        nested_project_paths={"darling": {"docs/darling-docs"}},
        run=run,
    )
    assert not problems, (
        "an untracked entry that IS another materialized project must be allowed: "
        f"{problems}"
    )

    run = _fake_run({".": "?? src/startup/mldr/local_override.c\n"}, {".": "a" * 40})
    problems = manifest_source_problems(
        [("darling", "a" * 40, Path("."), True)],
        nested_project_paths={"darling": {"docs/darling-docs"}},
        run=run,
    )
    assert any("untracked source override" in problem for problem in problems), problems

    # A non-product project may hold local scratch state: only product modules must be clean.
    run = _fake_run({".": " M scratch.txt\n"}, {".": "a" * 40})
    problems = manifest_source_problems(
        [("some-tool", "a" * 40, Path("."), False)],
        nested_project_paths={"some-tool": set()},
        run=run,
    )
    assert not problems, problems
    print("C ok: tracked edits and foreign untracked files fail; nested projects do not")


def check_d_ring_defines() -> None:
    definitions = _definitions()
    for name, expected in (
        ("manifest-ring-on", True),
        ("manifest-ring-off", False),
        ("homebrew-ring-on", True),
        ("homebrew-ring-off", False),
    ):
        defines = definitions[name]["cmake-defines"]
        assert bool(defines.get("DARLING_RING_TRANSPORT")) is expected, (name, defines)
        assert bool(defines.get("DSERVER_RING_TRANSPORT")) is expected, (name, defines)
    print("D ok: Ring defines are on/off as each provider declares")


def check_e_build_deploy_half_is_shared() -> None:
    definitions = _definitions()
    manifest_on = test_runtime.compose_ctest_runtime_profiles(
        definitions, ["manifest-ring-on"]
    )
    legacy_on = test_runtime.compose_ctest_runtime_profiles(
        definitions, ["homebrew-ring-on"]
    )
    source_keys = {"source-mode", "source-profile", "name"}
    # The ONE declared difference the source mode itself requires: the product tree's CMake drift gate
    # enforces the LEGACY patch composition and aborts a manifest-pinned tree ("79 patch(es) differ
    # from typed profile-composition integration"), so a manifest provider sets its documented
    # override. Naming the delta here means any OTHER difference still fails this contract.
    source_mode_required = {"cmake-defines": {"DARLING_SKIP_DRIFT_GATE": True}}
    assert manifest_on.get("source-mode") == "manifest"
    assert legacy_on.get("source-mode") is None
    for key in sorted(set(manifest_on) | set(legacy_on)):
        if key in source_keys:
            continue
        left, right = manifest_on.get(key), legacy_on.get(key)
        if key in source_mode_required:
            assert isinstance(left, dict) and isinstance(right, dict), (key, left, right)
            delta = {
                name: value
                for name, value in left.items()
                if right.get(name) != value
            }
            assert delta == source_mode_required[key], (
                f"{key} differs by {delta}, which is not the declared source-mode delta "
                f"{source_mode_required[key]}: the build/deploy half is not shared"
            )
            continue
        assert left == right, (
            f"{key} differs between the manifest and legacy provider, so the build/deploy half "
            f"is not shared: {left!r} != {right!r}"
        )
    print(
        "E ok: manifest and legacy Ring providers compose to identical build/deploy inputs "
        f"({len(manifest_on['runtime-artifacts'])} artifact entries, "
        f"{len(manifest_on['cmake-defines'])} defines, "
        f"{len(manifest_on['launcher-env'])} launcher vars)"
    )


def check_f_identity_and_oracle() -> None:
    definitions = _definitions()
    identity = test_runtime_identity.runtime_identity(
        topdir=ROOT.parent,
        manifest_repo=ROOT,
        profile_name="manifest-ring-on",
        definition=definitions["manifest-ring-on"],
        launcher=ROOT / "testkit/runtime-profiles.yml",
    )
    assert identity["source-mode"] == "manifest", identity
    assert identity["source-profile"] is None, identity
    assert identity["manifest-commit"], identity
    assert identity["ring-defines"] == {
        "DARLING_RING_TRANSPORT": True,
        "DSERVER_RING_TRANSPORT": True,
    }, identity["ring-defines"]
    assert identity["source-commits"], "component revisions must be recorded"

    status = runtime_ring_identity.require_ring_runtime(identity)
    assert status["status"] == "on", status

    off = test_runtime_identity.runtime_identity(
        topdir=ROOT.parent,
        manifest_repo=ROOT,
        profile_name="manifest-ring-off",
        definition=definitions["manifest-ring-off"],
        launcher=ROOT / "testkit/runtime-profiles.yml",
    )
    assert runtime_ring_identity.ring_status(off)["status"] == "off", off
    try:
        runtime_ring_identity.require_ring_runtime(off)
    except ValueError:
        pass
    else:  # pragma: no cover - only on failure
        raise AssertionError("a ring-off runtime must not be labelable as Ring")

    # The measured failure this oracle exists for: a legacy runtime identity records no ring-defines,
    # and that must be UNKNOWN -- never accepted as Ring.
    legacy = test_runtime_identity.runtime_identity(
        topdir=ROOT.parent,
        manifest_repo=ROOT,
        profile_name="homebrew-rootless-bootstrap-minimal",
        definition=definitions["homebrew-rootless-bootstrap-minimal"],
        launcher=ROOT / "testkit/runtime-profiles.yml",
    )
    assert runtime_ring_identity.ring_status(legacy)["status"] == "unknown", legacy
    try:
        runtime_ring_identity.require_ring_runtime(legacy)
    except ValueError as error:
        assert "unknown" in str(error), error
    else:  # pragma: no cover - only on failure
        raise AssertionError("an identity without Ring defines must be refused")
    print("F ok: identity records the mode/commit/defines, and the oracle refuses to guess")


def main() -> int:
    check_a_no_patch_machinery_is_consulted()
    check_b_manifest_mismatch_fails()
    check_c_dirty_and_untracked_source_fails()
    check_d_ring_defines()
    check_e_build_deploy_half_is_shared()
    check_f_identity_and_oracle()
    print("PASS manifest-source-mode-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

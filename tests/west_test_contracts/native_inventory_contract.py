"""Inventory preserves patch bindings, rejects unreviewed cases and never runs them."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "scripts/audit-test-registration.py"
sys.path.insert(0, str(ROOT))
from west_commands.test_manifest import normalize_test_profile

with tempfile.TemporaryDirectory(prefix="west-native-inventory-contract-") as temporary:
    top = Path(temporary)
    workspace = top / "manifest"
    workspace.mkdir()
    (top / ".west").mkdir()
    (top / ".west/config").write_text("[manifest]\npath = manifest\nfile = west.yml\n")
    (workspace / "west.yml").write_text(
        "manifest:\n  projects:\n    - name: fixture-owner\n"
        "      path: src/fixture\n      url: https://example.invalid/fixture\n"
        "  self:\n    path: manifest\n"
    )
    (workspace / "west_commands").symlink_to(ROOT / "west_commands", target_is_directory=True)
    fixture_root = top / "src/fixture"
    fixture_root.mkdir(parents=True)
    (fixture_root / "probe.c").write_text("int main(void) { return 0; }\n")
    profile = workspace / "patches/probe"
    profile.mkdir(parents=True)
    profile_data = {
        "test-profiles": {
            "shared": {"runner": "guest-c-fixture", "runs": "guest",
                       "repo": "fixture-owner", "script": "probe.c"}
        },
        "patches": [
            {"path": name, "module": "src/fixture", "bead": owner,
             "tests": [{"name": "shared_probe", "use": "shared"}]}
            for name, owner in (("first.patch", "first-owner"), ("second.patch", "second-owner"))
        ],
    }
    # JSON is a YAML subset: exercise the real production normalizer.
    (profile / "patches.yml").write_text(json.dumps(profile_data))
    (workspace / "audits").mkdir()
    policy = workspace / "audits/native-test-applicability.json"
    reviews = {}
    for patch_data in normalize_test_profile(profile_data)["patches"]:
        declaration = {"project": "src/fixture", "test": patch_data["tests"][0]}
        digest = hashlib.sha256(json.dumps(
            declaration, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        reviews[f"probe:{patch_data['path']}:1"] = {
            "policy": "native_reference", "identity_sha256": digest,
            "reason": "Synthetic public scenario for the inventory contract.",
        }
    policy.write_text(json.dumps({
        "groups": [{"policy": "native_reference",
                    "reason": "Synthetic public scenario for the inventory contract."}],
        "bindings": reviews,
    }))
    (workspace / "testkit").mkdir()
    built_marker = top / "product-was-built"
    executed_marker = top / "test-was-executed"
    (workspace / "testkit/CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(inventory_probe NONE)\n"
        "enable_testing()\n"
        f'add_custom_target(product ALL COMMAND "${{CMAKE_COMMAND}}" -E touch "{built_marker}")\n'
        f'add_test(NAME native_probe COMMAND "${{CMAKE_COMMAND}}" -E touch "{executed_marker}")\n'
        'set_tests_properties(native_probe PROPERTIES LABELS "env:macos")\n'
    )

    def collect(destination):
        return subprocess.run(
            [sys.executable, "-B", str(COLLECTOR), str(destination),
             "--workspace", str(workspace)],
            cwd=workspace, capture_output=True, text=True, timeout=30,
        )

    output = top / "known"
    result = collect(output)
    assert result.returncode == 0, result.stdout + result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    assert {(row["patch"], row["bead"]) for row in inventory["bindings"]} == {
        ("first.patch", "first-owner"), ("second.patch", "second-owner")
    }, "a shared fixture must retain both independent patch ownership bindings"
    assert all(row["asset_exists"] for row in inventory["bindings"]), \
        "West project aliases must resolve fixture assets rather than report false gaps"
    assert inventory["summary"]["native_policy"] == {"native_reference": 2}, \
        "inherited guest profile bindings must be included in applicability accounting"
    assert {case["name"] for case in inventory["workspace_ctest"]} == {"native_probe"}
    assert not built_marker.exists(), "discovery must not build product targets"
    assert not executed_marker.exists(), "discovery must not execute registered cases"

    def reject_changed_profile(changed, directory, binding):
        (profile / "patches.yml").write_text(json.dumps(changed))
        rejected_output = top / directory
        rejected = collect(rejected_output)
        assert rejected.returncode != 0, "unreviewed binding was silently accepted"
        assert binding in rejected.stderr, \
            "failure must identify the affected binding, not unrelated infrastructure"
        assert not (rejected_output / "inventory.json").exists(), \
            "a rejected census must not publish a successful inventory snapshot"

    # A new binding cannot borrow approval from an identically named case.
    duplicate_name = deepcopy(profile_data)
    duplicate_name["patches"].append({
        "path": "third.patch", "module": "src/fixture",
        "tests": [{"name": "shared_probe", "use": "shared"}],
    })
    reject_changed_profile(duplicate_name, "same-name", "probe:third.patch:1")

    # Existing ownership/name cannot bless changed scenario parameters either.
    changed_scenario = deepcopy(profile_data)
    changed_scenario["patches"][1]["tests"][0]["run-args"] = ["different-scenario"]
    reject_changed_profile(changed_scenario, "changed-scenario", "probe:second.patch:1")

    # Compile-tier ABI/header checks require review just like runtime cases.
    compile_case = deepcopy(profile_data)
    compile_case["patches"].append({
        "path": "compile.patch", "module": "src/fixture",
        "tests": [{"name": "shared_probe", "runner": "c-fixture", "runs": "host",
                   "coverage-tier": "compile", "repo": "fixture-owner", "script": "probe.c"}],
    })
    reject_changed_profile(compile_case, "unreviewed-compile", "probe:compile.patch:1")

    def reject_and_capture(changed, directory):
        (profile / "patches.yml").write_text(json.dumps(changed))
        rejected_output = top / directory
        rejected = collect(rejected_output)
        assert rejected.returncode != 0, "unreviewed binding was silently accepted"
        assert not (rejected_output / "inventory.json").exists(), \
            "a rejected census must not publish a successful inventory snapshot"
        return rejected.stderr

    # Every gap is reported at once. Stopping at the first one makes a metadata
    # edit look like a single missing review while the rest stay invisible.
    several_gaps = deepcopy(profile_data)
    for name in ("fourth.patch", "fifth.patch"):
        several_gaps["patches"].append({
            "path": name, "module": "src/fixture",
            "tests": [{"name": "shared_probe", "use": "shared"}],
        })
    several_gaps["patches"][1]["tests"][0]["run-args"] = ["changed-declaration"]
    stderr = reject_and_capture(several_gaps, "several-gaps")
    for binding in ("probe:fourth.patch:1", "probe:fifth.patch:1", "probe:second.patch:1"):
        assert binding in stderr, \
            f"every applicability gap must be named, not only the first: {binding} missing"
    assert "3 applicability problem(s)" in stderr, \
        "the report must state how many problems were found"

    # A policy entry whose binding no longer exists is a re-recording artifact:
    # silently keeping it lets the review record drift away from the metadata.
    (profile / "patches.yml").write_text(json.dumps(profile_data))
    orphaned = dict(reviews)
    orphaned["probe:ghost.patch:1"] = {
        "policy": "native_reference", "identity_sha256": "0" * 64,
        "reason": "Synthetic public scenario for the inventory contract.",
    }
    policy.write_text(json.dumps({
        "groups": [{"policy": "native_reference",
                    "reason": "Synthetic public scenario for the inventory contract."}],
        "bindings": orphaned,
    }))
    try:
        orphan_stderr = reject_and_capture(profile_data, "orphan-policy")
        assert "probe:ghost.patch:1" in orphan_stderr, \
            "a policy entry with no binding must be reported, not ignored"
    finally:
        policy.write_text(json.dumps({
            "groups": [{"policy": "native_reference",
                        "reason": "Synthetic public scenario for the inventory contract."}],
            "bindings": reviews,
        }))
    assert not built_marker.exists() and not executed_marker.exists()

print("PASS native-inventory-contract")

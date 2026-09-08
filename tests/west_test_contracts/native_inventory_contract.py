"""Inventory preserves patch bindings, rejects unreviewed cases and never runs them."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "scripts/audit-test-registration.py"

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
    policy.write_text(json.dumps({
        "groups": [{"names": ["shared_probe"], "policy": "native_reference",
                    "reason": "Synthetic public scenario for the inventory contract."}],
        "aliases": {},
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

    # A newly introduced runtime binding must not inherit a default PASS/policy.
    profile_data["patches"][1]["tests"][0]["name"] = "unreviewed_probe"
    (profile / "patches.yml").write_text(json.dumps(profile_data))
    rejected_output = top / "unreviewed"
    rejected = collect(rejected_output)
    assert rejected.returncode != 0, "unreviewed runtime binding was silently accepted"
    assert "probe:second.patch:1" in rejected.stderr, \
        "failure must identify the unreviewed binding, not unrelated infrastructure"
    assert not (rejected_output / "inventory.json").exists(), \
        "a rejected census must not publish a successful inventory snapshot"
    assert not built_marker.exists() and not executed_marker.exists()

print("PASS native-inventory-contract")

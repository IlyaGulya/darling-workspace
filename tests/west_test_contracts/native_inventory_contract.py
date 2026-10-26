"""Inventory preserves distinct workloads, rejects unreviewed inputs and never runs them."""
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
from west_commands.test_manifest import normalize_workloads

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
    (workspace / "testkit").mkdir()
    catalog_path = workspace / "testkit/workloads.yml"
    catalog_data = {"version": 1, "workloads": [
        {"id": identifier, "module": "src/fixture", "bead": owner,
         "name": "shared_probe", "runner": "guest-c-fixture", "env": "darling",
         "repo": "fixture-owner", "script": "probe.c"}
        for identifier, owner in (("first", "first-owner"), ("second", "second-owner"))
    ]}
    catalog_path.write_text(json.dumps(catalog_data))
    (workspace / "audits").mkdir()
    policy = workspace / "audits/native-test-applicability.json"
    reviews = {}
    for test in normalize_workloads(catalog_data)["workloads"]:
        declaration = {"project": "src/fixture", "test": {key: value for key, value in test.items() if key != "id"}}
        digest = hashlib.sha256(json.dumps(
            declaration, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        reviews[test["id"]] = {
            "policy": "native_reference", "identity_sha256": digest,
            "reason": "Synthetic public scenario for the inventory contract.",
        }

    def write_policy(bindings):
        policy.write_text(json.dumps({
            "groups": [{"policy": "native_reference", "reason": "Synthetic public scenario."}],
            "bindings": bindings,
        }))

    write_policy(reviews)
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
            [sys.executable, "-B", str(COLLECTOR), str(destination), "--workspace", str(workspace)],
            cwd=workspace, capture_output=True, text=True, timeout=30,
        )

    output = top / "known"
    result = collect(output)
    assert result.returncode == 0, result.stdout + result.stderr
    inventory = json.loads((output / "inventory.json").read_text())
    assert {(row["binding_id"], row["bead"]) for row in inventory["bindings"]} == {
        ("first", "first-owner"), ("second", "second-owner")
    }, "a shared fixture must retain every distinct workload binding"
    assert all(row["asset_exists"] for row in inventory["bindings"]), "West aliases must resolve assets"
    assert inventory["summary"]["native_policy"] == {"native_reference": 2}
    assert {case["name"] for case in inventory["workspace_ctest"]} == {"native_probe"}
    assert not built_marker.exists(), "discovery must not build product targets"
    assert not executed_marker.exists(), "discovery must not execute registered cases"

    def reject(changed, directory, *bindings):
        catalog_path.write_text(json.dumps(changed))
        destination = top / directory
        result = collect(destination)
        assert result.returncode != 0, "unreviewed declaration was accepted"
        assert not (destination / "inventory.json").exists(), "rejection published a success snapshot"
        for binding in bindings:
            assert binding in result.stderr, (binding, result.stderr)
        return result.stderr

    duplicate = deepcopy(catalog_data)
    duplicate["workloads"].append({**duplicate["workloads"][0], "id": "third"})
    reject(duplicate, "same-name", "third")

    changed = deepcopy(catalog_data)
    changed["workloads"][1]["run-args"] = ["different-scenario"]
    reject(changed, "changed-scenario", "second")

    compile_case = deepcopy(catalog_data)
    compile_case["workloads"].append({"id": "compile", "module": "src/fixture", "name": "shared_probe",
        "runner": "c-fixture", "env": "host", "coverage-tier": "compile", "repo": "fixture-owner", "script": "probe.c"})
    reject(compile_case, "unreviewed-compile", "compile")

    several = deepcopy(changed)
    for identifier in ("fourth", "fifth"):
        several["workloads"].append({**several["workloads"][0], "id": identifier})
    errors = reject(several, "several-gaps", "fourth", "fifth", "second")
    assert "3 applicability problem(s)" in errors, errors

    orphaned = {**reviews, "ghost": {"policy": "native_reference", "identity_sha256": "0" * 64,
        "reason": "Synthetic public scenario."}}
    write_policy(orphaned)
    try:
        reject(catalog_data, "orphan-policy", "ghost")
    finally:
        write_policy(reviews)
    assert not built_marker.exists() and not executed_marker.exists()

print("PASS native-inventory-contract")

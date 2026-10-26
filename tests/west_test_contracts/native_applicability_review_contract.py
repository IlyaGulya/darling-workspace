"""Applicability review carries explicit stable-id renames without guessing policy."""
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

GROUPS = [
    {"policy": "native_reference", "reason": "Synthetic public reference."},
    {"policy": "darling_internal_gate", "reason": "Synthetic internal gate."},
]


def scenario(identifier, name, argument):
    return {"id": identifier, "name": name, "module": "src/fixture", "runner": "guest-c-fixture",
            "env": "darling", "repo": "fixture-owner", "script": "probe.c", "run-args": [argument]}


def reviews(catalog):
    entries = {}
    for test in normalize_workloads(catalog)["workloads"]:
        declaration = {"project": "src/fixture", "test": {key: value for key, value in test.items() if key != "id"}}
        digest = hashlib.sha256(json.dumps(declaration, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        entries[test["id"]] = {"name": test["name"], "identity_sha256": digest,
            "policy": "native_reference" if test["name"] == "alpha_probe" else "darling_internal_gate",
            "reason": f"Reviewed synthetic scenario for {test['name']}."}
    return entries


with tempfile.TemporaryDirectory(prefix="west-applicability-review-contract-") as temporary:
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
    (workspace / "audits").mkdir()
    record = workspace / "audits/native-test-applicability.json"
    (workspace / "testkit").mkdir()
    catalog_path = workspace / "testkit/workloads.yml"
    # No CMakeLists: a review command attempting configuration cannot pass.
    catalog = {"version": 1, "workloads": [
        scenario("alpha", "alpha_probe", "alpha"),
        scenario("beta", "beta_probe", "beta"),
        scenario("first", "first_probe", "first"),
        scenario("second", "second_probe", "second"),
        scenario("third", "third_probe", "third"),
    ]}

    def write_catalog(data):
        catalog_path.write_text(json.dumps(data))

    def write_record(entries):
        record.write_text(json.dumps({"qualification": "Synthetic reviewed applicability.",
            "groups": GROUPS, "bindings": entries}, indent=2) + "\n")

    def run(*arguments):
        return subprocess.run([sys.executable, "-B", str(COLLECTOR), *arguments,
            "--workspace", str(workspace)], cwd=workspace, capture_output=True, text=True, timeout=60)

    def report():
        before = record.read_bytes()
        result = run("report")
        assert result.returncode in (0, 1), result.stdout + result.stderr
        assert record.read_bytes() == before, "report changed reviewed policy"
        return result, json.loads(result.stdout)

    def reviewed(*arguments):
        result = run(*arguments)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def refuse(*arguments):
        before = record.read_bytes()
        result = run(*arguments)
        assert result.returncode != 0, result.stdout + result.stderr
        assert record.read_bytes() == before, "refused carry changed reviewed policy"
        return result

    write_catalog(catalog)
    entries = reviews(catalog)
    write_record(entries)
    result, document = report()
    assert result.returncode == 0, result.stderr
    assert document["counts"] == {"bindings": 5, "policy_records": 5, "reviewed_bindings": 5,
        "bindings_without_policy": 0, "identity_mismatches": 0, "policy_records_without_binding": 0, "problems": 0}
    assert {row["binding_id"]: row["identity_sha256"] for row in document["bindings"]} == {
        key: entry["identity_sha256"] for key, entry in entries.items()}
    assert reviewed("report").stdout == result.stdout

    renamed = deepcopy(catalog)
    renamed["workloads"][1]["id"] = "carried"
    write_catalog(renamed)
    result, document = report()
    assert result.returncode == 1
    assert document["bindings_without_policy"] == ["carried"]
    assert document["policy_records_without_binding"] == ["beta"]
    assert document["identity_mismatches"] == []
    reviewed("carry", "--move", "beta=carried")
    written = json.loads(record.read_text())["bindings"]
    assert written["carried"] == entries["beta"], "policy and reason did not move verbatim"
    assert "beta" not in written
    result, document = report()
    assert result.returncode == 0 and document["problems"] == []
    settled = record.read_bytes()
    assert "already applied" in reviewed("carry", "--move", "beta=carried").stdout
    assert record.read_bytes() == settled

    grown = deepcopy(renamed)
    grown["workloads"].append(scenario("extra", "alpha_extra", "extra"))
    write_catalog(grown)
    assert "ghost declares no test" in refuse("carry", "--move", "carried=ghost").stderr
    assert "no applicability policy is recorded for ghost" in refuse("carry", "--move", "ghost=extra").stderr
    assert "declares a different test" in refuse("carry", "--move", "alpha=extra").stderr
    refuse("carry", "--move", "alpha=extra", "--move", "carried=extra")
    refuse("carry", "--move", "alpha=extra", "--drop", "alpha")

    # Removing/reordering siblings cannot shift any remaining stable id.
    reordered = deepcopy(renamed)
    reordered["workloads"] = [reordered["workloads"][4], reordered["workloads"][1],
        reordered["workloads"][0], reordered["workloads"][3]]
    write_catalog(reordered)
    result, document = report()
    assert result.returncode == 1
    assert document["identity_mismatches"] == []
    assert document["bindings_without_policy"] == []
    assert document["policy_records_without_binding"] == ["first"]
    reviewed("carry", "--drop", "first")
    result, document = report()
    assert result.returncode == 0 and document["counts"]["reviewed_bindings"] == 4
    settled = record.read_bytes()
    reviewed("carry", "--drop", "first")
    assert record.read_bytes() == settled
    assert not list(workspace.rglob("CMakeCache.txt")), "review command configured a build"

print("PASS native-applicability-review-contract")

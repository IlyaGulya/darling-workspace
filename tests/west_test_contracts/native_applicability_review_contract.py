"""Applicability review commands re-record a fixture record without guessing keys."""
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

GROUPS = [
    {"policy": "native_reference",
     "reason": "Synthetic public scenario for the applicability review contract."},
    {"policy": "darling_internal_gate",
     "reason": "Synthetic Darling-internal scenario for the applicability review contract."},
]
POLICY = {
    "alpha_probe": "native_reference",
    "alpha_extra": "native_reference",
    "alpha_more": "darling_internal_gate",
    "beta_probe": "darling_internal_gate",
    "first_probe": "darling_internal_gate",
    "second_probe": "native_reference",
    "third_probe": "darling_internal_gate",
}


def declaration_digest(test):
    """The identity an applicability record stores, computed independently here."""
    declaration = {"project": "src/fixture", "test": test}
    return hashlib.sha256(json.dumps(
        declaration, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()


def reviews(profile_data):
    entries = {}
    for patch in normalize_test_profile(profile_data)["patches"]:
        for ordinal, test in enumerate(patch["tests"], 1):
            name = test.get("name")
            entries[f"probe:{patch['path']}:{ordinal}"] = {
                "name": name,
                "identity_sha256": declaration_digest(test),
                "policy": POLICY[name],
                "reason": f"Reviewed synthetic scenario for {name}.",
            }
    return entries


def scenario(name, argument):
    return {"name": name, "use": "shared", "run-args": [argument]}


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
    profile = workspace / "patches/probe"
    profile.mkdir(parents=True)
    # No testkit tree: these commands read declarations and review policy, so a
    # command that configured or built anything would fail rather than pass quietly.
    profile_data = {
        "test-profiles": {
            "shared": {"runner": "guest-c-fixture", "runs": "guest",
                       "repo": "fixture-owner", "script": "probe.c"}
        },
        "patches": [
            {"path": "alpha.patch", "module": "src/fixture", "bead": "alpha-owner",
             "tests": [scenario("alpha_probe", "alpha")]},
            {"path": "beta.patch", "module": "src/fixture", "bead": "beta-owner",
             "tests": [scenario("beta_probe", "beta")]},
            {"path": "shift.patch", "module": "src/fixture", "bead": "shift-owner",
             "tests": [scenario("first_probe", "first"),
                       scenario("second_probe", "second"),
                       scenario("third_probe", "third")]},
        ],
    }

    def write_manifest(data):
        # JSON is a YAML subset: exercise the real production normalizer.
        (profile / "patches.yml").write_text(json.dumps(data))

    def write_record(entries):
        record.write_text(json.dumps(
            {"qualification": "Synthetic reviewed applicability for the review contract.",
             "groups": GROUPS, "bindings": entries}, indent=2) + "\n")

    def run(*arguments):
        return subprocess.run(
            [sys.executable, "-B", str(COLLECTOR), *arguments, "--workspace", str(workspace)],
            cwd=workspace, capture_output=True, text=True, timeout=60,
        )

    def reviewed(command, *arguments):
        before = record.read_text()
        result = run(command, *arguments)
        assert result.returncode == 0, result.stdout + result.stderr
        if command == "report":
            assert record.read_text() == before, "report is read-only"
        return result

    def report():
        before = record.read_text()
        result = run("report")
        assert result.returncode in (0, 1), result.stdout + result.stderr
        assert record.read_text() == before, "report is read-only"
        return result, json.loads(result.stdout)

    def refuse(command, *arguments):
        before = record.read_text()
        result = run(command, *arguments)
        assert result.returncode != 0, \
            "a refused review must exit non-zero: " + result.stdout + result.stderr
        assert record.read_text() == before, "a refused review must not write the record"
        return result

    write_manifest(profile_data)
    entries = reviews(profile_data)
    write_record(entries)

    # Report: every binding carries the identity the record is keyed by.
    result, document = report()
    assert result.returncode == 0, result.stdout + result.stderr
    assert document["counts"] == {
        "bindings": 5, "policy_records": 5, "reviewed_bindings": 5,
        "bindings_without_policy": 0, "identity_mismatches": 0,
        "policy_records_without_binding": 0, "problems": 0,
    }, document["counts"]
    assert document["problems"] == []
    assert {row["binding_id"]: row["identity_sha256"] for row in document["bindings"]} == {
        key: entry["identity_sha256"] for key, entry in entries.items()
    }, "report must publish the declaration identity the reviewed record is keyed by"
    assert reviewed("report").stdout == result.stdout, "report is repeatable"

    # Carry: a patch entry renamed in the manifest keeps its declaration identity,
    # so its review moves verbatim to the new key instead of being re-derived.
    renamed = deepcopy(profile_data)
    renamed["patches"][1]["path"] = "carried.patch"
    write_manifest(renamed)
    result, document = report()
    assert result.returncode != 0, "a record that no longer matches the tree must not report clean"
    assert document["bindings_without_policy"] == ["probe:carried.patch:1"]
    assert document["policy_records_without_binding"] == ["probe:beta.patch:1"]
    assert document["identity_mismatches"] == []
    assert len(document["problems"]) == 2, document["problems"]
    verbatim = entries["probe:beta.patch:1"]
    output = reviewed("carry", "--move", "probe:beta.patch:1=probe:carried.patch:1")
    assert "carried probe:beta.patch:1 -> probe:carried.patch:1" in output.stdout, output.stdout
    written = json.loads(record.read_text())["bindings"]
    assert written["probe:carried.patch:1"] == verbatim, "policy and reason move verbatim"
    assert "probe:beta.patch:1" not in written, "the replaced record is dropped, not left behind"
    result, document = report()
    assert result.returncode == 0 and document["problems"] == [], document["problems"]
    settled = record.read_text()
    output = reviewed("carry", "--move", "probe:beta.patch:1=probe:carried.patch:1")
    assert "already applied" in output.stdout, output.stdout
    assert record.read_text() == settled, "carry is idempotent"

    # Carry refusals. A second scenario in one patch entry leaves one binding
    # unreviewed for the refusals that need a real binding with no policy.
    grown = deepcopy(renamed)
    grown["patches"][0]["tests"].append(scenario("alpha_extra", "alpha-extra"))
    write_manifest(grown)
    refused = refuse("carry", "--move", "probe:carried.patch:1=probe:ghost.patch:2")
    assert "probe:ghost.patch:2 declares no test in the current tree" in refused.stderr, refused.stderr
    refused = refuse("carry", "--move", "probe:ghost.patch:1=probe:alpha.patch:2")
    assert "no applicability policy is recorded for probe:ghost.patch:1" in refused.stderr, \
        "carry must refuse to invent a policy for a key that had none"
    refused = refuse("carry", "--move", "probe:alpha.patch:1=probe:alpha.patch:2")
    assert "probe:alpha.patch:2 declares a different test than the review recorded " \
           "at probe:alpha.patch:1" in refused.stderr, refused.stderr

    # Rekey: removing a sibling shifts the ordinals of the remaining declarations,
    # which keep their identity, so the same (name, identity) pairs are re-recorded
    # under their new keys.
    shifted = deepcopy(grown)
    del shifted["patches"][2]["tests"][0]
    write_manifest(shifted)
    result, document = report()
    assert result.returncode != 0
    assert document["identity_mismatches"] == ["probe:shift.patch:1", "probe:shift.patch:2"]
    assert document["policy_records_without_binding"] == ["probe:shift.patch:3"]

    # Rekey refusals, in the drift the shift created: the record being re-keyed
    # must describe the declaration that now holds the key, and a key that has no
    # review is not re-keyed from nothing.
    refused = refuse("rekey", "--shift", "probe:alpha.patch:1=probe:carried.patch:1")
    assert "pure index shift" in refused.stderr, refused.stderr
    refused = refuse("rekey", "--shift", "probe:shift.patch:1=probe:shift.patch:2")
    assert "probe:shift.patch:2 declares a different test than the review recorded " \
           "at probe:shift.patch:1" in refused.stderr, \
        "rekey must refuse an identity that does not match the record it re-keys"
    grown_more = deepcopy(shifted)
    grown_more["patches"][0]["tests"].append(scenario("alpha_more", "alpha-more"))
    write_manifest(grown_more)
    refused = refuse("rekey", "--shift", "probe:alpha.patch:2=probe:alpha.patch:3")
    assert "no applicability policy is recorded for probe:alpha.patch:2" in refused.stderr, \
        refused.stderr

    recorded = json.loads(record.read_text())["bindings"]
    second, third = recorded["probe:shift.patch:2"], recorded["probe:shift.patch:3"]
    output = reviewed("rekey", "--shift", "probe:shift.patch:2=probe:shift.patch:1",
                      "--shift", "probe:shift.patch:3=probe:shift.patch:2",
                      "--drop", "probe:shift.patch:1")
    assert "dropped probe:shift.patch:1" in output.stdout, output.stdout
    written = json.loads(record.read_text())["bindings"]
    assert written["probe:shift.patch:1"] == second, "a shifted binding keeps its review verbatim"
    assert written["probe:shift.patch:2"] == third
    assert [key for key in written if key.startswith("probe:shift")] == \
        ["probe:shift.patch:1", "probe:shift.patch:2"], written.keys()
    assert entries["probe:shift.patch:1"] not in written.values(), \
        "the removed sibling's review is dropped rather than orphaned"
    settled = record.read_text()
    output = reviewed("rekey", "--shift", "probe:shift.patch:2=probe:shift.patch:1",
                      "--shift", "probe:shift.patch:3=probe:shift.patch:2",
                      "--drop", "probe:shift.patch:1")
    assert "already applied" in output.stdout, output.stdout
    assert record.read_text() == settled, "rekey is idempotent"

    # The record the commands wrote reads back clean once the two bindings the
    # refusals deliberately left unreviewed are reviewed too.
    written = json.loads(record.read_text())
    expected = reviews(grown_more)
    missing = sorted(set(expected) - set(written["bindings"]))
    assert missing == ["probe:alpha.patch:2", "probe:alpha.patch:3"], missing
    assert set(written["bindings"]) - set(expected) == set(), "no review is left orphaned"
    write_record({**written["bindings"], **{key: expected[key] for key in missing}})
    result, document = report()
    assert result.returncode == 0, result.stdout + result.stderr
    assert document["problems"] == [], document["problems"]
    assert document["counts"]["bindings"] == 6, document["counts"]
    assert document["counts"]["reviewed_bindings"] == 6, document["counts"]
    assert not (workspace / "testkit").exists(), "review commands never configure a build"

print("PASS native-applicability-review-contract")

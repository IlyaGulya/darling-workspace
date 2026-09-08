#!/usr/bin/env python3
"""Read-only registration/applicability audit; no product build or test execution.
Generated evidence belongs in OUTPUT, not in source control.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path, help="external directory for generated evidence")
parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
workspace = args.workspace.resolve()
output = args.output.resolve()
sys.path.insert(0, str(workspace / "west_commands"))
from test_manifest import load_test_profile

project_paths = dict(line.split("\t", 1) for line in subprocess.check_output(
    ["west", "list", "-f", "{name}\t{path}"], cwd=workspace, text=True).splitlines())


def identity(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


profiles = []
patches = []
bindings = []
for manifest in sorted((workspace / "patches").glob("*/patches.yml")):
    data = load_test_profile(manifest)
    profile = manifest.parent.name
    profiles.append({"profile": profile, "manifest": str(manifest.relative_to(workspace)),
                     "sha256": identity(manifest), "base_profile": data.get("base-profile")})
    for patch in data.get("patches", []):
        owner = {"profile": profile, "patch": patch["path"], "module": patch["module"],
                 "bead": patch.get("bead"), "source_commit": patch.get("source-commit")}
        tests = patch.get("tests") or []
        patches.append({**owner, "test_count": len(tests),
                        "exception": patch.get("test-exception"),
                        "publication_status": patch.get("publication-status")})
        for ordinal, test in enumerate(tests, 1):
            repo = test.get("repo", patch["module"])
            repo_root = workspace if repo in {"darling-workspace", "manifest"} else workspace.parent / project_paths.get(repo, repo)
            script = test.get("script")
            asset = repo_root / script if script else None
            runner = test.get("runner") or ("ctest" if test.get("ctest-label") else "command" if test.get("command") else "script" if script else "unspecified")
            row = {**owner, "binding_id": f"{profile}:{patch['path']}:{ordinal}",
                   "name": test.get("name"), "runner": runner,
                   "environment": test.get("env", "host"),
                   "coverage_tier": test.get("coverage-tier"),
                   "blocked": test.get("blocked"), "red": bool(test.get("red")),
                   "red_proof": test.get("red-proof"),
                   "repo": repo, "script": script,
                   "asset_path": str(asset) if asset else None,
                   "asset_exists": asset.exists() if asset else None,
                   "asset_sha256": identity(asset) if asset else None,
                   "ctest_selector": test.get("ctest-label"),
                   "requirements": test.get("requires", []),
                   "normalized_test": test}
            bindings.append(row)

output.mkdir(parents=True, exist_ok=True)
configure = subprocess.run(
    ["cmake", "-S", str(workspace / "testkit"), "-B", str(output / "ctest-build"),
     "-DBUILD_TESTING=ON"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
(output / "configure.log").write_text(configure.stdout)
configure.check_returncode()
discovery = subprocess.run(
    ["ctest", "--test-dir", str(output / "ctest-build"), "--show-only=json-v1"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
(output / "discovery.log").write_text(discovery.stderr)
discovery.check_returncode()
ctest_path = output / "ctest.json"
ctest_path.write_text(discovery.stdout)
ctest = json.loads(discovery.stdout)
ctest_cases = []
if ctest:
    for test in ctest.get("tests", []):
        properties = {p["name"]: p["value"] for p in test.get("properties", [])}
        labels = properties.get("LABELS", [])
        command = test.get("command", [])
        source = None
        if "--source" in command:
            index = command.index("--source")
            if index + 1 < len(command):
                source = command[index + 1]
        ctest_cases.append({"name": test["name"], "labels": labels,
                            "command": command, "source": source,
                            "properties": properties})

policy_spec = json.loads((workspace / "audits/native-test-applicability.json").read_text())
policy_groups = {group["policy"]: group for group in policy_spec["groups"]}
if len(policy_groups) != len(policy_spec["groups"]):
    raise ValueError("duplicate applicability policy group")
reviewed_bindings = policy_spec["bindings"]

for row in bindings:
    matching = [case for case in ctest_cases if row["asset_path"] and case["source"]
                and Path(case["source"]).resolve() == Path(row["asset_path"]).resolve()]
    row["workspace_ctest_same_source"] = [case["name"] for case in matching]
    row["registration_surface"] = "source-ctest-runner" if row["runner"] != "ctest" and row["ctest_selector"] else "workspace-ctest-selector" if row["runner"] == "ctest" else "direct-metadata-runner"
    declaration = {"project": project_paths.get(row["repo"], row["repo"]),
                   "test": row["normalized_test"]}
    declaration_bytes = json.dumps(declaration, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode()
    row["identity_sha256"] = hashlib.sha256(declaration_bytes).hexdigest()
    review = reviewed_bindings.get(row["binding_id"])
    if review is None:
        raise ValueError(f"binding has no reviewed applicability policy: {row['binding_id']}")
    if review["identity_sha256"] != row["identity_sha256"]:
        raise ValueError(f"binding declaration changed since applicability review: {row['binding_id']}")
    policy = policy_groups[review["policy"]]
    row["native_policy"] = {"policy": policy["policy"], "reason": review["reason"]}
    row["audit_execution_status"] = "not_run"
    if row["asset_exists"] is False:
        row["asset_resolution"] = "unresolved_in_current_checkout"
        if row["source_commit"] and row["repo"] not in {"darling-workspace", "manifest"}:
            root = workspace.parent / project_paths.get(row["repo"], row["repo"])
            check = subprocess.run(["git", "-C", str(root), "cat-file", "-e",
                                    f"{row['source_commit']}:{row['script']}"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            row["declared_commit_asset"] = "present" if check.returncode == 0 else "unresolved"
            if check.returncode:
                row["declared_commit_asset_diagnostic"] = check.stderr.strip()

summary = {
    "profiles": len(profiles), "patch_entries": len(patches), "test_bindings": len(bindings),
    "patches_without_tests": [p for p in patches if not p["test_count"]],
    "environments": dict(collections.Counter(r["environment"] for r in bindings)),
    "runners": dict(collections.Counter(r["runner"] for r in bindings)),
    "coverage_tiers": dict(collections.Counter(r["coverage_tier"] for r in bindings)),
    "blocked_bindings": sum(bool(r["blocked"]) for r in bindings),
    "unresolved_script_assets_in_current_checkout": [{k: r[k] for k in ("binding_id", "name", "asset_path")} for r in bindings if r["asset_exists"] is False],
    "workspace_ctest_entries": len(ctest_cases),
    "native_policy": dict(collections.Counter(r["native_policy"]["policy"] for r in bindings)),
    "same_source_metadata_ctest_bindings": sum(bool(r["workspace_ctest_same_source"]) for r in bindings),
}
snapshot = {"scope": "All concrete patches/*/patches.yml entries normalized independently, not an expanded runtime stack. Shared fixtures remain separate patch bindings. CTest scope is the configured workspace testkit with default options; source-repo CTest builds and disabled conditional suites are not executed or exhaustively discovered. Missing assets and unresolved applicability are not silently excluded. No run verdict is inferred from metadata, labels or declarations.",
            "normalizer_sha256": identity(workspace / "west_commands/test_manifest.py"),
            "policy_sha256": identity(workspace / "audits/native-test-applicability.json"),
            "profiles": profiles, "patches": patches, "bindings": bindings,
            "workspace_ctest": ctest_cases, "summary": summary}
output.mkdir(parents=True, exist_ok=True)
(output / "inventory.json").write_text(json.dumps(snapshot, indent=2) + "\n")
print(json.dumps(summary, indent=2))

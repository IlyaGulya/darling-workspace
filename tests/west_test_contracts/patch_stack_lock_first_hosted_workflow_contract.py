#!/usr/bin/env python3
"""Keep the lock-first acceptance tier manual and semantically exact."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
import patch_stack_lock_first_acceptance as acceptance


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def must_fail(fn, *args) -> None:
    try:
        fn(*args)
    except acceptance.AcceptanceError:
        return
    raise AssertionError("lock-first compare accepted invalid evidence")


def synthetic_compare_contract() -> None:
    """Exercise the typed six-lock comparison without West or hosted state."""
    with tempfile.TemporaryDirectory(prefix="lock-first-compare-contract-") as temporary:
        root = Path(temporary)
        source = root / "source"
        git(root, "init", "-q", str(source))
        git(source, "config", "user.name", "Contract")
        git(source, "config", "user.email", "contract@example.invalid")
        (source / "base").write_text("base\n")
        git(source, "add", "base")
        git(source, "commit", "-qm", "base")
        base = git(source, "rev-parse", "HEAD")
        commits = []
        for index in range(6):
            (source / f"series-{index}").write_text(f"{index}\n")
            git(source, "add", f"series-{index}")
            git(source, "commit", "-qm", f"series {index}")
            commits.append(git(source, "rev-parse", "HEAD"))
        bare = root / "source.git"
        git(root, "clone", "--bare", "-q", str(source), str(bare))
        control_workspace, lock_workspace = root / "control", root / "lock-first"
        for workspace in (control_workspace, lock_workspace):
            workspace.mkdir()
            git(workspace, "clone", "-q", str(bare), "darling")
            git(workspace / "darling", "update-ref", "refs/heads/integration/homebrew", commits[-1])
        integration_tree = git(source, "rev-parse", "HEAD^{tree}")
        row = {"module": "darling", "west_name": "darling", "path": "darling", "integration_oid": commits[-1], "tree": integration_tree, "status": ""}
        modules = {"profile": "homebrew", "modules": [row]}
        manifest = {"frozen_manifest_sha256": "a" * 64, "generated_profile_lock": {"sha256": "b" * 64, "size": 1}}
        control_map, lock_map = root / "control-modules.json", root / "lock-first-modules.json"
        control_manifest, lock_manifest = root / "control-manifest.json", root / "lock-first-manifest.json"
        for path, value in ((control_map, modules), (lock_map, modules), (control_manifest, manifest), (lock_manifest, manifest)):
            path.write_text(json.dumps(value))
        locks = root / "locks"; locks.mkdir()
        mapping_entries, series = [], []
        for index, commit in enumerate(commits):
            patch = f"darling/series-{index}.patch"
            lock_name = f"series-{index}.yml"
            tree = git(source, "rev-parse", f"{commit}^{{tree}}")
            (locks / lock_name).write_text(json.dumps({
                "schema_version": 2,
                "project": {"name": "darling", "path": "."},
                "upstream": {"url": "https://example.invalid/upstream.git", "base_commit": base},
                "mirror": {
                    "url": "https://example.invalid/mirror.git",
                    "base_ref": f"refs/tags/patch-stack/v1/bases/{base}", "base_oid": base,
                    "source_ref": f"refs/tags/patch-stack/v1/sources/{commit}", "source_oid": commit,
                },
                "source_commit": commit, "ordered_commits": [commit], "expected_tree": tree,
            }))
            mapping_entries.append({"profile": "homebrew", "module": "darling", "patch": patch, "lock": lock_name})
            series.append({"patch": patch, "base": base, "source": commit, "canonical_tree": tree, "applied_commit": commit, "applied_tree": tree, "verdict": "VALID"})
        mapping = locks / "lock-first-series-v1.yml"
        mapping.write_text(json.dumps({"schema_version": 1, "series": mapping_entries}))
        evidence = root / "lock-first-evidence.json"
        evidence.write_text(json.dumps({"verdict": "VALID", "series": series}))
        result = root / "result.json"
        transaction_root = root / "transactions"; transaction_root.mkdir()
        args = (control_map, lock_map, control_manifest, lock_manifest, evidence, mapping, control_workspace, lock_workspace, transaction_root, result)
        acceptance.compare_lock_first(*args)
        assert json.loads(result.read_text())["verdict"] == "VALID"
        for mutate in (
            lambda value: value["series"].pop(),
            lambda value: value["series"].append(dict(value["series"][0])),
            lambda value: value["series"].__setitem__(1, dict(value["series"][0], patch=value["series"][1]["patch"])),
            lambda value: value.__setitem__("series", list(reversed(value["series"]))),
            lambda value: value["series"][0].__setitem__("canonical_tree", "0" * 40),
        ):
            candidate = json.loads(evidence.read_text())
            mutate(candidate)
            negative = root / f"negative-{len(list(root.glob('negative-*.json')))}.json"
            negative.write_text(json.dumps(candidate))
            negative_result = root / f"negative-result-{negative.stem}.json"
            must_fail(acceptance.compare_lock_first, control_map, lock_map, control_manifest, lock_manifest, negative, mapping, control_workspace, lock_workspace, transaction_root, negative_result)
            assert not negative_result.exists()
        # The mapping and every referenced schema-v2 lock are independently
        # fail-closed, including containment before any resolve() operation.
        original_mapping = mapping.read_text()
        original_lock = (locks / "series-0.yml").read_text()
        def mapping_failure(name, mutate):
            candidate = json.loads(original_mapping); mutate(candidate)
            mapping.write_text(json.dumps(candidate))
            result_path = root / f"{name}-result.json"
            try:
                must_fail(acceptance.compare_lock_first, *args[:-1], result_path)
                assert not result_path.exists()
            finally:
                mapping.write_text(original_mapping)
        mapping_failure("duplicate-lock", lambda value: value["series"][1].__setitem__("lock", value["series"][0]["lock"]))
        mapping_failure("wrong-profile", lambda value: value["series"][0].__setitem__("profile", "perf"))
        mapping_failure("wrong-module", lambda value: value["series"][0].__setitem__("module", "missing-module"))
        def lock_failure(name, mutate):
            mutate()
            result_path = root / f"{name}-result.json"
            try:
                must_fail(acceptance.compare_lock_first, *args[:-1], result_path)
                assert not result_path.exists()
            finally:
                (locks / "series-0.yml").unlink(missing_ok=True)
                (locks / "series-0.yml").write_text(original_lock)
        lock_failure("malformed-schema", lambda: (locks / "series-0.yml").write_text("schema_version: 2\n"))
        def mismatched_mirror():
            value = json.loads(original_lock); value["mirror"]["source_oid"] = base
            (locks / "series-0.yml").write_text(json.dumps(value))
        lock_failure("mirror-source-mismatch", mismatched_mirror)
        leaf_target = locks / "leaf-target.yml"; leaf_target.write_text(original_lock)
        (locks / "series-0.yml").unlink(); (locks / "series-0.yml").symlink_to(leaf_target.name)
        try:
            must_fail(acceptance.compare_lock_first, *args[:-1], root / "leaf-symlink-result.json")
        finally:
            (locks / "series-0.yml").unlink(); (locks / "series-0.yml").write_text(original_lock); leaf_target.unlink()
        intermediate = locks / "linked"; intermediate.symlink_to(locks, target_is_directory=True)
        mapping_failure("intermediate-symlink", lambda value: value["series"][0].__setitem__("lock", "linked/series-0.yml"))
        intermediate.unlink()
        existing_result = root / "existing-result.json"; existing_result.write_text("old\n")
        must_fail(acceptance.compare_lock_first, *args[:-1], existing_result)
        result_target = root / "result-target.json"; result_target.write_text("old\n")
        symlink_result = root / "symlink-result.json"; symlink_result.symlink_to(result_target.name)
        must_fail(acceptance.compare_lock_first, *args[:-1], symlink_result)
        git(lock_workspace / "darling", "update-ref", "refs/west/patch-stack-lock-first/test", commits[-1])
        must_fail(acceptance.compare_lock_first, *args[:-1], root / "transaction-ref-result.json")
        git(lock_workspace / "darling", "update-ref", "-d", "refs/west/patch-stack-lock-first/test")
        disposable = transaction_root / "west-patch-lock-first-contract-root"; disposable.mkdir()
        try:
            must_fail(acceptance.compare_lock_first, *args[:-1], root / "disposable-root-result.json")
        finally:
            disposable.rmdir()
workflow = (ROOT / ".github/workflows/patch-stack-lock-first.yml").read_text()
assert "on:\n  workflow_dispatch:" in workflow
assert "push:" not in workflow and "schedule:" not in workflow
assert "if: github.event_name == 'workflow_dispatch'" in workflow
assert "runs-on: ubuntu-latest" in workflow and "timeout-minutes: 25" in workflow
assert "jdx/mise-action@5228313ee0372e111a38da051671ca30fc5a96db" in workflow
assert "working_directory: darling-dev/darling-workspace" in workflow
assert "cache: false" in workflow and "cache_save: false" in workflow
assert "actions/cache" not in workflow and "MISE_CACHE_DIR" not in workflow
assert workflow.count("west patch apply --profile homebrew") == 2
assert "west patch apply --profile homebrew --lock-first" in workflow
assert "mise exec -- uv --version" in workflow
assert "mise exec -- west --version" in workflow
assert "mise_toml_sha256=" in workflow and "sha256sum mise.toml" in workflow
assert "python3 -m venv" not in workflow and "pip install west" not in workflow
assert "python3 -c 'import west'" not in workflow
assert "LOCK_FIRST_TOOLS" not in workflow
for line in workflow.splitlines():
    stripped = line.strip()
    assert not stripped.startswith("west "), line
assert '\n            HOME="$home" west ' not in workflow
assert 'HOME="$home" mise exec' not in workflow
verification = workflow.split("- name: Verify isolated West under empty homes", 1)[1].split("- name: Configure repository-local identity", 1)[0]
assert 'mise -C "$workspace" exec -- env HOME="$home" west --version' in verification
assert "mise exec --" not in verification, "verification may not launch outside project mise.toml"
assert 'mise -C "$workspace" exec -- env HOME="$home" ci/bootstrap-west.sh' in workflow
assert 'mise -C "$workspace" exec -- env HOME="$home" west list' in workflow
assert 'mise -C "$workspace" exec -- env HOME="$LOCK_FIRST_ROOT/control-home"' in workflow
assert 'mise -C "$workspace" exec -- env HOME="$LOCK_FIRST_ROOT/lock-first-home"' in workflow
assert "--lock-first-evidence \"$LOCK_FIRST_ROOT/evidence/lock-first-evidence.json\"" in workflow
assert "--shadow-lock" not in workflow
assert "patch_stack_lock_first_acceptance.py compare-lock-first" in workflow
assert "--mapping locks/patch-stack/lock-first-series-v1.yml" in workflow
assert "--lock-first-workspace \"$LOCK_FIRST_ROOT/lock-first/darling-workspace\"" in workflow
assert "--transaction-root \"$RUNNER_TEMP\"" in workflow
assert "patch_stack_shadow_acceptance.py compare" not in workflow
assert "git clone --no-local --no-hardlinks" in workflow and "fetch-depth: 0" in workflow
assert "cleanup_status=" in workflow and "if: always()" in workflow
assert "west-patch-shadow-* west-lock-materialize-* west-patch-lock-first-*" in workflow
assert "patch-stack-lock-first-acceptance" in workflow
compare_source = (ROOT / "ci/patch_stack_lock_first_acceptance.py").read_text()
assert "BATCH_SIZE = 6" in compare_source
assert "refs/west/patch-stack-lock-first/" in compare_source
assert "west-patch-lock-first-*" in compare_source
synthetic_compare_contract()
print("patch-stack lock-first hosted-workflow contract: PASS")

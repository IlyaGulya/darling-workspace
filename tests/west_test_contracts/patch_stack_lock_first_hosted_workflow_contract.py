#!/usr/bin/env python3
"""Manual immutable-oracle workflow and typed compare contract."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
import patch_stack_acceptance as capture
import patch_stack_lock_first_acceptance as acceptance


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def must_fail(callback, *args, contains: str | None = None) -> None:
    try:
        callback(*args)
    except acceptance.AcceptanceError as error:
        if contains is not None:
            assert contains in str(error), error
    else:
        raise AssertionError("hosted comparison accepted invalid evidence")


def full_checkout_contract() -> None:
    with tempfile.TemporaryDirectory(
        prefix="lock-first-full-checkout-contract-"
    ) as temp:
        root = Path(temp)
        source = root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "config", "user.name", "Checkout Contract")
        git(source, "config", "user.email", "checkout@example.invalid")
        (source / "fixture").write_text("one\n")
        git(source, "add", "fixture")
        git(source, "commit", "-qm", "one")
        (source / "fixture").write_text("two\n")
        git(source, "commit", "-qam", "two")
        shallow = root / "shallow"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                source.as_uri(),
                str(shallow),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            capture.assert_clean_odb(shallow)
        except capture.AcceptanceError as error:
            assert "shallow repository" in str(error), error
        else:
            raise AssertionError("strict capture accepted shallow checkout")
        complete = root / "complete"
        subprocess.run(
            ["git", "clone", "-q", source.as_uri(), str(complete)],
            check=True,
        )
        capture.assert_clean_odb(complete)


def composed_capture_contract() -> None:
    """A composed target publishes prerequisite-only modules and their layer."""
    with tempfile.TemporaryDirectory(
        prefix="composed-capture-contract-"
    ) as temp:
        workspace = Path(temp)
        locks = workspace / "locks" / "patch-stack"
        locks.mkdir(parents=True)
        (workspace / "patches" / "homebrew").mkdir(parents=True)
        (workspace / "patches" / "arch").mkdir(parents=True)
        (locks / "lock-first-profiles-v1.yml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "profiles": [
                        {"profile": "homebrew", "mapping": "homebrew.yml"},
                        {"profile": "arch", "mapping": "arch.yml"},
                    ],
                },
                sort_keys=False,
            )
        )
        for profile, prerequisite in (
            ("homebrew", None),
            ("arch", "homebrew"),
        ):
            (locks / f"{profile}.yml").write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 3,
                        "profile": profile,
                        "composition": f"{profile}-composition.yml",
                    },
                    sort_keys=False,
                )
            )
            (locks / f"{profile}-composition.yml").write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 3,
                        "profile": profile,
                        "prerequisites": (
                            [] if prerequisite is None else [{"profile": prerequisite}]
                        ),
                    },
                    sort_keys=False,
                )
            )
            profile_data: dict[str, object] = {
                "patches": [
                    {
                        "module": (
                            "darling/src/external/libplatform"
                            if profile == "homebrew"
                            else "darling/src/external/libunwind"
                        ),
                        "path": f"{profile}.patch",
                    }
                ]
            }
            if prerequisite is not None:
                profile_data["base-profile"] = prerequisite
            (workspace / "patches" / profile / "patches.yml").write_text(
                yaml.safe_dump(profile_data, sort_keys=False)
            )

        available = {
            module: {"name": module.replace("/", "-"), "path": workspace / module}
            for module in (
                "darling",
                "darling/src/external/libplatform",
                "darling/src/external/libunwind",
            )
        }
        assert capture.generated_lock_profiles(workspace, "arch") == [
            "homebrew",
            "arch",
        ]
        assert capture.composed_project_profiles(
            workspace, "arch", available
        ) == {
            "darling": "arch",
            "darling/src/external/libplatform": "homebrew",
            "darling/src/external/libunwind": "arch",
        }

        revisions = {
            "darling": "1" * 40,
            "darling/src/external/libplatform": "2" * 40,
            "darling/src/external/libunwind": "3" * 40,
        }
        generated = workspace / "patches" / "arch" / "west.lock.yml"
        generated.write_text(
            yaml.safe_dump(
                {
                    "manifest": {
                        "projects": [
                            {
                                "name": module.replace("/", "-"),
                                "path": module,
                                "revision": revision,
                            }
                            for module, revision in revisions.items()
                        ]
                    }
                },
                sort_keys=False,
            )
        )
        assert capture.generated_lock_revisions(generated) == revisions
        rows = [
            {
                "module": module,
                "integration_profile": (
                    "homebrew" if module.endswith("libplatform") else "arch"
                ),
                "integration_oid": revision,
            }
            for module, revision in revisions.items()
        ]
        capture.verify_generated_module_revisions(rows, revisions)
        mismatched = dict(revisions)
        mismatched["darling/src/external/libplatform"] = "4" * 40
        try:
            capture.verify_generated_module_revisions(rows, mismatched)
        except capture.AcceptanceError as error:
            assert "final generated lock revision differs" in str(error), error
        else:
            raise AssertionError(
                "capture accepted a prerequisite integration ref that "
                "differed from the final generated lock"
            )


def nested_layout_compare_contract() -> None:
    with tempfile.TemporaryDirectory(
        prefix="immutable-compare-contract-"
    ) as temp:
        root = Path(temp)
        # Hosted lock-first has separate West and manifest roots.  Captured
        # module paths are relative to the West topdir, while the manifest
        # repository (and its locks) is nested below it.
        west_topdir = root / "lock-first"
        manifest_workspace = west_topdir / "darling-workspace"
        repo = west_topdir / "darling"
        manifest_workspace.mkdir(parents=True)
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Compare Contract")
        git(repo, "config", "user.email", "compare@example.invalid")
        (repo / "fixture").write_text("base\n")
        git(repo, "add", "fixture")
        git(repo, "commit", "-qm", "base")
        base = git(repo, "rev-parse", "HEAD")
        base_tree = git(repo, "rev-parse", "HEAD^{tree}")
        commits: list[str] = []
        trees: list[str] = []
        for index in (1, 2):
            (repo / "fixture").write_text(f"series {index}\n")
            git(repo, "commit", "-qam", f"series {index}")
            commits.append(git(repo, "rev-parse", "HEAD"))
            trees.append(git(repo, "rev-parse", "HEAD^{tree}"))
        git(
            repo,
            "update-ref",
            "refs/heads/integration/homebrew",
            commits[-1],
        )

        locks = manifest_workspace / "locks"
        locks.mkdir()
        entries = []
        boundaries = [base, commits[0]]
        for index, (boundary, commit, tree) in enumerate(
            zip(boundaries, commits, trees, strict=True), 1
        ):
            lock_name = f"series-{index}.yml"
            (locks / lock_name).write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 2,
                        "project": {"name": "darling", "path": "."},
                        "upstream": {
                            "url": "https://example.invalid/upstream.git",
                            "base_commit": boundary,
                        },
                        "mirror": {
                            "url": "https://example.invalid/immutable.git",
                            "base_ref": (
                                "refs/tags/patch-stack/v1/bases/" + boundary
                            ),
                            "base_oid": boundary,
                            "source_ref": (
                                "refs/tags/patch-stack/v1/sources/" + commit
                            ),
                            "source_oid": commit,
                        },
                        "source_commit": commit,
                        "ordered_commits": [commit],
                        "expected_tree": tree,
                    },
                    sort_keys=False,
                )
            )
            entries.append(
                {
                    "profile": "homebrew",
                    "module": "darling",
                    "patch": f"darling/series-{index}.patch",
                    "lock": lock_name,
                }
            )
        mapping = locks / "fixture-series-v2.yml"
        mapping_value = {
            "schema_version": 3,
            "profile": "homebrew",
            "batch_id": "immutable-compare-fixture",
            "expected_count": 2,
            "composition": "fixture-composition-v2.yml",
            "series": entries,
        }
        mapping.write_text(yaml.safe_dump(mapping_value, sort_keys=False))
        frozen = manifest_workspace / "west.lock.yml"
        frozen.write_text("manifest:\n  projects: []\n")
        composition = {
            "schema_version": 3,
            "profile": "homebrew",
            "prerequisites": [],
            "frozen_manifest": {
                "path": "west.lock.yml",
                "sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
            },
            "mapping": {
                "path": mapping.name,
                "sha256": hashlib.sha256(mapping.read_bytes()).hexdigest(),
                "batch_id": mapping_value["batch_id"],
                "expected_count": 2,
            },
            "modules": [
                {
                    "module": "darling",
                    "starting": {"tree": base_tree},
                    "series": [
                        {
                            "patch": entry["patch"],
                            "lock": entry["lock"],
                            "expected_applied_tree": trees[index],
                        }
                        for index, entry in enumerate(entries)
                    ],
                    "final_tree": trees[-1],
                    "integration_final_tree": trees[-1],
                }
            ],
        }
        (locks / "fixture-composition-v2.yml").write_text(
            yaml.safe_dump(composition, sort_keys=False)
        )
        git(manifest_workspace, "init", "-q")
        git(manifest_workspace, "config", "user.name", "Compare Workspace")
        git(manifest_workspace, "config", "user.email", "compare-workspace@example.invalid")
        git(manifest_workspace, "add", "locks", "west.lock.yml")
        git(manifest_workspace, "commit", "-qm", "trusted compare workspace")
        workspace_commit = git(manifest_workspace, "rev-parse", "HEAD")
        batch = acceptance.load_batch(mapping, {"darling"})
        row = {
            "module": "darling",
            "west_name": "darling",
            "path": "darling",
            "integration_profile": "homebrew",
            "integration_oid": commits[-1],
            "tree": trees[-1],
            "status": "",
        }
        module_map = root / "lock-first-modules.json"
        module_map.write_text(
            json.dumps({"profile": "homebrew", "modules": [row]})
        )
        generated = {
            "profile": "homebrew",
            "path": "patches/homebrew/west.lock.yml",
            "size": 17,
            "sha256": "b" * 64,
        }
        manifest_value = {
            "workspace_commit": workspace_commit,
            "frozen_manifest_sha256": "a" * 64,
            "generated_profile_locks": [generated],
            "validated_nested_children": {},
        }
        manifest = root / "lock-first-manifest.json"
        manifest.write_text(json.dumps(manifest_value))
        series = [
            {
                "module": "darling",
                "patch": entry["patch"],
                "base": boundaries[index],
                "source": commits[index],
                "canonical_tree": trees[index],
                "applied_commit": commits[index],
                "applied_tree": trees[index],
                "verdict": "VALID",
            }
            for index, entry in enumerate(entries)
        ]
        evidence_value = {
            "evidence_schema_version": 2,
            "verdict": "VALID",
            "batch_id": mapping_value["batch_id"],
            "expected_count": 2,
            "module_order": ["darling"],
            "series_order": [
                {"module": entry["module"], "patch": entry["patch"]}
                for entry in entries
            ],
            "series": series,
            "profile_composition": batch["profile_composition"],
        }
        evidence = root / "lock-first-evidence.json"
        evidence.write_text(json.dumps(evidence_value))
        oracle_value = {
            "oracle_schema_version": 2,
            "mode": "immutable-cherry-pick-oracle",
            "profile": "homebrew",
            "profile_order": ["homebrew"],
            "batches": [
                {
                    "profile": "homebrew",
                    "batch_id": mapping_value["batch_id"],
                    "expected_count": 2,
                    "module_order": ["darling"],
                    "series_order": evidence_value["series_order"],
                    "series": series,
                    "verdict": "VALID",
                }
            ],
            "modules": [
                {
                    "module": "darling",
                    "commit": commits[-1],
                    "tree": trees[-1],
                }
            ],
            "generated_profile_locks": [generated],
            "frozen_manifest_sha256": "a" * 64,
            "clean_odb": {
                "module_count": 1,
                "immutable_fetch_transactions": 1,
                "alternates": 0,
                "shallow": 0,
                "partial": 0,
            },
            "cleanup": {
                "root": "removed",
                "worktrees": "removed",
                "refs": "removed",
            },
            "verdict": "VALID",
        }
        oracle_path = root / "immutable-oracle.json"
        oracle_path.write_text(json.dumps(oracle_value))
        transactions = root / "transactions"
        transactions.mkdir()

        def compare(
            oracle_file: Path,
            evidence_file: Path,
            result: Path,
            *,
            candidate_root: Path = west_topdir,
        ) -> None:
            acceptance.compare_immutable_oracle(
                oracle_file,
                module_map,
                manifest,
                evidence_file,
                mapping,
                candidate_root,
                transactions,
                result,
                manifest_workspace=manifest_workspace,
            )

        result = root / "result.json"
        compare(oracle_path, evidence, result)
        payload = json.loads(result.read_text())
        assert payload["verdict"] == "VALID"
        assert payload["control_mode"] == "immutable-cherry-pick-oracle"
        assert payload["candidate_mode"] == "default-lock-first"
        assert west_topdir != manifest_workspace
        assert (manifest_workspace / ".git").exists()
        assert repo.relative_to(west_topdir) == Path("darling")
        assert not (west_topdir / ".git").exists()

        # The nested manifest repository is not the West candidate root.  A
        # verifier accidentally resolving module paths from it must fail
        # closed instead of accepting the synthetic layout.
        try:
            compare(
                oracle_path,
                evidence,
                root / "wrong-candidate-result.json",
                candidate_root=manifest_workspace,
            )
        except acceptance.AcceptanceError as error:
            assert "workspace repository missing" in str(error), error
        else:
            raise AssertionError("manifest root was accepted as West candidate root")

        # The production verifier, not this contract, must bind the captured
        # manifest to the candidate workspace HEAD.
        original_manifest = manifest.read_bytes()
        forged_manifest = json.loads(original_manifest)
        forged_manifest["workspace_commit"] = "0" * 40
        manifest.write_text(json.dumps(forged_manifest))
        try:
            compare(oracle_path, evidence, root / "forged-manifest-result.json")
        except acceptance.AcceptanceError as error:
            assert "candidate HEAD" in str(error), error
        else:
            raise AssertionError("production compare accepted a forged workspace_commit")
        finally:
            manifest.write_bytes(original_manifest)

        for name, mutation, message in (
            (
                "wrong-schema",
                lambda value: value.__setitem__("oracle_schema_version", 1),
                "schema version",
            ),
            (
                "wrong-mode",
                lambda value: value.__setitem__("mode", "legacy"),
                "control mode",
            ),
            (
                "wrong-tree",
                lambda value: value["modules"][0].__setitem__(
                    "tree", "0" * 40
                ),
                "module trees",
            ),
            (
                "wrong-order",
                lambda value: value["batches"][0].__setitem__(
                    "series_order",
                    list(reversed(value["batches"][0]["series_order"])),
                ),
                "series order",
            ),
            (
                "unclean-odb",
                lambda value: value["clean_odb"].__setitem__("alternates", 1),
                "clean-ODB",
            ),
        ):
            bad = copy.deepcopy(oracle_value)
            mutation(bad)
            path = root / f"{name}.json"
            path.write_text(json.dumps(bad))
            output = root / f"{name}-result.json"
            must_fail(compare, path, evidence, output, contains=message)
            assert not output.exists()

        bad_evidence = copy.deepcopy(evidence_value)
        bad_evidence["series"] = list(reversed(bad_evidence["series"]))
        bad_evidence_path = root / "bad-evidence.json"
        bad_evidence_path.write_text(json.dumps(bad_evidence))
        must_fail(
            compare,
            oracle_path,
            bad_evidence_path,
            root / "bad-evidence-result.json",
        )

        existing = root / "existing.json"
        existing.write_text("old\n")
        must_fail(compare, oracle_path, evidence, existing, contains="already")
        linked_target = root / "linked-target"
        linked_target.write_text("old\n")
        linked = root / "linked-result"
        linked.symlink_to(linked_target)
        must_fail(compare, oracle_path, evidence, linked, contains="already")

        git(
            repo,
            "update-ref",
            "refs/west/patch-stack-lock-first/leftover",
            commits[-1],
        )
        must_fail(
            compare,
            oracle_path,
            evidence,
            root / "ref-leftover-result.json",
            contains="transaction refs",
        )
        git(
            repo,
            "update-ref",
            "-d",
            "refs/west/patch-stack-lock-first/leftover",
        )
        disposable = transactions / "west-patch-lock-first-leftover"
        disposable.mkdir()
        try:
            must_fail(
                compare,
                oracle_path,
                evidence,
                root / "root-leftover-result.json",
                contains="disposable roots",
            )
        finally:
            disposable.rmdir()


workflow_path = ROOT / ".github/workflows/patch-stack-lock-first.yml"
workflow = workflow_path.read_text()
assert "on:\n  workflow_dispatch:" in workflow
assert "\n  push:" not in workflow and "\n  schedule:" not in workflow
assert "if: github.event_name == 'workflow_dispatch'" in workflow
assert "runs-on: ubuntu-latest" in workflow
assert "timeout-minutes: 75" in workflow
assert "fetch-depth: 0" in workflow
assert "tests/patch_stack_immutable_oracle.py" in workflow
assert "Independent immutable clean-ODB oracle" in workflow
assert "Candidate default-lock-first materialization" in workflow
assert workflow.count("west patch apply --profile homebrew") == 1
assert "compare-immutable-oracle" in workflow
assert "--oracle \"$LOCK_FIRST_ROOT/evidence/immutable-oracle.json\"" in workflow
assert "--candidate-workspace \"$LOCK_FIRST_ROOT/lock-first\"" in workflow
assert "--candidate-workspace \"$LOCK_FIRST_ROOT/lock-first/darling-workspace\"" not in workflow
assert "--manifest-workspace \"$LOCK_FIRST_ROOT/lock-first/darling-workspace\"" in workflow
assert "patch_stack_acceptance.py capture" in workflow
assert "patch_stack_acceptance.py stage" in workflow
assert "if: always()" in workflow
assert "west-lock-materialize-* west-patch-lock-first-*" in workflow
for forbidden in (
    "--legacy-mbox",
    "--shadow-lock",
    "patch_stack_legacy_oracle",
    "patch_stack_shadow",
):
    assert forbidden not in workflow
assert {
    "immutable-oracle.json",
    "lock-first-manifest.json",
    "lock-first-modules.json",
    "lock-first-evidence.json",
    "acceptance-result.json",
    "cleanup.txt",
    "diagnostics.txt",
    "capture-diagnostics.json",
} == capture.ARTIFACT_ALLOWLIST

compare_source = (
    ROOT / "ci/patch_stack_lock_first_acceptance.py"
).read_text()
capture_source = (ROOT / "ci/patch_stack_acceptance.py").read_text()
assert "compare_immutable_oracle" in compare_source
assert "compare_lock_first" not in compare_source
assert "rev-parse\", \"--is-shallow-repository\"" in capture_source
assert "shallow repository" in capture_source
full_checkout_contract()
composed_capture_contract()
nested_layout_compare_contract()
print("patch-stack lock-first hosted-workflow contract: PASS")

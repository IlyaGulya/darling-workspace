#!/usr/bin/env python3
"""Behavioral contract for immutable acceptance-candidate cache hydration."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
import patch_stack_lock_first_acceptance as acceptance


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def configure(repository: Path) -> None:
    git(repository, "config", "user.name", "Candidate Cache Contract")
    git(repository, "config", "user.email", "candidate-cache@example.invalid")


def clone(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "-q", str(source), str(destination)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="acceptance-candidate-cache-") as raw:
        root = Path(raw)
        source_module = root / "source-module"
        source_module.mkdir()
        git(source_module, "init", "-q")
        configure(source_module)
        (source_module / "fixture.txt").write_text("base\n")
        git(source_module, "add", "fixture.txt")
        git(source_module, "commit", "-qm", "base")
        base = git(source_module, "rev-parse", "HEAD")

        candidate_root = root / "candidate"
        manifest_workspace = candidate_root / "darling-workspace"
        manifest_workspace.mkdir(parents=True)
        git(manifest_workspace, "init", "-q")
        configure(manifest_workspace)
        (manifest_workspace / "west.lock.yml").write_text(
            yaml.safe_dump(
                {
                    "manifest": {
                        "projects": [
                            {
                                "name": "fixture-module",
                                "path": "fixture/module",
                                "revision": base,
                            }
                        ]
                    }
                },
                sort_keys=False,
            )
        )
        git(manifest_workspace, "add", "west.lock.yml")
        git(manifest_workspace, "commit", "-qm", "manifest base")
        manifest_head = git(manifest_workspace, "rev-parse", "HEAD")

        candidate_module = candidate_root / "fixture/module"
        clone(source_module, candidate_module)
        configure(candidate_module)
        (candidate_module / "fixture.txt").write_text("candidate\n")
        git(candidate_module, "add", "fixture.txt")
        git(candidate_module, "commit", "-qm", "candidate")
        commit = git(candidate_module, "rev-parse", "HEAD")
        tree = git(candidate_module, "rev-parse", "HEAD^{tree}")
        git(candidate_module, "branch", "integration/homebrew", commit)

        generated_path = manifest_workspace / "patches/homebrew/west.lock.yml"
        generated_data = b"manifest:\n  projects: []\n"
        generated_path.parent.mkdir(parents=True)
        generated_path.write_bytes(generated_data)
        generated_row = {
            "profile": "homebrew",
            "path": "patches/homebrew/west.lock.yml",
            "size": len(generated_data),
            "sha256": hashlib.sha256(generated_data).hexdigest(),
            "semantic_sha256": "a" * 64,
        }
        evidence_path = root / "candidate-evidence.json"
        evidence_data = b'{"verdict":"VALID"}\n'
        evidence_path.write_bytes(evidence_data)
        modules_path = root / "modules.json"
        write_json(
            modules_path,
            {
                "profile": "homebrew",
                "modules": [
                    {
                        "module": "fixture/module",
                        "path": "fixture/module",
                        "integration_oid": commit,
                        "tree": tree,
                    }
                ],
            },
        )
        candidate_manifest_path = root / "candidate-manifest.json"
        write_json(
            candidate_manifest_path,
            {
                "workspace_commit": manifest_head,
                "frozen_manifest_sha256": "b" * 64,
                "generated_profile_locks": [generated_row],
                "validated_nested_children": {},
            },
        )

        cache_parent = root / "cache"
        cache_parent.mkdir(mode=0o700)
        key = "c" * 64
        cache = cache_parent / f"candidate-{key}"
        acceptance.publish_candidate_cache(
            manifest_workspace,
            candidate_root,
            "homebrew",
            cache,
            key,
            evidence_path,
            modules_path,
            candidate_manifest_path,
        )
        assert cache.is_dir() and (cache / "index.json").is_file()
        acceptance.publish_candidate_cache(
            manifest_workspace,
            candidate_root,
            "homebrew",
            cache,
            key,
            evidence_path,
            modules_path,
            candidate_manifest_path,
        )

        hydrated_root = root / "hydrated"
        hydrated_manifest = hydrated_root / "darling-workspace"
        clone(manifest_workspace, hydrated_manifest)
        hydrated_module = hydrated_root / "fixture/module"
        clone(source_module, hydrated_module)
        hydrated_evidence = root / "hydrated-evidence.json"
        acceptance.hydrate_candidate_cache(
            hydrated_manifest,
            hydrated_root,
            "homebrew",
            cache,
            key,
            hydrated_evidence,
            [str(root / "must-not-run-west")],
        )
        assert git(hydrated_module, "rev-parse", "HEAD") == commit
        assert git(hydrated_module, "symbolic-ref", "--short", "HEAD") == (
            "integration/homebrew"
        )
        assert (hydrated_manifest / generated_row["path"]).read_bytes() == generated_data
        assert hydrated_evidence.read_bytes() == evidence_data

        index = json.loads((cache / "index.json").read_text())
        bundle = cache / index["modules"][0]["cache_path"]
        bundle.write_bytes(bundle.read_bytes() + b"corrupt")
        rejected_root = root / "rejected"
        rejected_manifest = rejected_root / "darling-workspace"
        clone(manifest_workspace, rejected_manifest)
        clone(source_module, rejected_root / "fixture/module")
        try:
            acceptance.hydrate_candidate_cache(
                rejected_manifest,
                rejected_root,
                "homebrew",
                cache,
                key,
                root / "rejected-evidence.json",
                [str(root / "must-not-run-west")],
            )
        except acceptance.AcceptanceError as error:
            assert "content differs" in str(error)
        else:
            raise AssertionError("corrupt candidate cache was accepted")

    print("acceptance candidate cache contract: PASS")


if __name__ == "__main__":
    main()

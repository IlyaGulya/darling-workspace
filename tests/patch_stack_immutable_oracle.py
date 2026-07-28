#!/usr/bin/env python3
"""Independent clean-ODB oracle for canonical patch-stack acceptance.

The oracle deliberately does not call the production batch materializer.  It
loads the same typed schema-v2 locks, fetches only their declared immutable
refs into newly initialized repositories, validates the object topology, and
replays each ordered commit with plain ``git cherry-pick``.  Historical patch
archives are never opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_stack_lock_first
import patch_stack_materialize
import patch_stack_profile_composition


class OracleError(RuntimeError):
    pass


IDENTITY = (
    "-c",
    "user.name=West Test",
    "-c",
    "user.email=west-test@example.invalid",
)


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise OracleError(message)


def git(
    repo: Path,
    *args: str,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    if result.returncode:
        raise OracleError(
            f"{repo}: git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip() if capture else ""


def assert_clean_odb(repo: Path) -> None:
    fail(git(repo, "rev-parse", "--is-shallow-repository") == "false", f"{repo}: shallow repository")
    fail(not git_optional(repo, "config", "--get", "extensions.partialClone"), f"{repo}: partial clone")
    alternates = Path(git(repo, "rev-parse", "--git-path", "objects/info/alternates"))
    if not alternates.is_absolute():
        alternates = repo / alternates
    fail(not alternates.exists(), f"{repo}: alternates are forbidden")
    replace_refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/replace/")
    fail(not replace_refs, f"{repo}: replace refs are forbidden")


def git_optional(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 1:
        return ""
    if result.returncode:
        raise OracleError(
            f"{repo}: git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def load_profile(workspace: Path, profile: str) -> dict[str, Any]:
    path = workspace / "patches" / profile / "patches.yml"
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise OracleError(f"{profile}: invalid profile metadata: {error}") from error
    fail(
        isinstance(value, dict)
        and isinstance(value.get("patches"), list)
        and isinstance(value.get("integration-date"), str),
        f"{profile}: invalid profile metadata",
    )
    return value


def profile_stack(workspace: Path, profile: str) -> list[str]:
    reverse: list[str] = []
    current: str | None = profile
    while current is not None:
        fail(current not in reverse, "profile dependency cycle")
        reverse.append(current)
        value = load_profile(workspace, current)
        base = value.get("base-profile")
        fail(base is None or isinstance(base, str), f"{current}: invalid base-profile")
        current = base
    return list(reversed(reverse))


def typed_plan(
    workspace: Path,
    profile: str,
    mapping_override: Path | None,
) -> tuple[dict[str, Any], patch_stack_lock_first.LockFirstPlan]:
    profile_data = load_profile(workspace, profile)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for patch in profile_data["patches"]:
        fail(
            isinstance(patch, dict)
            and isinstance(patch.get("module"), str)
            and isinstance(patch.get("path"), str),
            f"{profile}: malformed patch entry",
        )
        grouped.setdefault(patch["module"], []).append(patch)
    mapping = mapping_override
    if mapping is None:
        mapping = patch_stack_lock_first.mapping_for_profile(profile)
    try:
        plan = patch_stack_lock_first.plan(
            profile, profile_data["patches"], mapping, grouped
        )
    except patch_stack_lock_first.LockFirstError as error:
        raise OracleError(str(error)) from error
    fail(plan.composition is not None, f"{profile}: typed composition is required")
    return profile_data, plan


def load_lock(entry: dict[str, str]) -> dict[str, Any]:
    try:
        lock = patch_stack_materialize.load_lock(Path(entry["lock_path"]))
    except (OSError, ValueError, patch_stack_materialize.MaterializeError) as error:
        raise OracleError(f"{entry['patch']}: invalid immutable lock: {error}") from error
    fail(lock.get("schema_version") == 2, f"{entry['patch']}: lock is not schema-v2")
    return lock


def validate_lock(
    repo: Path,
    entry: dict[str, str],
    lock: dict[str, Any],
    base_ref: str,
    source_ref: str,
) -> dict[str, Any]:
    base = git(repo, "rev-parse", f"{base_ref}^{{commit}}")
    source = git(repo, "rev-parse", f"{source_ref}^{{commit}}")
    fail(base == lock["mirror"]["base_oid"], f"{entry['patch']}: base ref moved")
    fail(source == lock["mirror"]["source_oid"], f"{entry['patch']}: source ref moved")
    fail(base == lock["upstream"]["base_commit"], f"{entry['patch']}: base mismatch")
    fail(source == lock["source_commit"], f"{entry['patch']}: source mismatch")
    ordered = git(repo, "rev-list", "--reverse", f"{base}..{source}").splitlines()
    fail(ordered == lock["ordered_commits"], f"{entry['patch']}: ordered commits differ")
    previous = base
    metadata: list[dict[str, str]] = []
    for ordinal, commit in enumerate(ordered, 1):
        parents = git(repo, "show", "-s", "--format=%P", commit).split()
        fail(parents == [previous], f"{entry['patch']}: commit {ordinal} is nonlinear or a merge")
        fields = git(
            repo,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%s",
            commit,
        ).split("\0")
        fail(len(fields) == 7 and all(fields), f"{entry['patch']}: commit {ordinal} metadata is incomplete")
        metadata.append(
            {
                "oid": commit,
                "author_name": fields[0],
                "author_email": fields[1],
                "author_date": fields[2],
                "committer_name": fields[3],
                "committer_email": fields[4],
                "committer_date": fields[5],
                "subject": fields[6],
            }
        )
        previous = commit
    tree = git(repo, "rev-parse", f"{source}^{{tree}}")
    fail(tree == lock["expected_tree"], f"{entry['patch']}: source tree differs")
    return {
        "base": base,
        "source": source,
        "ordered_commits": ordered,
        "metadata": metadata,
        "expected_tree": tree,
    }


def replay_commit(repo: Path, commit: str) -> None:
    author_date = git(repo, "show", "-s", "--format=%aI", commit)
    env = os.environ.copy()
    env["GIT_COMMITTER_DATE"] = author_date
    result = subprocess.run(
        ["git", *IDENTITY, "cherry-pick", "--no-gpg-sign", commit],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if result.returncode:
        subprocess.run(
            ["git", "cherry-pick", "--abort"],
            cwd=repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        raise OracleError(
            f"{repo}: immutable cherry-pick {commit} failed "
            f"({result.returncode}): {result.stderr.strip()}"
        )


def stable_patch_ids(repo: Path, start: str, end: str) -> list[str]:
    identities: list[str] = []
    for commit in git(repo, "rev-list", "--reverse", f"{start}..{end}").splitlines():
        shown = subprocess.run(
            ["git", "show", "--pretty=format:%H", "--full-index", "--patch", commit],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if shown.returncode:
            raise OracleError(
                f"{repo}: git show {commit} failed ({shown.returncode}): "
                f"{shown.stderr.decode().strip()}"
            )
        identified = subprocess.run(
            ["git", "patch-id", "--stable"],
            cwd=repo,
            input=shown.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if identified.returncode:
            raise OracleError(
                f"{repo}: git patch-id failed ({identified.returncode}): "
                f"{identified.stderr.decode().strip()}"
            )
        rows = [line.decode().split() for line in identified.stdout.splitlines() if line.strip()]
        fail(
            len(rows) == 1 and len(rows[0]) == 2,
            f"{repo}: commit {commit} has no unique stable patch identity",
        )
        identities.append(rows[0][0])
    return identities


def assert_exact_replay(
    repo: Path,
    proof: dict[str, Any],
    before: str,
    after: str,
) -> None:
    """Independently prove that a composed replay retained exact patch identity."""
    applied = git(repo, "rev-list", "--reverse", f"{before}..{after}").splitlines()
    fail(
        len(applied) == len(proof["ordered_commits"]),
        f"{repo}: composed replay commit count differs",
    )
    previous = before
    for commit in applied:
        fail(
            git(repo, "show", "-s", "--format=%P", commit).split() == [previous],
            f"{repo}: composed replay is nonlinear",
        )
        previous = commit
    fail(
        stable_patch_ids(repo, proof["base"], proof["source"])
        == stable_patch_ids(repo, before, after),
        f"{repo}: composed replay stable patch identity differs",
    )


def verify_parent_integration(
    repo: Path,
    content_tree: str,
    integration_tree: str,
    targets: dict[str, Path],
) -> None:
    """Allow only exact managed gitlink publication around parent content.

    Parent gitlink commit IDs are lifecycle outputs and can differ when an
    independent oracle uses a different, declared commit-construction
    mechanism.  Their child trees and the parent's non-gitlink content remain
    exact.
    """
    result = subprocess.run(
        ["git", "diff-tree", "--raw", "-r", content_tree, integration_tree],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise OracleError(
            f"{repo}: cannot compare parent integration trees "
            f"({result.returncode}): {result.stderr.strip()}"
        )
    expected_paths = {
        str(Path(module).relative_to("darling")): target
        for module, target in targets.items()
        if module.startswith("darling/")
    }
    observed: set[str] = set()
    for line in filter(None, result.stdout.splitlines()):
        fields = line.split("\t", 1)
        fail(len(fields) == 2, f"{repo}: malformed parent integration diff")
        meta, path = fields
        tokens = meta.split()
        fail(len(tokens) >= 4, f"{repo}: malformed parent integration metadata")
        modes = [tokens[0].lstrip(":"), tokens[1]]
        fail(
            path in expected_paths and modes == ["160000", "160000"],
            f"{repo}: parent integration changed non-managed content {path}",
        )
        actual_gitlink = git(repo, "ls-tree", integration_tree, "--", path).split()
        fail(
            len(actual_gitlink) >= 3
            and actual_gitlink[0] == "160000"
            and actual_gitlink[1] == "commit",
            f"{repo}: parent integration gitlink is invalid: {path}",
        )
        fail(
            actual_gitlink[2] == git(expected_paths[path], "rev-parse", "HEAD"),
            f"{repo}: parent integration gitlink differs from managed child: {path}",
        )
        observed.add(path)
    fail(
        observed == set(expected_paths),
        f"{repo}: parent integration did not publish the exact managed child set",
    )


def generated_lock(
    frozen: dict[str, Any],
    phase: str,
    phase_modules: list[str],
    targets: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    value = json.loads(json.dumps(frozen))
    revisions = {"darling": git(targets["darling"], "rev-parse", "HEAD")}
    revisions.update(
        {
            module: git(targets[module], "rev-parse", "HEAD")
            for module in phase_modules
        }
    )
    for project in value["manifest"]["projects"]:
        path = project.get("path", project.get("name"))
        if path in revisions:
            project["revision"] = revisions[path]
    payload = yaml.safe_dump(value, sort_keys=False, width=1000).encode()
    return value, {
        "profile": phase,
        "path": f"patches/{phase}/west.lock.yml",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def apply(
    workspace: Path,
    profile: str,
    mapping_path: Path | None,
    output: Path,
) -> None:
    """Publish one immutable-oracle result only after clean teardown."""
    fail(not output.exists() and not output.is_symlink(), "oracle output already exists")
    stack = profile_stack(workspace, profile)
    plans: list[tuple[str, dict[str, Any], patch_stack_lock_first.LockFirstPlan]] = []
    for phase in stack:
        override = mapping_path if phase == profile else None
        profile_data, plan = typed_plan(workspace, phase, override)
        plans.append((phase, profile_data, plan))
    frozen_path = workspace / "west.lock.yml"
    fail(frozen_path.is_file() and not frozen_path.is_symlink(), "frozen manifest is unavailable")
    try:
        frozen = yaml.safe_load(frozen_path.read_text())
    except yaml.YAMLError as error:
        raise OracleError(f"invalid frozen manifest: {error}") from error
    fail(
        isinstance(frozen, dict)
        and isinstance(frozen.get("manifest"), dict)
        and isinstance(frozen["manifest"].get("projects"), list),
        "frozen manifest has no project list",
    )

    entries_by_module: OrderedDict[str, list[tuple[str, dict[str, str], dict[str, Any]]]] = OrderedDict()
    for phase, _profile_data, plan in plans:
        for entry in plan:
            entries_by_module.setdefault(entry["module"], []).append(
                (phase, entry, load_lock(entry))
            )
    entries_by_module.setdefault("darling", [])

    root = Path(tempfile.mkdtemp(prefix="west-immutable-oracle-"))
    payload: dict[str, Any] | None = None
    targets: dict[str, Path] = {}
    primary_error: BaseException | None = None
    try:
        proofs: dict[tuple[str, str], dict[str, Any]] = {}
        fetch_transactions = 0
        for module in sorted(entries_by_module, key=lambda item: (len(Path(item).parts), item)):
            configured = entries_by_module[module]
            target = root / module
            target.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir(exist_ok=True)
            git(target, "init", "-q", capture=False)
            targets[module] = target
            if not configured:
                continue
            mirrors = {lock["mirror"]["url"] for _phase, _entry, lock in configured}
            fail(len(mirrors) == 1, f"{module}: immutable locks use multiple mirrors")
            git(target, "remote", "add", "immutable", next(iter(mirrors)), capture=False)
            transaction = uuid.uuid4().hex
            specs: list[str] = []
            local_refs: list[tuple[str, str]] = []
            for index, (_phase, _entry, lock) in enumerate(configured):
                base_ref = f"refs/oracle/{transaction}/{index}/base"
                source_ref = f"refs/oracle/{transaction}/{index}/source"
                specs.extend(
                    [
                        f"{lock['mirror']['base_ref']}:{base_ref}",
                        f"{lock['mirror']['source_ref']}:{source_ref}",
                    ]
                )
                local_refs.append((base_ref, source_ref))
            git(target, "fetch", "--no-tags", "immutable", *specs, capture=False)
            fetch_transactions += 1
            assert_clean_odb(target)
            for (_phase, entry, lock), (base_ref, source_ref) in zip(
                configured, local_refs, strict=True
            ):
                proofs[(entry["module"], entry["patch"])] = validate_lock(
                    target, entry, lock, base_ref, source_ref
                )
            git(target, "fsck", "--no-dangling", capture=False)
            first = proofs[(configured[0][1]["module"], configured[0][1]["patch"])]["base"]
            git(target, "checkout", "--quiet", "--detach", first, capture=False)

        phase_results: dict[str, list[dict[str, str]]] = {}
        generated_locks: list[dict[str, Any]] = []
        current_lock = frozen
        for phase, profile_data, plan in plans:
            results: list[dict[str, str]] = []
            parent_content_tree: str | None = None
            for module in plan.batch["module_order"]:
                target = targets[module]
                for entry in (candidate for candidate in plan if candidate["module"] == module):
                    proof = proofs[(entry["module"], entry["patch"])]
                    before = git(target, "rev-parse", "HEAD")
                    before_tree = git(target, "rev-parse", "HEAD^{tree}")
                    base_tree = git(target, "rev-parse", f"{proof['base']}^{{tree}}")
                    for commit in proof["ordered_commits"]:
                        replay_commit(target, commit)
                    after = git(target, "rev-parse", "HEAD")
                    applied_tree = git(target, "rev-parse", "HEAD^{tree}")
                    if before_tree == base_tree:
                        expected_tree = proof["expected_tree"]
                    else:
                        assert_exact_replay(target, proof, before, after)
                        # A parent tree embeds generated child commit IDs.
                        # The independent oracle derives that tree rather than
                        # importing an integration-only object.  Exact parent
                        # content and child publication are checked below.
                        expected_tree = (
                            applied_tree
                            if module == "darling"
                            else plan.composition["boundaries"][
                                (module, entry["patch"])
                            ]
                        )
                    fail(
                        applied_tree == expected_tree,
                        f"{phase}/{entry['patch']}: oracle tree {applied_tree} "
                        f"differs from expected tree {expected_tree}",
                    )
                    results.append(
                        {
                            "module": module,
                            "patch": entry["patch"],
                            "base": proof["base"],
                            "source": proof["source"],
                            "canonical_tree": proof["expected_tree"],
                            "applied_commit": git(target, "rev-parse", "HEAD"),
                            "applied_tree": applied_tree,
                            "verdict": "VALID",
                        }
                    )
                if module == "darling":
                    parent_content_tree = git(target, "rev-parse", "HEAD^{tree}")
            darling = targets["darling"]
            nested = [
                str(Path(module).relative_to("darling"))
                for module in plan.batch["module_order"]
                if module != "darling" and module.startswith("darling/")
            ]
            if nested:
                git(darling, "add", "--", *nested, capture=False)
                date = profile_data["integration-date"]
                env = os.environ.copy()
                env["GIT_AUTHOR_DATE"] = date
                env["GIT_COMMITTER_DATE"] = date
                git(
                    darling,
                    *IDENTITY,
                    "commit",
                    "-m",
                    f"Integrate {phase} patch profile",
                    capture=False,
                    env=env,
                )
            fail(
                parent_content_tree is not None,
                f"{phase}: parent content boundary was not replayed",
            )
            verify_parent_integration(
                darling,
                parent_content_tree,
                git(darling, "rev-parse", "HEAD^{tree}"),
                {
                    module: targets[module]
                    for module in plan.batch["module_order"]
                    if module != "darling" and module.startswith("darling/")
                },
            )
            expected_finals = plan.composition["integration_finals"]
            for module, expected_tree in expected_finals.items():
                if module == "darling":
                    continue
                try:
                    patch_stack_profile_composition.verify_integration(
                        module,
                        targets[module],
                        expected_tree,
                        expected_finals,
                        targets,
                    )
                except patch_stack_profile_composition.ProfileCompositionError as error:
                    raise OracleError(f"{phase}/{module}: {error}") from error
            current_lock, generated_row = generated_lock(
                current_lock, phase, plan.batch["module_order"], targets
            )
            generated_locks.append(generated_row)
            phase_results[phase] = results

        module_rows = [
            {
                "module": module,
                "commit": git(targets[module], "rev-parse", "HEAD"),
                "tree": git(targets[module], "rev-parse", "HEAD^{tree}"),
            }
            for module in sorted(entries_by_module)
        ]
        batches = []
        for phase, _profile_data, plan in plans:
            results = phase_results[phase]
            fail(len(results) == plan.batch["expected_count"], f"{phase}: oracle count differs")
            batches.append(
                {
                    "profile": phase,
                    "batch_id": plan.batch["batch_id"],
                    "expected_count": plan.batch["expected_count"],
                    "module_order": plan.batch["module_order"],
                    "series_order": plan.batch["series_order"],
                    "series": results,
                    "verdict": "VALID",
                }
            )
        payload = {
            "oracle_schema_version": 2,
            "mode": "immutable-cherry-pick-oracle",
            "profile": profile,
            "profile_order": stack,
            "batches": batches,
            "modules": module_rows,
            "generated_profile_locks": generated_locks,
            "frozen_manifest_sha256": hashlib.sha256(frozen_path.read_bytes()).hexdigest(),
            "clean_odb": {
                "module_count": len(entries_by_module),
                "immutable_fetch_transactions": fetch_transactions,
                "alternates": 0,
                "shallow": 0,
                "partial": 0,
            },
            "cleanup": {"root": "removed", "worktrees": "removed", "refs": "removed"},
            "verdict": "VALID",
        }
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            shutil.rmtree(root)
        except OSError as cleanup_error:
            if primary_error is not None:
                primary_error.add_note(
                    f"immutable oracle cleanup also failed: {cleanup_error}"
                )
            else:
                raise OracleError(
                    f"immutable oracle cleanup failed: {cleanup_error}"
                ) from cleanup_error

    fail(payload is not None, "oracle did not produce evidence")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        temporary.replace(output)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise OracleError(f"oracle evidence write failed: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--profile", choices=("homebrew", "perf", "arch"), required=True)
    parser.add_argument("--mapping", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        apply(
            args.workspace.resolve(),
            args.profile,
            args.mapping.resolve() if args.mapping else None,
            args.output.resolve(),
        )
    except (
        OracleError,
        patch_stack_lock_first.LockFirstError,
        patch_stack_materialize.MaterializeError,
    ) as error:
        print(f"immutable lock oracle: ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

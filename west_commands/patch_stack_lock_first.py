"""Typed, opt-in canonical replacement for one legacy patch application.

This module deliberately contains no profile or patch-name literals.  The
allowlist is data in ``locks/patch-stack/lock-first-series-v1.yml`` and is
validated before the normal patch lifecycle mutates a worktree.
"""
from __future__ import annotations

import tempfile
import uuid
import shutil
import os
import subprocess
from pathlib import Path
from typing import Any

import patch_stack_materialize
import patch_stack_shadow


class LockFirstError(RuntimeError):
    pass


ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "locks" / "patch-stack" / "lock-first-series-v1.yml"


def _cherry_pick(repo: Path, commit: str) -> None:
    author_date = patch_stack_materialize._git(repo, "show", "-s", "--format=%aI", commit)
    result = subprocess.run(
        ["git", "cherry-pick", "--no-edit", commit], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GIT_COMMITTER_DATE": author_date},
    )
    if result.returncode:
        raise LockFirstError(f"git cherry-pick {commit} failed ({result.returncode}): {result.stderr.strip()}")


def plan(profile: str, patches: list[dict[str, Any]], mapping_path: Path = MAPPING) -> dict[str, str]:
    """Return exactly one pre-authorized series, or fail before mutation."""
    try:
        return patch_stack_shadow.plan(profile, patches, mapping_path)
    except patch_stack_shadow.ShadowError as error:
        raise LockFirstError(str(error)) from error


def materialize_into(
    repo: Path,
    lock_first_plan: dict[str, str],
    legacy_patch: Path,
    oracle_evidence: Path | None = None,
) -> dict[str, Any]:
    """Verify both canonical and legacy inputs, then advance ``repo`` to source.

    ``patch_stack_materialize`` proves the immutable graph in this repository.
    The independent shadow runner is the legacy-mbox equivalence oracle; it
    never accesses the integration worktree.  The temporary result ref is
    removed before returning, so lock-first leaves no canonical publication
    state behind in a production checkout.
    """
    lock_path = Path(lock_first_plan["lock_path"])
    lock = patch_stack_materialize.load_lock(lock_path)
    transaction = uuid.uuid4().hex
    result_ref = f"refs/west/patch-stack-results/lock-first/{transaction}"
    fetched_ref = f"refs/west/patch-stack-lock-first/{transaction}"
    evidence = oracle_evidence or Path(tempfile.gettempdir()) / f"west-patch-lock-first-{transaction}.json"
    source: str | None = None
    root = Path(tempfile.gettempdir()) / f"west-patch-lock-first-{transaction}"
    try:
        oracle = patch_stack_shadow.run_shadow(
            shadow_plan=lock_first_plan,
            legacy_patch=legacy_patch,
            evidence_path=evidence,
        )
        # The production Darling parent can legitimately report an untracked
        # nested West project.  Do not weaken materializer cleanliness for
        # that layout: validate in a fresh independent object database.
        root.mkdir()
        canonical = root / "canonical"
        patch_stack_materialize._git(root, "init", "-q", str(canonical))
        patch_stack_materialize._git(canonical, "remote", "add", "immutable", lock["mirror"]["url"])
        patch_stack_materialize._git(
            canonical, "fetch", "--no-tags", "immutable",
            f"{lock['mirror']['base_ref']}:refs/heads/seed",
        )
        patch_stack_materialize._git(canonical, "checkout", "--detach", "refs/heads/seed")
        result = patch_stack_materialize.materialize(
            canonical, lock_path, result_ref=result_ref,
        )
        source = result["fetched"]["source_oid"]
        if result["verdict"] != "VALID" or result["resulting_tree"] != oracle["legacy_resulting_tree"]:
            raise LockFirstError("canonical materialization and legacy oracle differ")
        patch_stack_materialize._git(
            repo, "fetch", "--no-tags", "--no-recurse-submodules", lock["mirror"]["url"],
            f"{lock['mirror']['source_ref']}:{fetched_ref}",
        )
        if patch_stack_materialize._oid(repo, fetched_ref) != source:
            raise LockFirstError("production immutable source fetch differs from validated source")
        # The profile can intentionally apply a retained patch onto a newer
        # effective base than the lock's historical upstream base.  Preserve
        # that lifecycle by replaying the immutable, validated commits with
        # ordinary Git rather than resetting the production checkout to the
        # historical source tip (which would discard intervening upstream).
        for commit in result["ordered_commits"]:
            _cherry_pick(repo, commit)
        return {"source": source, "tree": result["resulting_tree"], "oracle": oracle}
    except (patch_stack_shadow.ShadowError, patch_stack_materialize.MaterializeError) as error:
        raise LockFirstError(str(error)) from error
    finally:
        # Never remove an existing/concurrently replaced ref: only delete the
        # exact transaction result created by this invocation.
        try:
            if source is not None:
                outcome = patch_stack_materialize._rollback_result(root / "canonical", result_ref, source)
                if outcome not in ("removed", "already-absent"):
                    raise LockFirstError(f"lock-first result-ref cleanup failed: {outcome}")
            patch_stack_materialize._delete_ref(repo, fetched_ref)
            if root.exists():
                shutil.rmtree(root)
        finally:
            if oracle_evidence is None:
                evidence.unlink(missing_ok=True)

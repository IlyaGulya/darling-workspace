#!/usr/bin/env python3
"""Focused no-mbox contract for runtime-source homebrew materialization."""
from __future__ import annotations

import sys
import types
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import test_runtime_source as runtime_source


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def must_raise(expected, callback) -> BaseException:
    try:
        callback()
    except expected as error:
        return error
    raise AssertionError(f"expected {expected.__name__}")


def runtime_fixture(root: Path):
    """Create eight real source repositories and a 3-commit XNU immutable lock."""
    modules = [
        "darling/src/external/darlingserver", "darling/src/external/xnu",
        "darling/src/external/libplatform", "darling/src/external/perl",
        "darling/src/external/libressl-2.8.3", "darling/src/external/libpthread",
        "darling", "darling/src/external/installer",
    ]
    projects = {}
    for module in modules:
        repo = root / "sources" / module
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Runtime contract")
        git(repo, "config", "user.email", "runtime-contract@example.invalid")
        (repo / "fixture").write_text("base\n")
        git(repo, "add", "fixture"); git(repo, "commit", "-qm", "base")
        projects[module] = repo
    xnu = projects["darling/src/external/xnu"]
    base = git(xnu, "rev-parse", "HEAD")
    bare = root / "immutable-xnu.git"
    git(root, "init", "--bare", "-q", str(bare))
    git(xnu, "remote", "add", "immutable", bare.as_uri())
    git(xnu, "tag", f"patch-stack/v1/bases/{base}", base)
    commits=[]
    for value in ("one", "two", "three"):
        (xnu / "fixture").write_text(value + "\n")
        git(xnu, "commit", "-am", value)
        commits.append(git(xnu, "rev-parse", "HEAD"))
    source, tree = commits[-1], git(xnu, "rev-parse", "HEAD^{tree}")
    git(xnu, "tag", f"patch-stack/v1/sources/{source}", source)
    git(xnu, "push", "-q", "immutable", "HEAD:refs/heads/main", "--tags")
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    git(xnu, "reset", "--hard", "-q", base)
    lock = {
        "schema_version": 2, "project": {"name": "xnu", "path": "."},
        "upstream": {"url": bare.as_uri(), "base_commit": base},
        "mirror": {"url": bare.as_uri(),
            "base_ref": f"refs/tags/patch-stack/v1/bases/{base}", "base_oid": base,
            "source_ref": f"refs/tags/patch-stack/v1/sources/{source}", "source_oid": source},
        "source_commit": source, "ordered_commits": commits, "expected_tree": tree,
    }
    lock_path = root / "eunion-hardening.yml"
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False))
    patches = [{"module": module, "path": f"{module}/fixture.patch"} for module in modules]
    plan_entries = [
        {"profile": "homebrew", "module": patch["module"], "patch": patch["path"],
         "lock": str(lock_path), "lock_path": str(lock_path)}
        for patch in patches
    ]
    plan = runtime_source.patch_stack_lock_first.LockFirstPlan(plan_entries, {
        "batch_id": "darling-homebrew-lock-first-batch-7", "expected_count": 69,
        "series": plan_entries,
    })
    messages=[]
    host = types.SimpleNamespace(
        inf=messages.append, _projects=lambda: projects,
        _profile_stack=lambda _profile: ["homebrew"],
        _profile_stack_modules=lambda _profile: set(modules),
        _load_profile=lambda _profile: {"patches": patches},
        _manifest_revision=lambda module: base if module == "darling/src/external/xnu" else git(projects[module], "rev-parse", "HEAD"),
    )
    baseline = {module: (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain=v1"))
                for module, repo in projects.items()}
    return modules, projects, plan, host, xnu, lock_path, messages, baseline


def assert_source_restored(projects: dict[str, Path], baseline: dict[str, tuple[str, str]]) -> None:
    for module, repo in projects.items():
        assert (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain=v1")) == baseline[module]
        assert not Path(git(repo, "rev-parse", "--git-path", "rebase-apply")).exists()
        assert not list(repo.glob(".git/worktrees/*"))
        assert not list((repo / ".git").glob("refs/west/patch-stack-lock-first/**/*"))


def real_rollback_contract() -> None:
    """Exercise actual context cleanup and actual native replay before failure.

    The first/middle/last module cases prove lifecycle-wide cleanup.  The
    eunion case calls the real immutable fetch + native git-am replay, lets
    two commits land, and only then injects the original exception.
    """
    for exception_type in (runtime_source.patch_stack_lock_first.LockFirstError, KeyboardInterrupt):
        for failing_index in (0, 4, 7):
            with tempfile.TemporaryDirectory() as directory:
                modules, projects, plan, host, _xnu, _lock_path, messages, baseline = runtime_fixture(Path(directory))
                materializer = runtime_source.RuntimeSourceMaterializer(host)
                old_plan, old_batch, old_legacy = (runtime_source.patch_stack_lock_first.plan,
                                                   runtime_source.patch_stack_lock_first.materialize_batch_into,
                                                   runtime_source.git_for_temporary_patch_application)
                calls=[]
                try:
                    runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
                    runtime_source.git_for_temporary_patch_application = lambda *_args: (_ for _ in ()).throw(AssertionError("legacy fallback invoked"))
                    def fail_at(target, entries):
                        calls.append(entries[0]["module"])
                        if len(calls) - 1 == failing_index:
                            raise exception_type("injected module failure") if exception_type is not KeyboardInterrupt else KeyboardInterrupt()
                        return [], {}
                    runtime_source.patch_stack_lock_first.materialize_batch_into = fail_at
                    must_raise(exception_type, lambda: materializer.profile_worktree_checkout("homebrew").__enter__())
                    assert calls == modules[:failing_index + 1]
                    assert "PATCH_STACK_MODE=default-lock-first materializer=runtime-source" in messages
                    assert not any(line.startswith("PATCH_STACK_REPLAY") for line in messages)
                    assert_source_restored(projects, baseline)
                finally:
                    runtime_source.patch_stack_lock_first.plan = old_plan
                    runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch
                    runtime_source.git_for_temporary_patch_application = old_legacy
        with tempfile.TemporaryDirectory() as directory:
            modules, projects, plan, host, _xnu, _lock_path, messages, baseline = runtime_fixture(Path(directory))
            materializer = runtime_source.RuntimeSourceMaterializer(host)
            old_plan, old_cherry, old_legacy = (runtime_source.patch_stack_lock_first.plan,
                                                runtime_source.patch_stack_lock_first._cherry_pick,
                                                runtime_source.git_for_temporary_patch_application)
            replayed=[]
            try:
                runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
                runtime_source.git_for_temporary_patch_application = lambda *_args: (_ for _ in ()).throw(AssertionError("legacy fallback invoked"))
                def interrupt_after_two(repo, commit):
                    if len(replayed) == 2:
                        raise exception_type("injected eunion hardening failure") if exception_type is not KeyboardInterrupt else KeyboardInterrupt()
                    old_cherry(repo, commit); replayed.append(commit)
                runtime_source.patch_stack_lock_first._cherry_pick = interrupt_after_two
                # Skip preceding modules; invoke the genuine XNU batch only.
                def xnu_only(target, entries):
                    if entries[0]["module"] == "darling/src/external/xnu":
                        return old_batch(target, entries)
                    return [], {}
                old_batch = runtime_source.patch_stack_lock_first.materialize_batch_into
                runtime_source.patch_stack_lock_first.materialize_batch_into = xnu_only
                must_raise(exception_type, lambda: materializer.profile_worktree_checkout("homebrew").__enter__())
                assert len(replayed) == 2
                assert not any(line.startswith("PATCH_STACK_REPLAY") for line in messages)
                assert_source_restored(projects, baseline)
            finally:
                runtime_source.patch_stack_lock_first.plan = old_plan
                runtime_source.patch_stack_lock_first._cherry_pick = old_cherry
                runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch
                runtime_source.git_for_temporary_patch_application = old_legacy
    # A malformed final result (the point immediately before the context is
    # returned to its caller) must take the same all-worktree rollback path.
    with tempfile.TemporaryDirectory() as directory:
        modules, projects, plan, host, _xnu, _lock_path, messages, baseline = runtime_fixture(Path(directory))
        materializer = runtime_source.RuntimeSourceMaterializer(host)
        old_plan, old_batch = (runtime_source.patch_stack_lock_first.plan,
                               runtime_source.patch_stack_lock_first.materialize_batch_into)
        calls=[]
        try:
            runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
            runtime_source.patch_stack_lock_first.materialize_batch_into = (
                lambda _target, entries: (calls.append(entries[0]["module"]) or ([], {}))
            )
            must_raise(runtime_source.patch_stack_lock_first.LockFirstError,
                       lambda: materializer.profile_worktree_checkout("homebrew").__enter__())
            assert calls == modules
            assert not any(line.startswith("PATCH_STACK_REPLAY") for line in messages)
            assert_source_restored(projects, baseline)
        finally:
            runtime_source.patch_stack_lock_first.plan = old_plan
            runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch


def main() -> None:
    modules = [
        "darling/src/external/darlingserver", "darling/src/external/xnu",
        "darling/src/external/libplatform", "darling/src/external/perl",
        "darling/src/external/libressl-2.8.3", "darling/src/external/libpthread",
        "darling", "darling/src/external/installer",
    ]
    patches = [{"module": module, "path": f"{module}/p.patch"} for module in modules]
    messages: list[str] = []
    host = types.SimpleNamespace(
        inf=messages.append,
        _profile_stack=lambda _profile: ["homebrew"],
        _load_profile=lambda _profile: {"patches": patches},
    )
    materializer = runtime_source.RuntimeSourceMaterializer(host)
    plan = runtime_source.patch_stack_lock_first.LockFirstPlan(
        [{"profile": "homebrew", "module": patch["module"], "patch": patch["path"], "lock": "x", "lock_path": "x"} for patch in patches],
        {"batch_id": "darling-homebrew-lock-first-batch-7", "expected_count": 69,
         "series_order": [{"module": patch["module"], "patch": patch["path"]} for patch in patches],
         "module_order": modules},
    )
    old_plan = runtime_source.patch_stack_lock_first.plan
    old_batch = runtime_source.patch_stack_lock_first.materialize_batch_into
    calls: list[str] = []
    try:
        runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
        def batch(target, entries):
            calls.append(entries[0]["module"])
            return ([{"module": entries[0]["module"]}] * (69 if entries[0]["module"] == modules[-1] else 0), {})
        runtime_source.patch_stack_lock_first.materialize_batch_into = batch
        # Make the synthetic 69-result batch land on the final module while
        # still proving all eight module calls occur in typed order.
        materializer._materialize_canonical_homebrew({module: Path("/tmp") for module in modules})
        assert calls == modules
        assert messages[0] == "PATCH_STACK_MODE=default-lock-first materializer=runtime-source"
        assert messages[-1].startswith("PATCH_STACK_REPLAY batch=darling-homebrew-lock-first-batch-7 expected=69 applied=69 modules=8 elapsed_seconds=")
        assert messages[-1].endswith(" verdict=VALID")
        runtime_source.patch_stack_lock_first.plan = lambda *_args: (_ for _ in ()).throw(runtime_source.patch_stack_lock_first.LockFirstError("bad mapping"))
        messages.clear()
        try:
            materializer._materialize_canonical_homebrew({module: Path("/tmp") for module in modules})
        except runtime_source.patch_stack_lock_first.LockFirstError:
            pass
        else:
            raise AssertionError("invalid mapping fell through")
        assert not messages
    finally:
        runtime_source.patch_stack_lock_first.plan = old_plan
        runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch
    real_rollback_contract()
    print("runtime-source lock-first contract: PASS")


if __name__ == "__main__":
    main()

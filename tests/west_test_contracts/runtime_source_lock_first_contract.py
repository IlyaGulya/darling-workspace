#!/usr/bin/env python3
"""Focused no-mbox contract for runtime-source lock-first materialization."""
from __future__ import annotations

import os
import sys
import types
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

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
    darling_base = git(projects["darling"], "rev-parse", "HEAD")
    darling_lock_path = root / "darling-base.yml"
    darling_lock = dict(lock)
    darling_lock["upstream"] = {"url": bare.as_uri(), "base_commit": darling_base}
    darling_lock["mirror"] = dict(lock["mirror"])
    darling_lock["mirror"]["base_ref"] = f"refs/tags/patch-stack/v1/bases/{darling_base}"
    darling_lock["mirror"]["base_oid"] = darling_base
    darling_lock_path.write_text(yaml.safe_dump(darling_lock, sort_keys=False))
    patches = [{"module": module, "path": f"{module}/fixture.patch"} for module in modules]
    plan_entries = [
        {"profile": "homebrew", "module": patch["module"], "patch": patch["path"],
         "lock": str(darling_lock_path if patch["module"] == "darling" else lock_path),
         "lock_path": str(darling_lock_path if patch["module"] == "darling" else lock_path)}
        for patch in patches
    ]
    plan = runtime_source.patch_stack_lock_first.LockFirstPlan(plan_entries, {
        "batch_id": "darling-homebrew-rootless-productization-batch-8",
        "expected_count": 72,
        "series": plan_entries,
    }, {
        "schema_version": 2, "path": "fixture-profile-composition.yml", "prerequisites": [],
        "frozen_manifest": {"path": "west.lock.yml", "sha256": "0" * 64},
        "starts": {module: {"source_oid": base, "tree": tree} for module in modules},
        "boundaries": {(entry["module"], entry["patch"]): tree for entry in plan_entries},
        "finals": {module: tree for module in modules},
        "integration_finals": {module: tree for module in modules},
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
    for repo in projects.values():
        subprocess.run(["git", "config", "--unset-all", "user.name"], cwd=repo,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "--unset-all", "user.email"], cwd=repo,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return modules, projects, plan, host, xnu, lock_path, messages, baseline, commits


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
                modules, projects, plan, host, _xnu, _lock_path, messages, baseline, _commits = runtime_fixture(Path(directory))
                materializer = runtime_source.RuntimeSourceMaterializer(host)
                old_plan, old_batch = (
                    runtime_source.patch_stack_lock_first.plan,
                    runtime_source.patch_stack_lock_first.materialize_batch_into,
                )
                calls=[]
                try:
                    runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
                    def fail_at(target, entries, **_kwargs):
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
        with tempfile.TemporaryDirectory() as directory:
            modules, projects, plan, host, _xnu, _lock_path, messages, baseline, _commits = runtime_fixture(Path(directory))
            materializer = runtime_source.RuntimeSourceMaterializer(host)
            old_plan, old_cherry = (
                runtime_source.patch_stack_lock_first.plan,
                runtime_source.patch_stack_lock_first._cherry_pick,
            )
            replayed=[]
            try:
                runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
                def interrupt_after_two(repo, commit, **kwargs):
                    if len(replayed) == 2:
                        raise exception_type("injected eunion hardening failure") if exception_type is not KeyboardInterrupt else KeyboardInterrupt()
                    old_cherry(repo, commit, **kwargs); replayed.append(commit)
                runtime_source.patch_stack_lock_first._cherry_pick = interrupt_after_two
                # Skip preceding modules; invoke the genuine XNU batch only.
                def xnu_only(target, entries, **kwargs):
                    if entries[0]["module"] == "darling/src/external/xnu":
                        return old_batch(target, entries, **kwargs)
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
    # A malformed final result (the point immediately before the context is
    # returned to its caller) must take the same all-worktree rollback path.
    with tempfile.TemporaryDirectory() as directory:
        modules, projects, plan, host, _xnu, _lock_path, messages, baseline, _commits = runtime_fixture(Path(directory))
        materializer = runtime_source.RuntimeSourceMaterializer(host)
        old_plan, old_batch = (runtime_source.patch_stack_lock_first.plan,
                               runtime_source.patch_stack_lock_first.materialize_batch_into)
        calls=[]
        try:
            runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
            runtime_source.patch_stack_lock_first.materialize_batch_into = (
                lambda _target, entries, **_kwargs: (calls.append(entries[0]["module"]) or ([], {}))
            )
            must_raise(runtime_source.patch_stack_lock_first.LockFirstError,
                       lambda: materializer.profile_worktree_checkout("homebrew").__enter__())
            assert calls == modules
            assert not any(line.startswith("PATCH_STACK_REPLAY") for line in messages)
            assert_source_restored(projects, baseline)
        finally:
            runtime_source.patch_stack_lock_first.plan = old_plan
            runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch


def identity_contract() -> None:
    """Reproduce the hosted empty-ident failure without mutating any config."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        modules, projects, plan, host, xnu, _lock_path, messages, baseline, commits = runtime_fixture(root)
        materializer = runtime_source.RuntimeSourceMaterializer(host)
        source_author = git(xnu, "show", "-s", "--format=%an%x00%ae%x00%aI", commits[0])
        configs_before = {module: git(repo, "config", "--local", "--list") for module, repo in projects.items()}
        old_plan, old_batch = (
            runtime_source.patch_stack_lock_first.plan,
            runtime_source.patch_stack_lock_first.materialize_batch_into,
        )
        try:
            runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
            # This contract isolates command-scoped identity for the genuine
            # XNU replay; its other seven modules are intentional stubs.
            # Full profile-final trees are exercised by the composition E2E.
            plan.composition["integration_finals"] = {}
            real_batch = old_batch
            result_counts: list[tuple[str, int]] = []
            def batch(target, entries, **kwargs):
                if entries[0]["module"] == "darling/src/external/xnu":
                    result = real_batch(target, entries, **kwargs)
                    result_counts.append((entries[0]["module"], len(result[0])))
                    return result
                if entries[0]["module"] == "darling/src/external/installer":
                    # The synthetic fixture represents the remaining Batch 8
                    # entries without inventing extra repositories.
                    result = ([{"module": "synthetic"}] * 71, {})
                    result_counts.append((entries[0]["module"], len(result[0])))
                    return result
                return [], {}
            runtime_source.patch_stack_lock_first.materialize_batch_into = batch
            empty_home = root / "empty-home"; empty_home.mkdir()
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_AUTHOR_") and not key.startswith("GIT_COMMITTER_")}
            env.update({"HOME": str(empty_home), "GIT_CONFIG_NOSYSTEM": "1"})
            with mock.patch.dict(os.environ, env, clear=True):
                global_before = subprocess.run(
                    ["git", "config", "--global", "--list"], check=False,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                with materializer.profile_worktree_checkout("homebrew"):
                    target = host._project_overrides["darling/src/external/xnu"]
                    applied = git(target, "show", "-s", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI", "HEAD~2")
                    author_name, author_email, author_date, committer_name, committer_email, committer_date = applied.split("\x00")
                    assert "\x00".join((author_name, author_email, author_date)) == source_author
                    assert (committer_name, committer_email) == ("West Test", "west-test@example.invalid")
                    assert committer_date == author_date
                assert result_counts == [
                    ("darling/src/external/xnu", 1),
                    ("darling/src/external/installer", 71),
                ]
                global_after = subprocess.run(
                    ["git", "config", "--global", "--list"], check=False,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                assert (global_after.returncode, global_after.stdout, global_after.stderr) == (
                    global_before.returncode, global_before.stdout, global_before.stderr
                )
            assert any(line.startswith("PATCH_STACK_REPLAY ") and line.endswith("verdict=VALID") for line in messages)
            assert_source_restored(projects, baseline)
            assert {module: git(repo, "config", "--local", "--list") for module, repo in projects.items()} == configs_before
        finally:
            runtime_source.patch_stack_lock_first.plan = old_plan
            runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch


def profile_routing_contract() -> None:
    """Only approved whole profiles take the typed runtime-source route."""
    arch_modules = [
        "darling/src/external/libunwind", "darling/src/external/xnu",
        "darling/src/external/darlingserver", "darling",
    ]
    perf_modules = [
        "darling", "darling/src/external/xnu", "darling/src/external/dyld",
        "darling/src/external/darlingserver",
    ]
    profile_patches = {
        "arch": [{"module": module, "path": f"arch/{module}.patch"} for module in arch_modules],
        "perf": [{"module": module, "path": f"perf/{module}.patch"} for module in perf_modules],
    }
    messages: list[str] = []
    host = types.SimpleNamespace(
        inf=messages.append,
        _profile_stack=lambda profile: [profile],
        _load_profile=lambda profile: {"patches": profile_patches[profile]},
    )
    materializer = runtime_source.RuntimeSourceMaterializer(host)
    old_plan = runtime_source.patch_stack_lock_first.plan
    old_batch = runtime_source.patch_stack_lock_first.materialize_batch_into
    old_run = runtime_source.subprocess.run
    old_load_lock = runtime_source.patch_stack_materialize.load_lock
    old_git = runtime_source.patch_stack_materialize._git
    calls: list[tuple[str, list[str]]] = []
    try:
        def composition(entries):
            modules = list(dict.fromkeys(entry["module"] for entry in entries))
            return {
                "schema_version": 2, "path": "synthetic-profile-composition.yml", "prerequisites": [],
                "frozen_manifest": {"path": "west.lock.yml", "sha256": "0" * 64},
                "starts": {module: {"source_oid": "0" * 40, "tree": "1" * 40} for module in modules},
                "boundaries": {(entry["module"], entry["patch"]): f"{index + 2:040x}" for index, entry in enumerate(entries)},
                "finals": {module: next(f"{index + 2:040x}" for index, entry in reversed(list(enumerate(entries))) if entry["module"] == module) for module in modules},
                "integration_finals": {module: next(f"{index + 2:040x}" for index, entry in reversed(list(enumerate(entries))) if entry["module"] == module) for module in modules},
            }

        def plan(profile, patches, *_args):
            entries = [
                {"profile": profile, "module": patch["module"], "patch": patch["path"],
                 "lock": f"entry-{index}.yml", "lock_path": f"entry-{index}.yml"}
                for index, patch in enumerate(patches)
            ]
            if profile == "arch":
                batch_id, expected_count = "darling-arch-lock-first-batch-1", 19
            elif profile == "perf":
                batch_id, expected_count = "darling-perf-lock-first-batch-1", 7
            else:
                batch_id, expected_count = f"{profile}-batch", len(entries)
            return runtime_source.patch_stack_lock_first.LockFirstPlan(entries, {
                "batch_id": batch_id, "expected_count": expected_count,
                "series_order": [{"module": entry["module"], "patch": entry["patch"]} for entry in entries],
                "module_order": list(dict.fromkeys(entry["module"] for entry in entries)),
            }, composition(entries))
        runtime_source.patch_stack_lock_first.plan = plan
        def batch(_target, entries, **_kwargs):
            calls.append((entries[0]["module"], [entry["patch"] for entry in entries]))
            if entries[0]["profile"] == "perf":
                count = {
                    "darling": 1, "darling/src/external/xnu": 2,
                    "darling/src/external/dyld": 1,
                    "darling/src/external/darlingserver": 3,
                }[entries[0]["module"]]
            else:
                count = 16 if entries[0]["module"] == "darling" else 1
            return ([{"module": entries[0]["module"]}] * count, {})
        runtime_source.patch_stack_lock_first.materialize_batch_into = batch
        runtime_source.patch_stack_materialize.load_lock = (
            lambda _path: {"upstream": {"base_commit": "0" * 40}}
        )
        runtime_source.patch_stack_materialize._git = lambda *_args, **_kwargs: ""
        # Runtime-source records disposable parent boundaries after each
        # composition phase.  This pure planner fixture deliberately has no
        # Git repositories, so acknowledge only those lifecycle commands
        # while the real replay contracts cover native Git behavior.
        targets = {module: Path("/tmp") / str(index) for index, module in enumerate(arch_modules)}
        target_trees = {
            str(targets[module]): plan("arch", profile_patches["arch"]).composition["integration_finals"][module]
            for module in arch_modules
        }
        def fake_run(args, **kwargs):
            stdout = target_trees.get(str(kwargs.get("cwd")), "") if args[-1] == "HEAD^{tree}" else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout)
        runtime_source.subprocess.run = fake_run
        materializer._materialize_canonical_profile("arch", targets)
        assert calls == [
            (module, [f"arch/{module}.patch"]) for module in arch_modules
        ]
        assert messages[0] == "PATCH_STACK_MODE=default-lock-first materializer=runtime-source profile=arch"
        assert messages[-1].startswith("PATCH_STACK_REPLAY batch=darling-arch-lock-first-batch-1 expected=19 applied=19 modules=4 elapsed_seconds=")
        assert messages[-1].endswith(" verdict=VALID")
        assert runtime_source.RuntimeSourceMaterializer._materialize_canonical_profile
        messages.clear()
        calls.clear()
        perf_targets = {module: Path("/tmp") / f"perf-{index}" for index, module in enumerate(perf_modules)}
        perf_plan = plan("perf", profile_patches["perf"])
        target_trees.update({
            str(perf_targets[module]): perf_plan.composition["integration_finals"][module]
            for module in perf_modules
        })
        materializer._materialize_canonical_profile(
            "perf", perf_targets,
        )
        assert calls == [(module, [f"perf/{module}.patch"]) for module in perf_modules]
        assert messages[0] == "PATCH_STACK_MODE=default-lock-first materializer=runtime-source"
        assert messages[-1].startswith(
            "PATCH_STACK_REPLAY batch=darling-perf-lock-first-batch-1 "
            "expected=7 applied=7 modules=4 elapsed_seconds="
        )
        assert messages[-1].endswith(" verdict=VALID")
        messages.clear()
        calls.clear()
        divergent_entries = [
            {"profile": "arch", "module": arch_modules[0], "patch": "arch/one.patch", "lock_path": "one.yml"},
            {"profile": "arch", "module": arch_modules[0], "patch": "arch/two.patch", "lock_path": "two.yml"},
            *[{"profile": "arch", "module": module, "patch": f"arch/{index}-{module}.patch", "lock_path": f"rest-{index}.yml"}
              for index, module in enumerate(arch_modules[1:], 1)],
            *[{"profile": "arch", "module": arch_modules[-1], "patch": f"arch/filler-{index}.patch", "lock_path": f"filler-{index}.yml"}
              for index in range(13)],
        ]
        divergent = runtime_source.patch_stack_lock_first.LockFirstPlan(
            divergent_entries,
            {"batch_id": "darling-arch-lock-first-batch-1", "expected_count": 19},
        )
        messages.clear()
        runtime_source.patch_stack_lock_first.plan = lambda *_args: divergent
        try:
            materializer._materialize_canonical_profile("arch", targets)
        except runtime_source.patch_stack_lock_first.LockFirstError as error:
            assert "profile-composition" in str(error)
        else:
            raise AssertionError("arch accepted a missing profile composition")
        assert not messages and not calls

        # Profile composition, rather than an impossible source-OID chain,
        # authorizes a native replay on an inherited but tree-equivalent base.
        rewritten_entries = [
            {"profile": "arch", "module": "darling", "patch": "darling/ci.patch", "lock_path": "ci.yml"},
            {"profile": "arch", "module": "darling", "patch": "darling/shellspawn.patch", "lock_path": "shellspawn.yml"},
            *[{"profile": "arch", "module": module, "patch": f"arch/{index}-{module}.patch", "lock_path": f"tail-{index}.yml"}
              for index, module in enumerate(arch_modules[:-1], 1)],
            *[{"profile": "arch", "module": "darling", "patch": f"arch/filler-{index}.patch", "lock_path": f"filler-{index}.yml"}
              for index in range(13)],
        ]
        rewritten = runtime_source.patch_stack_lock_first.LockFirstPlan(
            rewritten_entries,
            {"batch_id": "darling-arch-lock-first-batch-1", "expected_count": 19},
            composition(rewritten_entries),
        )
        assert rewritten.composition is not None
        assert rewritten.composition["boundaries"][("darling", "darling/shellspawn.patch")]
    finally:
        runtime_source.patch_stack_lock_first.plan = old_plan
        runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch
        runtime_source.subprocess.run = old_run
        runtime_source.patch_stack_materialize.load_lock = old_load_lock
        runtime_source.patch_stack_materialize._git = old_git
        runtime_source.subprocess.run = old_run


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
        {
            "batch_id": "darling-homebrew-rootless-productization-batch-8",
            "expected_count": 72,
         "series_order": [{"module": patch["module"], "patch": patch["path"]} for patch in patches],
         "module_order": modules},
        {"schema_version": 2, "path": "synthetic-profile-composition.yml", "prerequisites": [],
         "frozen_manifest": {"path": "west.lock.yml", "sha256": "0" * 64},
         "starts": {module: {"source_oid": "0" * 40, "tree": "1" * 40} for module in modules},
         "boundaries": {(patch["module"], patch["path"]): f"{index + 2:040x}" for index, patch in enumerate(patches)},
         "finals": {module: f"{index + 2:040x}" for index, module in enumerate(modules)},
         "integration_finals": {module: f"{index + 2:040x}" for index, module in enumerate(modules)}},
    )
    old_plan = runtime_source.patch_stack_lock_first.plan
    old_batch = runtime_source.patch_stack_lock_first.materialize_batch_into
    old_run = runtime_source.subprocess.run
    old_load_lock = runtime_source.patch_stack_materialize.load_lock
    old_git = runtime_source.patch_stack_materialize._git
    calls: list[str] = []
    try:
        runtime_source.patch_stack_lock_first.plan = lambda *_args: plan
        def batch(target, entries, **_kwargs):
            calls.append(entries[0]["module"])
            count = 65 if entries[0]["module"] == "darling/src/external/installer" else 1
            return ([{"module": entries[0]["module"]}] * count, {})
        runtime_source.patch_stack_lock_first.materialize_batch_into = batch
        runtime_source.patch_stack_materialize.load_lock = (
            lambda _path: {"upstream": {"base_commit": "0" * 40}}
        )
        runtime_source.patch_stack_materialize._git = lambda *_args, **_kwargs: ""
        targets = {module: Path("/tmp") / str(index) for index, module in enumerate(modules)}
        target_trees = {
            str(targets[module]): plan.composition["integration_finals"][module]
            for module in modules
        }
        def fake_run(args, **kwargs):
            stdout = target_trees.get(str(kwargs.get("cwd")), "") if args[-1] == "HEAD^{tree}" else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout)
        runtime_source.subprocess.run = fake_run
        # The profile-stack batch is derived from typed entries rather than a
        # homebrew-only literal while all eight module calls stay in order.
        materializer._materialize_canonical_profile("homebrew", targets)
        assert calls == modules
        assert messages[0] == "PATCH_STACK_MODE=default-lock-first materializer=runtime-source"
        assert messages[-1].startswith(
            "PATCH_STACK_REPLAY "
            "batch=darling-homebrew-rootless-productization-batch-8 "
            "expected=72 applied=72 modules=8 elapsed_seconds="
        )
        assert messages[-1].endswith(" verdict=VALID")
        runtime_source.patch_stack_lock_first.plan = lambda *_args: (_ for _ in ()).throw(runtime_source.patch_stack_lock_first.LockFirstError("bad mapping"))
        messages.clear()
        try:
            materializer._materialize_canonical_profile("homebrew", targets)
        except runtime_source.patch_stack_lock_first.LockFirstError:
            pass
        else:
            raise AssertionError("invalid mapping fell through")
        assert not messages
    finally:
        runtime_source.patch_stack_lock_first.plan = old_plan
        runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch
        runtime_source.subprocess.run = old_run
        runtime_source.patch_stack_materialize.load_lock = old_load_lock
        runtime_source.patch_stack_materialize._git = old_git
    real_rollback_contract()
    identity_contract()
    profile_routing_contract()
    print("runtime-source lock-first contract: PASS")


if __name__ == "__main__":
    main()

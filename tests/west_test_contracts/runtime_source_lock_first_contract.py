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

AUTHORED_BATCHES = {
    profile: runtime_source.patch_stack_lock_first.load_mapping(
        runtime_source.patch_stack_lock_first.mapping_for_profile(profile), profile,
    )
    for profile in ("homebrew", "perf", "arch")
}


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
        "batch_id": AUTHORED_BATCHES["homebrew"]["batch_id"],
        "expected_count": AUTHORED_BATCHES["homebrew"]["expected_count"],
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
                    # Represent the other authored series without inventing
                    # extra repositories; XNU contributes one genuine replay.
                    result = ([{"module": "synthetic"}] * (plan.batch["expected_count"] - 1), {})
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
                    ("darling/src/external/installer", plan.batch["expected_count"] - 1),
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
            authored = AUTHORED_BATCHES[profile]
            batch_id, expected_count = authored["batch_id"], authored["expected_count"]
            return runtime_source.patch_stack_lock_first.LockFirstPlan(entries, {
                "batch_id": batch_id, "expected_count": expected_count,
                "series_order": [{"module": entry["module"], "patch": entry["patch"]} for entry in entries],
                "module_order": list(dict.fromkeys(entry["module"] for entry in entries)),
            }, composition(entries))
        runtime_source.patch_stack_lock_first.plan = plan
        def batch(_target, entries, **_kwargs):
            calls.append((entries[0]["module"], [entry["patch"] for entry in entries]))
            count = sum(
                entry["module"] == entries[0]["module"]
                for entry in AUTHORED_BATCHES[entries[0]["profile"]]["series"]
            )
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
        arch_batch = AUTHORED_BATCHES["arch"]
        assert messages[-1].startswith(
            f"PATCH_STACK_REPLAY batch={arch_batch['batch_id']} "
            f"expected={arch_batch['expected_count']} applied={arch_batch['expected_count']} modules=4 elapsed_seconds="
        )
        assert messages[-1].endswith(" verdict=VALID")
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
        perf_batch = AUTHORED_BATCHES["perf"]
        assert messages[-1].startswith(
            f"PATCH_STACK_REPLAY batch={perf_batch['batch_id']} "
            f"expected={perf_batch['expected_count']} applied={perf_batch['expected_count']} modules=4 elapsed_seconds="
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
              for index in range(arch_batch["expected_count"] - 5)],
        ]
        divergent = runtime_source.patch_stack_lock_first.LockFirstPlan(
            divergent_entries,
            {"batch_id": arch_batch["batch_id"], "expected_count": arch_batch["expected_count"]},
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
    finally:
        runtime_source.patch_stack_lock_first.plan = old_plan
        runtime_source.patch_stack_lock_first.materialize_batch_into = old_batch
        runtime_source.subprocess.run = old_run
        runtime_source.patch_stack_materialize.load_lock = old_load_lock
        runtime_source.patch_stack_materialize._git = old_git


def current_minus_module_scope_contract() -> None:
    """Omitting a child patch must not omit or reject its parent module."""
    with tempfile.TemporaryDirectory() as directory:
        _, projects, plan, host, xnu, lock_path, _, _, _ = runtime_fixture(Path(directory))
        host._active_profile = "homebrew"
        patches = host._load_profile("homebrew")["patches"]
        child_patch = next(patch for patch in patches if patch["module"] == "darling/src/external/xnu")
        parent_patch = next(patch for patch in patches if patch["module"] == "darling")
        # Both synthetic modules change the same relative fixture path using
        # the real immutable graph; only their ownership/skip selection differs.
        for entry in plan:
            if entry["module"] == "darling":
                entry["lock_path"] = str(lock_path)
        materializer = runtime_source.RuntimeSourceMaterializer(host)
        parent = projects["darling"]
        with mock.patch.object(runtime_source.patch_stack_lock_first, "plan", return_value=plan):
            materializer.apply_current_minus_profile(child_patch, {}, "darling", parent)
            assert (parent / "fixture").read_text() == "three\n"
            materializer.apply_current_minus_profile(child_patch, {}, "darling/src/external/xnu", xnu)
            assert (xnu / "fixture").read_text() == "base\n"
            materializer.apply_current_minus_profile(
                child_patch, {"current-minus-skip-patches": [parent_patch["path"]]},
                "darling", parent,
            )
            assert (parent / "fixture").read_text() == "base\n"
            before = git(parent, "rev-parse", "HEAD")
            must_raise(runtime_source.patch_stack_lock_first.LockFirstError, lambda:
                materializer.apply_current_minus_profile(
                    child_patch, {"current-minus-skip-patches": ["missing.patch"]},
                    "darling", parent,
                ))
            assert git(parent, "rev-parse", "HEAD") == before


def authored_batch_admission_contract() -> None:
    """An approved mapping, not historical Python literals, selects the batch."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _, _, original, host, target, lock_path, _, _, _ = runtime_fixture(root)
        module = "darling/src/external/xnu"
        entry = next(dict(item) for item in original if item["module"] == module)
        entry["lock"] = lock_path.name
        mapping = {
            "schema_version": 2,
            "profile": "homebrew",
            "batch_id": "authored-runtime-contract",
            "expected_count": 1,
            "series": [{key: entry[key] for key in ("profile", "module", "patch", "lock")}],
        }
        mapping_path = root / "mapping.yml"
        mapping_path.write_text(yaml.safe_dump(mapping, sort_keys=False))
        composition = {
            **original.composition,
            **{key: {module: original.composition[key][module]}
               for key in ("starts", "finals", "integration_finals")},
            "boundaries": {
                (module, entry["patch"]): original.composition["finals"][module],
            },
        }
        plan = runtime_source.patch_stack_lock_first.LockFirstPlan([entry], mapping, composition)
        host._load_profile = lambda _name: {
            "patches": [{"module": module, "path": entry["patch"]}],
        }
        assert (target / "fixture").read_text() == "base\n"
        with (
            mock.patch.object(runtime_source.patch_stack_lock_first, "plan", return_value=plan),
            mock.patch.object(runtime_source.patch_stack_lock_first, "mapping_for_profile", return_value=mapping_path),
        ):
            runtime_source.RuntimeSourceMaterializer(host)._materialize_canonical_profile(
                "homebrew", {module: target},
            )
        assert (target / "fixture").read_text() == "three\n"


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
            "batch_id": AUTHORED_BATCHES["homebrew"]["batch_id"],
            "expected_count": AUTHORED_BATCHES["homebrew"]["expected_count"],
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
            count = plan.batch["expected_count"] - len(modules) + 1 if entries[0]["module"] == "darling/src/external/installer" else 1
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
        # Valid authored fixtures must not turn the exact runtime gate into
        # a moving target: reject count, identity and order drift before replay.
        for field, value in (
            ("batch_id", plan.batch["batch_id"] + "-unapproved"),
            ("expected_count", plan.batch["expected_count"] - 1),
            ("expected_count", plan.batch["expected_count"] + 1),
            ("module_order", list(reversed(plan.batch["module_order"]))),
        ):
            original = plan.batch[field]
            plan.batch[field] = value
            messages.clear()
            calls.clear()
            try:
                must_raise(runtime_source.patch_stack_lock_first.LockFirstError,
                           lambda: materializer._materialize_canonical_profile("homebrew", targets))
                assert not calls and not messages
            finally:
                plan.batch[field] = original
        # Even an approved batch cannot report success with a short or excess
        # result set. Exercise both boundaries through the real result gate.
        for delta in (-1, 1):
            def wrong_result_count(target, entries, **kwargs):
                results, metrics = batch(target, entries, **kwargs)
                if entries[0]["module"] == modules[-1]:
                    results = results[:-1] if delta < 0 else results + [results[-1]]
                return results, metrics
            runtime_source.patch_stack_lock_first.materialize_batch_into = wrong_result_count
            messages.clear()
            calls.clear()
            must_raise(runtime_source.patch_stack_lock_first.LockFirstError,
                       lambda: materializer._materialize_canonical_profile("homebrew", targets))
            assert not any(line.startswith("PATCH_STACK_REPLAY ") for line in messages)
        runtime_source.patch_stack_lock_first.materialize_batch_into = batch
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
    authored_batch_admission_contract()
    current_minus_module_scope_contract()
    real_rollback_contract()
    identity_contract()
    profile_routing_contract()
    print("runtime-source lock-first contract: PASS")


if __name__ == "__main__":
    main()

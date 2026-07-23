#!/usr/bin/env python3
"""Lock-first is opt-in, graph-based, and leaves the legacy apply path intact."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import types
import json
from collections import OrderedDict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")
class WestCommand: pass
west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import patch as patch_command
import patch_stack_lock_first as lock_first


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def mapping_doc(series, *, profile="homebrew", batch_id="synthetic-batch", expected_count=None):
    return {"schema_version": 2, "profile": profile, "batch_id": batch_id,
            "expected_count": len(series) if expected_count is None else expected_count, "series": series}


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        bare, work, production = root / "mirror.git", root / "work", root / "production"
        git(root, "init", "--bare", "-q", str(bare)); git(root, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.name", "Test"); git(work, "config", "user.email", "test@example.invalid")
        (work / "a").write_text("base\n"); git(work, "add", "a"); git(work, "commit", "-qm", "base")
        base = git(work, "rev-parse", "HEAD")
        (work / "a").write_text("canonical\n"); git(work, "commit", "-qam", "canonical")
        source, tree = git(work, "rev-parse", "HEAD"), git(work, "rev-parse", "HEAD^{tree}")
        patch = root / "one.patch"
        patch.write_text(subprocess.run(["git", "format-patch", "--stdout", f"{base}..{source}"], cwd=work, check=True, text=True, stdout=subprocess.PIPE).stdout)
        (root / "west.lock.yml").write_text("manifest: synthetic\n")
        git(work, "tag", f"patch-stack/v1/bases/{base}", base); git(work, "tag", f"patch-stack/v1/sources/{source}", source)
        git(work, "push", "-q", "origin", "HEAD:refs/heads/main", "--tags"); git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
        git(root, "clone", "-q", str(bare), str(production))
        git(production, "config", "user.name", "Test")
        git(production, "config", "user.email", "test@example.invalid")
        git(production, "reset", "--hard", "-q", base)
        lock = {"schema_version": 2, "project": {"name": "synthetic", "path": "."},
                "upstream": {"url": bare.as_uri(), "base_commit": base},
                "mirror": {"url": bare.as_uri(), "base_ref": f"refs/tags/patch-stack/v1/bases/{base}", "base_oid": base,
                           "source_ref": f"refs/tags/patch-stack/v1/sources/{source}", "source_oid": source},
                "source_commit": source, "ordered_commits": [source], "expected_tree": tree}
        lock_path = root / "one.yml"; lock_path.write_text(yaml.safe_dump(lock, sort_keys=False))
        mapping = root / "lock-first.yml"; mapping.write_text(yaml.safe_dump(mapping_doc([{"profile": "homebrew", "module": "darling", "patch": "darling/sandbox-exec-pass-through.patch", "lock": "one.yml"}]), sort_keys=False))
        patches = [{"module": "darling", "path": "darling/sandbox-exec-pass-through.patch"}]
        selected = lock_first.plan("homebrew", patches, mapping)
        assert len(selected) == 1
        # Batch 6 is the exact grouped homebrew selection: 25 Darlingserver
        # series first, then the retained Batch 5 modules.  This binds the
        # data mapping to the real profile order before an apply can mutate.
        homebrew = yaml.safe_load((ROOT / "patches/homebrew/patches.yml").read_text())
        homebrew_patches = homebrew["patches"]
        homebrew_grouped = OrderedDict()
        for entry in homebrew_patches:
            homebrew_grouped.setdefault(entry["module"], []).append(entry)
        batch_six = lock_first.plan("homebrew", homebrew_patches, lock_first.MAPPING, homebrew_grouped)
        expected_darlingserver = [
            entry["path"] for entry in homebrew_patches
            if entry["module"] == "darling/src/external/darlingserver"
        ]
        observed_darlingserver = [
            entry["patch"] for entry in batch_six
            if entry["module"] == "darling/src/external/darlingserver"
        ]
        assert len(batch_six) == 44 and batch_six.batch["expected_count"] == 44
        assert batch_six.batch["module_order"] == [
            "darling/src/external/darlingserver",
            "darling/src/external/libplatform",
            "darling/src/external/perl",
            "darling/src/external/libressl-2.8.3",
            "darling/src/external/libpthread",
            "darling",
            "darling/src/external/installer",
        ]
        assert len(expected_darlingserver) == 25
        assert observed_darlingserver == expected_darlingserver
        # The planner validates the real apply order, not the flat YAML
        # order: _group() executes all patches of the first module before a
        # later profile entry in the next module.
        grouped_patches = [
            {"module": "external", "path": "external/first.patch"},
            {"module": "darling", "path": "darling/only.patch"},
            {"module": "external", "path": "external/second.patch"},
        ]
        grouped_execution = {
            "external": [grouped_patches[0], grouped_patches[2]],
            "darling": [grouped_patches[1]],
        }
        grouped_series = []
        for index, item in enumerate((grouped_patches[0], grouped_patches[2], grouped_patches[1])):
            name = f"grouped-{index}.yml"
            (root / name).write_text(lock_path.read_text())
            grouped_series.append({"profile": "homebrew", "module": item["module"], "patch": item["path"], "lock": name})
        grouped_mapping = root / "grouped.yml"
        grouped_mapping.write_text(yaml.safe_dump(mapping_doc(grouped_series, batch_id="grouped"), sort_keys=False))
        assert [entry["patch"] for entry in lock_first.plan("homebrew", grouped_patches, grouped_mapping, grouped_execution)] == [entry["patch"] for entry in grouped_series]
        raw_order_mapping = root / "raw-order.yml"
        raw_order_mapping.write_text(yaml.safe_dump(mapping_doc([grouped_series[0], grouped_series[2], grouped_series[1]], batch_id="raw-order"), sort_keys=False))
        try:
            lock_first.plan("homebrew", grouped_patches, raw_order_mapping, grouped_execution)
        except lock_first.LockFirstError:
            pass
        else:
            raise AssertionError("planner accepted raw profile order instead of grouped execution order")
        # Batch metadata accepts an ordered 6-series slice and fails closed
        # for empty, duplicate, missing, reordered, or incompatible entries.
        batch_patches = [{"module": "darling", "path": f"darling/p{index}.patch"} for index in range(6)]
        batch_series = []
        for index, item in enumerate(batch_patches):
            lock_name = f"one-{index}.yml"
            (root / lock_name).write_text(lock_path.read_text())
            batch_series.append({"profile": "homebrew", "module": "darling", "patch": item["path"], "lock": lock_name})
        batch_mapping = root / "batch.yml"
        batch_mapping.write_text(yaml.safe_dump(mapping_doc(batch_series, batch_id="six-series"), sort_keys=False))
        batch = lock_first.plan("homebrew", batch_patches, batch_mapping)
        assert [entry["patch"] for entry in batch] == [item["path"] for item in batch_patches]
        # v1 is only a migration input; execution accepts the explicit v2
        # shape.  The count remains data, including 1/6/12 exact batches.
        migrated = lock_first.migrate_mapping_v1({"schema_version": 1, "series": batch_series}, batch_id="migrated")
        assert migrated["schema_version"] == 2 and migrated["expected_count"] == len(batch_series)
        for exact_count in (1, 6, 12):
            exact_patches = [{"module": "darling", "path": f"darling/exact-{index}.patch"} for index in range(exact_count)]
            exact_series = []
            for index, item in enumerate(exact_patches):
                lock_name = f"exact-{exact_count}-{index}.yml"
                (root / lock_name).write_text(lock_path.read_text())
                exact_series.append({"profile": "homebrew", "module": "darling", "patch": item["path"], "lock": lock_name})
            exact_mapping = root / f"exact-{exact_count}.yml"
            exact_mapping.write_text(yaml.safe_dump(mapping_doc(exact_series, batch_id=f"exact-{exact_count}"), sort_keys=False))
            assert len(lock_first.plan("homebrew", exact_patches, exact_mapping)) == exact_count
        def must_fail(fn, *args):
            try: fn(*args)
            except lock_first.LockFirstError: return
            raise AssertionError("lock-first accepted invalid metadata")
        empty = root / "empty.yml"; empty.write_text(yaml.safe_dump(mapping_doc([], expected_count=0)))
        must_fail(lock_first.plan, "homebrew", batch_patches, empty)
        duplicate = root / "duplicate.yml"; duplicate.write_text(yaml.safe_dump(mapping_doc(batch_series + [batch_series[0]], batch_id="duplicate")))
        must_fail(lock_first.plan, "homebrew", batch_patches, duplicate)
        missing = root / "missing.yml"; missing.write_text(yaml.safe_dump(mapping_doc([dict(batch_series[0], patch="darling/missing.patch")], batch_id="missing")))
        must_fail(lock_first.plan, "homebrew", batch_patches, missing)
        reordered = root / "reordered.yml"; reordered.write_text(yaml.safe_dump(mapping_doc(list(reversed(batch_series),), batch_id="reordered")))
        must_fail(lock_first.plan, "homebrew", batch_patches, reordered)
        incompatible_lock = dict(lock); incompatible_lock["mirror"] = dict(lock["mirror"], base_oid="0" * 40)
        (root / "incompatible.yml").write_text(yaml.safe_dump(incompatible_lock, sort_keys=False))
        incompatible = root / "incompatible-map.yml"; incompatible.write_text(yaml.safe_dump(mapping_doc([dict(batch_series[0], lock="incompatible.yml")], batch_id="incompatible")))
        must_fail(lock_first.plan, "homebrew", batch_patches, incompatible)
        wrong_count = root / "wrong-count.yml"; wrong_count.write_text(yaml.safe_dump(mapping_doc(batch_series, expected_count=len(batch_series) - 1)))
        must_fail(lock_first.plan, "homebrew", batch_patches, wrong_count)
        wrong_profile = root / "wrong-profile.yml"; wrong_profile.write_text(yaml.safe_dump(mapping_doc(batch_series, profile="perf")))
        must_fail(lock_first.plan, "homebrew", batch_patches, wrong_profile)
        wrong_batch = root / "wrong-batch.yml"; wrong_batch.write_text(yaml.safe_dump(dict(mapping_doc(batch_series), batch_id="")))
        must_fail(lock_first.plan, "homebrew", batch_patches, wrong_batch)
        malformed_scalar = root / "malformed-scalar.yml"; malformed_scalar.write_text(yaml.safe_dump(mapping_doc([dict(batch_series[0], lock=None)])))
        must_fail(lock_first.plan, "homebrew", batch_patches, malformed_scalar)
        duplicate_lock = root / "duplicate-lock.yml"; duplicate_lock.write_text(yaml.safe_dump(mapping_doc([batch_series[0], dict(batch_series[1], lock=batch_series[0]["lock"])])))
        must_fail(lock_first.plan, "homebrew", batch_patches, duplicate_lock)
        linked = root / "linked"; linked.symlink_to(root, target_is_directory=True)
        intermediate_symlink = root / "intermediate-symlink.yml"; intermediate_symlink.write_text(yaml.safe_dump(mapping_doc([dict(batch_series[0], lock="linked/one.yml")])))
        must_fail(lock_first.plan, "homebrew", batch_patches, intermediate_symlink)
        evidence = root / "batch-evidence.json"
        valid_entry = {"module": "darling", "patch": "darling/p0.patch", "base": base, "source": source, "canonical_tree": tree, "applied_commit": source, "applied_tree": tree, "verdict": "VALID"}
        one_batch = {"batch_id": "one", "expected_count": 1,
                     "module_order": ["darling"],
                     "series_order": [{"module": "darling", "patch": "darling/p0.patch"}]}
        lock_first.write_batch_evidence(evidence, [valid_entry], one_batch)
        written_evidence = yaml.safe_load(evidence.read_text())
        assert written_evidence["verdict"] == "VALID" and written_evidence["evidence_schema_version"] == 2
        blocked = root / "blocked"; blocked.write_text("not a directory")
        must_fail(lock_first.write_batch_evidence, blocked / "evidence.json", [], {"batch_id": "empty", "expected_count": 0, "patches": []})
        for bad, expected in (
            ([], []),
            ([dict(valid_entry, patch="darling/extra.patch")], ["darling/p0.patch"]),
            ([dict(valid_entry, patch="darling/p0.patch"), dict(valid_entry, patch="darling/p1.patch")], ["darling/p0.patch"]),
            ([dict(valid_entry, patch="darling/p1.patch"), valid_entry], ["darling/p0.patch", "darling/p1.patch"]),
            ([dict(valid_entry, verdict="FAIL")], ["darling/p0.patch"]),
            ([dict(valid_entry, canonical_tree="BAD")], ["darling/p0.patch"]),
            ([dict(valid_entry, extra="x")], ["darling/p0.patch"]),
            ([{"patch": "darling/p0.patch"}], ["darling/p0.patch"]),
            ([dict(valid_entry, patch="darling/p0.patch")], ["darling/p0.patch", "darling/p1.patch"]),
            ([valid_entry, valid_entry], ["darling/p0.patch", "darling/p0.patch"]),
        ):
            candidate = root / f"bad-evidence-{len(list(root.glob('bad-evidence-*')))}.json"
            must_fail(lock_first.write_batch_evidence, candidate, bad, {
                "batch_id": "bad", "expected_count": len(expected),
                "module_order": ["darling"],
                "series_order": [{"module": "darling", "patch": patch} for patch in expected],
            })
            assert not candidate.exists()
        assert not list(root.glob("*.tmp"))
        assert lock_first.materialize_into(production, selected[0], patch)["canonical_tree"] == tree
        assert git(production, "rev-parse", "HEAD^{tree}") == tree
        assert not git(production, "for-each-ref", "refs/west/patch-stack-results/lock-first")
        # Existing canonical result refs are exercised by the materializer
        # contract; lock-first uses an isolated canonical repository.
        git(production, "reset", "--hard", "-q", base)
        assert lock_first.materialize_into(production, selected[0], patch)["canonical_tree"] == tree
        # Differential native-Git fixture: messages which are unsafe to
        # reserialize by string trimming must yield byte-identical history.
        differential = root / "differential"; git(root, "clone", "-q", str(bare), str(differential))
        git(differential, "config", "user.name", "Test"); git(differential, "config", "user.email", "test@example.invalid")
        git(differential, "reset", "--hard", "-q", base); fixture_base = git(differential, "rev-parse", "HEAD")
        messages = ["\nleading blank\n\ninterior blank\n", "trailing spaces   \n\n", "Unicode: café ��\n\n"]
        commits = []
        for index, message in enumerate(messages):
            (differential / f"fixture-{index}").write_text(message, encoding="utf-8")
            message_path = differential / "message"; message_path.write_text(message, encoding="utf-8")
            git(differential, "add", f"fixture-{index}")
            subprocess.run(["git", "commit", "--cleanup=verbatim", "-q", "-F", str(message_path)], cwd=differential, check=True)
            commits.append(git(differential, "rev-parse", "HEAD"))
        raw_sources = [subprocess.check_output(["git", "cat-file", "commit", commit], cwd=differential) for commit in commits]
        assert b"\n\nleading blank\n\ninterior blank\n" in raw_sources[0]
        assert b"trailing spaces   \n\n" in raw_sources[1]
        assert "Unicode: café ��\n\n".encode() in raw_sources[2]
        legacy, canonical = root / "legacy", root / "canonical"
        git(root, "clone", "-q", str(differential), str(legacy)); git(root, "clone", "-q", str(differential), str(canonical))
        for repo in (legacy, canonical):
            git(repo, "config", "user.name", "Test"); git(repo, "config", "user.email", "test@example.invalid"); git(repo, "reset", "--hard", "-q", fixture_base)
        mbox = subprocess.check_output(["git", "format-patch", "--stdout", "--no-stat", "--full-index", f"{fixture_base}..{commits[-1]}"], cwd=differential)
        (root / "fixture.mbox").write_bytes(mbox)
        subprocess.run(["git", "am", "--3way", "--committer-date-is-author-date", str(root / "fixture.mbox")], cwd=legacy, check=True)
        for commit in commits: lock_first._cherry_pick(canonical, commit)
        assert git(legacy, "rev-list", "--reverse", f"{fixture_base}..HEAD") == git(canonical, "rev-list", "--reverse", f"{fixture_base}..HEAD")
        assert git(legacy, "show", "-s", "--format=raw", "HEAD") == git(canonical, "show", "-s", "--format=raw", "HEAD")
        # Exercise the real lock-first replay through _apply(), rather than
        # treating a series-level mock as proof of a mid-series failure.  The
        # first two immutable commits must reach the production repository;
        # the third then raises the injected original exception.  _apply()
        # owns the rollback of that partially replayed integration branch.
        git(work, "reset", "--hard", "-q", base)
        multi_commits = []
        for index in range(1, 4):
            (work / f"replay-{index}").write_text(f"replay {index}\n")
            git(work, "add", f"replay-{index}")
            git(work, "commit", "-qm", f"replay {index}")
            multi_commits.append(git(work, "rev-parse", "HEAD"))
        multi_source, multi_tree = multi_commits[-1], git(work, "rev-parse", "HEAD^{tree}")
        git(work, "tag", f"patch-stack/v1/sources/{multi_source}", multi_source)
        # The synthetic stack deliberately forks from the original one-commit
        # fixture, so only its immutable source tag is needed by the mirror.
        git(work, "push", "-q", "origin", "--tags")
        multi_patch = root / "multi.patch"
        multi_patch.write_text(subprocess.run(
            ["git", "format-patch", "--stdout", f"{base}..{multi_source}"],
            cwd=work, check=True, text=True, stdout=subprocess.PIPE,
        ).stdout)
        multi_lock = {
            "schema_version": 2, "project": {"name": "synthetic", "path": "."},
            "upstream": {"url": bare.as_uri(), "base_commit": base},
            "mirror": {
                "url": bare.as_uri(),
                "base_ref": f"refs/tags/patch-stack/v1/bases/{base}", "base_oid": base,
                "source_ref": f"refs/tags/patch-stack/v1/sources/{multi_source}", "source_oid": multi_source,
            },
            "source_commit": multi_source, "ordered_commits": multi_commits,
            "expected_tree": multi_tree,
        }
        multi_lock_path = root / "multi.yml"
        multi_lock_path.write_text(yaml.safe_dump(multi_lock, sort_keys=False))
        multi_mapping = root / "multi-map.yml"
        multi_patches = [{"module": "darling/src/external/darlingserver", "path": "darlingserver/test-diagnostics-trace.patch"}]
        multi_mapping.write_text(yaml.safe_dump(mapping_doc([
            {"profile": "homebrew", "module": "darling/src/external/darlingserver", "patch": "darlingserver/test-diagnostics-trace.patch", "lock": "multi.yml"},
        ], batch_id="real-three-commit"), sort_keys=False))
        real_command = patch_command.DarlingPatch.__new__(patch_command.DarlingPatch)
        real_command._base_profile = None
        real_command.manifest = types.SimpleNamespace(repo_abspath=root)
        real_command._group = lambda _patches: {"darling/src/external/darlingserver": multi_patches}
        real_command._require_base_applied = lambda _modules: None
        real_command._ensure_generated_context = lambda *_args: None
        real_command._base_revision = lambda _module: base
        real_command._repo = lambda _module: production
        real_command._reset_submodule_index = lambda _repo: None
        real_command._verify_patch = lambda _profile_dir, _patch: multi_patch
        real_command._record_integration = lambda *_args: (_ for _ in ()).throw(AssertionError("record must not run"))
        real_command.inf = lambda _message: None
        real_command.die = lambda message, **_kwargs: (_ for _ in ()).throw(RuntimeError(message))
        old_mapping, old_cherry_pick = lock_first.MAPPING, lock_first._cherry_pick
        temporary_root = Path(tempfile.gettempdir())
        patterns = ("west-lock-materialize-*", "west-patch-lock-first-*", "west-patch-shadow-*")
        try:
            lock_first.MAPPING = multi_mapping
            for injected in (lock_first.LockFirstError("third replay failure"), KeyboardInterrupt()):
                git(production, "reset", "--hard", "-q", base)
                subprocess.run(["git", "branch", "-D", "integration/homebrew"], cwd=production, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                before_roots = {path.resolve() for pattern in patterns for path in temporary_root.glob(pattern)}
                replayed = []
                def fail_third(repo, commit):
                    if repo == production:
                        replayed.append(commit)
                        if commit == multi_commits[2]:
                            raise injected
                    return old_cherry_pick(repo, commit)
                lock_first._cherry_pick = fail_third
                evidence_path = root / f"real-three-{type(injected).__name__}.json"
                try:
                    real_command._apply("homebrew", root, multi_patches, "0", False, False, None, True, str(evidence_path))
                except KeyboardInterrupt:
                    assert isinstance(injected, KeyboardInterrupt)
                except RuntimeError as error:
                    assert isinstance(injected, lock_first.LockFirstError)
                    assert str(error) == "third replay failure", error
                else:
                    raise AssertionError("third replay failure was accepted")
                assert replayed == multi_commits
                assert git(production, "rev-parse", "HEAD") == base
                assert git(production, "rev-parse", "HEAD^{tree}") == git(work, "rev-parse", f"{base}^{{tree}}")
                assert not (production / git(production, "rev-parse", "--git-path", "rebase-apply")).exists()
                refs = git(production, "for-each-ref", "--format=%(refname)")
                assert "refs/west/patch-stack-materialize/" not in refs
                assert "refs/west/patch-stack-lock-first/" not in refs
                assert "refs/west/patch-stack-results/" not in refs
                assert not evidence_path.exists()
                assert {path.resolve() for pattern in patterns for path in temporary_root.glob(pattern)} == before_roots
                assert "west-lock-materialize-" not in git(production, "worktree", "list", "--porcelain")
        finally:
            lock_first.MAPPING, lock_first._cherry_pick = old_mapping, old_cherry_pick
        # Production orchestration: no flag never invokes canonical code; the
        # typed plan is built before _prepare and failures roll back/SIGINT.
        command = patch_command.DarlingPatch.__new__(patch_command.DarlingPatch)
        command._base_profile = None; command.manifest = types.SimpleNamespace(repo_abspath=root)
        command._group = lambda _patches: {"darling": patches}; command._require_base_applied = lambda _modules: None
        command._ensure_generated_context = lambda *_args: None; command._repo = lambda _module: production
        prepared: list[bool] = []
        command._prepare = lambda *_args, **_kwargs: prepared.append(True)
        command._reset_submodule_index = lambda _repo: None; command._verify_patch = lambda *_args: patch
        command._record_integration = lambda *_args: root / "generated.lock"; command._abort_am = lambda _repo: None
        resets: list[bool] = []; command._reset = lambda *_args, **_kwargs: resets.append(True)
        command.inf = lambda _message: None; command.die = lambda message, **_kwargs: (_ for _ in ()).throw(RuntimeError(message))
        old_plan, old_into = patch_command.patch_stack_lock_first.plan, patch_command.patch_stack_lock_first.materialize_into
        old_writer = patch_command.patch_stack_lock_first.write_batch_evidence
        calls: list[object] = []
        patch_command.patch_stack_lock_first.plan = lambda *_args: selected
        patch_command.patch_stack_lock_first.materialize_into = lambda _repo, entry, *_args: calls.append(entry["patch"]) or {"module": entry["module"], "patch": entry["patch"], "base": base, "source": source, "canonical_tree": tree, "applied_commit": source, "applied_tree": tree, "verdict": "VALID"}
        try:
            existing_evidence = root / "existing-evidence.json"; existing_evidence.write_text("old\n")
            try: command._apply("homebrew", root, patches, "0", False, False, None, True, str(existing_evidence))
            except RuntimeError: pass
            else: raise AssertionError("pre-existing evidence was accepted")
            assert not prepared and existing_evidence.read_text() == "old\n"
            existing_evidence.unlink()
            command._apply("homebrew", root, patches, "0", False, False, None, False)
            assert not calls, "normal no-flag apply invoked lock-first"
            command._apply("homebrew", root, patches, "0", False, False, None, True)
            assert calls == ["darling/sandbox-exec-pass-through.patch"]
            # Every selected series is run once in profile order. Failure and
            # SIGINT in the middle both reset the entire touched transaction.
            command._group = lambda _patches: {"darling": batch_patches}
            patch_command.patch_stack_lock_first.plan = lambda *_args: batch
            calls.clear(); resets.clear()
            batch_output = root / "orchestration-evidence.json"
            command._apply("homebrew", root, batch_patches, "0", False, False, None, True, str(batch_output))
            assert calls == [item["path"] for item in batch_patches]
            payload = json.loads(batch_output.read_text())
            assert payload["expected_count"] == len(batch_patches) and len(payload["series"]) == len(batch_patches)
            # Integration recording happens before evidence publication. Both
            # failure paths roll back and leave no successful evidence behind.
            resets.clear()
            record_output = root / "record-failure-evidence.json"
            writer_calls: list[bool] = []
            command._record_integration = lambda *_args: (_ for _ in ()).throw(RuntimeError("record failure"))
            patch_command.patch_stack_lock_first.write_batch_evidence = lambda *_args: writer_calls.append(True)
            try: command._apply("homebrew", root, batch_patches, "0", False, False, None, True, str(record_output))
            except RuntimeError: pass
            else: raise AssertionError("record failure was accepted")
            assert not writer_calls and resets and not record_output.exists()
            resets.clear()
            writer_output = root / "writer-failure-evidence.json"
            command._record_integration = lambda *_args: root / "generated.lock"
            patch_command.patch_stack_lock_first.write_batch_evidence = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("writer failure"))
            try: command._apply("homebrew", root, batch_patches, "0", False, False, None, True, str(writer_output))
            except RuntimeError: pass
            else: raise AssertionError("writer failure was accepted")
            assert resets and not writer_output.exists()
            patch_command.patch_stack_lock_first.write_batch_evidence = old_writer
            # Evidence publication accepts only the typed planner result; a
            # list with equivalent entries is not metadata.
            resets.clear(); plain_output = root / "plain-plan-evidence.json"
            patch_command.patch_stack_lock_first.plan = lambda *_args: list(batch)
            patch_command.patch_stack_lock_first.materialize_into = lambda _repo, entry, *_args: {"module": entry["module"], "patch": entry["patch"], "base": base, "source": source, "canonical_tree": tree, "applied_commit": source, "applied_tree": tree, "verdict": "VALID"}
            try: command._apply("homebrew", root, batch_patches, "0", False, False, None, True, str(plain_output))
            except RuntimeError: pass
            else: raise AssertionError("untyped lock-first plan was accepted")
            assert resets and not plain_output.exists()
            patch_command.patch_stack_lock_first.plan = lambda *_args: batch
            for position in (1, (len(batch_patches) + 1) // 2, len(batch_patches)):
                resets.clear(); interrupted = {"count": 0}
                def interrupting(_repo, entry, *_args):
                    interrupted["count"] += 1
                    if interrupted["count"] == position: raise KeyboardInterrupt()
                    return {"module": entry["module"], "patch": entry["patch"], "base": base, "source": source, "canonical_tree": tree, "applied_commit": source, "applied_tree": tree, "verdict": "VALID"}
                patch_command.patch_stack_lock_first.materialize_into = interrupting
                try: command._apply("homebrew", root, batch_patches, "0", False, False, None, True)
                except KeyboardInterrupt: pass
                else: raise AssertionError("SIGINT was swallowed")
                assert interrupted["count"] == position and resets
                resets.clear(); failure = {"count": 0}
                def failing_at(_repo, planned_entry, *_args):
                    failure["count"] += 1
                    if failure["count"] == position: raise lock_first.LockFirstError("series failure")
                    return {"module": planned_entry["module"], "patch": planned_entry["patch"], "base": base, "source": source, "canonical_tree": tree, "applied_commit": source, "applied_tree": tree, "verdict": "VALID"}
                patch_command.patch_stack_lock_first.materialize_into = failing_at
                try: command._apply("homebrew", root, batch_patches, "0", False, False, None, True)
                except RuntimeError: pass
                else: raise AssertionError("series failure was accepted")
                assert failure["count"] == position and resets
            # Multi-module failure handling is keyed by the real grouped
            # execution order. Batch 5 places libpthread before Darling and
            # both ordered Installer series after it. Model immutable commit
            # replay inside each series, so the final case is genuinely the
            # third commit in archive-path-containment, not merely a failure
            # before that series starts. Every case must reset the touched
            # transaction without publishing aggregate evidence.
            external_patches = [
                {"module": "darling/src/external/libplatform", "path": "libplatform/bzero-return-register.patch"},
                {"module": "darling/src/external/perl", "path": "perl/disable-nsgetexecutablepath.patch"},
                {"module": "darling/src/external/libressl-2.8.3", "path": "libressl/libressl-283-nist-strict-aliasing.patch"},
                {"module": "darling/src/external/libpthread", "path": "libpthread/psynch-kernel-return-helper.patch"},
                {"module": "darling", "path": "darling/p0.patch"},
                {"module": "darling/src/external/installer", "path": "installer/normalize-payload-paths.patch"},
                {"module": "darling/src/external/installer", "path": "installer/archive-path-containment.patch"},
            ]
            external_grouped = {
                external_patches[0]["module"]: [external_patches[0]],
                external_patches[1]["module"]: [external_patches[1]],
                external_patches[2]["module"]: [external_patches[2]],
                external_patches[3]["module"]: [external_patches[3]],
                "darling": [external_patches[4]],
                external_patches[5]["module"]: [external_patches[5], external_patches[6]],
            }
            external_plan = lock_first.LockFirstPlan(
                [{"profile": "homebrew", "module": item["module"], "patch": item["path"], "lock": "unused.yml", "lock_path": str(lock_path)} for item in external_patches],
                {"batch_id": "external-pilot", "expected_count": len(external_patches)},
            )
            repositories = {module: root / f"repo-{index}" for index, module in enumerate(external_grouped)}
            command._group = lambda _patches: external_grouped
            command._repo = lambda module: repositories[module]
            command._prepare = lambda *_args, **_kwargs: None
            patch_command.patch_stack_lock_first.plan = lambda *_args: external_plan
            scenarios = (
                ("first-new", "libpthread/psynch-kernel-return-helper.patch", 1),
                ("between-installer-series", "installer/archive-path-containment.patch", 1),
                ("last-installer-commit", "installer/archive-path-containment.patch", 3),
            )
            commit_counts = {
                "libpthread/psynch-kernel-return-helper.patch": 3,
                "installer/archive-path-containment.patch": 3,
            }
            for name, failing_patch, failing_commit in scenarios:
                for interrupt in (False, True):
                    attempts, aborted, replayed = {"count": 0}, [], []
                    command._abort_am = lambda repo: aborted.append(repo)
                    resets.clear()
                    evidence_path = root / f"external-{name}-{interrupt}.json"
                    def fail_external(_repo, _entry, *_args):
                        attempts["count"] += 1
                        for commit_index in range(1, commit_counts.get(_entry["patch"], 1) + 1):
                            replayed.append((_entry["patch"], commit_index))
                            if _entry["patch"] == failing_patch and commit_index == failing_commit:
                                if interrupt:
                                    raise KeyboardInterrupt()
                                raise lock_first.LockFirstError("external commit replay failure")
                        return {"module": _entry["module"], "patch": _entry["patch"], "base": base, "source": source, "canonical_tree": tree, "applied_commit": source, "applied_tree": tree, "verdict": "VALID"}
                    patch_command.patch_stack_lock_first.materialize_into = fail_external
                    try:
                        command._apply("homebrew", root, external_patches, "0", False, False, None, True, str(evidence_path))
                    except KeyboardInterrupt:
                        assert interrupt
                    except RuntimeError:
                        assert not interrupt
                    else:
                        raise AssertionError("external failure was accepted")
                    assert resets and not evidence_path.exists()
                    failing_position = next(index for index, item in enumerate(external_patches) if item["path"] == failing_patch)
                    expected_aborted = list(dict.fromkeys(repositories[item["module"]] for item in external_patches[:failing_position + 1]))
                    assert aborted == expected_aborted
                    assert replayed[-1] == (failing_patch, failing_commit)
            # The Batch 6 Darlingserver portion is a single module batch. The
            # existing real three-commit fixture above proves commit-level
            # rollback; this orchestration-only fixture proves the first,
            # middle and final actual profile entries take the normal _apply
            # rollback path without claiming that the mock replays commits.
            darlingserver_patches = [
                {"module": "darling/src/external/darlingserver", "path": path}
                for path in expected_darlingserver
            ]
            darlingserver_grouped = {"darling/src/external/darlingserver": darlingserver_patches}
            darlingserver_plan = lock_first.LockFirstPlan(
                [{"profile": "homebrew", "module": item["module"], "patch": item["path"], "lock": "unused.yml", "lock_path": str(lock_path)} for item in darlingserver_patches],
                {"batch_id": "darlingserver-batch", "expected_count": len(darlingserver_patches)},
            )
            command._group = lambda _patches: darlingserver_grouped
            command._repo = lambda _module: production
            command._prepare = lambda *_args, **_kwargs: None
            patch_command.patch_stack_lock_first.plan = lambda *_args: darlingserver_plan
            for position in (1, (len(darlingserver_patches) + 1) // 2, len(darlingserver_patches)):
                for interrupt in (False, True):
                    attempts, aborted = {"count": 0}, []
                    command._abort_am = lambda repo: aborted.append(repo)
                    resets.clear()
                    evidence_path = root / f"darlingserver-{position}-{interrupt}.json"
                    def fail_darlingserver(_repo, entry, *_args):
                        attempts["count"] += 1
                        if attempts["count"] == position:
                            if interrupt:
                                raise KeyboardInterrupt()
                            raise lock_first.LockFirstError("Darlingserver series failure")
                        return {"module": entry["module"], "patch": entry["patch"], "base": base, "source": source, "canonical_tree": tree, "applied_commit": source, "applied_tree": tree, "verdict": "VALID"}
                    patch_command.patch_stack_lock_first.materialize_into = fail_darlingserver
                    try:
                        command._apply("homebrew", root, darlingserver_patches, "0", False, False, None, True, str(evidence_path))
                    except KeyboardInterrupt:
                        assert interrupt
                    except RuntimeError:
                        assert not interrupt
                    else:
                        raise AssertionError("Darlingserver failure was accepted")
                    assert attempts["count"] == position and resets and not evidence_path.exists()
                    assert aborted == [production]
            prepared.clear()
            patch_command.patch_stack_lock_first.plan = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("bad mapping"))
            try: command._apply("homebrew", root, patches, "0", False, False, None, True)
            except RuntimeError: pass
            else: raise AssertionError("bad typed plan mutated")
            assert not prepared
            command._group = lambda _patches: {"darling": patches}
            patch_command.patch_stack_lock_first.plan = lambda *_args: selected
            patch_command.patch_stack_lock_first.materialize_into = lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt())
            try: command._apply("homebrew", root, patches, "0", False, False, None, True)
            except KeyboardInterrupt: pass
            else: raise AssertionError("SIGINT swallowed")
            assert resets
        finally:
            patch_command.patch_stack_lock_first.plan, patch_command.patch_stack_lock_first.materialize_into = old_plan, old_into
            patch_command.patch_stack_lock_first.write_batch_evidence = old_writer
    print("patch-stack lock-first contract: PASS")


if __name__ == "__main__":
    main()

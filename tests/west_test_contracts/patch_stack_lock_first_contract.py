#!/usr/bin/env python3
"""Lock-first is opt-in, graph-based, and leaves the legacy apply path intact."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import types
import json
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
            # execution order.  The three external pilot entries run before
            # Darling, so failure/SIGINT at each external position must abort
            # every repository touched up to that point and reset the entire
            # profile transaction without publishing aggregate evidence.
            external_patches = [
                {"module": "darling/src/external/libplatform", "path": "libplatform/bzero-return-register.patch"},
                {"module": "darling/src/external/perl", "path": "perl/disable-nsgetexecutablepath.patch"},
                {"module": "darling/src/external/libressl-2.8.3", "path": "libressl/libressl-283-nist-strict-aliasing.patch"},
                {"module": "darling", "path": "darling/p0.patch"},
            ]
            external_grouped = {
                external_patches[0]["module"]: [external_patches[0]],
                external_patches[1]["module"]: [external_patches[1]],
                external_patches[2]["module"]: [external_patches[2]],
                "darling": [external_patches[3]],
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
            for position in (1, 2, 3):
                for interrupt in (False, True):
                    attempts, aborted = {"count": 0}, []
                    command._abort_am = lambda repo: aborted.append(repo)
                    resets.clear()
                    evidence_path = root / f"external-{position}-{interrupt}.json"
                    def fail_external(_repo, _entry, *_args):
                        attempts["count"] += 1
                        if attempts["count"] == position:
                            if interrupt:
                                raise KeyboardInterrupt()
                            raise lock_first.LockFirstError("external series failure")
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
                    assert attempts["count"] == position and resets and not evidence_path.exists()
                    assert aborted == [repositories[item["module"]] for item in external_patches[:position]]
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

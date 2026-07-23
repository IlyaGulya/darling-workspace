#!/usr/bin/env python3
"""CLI-mode contract for the homebrew lock-first default cutover."""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import patch as patch_command
import patch_stack_lock_first as lock_first


def fail(message: str, **_kwargs) -> None:
    raise RuntimeError(message)


def expect_failure(fn, text: str) -> None:
    try:
        fn()
    except RuntimeError as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError("expected fail-closed rejection")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "west.lock.yml").write_text("manifest: frozen\n")
        patch_path = root / "one.patch"
        patch_path.write_text("From placeholder\n")
        patches = [{"module": "darling", "path": "darling/one.patch"}]
        plan = lock_first.LockFirstPlan(
            [{"profile": "homebrew", "module": "darling", "patch": "darling/one.patch", "lock": "one.yml", "lock_path": str(root / "one.yml")}],
            {"batch_id": "cutover", "expected_count": 1,
             "series_order": [{"module": "darling", "patch": "darling/one.patch"}],
             "module_order": ["darling"]},
        )
        command = patch_command.DarlingPatch.__new__(patch_command.DarlingPatch)
        command.manifest = types.SimpleNamespace(repo_abspath=root)
        command._base_profile = None
        command._group = lambda _patches: {"darling": patches}
        command._require_base_applied = lambda _modules: None
        command._ensure_generated_context = lambda *_args: None
        command._repo = lambda _module: root
        command._reset_submodule_index = lambda _repo: None
        command._verify_patch = lambda *_args: patch_path
        command._record_integration = lambda *_args: root / "patches/homebrew/west.lock.yml"
        command._abort_am = lambda _repo: None
        command.inf = lambda _message: None
        command.die = fail

        prepared: list[str] = []
        resets: list[bool] = []
        command._prepare = lambda module, *_args, **_kwargs: prepared.append(module)
        command._reset = lambda *_args, **_kwargs: resets.append(True)

        canonical: list[str] = []
        legacy: list[tuple[str, ...]] = []
        shadows: list[str] = []
        old_plan = patch_command.patch_stack_lock_first.plan
        old_into = patch_command.patch_stack_lock_first.materialize_into
        old_batch = patch_command.patch_stack_lock_first.materialize_batch_into
        old_writer = patch_command.patch_stack_lock_first.write_batch_evidence
        old_git_am = patch_command.git_for_patch_application
        old_shadow_plan = patch_command.patch_stack_shadow.plan
        old_shadow = patch_command.patch_stack_shadow.run_shadow
        try:
            patch_command.patch_stack_lock_first.plan = lambda *_args: plan
            patch_command.patch_stack_lock_first.materialize_into = (
                lambda _repo, entry, *_args: canonical.append(entry["patch"]) or {
                    "module": entry["module"], "patch": entry["patch"],
                    "base": "a" * 40, "source": "b" * 40,
                    "canonical_tree": "c" * 40, "applied_commit": "d" * 40,
                    "applied_tree": "e" * 40, "verdict": "VALID",
                }
            )
            patch_command.patch_stack_lock_first.materialize_batch_into = lambda repo, entries: (
                [patch_command.patch_stack_lock_first.materialize_into(repo, entry) for entry in entries],
                {"immutable_fetch_transactions": 1, "temporary_contexts": 1, "validated_locks": len(entries), "replayed_commits": len(entries)},
            )
            patch_command.git_for_patch_application = lambda _repo, *args: legacy.append(args)
            patch_command.patch_stack_shadow.plan = lambda *_args: {"module": "darling", "patch": "darling/one.patch"}
            patch_command.patch_stack_shadow.run_shadow = lambda **_kwargs: shadows.append("run") or {
                "legacy_resulting_tree": "c" * 40, "canonical_resulting_tree": "c" * 40,
            }

            # Homebrew default and the retained explicit alias are precisely
            # the same canonical path; neither calls legacy git am.
            command._apply("homebrew", root, patches, "0", False)
            assert canonical == ["darling/one.patch"] and not legacy
            canonical.clear(); prepared.clear()
            command._apply("homebrew", root, patches, "0", False, lock_first=True)
            assert canonical == ["darling/one.patch"] and not legacy

            # The fallback is explicit and uses only the retained git-am path.
            canonical.clear(); prepared.clear()
            command._apply("homebrew", root, patches, "0", False, legacy_mbox=True)
            assert not canonical and len(legacy) == 1 and prepared == ["darling"]

            # Shadow retains its established diagnostic/legacy behavior.
            legacy.clear(); shadows.clear(); canonical.clear()
            command._apply("homebrew", root, patches, "0", False, shadow_lock=True)
            assert not canonical and len(legacy) == 1 and shadows == ["run"]

            # Other profiles remain legacy by default. --legacy-mbox is an
            # explicit no-op spelling of that mode, avoiding ambiguous state.
            legacy.clear(); canonical.clear()
            command._apply("perf", root, patches, "0", False)
            command._apply("perf", root, patches, "0", False, legacy_mbox=True)
            assert len(legacy) == 2 and not canonical

            # Invalid combinations reject before plan construction, prepare,
            # fetch, branch, or worktree mutation.
            for kwargs in (
                {"legacy_mbox": True, "lock_first": True},
                {"legacy_mbox": True, "lock_first_evidence": str(root / "evidence.json")},
                {"legacy_mbox": True, "shadow_lock": True},
                {"legacy_mbox": True, "shadow_evidence": str(root / "shadow.json")},
            ):
                prepared.clear()
                expect_failure(lambda kwargs=kwargs: command._apply("homebrew", root, patches, "0", False, **kwargs), "--legacy-mbox")
                assert not prepared

            # A corrupt/incomplete default mapping cannot fall back to legacy.
            prepared.clear(); canonical.clear(); legacy.clear()
            patch_command.patch_stack_lock_first.plan = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("mapping incomplete"))
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", False), "mapping incomplete")
            assert not prepared and not canonical and not legacy

            # Default mode permits optional explicit evidence, but an existing
            # regular file/symlink is rejected before prepare. This is the same
            # fail-closed rule as the previous explicit opt-in form.
            patch_command.patch_stack_lock_first.plan = lambda *_args: plan
            occupied = root / "occupied.json"; occupied.write_text("old\n")
            prepared.clear()
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", False, lock_first_evidence=str(occupied)), "new regular output")
            assert not prepared and occupied.read_text() == "old\n"
            occupied.unlink()
            symlink = root / "evidence-link.json"; symlink.symlink_to(root / "target.json")
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", False, lock_first_evidence=str(symlink)), "new regular output")

            # Canonical failures and interrupts use the existing forced reset
            # lifecycle, while legacy fallback preserves the prior lifecycle.
            resets.clear()
            patch_command.patch_stack_lock_first.materialize_into = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("replay failure"))
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", False), "replay failure")
            assert resets
            resets.clear()
            patch_command.patch_stack_lock_first.materialize_into = lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt())
            try:
                command._apply("homebrew", root, patches, "0", False)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("default canonical SIGINT was swallowed")
            assert resets

            # `_record_integration()` writes the generated profile lock before
            # explicit evidence publication. A later writer failure must roll
            # back both Git branches and that manifest artifact, while keeping
            # the original writer error visible.
            generated = root / "patches/homebrew/west.lock.yml"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("previous generated lock\n")
            resets.clear()
            patch_command.patch_stack_lock_first.materialize_into = (
                lambda _repo, entry, *_args: {
                    "module": entry["module"], "patch": entry["patch"],
                    "base": "a" * 40, "source": "b" * 40,
                    "canonical_tree": "c" * 40, "applied_commit": "d" * 40,
                    "applied_tree": "e" * 40, "verdict": "VALID",
                }
            )
            command._record_integration = lambda *_args: (generated.parent.mkdir(parents=True, exist_ok=True), generated.write_text("partial\n"), generated)[-1]
            patch_command.patch_stack_lock_first.write_batch_evidence = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("evidence failure"))
            evidence = root / "new-evidence.json"
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", False, lock_first_evidence=str(evidence)), "evidence failure")
            assert resets and generated.read_text() == "previous generated lock\n" and not evidence.exists()
        finally:
            patch_command.patch_stack_lock_first.plan = old_plan
            patch_command.patch_stack_lock_first.materialize_into = old_into
            patch_command.patch_stack_lock_first.materialize_batch_into = old_batch
            patch_command.patch_stack_lock_first.write_batch_evidence = old_writer
            patch_command.git_for_patch_application = old_git_am
            patch_command.patch_stack_shadow.plan = old_shadow_plan
            patch_command.patch_stack_shadow.run_shadow = old_shadow
    print("patch-stack default-cutover contract: PASS")


if __name__ == "__main__":
    main()

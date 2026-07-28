#!/usr/bin/env python3
"""CLI-mode contract for the all-profile lock-first default cutover."""
from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import sys
import tempfile
import types
import re
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
    parser = argparse.ArgumentParser()
    command_parsers = parser.add_subparsers(dest="command", required=True)
    parser_command = patch_command.DarlingPatch.__new__(
        patch_command.DarlingPatch
    )
    parser_command.name = "patch"
    parser_command.description = "patch contract"
    parser_command.do_add_parser(command_parsers)
    normal = parser.parse_args(["patch", "apply", "--profile", "homebrew"])
    compatible = parser.parse_args(
        ["patch", "apply", "--profile", "homebrew", "--roll-back"]
    )
    assert normal.roll_back is False and compatible.roll_back is True
    help_output = io.StringIO()
    with contextlib.redirect_stdout(help_output):
        try:
            parser.parse_args(["patch", "apply", "--help"])
        except SystemExit as error:
            assert error.code == 0
        else:
            raise AssertionError("argparse help did not exit")
    assert "deprecated compatibility no-op" in help_output.getvalue()
    assert "roll_back" not in inspect.signature(
        patch_command.DarlingPatch._apply
    ).parameters

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "west.lock.yml").write_text("manifest: frozen\n")
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
        command._ensure_composition_prerequisites = lambda *_args: None
        command._ensure_generated_context = lambda *_args: None
        command._repo = lambda _module: root
        command._reset_submodule_index = lambda _repo: None
        command._record_integration = lambda *_args: root / "patches/homebrew/west.lock.yml"
        command._abort_am = lambda _repo: None
        messages: list[str] = []
        command.inf = messages.append
        command.die = fail

        prepared: list[str] = []
        resets: list[bool] = []
        command._prepare = lambda module, *_args, **_kwargs: prepared.append(module)
        command._reset = lambda *_args, **_kwargs: resets.append(True)

        canonical: list[str] = []
        old_plan = patch_command.patch_stack_lock_first.plan
        old_into = patch_command.patch_stack_lock_first.materialize_into
        old_batch = patch_command.patch_stack_lock_first.materialize_batch_into
        old_writer = patch_command.patch_stack_lock_first.write_batch_evidence
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
            patch_command.patch_stack_lock_first.materialize_batch_into = lambda repo, entries, **_kwargs: (
                [patch_command.patch_stack_lock_first.materialize_into(repo, entry) for entry in entries],
                {"immutable_fetch_transactions": 1, "temporary_contexts": 1, "validated_locks": len(entries), "replayed_commits": len(entries)},
            )
            # Homebrew default and the retained explicit alias are precisely
            # the same canonical path.
            command._apply("homebrew", root, patches, "0")
            assert canonical == ["darling/one.patch"]
            assert "PATCH_STACK_MODE=default-lock-first" in messages
            success = next(message for message in messages if message.startswith("PATCH_STACK_REPLAY "))
            assert re.fullmatch(r"PATCH_STACK_REPLAY profile=homebrew batch_id=cutover expected_series=1 applied_series=1 module_count=1 elapsed_replay_seconds=\d+\.\d{3} verdict=VALID", success)
            canonical.clear(); prepared.clear(); messages.clear()
            command._apply("homebrew", root, patches, "0", lock_first=True)
            assert canonical == ["darling/one.patch"]
            assert "PATCH_STACK_MODE=explicit-lock-first" in messages

            # Every configured production profile is canonical by default;
            # there is no archive-backed mode or silent fallback.
            canonical.clear(); messages.clear()
            command._apply("perf", root, patches, "0")
            command._apply("arch", root, patches, "0")
            assert canonical == ["darling/one.patch", "darling/one.patch"]
            assert messages.count("PATCH_STACK_MODE=default-lock-first") == 2

            # A corrupt/incomplete default mapping cannot fall back to legacy.
            prepared.clear(); canonical.clear(); messages.clear()
            patch_command.patch_stack_lock_first.plan = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("mapping incomplete"))
            expect_failure(lambda: command._apply("homebrew", root, patches, "0"), "mapping incomplete")
            assert not prepared and not canonical
            assert not any(message.startswith("PATCH_STACK_REPLAY ") for message in messages)

            # Default mode permits optional explicit evidence, but an existing
            # regular file/symlink is rejected before prepare. This is the same
            # fail-closed rule as the previous explicit opt-in form.
            patch_command.patch_stack_lock_first.plan = lambda *_args: plan
            occupied = root / "occupied.json"; occupied.write_text("old\n")
            prepared.clear()
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", lock_first_evidence=str(occupied)), "new regular output")
            assert not prepared and occupied.read_text() == "old\n"
            occupied.unlink()
            symlink = root / "evidence-link.json"; symlink.symlink_to(root / "target.json")
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", lock_first_evidence=str(symlink)), "new regular output")

            # Canonical failures and interrupts use the existing forced reset
            # lifecycle.
            resets.clear()
            patch_command.patch_stack_lock_first.materialize_into = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("replay failure"))
            expect_failure(lambda: command._apply("homebrew", root, patches, "0"), "replay failure")
            assert resets
            resets.clear()
            patch_command.patch_stack_lock_first.materialize_into = lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt())
            try:
                command._apply("homebrew", root, patches, "0")
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
            expect_failure(lambda: command._apply("homebrew", root, patches, "0", lock_first_evidence=str(evidence)), "evidence failure")
            assert resets and generated.read_text() == "previous generated lock\n" and not evidence.exists()
        finally:
            patch_command.patch_stack_lock_first.plan = old_plan
            patch_command.patch_stack_lock_first.materialize_into = old_into
            patch_command.patch_stack_lock_first.materialize_batch_into = old_batch
            patch_command.patch_stack_lock_first.write_batch_evidence = old_writer
        source = (ROOT / "west_commands" / "patch.py").read_text()
        assert "--legacy-mbox" not in source
        assert "--shadow-lock" not in source
        assert "--shadow-evidence" not in source
        assert source.count('"--roll-back"') == 1
        apply_dispatch = source.split('elif args.action == "apply":', 1)[1].split(
            "\n        else:", 1
        )[0]
        assert "args.roll_back" not in apply_dispatch
        assert "git_for_patch_application" not in source
    print("patch-stack default-cutover contract: PASS")


if __name__ == "__main__":
    main()

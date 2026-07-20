#!/usr/bin/env python3
"""Lock-first is opt-in, graph-based, and leaves the legacy apply path intact."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import types
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
        mapping = root / "lock-first.yml"; mapping.write_text(yaml.safe_dump({"schema_version": 1, "series": [{"profile": "homebrew", "module": "darling", "patch": "darling/sandbox-exec-pass-through.patch", "lock": "one.yml"}]}, sort_keys=False))
        patches = [{"module": "darling", "path": "darling/sandbox-exec-pass-through.patch"}]
        selected = lock_first.plan("homebrew", patches, mapping)
        assert lock_first.materialize_into(production, selected, patch)["tree"] == tree
        assert git(production, "rev-parse", "HEAD^{tree}") == tree
        assert not git(production, "for-each-ref", "refs/west/patch-stack-results/lock-first")
        # Existing result refs are not overwritten.  Materializer must reject
        # it before graph movement, and a repeated clean invocation works.
        original = git(production, "rev-parse", "HEAD")
        import patch_stack_materialize
        original_uuid = patch_stack_materialize.uuid.uuid4
        patch_stack_materialize.uuid.uuid4 = lambda: types.SimpleNamespace(hex="fixed")
        try:
            result_ref = "refs/west/patch-stack-results/lock-first/fixed"
            git(production, "update-ref", result_ref, source)
            try: lock_first.materialize_into(production, selected, patch)
            except lock_first.LockFirstError: pass
            else: raise AssertionError("existing result ref was accepted")
            assert git(production, "rev-parse", result_ref) == source and git(production, "rev-parse", "HEAD") == original
            git(production, "update-ref", "-d", result_ref)
        finally:
            patch_stack_materialize.uuid.uuid4 = original_uuid
        git(production, "reset", "--hard", "-q", base)
        assert lock_first.materialize_into(production, selected, patch)["tree"] == tree
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
        calls: list[object] = []
        patch_command.patch_stack_lock_first.plan = lambda *_args: selected
        patch_command.patch_stack_lock_first.materialize_into = lambda *_args: calls.append(True) or {"tree": tree}
        try:
            command._apply("homebrew", root, patches, "0", False, False, None, False)
            assert not calls, "normal no-flag apply invoked lock-first"
            command._apply("homebrew", root, patches, "0", False, False, None, True)
            assert calls == [True]
            prepared.clear()
            patch_command.patch_stack_lock_first.plan = lambda *_args: (_ for _ in ()).throw(lock_first.LockFirstError("bad mapping"))
            try: command._apply("homebrew", root, patches, "0", False, False, None, True)
            except RuntimeError: pass
            else: raise AssertionError("bad typed plan mutated")
            assert not prepared
            patch_command.patch_stack_lock_first.plan = lambda *_args: selected
            patch_command.patch_stack_lock_first.materialize_into = lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt())
            try: command._apply("homebrew", root, patches, "0", False, False, None, True)
            except KeyboardInterrupt: pass
            else: raise AssertionError("SIGINT swallowed")
            assert resets
        finally:
            patch_command.patch_stack_lock_first.plan, patch_command.patch_stack_lock_first.materialize_into = old_plan, old_into
    print("patch-stack lock-first contract: PASS")


if __name__ == "__main__":
    main()

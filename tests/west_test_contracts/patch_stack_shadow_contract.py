#!/usr/bin/env python3
"""Synthetic opt-in shadow contract; production refs are never inputs or outputs."""
from __future__ import annotations
import json
import shutil
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
class WestCommand:
    pass
west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)
import patch_stack_shadow as shadow
import patch as patch_command

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()

def must_fail(**kwargs: object) -> None:
    try: shadow.run_shadow(**kwargs)  # type: ignore[arg-type]
    except shadow.ShadowError: return
    raise AssertionError("shadow unexpectedly succeeded")

def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); bare = root / "mirror.git"; work = root / "work"; production = root / "production"
        git(root, "init", "--bare", "-q", str(bare)); git(root, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.name", "Test"); git(work, "config", "user.email", "test@example.invalid")
        (work / "a").write_text("base\n"); git(work, "add", "a"); git(work, "commit", "-qm", "base"); base = git(work, "rev-parse", "HEAD")
        (work / "a").write_text("changed\n"); git(work, "commit", "-qam", "change"); source = git(work, "rev-parse", "HEAD"); tree = git(work, "rev-parse", "HEAD^{tree}")
        patch = root / "change.patch"; patch.write_text(subprocess.run(["git", "format-patch", "--stdout", f"{base}..{source}"], cwd=work, check=True, text=True, stdout=subprocess.PIPE).stdout)
        git(work, "tag", f"patch-stack/v1/bases/{base}", base); git(work, "tag", f"patch-stack/v1/sources/{source}", source); git(work, "push", "-q", "origin", "HEAD:refs/heads/main", "--tags"); git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
        git(root, "clone", "-q", str(bare), str(production)); before = git(production, "show-ref", "--head")
        lock = {"schema_version": 2, "project":{"name":"synthetic","path":"."}, "upstream":{"url":bare.as_uri(),"base_commit":base}, "mirror":{"url":bare.as_uri(),"base_ref":f"refs/tags/patch-stack/v1/bases/{base}","base_oid":base,"source_ref":f"refs/tags/patch-stack/v1/sources/{source}","source_oid":source}, "source_commit":source,"ordered_commits":[source],"expected_tree":tree}
        lock_path = root / "one.yml"; lock_path.write_text(yaml.safe_dump(lock, sort_keys=False)); mapping = root / "shadow.yml"; mapping.write_text(yaml.safe_dump({"schema_version":1,"series":[{"profile":"homebrew","module":"darling","patch":"darling/sandbox-exec-pass-through.patch","lock":"one.yml"}]}, sort_keys=False))
        patches = [{"module":"darling", "path":"darling/sandbox-exec-pass-through.patch"}]
        selected = shadow.plan("homebrew", patches, mapping)
        evidence = root / "evidence.json"; result = shadow.run_shadow(shadow_plan=selected, legacy_patch=patch, evidence_path=evidence)
        assert result["verdict"] == "VALID" and result["legacy_resulting_tree"] == result["canonical_resulting_tree"] == tree
        assert result["fetched_legacy_base_oid"] == base
        assert result["legacy_mbox_ordered_commits"] == [source] and result["legacy_mbox_commit_count"] == 1
        assert git(production, "show-ref", "--head") == before, "shadow changed production integration refs"
        # No opt-in means this module is not called; parser wires it only under --shadow-lock.
        assert "--shadow-lock" in (ROOT / "west_commands" / "patch.py").read_text()
        for bad_patches in ([{"module":"darling", "path":"darling/not-allowed.patch"}], patches * 2):
            try: shadow.plan("homebrew", bad_patches, mapping)
            except shadow.ShadowError: pass
            else: raise AssertionError("invalid typed plan unexpectedly succeeded")
        missing = root / "missing.yml"; missing.write_text(yaml.safe_dump({"schema_version":1,"series":[]}, sort_keys=False))
        try: shadow.plan("homebrew", patches, missing)
        except shadow.ShadowError: pass
        else: raise AssertionError("missing typed plan unexpectedly succeeded")
        absolute = root / "absolute.yml"; absolute.write_text(yaml.safe_dump({"schema_version":1,"series":[{"profile":"homebrew","module":"darling","patch":patches[0]["path"],"lock":str(lock_path)}]}, sort_keys=False))
        try: shadow.plan("homebrew", patches, absolute)
        except shadow.ShadowError: pass
        else: raise AssertionError("absolute lock path unexpectedly succeeded")
        escaped = root / "escaped.yml"; escaped.write_text(yaml.safe_dump({"schema_version":1,"series":[{"profile":"homebrew","module":"darling","patch":patches[0]["path"],"lock":"../one.yml"}]}, sort_keys=False))
        try: shadow.plan("homebrew", patches, escaped)
        except shadow.ShadowError: pass
        else: raise AssertionError("escaping lock path unexpectedly succeeded")
        symlink = root / "linked.yml"; symlink.symlink_to(lock_path)
        linked = root / "linked-map.yml"; linked.write_text(yaml.safe_dump({"schema_version":1,"series":[{"profile":"homebrew","module":"darling","patch":patches[0]["path"],"lock":"linked.yml"}]}, sort_keys=False))
        try: shadow.plan("homebrew", patches, linked)
        except shadow.ShadowError: pass
        else: raise AssertionError("symlink lock path unexpectedly succeeded")
        bad_patch = root / "missing.patch"; must_fail(shadow_plan=selected, legacy_patch=bad_patch)
        bad_lock = dict(lock); bad_lock["expected_tree"] = "0" * 40; lock_path.write_text(yaml.safe_dump(bad_lock, sort_keys=False)); must_fail(shadow_plan=selected, legacy_patch=patch); lock_path.write_text(yaml.safe_dump(lock, sort_keys=False))
        bad_mirror = dict(lock); bad_mirror["mirror"] = dict(lock["mirror"], url=(root / "absent.git").as_uri()); lock_path.write_text(yaml.safe_dump(bad_mirror, sort_keys=False)); must_fail(shadow_plan=selected, legacy_patch=patch); lock_path.write_text(yaml.safe_dump(lock, sort_keys=False))
        # Failure while atomically publishing the outer evidence must remove
        # the transaction-specific temporary file.
        failing_evidence = root / "evidence-fail.json"
        original_replace = Path.replace
        def fail_outer_replace(path: Path, target: str | Path) -> Path:
            if path.name.startswith("evidence-fail.json."):
                raise OSError("controlled evidence replace failure")
            return original_replace(path, target)
        Path.replace = fail_outer_replace  # type: ignore[method-assign]
        try:
            must_fail(shadow_plan=selected, legacy_patch=patch, evidence_path=failing_evidence)
        finally:
            Path.replace = original_replace  # type: ignore[method-assign]
        assert not list(root.glob("evidence-fail.json.*.tmp"))
        original_rmtree = shadow.shutil.rmtree
        shadow.shutil.rmtree = lambda _: (_ for _ in ()).throw(OSError("controlled cleanup failure"))
        cleanup_evidence = root / "cleanup.json"
        try:
            must_fail(shadow_plan=selected, legacy_patch=patch, evidence_path=cleanup_evidence)
        finally:
            shadow.shutil.rmtree = original_rmtree
        cleanup = json.loads(cleanup_evidence.read_text()); leaked = Path(tempfile.gettempdir()) / f"west-patch-shadow-{cleanup['transaction_id']}"
        assert cleanup["verdict"] == "ERROR" and leaked.exists()
        shutil.rmtree(leaked)
        # Repeated opt-in transactions are independent and leave no permanent refs.
        second = shadow.run_shadow(shadow_plan=selected, legacy_patch=patch)
        assert second["evidence_path"] != result["evidence_path"]
        default_evidence = Path(second["evidence_path"])
        assert default_evidence.is_file()
        default_evidence.unlink()
        assert git(production, "show-ref", "--head") == before
        print("patch-stack shadow contract: PASS")

        # Exercise DarlingPatch._apply, the implementation reached by
        # `west patch apply`, rather than only the isolated runner.  The test
        # keeps the ordinary git-am lifecycle and substitutes only the
        # external clean-ODB comparison.
        git(work, "checkout", "-q", "main")
        git(work, "reset", "--hard", "-q", base)
        apply_patch = root / "apply.patch"
        apply_patch.write_text(patch.read_text())
        (root / "west.lock.yml").write_text("manifest: synthetic\n")
        patches_for_apply = [{"module": "darling", "path": "fixture.patch", "sha256sum": "unused"}]
        target_plan = {"profile": "homebrew", "module": "darling", "patch": "fixture.patch", "lock_path": str(lock_path)}
        command = patch_command.DarlingPatch.__new__(patch_command.DarlingPatch)
        command._base_profile = None
        command.manifest = types.SimpleNamespace(repo_abspath=root)
        command._group = lambda _patches: {"darling": patches_for_apply}
        command._require_base_applied = lambda _modules: None
        command._ensure_generated_context = lambda *_args, **_kwargs: None
        command._repo = lambda _module: work
        def prepare(_module: str, repo: Path, branch: str, parent: bool = False) -> None:
            git(repo, "am", "--abort") if (repo / ".git/rebase-apply").exists() else None
            git(repo, "switch", "-q", "--detach", base)
            git(repo, "branch", "-f", branch, base)
            git(repo, "switch", "-q", branch)
        command._prepare = prepare
        command._verify_patch = lambda *_args, **_kwargs: apply_patch
        recorded: list[str] = []
        command._record_integration = lambda *_args, **_kwargs: recorded.append(git(work, "rev-parse", "HEAD^{tree}"))
        command._abort_am = lambda repo: None
        reset_calls: list[bool] = []
        def reset(*_args: object, **_kwargs: object) -> None:
            reset_calls.append(True)
            git(work, "switch", "-q", "--detach", base)
            subprocess.run(["git", "branch", "-D", "integration/homebrew"], cwd=work, check=False, stdout=subprocess.DEVNULL)
        command._reset = reset
        command.inf = lambda _message: None
        command.die = lambda message, **_kwargs: (_ for _ in ()).throw(RuntimeError(message))
        original_plan, original_run = patch_command.patch_stack_shadow.plan, patch_command.patch_stack_shadow.run_shadow
        calls: list[dict[str, object]] = []
        patch_command.patch_stack_shadow.plan = lambda _profile, _patches: target_plan
        patch_command.patch_stack_shadow.run_shadow = lambda **kwargs: calls.append(kwargs) or {"legacy_resulting_tree": tree, "canonical_resulting_tree": tree}
        try:
            command._apply("homebrew", root, patches_for_apply, "0", False, False)
            no_flag_tree = recorded[-1]
            no_flag_ref = git(work, "rev-parse", "refs/heads/integration/homebrew")
            command._apply("homebrew", root, patches_for_apply, "0", False, True)
            assert len(calls) == 1 and recorded[-1] == no_flag_tree == tree
            assert git(work, "rev-parse", "refs/heads/integration/homebrew") == no_flag_ref
            # A typed-plan rejection occurs before _prepare (and therefore
            # before any production mutation).
            prepared: list[bool] = []
            command._prepare = lambda *_args, **_kwargs: prepared.append(True)
            patch_command.patch_stack_shadow.plan = lambda *_args: (_ for _ in ()).throw(shadow.ShadowError("duplicate entry"))
            try: command._apply("homebrew", root, patches_for_apply, "0", False, True)
            except RuntimeError: pass
            else: raise AssertionError("invalid plan unexpectedly applied")
            assert not prepared
            # A shadow failure takes the normal rollback path even without
            # --roll-back, leaving the legacy integration at its base.
            command._prepare = prepare
            patch_command.patch_stack_shadow.plan = lambda _profile, _patches: target_plan
            patch_command.patch_stack_shadow.run_shadow = lambda **_kwargs: (_ for _ in ()).throw(shadow.ShadowError("controlled shadow failure"))
            try: command._apply("homebrew", root, patches_for_apply, "0", False, True)
            except RuntimeError: pass
            else: raise AssertionError("shadow failure unexpectedly applied")
            assert reset_calls and git(work, "rev-parse", "HEAD") == base
            # This is deliberately a real BaseException: SIGINT after the
            # legacy am must reset the partial integration branch and remain
            # visible to the caller rather than becoming command.die().
            patch_command.patch_stack_shadow.run_shadow = lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
            try: command._apply("homebrew", root, patches_for_apply, "0", False, True)
            except KeyboardInterrupt: pass
            else: raise AssertionError("KeyboardInterrupt was swallowed")
            assert git(work, "rev-parse", "HEAD") == base
            assert subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/integration/homebrew"], cwd=work).returncode == 1
        finally:
            patch_command.patch_stack_shadow.plan = original_plan
            patch_command.patch_stack_shadow.run_shadow = original_run
        print("patch-stack shadow orchestration contract: PASS")

if __name__ == "__main__": main()

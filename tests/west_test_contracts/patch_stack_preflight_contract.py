#!/usr/bin/env python3
"""Synthetic read-only contract for patch-stack preflight."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_stack_preflight as preflight


def run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def snapshot(repo: Path) -> tuple[str, str, str, str]:
    return (run(repo, "show-ref", "--head"), run(repo, "status", "--porcelain=v1", "--untracked-files=all"), run(repo, "rev-parse", "HEAD"), run(repo, "count-objects", "-v"))


def write_lock(path: Path, repo: Path, base: str, commits: list[str], tree: str, **changes: object) -> None:
    source = commits[-1]
    lock = {"schema_version": 1, "project": {"name": "synthetic", "path": str(repo)}, "upstream": {"url": "https://example.invalid/upstream", "base_commit": base}, "mirror": {"url": "file:///synthetic-mirror", "immutable_ref": f"refs/patch-stack/v1/sources/{source}", "immutable_oid": source}, "source_commit": source, "ordered_commits": commits, "expected_tree": tree}
    lock.update(changes)
    path.write_text(json.dumps(lock))


def write_v2_lock(path: Path, repo: Path, base: str, commits: list[str], tree: str, **changes: object) -> None:
    source = commits[-1]
    lock = {
        "schema_version": 2,
        "project": {"name": "synthetic", "path": "."},
        "upstream": {"url": "https://example.invalid/upstream", "base_commit": base},
        "mirror": {
            "url": "https://example.invalid/mirror",
            "base_ref": f"refs/tags/patch-stack/v1/bases/{base}",
            "base_oid": base,
            "source_ref": f"refs/tags/patch-stack/v1/sources/{source}",
            "source_oid": source,
        },
        "source_commit": source,
        "ordered_commits": commits,
        "expected_tree": tree,
    }
    lock.update(changes)
    path.write_text(yaml.safe_dump(lock, sort_keys=False))


def assert_result(repo: Path, lock: Path, verdict: str, code: int) -> None:
    before = snapshot(repo)
    result = preflight.inspect(repo, lock)
    assert snapshot(repo) == before, "preflight mutated Git state"
    assert result["overall_verdict"] == verdict and result["exit_code"] == code, result
    assert {"schema_version", "inputs", "checks", "overall_verdict", "next_safe_action", "exit_code"} <= set(result)


def assert_subprocess(repo: Path, lock: Path, code: int, json_mode: bool = True, cwd: Path | None = None) -> None:
    command = [sys.executable, "-B", str(ROOT / "west_commands" / "patch_stack_preflight.py"), "--repo", str(repo), "--lock", str(lock)]
    if json_mode:
        command.append("--json")
    before = snapshot(repo) if (repo / ".git").exists() else None
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == code and "Traceback" not in result.stderr, result.stderr
    if json_mode:
        assert json.loads(result.stdout)["exit_code"] == code
    if before is not None:
        assert snapshot(repo) == before


def assert_west(repo: Path, lock: Path, code: int, cwd: Path | None = None) -> None:
    before = snapshot(repo) if (repo / ".git").exists() else None
    result = subprocess.run(["west", "patch", "preflight", "--repo", str(repo), "--lock", str(lock), "--json"], cwd=cwd or ROOT.parent, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == code and "Traceback" not in result.stderr, result.stderr
    assert json.loads(result.stdout)["exit_code"] == code
    if before is not None:
        assert snapshot(repo) == before


def assert_git_error(repo: Path, lock: Path, expected: tuple[str, ...]) -> None:
    """Inject one unexpected Git failure and prove it remains read-only ERROR."""
    before = snapshot(repo)
    original = preflight._git

    def failing(path: Path, *args: str) -> tuple[int, str, str]:
        if args == expected:
            return 77, "", "controlled synthetic Git failure"
        return original(path, *args)

    preflight._git = failing
    try:
        result = preflight.inspect(repo, lock)
    finally:
        preflight._git = original
    assert snapshot(repo) == before, f"preflight mutated Git state after {expected} failure"
    assert result["overall_verdict"] == "ERROR" and result["exit_code"] == preflight.EXIT_TOOL, result
    assert "Traceback" not in json.dumps(result)


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(temp) / "repo"; repo.mkdir()
        run(repo, "init", "-q"); run(repo, "config", "user.name", "Test"); run(repo, "config", "user.email", "test@example.invalid")
        (repo / "a").write_text("base\n"); run(repo, "add", "a"); run(repo, "commit", "-qm", "base"); base = run(repo, "rev-parse", "HEAD")
        (repo / "a").write_text("one\n"); run(repo, "commit", "-qam", "one"); one = run(repo, "rev-parse", "HEAD")
        (repo / "b").write_text("two\n"); run(repo, "add", "b"); run(repo, "commit", "-qm", "two"); two = run(repo, "rev-parse", "HEAD")
        tree = run(repo, "rev-parse", "HEAD^{tree}"); run(repo, "update-ref", f"refs/patch-stack/v1/sources/{two}", two)
        lock = Path(temp) / "lock.json"; write_lock(lock, repo, base, [one, two], tree)
        assert_result(repo, lock, "VALID", 0)
        assert_subprocess(repo, lock, 0); assert_subprocess(repo, lock, 0, False)
        assert_west(repo, lock, 0)
        bad = Path(temp) / "bad.json"
        write_lock(bad, repo, base, [one, two], "0" * 40); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        one_ref = f"refs/patch-stack/v1/sources/{one}"
        run(repo, "update-ref", one_ref, one)
        write_lock(bad, repo, base, [two, one], tree, source_commit=one, mirror={"url":"file:///synthetic-mirror","immutable_ref":one_ref,"immutable_oid":one}); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        run(repo, "update-ref", "-d", one_ref)
        # Canonical source refs reject generic refs, a different SHA suffix,
        # and the old bases namespace before any repository inspection.
        write_lock(bad, repo, base, [one, two], tree, mirror={"url":"file:///synthetic-mirror","immutable_ref":"refs/heads/source","immutable_oid":two}); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        write_lock(bad, repo, base, [one, two], tree, mirror={"url":"file:///synthetic-mirror","immutable_ref":f"refs/patch-stack/v1/sources/{one}","immutable_oid":two}); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        write_lock(bad, repo, base, [one, two], tree, mirror={"url":"file:///synthetic-mirror","immutable_ref":f"refs/patch-stack/v1/bases/{two}","immutable_oid":two}); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        write_lock(bad, repo, base, [one, two], tree, mirror={"url":"file:///synthetic-mirror","immutable_ref":f"refs/patch-stack/v1/sources/{two}","immutable_oid":one}); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        write_lock(bad, repo, base, [one, two], tree); assert_result(repo, bad, "VALID", preflight.EXIT_VALID)
        assert_subprocess(repo, bad, preflight.EXIT_VALID)
        bad.write_text("{}"); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        source_ref = f"refs/patch-stack/v1/sources/{two}"
        run(repo, "update-ref", "-d", source_ref); assert_result(repo, lock, "INCOMPLETE", preflight.EXIT_MISSING); assert_subprocess(repo, lock, preflight.EXIT_MISSING); run(repo, "update-ref", source_ref, two)
        run(repo, "update-ref", source_ref, one); assert_result(repo, lock, "INVALID", preflight.EXIT_INVALID); run(repo, "update-ref", source_ref, two)
        malformed = copy.deepcopy(json.loads(lock.read_text())); malformed["source_commit"] = "abc"; bad.write_text(json.dumps(malformed)); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID); assert_subprocess(repo, bad, preflight.EXIT_INVALID)
        malformed = copy.deepcopy(json.loads(lock.read_text())); malformed["source_commit"] = "A" * 40; bad.write_text(json.dumps(malformed)); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        assert_subprocess(repo, Path(temp) / "missing-lock.json", preflight.EXIT_TOOL)
        assert_subprocess(Path(temp) / "missing-repo", lock, preflight.EXIT_TOOL)
        assert_west(repo, Path(temp) / "missing-lock.json", preflight.EXIT_TOOL)
        (repo / "untracked").write_text("x"); assert_result(repo, lock, "INVALID", preflight.EXIT_DIRTY); (repo / "untracked").unlink()
        (repo / "untracked").write_text("x"); assert_subprocess(repo, lock, preflight.EXIT_DIRTY); (repo / "untracked").unlink()
        (repo / "untracked").write_text("x"); assert_west(repo, lock, preflight.EXIT_DIRTY); (repo / "untracked").unlink()
        (repo / "a").write_text("dirty\n"); assert_result(repo, lock, "INVALID", preflight.EXIT_DIRTY); run(repo, "checkout", "--", "a")
        alt = repo / ".git" / "objects" / "info" / "alternates"; alt.parent.mkdir(parents=True, exist_ok=True); alt.write_text("/nonexistent\n"); assert_result(repo, lock, "INVALID", preflight.EXIT_INVALID); alt.unlink()
        write_lock(bad, repo, "f" * 40, [one, two], tree); assert_result(repo, bad, "INCOMPLETE", preflight.EXIT_MISSING)
        write_lock(bad, repo, base, [one, two], tree, project={"name":"synthetic","path":str(Path(temp) / "other")}); assert_result(repo, bad, "INVALID", preflight.EXIT_INVALID)
        run(repo, "config", "extensions.partialClone", "promisor"); assert_result(repo, lock, "INVALID", preflight.EXIT_INVALID); run(repo, "config", "--unset", "extensions.partialClone")
        (repo / ".git" / "shallow").write_text(base + "\n"); assert_result(repo, lock, "INVALID", preflight.EXIT_INVALID); (repo / ".git" / "shallow").unlink()
        # Unexpected failures after the repository probe are tool errors, not
        # successful empty output. Each injected path stays read-only.
        assert_git_error(repo, lock, ("status", "--porcelain=v1", "--untracked-files=all"))
        assert_git_error(repo, lock, ("rev-list", "--count", f"{base}..{two}"))
        assert_git_error(repo, lock, ("show", "-s", "--format=%P", one))
        assert_git_error(repo, lock, ("rev-parse", f"{two}^{{tree}}"))
        assert_git_error(repo, lock, ("config", "--get", "extensions.partialClone"))

        # Hosted tag schema v2 validates the exact base and source boundary.
        base_tag = f"refs/tags/patch-stack/v1/bases/{base}"
        source_tag = f"refs/tags/patch-stack/v1/sources/{two}"
        run(repo, "update-ref", base_tag, base); run(repo, "update-ref", source_tag, two)
        # This is intentionally YAML, and both direct and West CLI calls run
        # from a foreign workspace directory with absolute --repo/--lock.
        v2 = Path(temp) / "lock-v2.yml"; write_v2_lock(v2, repo, base, [one, two], tree)
        foreign_cwd = ROOT / "docs"
        assert_result(repo, v2, "VALID", preflight.EXIT_VALID); assert_subprocess(repo, v2, preflight.EXIT_VALID); assert_west(repo, v2, preflight.EXIT_VALID)
        assert_subprocess(repo, v2, preflight.EXIT_VALID, cwd=foreign_cwd); assert_west(repo, v2, preflight.EXIT_VALID, cwd=foreign_cwd)
        nested = repo / "nested"; nested.mkdir()
        assert_result(nested, v2, "INVALID", preflight.EXIT_INVALID)
        write_v2_lock(v2, repo, base, [one, two], tree, project={"name": "synthetic", "path": str(repo)})
        assert_result(repo, v2, "INVALID", preflight.EXIT_INVALID)
        write_v2_lock(v2, repo, base, [one, two], tree)
        # An annotated tag is accepted only because ^{commit} peels to the
        # same declared base OID.
        run(repo, "update-ref", "-d", base_tag); run(repo, "tag", "-a", "patch-stack/v1/bases/" + base, "-m", "base", base)
        assert_result(repo, v2, "VALID", preflight.EXIT_VALID)
        run(repo, "update-ref", "-d", base_tag); run(repo, "update-ref", base_tag, base)
        run(repo, "update-ref", "-d", base_tag); assert_result(repo, v2, "INCOMPLETE", preflight.EXIT_MISSING); run(repo, "update-ref", base_tag, base)
        run(repo, "update-ref", base_tag, one); assert_result(repo, v2, "INVALID", preflight.EXIT_INVALID); run(repo, "update-ref", base_tag, base)
        run(repo, "update-ref", "-d", source_tag); assert_result(repo, v2, "INCOMPLETE", preflight.EXIT_MISSING); run(repo, "update-ref", source_tag, two)
        run(repo, "update-ref", source_tag, one); assert_result(repo, v2, "INVALID", preflight.EXIT_INVALID); run(repo, "update-ref", source_tag, two)
        write_v2_lock(v2, repo, base, [one, two], tree, mirror={"url":"https://example.invalid/mirror","base_ref":f"refs/tags/patch-stack/v1/bases/{one}","base_oid":base,"source_ref":source_tag,"source_oid":two}); assert_result(repo, v2, "INVALID", preflight.EXIT_INVALID)
        write_v2_lock(v2, repo, base, [one, two], tree, mirror={"url":"https://example.invalid/mirror","base_ref":base_tag,"base_oid":one,"source_ref":source_tag,"source_oid":two}); assert_result(repo, v2, "INVALID", preflight.EXIT_INVALID)
        # A v1-shaped mirror is not a v2 lock and a v2-shaped mirror is not a
        # v1 lock: schema selection is fail-closed by schema_version.
        write_v2_lock(v2, repo, base, [one, two], tree, schema_version=1); assert_result(repo, v2, "INVALID", preflight.EXIT_INVALID)
        write_lock(v2, repo, base, [one, two], tree, schema_version=2); assert_result(repo, v2, "INVALID", preflight.EXIT_INVALID)
    print("PASS patch-stack-preflight-contract")


if __name__ == "__main__": main()

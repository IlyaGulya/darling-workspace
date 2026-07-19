#!/usr/bin/env python3
"""Synthetic transactional contract for canonical lock materialization."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_stack_materialize as materialize


def run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def exists(repo: Path, ref: str) -> bool:
    return subprocess.run(["git", "show-ref", "--verify", "--quiet", ref], cwd=repo).returncode == 0


def lock_data(mirror: Path, base: str, ordered: list[str], tree: str) -> dict[str, object]:
    source = ordered[-1]
    return {"schema_version": 2, "project": {"name": "synthetic", "path": "."},
            "upstream": {"url": "https://example.invalid/upstream", "base_commit": base},
            "mirror": {"url": mirror.as_uri(), "base_ref": f"refs/tags/patch-stack/v1/bases/{base}", "base_oid": base,
                       "source_ref": f"refs/tags/patch-stack/v1/sources/{source}", "source_oid": source},
            "source_commit": source, "ordered_commits": ordered, "expected_tree": tree}


def write_lock(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def assert_failure(repo: Path, lock: Path, *, result_ref: str, evidence: Path, text: str | None = None, preserve_result: bool = False) -> dict[str, object]:
    try:
        materialize.materialize(repo, lock, result_ref, evidence)
    except materialize.MaterializeError as error:
        if text:
            assert text in str(error), error
    else:
        raise AssertionError("materialization unexpectedly succeeded")
    if not preserve_result:
        assert not exists(repo, result_ref), f"failed transaction left {result_ref}"
    if evidence.exists():
        payload = json.loads(evidence.read_text())
        assert payload["verdict"] == "ERROR", payload
        return payload
    return {}


def assert_transaction_recovered(repo: Path, temp: Path, payload: dict[str, object], result_ref: str, existing: str, existing_oid: str) -> None:
    """Restore a deliberately failed cleanup, then prove no transaction state remains."""
    transaction = str(payload["transaction_id"])
    root = Path(tempfile.gettempdir()) / f"west-lock-materialize-{transaction}"
    worktree = root / "source"
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if root.exists():
        shutil.rmtree(root)
    for suffix in ("base", "source"):
        run(repo, "update-ref", "-d", f"refs/west/patch-stack-materialize/{transaction}/{suffix}")
    refs = run(repo, "for-each-ref", "--format=%(refname)")
    assert f"refs/west/patch-stack-materialize/{transaction}/" not in refs, refs
    worktrees = run(repo, "worktree", "list", "--porcelain")
    assert not root.exists() and str(worktree) not in worktrees, worktrees
    assert not exists(repo, result_ref), result_ref
    assert run(repo, "rev-parse", existing) == existing_oid


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary); bare = temp / "mirror.git"; work = temp / "work"; repo = temp / "repo"
        run(temp, "init", "--bare", "-q", str(bare)); run(temp, "clone", "-q", str(bare), str(work))
        run(work, "config", "user.name", "Contract"); run(work, "config", "user.email", "contract@example.invalid")
        (work / "a").write_text("base\n"); run(work, "add", "a"); run(work, "commit", "-qm", "base"); base = run(work, "rev-parse", "HEAD")
        run(work, "tag", f"patch-stack/v1/bases/{base}")
        (work / "a").write_text("one\n"); run(work, "commit", "-qam", "one"); one = run(work, "rev-parse", "HEAD")
        (work / "b").write_text("two\n"); run(work, "add", "b"); run(work, "commit", "-qm", "two"); two = run(work, "rev-parse", "HEAD")
        tree = run(work, "rev-parse", "HEAD^{tree}"); run(work, "tag", f"patch-stack/v1/sources/{two}")
        run(work, "push", "-q", "origin", "HEAD:refs/heads/main", "--tags")
        run(bare, "symbolic-ref", "HEAD", "refs/heads/main")
        run(temp, "clone", "-q", str(bare), str(repo)); run(repo, "config", "user.name", "Contract"); run(repo, "config", "user.email", "contract@example.invalid")
        data = lock_data(bare, base, [one, two], tree); lock = temp / "lock.yml"; write_lock(lock, data)

        evidence = temp / "success.json"; result = "refs/west/patch-stack-results/contract-success"
        outcome = materialize.materialize(repo, lock, result, evidence)
        assert outcome["verdict"] == outcome["status"] == "VALID" and outcome["evidence"] == "written", outcome
        assert exists(repo, result) and run(repo, "rev-parse", f"{result}^{{tree}}") == tree
        assert outcome["transaction_id"] and all("patch-stack-materialize/" not in ref for ref in run(repo, "for-each-ref", "--format=%(refname)").splitlines())

        # Existing results are create-only and are never modified, even when their OID is wrong.
        existing = "refs/west/patch-stack-results/existing"; run(repo, "update-ref", existing, base)
        assert_failure(repo, lock, result_ref=existing, evidence=temp / "existing.json", text="already exists", preserve_result=True)
        assert run(repo, "rev-parse", existing) == base

        # The real West entry point has the same JSON success and documented
        # nonzero-error contract as the typed implementation.
        if not os.environ.get("PATCH_STACK_MATERIALIZE_CONTRACT_SKIP_WEST_SUBPROCESS"):
            cli_ref = "refs/west/patch-stack-results/cli-success"; cli_evidence = temp / "cli-success.json"
            command = ["west", "patch", "materialize-lock", "--repo", str(repo), "--lock", str(lock), "--result-ref", cli_ref, "--evidence", str(cli_evidence), "--json"]
            cli = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert cli.returncode == 0 and json.loads(cli.stdout)["verdict"] == "VALID", (cli.stdout, cli.stderr)
            invalid_lock = temp / "invalid.yml"; invalid_lock.write_text("{}\n")
            cli_bad_command = ["west", "patch", "materialize-lock", "--repo", str(repo), "--lock", str(invalid_lock), "--result-ref", "refs/west/patch-stack-results/cli-invalid", "--evidence", str(temp / "cli-invalid.json"), "--json"]
            cli_bad = subprocess.run(cli_bad_command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert cli_bad.returncode == 1 and "Traceback" not in cli_bad.stderr, cli_bad.stderr
            cli_existing = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert cli_existing.returncode == 1 and run(repo, "rev-parse", cli_ref) == two, cli_existing.stderr

        for invalid in ("refs/west/patch-stack-results/../escape", "refs/west/patch-stack-results/", "refs/heads/outside"):
            try:
                materialize.materialize(repo, lock, invalid, temp / "invalid.json")
            except materialize.MaterializeError:
                pass
            else:
                raise AssertionError(f"accepted invalid result ref {invalid}")

        # INCOMPLETE is fetchable only when every completed check passed and
        # the sole gaps are declared objects/immutable tags. A mixed
        # INCOMPLETE + FAIL must never reach fetch.
        original_inspect = materialize.inspect
        materialize.inspect = lambda *_: {"overall_verdict": "INCOMPLETE", "checks": [
            {"name": "repository_top_level", "status": "FAIL"},
            {"name": "declared_objects", "status": "UNKNOWN"},
        ]}
        try:
            rejected = assert_failure(repo, lock, result_ref="refs/west/patch-stack-results/mixed-incomplete", evidence=temp / "mixed-incomplete.json", text="pre-fetch preflight")
        finally:
            materialize.inspect = original_inspect
        assert rejected["fetched"] == {}, rejected

        # Immutable boundary and graph failures are caught after fetch, with no published result.
        bad = temp / "bad.yml"
        variants: list[tuple[str, dict[str, object]]] = []
        shifted = copy.deepcopy(data); shifted["mirror"]["source_oid"] = one  # type: ignore[index]
        variants.append(("shifted", shifted))
        missing = copy.deepcopy(data); missing["mirror"]["source_ref"] = f"refs/tags/patch-stack/v1/sources/{'f' * 40}"  # type: ignore[index]
        variants.append(("missing", missing))
        wrong_order = copy.deepcopy(data); wrong_order["ordered_commits"] = [two, one]  # type: ignore[index]
        variants.append(("wrong-order", wrong_order))
        wrong_tree = copy.deepcopy(data); wrong_tree["expected_tree"] = "0" * 40  # type: ignore[index]
        variants.append(("wrong-tree", wrong_tree))
        dependent_wrong_order = copy.deepcopy(data); dependent_wrong_order["mirror"]["base_ref"] = "refs/west/patch-stack-results/contract-success"  # type: ignore[index]
        variants.append(("dependent-wrong-order", dependent_wrong_order))
        for name, variant in variants:
            write_lock(bad, variant)
            assert_failure(repo, bad, result_ref=f"refs/west/patch-stack-results/{name}", evidence=temp / f"{name}.json")

        # Merge source: rev-list can enumerate it, but an ordered stack must have one parent at each commit.
        run(work, "checkout", "-qb", "side", base); (work / "side").write_text("side\n"); run(work, "add", "side"); run(work, "commit", "-qm", "side")
        run(work, "checkout", "-q", "main"); run(work, "merge", "--no-ff", "-qm", "merge side", "side"); merge = run(work, "rev-parse", "HEAD"); merge_tree = run(work, "rev-parse", "HEAD^{tree}")
        run(work, "tag", f"patch-stack/v1/sources/{merge}"); run(work, "push", "-q", "origin", "main", "--tags")
        merge_lock = lock_data(bare, base, run(work, "rev-list", "--reverse", f"{base}..{merge}").splitlines(), merge_tree); write_lock(bad, merge_lock)
        assert_failure(repo, bad, result_ref="refs/west/patch-stack-results/merge", evidence=temp / "merge.json", text="nonlinear")

        (repo / "dirty").write_text("dirty\n"); assert_failure(repo, lock, result_ref="refs/west/patch-stack-results/dirty", evidence=temp / "dirty.json", text="dirty"); (repo / "dirty").unlink()
        unreachable = copy.deepcopy(data); unreachable["mirror"]["url"] = (temp / "missing.git").as_uri()  # type: ignore[index]
        write_lock(bad, unreachable); assert_failure(repo, bad, result_ref="refs/west/patch-stack-results/fetch", evidence=temp / "fetch.json", text="fetch")

        # A stale transaction namespace is neither reused nor removed by a new transaction.
        stale = "refs/west/patch-stack-materialize/stale/source"; run(repo, "update-ref", stale, two)
        stale_root = temp / "stale"; run(repo, "worktree", "add", "--detach", "-q", str(stale_root), two)
        concurrent_locks = [temp / "concurrent-a.json", temp / "concurrent-b.json"]
        def concurrent(index: int) -> dict[str, object]:
            return materialize.materialize(repo, lock, f"refs/west/patch-stack-results/concurrent-{index}", concurrent_locks[index])
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(concurrent, range(2)))
        assert all(item["verdict"] == "VALID" for item in outcomes) and exists(repo, stale)
        run(repo, "worktree", "remove", "--force", str(stale_root)); run(repo, "update-ref", "-d", stale)

        # Controlled tool failures must be ERROR transactions, never silent successes.
        original_run, original_git, original_write, original_rmtree = materialize._run, materialize._git, materialize._write_evidence, materialize.shutil.rmtree
        foreign_root = Path(tempfile.mkdtemp(prefix="west-lock-materialize-foreign-"))
        def controlled(name: str, mutate, expected: str, recover: bool = False) -> None:
            try:
                mutate()
                payload = assert_failure(repo, lock, result_ref=f"refs/west/patch-stack-results/{name}", evidence=temp / f"{name}.json")
            finally:
                materialize._run, materialize._git, materialize._write_evidence, materialize.shutil.rmtree = original_run, original_git, original_write, original_rmtree
            assert expected in json.dumps(payload), payload
            if recover:
                assert_transaction_recovered(repo, temp, payload, f"refs/west/patch-stack-results/{name}", existing, base)
                assert foreign_root.exists(), "recovery touched another transaction root"

        def show_ref_failure() -> None:
            def failing(repo_path: Path, *args: str):
                if args[:1] == ("show-ref",):
                    return subprocess.CompletedProcess(["git", *args], 77, "", "controlled show-ref failure")
                return original_run(repo_path, *args)
            materialize._run = failing
        controlled("show-ref-failure", show_ref_failure, "show-ref")

        def interrupt_before() -> None:
            def failing(repo_path: Path, *args: str) -> str:
                if args[:1] == ("fetch",): raise materialize.MaterializeError("controlled interruption before worktree")
                return original_git(repo_path, *args)
            materialize._git = failing
        controlled("interrupt-before", interrupt_before, "interruption before")

        def interrupt_after() -> None:
            def failing(repo_path: Path, *args: str) -> str:
                value = original_git(repo_path, *args)
                if args[:2] == ("worktree", "add"): raise materialize.MaterializeError("controlled interruption after worktree")
                return value
            materialize._git = failing
        controlled("interrupt-after", interrupt_after, "interruption after")

        def cleanup_worktree_failure() -> None:
            def failing(repo_path: Path, *args: str) -> str:
                if args[:2] == ("worktree", "remove"): raise materialize.MaterializeError("controlled worktree cleanup failure")
                return original_git(repo_path, *args)
            materialize._git = failing
        controlled("cleanup-worktree", cleanup_worktree_failure, "cleanup failed", recover=True)

        def cleanup_ref_failure() -> None:
            def failing(repo_path: Path, *args: str):
                if args[:2] == ("update-ref", "-d"):
                    return subprocess.CompletedProcess(["git", *args], 77, "", "controlled ref cleanup failure")
                return original_run(repo_path, *args)
            materialize._run = failing
        controlled("cleanup-ref", cleanup_ref_failure, "cleanup failed", recover=True)

        def cleanup_root_failure() -> None:
            def failing(path: Path) -> None: raise OSError("controlled root cleanup failure")
            materialize.shutil.rmtree = failing
        controlled("cleanup-root", cleanup_root_failure, "cleanup failed", recover=True)

        def result_creation_failure() -> None:
            def failing(repo_path: Path, *args: str) -> str:
                if args[:1] == ("update-ref",) and len(args) == 4: raise materialize.MaterializeError("controlled result ref failure")
                return original_git(repo_path, *args)
            materialize._git = failing
        controlled("result-create", result_creation_failure, "result ref failure")

        # KeyboardInterrupt is what SIGINT becomes in Python. It is not an
        # Exception, so this proves the transaction's BaseException path
        # cleans refs/worktree and re-raises rather than swallowing the signal.
        def native_interrupt(repo_path: Path, *args: str) -> str:
            value = original_git(repo_path, *args)
            if args[:2] == ("worktree", "add"):
                raise KeyboardInterrupt("controlled SIGINT")
            return value
        materialize._git = native_interrupt
        interrupt_ref = "refs/west/patch-stack-results/keyboard-interrupt"
        interrupt_evidence = temp / "keyboard-interrupt.json"
        try:
            materialize.materialize(repo, lock, interrupt_ref, interrupt_evidence)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("KeyboardInterrupt was swallowed")
        finally:
            materialize._git = original_git
        interrupt_payload = json.loads(interrupt_evidence.read_text())
        assert interrupt_payload["verdict"] == "ERROR"
        assert_transaction_recovered(repo, temp, interrupt_payload, interrupt_ref, existing, base)

        def interrupt_after_publish(repo_path: Path, *args: str) -> str:
            value = original_git(repo_path, *args)
            if args[:1] == ("update-ref",) and len(args) == 4:
                raise KeyboardInterrupt("controlled SIGINT after result creation")
            return value
        materialize._git = interrupt_after_publish
        publish_interrupt_ref = "refs/west/patch-stack-results/keyboard-interrupt-publish"
        publish_interrupt_evidence = temp / "keyboard-interrupt-publish.json"
        try:
            materialize.materialize(repo, lock, publish_interrupt_ref, publish_interrupt_evidence)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("post-publication KeyboardInterrupt was swallowed")
        finally:
            materialize._git = original_git
        publish_payload = json.loads(publish_interrupt_evidence.read_text())
        assert publish_payload["result_ref_status"].startswith("rollback:"), publish_payload
        assert_transaction_recovered(repo, temp, publish_payload, publish_interrupt_ref, existing, base)

        def evidence_failure() -> None:
            def failing(path: Path, payload: dict[str, object]) -> None: raise OSError("controlled evidence failure")
            materialize._write_evidence = failing
        # There is deliberately no evidence file when its atomic write fails; the ref still rolls back.
        try:
            evidence_failure(); assert_failure(repo, lock, result_ref="refs/west/patch-stack-results/evidence", evidence=temp / "evidence.json", text="evidence")
        finally:
            materialize._run, materialize._git, materialize._write_evidence, materialize.shutil.rmtree = original_run, original_git, original_write, original_rmtree

        shutil.rmtree(foreign_root)

        print("patch-stack materialize contract: PASS")


if __name__ == "__main__":
    main()

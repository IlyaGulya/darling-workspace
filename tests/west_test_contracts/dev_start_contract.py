#!/usr/bin/env python3
"""Behavioral contract for exact, independent ``west dev start`` checkouts."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import dev_start


REAL_GIT = shutil.which("git")
assert REAL_GIT is not None, "git is required by this contract"
GIT_ENV: dict[str, str] = {}


def git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [REAL_GIT, *args],
        cwd=repo,
        env=GIT_ENV,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git(repo: Path, *args: str) -> str:
    result = git_result(repo, *args)
    assert result.returncode == 0, (repo, args, result.stdout, result.stderr)
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    revision = git(repo, "rev-parse", "HEAD")
    assert len(revision) == 40 and set(revision) <= set("0123456789abcdef"), revision
    return revision


def initialize_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Dev Start Contract")
    git(repo, "config", "user.email", "dev-start@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")


def filesystem_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Capture bytes and mutation-relevant metadata, excluding access times."""
    snapshot: dict[str, tuple[Any, ...]] = {}
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
    for path in paths:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        common = (
            stat.S_IFMT(metadata.st_mode),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_nlink,
        )
        if stat.S_ISREG(metadata.st_mode):
            payload = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(metadata.st_mode):
            payload = os.readlink(path)
        else:
            payload = ""
        snapshot[relative] = (*common, payload)
    return snapshot


def git_facts(repositories: list[Path]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for repo in repositories:
        status_result = git_result(repo, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all")
        assert status_result.returncode == 0, status_result.stderr
        facts.append(
            {
                "head": git(repo, "rev-parse", "HEAD"),
                "tree": git(repo, "rev-parse", "HEAD^{tree}"),
                "branch": git(repo, "symbolic-ref", "--short", "HEAD"),
                "refs": git(repo, "for-each-ref", "--format=%(refname)%00%(objectname)"),
                "status": status_result.stdout,
            }
        )
    return facts


def resolved_git_path(repo: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = repo / path
    return path.resolve(strict=True)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def object_file_identities(repo: Path) -> set[tuple[int, int]]:
    objects = resolved_git_path(repo, git(repo, "rev-parse", "--git-path", "objects"))
    identities = {
        (entry.stat().st_dev, entry.stat().st_ino)
        for entry in objects.rglob("*")
        if entry.is_file() and not entry.is_symlink()
    }
    assert identities, f"empty object database: {objects}"
    return identities


def assert_independent_complete_clone(repo: Path, revision: str) -> None:
    repo_root = repo.resolve(strict=True)
    assert git(repo, "rev-parse", "--show-toplevel") == str(repo_root)
    assert git(repo, "rev-parse", "HEAD") == revision
    assert git(repo, "rev-parse", "--is-shallow-repository") == "false"

    git_dir = resolved_git_path(repo, git(repo, "rev-parse", "--git-dir"))
    common_dir = resolved_git_path(repo, git(repo, "rev-parse", "--git-common-dir"))
    objects = resolved_git_path(repo, git(repo, "rev-parse", "--git-path", "objects"))
    assert git_dir == common_dir, (git_dir, common_dir)
    assert is_within(common_dir, repo_root), (common_dir, repo_root)
    assert is_within(objects, common_dir), (objects, common_dir)

    shallow = git(repo, "rev-parse", "--git-path", "shallow")
    shallow_path = Path(shallow) if Path(shallow).is_absolute() else repo / shallow
    assert not shallow_path.exists() and not shallow_path.is_symlink(), shallow_path
    for name in ("alternates", "http-alternates"):
        candidate_text = git(repo, "rev-parse", "--git-path", f"objects/info/{name}")
        candidate = Path(candidate_text) if Path(candidate_text).is_absolute() else repo / candidate_text
        assert not candidate.exists() and not candidate.is_symlink(), candidate

    partial = git_result(repo, "config", "--local", "--get", "extensions.partialClone")
    promisors = git_result(repo, "config", "--local", "--get-regexp", r"^remote\..*\.promisor$")
    assert partial.returncode == 1 and not partial.stdout, partial
    assert promisors.returncode == 1 and not promisors.stdout, promisors
    assert not list(objects.glob("pack/*.promisor")), objects

    missing = git(repo, "rev-list", "--objects", "--missing=print", revision)
    assert all(not line.startswith("?") for line in missing.splitlines()), missing
    git(repo, "cat-file", "-e", f"{revision}^{{commit}}")
    git(repo, "fsck", "--connectivity-only", "--no-dangling", revision)


def assert_result_schema(results: list[dict[str, Any]]) -> None:
    required = {"name", "returncode", "duration_ns", "stdout_tail", "stderr_tail"}
    for result in results:
        assert required <= set(result) <= required | {"resolved_commit"}, result
        assert isinstance(result["name"], str) and isinstance(result["returncode"], int), result
        assert isinstance(result["duration_ns"], int) and result["duration_ns"] >= 0, result
        assert len(result["stdout_tail"].encode("utf-8")) <= dev_start._EVIDENCE_TAIL, result
        assert len(result["stderr_tail"].encode("utf-8")) <= dev_start._EVIDENCE_TAIL, result


def assert_plan(
    plan: dict[str, Any],
    source: Path,
    destination: Path,
    evidence: Path,
    outer_revision: str,
    nested_revision: str,
    branch: str,
) -> None:
    assert set(plan) == dev_start._TOP_LEVEL_FIELDS, plan
    assert plan["schema_version"] == dev_start.SCHEMA_VERSION == 1
    assert plan["operation"] == "start" and plan["state"] == "planned"
    assert len(plan["transaction_id"]) == 32 and set(plan["transaction_id"]) <= set("0123456789abcdef")
    assert plan["created_paths"] == [] and plan["destination_identity"] is None
    assert plan["quarantine_identity"] is None
    assert plan["evidence_identity"] is None and plan["evidence_generation"] == 0
    assert plan["returncode"] is None
    assert plan["next_safe_action"] == "execute_start"

    inputs = plan["inputs"]
    assert set(inputs) == {
        "source", "destination", "evidence", "requested_base", "resolved_base",
        "branch", "bead", "module", "destination_parent_identity",
        "evidence_parent_identity", "repositories", "forbidden_roots",
    }
    assert inputs["source"] == str(source.resolve())
    assert inputs["destination"] == str(destination.resolve())
    assert inputs["evidence"] == str(evidence.resolve())
    assert inputs["requested_base"] == "refs/heads/main"
    assert inputs["resolved_base"] == outer_revision
    assert inputs["branch"] == branch and inputs["bead"] == "bd-contract"
    assert inputs["module"] == "modules/widget"
    assert inputs["forbidden_roots"] == [str(source.resolve())]
    repositories = inputs["repositories"]
    assert [(item["relative_path"], item["revision"]) for item in repositories] == [
        (".", outer_revision),
        ("modules/widget", nested_revision),
    ]
    assert [item["source"] for item in repositories] == [
        str(source.resolve()),
        str((source / "modules/widget").resolve()),
    ]
    for item in repositories:
        assert set(item) == {"relative_path", "source", "revision", "source_identity"}
        assert set(item["source_identity"]) == {"device", "inode"}

    mutation_names = [step["name"] for step in plan["steps"] if step["mutating"]]
    assert mutation_names == [
        "clone:darling",
        "checkout_detached:darling",
        "clone:modules/widget",
        "checkout_detached:modules/widget",
        "activate_branch:modules/widget",
    ], mutation_names
    assert "activate_branch:darling" not in {step["name"] for step in plan["steps"]}
    assert all(step["argv"][0] == "git" and "worktree" not in step["argv"] for step in plan["steps"])
    assert len(plan["steps"]) == len({step["name"] for step in plan["steps"]})
    result_names = {result["name"] for result in plan["results"]}
    assert not result_names.intersection(mutation_names), result_names
    resolution = [result for result in plan["results"] if result["name"] == "resolve_base"]
    assert len(resolution) == 1 and resolution[0]["resolved_commit"] == outer_revision
    assert_result_schema(plan["results"])


def build_plan(
    source: Path,
    destination: Path,
    evidence: Path,
    outer_revision: str,
    nested_revision: str,
    branch: str,
) -> dict[str, Any]:
    before_entries = sorted(path.name for path in destination.parent.iterdir())
    plan = dev_start.build_start_plan(
        source,
        destination,
        "refs/heads/main",
        branch,
        "bd-contract",
        "modules/widget",
        evidence,
        [source],
    )
    after_entries = sorted(path.name for path in destination.parent.iterdir())
    assert before_entries == after_entries, "planning created a filesystem path"
    staging = dev_start._staging_path(destination.resolve(), plan["transaction_id"])
    assert not destination.exists() and not destination.is_symlink()
    assert not evidence.exists() and not evidence.is_symlink()
    assert not evidence.with_name(evidence.name + ".tmp").exists()
    assert not staging.exists() and not staging.is_symlink()
    assert_plan(plan, source, destination, evidence, outer_revision, nested_revision, branch)
    return plan


def write_git_wrapper(path: Path) -> None:
    script = f"""#!{sys.executable}
import json
import os
import sys

real_git = os.environ["CONTRACT_REAL_GIT"]
arguments = sys.argv[1:]
log = os.environ.get("START_COMMAND_LOG")
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({{"argv": arguments, "no_replace": os.environ.get("GIT_NO_REPLACE_OBJECTS")}}) + "\\n")
target = os.environ.get("START_FAIL_TARGET")
if target and "clone" in arguments and arguments[-1] == target:
    sys.stderr.write("discarded-prefix:" + "x" * 12000 + "\\nCONTROLLED_NESTED_CLONE_FAILURE\\n")
    raise SystemExit(73)
os.execv(real_git, [real_git, *arguments])
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)


def main() -> None:
    original_environment = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory(prefix="dev-start-contract-") as temporary:
            temp = Path(temporary)
            home = temp / "home"
            xdg = temp / "xdg"
            scratch = temp / "tmp"
            wrapper_dir = temp / "bin"
            for directory in (home, xdg, scratch, wrapper_dir):
                directory.mkdir()
            wrapper = wrapper_dir / "git"
            write_git_wrapper(wrapper)

            os.environ.clear()
            os.environ.update(
                {
                    "PATH": f"{wrapper_dir}:/usr/bin:/bin",
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(xdg),
                    "TMPDIR": str(scratch),
                    "LC_ALL": "C",
                    "CONTRACT_REAL_GIT": REAL_GIT,
                }
            )
            GIT_ENV.clear()
            GIT_ENV.update(os.environ)
            GIT_ENV.update(
                {
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_DEFAULT_HASH": "sha1",
                    "GIT_AUTHOR_NAME": "Dev Start Contract",
                    "GIT_AUTHOR_EMAIL": "dev-start@example.invalid",
                    "GIT_COMMITTER_NAME": "Dev Start Contract",
                    "GIT_COMMITTER_EMAIL": "dev-start@example.invalid",
                }
            )

            source = temp / "canonical"
            initialize_repo(source)
            nested = source / "modules/widget"
            initialize_repo(nested)
            (nested / "widget.txt").write_bytes(b"canonical widget bytes\n")
            nested_revision = commit(nested, "nested base")
            (source / "outer.txt").write_bytes(b"canonical outer bytes\n")
            outer_revision = commit(source, "record nested gitlink")
            (source / "second.txt").write_bytes(b"exact selected base\n")
            outer_revision = commit(source, "outer exact base")
            assert git(source, "ls-tree", "HEAD", "modules/widget").split()[:3] == [
                "160000", "commit", nested_revision
            ]

            # An object that exists locally but is unreachable from all advertised
            # refs cannot be promised to a clone and must fail during pure planning.
            git(source, "checkout", "-q", "-b", "unadvertised")
            (source / "unadvertised.txt").write_bytes(b"locally present only\n")
            unadvertised_revision = commit(source, "unadvertised exact commit")
            git(source, "checkout", "-q", "main")
            git(source, "branch", "-D", "unadvertised")
            assert git(source, "cat-file", "-t", unadvertised_revision) == "commit"

            source_repositories = [source, nested]
            source_facts_before = git_facts(source_repositories)
            assert all(fact["status"] == "" for fact in source_facts_before)
            source_bytes_before = filesystem_snapshot(source)

            sentinel_file = temp / "KEEP-neighbor.txt"
            sentinel_file.write_bytes(b"neighbor must survive every transaction\n")
            sentinel_dir = temp / "KEEP-neighbor-dir"
            sentinel_dir.mkdir()
            (sentinel_dir / "payload").write_bytes(b"foreign payload\n")
            sentinel_file_before = filesystem_snapshot(sentinel_file)
            sentinel_dir_before = filesystem_snapshot(sentinel_dir)

            # Forbidden active-West roots are rejected before Git inspection or
            # transaction writes, including the generated staging pathname.
            active_west = temp / "active-west"
            initialize_repo(active_west)
            (active_west / "sentinel").write_bytes(b"active repository sentinel\n")
            commit(active_west, "active West root")

            def reject_forbidden(
                candidate_destination: Path,
                candidate_evidence: Path,
                forbidden_root: Path,
            ) -> None:
                before_root = filesystem_snapshot(forbidden_root)
                before_entries = sorted(path.name for path in temp.iterdir())
                try:
                    dev_start.build_start_plan(
                        source,
                        candidate_destination,
                        "refs/heads/main",
                        "dev/forbidden-contract",
                        "bd-contract",
                        "modules/widget",
                        candidate_evidence,
                        [forbidden_root],
                    )
                except dev_start.StartError as error:
                    assert "inside active West repository" in str(error), error
                else:
                    raise AssertionError("accepted output inside an active West root")
                assert filesystem_snapshot(forbidden_root) == before_root
                assert sorted(path.name for path in temp.iterdir()) == before_entries
                assert not candidate_destination.exists() and not candidate_destination.is_symlink()
                assert not candidate_evidence.exists() and not candidate_evidence.is_symlink()

            reject_forbidden(active_west / "destination", temp / "forbidden-destination.json", active_west)
            reject_forbidden(temp / "outside-destination", active_west / "evidence.json", active_west)

            fixed_transaction = "a" * 32
            staged_destination = temp / "staging-output"
            staged_root = dev_start._staging_path(staged_destination, fixed_transaction)
            initialize_repo(staged_root)
            (staged_root / "sentinel").write_bytes(b"staging root sentinel\n")
            commit(staged_root, "active West staging root")
            original_uuid4 = dev_start.uuid.uuid4

            class FixedUuid:
                hex = fixed_transaction

            dev_start.uuid.uuid4 = lambda: FixedUuid()
            try:
                reject_forbidden(
                    staged_destination,
                    temp / "forbidden-staging.json",
                    staged_root,
                )
            finally:
                dev_start.uuid.uuid4 = original_uuid4

            # A raw exact OID is resolvable locally, but is not clone-transferable
            # when no advertised head or tag reaches it.
            unreachable_destination = temp / "unreachable-checkout"
            unreachable_evidence = temp / "unreachable.json"
            before_unreachable_entries = sorted(path.name for path in temp.iterdir())
            try:
                dev_start.build_start_plan(
                    source,
                    unreachable_destination,
                    unadvertised_revision,
                    "dev/unadvertised-contract",
                    "bd-contract",
                    "modules/widget",
                    unreachable_evidence,
                    [source],
                )
            except dev_start.StartError as error:
                assert "unreachable from every advertised ref" in str(error), error
            else:
                raise AssertionError("planned a clone for a locally present unadvertised commit")
            assert sorted(path.name for path in temp.iterdir()) == before_unreachable_entries
            assert not unreachable_destination.exists() and not unreachable_evidence.exists()

            destination = temp / "checkout"
            evidence = temp / "start-success.json"
            branch = "dev/bd-contract"
            success_command_log = temp / "success-git-environment.jsonl"
            success_command_log.touch()
            os.environ["START_COMMAND_LOG"] = str(success_command_log)
            try:
                plan = build_plan(
                    source, destination, evidence, outer_revision, nested_revision, branch
                )
                assert filesystem_snapshot(source) == source_bytes_before, "planning mutated canonical bytes or metadata"
                committed = dev_start.execute_start(plan)
            finally:
                os.environ.pop("START_COMMAND_LOG", None)
            success_calls = [
                json.loads(line)
                for line in success_command_log.read_text(encoding="utf-8").splitlines()
            ]
            assert success_calls and all(call["no_replace"] == "1" for call in success_calls), success_calls
            persisted = json.loads(evidence.read_text(encoding="utf-8"))
            assert committed == persisted
            assert set(committed) == dev_start._TOP_LEVEL_FIELDS
            assert committed["state"] == "committed"
            assert committed["returncode"] == 0
            assert committed["next_safe_action"] == "use_destination"
            assert committed["created_paths"] == [str(destination)]
            assert committed["destination_identity"] == {
                "device": destination.stat().st_dev,
                "inode": destination.stat().st_ino,
            }
            assert committed["quarantine_identity"] is None
            assert committed["evidence_generation"] > 0
            assert committed["evidence_identity"] == {
                "device": evidence.stat().st_dev,
                "inode": evidence.stat().st_ino,
            }
            assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
            lock = evidence.with_name(f".{evidence.name}.lock")
            assert lock.is_file() and stat.S_IMODE(lock.stat().st_mode) == 0o600
            assert evidence.read_bytes().endswith(b"\n") and evidence.stat().st_size <= dev_start._EVIDENCE_LIMIT
            assert not evidence.with_name(evidence.name + ".tmp").exists()
            assert not (destination / dev_start._MARKER_NAME).exists()
            assert_result_schema(committed["results"])
            terminal = [item for item in committed["results"] if item["name"] == "start_transaction"]
            assert len(terminal) == 1 and terminal[0]["returncode"] == 0, terminal

            destination_nested = destination / "modules/widget"
            assert_independent_complete_clone(destination, outer_revision)
            assert_independent_complete_clone(destination_nested, nested_revision)
            assert git(destination, "status", "--porcelain=v1", "--untracked-files=all") == ""
            assert git(destination_nested, "status", "--porcelain=v1", "--untracked-files=all") == ""
            assert sum(item["name"] == "inspect_gitlinks:darling" for item in committed["results"]) >= 3
            assert sum(item["name"] == "inspect_gitlinks:modules/widget" for item in committed["results"]) >= 3
            detached = git_result(destination, "symbolic-ref", "-q", "--short", "HEAD")
            assert detached.returncode == 1 and detached.stdout == "", detached
            assert git(destination, "for-each-ref", "--format=%(refname)", "refs/heads") == ""
            assert git(destination_nested, "symbolic-ref", "--short", "HEAD") == branch
            assert git(destination_nested, "for-each-ref", "--format=%(refname)", "refs/heads") == f"refs/heads/{branch}"
            assert git_result(source, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 1
            assert git_result(nested, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 1
            assert (destination / "outer.txt").read_bytes() == (source / "outer.txt").read_bytes()
            assert (destination_nested / "widget.txt").read_bytes() == (nested / "widget.txt").read_bytes()
            assert (destination / "outer.txt").stat().st_ino != (source / "outer.txt").stat().st_ino
            assert (destination_nested / "widget.txt").stat().st_ino != (nested / "widget.txt").stat().st_ino

            source_outer_objects = object_file_identities(source)
            source_nested_objects = object_file_identities(nested)
            clone_outer_objects = object_file_identities(destination)
            clone_nested_objects = object_file_identities(destination_nested)
            assert clone_outer_objects.isdisjoint(source_outer_objects)
            assert clone_nested_objects.isdisjoint(source_nested_objects)
            assert clone_outer_objects.isdisjoint(clone_nested_objects)

            destination_before_recover = filesystem_snapshot(destination)
            evidence_before_recover = evidence.read_bytes()
            recovered_committed = dev_start.recover_start(evidence)
            assert recovered_committed == committed
            assert filesystem_snapshot(destination) == destination_before_recover
            assert evidence.read_bytes() == evidence_before_recover

            failed_destination = temp / "failed-checkout"
            failed_evidence = temp / "start-failed.json"
            failed_plan = build_plan(
                source,
                failed_destination,
                failed_evidence,
                outer_revision,
                nested_revision,
                "dev/failure-contract",
            )
            failed_staging = dev_start._staging_path(failed_destination.resolve(), failed_plan["transaction_id"])
            command_log = temp / "failure-git-argv.jsonl"
            os.environ["START_FAIL_TARGET"] = str(failed_staging / "modules/widget")
            os.environ["START_COMMAND_LOG"] = str(command_log)
            try:
                failed_payload = dev_start.execute_start(failed_plan)
            finally:
                os.environ.pop("START_FAIL_TARGET", None)
                os.environ.pop("START_COMMAND_LOG", None)
            assert failed_payload["state"] == "rolled_back"
            assert failed_payload["returncode"] == 73

            calls = [json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()]
            expected_failed_argv = dev_start._clone_argv(
                nested.resolve(), failed_staging / "modules/widget"
            )[1:]
            matching_failed_calls = [call for call in calls if call["argv"] == expected_failed_argv]
            assert len(matching_failed_calls) == 1, calls
            assert all(call["no_replace"] == "1" for call in calls), calls
            assert json.loads(failed_evidence.read_text(encoding="utf-8")) == failed_payload
            assert failed_payload["next_safe_action"] == "build_a_new_start_plan"
            assert failed_payload["created_paths"] == [] and failed_payload["destination_identity"] is None
            clone_failure = [item for item in failed_payload["results"] if item["name"] == "clone:modules/widget"]
            assert len(clone_failure) == 1 and clone_failure[0]["returncode"] == 73, clone_failure
            assert "CONTROLLED_NESTED_CLONE_FAILURE" in clone_failure[0]["stderr_tail"]
            assert "discarded-prefix:" not in clone_failure[0]["stderr_tail"]
            assert len(clone_failure[0]["stderr_tail"].encode("utf-8")) <= dev_start._EVIDENCE_TAIL
            failed_terminal = [item for item in failed_payload["results"] if item["name"] == "start_transaction"]
            assert len(failed_terminal) == 1 and failed_terminal[0]["returncode"] == 73, failed_terminal
            assert not failed_destination.exists() and not failed_destination.is_symlink()
            assert not failed_staging.exists() and not failed_staging.is_symlink()
            failed_quarantine = dev_start._quarantine_path(
                failed_destination.resolve(), failed_plan["transaction_id"]
            )
            assert not failed_quarantine.exists() and not failed_quarantine.is_symlink()
            assert not failed_evidence.with_name(failed_evidence.name + ".tmp").exists()

            # The destination can race into existence after the explicit check.
            # Atomic no-replace publication must preserve it byte-for-byte rather
            # than overwrite it, and rollback may remove only owned staging.
            race_destination = temp / "publication-race"
            race_evidence = temp / "publication-race.json"
            race_plan = build_plan(
                source,
                race_destination,
                race_evidence,
                outer_revision,
                nested_revision,
                "dev/publication-race",
            )
            race_staging = dev_start._staging_path(race_destination.resolve(), race_plan["transaction_id"])
            race_quarantine = dev_start._quarantine_path(race_destination.resolve(), race_plan["transaction_id"])
            original_rename_noreplace = dev_start._rename_noreplace
            foreign_publication_snapshots: list[dict[str, tuple[Any, ...]]] = []

            def inject_foreign_destination(source_path: Path, destination_path: Path) -> None:
                if source_path == race_staging and destination_path == race_destination:
                    race_destination.mkdir()
                    (race_destination / "FOREIGN-sentinel").write_bytes(
                        b"appeared between publication check and rename\n"
                    )
                    foreign_publication_snapshots.append(filesystem_snapshot(race_destination))
                original_rename_noreplace(source_path, destination_path)

            dev_start._rename_noreplace = inject_foreign_destination
            try:
                race_receipt = dev_start.execute_start(race_plan)
            finally:
                dev_start._rename_noreplace = original_rename_noreplace
            assert race_receipt["state"] == "failed" and race_receipt["returncode"] == 1, race_receipt
            assert race_receipt["next_safe_action"] == "inspect_ownership_before_manual_recovery"
            assert json.loads(race_evidence.read_text(encoding="utf-8")) == race_receipt
            assert len(foreign_publication_snapshots) == 1
            assert race_receipt["destination_identity"] != dev_start._identity(race_destination)
            assert filesystem_snapshot(race_destination) == foreign_publication_snapshots[0]
            assert not race_staging.exists() and not race_staging.is_symlink()
            assert not race_quarantine.exists() and not race_quarantine.is_symlink()
            race_terminal = [item for item in race_receipt["results"] if item["name"] == "start_transaction"]
            assert len(race_terminal) == 1 and race_terminal[0]["returncode"] == 1, race_terminal
            assert "no-replace rename destination already exists" in race_terminal[0]["stderr_tail"]
            assert "unexpected non-owned path remains after rollback" in race_terminal[0]["stderr_tail"]

            # Simulate a pathname swap while the owned transaction tree is moved
            # into the private quarantine's ``owned`` child. Identity verification
            # must stop cleanup with both the foreign child and displaced owned
            # tree intact.
            swap_destination = temp / "swap-checkout"
            swap_evidence = temp / "swap.json"
            swap_plan = build_plan(
                source,
                swap_destination,
                swap_evidence,
                outer_revision,
                nested_revision,
                "dev/swap-contract",
            )
            swap_staging = dev_start._staging_path(swap_destination.resolve(), swap_plan["transaction_id"])
            swap_quarantine = dev_start._quarantine_path(swap_destination.resolve(), swap_plan["transaction_id"])
            quarantined_tree = swap_quarantine / "owned"
            swap_staging.mkdir(mode=0o700)
            (swap_staging / "owned").write_bytes(b"transaction-owned bytes\n")
            owned_identity = dev_start._identity(swap_staging)
            owned_before_swap = filesystem_snapshot(swap_staging)
            held_owned = temp / "swap-owned-held"
            foreign_swap_snapshots: list[dict[str, tuple[Any, ...]]] = []
            active_swap = copy.deepcopy(swap_plan)
            active_swap["state"] = "active"
            active_swap["created_paths"] = [str(swap_staging)]
            active_swap["destination_identity"] = owned_identity
            active_swap["next_safe_action"] = "recover_start"
            dev_start._durable_write(swap_evidence, active_swap)

            def inject_quarantine_swap(source_path: Path, destination_path: Path) -> None:
                if source_path == swap_staging and destination_path == quarantined_tree:
                    os.rename(source_path, held_owned)
                    quarantined_tree.mkdir(mode=0o700)
                    (quarantined_tree / "FOREIGN-sentinel").write_bytes(
                        b"foreign pathname-swap bytes\n"
                    )
                    foreign_swap_snapshots.append(filesystem_snapshot(quarantined_tree))
                    return
                original_rename_noreplace(source_path, destination_path)

            dev_start._rename_noreplace = inject_quarantine_swap
            try:
                try:
                    dev_start._rollback(active_swap, swap_evidence)
                except dev_start.StartError as error:
                    assert "failed identity verification" in str(error), error
                else:
                    raise AssertionError("rollback deleted a pathname-swapped foreign tree")
            finally:
                dev_start._rename_noreplace = original_rename_noreplace
            assert len(foreign_swap_snapshots) == 1
            assert filesystem_snapshot(quarantined_tree) == foreign_swap_snapshots[0]
            assert filesystem_snapshot(held_owned) == owned_before_swap
            assert not swap_staging.exists() and not swap_staging.is_symlink()
            assert swap_quarantine.is_dir()
            assert stat.S_IMODE(swap_quarantine.stat().st_mode) == 0o700
            assert set(swap_quarantine.iterdir()) == {
                quarantined_tree,
                swap_quarantine / dev_start._MARKER_NAME,
            }
            swap_receipt = dev_start.recover_start(swap_evidence)
            assert swap_receipt["state"] == "failed"
            assert swap_receipt["returncode"] == 1
            assert swap_receipt["next_safe_action"] == "recover_start"
            assert filesystem_snapshot(quarantined_tree) == foreign_swap_snapshots[0]
            assert filesystem_snapshot(held_owned) == owned_before_swap
            swap_receipt = json.loads(swap_evidence.read_text(encoding="utf-8"))
            assert swap_receipt["state"] == "failed" and swap_receipt["returncode"] == 1
            assert swap_receipt["quarantine_identity"] == dev_start._identity(swap_quarantine)
            assert str(swap_quarantine) in swap_receipt["created_paths"]
            # Swapping the quarantine pathname after its descriptor is opened
            # must not redirect recursive deletion into the replacement tree.
            late_destination = temp / "late-swap-checkout"
            late_evidence = temp / "late-swap.json"
            late_plan = build_plan(
                source,
                late_destination,
                late_evidence,
                outer_revision,
                nested_revision,
                "dev/late-swap-contract",
            )
            late_staging = dev_start._staging_path(
                late_destination.resolve(), late_plan["transaction_id"]
            )
            late_quarantine = dev_start._quarantine_path(
                late_destination.resolve(), late_plan["transaction_id"]
            )
            late_staging.mkdir(mode=0o700)
            (late_staging / "owned").write_bytes(b"late-owned bytes\n")
            late_active = copy.deepcopy(late_plan)
            late_active["state"] = "active"
            late_active["created_paths"] = [str(late_staging)]
            late_active["destination_identity"] = dev_start._identity(late_staging)
            late_active["next_safe_action"] = "recover_start"
            dev_start._durable_write(late_evidence, late_active)
            original_rmtree = dev_start.shutil.rmtree
            held_quarantine = temp / "late-quarantine-held"
            late_foreign_snapshot: list[dict[str, tuple[Any, ...]]] = []

            def inject_late_quarantine_swap(path: Any, *args: Any, **kwargs: Any) -> None:
                if path == "owned" and kwargs.get("dir_fd") is not None:
                    os.rename(late_quarantine, held_quarantine)
                    late_quarantine.mkdir(mode=0o700)
                    (late_quarantine / "FOREIGN-sentinel").write_bytes(
                        b"late pathname replacement\n"
                    )
                    late_foreign_snapshot.append(filesystem_snapshot(late_quarantine))
                original_rmtree(path, *args, **kwargs)

            dev_start.shutil.rmtree = inject_late_quarantine_swap
            try:
                try:
                    dev_start._rollback(late_active, late_evidence)
                except dev_start.StartError as error:
                    assert "pathname was substituted" in str(error), error
                else:
                    raise AssertionError("late quarantine substitution was accepted")
            finally:
                dev_start.shutil.rmtree = original_rmtree
            assert len(late_foreign_snapshot) == 1
            assert filesystem_snapshot(late_quarantine) == late_foreign_snapshot[0]
            assert held_quarantine.is_dir()
            assert list(held_quarantine.iterdir()) == []

            # Identity-safe unlink first moves the pathname into an opened
            # private directory. A substitution at that move is restored, not
            # unlinked.
            unlink_target = temp / "unlink-target.json"
            unlink_target.write_bytes(b"expected inode\n")
            unlink_target.chmod(0o600)
            unlink_identity = dev_start._identity(unlink_target)
            unlink_held = temp / "unlink-held.json"
            original_rename_at = dev_start._rename_noreplace_at
            unlink_foreign_before: list[dict[str, tuple[Any, ...]]] = []

            def inject_unlink_swap(
                source_fd: int,
                source_name: str,
                destination_fd: int,
                destination_name: str,
            ) -> None:
                if source_name == unlink_target.name and destination_name == "victim":
                    os.rename(unlink_target, unlink_held)
                    unlink_target.write_bytes(b"foreign replacement\n")
                    unlink_target.chmod(0o600)
                    unlink_foreign_before.append(filesystem_snapshot(unlink_target))
                original_rename_at(
                    source_fd, source_name, destination_fd, destination_name
                )

            dev_start._rename_noreplace_at = inject_unlink_swap
            try:
                assert not dev_start._unlink_if_identity(
                    unlink_target, unlink_identity
                )
            finally:
                dev_start._rename_noreplace_at = original_rename_at
            assert filesystem_snapshot(unlink_target) == unlink_foreign_before[0]
            assert unlink_held.read_bytes() == b"expected inode\n"

            # A durable staging reservation makes the mkdir-to-marker crash
            # window recoverable when the exact reserved directory is empty.
            empty_destination = temp / "empty-crash-checkout"
            empty_evidence = temp / "empty-crash.json"
            empty_plan = build_plan(
                source,
                empty_destination,
                empty_evidence,
                outer_revision,
                nested_revision,
                "dev/empty-crash-contract",
            )
            empty_staging = dev_start._staging_path(
                empty_destination.resolve(), empty_plan["transaction_id"]
            )
            empty_active = copy.deepcopy(empty_plan)
            empty_active["state"] = "active"
            empty_active["created_paths"] = [str(empty_staging)]
            empty_active["next_safe_action"] = "recover_start"
            dev_start._durable_write(empty_evidence, empty_active)
            empty_staging.mkdir(mode=0o700)
            empty_receipt = dev_start.recover_start(empty_evidence)
            assert empty_receipt["state"] == "rolled_back"
            assert empty_receipt["returncode"] == 0
            assert not empty_staging.exists() and not empty_staging.is_symlink()
            assert not dev_start._quarantine_path(
                empty_destination.resolve(), empty_plan["transaction_id"]
            ).exists()
            # SIGINT during private cleanup returns a durable rc=130 receipt
            # while retaining recoverable ownership. A subsequent recovery
            # completes cleanup and preserves the interruption return code.
            sigint_destination = temp / "sigint-recovery-checkout"
            sigint_evidence = temp / "sigint-recovery.json"
            sigint_plan = build_plan(
                source,
                sigint_destination,
                sigint_evidence,
                outer_revision,
                nested_revision,
                "dev/sigint-recovery-contract",
            )
            sigint_staging = dev_start._staging_path(
                sigint_destination.resolve(), sigint_plan["transaction_id"]
            )
            sigint_staging.mkdir(mode=0o700)
            (sigint_staging / "owned").write_bytes(b"interrupt-owned bytes\n")
            sigint_active = copy.deepcopy(sigint_plan)
            sigint_active["state"] = "active"
            sigint_active["created_paths"] = [str(sigint_staging)]
            sigint_active["destination_identity"] = dev_start._identity(sigint_staging)
            sigint_active["next_safe_action"] = "recover_start"
            dev_start._durable_write(sigint_evidence, sigint_active)
            original_sigint_rmtree = dev_start.shutil.rmtree
            injected_sigint = False

            def interrupt_quarantine_cleanup(
                path: Any, *args: Any, **kwargs: Any
            ) -> None:
                nonlocal injected_sigint
                if (
                    not injected_sigint
                    and path == "owned"
                    and kwargs.get("dir_fd") is not None
                ):
                    injected_sigint = True
                    raise KeyboardInterrupt
                original_sigint_rmtree(path, *args, **kwargs)

            dev_start.shutil.rmtree = interrupt_quarantine_cleanup
            try:
                interrupted_receipt = dev_start.recover_start(sigint_evidence)
            finally:
                dev_start.shutil.rmtree = original_sigint_rmtree
            assert injected_sigint
            assert interrupted_receipt["state"] == "failed"
            assert interrupted_receipt["returncode"] == 130
            assert interrupted_receipt["next_safe_action"] == "recover_start"
            assert interrupted_receipt["created_paths"]
            recovered_after_sigint = dev_start.recover_start(sigint_evidence)
            assert recovered_after_sigint["state"] == "rolled_back"
            assert recovered_after_sigint["returncode"] == 130
            assert not sigint_staging.exists() and not sigint_staging.is_symlink()
            assert not dev_start._quarantine_path(
                sigint_destination.resolve(), sigint_plan["transaction_id"]
            ).exists()



            refusal_destination = temp / "foreign-checkout"
            refusal_evidence = temp / "active-refusal.json"
            refusal_plan = build_plan(
                source,
                refusal_destination,
                refusal_evidence,
                outer_revision,
                nested_revision,
                "dev/refusal-contract",
            )
            active = copy.deepcopy(refusal_plan)
            active["state"] = "active"
            active["next_safe_action"] = "recover_start"
            dev_start._durable_write(refusal_evidence, active)
            foreign_staging = dev_start._staging_path(refusal_destination.resolve(), active["transaction_id"])
            foreign_staging.mkdir(mode=0o700)
            foreign_payload = foreign_staging / "FOREIGN-sentinel"
            foreign_payload.write_bytes(b"not owned by the active journal\n")
            foreign_before = filesystem_snapshot(foreign_staging)
            refusal_receipt = dev_start.recover_start(refusal_evidence)
            assert refusal_receipt["state"] == "failed"
            assert refusal_receipt["returncode"] == 1
            assert refusal_receipt["next_safe_action"] == "inspect_ownership_before_manual_recovery"
            assert filesystem_snapshot(foreign_staging) == foreign_before
            assert not refusal_destination.exists() and not refusal_destination.is_symlink()
            refusal_payload = json.loads(refusal_evidence.read_text(encoding="utf-8"))
            assert refusal_payload["state"] == "failed"
            assert refusal_payload["returncode"] == 1
            assert refusal_payload["next_safe_action"] == "inspect_ownership_before_manual_recovery"
            refusal_result = [item for item in refusal_payload["results"] if item["name"] == "recover_start"]
            assert len(refusal_result) == 1 and refusal_result[0]["returncode"] == 1, refusal_result

            # Recovery refuses a byte-for-byte evidence substitution when the
            # pathname inode no longer matches the journal's recorded identity.
            substitution_destination = temp / "substitution-checkout"
            substitution_evidence = temp / "substitution.json"
            substitution_payload = build_plan(
                source,
                substitution_destination,
                substitution_evidence,
                outer_revision,
                nested_revision,
                "dev/substitution-contract",
            )
            dev_start._durable_write(substitution_evidence, substitution_payload)
            recorded_substitution_identity = dict(substitution_payload["evidence_identity"])
            original_substitution = temp / "substitution-original.json"
            os.rename(substitution_evidence, original_substitution)
            substituted_bytes = original_substitution.read_bytes()
            descriptor = os.open(
                substitution_evidence,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(substituted_bytes)
            foreign_evidence_before = filesystem_snapshot(substitution_evidence)
            original_evidence_before = filesystem_snapshot(original_substitution)
            assert dev_start._identity(substitution_evidence) != recorded_substitution_identity
            try:
                dev_start.recover_start(substitution_evidence)
            except dev_start.StartError as error:
                assert "main evidence inode differs from its recorded identity" in str(error), error
            else:
                raise AssertionError("recovery accepted substituted evidence")
            assert filesystem_snapshot(substitution_evidence) == foreign_evidence_before
            assert filesystem_snapshot(original_substitution) == original_evidence_before
            assert not substitution_destination.exists()

            # A valid next-generation temporary journal is atomically exchanged
            # into place, its displaced predecessor is removed by identity, and
            # public recovery then writes the next durable terminal generation.
            exchange_destination = temp / "exchange-checkout"
            exchange_evidence = temp / "exchange.json"
            exchange_main = build_plan(
                source,
                exchange_destination,
                exchange_evidence,
                outer_revision,
                nested_revision,
                "dev/exchange-contract",
            )
            dev_start._durable_write(exchange_evidence, exchange_main)
            main_evidence_identity = dict(exchange_main["evidence_identity"])
            exchange_temporary = exchange_evidence.with_name(exchange_evidence.name + ".tmp")
            descriptor = os.open(
                exchange_temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            temporary_metadata = os.fstat(descriptor)
            temporary_identity = {
                "device": temporary_metadata.st_dev,
                "inode": temporary_metadata.st_ino,
            }
            exchange_next = copy.deepcopy(exchange_main)
            exchange_next["state"] = "active"
            exchange_next["next_safe_action"] = "recover_start"
            exchange_next["evidence_generation"] = exchange_main["evidence_generation"] + 1
            exchange_next["evidence_identity"] = temporary_identity
            encoded_next = (
                json.dumps(exchange_next, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded_next)
                stream.flush()
                os.fsync(stream.fileno())
            promoted = dev_start._load_recoverable_evidence(exchange_evidence)
            assert promoted == exchange_next
            assert dev_start._identity(exchange_evidence) == temporary_identity
            assert dev_start._identity(exchange_evidence) != main_evidence_identity
            assert not exchange_temporary.exists() and not exchange_temporary.is_symlink()
            exchange_receipt = dev_start.recover_start(exchange_evidence)
            assert exchange_receipt["state"] == "rolled_back"
            assert exchange_receipt["returncode"] == 0
            assert exchange_receipt["evidence_generation"] == exchange_next["evidence_generation"] + 1
            assert exchange_receipt["evidence_identity"] == dev_start._identity(exchange_evidence)
            assert json.loads(exchange_evidence.read_text(encoding="utf-8")) == exchange_receipt
            assert not exchange_destination.exists() and not exchange_destination.is_symlink()

            assert filesystem_snapshot(sentinel_file) == sentinel_file_before
            assert filesystem_snapshot(sentinel_dir) == sentinel_dir_before
            assert filesystem_snapshot(source) == source_bytes_before, "execution or recovery mutated canonical source"
            assert git_facts(source_repositories) == source_facts_before, "canonical Git metadata changed"
            assert all(git(repo, "status", "--porcelain=v1", "--untracked-files=all") == "" for repo in source_repositories)

            print("dev start contract: PASS")
    finally:
        os.environ.clear()
        os.environ.update(original_environment)


if __name__ == "__main__":
    main()

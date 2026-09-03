#!/usr/bin/env python3
"""Focused behavioral contract for patch explain/check/status UX."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
try:
    from west.commands import WestCommand as _WestCommand
except ModuleNotFoundError:
    west_module = types.ModuleType("west")
    west_commands_module = types.ModuleType("west.commands")

    class _WestCommand:
        pass

    west_commands_module.WestCommand = _WestCommand
    west_module.commands = west_commands_module
    sys.modules["west"] = west_module
    sys.modules["west.commands"] = west_commands_module
import patch as patch_command
import patch_explain


class Plan(list):
    def __init__(self, entries, composition):
        super().__init__(entries)
        self.composition = composition


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def snapshot(repo: Path) -> tuple[str, str, str, str]:
    return (
        git(repo, "show-ref", "--head"),
        git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        git(repo, "rev-parse", "HEAD"),
        git(repo, "count-objects", "-v"),
    )


def write_lock(path: Path, base: str, source: str, tree: str) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "project": {"name": "synthetic", "path": "."},
                "upstream": {
                    "url": "https://example.invalid/upstream",
                    "base_commit": base,
                },
                "mirror": {
                    "url": "https://example.invalid/mirror",
                    "base_ref": f"refs/tags/patch-stack/v1/bases/{base}",
                    "base_oid": base,
                    "source_ref": f"refs/tags/patch-stack/v1/sources/{source}",
                    "source_oid": source,
                },
                "source_commit": source,
                "ordered_commits": [source],
                "expected_tree": tree,
            },
            sort_keys=False,
        )
    )


def make_plan(temp: Path, base: str, one: str, two: str, base_tree: str, one_tree: str, two_tree: str) -> Plan:
    lock_one = temp / "one.yml"
    lock_two = temp / "two.yml"
    write_lock(lock_one, base, one, one_tree)
    write_lock(lock_two, one, two, two_tree)
    entries = [
        {"module": "synthetic", "patch": "one.patch", "lock_path": str(lock_one)},
        {"module": "synthetic", "patch": "two.patch", "lock_path": str(lock_two)},
    ]
    composition = {
        "schema_version": 3,
        "prerequisites": [],
        "starts": {"synthetic": {"tree": base_tree}},
        "boundaries": {
            ("synthetic", "one.patch"): one_tree,
            ("synthetic", "two.patch"): two_tree,
        },
        "finals": {"synthetic": two_tree},
        "integration_finals": {"synthetic": two_tree},
    }
    return Plan(entries, composition)


def assert_gitlink_diff(temp: Path) -> None:
    child = temp / "gitlink-child"
    child.mkdir()
    git(child, "init", "-q")
    git(child, "config", "user.name", "Test")
    git(child, "config", "user.email", "test@example.invalid")
    (child / "value").write_text("one\n")
    git(child, "add", "value")
    git(child, "commit", "-qm", "one")
    child_one = git(child, "rev-parse", "HEAD")
    (child / "value").write_text("two\n")
    git(child, "commit", "-qam", "two")
    child_two = git(child, "rev-parse", "HEAD")
    child_tree = git(child, "rev-parse", "HEAD^{tree}")
    git(child, "branch", "integration/demo", child_two)

    parent = temp / "gitlink-parent"
    parent.mkdir()
    git(parent, "init", "-q")
    git(parent, "config", "user.name", "Test")
    git(parent, "config", "user.email", "test@example.invalid")
    (parent / "value").write_text("same\n")
    git(parent, "add", "value")
    git(parent, "update-index", "--add", "--cacheinfo", f"160000,{child_one},child")
    git(parent, "commit", "-qm", "typed tree")
    expected_tree = git(parent, "rev-parse", "HEAD^{tree}")
    git(parent, "update-index", "--cacheinfo", f"160000,{child_two},child")
    git(parent, "commit", "-qm", "generated gitlink")
    git(parent, "branch", "integration/demo", "HEAD")

    expected = {
        "darling": expected_tree,
        "darling/child": child_tree,
    }
    repos = {
        "darling": parent,
        "darling/child": child,
    }
    state = patch_explain.inspect_integration(
        "darling", parent, expected_tree, expected, repos, "integration/demo"
    )
    assert state["classification"] == "matched"
    git(child, "branch", "-D", "integration/demo")
    state = patch_explain.inspect_integration(
        "darling", parent, expected_tree, expected, repos, "integration/demo"
    )
    assert state["classification"] == "unavailable"
    assert state["recovery"] == "patch_apply"
    assert state["unavailable_module"] is None


class Die(RuntimeError):
    pass


def fake_command(output: list[str]):
    command = object.__new__(patch_command.DarlingPatch)
    command.name = "patch"
    command.description = "Apply tracked Darling patch profiles"
    command.inf = output.append

    def die(message, **_kwargs):
        raise Die(message)

    command.die = die
    return command


def assert_parser_contract() -> None:
    class Adder:
        def __init__(self):
            self.parser = argparse.ArgumentParser()

        def add_parser(self, _name, **_kwargs):
            return self.parser

    adder = Adder()
    fake_command([]).do_add_parser(adder)
    parsed = adder.parser.parse_args(["explain", "--profile", "demo", "--module", "m", "--full"])
    assert parsed.profile == "demo" and parsed.module == "m" and parsed.full
    try:
        adder.parser.parse_args(["explain", "--profile", "demo", "--module", "m", "--series", "p"])
    except SystemExit as error:
        assert error.code != 0
    else:
        raise AssertionError("mutually exclusive selectors were accepted")


def assert_negative_helpers(temp: Path) -> None:
    captured = {}
    original_run = patch_explain.subprocess.run

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 1, "", "")

    patch_explain.subprocess.run = fake_run
    try:
        patch_explain._git(temp, "show-ref")
    finally:
        patch_explain.subprocess.run = original_run
    assert captured["env"]["GIT_NO_LAZY_FETCH"] == "1"
    original_git = patch_explain._git
    patch_explain._git = lambda _repo, *_args: subprocess.CompletedProcess(
        [],
        128,
        "",
        "fatal: 'refs/heads/integration/demo' - not a valid ref",
    )
    try:
        assert patch_explain._branch_oid(temp, "integration/demo") is None
    finally:
        patch_explain._git = original_git

    original_available = patch_explain.repo_available
    original_branch = patch_explain._branch_oid
    original_tree = patch_explain._tree
    patch_explain.repo_available = lambda _repo: True
    patch_explain._branch_oid = lambda _repo, _branch: None
    try:
        state = patch_explain.inspect_integration(
            "darling",
            temp,
            "a" * 40,
            {"darling": "a" * 40, "darling/child": "b" * 40},
            {"darling": temp, "darling/child": None},
            "integration/demo",
        )
        assert state["classification"] == "missing"
        patch_explain._branch_oid = lambda _repo, _branch: "c" * 40
        patch_explain._tree = lambda _repo, _revision: None
        state = patch_explain.inspect_integration(
            "synthetic",
            temp,
            "a" * 40,
            {"synthetic": "a" * 40},
            {"synthetic": temp},
            "integration/demo",
        )
        assert state["classification"] == "unavailable"
        assert "unreadable" in state["detail"]
    finally:
        patch_explain.repo_available = original_available
        patch_explain._branch_oid = original_branch
        patch_explain._tree = original_tree

    graph = {
        "homebrew": {"profile": "homebrew", "prerequisites": []},
        "perf": {
            "profile": "perf",
            "prerequisites": [{"profile": "homebrew"}],
        },
        "arch": {
            "profile": "arch",
            "prerequisites": [{"profile": "perf"}],
        },
    }
    assert patch_explain.transitive_dependency_order(
        graph["arch"], lambda dependency: graph[dependency["profile"]]
    ) == ["homebrew", "perf"]

    output = []
    command = fake_command(output)
    args = argparse.Namespace(action="explain", json=True, profile="demo")
    try:
        command.do_run(args, ["--bogus"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("unknown diagnostic argument did not exit 2")
    assert len(output) == 1
    envelope = json.loads(output[0])
    assert envelope["schema_version"] == 1
    assert envelope["operation"] == "patch_explain"
    assert envelope["status"] == "error" and envelope["exit_code"] == 2

    output = []
    command = fake_command(output)
    command.manifest = types.SimpleNamespace(repo_abspath=str(temp))
    try:
        command.do_run(args, [])
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("missing profile did not exit 1")
    assert len(output) == 1
    envelope = json.loads(output[0])
    assert envelope["status"] == "error" and envelope["exit_code"] == 1


def assert_status_check_contract(repo: Path, plan: Plan, temp: Path) -> None:
    original_plan = patch_command.patch_stack_lock_first.plan
    patch_command.patch_stack_lock_first.plan = lambda *_args, **_kwargs: plan
    patches = [
        {"module": "synthetic", "path": f"patch-{index}.patch", "bead": f"b{index}"}
        for index in range(10)
    ]
    try:
        output: list[str] = []
        command = fake_command(output)
        command._projects = lambda: {
            "synthetic": types.SimpleNamespace(abspath=str(repo))
        }
        command._status("demo profile", patches, False)
        assert output[0] == (
            "status: 0 matched, 10 missing, 0 mismatched, 0 unavailable (of 10)"
        )
        assert output[1] == (
            "full details: west patch status --profile 'demo profile' --full"
        )
        assert len([line for line in output if line.startswith("MISSING")]) == 8
        assert output[-1] == "omitted: 2 finding(s)"

        output = []
        command = fake_command(output)
        command._projects = lambda: {
            "synthetic": types.SimpleNamespace(abspath=str(repo))
        }
        command._status("demo profile", patches, False, full=True)
        assert any("patch-9.patch" in line for line in output)
        assert not any(line.startswith("full details:") for line in output)

        output = []
        command = fake_command(output)
        command._projects = lambda: {
            "synthetic": types.SimpleNamespace(abspath=str(repo))
        }
        try:
            command._status("demo profile", patches, True, json_output=True)
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError("strict status succeeded for missing refs")
        assert len(output) == 1
        status_json = json.loads(output[0])
        assert status_json["schema_version"] == 1
        assert status_json["operation"] == "patch_status"
        assert len(status_json["items"]) == 10
        output = []
        command = fake_command(output)
        command._projects = lambda: {}
        try:
            command._status("demo profile", patches, True, json_output=True)
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError("strict status succeeded for an unavailable repository")
        assert len(output) == 1
        unavailable_json = json.loads(output[0])
        assert unavailable_json["summary"]["unavailable"] == 10
        assert unavailable_json["exit_code"] == 1
        assert {
            item["classification"] for item in unavailable_json["items"]
        } == {"unavailable"}

        profile_dir = temp / "demo profile"
        profile_dir.mkdir()
        output = []
        command = fake_command(output)
        command._validate_test_metadata = lambda _patch: []
        command._check(profile_dir, patches, False)
        assert output[0].startswith("test metadata: 0 covered")
        assert output[1] == (
            "full details: west patch check --profile 'demo profile' --full"
        )
        assert len([line for line in output if line.startswith("MISSING")]) == 8
        assert "omitted: 2 finding(s)" in output
        assert "full details: west patch check --profile 'demo profile' --full" in output

        output = []
        command = fake_command(output)
        command._validate_test_metadata = lambda _patch: []
        try:
            command._check(profile_dir, patches, True, json_output=True)
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError("strict check succeeded for missing metadata")
        assert len(output) == 1
        check_json = json.loads(output[0])
        assert check_json["schema_version"] == 1
        assert check_json["operation"] == "patch_check"
        assert len(check_json["items"]) == 10
    finally:
        patch_command.patch_stack_lock_first.plan = original_plan


def main() -> None:
    assert_parser_contract()
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        assert_negative_helpers(temp)
        assert_gitlink_diff(temp)
        repo = temp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
        (repo / "value").write_text("base\n")
        git(repo, "add", "value")
        git(repo, "commit", "-qm", "base")
        base = git(repo, "rev-parse", "HEAD")
        base_tree = git(repo, "rev-parse", "HEAD^{tree}")
        (repo / "value").write_text("one\n")
        git(repo, "commit", "-qam", "one")
        one = git(repo, "rev-parse", "HEAD")
        one_tree = git(repo, "rev-parse", "HEAD^{tree}")
        (repo / "value").write_text("two\n")
        git(repo, "commit", "-qam", "two")
        two = git(repo, "rev-parse", "HEAD")
        two_tree = git(repo, "rev-parse", "HEAD^{tree}")
        git(repo, "branch", "integration/demo", two)
        plan = make_plan(temp, base, one, two, base_tree, one_tree, two_tree)

        before = snapshot(repo)
        report = patch_explain.explain("demo", plan, {"synthetic": repo})
        assert snapshot(repo) == before, "explain mutated repository state"
        assert report["schema_version"] == 1 and report["operation"] == "patch_explain"
        assert report["dependency_order"] == []
        assert report["summary"] == {
            "total": 2,
            "matched": 2,
            "missing": 0,
            "mismatched": 0,
            "unavailable": 0,
            "integration": {
                "matched": 1,
                "missing": 0,
                "mismatched": 0,
                "unavailable": 0,
            },
        }
        assert [item["order"] for item in report["series"]] == [1, 2]
        assert [item["module_order"] for item in report["series"]] == [1, 2]
        assert report["series"][0]["base_commit"] == base
        assert report["series"][0]["base_tree"] == base_tree
        assert report["series"][1]["source_commit"] == two
        assert report["series"][1]["source_tree"] == two_tree
        assert report["series"][0]["starting_tree"] == base_tree
        assert report["series"][1]["expected_tree"] == two_tree
        assert report["series"][0]["applied_tree"] == one_tree
        assert report["series"][1]["applied_tree"] == two_tree
        assert json.dumps(report, sort_keys=True) == json.dumps(
            patch_explain.explain("demo", plan, {"synthetic": repo}), sort_keys=True
        )

        selected = patch_explain.explain("demo", plan, {"synthetic": repo}, module="synthetic")
        assert len(selected["series"]) == 2
        selected = patch_explain.explain("demo", plan, {"synthetic": repo}, series="two.patch")
        assert [item["series"] for item in selected["series"]] == ["two.patch"]
        assert (
            "full details: west patch explain --profile demo --series two.patch --full"
            in patch_explain.human_lines(selected)
        )
        git(repo, "checkout", "--detach", one)
        (repo / "value").write_text("wrong second boundary\n")
        git(repo, "commit", "-qam", "wrong second")
        wrong_second = git(repo, "rev-parse", "HEAD")
        git(repo, "branch", "-f", "integration/demo", wrong_second)
        first_only = patch_explain.explain(
            "demo", plan, {"synthetic": repo}, series="one.patch"
        )
        assert first_only["series"][0]["classification"] == "matched"
        assert (
            first_only["series"][0]["integration_classification"]
            == "mismatched"
        )
        git(repo, "branch", "-f", "integration/demo", two)
        for kwargs in ({"module": "absent"}, {"series": "absent.patch"}):
            try:
                patch_explain.explain("demo", plan, {"synthetic": repo}, **kwargs)
            except patch_explain.ExplainError:
                pass
            else:
                raise AssertionError(f"invalid selector accepted: {kwargs}")

        git(repo, "branch", "-D", "integration/demo")
        missing = patch_explain.explain("demo", plan, {"synthetic": repo})
        assert missing["summary"]["missing"] == 2
        git(repo, "branch", "integration/demo", one)
        mismatched = patch_explain.explain("demo", plan, {"synthetic": repo})
        assert mismatched["summary"]["mismatched"] == 2
        unavailable = patch_explain.explain("demo", plan, {})
        assert unavailable["summary"]["unavailable"] == 2
        assert unavailable["recommended_command"] == "west update synthetic"
        original_inspect = patch_explain.inspect_integration
        patch_explain.inspect_integration = lambda *_args, **_kwargs: {
            "applied_tree": two_tree,
            "classification": "unavailable",
            "detail": "dependency repository is locally unavailable",
            "unavailable_module": "darling/src/external/xnu",
            "recovery": "west_update",
        }
        try:
            dependency_missing = patch_explain.explain(
                "demo", plan, {"synthetic": repo}
            )
        finally:
            patch_explain.inspect_integration = original_inspect
        assert dependency_missing["recommended_command"] == (
            "west update darling/src/external/xnu"
        )
        patch_explain.inspect_integration = lambda *_args, **_kwargs: {
            "applied_tree": two_tree,
            "classification": "unavailable",
            "detail": "dependency integration ref/tree is locally unavailable",
            "unavailable_module": None,
            "recovery": "patch_apply",
        }
        try:
            generated_ref_missing = patch_explain.explain(
                "demo", plan, {"synthetic": repo}
            )
        finally:
            patch_explain.inspect_integration = original_inspect
        assert generated_ref_missing["recommended_command"] == (
            "west patch apply --profile demo --lock-first"
        )

        rows = [f"problem {index}" for index in range(10)]
        bounded = patch_explain.bounded_lines("explain", "demo profile", "summary", rows)
        assert bounded == [
            "summary",
            "full details: west patch explain --profile 'demo profile' --full",
            *rows[:8],
            "omitted: 2 finding(s)",
        ]
        assert patch_explain.bounded_lines(
            "check", "demo", "healthy summary", []
        ) == [
            "healthy summary",
            "full details: west patch check --profile demo --full",
        ]
        assert patch_explain.bounded_lines("explain", "demo", "summary", rows, full=True) == ["summary", *rows]

        git(repo, "branch", "-D", "integration/demo")
        assert_status_check_contract(repo, plan, temp)
    print("PASS patch-explain-contract")


if __name__ == "__main__":
    main()

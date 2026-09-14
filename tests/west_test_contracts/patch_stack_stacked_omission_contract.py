#!/usr/bin/env python3
"""A series omitted in one stacked phase must reach the phases that follow it.

Each stacked profile materializes one batch of a module's ordered series, and
every later batch continues from the tree the previous batch left behind.  A
current-minus proof omits one series, so the module no longer sits on the
canonical profile boundary once that series was skipped: the batches that
follow must still prove an exact native replay of their own immutable series,
but they must be compared with the tree the module actually started at instead
of with a composition boundary that necessarily contains the omitted change.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import test_runtime_source as runtime_source

MODULE = "darling/src/external/xnu"
PHASES = ("homebrew", "wget-residual")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def must_raise(expected, callback) -> BaseException:
    try:
        callback()
    except expected as error:
        return error
    raise AssertionError(f"expected {expected.__name__}")


def phase_plan(batch_id: str, entries: list[dict[str, str]], *,
               starts: str, boundaries: dict[str, str], final: str):
    """One typed batch whose declared boundaries assume every series applied."""
    composition = {
        "schema_version": 2,
        "path": f"{batch_id}.yml",
        "prerequisites": [],
        "frozen_manifest": {"path": "west.lock.yml", "sha256": "0" * 64},
        "starts": {MODULE: {"tree": starts}},
        "boundaries": {(MODULE, f"{MODULE}/{name}.patch"): tree for name, tree in boundaries.items()},
        "finals": {MODULE: final},
        "integration_finals": {MODULE: final},
    }
    return runtime_source.patch_stack_lock_first.LockFirstPlan(
        list(entries), {"batch_id": batch_id, "expected_count": len(entries)}, composition,
    )


def stacked_fixture(root: Path):
    """One module whose three series are split across two stacked phases.

    The series touch distinct files, so replaying a later series onto a tree
    that omitted an earlier one is a clean, exact replay - exactly the shape a
    current-minus proof relies on.
    """
    repo = root / "source"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Stacked Omission")
    git(repo, "config", "user.email", "stacked-omission@example.invalid")
    (repo / "base").write_text("base\n")
    git(repo, "add", "base")
    git(repo, "commit", "-qm", "base")
    revisions = {"base": git(repo, "rev-parse", "HEAD")}
    for name in ("omit", "keep", "next"):
        (repo / f"fixture-{name}").write_text(f"{name}\n")
        git(repo, "add", f"fixture-{name}")
        git(repo, "commit", "-qm", f"series {name}")
        revisions[name] = git(repo, "rev-parse", "HEAD")
    trees = {name: git(repo, "rev-parse", f"{oid}^{{tree}}") for name, oid in revisions.items()}
    mirror = root / "immutable.git"
    subprocess.run(["git", "init", "--bare", "-q", str(mirror)], check=True)
    for oid in revisions.values():
        git(repo, "tag", f"patch-stack/v1/bases/{oid}", oid)
        git(repo, "tag", f"patch-stack/v1/sources/{oid}", oid)
    git(repo, "push", "-q", str(mirror), "HEAD:refs/heads/main", "--tags")
    git(mirror, "symbolic-ref", "HEAD", "refs/heads/main")
    locks = {}
    for name, parent in (("omit", "base"), ("keep", "omit"), ("next", "keep")):
        source = revisions[name]
        locks[name] = root / f"{name}.yml"
        locks[name].write_text(yaml.safe_dump({
            "schema_version": 2,
            "project": {"name": MODULE, "path": "."},
            "upstream": {"url": mirror.as_uri(), "base_commit": revisions[parent]},
            "mirror": {
                "url": mirror.as_uri(),
                "base_ref": f"refs/tags/patch-stack/v1/bases/{revisions[parent]}",
                "base_oid": revisions[parent],
                "source_ref": f"refs/tags/patch-stack/v1/sources/{source}",
                "source_oid": source,
            },
            "source_commit": source,
            "ordered_commits": [source],
            "expected_tree": trees[name],
        }, sort_keys=False))
    entries = {
        name: {"profile": PHASES[0], "module": MODULE, "patch": f"{MODULE}/{name}.patch",
               "lock": locks[name].name, "lock_path": str(locks[name])}
        for name in ("omit", "keep", "next")
    }
    return types.SimpleNamespace(
        repo=repo, entries=entries, trees=trees,
        plans={
            PHASES[0]: phase_plan(
                "stacked-omission-homebrew", [entries["omit"], entries["keep"]],
                starts=trees["base"],
                boundaries={"omit": trees["omit"], "keep": trees["keep"]}, final=trees["keep"],
            ),
            PHASES[1]: phase_plan(
                "stacked-omission-wget-residual", [entries["next"]],
                starts=trees["keep"], boundaries={"next": trees["next"]}, final=trees["next"],
            ),
        },
    )


def stacked_materializer(fixture, patches):
    host = types.SimpleNamespace(
        inf=lambda _message: None,
        _profile_stack=lambda _profile: list(PHASES),
        _load_profile=lambda name: {"patches": patches[name]},
    )
    return runtime_source.RuntimeSourceMaterializer(host)


def staged_omit_plan(fixture) -> None:
    """Replay the first phase with its own last series omitted."""
    runtime_source.patch_stack_lock_first.materialize_batch_into(
        fixture.repo,
        [fixture.entries["omit"], fixture.entries["keep"]],
        reset_to_first_base=True,
        composition=fixture.plans[PHASES[0]].composition,
        skip_patches={fixture.entries["omit"]["patch"]},
    )


def undeclared_divergence_contract() -> None:
    """A skipped series is caller-owned state, not a licence for any tree.

    The batch primitive cannot tell an authorised omission from a wrong start
    tree, so a later batch that was not told about the omission must keep
    stopping on the declared composition boundary.
    """
    with tempfile.TemporaryDirectory() as directory:
        fixture = stacked_fixture(Path(directory))
        staged_omit_plan(fixture)
        assert not (fixture.repo / "fixture-omit").exists()
        error = must_raise(runtime_source.patch_stack_lock_first.LockFirstError, lambda:
            runtime_source.patch_stack_lock_first.materialize_batch_into(
                fixture.repo, [fixture.entries["next"]],
                composition=fixture.plans[PHASES[1]].composition,
            ))
        assert "boundary requires exact tree identity" in str(error)


def stacked_current_minus_contract() -> None:
    """An omission keeps its meaning across the phase boundary.

    The same omitted series lands on the same tree whether the remaining
    series are replayed in one batch or split over the stacked phases that
    the runtime profile actually declares - and that tree is not the declared
    canonical boundary, which still contains the omitted change.
    """
    with tempfile.TemporaryDirectory() as directory:
        fixture = stacked_fixture(Path(directory))
        results, _stats = runtime_source.patch_stack_lock_first.materialize_batch_into(
            fixture.repo,
            [fixture.entries["omit"], fixture.entries["keep"], fixture.entries["next"]],
            reset_to_first_base=True,
            skip_patches={fixture.entries["omit"]["patch"]},
        )
        reduced = git(fixture.repo, "rev-parse", "HEAD^{tree}")
        assert reduced == results[-1]["applied_tree"] != fixture.trees["next"]
        assert not (fixture.repo / "fixture-omit").exists()
        assert (fixture.repo / "fixture-next").read_text() == "next\n"

        patches = {
            PHASES[0]: [{"module": MODULE, "path": fixture.entries[name]["patch"]}
                        for name in ("omit", "keep")],
            PHASES[1]: [{"module": MODULE, "path": fixture.entries["next"]["patch"]}],
        }
        materializer = stacked_materializer(fixture, patches)
        with mock.patch.object(runtime_source.patch_stack_lock_first, "plan",
                               side_effect=lambda name, *_args: fixture.plans[name]):
            materializer.apply_profile_module_patches(
                "homebrew-lz4-source", MODULE, fixture.repo,
                skip_patch_paths={fixture.entries["omit"]["patch"]},
            )
        assert git(fixture.repo, "rev-parse", "HEAD^{tree}") == reduced
        assert not (fixture.repo / "fixture-omit").exists()
        assert (fixture.repo / "fixture-keep").read_text() == "keep\n"
        assert (fixture.repo / "fixture-next").read_text() == "next\n"


def canonical_phase_boundary_contract() -> None:
    """Without an omission every phase still ends on its declared boundary."""
    with tempfile.TemporaryDirectory() as directory:
        fixture = stacked_fixture(Path(directory))
        patches = {
            PHASES[0]: [{"module": MODULE, "path": fixture.entries[name]["patch"]}
                        for name in ("omit", "keep")],
            PHASES[1]: [{"module": MODULE, "path": fixture.entries["next"]["patch"]}],
        }
        materializer = stacked_materializer(fixture, patches)
        with mock.patch.object(runtime_source.patch_stack_lock_first, "plan",
                               side_effect=lambda name, *_args: fixture.plans[name]):
            materializer.apply_profile_module_patches("homebrew-lz4-source", MODULE, fixture.repo)
        assert git(fixture.repo, "rev-parse", "HEAD^{tree}") == fixture.trees["next"]


def main() -> None:
    undeclared_divergence_contract()
    stacked_current_minus_contract()
    canonical_phase_boundary_contract()
    print("patch-stack stacked omission contract: PASS")


if __name__ == "__main__":
    main()

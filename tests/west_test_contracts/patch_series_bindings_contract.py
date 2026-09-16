"""The export's binding report names what a series change leaves behind.

The report is what turns three late, unrelated failures - a receipt contract, a
tier registry contract, and a profile materialization - into one message at the
moment the export runs. Nothing here rewrites a binding: it is a report, so the
contract checks the four things it must get right - which binding is behind, the
commit the artifact now carries, the refspec that publishes it, and the closure
of profiles whose compositions must be reissued - plus silence when everything
already agrees, because a reminder that fires on a current tree is noise.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_series_bindings as bindings

OLD = "d" * 40
NEW = "c" * 40
MIRROR = "https://github.com/darling-next/darling.git"
PATCH = "darling/mldr-thread-create-futex-wait.patch"
LOCK = "darling-mldr-thread-create-futex-wait-v1.yml"


def exported_bytes(commit: str, count: int) -> bytes:
    return b"".join(f"From {commit} Mon Sep 17 00:00:00 2001\n\n".encode() for _ in range(count))


def build(root: Path, *, lock_commit: str, receipt_commit: str, receipt_count: int) -> None:
    locks = root / "locks" / "patch-stack"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "lock-first-profiles-v1.yml").write_text(yaml.safe_dump({"profiles": [
        {"profile": "homebrew", "mapping": "lock-first-series-v2.yml"},
    ]}))
    (locks / "lock-first-series-v2.yml").write_text(yaml.safe_dump({"profile": "homebrew", "series": [
        {"profile": "homebrew", "module": "darling", "patch": PATCH, "lock": LOCK},
    ]}))
    (locks / LOCK).write_text(yaml.safe_dump({
        "schema_version": 2,
        "source_commit": lock_commit,
        "ordered_commits": [OLD, NEW] if lock_commit == NEW else [OLD],
        "expected_tree": "e" * 40,
        "mirror": {"url": MIRROR, "source_oid": lock_commit, "source_ref": "refs/tags/" + lock_commit},
    }))
    (locks / "migration-inventory-v1.yml").write_text(yaml.safe_dump({"stacks": [{
        "lock": f"locks/patch-stack/{LOCK}",
        "source_commit": receipt_commit,
        "commit_count": receipt_count,
    }]}))
    # homebrew is the base; perf and wget-residual consume it directly, arch
    # consumes perf, so the dependents closure must reach arch through perf.
    for name, owner, prerequisites in (
        ("homebrew-profile-composition-v2.yml", "homebrew", []),
        ("perf-profile-composition-v2.yml", "perf", ["homebrew"]),
        ("wget-residual-profile-composition-v1.yml", "wget-residual", ["homebrew"]),
        ("arch-profile-composition-v2.yml", "arch", ["perf"]),
    ):
        (locks / name).write_text(yaml.safe_dump({
            "profile": owner,
            "prerequisites": [{"profile": profile} for profile in prerequisites],
            "modules": [],
        }))


with tempfile.TemporaryDirectory() as directory:
    locks_root = Path(directory) / "locks" / "patch-stack"

    # A lock and a receipt that still describe the one-commit series.
    build(Path(directory), lock_commit=OLD, receipt_commit=OLD, receipt_count=1)
    report = bindings.binding_report(
        locks_root, profile="homebrew", module="darling", patch=PATCH,
        commit=NEW, exported=exported_bytes(NEW, 2),
    )
    text = "\n".join(report)
    assert report, "a stale lock and receipt produced no report"
    assert f"{LOCK}: still records source_commit {OLD}" in text, text
    assert f"but {PATCH} now exports {NEW} (2 commit(s))" in text, text
    assert f"git push {MIRROR} {NEW}:refs/tags/patch-stack/v1/sources/{NEW}" in text, text
    assert "migration-inventory-v1.yml: row for" in text, text
    assert f"commit_count 1, not {NEW} with 2" in text, text
    assert "scripts/generate_profile_composition.py --profile homebrew" in text, text
    # The closure is transitive: arch is reached only through perf.
    for dependent in ("perf", "wget-residual", "arch"):
        assert dependent in text, text

    # A lock that already names the exported series is not a difference, but the
    # receipt is checked independently, so it can still be the stale one.
    build(Path(directory), lock_commit=NEW, receipt_commit=OLD, receipt_count=1)
    report = bindings.binding_report(
        locks_root, profile="homebrew", module="darling", patch=PATCH,
        commit=NEW, exported=exported_bytes(NEW, 2),
    )
    text = "\n".join(report)
    assert report and "still records source_commit" not in text, text
    assert "commit_count 1, not" in text, text

    # Everything agreeing is silence: a reminder on a current tree is noise.
    build(Path(directory), lock_commit=NEW, receipt_commit=NEW, receipt_count=2)
    assert bindings.binding_report(
        locks_root, profile="homebrew", module="darling", patch=PATCH,
        commit=NEW, exported=exported_bytes(NEW, 2),
    ) == []

    # A patch the profile's mapping does not lock, and a profile with no
    # mapping, have no immutable binding to report against.
    assert bindings.binding_report(
        locks_root, profile="homebrew", module="darling", patch="darling/other.patch",
        commit=NEW, exported=exported_bytes(NEW, 2),
    ) == []
    assert bindings.binding_report(
        locks_root, profile="not-a-profile", module="darling", patch=PATCH,
        commit=NEW, exported=exported_bytes(NEW, 2),
    ) == []

print("PASS patch-series-bindings-contract")

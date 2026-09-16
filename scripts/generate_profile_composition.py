#!/usr/bin/env python3
"""Derive a profile-composition lock from the immutable series it binds.

A schema-v3 composition lock records, for every module of a profile, the tree
the module starts from and the tree it produces after each locked patch. Those
values are derived: they are what replaying the locked immutable series
produces. Nothing derived them, though - they were written by hand - so a series
could not change without someone recomputing several files by hand, and the
failure mode of that is a materialization that refuses to run, or worse, a
receipt edited until it matched an error message.

This script derives them. It replays each profile's series through the same
lock-first machinery the materializer uses, recording the applied tree after
every patch, and renders the composition lock byte-for-byte in the checked-in
style. ``--check`` runs the identical derivation and only reports, naming the
field that disagrees.

Nothing here reads a value out of the checked-in lock: every tree comes from a
replay of the locked immutable refs or from the mapping and frozen manifest the
lock names. The lock stays a check - materialization still validates against it,
so a receipt written here cannot make a wrong series look right.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))

import patch_stack_lock_first as lock_first  # noqa: E402
import test_manifest  # noqa: E402


class DerivationError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise DerivationError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


def _module_repo(darling: Path, module: str) -> Path:
    """Return the repository a module name refers to."""
    if module == "darling":
        return darling
    prefix = "darling/"
    if not module.startswith(prefix):
        raise DerivationError(f"module {module!r} is outside the darling checkout")
    return darling / module[len(prefix):]


def _profile_order(profile: str) -> list[str]:
    """Return the profile graph in prerequisite-first order."""
    order: list[str] = []
    visiting: set[str] = set()

    def visit(current: str) -> None:
        if current in order:
            return
        if current in visiting:
            raise DerivationError(f"cyclic profile composition at {current}")
        visiting.add(current)
        mapping_path = lock_first.mapping_for_profile(current)
        metadata = lock_first.load_mapping(mapping_path, current)
        composition_path = mapping_path.parent / metadata["composition"]
        checked_in = yaml.safe_load(composition_path.read_text())
        for prerequisite in checked_in["prerequisites"]:
            visit(prerequisite["profile"])
        visiting.remove(current)
        order.append(current)

    visit(profile)
    return order


def _profile_inputs(profile: str) -> dict[str, Any]:
    mapping_path = lock_first.mapping_for_profile(profile)
    metadata = lock_first.load_mapping(mapping_path, profile)
    composition_path = mapping_path.parent / metadata["composition"]
    if composition_path.is_symlink() or not composition_path.is_file():
        raise DerivationError(f"{profile}: composition lock is absent: {composition_path.name}")
    profile_path = ROOT / "patches" / profile / "patches.yml"
    try:
        profile_data = test_manifest.load_test_profile(profile_path)
    except test_manifest.ManifestError as error:
        raise DerivationError(f"{profile}: {error}") from error
    patches = [
        {"module": item["module"], "path": item["path"]}
        for item in profile_data.get("patches", [])
        if isinstance(item, dict) and item.get("module") and item.get("path")
    ]
    if not patches:
        raise DerivationError(f"{profile}: declares no patch with a module")
    # The series order comes from the typed mapping; the entry list is built here
    # rather than through lock_first.plan because plan() binds the composition
    # lock, and a dependent profile's checked-in lock still records the digest of
    # the prerequisite file this run is about to reissue. Deriving from locks
    # that are known stale would make the reissue depend on the drift it exists
    # to remove.
    locks_root = mapping_path.parent
    seen: set[tuple[str, str]] = set()
    execution_order = [(patch["module"], patch["path"]) for patch in patches]
    entries: list[dict[str, Any]] = []
    for entry in metadata["series"]:
        key = (entry["module"], entry["patch"])
        if key in seen:
            raise DerivationError(f"{profile}: duplicate typed lock-first entry: {entry['patch']}")
        seen.add(key)
        matches = [position for position, candidate in enumerate(execution_order) if candidate == key]
        if len(matches) != 1:
            raise DerivationError(
                f"{profile}: allowlisted lock-first patch must occur exactly once: {entry['patch']}"
            )
        lock_path = locks_root / entry["lock"]
        if lock_path.is_symlink() or not lock_path.is_file():
            raise DerivationError(f"{profile}: lock is absent: {entry['lock']}")
        entries.append({
            "module": entry["module"],
            "patch": entry["patch"],
            "lock": entry["lock"],
            "lock_path": str(lock_path),
            "execution_index": str(matches[0]),
        })
    if len(entries) != metadata["expected_count"] or not entries:
        raise DerivationError(
            f"{profile}: mapping declares {metadata['expected_count']} entries, "
            f"resolved {len(entries)}"
        )
    return {
        "profile": profile,
        "mapping_path": mapping_path,
        "composition_path": composition_path,
        "metadata": metadata,
        "entries": entries,
        "module_order": list(dict.fromkeys(entry["module"] for entry in entries)),
    }


def _replay_profile(
    inputs: dict[str, Any],
    darling: Path,
    worktrees: dict[str, Path],
    scratch: Path,
) -> dict[str, dict[str, Any]]:
    """Replay one profile's series, recording the tree after every patch."""
    recorded: dict[str, dict[str, Any]] = {}
    for module in inputs["module_order"]:
        module_entries = [entry for entry in inputs["entries"] if entry["module"] == module]
        repo = _module_repo(darling, module)
        if not repo.is_dir():
            raise DerivationError(f"{inputs['profile']}: module checkout is absent: {repo}")
        fresh = module not in worktrees
        if fresh:
            worktree = scratch / module.replace("/", "__")
            _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
            worktrees[module] = worktree
        captured: dict[str, dict[str, Any]] = {}
        lock_first.materialize_batch_into(
            worktrees[module],
            module_entries,
            reset_to_first_base=fresh,
            record=captured,
        )
        if module not in captured:
            raise DerivationError(f"{inputs['profile']}: no boundary was recorded for {module}")
        recorded[module] = captured[module]
    return recorded


def _prerequisite_payload(
    prerequisite: str,
    derived_files: dict[str, bytes],
    compositions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Describe a prerequisite profile the way the validator expects it."""
    composition = compositions[prerequisite]
    name = composition["composition_path"].name
    trees = {
        module["module"]: (
            module["final_tree"] if module["module"] == "darling"
            else module["integration_final_tree"]
        )
        for module in composition["modules"]
    }
    return {
        "profile": prerequisite,
        "composition": name,
        "sha256": hashlib.sha256(derived_files[prerequisite]).hexdigest(),
        "frozen_manifest": {
            "path": composition["frozen_manifest"]["path"],
            "sha256": composition["frozen_manifest"]["sha256"],
        },
        "module_trees": trees,
    }


def _render(
    profile: str,
    recorded: dict[str, dict[str, Any]],
    inputs: dict[str, Any],
    derived_files: dict[str, bytes],
    compositions: dict[str, dict[str, Any]],
    prerequisites: list[str],
) -> bytes:
    frozen_manifest = {
        "path": inputs["frozen_manifest"]["path"],
        "sha256": inputs["frozen_manifest"]["sha256"],
    }
    modules: list[dict[str, Any]] = []
    for module in inputs["module_order"]:
        captured = recorded[module]
        series = [
            {
                "patch": entry["patch"],
                "lock": Path(entry["lock_path"]).name,
                "expected_applied_tree": captured["boundaries"][entry["patch"]],
            }
            for entry in inputs["entries"] if entry["module"] == module
        ]
        if set(captured["boundaries"]) != {entry["patch"] for entry in inputs["entries"] if entry["module"] == module}:
            raise DerivationError(f"{profile}: recorded boundaries do not match the series of {module}")
        final_tree = captured["boundaries"][series[-1]["patch"]]
        # A parent tree carries gitlink commit IDs, which are generated lifecycle
        # evidence; its integration boundary is its own content tree, so the two
        # are the same value for every module this derivation covers.
        modules.append({
            "module": module,
            "starting": {"tree": captured["starting"]},
            "series": series,
            "final_tree": final_tree,
            "integration_final_tree": final_tree,
        })
    payload = {
        "schema_version": 3,
        "profile": profile,
        "prerequisites": [
            _prerequisite_payload(name, derived_files, compositions) for name in prerequisites
        ],
        "frozen_manifest": frozen_manifest,
        "mapping": {
            "path": inputs["mapping_path"].name,
            "sha256": hashlib.sha256(inputs["mapping_path"].read_bytes()).hexdigest(),
            "batch_id": inputs["metadata"]["batch_id"],
            "expected_count": inputs["metadata"]["expected_count"],
        },
        "modules": modules,
    }
    return yaml.safe_dump(
        payload, sort_keys=False, indent=2, default_flow_style=False, width=1000
    ).encode()


def _differences(checked_in: Any, derived: Any, path: str = "") -> list[str]:
    """Return the field-level differences between two decoded documents."""
    if isinstance(checked_in, dict) and isinstance(derived, dict):
        lines: list[str] = []
        for key in dict.fromkeys([*checked_in, *derived]):
            child = f"{path}.{key}" if path else str(key)
            if key not in checked_in:
                lines.append(f"{child}: absent from the composition, derived={derived[key]!r}")
            elif key not in derived:
                lines.append(f"{child}: present in the composition, not derived")
            else:
                lines.extend(_differences(checked_in[key], derived[key], child))
        return lines
    if isinstance(checked_in, list) and isinstance(derived, list):
        lines = []
        if len(checked_in) != len(derived):
            lines.append(f"{path}: composition has {len(checked_in)} entries, derived {len(derived)}")
        for index, (left, right) in enumerate(zip(checked_in, derived)):
            lines.extend(_differences(left, right, f"{path}[{index}]"))
        return lines
    if checked_in != derived:
        return [f"{path}: composition={checked_in!r} derived={derived!r}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="homebrew",
                        help="profile to derive (its prerequisites are derived first)")
    parser.add_argument("--check", action="store_true",
                        help="report whether the composition lock is current without writing")
    parser.add_argument("--darling", type=Path, default=ROOT.parent / "darling",
                        help="path of the darling checkout the module series replay into")
    args = parser.parse_args()

    order = _profile_order(args.profile)
    scratch = Path(tempfile.mkdtemp(prefix="west-profile-composition-"))
    worktrees: dict[str, Path] = {}
    derived_files: dict[str, bytes] = {}
    compositions: dict[str, dict[str, Any]] = {}
    status = 0
    pending: list[tuple[Path, bytes]] = []
    try:
        for profile in order:
            inputs = _profile_inputs(profile)
            checked_in_bytes = inputs["composition_path"].read_bytes()
            checked_in = yaml.safe_load(checked_in_bytes)
            inputs["frozen_manifest"] = checked_in["frozen_manifest"]
            recorded = _replay_profile(inputs, args.darling, worktrees, scratch)
            rendered = _render(
                profile, recorded, inputs, derived_files, compositions,
                [item["profile"] for item in checked_in["prerequisites"]],
            )
            compositions[profile] = {
                "composition_path": inputs["composition_path"],
                "modules": yaml.safe_load(rendered)["modules"],
                "frozen_manifest": yaml.safe_load(rendered)["frozen_manifest"],
            }
            derived_files[profile] = rendered
            differences = _differences(checked_in, yaml.safe_load(rendered))
            style_only = not differences and checked_in_bytes != rendered
            if style_only:
                # Reissuing a receipt must not rewrite a file it did not change:
                # formatting churn in a generated registry is a tooling bug, not
                # review noise, so this refuses to write and says so.
                status = 1
                print(
                    f"{inputs['composition_path'].name}: values are current but the "
                    "rendered style differs; fix the renderer, do not rewrite the file",
                    file=sys.stderr,
                )
                continue
            if differences:
                # A write run that changes nothing exits zero; only --check
                # reports drift as a failure, because only --check promises not
                # to have fixed it.
                status = 1 if args.check else 0
                print(f"{inputs['composition_path'].name}: differs from its derivation", file=sys.stderr)
                for line in differences:
                    print(f"  {line}", file=sys.stderr)
                if not args.check:
                    pending.append((inputs["composition_path"], rendered))
            elif args.check:
                print(f"{inputs['composition_path'].name}: current")
            else:
                print(f"{inputs['composition_path'].name}: already current")
        # Every file in the chain is written only once the whole chain derived:
        # a half-updated set of receipts is worse than none, because the
        # prerequisite digests would refer to files that are not on disk.
        for path, rendered in pending:
            path.write_bytes(rendered)
            print(f"rewrote {path.name}", file=sys.stderr)
    finally:
        for module, worktree in worktrees.items():
            repo = _module_repo(args.darling, module)
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                           cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["rm", "-rf", str(scratch)], check=False)
    return status


if __name__ == "__main__":
    raise SystemExit(main())

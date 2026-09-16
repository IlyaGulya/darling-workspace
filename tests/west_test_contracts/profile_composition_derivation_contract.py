"""Behavioral contract for the profile-composition derivation.

The derivation itself replays every locked series, which costs minutes and an
immutable mirror, so this contract covers the decisions around it: which field a
drift is reported against, that a style-only difference is refused rather than
written, how a module name maps to its repository, and how a prerequisite is
described from the file this run derived rather than the one on disk. The
end-to-end gate for a stale receipt stays the profile materialization the host
tier already runs - that is what caught the drift this derivation now fixes.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "generate_profile_composition", ROOT / "scripts" / "generate_profile_composition.py"
)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)

# A drift names the field that disagrees, at the nesting it occurs in.
checked_in = {"modules": [{"series": [{"expected_applied_tree": "a" * 40}]}]}
derived = {"modules": [{"series": [{"expected_applied_tree": "b" * 40}]}]}
differences = generator._differences(checked_in, derived)
assert differences == [
    f"modules[0].series[0].expected_applied_tree: composition={'a' * 40!r} derived={'b' * 40!r}"
], differences

# A missing or extra field is reported rather than silently accepted.
assert generator._differences({"profile": "homebrew"}, {}) == [
    "profile: present in the composition, not derived"
]
assert generator._differences({}, {"profile": "homebrew"}) == [
    "profile: absent from the composition, derived='homebrew'"
]

# Values that agree are not a difference, and a file whose bytes differ anyway is
# a style difference - which must never be written over.
assert generator._differences(checked_in, checked_in) == []
assert not generator._style_only([], b"same", b"same")
assert generator._style_only([], b"one", b"two")
assert not generator._style_only(["a difference"], b"one", b"two")

# A module name maps to the repository it is checked out as, and a name outside
# the darling checkout is refused instead of silently pointing at a wrong repo.
darling = Path("/example/darling")
assert generator._module_repo(darling, "darling") == darling
assert generator._module_repo(darling, "darling/src/external/xnu") == darling / "src/external/xnu"
# Only a name outside the darling checkout is refused here; whether the mapped
# path is a real repository is checked when the replay starts.
assert generator._module_repo(darling, "darling/sibling") == darling / "sibling"
for bad in ("homebrew", ""):
    try:
        generator._module_repo(darling, bad)
    except generator.DerivationError:
        pass
    else:
        raise AssertionError(f"module outside the checkout was accepted: {bad!r}")

# A prerequisite is described from the file this run derived: its digest must be
# the digest of those bytes, not of whatever is checked in, and its module trees
# take the parent's final tree and a nested module's integration tree.
prerequisite = generator._prerequisite_payload(
    "homebrew",
    {"homebrew": b"derived bytes\n"},
    {
        "homebrew": {
            "composition_path": Path("/locks/homebrew-profile-composition-v2.yml"),
            "modules": [
                {"module": "darling", "final_tree": "1" * 40, "integration_final_tree": "2" * 40},
                {"module": "darling/src/external/xnu", "final_tree": "3" * 40, "integration_final_tree": "4" * 40},
            ],
            "frozen_manifest": {"path": "west.lock.yml", "sha256": "5" * 64},
        }
    },
)
import hashlib  # noqa: E402

assert prerequisite["profile"] == "homebrew"
assert prerequisite["composition"] == "homebrew-profile-composition-v2.yml"
assert prerequisite["sha256"] == hashlib.sha256(b"derived bytes\n").hexdigest()
assert prerequisite["module_trees"] == {
    "darling": "1" * 40,
    "darling/src/external/xnu": "4" * 40,
}, prerequisite["module_trees"]

print("PASS profile-composition-derivation-contract")

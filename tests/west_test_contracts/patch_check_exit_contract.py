#!/usr/bin/env python3
"""Behavioral exit-code contract for patch test-metadata checks."""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import patch as patch_command


class CheckFailure(RuntimeError):
    pass


def command(errors, *, warnings=None, behavioral=False):
    instance = patch_command.DarlingPatch.__new__(patch_command.DarlingPatch)
    instance.inf = lambda _message: None
    instance.err = lambda _message: None
    instance.die = lambda message, **_kwargs: (_ for _ in ()).throw(CheckFailure(message))
    instance._validate_test_metadata = lambda _patch: list(errors)
    instance._quality_warnings = lambda _patch: list(warnings or [])
    instance._is_behavioral_test = lambda _test: behavioral
    instance._coverage_tier = lambda _test: "host"
    return instance


def expect_failure(call, text: str) -> None:
    try:
        call()
    except CheckFailure as error:
        assert text in str(error), error
    else:
        raise AssertionError(f"expected failure containing {text!r}")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        profile = Path(directory)
        invalid_patch = {"path": "invalid.patch", "tests": []}
        missing_patch = {"path": "missing.patch", "tests": []}
        quality_patch = {"path": "quality.patch", "tests": [{"name": "host"}]}

        invalid = command(["broken metadata"])
        expect_failure(
            lambda: invalid._check(profile, [invalid_patch], False),
            "1 invalid patch test metadata entries",
        )

        precedence = command([], warnings=["quality warning"], behavioral=True)
        precedence._validate_test_metadata = lambda patch: (
            ["broken metadata"] if patch["path"] == "invalid.patch" else []
        )
        expect_failure(
            lambda: precedence._check(
                profile,
                [invalid_patch, missing_patch, quality_patch],
                True,
                quality=True,
                strict_quality=True,
            ),
            "1 invalid patch test metadata entries",
        )
        expect_failure(
            lambda: precedence._check(
                profile,
                [missing_patch, quality_patch],
                True,
                quality=True,
                strict_quality=True,
            ),
            "1 missing patch test metadata entries",
        )

        missing = command([])
        missing._check(profile, [missing_patch], False)
        missing._check(profile, [missing_patch], False, quality=True)
        expect_failure(
            lambda: missing._check(profile, [missing_patch], True),
            "1 missing patch test metadata entries",
        )

        quality = command([], warnings=["quality warning"], behavioral=True)
        quality._check(profile, [quality_patch], False, quality=True)
        expect_failure(
            lambda: quality._check(
                profile,
                [quality_patch],
                False,
                quality=True,
                strict_quality=True,
            ),
            "1 patch test quality warning",
        )

    print("PASS patch-check-exit-contract")


if __name__ == "__main__":
    main()

"""Guard the prefix-bootstrap guidance in ``west test`` requirement diagnostics.

``--prefix PATH`` only resolves a path: the selection neither creates nor
provisions the prefix. The requirement check is where ``west test`` learns that
the selected prefix cannot serve the selected work - it is not bootstrapped, or
it lacks the guest toolchain the selection declares - so that is where it has
to name the exact bootstrap command instead of leaving the operator to
rediscover it from a downstream failure. No real prefix is involved: every case
below is a host-side directory that is deliberately incomplete.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.test import DarlingTest

# Nothing here may consult or write a verdict store: the requirement check is
# expected to fail before any test executes.
os.environ["WEST_TEST_VERDICT_CACHE"] = "off"

GUEST_CC = "/Library/Developer/CommandLineTools/usr/bin/clang"
GUEST_SYSROOT = "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
GUEST_CFLAGS = f"-isysroot {GUEST_SYSROOT}"

LAUNCHER_DETAIL = (
    "darling-launcher (DARLING, DARLING_LAUNCHER, prefix/bin/darling, "
    "or ~/work/darling-prefix/bin/darling)"
)
MISSING_TOOLCHAIN = (
    f"guest compiler missing in prefix root: {GUEST_CC}",
    f"guest compiler missing in base tree: {GUEST_CC}",
    f"guest SDK sysroot missing in prefix root: {GUEST_SYSROOT}",
    f"guest SDK sysroot missing in base tree: {GUEST_SYSROOT}",
)

MINIMAL_PROFILE = "homebrew-rootless-bootstrap-minimal"
PROVISIONING_PROFILE = "homebrew-guest-toolchain-provisioning"


def bootstrap_advice(prefix: Path, profile: str) -> str:
    """The one command that makes ``prefix`` usable for the selected work."""

    return (
        f"bootstrap this prefix first: west test --prefix {prefix} "
        f"--bootstrap-runtime-profile {profile}"
    )


def prefix_invocation(*, guest_c_fixture: bool) -> dict:
    """Return the invocation a prefix-backed metadata binding resolves to."""

    invocation = {
        "display": "cd . && <guest-c-fixture> tests/guest-c.c",
        "name": "prefix-guidance",
        "diag": "bare",
        "requires_resources": ["darling-prefix"],
        "requires_env": [],
    }
    if guest_c_fixture:
        invocation.update(
            {
                "guest_c_fixture": True,
                "guest_cc": GUEST_CC,
                "guest_cflags": GUEST_CFLAGS,
            }
        )
    return invocation


def metadata_refusal(root: Path, prefix: Path | None, invocation: dict) -> str:
    """Return the message a metadata selection dies with for this prefix.

    The metadata run is driven with only its own inputs stubbed: the resolved
    invocation and the route it would execute. The requirement check and the
    message it composes are the production ones.
    """

    command = DarlingTest.__new__(DarlingTest)
    command._prefix = None if prefix is None else str(prefix)
    command.manifest = SimpleNamespace(projects=[], repo_abspath=str(root))
    command._test_invocation = lambda _patch, _test: invocation
    command.inf = lambda _message: None
    command.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    try:
        command._run_metadata_tests(
            [({"path": "test/prefix-guidance.patch"}, {"name": "prefix-guidance"})],
            False,
            [],
        )
    except SystemExit as error:
        return str(error)
    raise AssertionError("a prefix-backed metadata selection ran without a usable prefix")


class _ParserAdder:
    """Supply the one West parser hook ``do_add_parser`` uses."""

    def add_parser(self, name: str, **kwargs):
        return argparse.ArgumentParser(prog=name, description=kwargs.get("description"))


def parsed_args(argv: list[str]):
    """Parse a command line through the command's own argument definitions.

    The refusal route reads the parsed namespace, so taking the real parser
    keeps the harness free of a hand-maintained attribute list.
    """

    command = DarlingTest.__new__(DarlingTest)
    command.name = "test"
    command.description = "Run Darling regression/compat tests"
    return command.do_add_parser(_ParserAdder()).parse_args(argv)


def ctest_refusal(root: Path, prefix: Path) -> str:
    """Return the message a direct ``--env darling`` CTest selection dies with."""

    (root / "testkit").mkdir(exist_ok=True)
    args = parsed_args(["--env", "darling", "--prefix", str(prefix)])
    command = DarlingTest.__new__(DarlingTest)
    command.manifest = SimpleNamespace(projects=[], repo_abspath=str(root))
    command.inf = lambda _message: None
    command.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    try:
        command._do_run(args, [])
    except SystemExit as error:
        return str(error)
    raise AssertionError("an env:darling CTest selection ran without a prefix launcher")


def toolchain_prefix(root: Path, name: str) -> Path:
    """Return a prefix that is otherwise ready and holds the guest CLT."""

    prefix = root / name
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "darling").write_text("launcher\n")
    for tree in (prefix, prefix / "libexec" / "darling"):
        (tree / "Library/Developer/CommandLineTools/usr/bin").mkdir(parents=True)
        (tree / "Library/Developer/CommandLineTools/usr/bin/clang").write_text("compiler\n")
        (tree / "Library/Developer/CommandLineTools/SDKs/MacOSX.sdk").mkdir(parents=True)
    return prefix


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)

    # A fresh prefix is a path the selection only resolves: it does not exist,
    # and the message has to name the bootstrap command for it.
    fresh = root / "fresh-prefix"
    assert not fresh.exists()
    refused = metadata_refusal(root, fresh, prefix_invocation(guest_c_fixture=False))
    assert refused == (
        "test/prefix-guidance.patch: missing required environment for "
        "prefix-guidance: "
        f"{LAUNCHER_DETAIL}; {fresh} is not bootstrapped: "
        f"{bootstrap_advice(fresh, MINIMAL_PROFILE)}"
    ), refused
    print(refused)

    # The same fresh prefix for a selection that compiles inside the guest:
    # one command has to satisfy both, so it is the provisioning profile.
    refused = metadata_refusal(root, fresh, prefix_invocation(guest_c_fixture=True))
    assert refused == (
        "test/prefix-guidance.patch: missing required environment for "
        "prefix-guidance: "
        f"{LAUNCHER_DETAIL}; {fresh} is not bootstrapped: "
        f"{bootstrap_advice(fresh, PROVISIONING_PROFILE)}, "
        + ", ".join(MISSING_TOOLCHAIN)
    ), refused
    print(refused)

    # A bootstrapped prefix without the guest toolchain: the launcher is there,
    # the declared compiler and SDK are not, and the bootstrap command is named.
    baseline = root / "baseline-prefix"
    (baseline / "bin").mkdir(parents=True)
    (baseline / "bin" / "darling").write_text("launcher\n")
    refused = metadata_refusal(root, baseline, prefix_invocation(guest_c_fixture=True))
    assert refused == (
        "test/prefix-guidance.patch: missing required environment for "
        "prefix-guidance: "
        + ", ".join(MISSING_TOOLCHAIN)
        + f", {bootstrap_advice(baseline, PROVISIONING_PROFILE)}"
    ), refused
    print(refused)

    # No prefix at all: the existing guidance stands, and no bootstrap command
    # is invented for a prefix the caller never selected.
    refused = metadata_refusal(root, None, prefix_invocation(guest_c_fixture=True))
    assert refused == (
        "test/prefix-guidance.patch: missing required environment for "
        "prefix-guidance: darling-prefix (--prefix, --prefix-profile, or "
        f"DPREFIX), {LAUNCHER_DETAIL}"
    ), refused
    assert "bootstrap this prefix first" not in refused, refused
    print(refused)

    # A prefix that has the launcher and the guest toolchain is usable: the
    # added guidance must not turn a working invocation into a refusal.
    ready = toolchain_prefix(root, "ready-prefix")
    command = DarlingTest.__new__(DarlingTest)
    command._prefix = str(ready)
    assert command._missing_requirements(prefix_invocation(guest_c_fixture=True)) == []
    assert command._missing_requirements(prefix_invocation(guest_c_fixture=False)) == []

    # The direct CTest path learns the same way and names the same command.
    refused = ctest_refusal(root, fresh)
    assert refused == (
        "env:darling CTest runs need the selected prefix launcher: "
        f"{fresh / 'bin' / 'darling'}; {fresh} is not bootstrapped: "
        f"{bootstrap_advice(fresh, PROVISIONING_PROFILE)}"
    ), refused
    print(refused)

print("PASS west-prefix-bootstrap-guidance-contract")

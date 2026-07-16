"""Contracts for the typed prebuilt guest Mach-O fixture runner."""

from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import test_guest_macho as module
from guest_toolchain import REVIEWED_COMMAND_LINE_TOOLS_SHA256
from test_execution import ProcessResult


production_corpus = ROOT / "testkit/fixtures/guest-macho/v1/corpus.yml"
production_fixture = module.load_guest_macho_fixture(
    ROOT, "testkit/fixtures/guest-macho/v1/corpus.yml", "select_fdset_guest"
)
assert production_fixture.artifact_sha256 == (
    "de9e7097a60f7f0aaf31bc6be0bac760bccf9f6d2a412d5b16aa14ec5685eab6"
)
assert production_fixture.expected_returncode == 0
assert production_fixture.expected_stdout == ("SELECT_FDSET_GUEST_OK",)
production_document = yaml.safe_load(production_corpus.read_text())
assert production_document["corpus"]["acceptance"]["hosted-run"] == 29476139812
assert production_document["corpus"]["acceptance"]["compare-job"] == 87555452302
assert len(production_document["fixtures"]["select_fdset_guest"]["independent-builds"]) == 2
assert production_document["toolchain"]["evidence-run"] == 29384636308
assert production_document["toolchain"]["package-sha256"] == dict(
    REVIEWED_COMMAND_LINE_TOOLS_SHA256
)
homebrew = yaml.safe_load((ROOT / "patches/homebrew/patches.yml").read_text())["patches"]
select_patch = next(
    item for item in homebrew if item.get("path") == "xnu/select-pselect-fdset.patch"
)
typed_tests = [
    test
    for test in select_patch["tests"]
    if test.get("runner") == "guest-macho-fixture"
]
assert [test["name"] for test in typed_tests] == ["select_fdset_guest_prebuilt"]
assert any(test.get("name") == "select_fdset_guest" for test in select_patch["tests"])


def make_macho(path: Path) -> None:
    dylib = b"/usr/lib/libSystem.B.dylib\0"
    dylib_size = (24 + len(dylib) + 7) & ~7
    dylib_command = struct.pack("<IIIIII", 0x0C, dylib_size, 24, 0, 0, 0)
    dylib_command += dylib.ljust(dylib_size - 24, b"\0")
    rpath = b"/usr/lib\0"
    rpath_size = (12 + len(rpath) + 7) & ~7
    rpath_command = struct.pack("<III", 0x8000001C, rpath_size, 12)
    rpath_command += rpath.ljust(rpath_size - 12, b"\0")
    commands = dylib_command + rpath_command
    header = struct.pack(
        "<IiiIIIII",
        0xFEEDFACF,
        0x01000007,
        3,
        2,
        2,
        len(commands),
        0,
        0,
    )
    path.write_bytes(header + commands)
    path.chmod(0o755)


with tempfile.TemporaryDirectory(prefix="west-guest-macho-contract-") as raw:
    root = Path(raw)
    trusted_root = root / "runner-temp"
    trusted_root.mkdir()
    os.environ["RUNNER_TEMP"] = str(trusted_root)
    os.environ["ROOTLESS_TIER_REPO"] = str(ROOT)
    source = root / "source.c"
    source.write_text("int main(void) { return 0; }\n")
    artifact = root / "fixture"
    make_macho(artifact)
    provenance = root / "provenance.txt"
    provenance.write_text(
        "review_status: reviewed\n"
        "evidence_run: 29384636308\n"
        "product_id: 041-90419\n"
        + "\n".join(f"{package_id} {digest}" for package_id, digest in REVIEWED_COMMAND_LINE_TOOLS_SHA256.items())
        + "\n"
    )
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    flags = [
        "-isysroot",
        "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
        "-std=gnu11",
        "-Wall",
        "-Wextra",
        "-Werror",
    ]
    corpus = root / "corpus.yml"
    corpus.write_text(
        yaml.safe_dump(
            {
                "schema": 1,
                "toolchain": {
                    "product-id": "041-90419",
                    "provenance": "provenance.txt",
                    "package-sha256": dict(REVIEWED_COMMAND_LINE_TOOLS_SHA256),
                },
                "fixtures": {
                    "select_fdset_guest": {
                        "source": "source.c",
                        "source-sha256": source_sha256,
                        "artifact": "fixture",
                        "artifact-sha256": artifact_sha256,
                        "build": {
                            "compiler-path": "/Library/Developer/CommandLineTools/usr/bin/clang",
                            "compiler-version": "Apple LLVM version 9.0.0",
                            "flags": flags,
                        },
                        "independent-builds": [
                            {
                                "name": "a",
                                "artifact-sha256": artifact_sha256,
                                "compiler-path": "/Library/Developer/CommandLineTools/usr/bin/clang",
                                "flags": flags,
                            },
                            {
                                "name": "b",
                                "artifact-sha256": artifact_sha256,
                                "compiler-path": "/Library/Developer/CommandLineTools/usr/bin/clang",
                                "flags": flags,
                            },
                        ],
                        "macho": {
                            "magic": "MH_MAGIC_64",
                            "architecture": "x86_64",
                            "filetype": "MH_EXECUTE",
                            "dylib-load-commands": [
                                {"command": "LC_LOAD_DYLIB", "name": "/usr/lib/libSystem.B.dylib"}
                            ],
                            "rpath-load-commands": ["/usr/lib"],
                        },
                        "expected": {
                            "returncode": 0,
                            "stdout-contains": ["SELECT_FDSET_GUEST_OK"],
                        },
                        "runtime": {
                            "rootless": True,
                            "guest-toolchain": "forbidden",
                            "prefix": "tier-owned-fresh",
                        },
                    }
                },
            },
            sort_keys=False,
        )
    )
    fixture = module.load_guest_macho_fixture(root, "corpus.yml", "select_fdset_guest")
    assert fixture.header.architecture == "x86_64"
    assert fixture.header.filetype == "MH_EXECUTE"
    assert fixture.header.rpath_load_commands == ("/usr/lib",)
    assert fixture.artifact_sha256 == artifact_sha256

    malformed = root / "malformed-load-command"
    malformed_bytes = bytearray(artifact.read_bytes())
    malformed_bytes[56] = 0xFF
    malformed.write_bytes(malformed_bytes)
    try:
        module._macho_header(malformed)
    except module.GuestMachoFixtureError as error:
        assert "invalid UTF-8" in str(error), error
    else:
        raise AssertionError("Mach-O decoder leaked UnicodeDecodeError")

    valid_manifest = corpus.read_text()
    document = yaml.safe_load(valid_manifest)
    document["fixtures"]["select_fdset_guest"]["macho"]["rpath-load-commands"] = ["/wrong"]
    corpus.write_text(yaml.safe_dump(document, sort_keys=False))
    try:
        module.load_guest_macho_fixture(root, "corpus.yml", "select_fdset_guest")
    except module.GuestMachoFixtureError as error:
        assert "LC_RPATH mismatch" in str(error), error
    else:
        raise AssertionError("corpus accepted mismatched LC_RPATH")
    corpus.write_text(valid_manifest)

    document = yaml.safe_load(valid_manifest)
    document["fixtures"]["select_fdset_guest"]["expected"]["stdout-contains"] = [""]
    corpus.write_text(yaml.safe_dump(document, sort_keys=False))
    try:
        module.load_guest_macho_fixture(root, "corpus.yml", "select_fdset_guest")
    except module.GuestMachoFixtureError as error:
        assert "non-empty strings" in str(error), error
    else:
        raise AssertionError("corpus accepted an empty expected marker")
    corpus.write_text(valid_manifest)

    bad = corpus.read_text().replace(artifact_sha256, "f" * 64, 1)
    corpus.write_text(bad)
    try:
        module.load_guest_macho_fixture(root, "corpus.yml", "select_fdset_guest")
    except module.GuestMachoFixtureError as error:
        assert "artifact SHA-256 mismatch" in str(error), error
    else:
        raise AssertionError("corpus accepted a mismatched artifact SHA-256")
    corpus.write_text(bad.replace("f" * 64, artifact_sha256, 1))

    prefix = trusted_root / "darling-rootless-smoke.contract"
    (prefix / "private/var/tmp").mkdir(parents=True)
    (prefix / "bin").mkdir()
    outside = root / "outside" / "darling-rootless-smoke.contract"
    (outside / "private/var/tmp").mkdir(parents=True)
    try:
        module._require_tier_owned_fresh_prefix(outside, env={})
    except module.GuestMachoFixtureError as error:
        assert "trusted root" in str(error), error
    else:
        raise AssertionError("runner accepted a prefix outside RUNNER_TEMP")
    ordinary_owner = prefix / ".west-tier-owner"
    ordinary_owner.write_text("schema=1\nkind=smoke\npid=1\n")
    launcher = prefix / "bin/darling"
    launcher.write_text("launcher\n")
    env = {
        "DPREFIX": str(prefix),
        "DARLING_ROOTLESS": "1",
        "DARLING_NOOVERLAYFS": "1",
        "DARLING_EUNION": "1",
        "WEST_TEST_FORBID_GUEST_TOOLCHAIN": "1",
    }
    calls = []

    class Command:
        topdir = str(root)
        _prefix = str(prefix)

        def _execution_env(self, _invocation):
            return dict(env)

        def _resolve_darling_launcher(self, _prefix):
            return str(launcher)

        def _record_failure_phase(self, _invocation, phase):
            raise AssertionError(f"unexpected failure phase: {phase}")

        def die(self, message):
            raise AssertionError(message)

        def err(self, message):
            raise AssertionError(message)

    invocation = {
        "name": "select_fdset_guest_prebuilt",
        "corpus": "corpus.yml",
        "fixture": "select_fdset_guest",
        "requires_resources": ["darling-prefix"],
        "requires_profile": None,
        "runtime_profile": None,
        "timeout_seconds": 30,
    }

    try:
        module.run_guest_macho_fixture(Command(), invocation, env)
    except AssertionError as error:
        assert "tier-owned prefix owner is missing" in str(error), error
    else:
        raise AssertionError("runner accepted an owner file inside an ordinary prefix")
    ordinary_owner.unlink()

    tier_owner = prefix.with_name(f"{prefix.name}.west-tier-owner")
    tier_owner.write_text("schema=1\nkind=smoke\npid=1\n")
    try:
        module.run_guest_macho_fixture(Command(), invocation, env)
    except AssertionError as error:
        assert "invalid pid" in str(error), error
    else:
        raise AssertionError("runner accepted a forged tier owner")
    tier_owner.write_text(f"schema=1\nkind=smoke\npid={os.getpid()}\n")
    try:
        module.run_guest_macho_fixture(Command(), invocation, env)
    except AssertionError as error:
        assert "not a live ancestor" in str(error), error
    else:
        raise AssertionError("runner accepted a non-ancestor owner pid")
    owner_pid = os.getppid()
    assert module._is_live_ancestor(owner_pid), owner_pid
    tier_owner.write_text(f"schema=1\nkind=smoke\npid={owner_pid}\n")

    original = module.run_guest_shell

    def fake_guest_shell(*args, **kwargs):
        calls.append((args, kwargs))
        return ProcessResult(0, stdout="SELECT_FDSET_GUEST_OK\n", stderr="")

    module.run_guest_shell = fake_guest_shell
    try:
        assert module.run_guest_macho_fixture(
            Command(),
            invocation,
            env,
        ) == 0
    finally:
        module.run_guest_shell = original
    assert calls, "typed runner did not launch the guest"
    assert not (prefix / "private/var/tmp/west-macho-select_fdset_guest").exists()
    assert not list((prefix / "private/var/tmp").glob(".west-macho-select_fdset_guest.*.tmp"))

    (prefix / "Library/Developer/CommandLineTools/usr/bin").mkdir(parents=True)
    try:
        module.run_guest_macho_fixture(
            Command(),
            invocation,
            env,
        )
    except AssertionError as error:
        assert "CommandLineTools" in str(error), error
    else:
        raise AssertionError("typed runner accepted a prefix containing CLT")

print("PASS guest-macho-contract")

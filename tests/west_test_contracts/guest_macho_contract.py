"""Contracts for the typed prebuilt guest Mach-O fixture runner."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
sys.path.insert(0, str(ROOT / "ci"))

import test_guest_macho as module
from guest_macho_batch_specs import FIXTURE_SPECS
from guest_toolchain import REVIEWED_COMMAND_LINE_TOOLS_SHA256
from test_execution import ProcessResult


PRODUCTION_CORPUS = ROOT / "testkit/fixtures/guest-macho/v1/corpus.yml"
CORPUS_ROOT = PRODUCTION_CORPUS.parent
ANCHOR = "select_fdset_guest"
ACCEPTED_COMMIT = "a53ec8109829fc8da743bcd97d0b14c92b3ca7c6"
ACCEPTED_SOURCE_REVISION = "a53ec8109829fc8da743bcd97d0b14c92b3ca7c6"
ACCEPTED_RUN = 29516601154
ACCEPTED_COMPARE_JOB = 87691198694
ACCEPTED_RESULT = "MACHO_CORPUS_BATCH_MATCH"
EXPECTED_NAMES = [spec.name for spec in FIXTURE_SPECS]
ACCEPTED_ARTIFACT_SHA256 = {
    "abort_with_payload_no_group_broadcast": "b3dff938f460202c26ed4b71a5c95817ed438c4216046b0183f09e71cbd0a2d8",
    "select_fdset_guest": "de9e7097a60f7f0aaf31bc6be0bac760bccf9f6d2a412d5b16aa14ec5685eab6",
    "getattrlist_name_objtype_guest": "b434c9f96613ea10399b433abf2ab38accc3c5b9bad7629904d71414cbc53819",
    "darwin_priority_guest": "fbca0bf42014f48b8118b0bdfe22e0b77b4bef5480d4af648d91c1a9505eb2d6",
    "socket_siocgifconf_guest": "3f04493aa574afe1df50d1c6c381ae56be8ec1ea444fdb2f61d2e28bd062efd5",
    "bzero_return_register_guest": "7d355056e48392d956b4fc4715d11c4cb46bf2076dbe3f2801bfe3d3dc83a213",
    "sigexc_sa_restart_guest": "f14b28c69cff49c5f605c0964f51181f381e1e7ba03c3bba55f6200a3c23c538",
    "sigexc_default_resend_self_guest": "8666cac78eeb32c50bf400d457879e79c98ef1afe9af731216cb7f888b735c5f",
    "ulock_eintr_retry_guest": "aa721051fac815b9e3d462b9b54d4f57ec0db9135a3953a61616f9c6a55cffc5",
    "vchroot_pathnull_guard_guest": "45fe9ce0b90de6bc185df3d1abc286422406929586bf915dbd21c3293d23cb9f",
    "chown_disabled_null_guard_guest": "86279359ef05a93badbc21137a6a7140732c60059d54f2d94c70dd3ee62ac07a",
    "fd_guard_ebadf_guest": "59bb4d1d8f23c90e5a0bda37ff95fffafbca44aaad64746fbd3cdc1a79be68c0",
    "fork_checkin_signal_storm_guest": "295a0f3a0f006baa5870014b9114a97133064a97910be6b057b7863422f15233",
    "rootless_no_mount_guest": "81f6d4d14ab7645ff8212cf129c58d6326bc1366f1c603f8de5608f0f68071da",
}

document = yaml.safe_load(PRODUCTION_CORPUS.read_text())
acceptance = document["corpus"]["acceptance"]
assert len(ACCEPTED_COMMIT) == 40 and all(
    character in "0123456789abcdef" for character in ACCEPTED_COMMIT
)
assert list(document["fixtures"]) == EXPECTED_NAMES
assert len(document["fixtures"]) == 14
assert acceptance["phase"] == "3B batch acceptance"
assert acceptance["hosted-run"] == ACCEPTED_RUN
assert acceptance["commit"] == ACCEPTED_COMMIT
assert acceptance["compare-job"] == ACCEPTED_COMPARE_JOB
assert acceptance["compare-result"] == ACCEPTED_RESULT
assert acceptance["fixture-count"] == 14
assert acceptance["anchor"] == ANCHOR
assert acceptance["anchor-sha256"] == ACCEPTED_ARTIFACT_SHA256[ANCHOR]

assert set(path.name for path in CORPUS_ROOT.iterdir()) == {"bin", "corpus.yml", "clt-provenance.txt"}
bin_dir = CORPUS_ROOT / "bin"
assert {path.name for path in bin_dir.iterdir()} == set(EXPECTED_NAMES)
assert all(path.is_file() and not path.is_symlink() for path in bin_dir.iterdir())
assert all((path.stat().st_mode & 0o777) == 0o755 for path in bin_dir.iterdir())

assert document["toolchain"]["evidence-run"] == 29384636308
assert document["toolchain"]["package-sha256"] == dict(REVIEWED_COMMAND_LINE_TOOLS_SHA256)
assert document["toolchain"]["review-status"] == "reviewed"

specs = {spec.name: spec for spec in FIXTURE_SPECS}
for name in EXPECTED_NAMES:
    spec = specs[name]
    entry = document["fixtures"][name]
    fixture = module.load_guest_macho_fixture(ROOT, "testkit/fixtures/guest-macho/v1/corpus.yml", name)
    assert fixture.header == module._macho_header(fixture.artifact_path)
    assert fixture.artifact_sha256 == ACCEPTED_ARTIFACT_SHA256[name]
    assert entry["artifact-sha256"] == ACCEPTED_ARTIFACT_SHA256[name]
    assert entry["source-project"] == spec.source_project
    assert entry["source"] == spec.source_path
    assert entry["source-revision"] == ACCEPTED_SOURCE_REVISION
    assert entry["source-sha256"] == spec.source_sha256
    assert module._sha256(ROOT / spec.source_path) == spec.source_sha256
    assert entry["patch"]["path"] == spec.patch_path
    assert entry["patch"]["sha256"] == spec.patch_sha256
    assert module._sha256(ROOT / spec.patch_path) == spec.patch_sha256
    assert entry["build"]["flags"] == list(spec.compile_flags)
    assert entry["build"]["link-flags"] == list(spec.link_flags)
    assert entry["runtime"]["profile"] == spec.runtime_profile
    assert entry["expected"]["returncode"] == 0
    assert entry["expected"]["stdout-contains"] == [spec.expected_marker]

    builds = entry["independent-builds"]
    assert [build["name"] for build in builds] == ["a", "b"]
    assert all(
        set(build)
        == {
            "name",
            "hosted-run",
            "job-id",
            "artifact-id",
            "artifact-archive-sha256",
            "artifact-sha256",
            "compiler-path",
            "flags",
            "link-flags",
            "runtime-mode",
            "runtime-status",
            "observed-marker",
        }
        for build in builds
    )
    assert all(build["hosted-run"] == ACCEPTED_RUN for build in builds)
    assert all(build["artifact-sha256"] == ACCEPTED_ARTIFACT_SHA256[name] for build in builds)
    assert all(build["flags"] == list(spec.compile_flags) for build in builds)
    assert all(build["link-flags"] == list(spec.link_flags) for build in builds)
    assert builds[0]["artifact-sha256"] == builds[1]["artifact-sha256"]

    runtime = entry["runtime"]
    for build in builds:
        assert build["runtime-mode"] == runtime["mode"]
        assert build["runtime-status"] == runtime["status"]
        assert build["observed-marker"] == runtime["observed-marker"]
    if name == ANCHOR:
        assert runtime["mode"] == "anchor"
        assert runtime["status"] == "OBSERVED"
        assert runtime["observed-marker"] == spec.expected_marker
    else:
        assert runtime["mode"] == "compile-only"
        assert runtime["status"] == "NOT_RUN"
        assert runtime["observed-marker"] == "NOT_OBSERVED"

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
        "repo_root": str(root),
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

    nested_topdir = root / "darling-dev"
    nested_repo = nested_topdir / "darling-workspace"
    nested_repo.mkdir(parents=True)
    for name in ("corpus.yml", "source.c", "fixture", "provenance.txt"):
        shutil.copy2(root / name, nested_repo / name)

    class NestedCommand(Command):
        topdir = str(nested_topdir)

    nested_invocation = {**invocation, "repo_root": str(nested_repo)}
    module.run_guest_shell = fake_guest_shell
    try:
        assert module.run_guest_macho_fixture(NestedCommand(), nested_invocation, env) == 0
    finally:
        module.run_guest_shell = original
    assert calls[-1][1]["cwd"] == nested_topdir

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

    outside = root / "outside-repo"
    outside.mkdir()
    for value in ("../outside-repo/corpus.yml", str(outside / "corpus.yml")):
        try:
            module._safe_repo_path(nested_repo, value, "corpus")
        except module.GuestMachoFixtureError:
            pass
        else:
            raise AssertionError(f"repository path escape was accepted: {value}")
    (nested_repo / "escape-link").symlink_to(outside, target_is_directory=True)
    try:
        module._safe_repo_path(nested_repo, "escape-link/corpus.yml", "corpus")
    except module.GuestMachoFixtureError:
        pass
    else:
        raise AssertionError("symlink repository path escape was accepted")

print("PASS guest-macho-contract")

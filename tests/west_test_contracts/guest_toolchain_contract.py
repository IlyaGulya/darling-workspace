"""Behavior contract for the declarative guest CommandLineTools provider."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import guest_toolchain as guest_toolchain_module
from guest_toolchain import (
    COMMAND_LINE_TOOLS_MANIFEST_URL,
    COMMAND_LINE_TOOLS_PACKAGE_IDS,
    GuestToolchainError,
    ensure_command_line_tools,
    guest_toolchain_provisioning_forbidden,
    require_guest_toolchain_provisioning_allowed,
)
from test_execution import ProcessResult
from prefix_repair import guest_c_fixture_prerequisite_problems, repair_prefix_prerequisites


assert not guest_toolchain_provisioning_forbidden({}), "CLT provisioning was unexpectedly forbidden"
assert guest_toolchain_provisioning_forbidden(
    {"WEST_TEST_FORBID_GUEST_TOOLCHAIN": "1"}
), "no-CLT policy did not reject guest toolchain provisioning"
assert not guest_toolchain_provisioning_forbidden(
    {"WEST_TEST_FORBID_GUEST_TOOLCHAIN": "0"}
), "no-CLT policy accepted an unrelated value"
require_guest_toolchain_provisioning_allowed({})
try:
    require_guest_toolchain_provisioning_allowed(
        {"WEST_TEST_FORBID_GUEST_TOOLCHAIN": "1"}
    )
except GuestToolchainError as error:
    assert error.kind == "policy", error.kind
    assert "no-CLT" in str(error), error
else:
    raise AssertionError("no-CLT policy allowed guest toolchain provisioning")


class Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        if size < 0:
            result, self.payload = self.payload, b""
            return result
        result, self.payload = self.payload[:size], self.payload[size:]
        return result


def selected_package_contract(root: Path) -> None:
    prefix = root / "selected-prefix"
    clt = prefix / "Library/Developer/CommandLineTools"
    (clt / "usr/bin").mkdir(parents=True)
    (clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
    clang = clt / "usr/bin/clang"
    clang.write_text("Apple LLVM version 9.0.0 (clang-900.0.39.2)")
    assert not repair_prefix_prerequisites(prefix).problems
    staging = prefix / "private/var/tmp"
    staging.mkdir(parents=True, exist_ok=True)
    foreign = [
        staging / "west-com.apple.pkg.foreign.pkg",
        staging / "west-clt13.2-foreign.pkg",
    ]
    for path in foreign:
        path.write_bytes(b"not owned by this invocation")
    payload = b"small authenticated-package stand-in"
    package = root / "selected.pkg"
    package.write_bytes(payload)
    receipt = prefix / ".west-command-line-tools.json"
    installs = []
    guest_calls = []
    outcome = "success"

    def no_network(*_args, **_kwargs):
        raise AssertionError("local package selector used the mirrored provider")

    def guest_runner(_launcher, runner_prefix, argv, **_kwargs):
        guest_calls.append(tuple(argv))
        if argv[0] == "/usr/bin/installer":
            staged = runner_prefix / argv[2].lstrip("/")
            assert staged.read_bytes() == payload
            assert staged not in foreign
            installs.append(tuple(argv))
            if outcome == "failure":
                return ProcessResult(1, stderr="installer rejected package")
            if outcome == "timeout":
                return ProcessResult(0, timed_out=True)
            if outcome == "success":
                clang.write_text("Apple clang version 13.0.0 (clang-1300.0.29.30)")
                (clt / "SDKs/MacOSX12.1.sdk").mkdir(exist_ok=True)
                # The canonical sysroot the prerequisites look for, created the way
                # the real flow creates it.
                canonical = clt / "SDKs/MacOSX.sdk"
                if not canonical.is_symlink() and not canonical.exists():
                    canonical.symlink_to("MacOSX12.1.sdk")
            return ProcessResult(0)
        assert tuple(argv) == (
            "/Library/Developer/CommandLineTools/usr/bin/clang", "--version",
        )
        return ProcessResult(0, stdout=clang.read_text())

    def provision(**env_overrides):
        return ensure_command_line_tools(
            prefix=prefix, launcher="launcher", cwd=root,
            env={"DARLING_CLT_PACKAGE": str(package), **env_overrides},
            opener=no_network, guest_runner=guest_runner, log=lambda _: None,
        )

    # Real authentication must run before an old CLT can short-circuit selection.
    try:
        provision()
    except GuestToolchainError as error:
        assert error.kind == "download", error
    else:
        raise AssertionError("wrong local package digest accepted")
    assert not guest_calls and not receipt.exists()
    assert clang.read_text() == "Apple LLVM version 9.0.0 (clang-900.0.39.2)"
    assert set(staging.iterdir()) == set(foreign)

    try:
        provision(WEST_TEST_FORBID_GUEST_TOOLCHAIN="1")
    except GuestToolchainError as error:
        assert error.kind == "policy", error
    else:
        raise AssertionError("local package selector bypassed no-CLT policy")
    assert not guest_calls

    # Isolate only the authentication boundary; production digest stays pinned.
    def authenticate_fixture(path):
        assert path.read_bytes() == payload

    with patch.object(guest_toolchain_module, "_verify_selected_package", authenticate_fixture):
        # The old layout occupies the SDK entry the installer creates, so the
        # conflict must be named here instead of arriving as a symlink error
        # buried inside installer output.
        try:
            provision()
        except GuestToolchainError as error:
            assert error.kind == "conflict", error
            assert "SDKs/MacOSX.sdk" in str(error), error
            assert "Command_Line_Tools_for_Xcode_13.2" in str(error), error
        else:
            raise AssertionError("an older CLT SDK layout was installed over silently")
        assert not [call for call in guest_calls if call[0] == "/usr/bin/installer"], (
            "the installer must not be started against a layout it cannot replace"
        )
        # Removing the occupied entry is the documented remedy for a disposable
        # prefix, and the upgrade then proceeds as before.
        (clt / "SDKs/MacOSX.sdk").rmdir()
        provision()
        assert len(installs) == 1, "old CLT paths bypassed requested upgrade"
        identity = json.loads(receipt.read_text())
        assert identity["sha256"] == (
            "7d45d981d51dce2b91de369eba56dbaf77ae175ff89bcaf6a885c3095587f8a8"
        )
        provision()
        assert len(installs) == 1, "same selected package was reinstalled"

        # A receipt for other bytes must not establish selected-package identity.
        receipt.write_text(json.dumps({**identity, "sha256": "0" * 64}))
        provision()
        assert len(installs) == 2

        # An authentic receipt does not excuse stale compiler/SDK prerequisites.
        clang.write_text("Apple LLVM version 9.0.0 (clang-900.0.39.2)")
        provision()
        assert len(installs) == 3
        (clt / "SDKs/MacOSX12.1.sdk").rmdir()
        provision()
        assert len(installs) == 4

        for outcome in ("failure", "timeout", "no-payload"):
            clang.write_text("Apple LLVM version 9.0.0 (clang-900.0.39.2)")
            try:
                provision()
            except GuestToolchainError as error:
                assert error.kind == ("setup" if outcome == "no-payload" else "install")
            else:
                raise AssertionError(f"{outcome} published a successful installation")
            assert not receipt.exists(), "failed install retained a success receipt"
            assert set(staging.iterdir()) == set(foreign), "staging cleanup was not exact"
            assert all(path.read_bytes() == b"not owned by this invocation" for path in foreign)
            assert package.read_bytes() == payload, "provider changed the supplied package"


def main() -> None:
    with patch.dict("os.environ", {}, clear=True), tempfile.TemporaryDirectory(
        prefix="west-guest-toolchain-contract-"
    ) as raw:
        root = Path(raw)
        prefix = root / "prefix"
        cache = root / "cache"
        (prefix / "private/var/tmp").mkdir(parents=True)
        foreign_package = prefix / "private/var/tmp/west-com.apple.pkg.foreign.pkg"
        foreign_package.write_bytes(b"another invocation owns this")
        (prefix / "bin").mkdir()

        package_payloads = {}
        package_entries = []
        for index, package_id in enumerate(COMMAND_LINE_TOOLS_PACKAGE_IDS):
            toc = f"<xar><toc><name>pkg-{index}</name></toc></xar>".encode()
            compressed = zlib.compress(toc)
            digest = hashlib.sha1(compressed).hexdigest()
            payload = (
                struct.pack(">4sHHQQI", b"xar!", 28, 1, len(compressed), len(toc), 1)
                + compressed + bytes.fromhex(digest) + f"pkg-{index}".encode()
            )
            url = f"https://swcdn.apple.com/{index}.pkg"
            package_payloads[url] = payload
            package_entries.append(
                {
                    "id": package_id,
                    "url": url,
                    "size": len(payload),
                    "digest": digest,
                }
            )
        reviewed_digests = dict(guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256)
        guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256.update(
            {
                package_id: hashlib.sha256(package_payloads[f"https://swcdn.apple.com/{index}.pkg"]).hexdigest()
                for index, package_id in enumerate(COMMAND_LINE_TOOLS_PACKAGE_IDS)
            }
        )
        manifest = json.dumps([{"packages": package_entries}]).encode()

        def opener(url, **_):
            if url == COMMAND_LINE_TOOLS_MANIFEST_URL:
                return Response(manifest)
            return Response(package_payloads[url])

        calls = []
        logs = []

        def guest_runner(launcher, runner_prefix, argv, **_):
            calls.append((launcher, runner_prefix, tuple(argv)))
            assert argv[0] == "/usr/bin/installer", argv
            assert argv[1] == "-pkg", argv
            assert argv[2].startswith("/private/var/tmp/west-com.apple.pkg."), argv
            assert argv[3:] == ("-target", "/"), argv
            clt = runner_prefix / "Library/Developer/CommandLineTools.apple-clt-test"
            (clt / "usr/bin").mkdir(parents=True, exist_ok=True)
            (clt / "SDKs/MacOSX.sdk").mkdir(parents=True, exist_ok=True)
            (clt / "usr/bin/clang").write_bytes(b"guest clang")
            return ProcessResult(0, stdout="installer: Installation complete\n")

        changed = ensure_command_line_tools(
            prefix=prefix,
            launcher=str(prefix / "bin/darling"),
            cwd=root,
            env={"DPREFIX": str(prefix)},
            cache_dir=cache,
            opener=opener,
            guest_runner=guest_runner,
            log=logs.append,
        )
        assert changed == list(COMMAND_LINE_TOOLS_PACKAGE_IDS), changed
        assert len(calls) == len(COMMAND_LINE_TOOLS_PACKAGE_IDS), calls
        assert set((prefix / "private/var/tmp").glob("west-*.pkg")) == {foreign_package}
        assert foreign_package.read_bytes() == b"another invocation owns this"
        assert not guest_c_fixture_prerequisite_problems(
            prefix,
            "/Library/Developer/CommandLineTools/usr/bin/clang",
            "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
        )
        assert len(list(cache.glob("*.pkg"))) == len(COMMAND_LINE_TOOLS_PACKAGE_IDS)

        def unexpected_runner(*_args, **_kwargs):
            raise AssertionError("idempotent provider attempted a second install")

        assert ensure_command_line_tools(
            prefix=prefix,
            launcher=str(prefix / "bin/darling"),
            cwd=root,
            env={"DPREFIX": str(prefix)},
            cache_dir=cache,
            opener=opener,
            guest_runner=unexpected_runner,
            log=lambda _: None,
        ) == ["guest CommandLineTools already provisioned"]

        invalid = json.loads(manifest)
        invalid[0]["packages"] = invalid[0]["packages"][:-1]
        try:
            ensure_command_line_tools(
                prefix=root / "missing-prefix",
                launcher="launcher",
                cwd=root,
                env={},
                cache_dir=cache,
                opener=lambda *_args, **_kwargs: Response(json.dumps(invalid).encode()),
                guest_runner=unexpected_runner,
                log=lambda _: None,
            )
        except GuestToolchainError:
            pass
        else:
            raise AssertionError("incomplete package manifest was accepted")

        bad_package = root / "bad.pkg"
        bad_package.write_bytes(package_payloads["https://swcdn.apple.com/0.pkg"] + b"tampered")
        package = guest_toolchain_module.CommandLineToolsPackage(
            COMMAND_LINE_TOOLS_PACKAGE_IDS[0],
            "https://swcdn.apple.com/0.pkg",
            bad_package.stat().st_size,
            package_entries[0]["digest"],
        )
        try:
            guest_toolchain_module._verify_package(bad_package, package, logs.append)
        except GuestToolchainError as error:
            assert error.kind == "download", error.kind
        else:
            raise AssertionError("tampered CommandLineTools payload was accepted")

        guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256.clear()
        guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256.update(reviewed_digests)

        selected_package_contract(root)

    print("PASS guest-toolchain-contract")


if __name__ == "__main__":
    main()

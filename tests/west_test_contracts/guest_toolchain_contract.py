"""Behavior contract for the declarative guest CommandLineTools provider."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
from pathlib import Path

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
from prefix_repair import guest_c_fixture_prerequisite_problems


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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="west-guest-toolchain-contract-") as raw:
        root = Path(raw)
        prefix = root / "prefix"
        cache = root / "cache"
        (prefix / "private/var/tmp").mkdir(parents=True)
        (prefix / "bin").mkdir()

        package_payloads = {}
        package_entries = []
        for index, package_id in enumerate(COMMAND_LINE_TOOLS_PACKAGE_IDS):
            payload = struct.pack(
                ">4sHHQQI", b"xar!", 28, 1, 1, 1, 1
            ) + f"pkg-{index}".encode()
            digest = hashlib.sha1(payload).hexdigest()
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
        package_entries[0]["digest"] = "0" * 40
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
        assert not list((prefix / "private/var/tmp").glob("west-*.pkg"))
        assert not guest_c_fixture_prerequisite_problems(
            prefix,
            "/Library/Developer/CommandLineTools/usr/bin/clang",
            "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
        )
        assert len(list(cache.glob("*.pkg"))) == len(COMMAND_LINE_TOOLS_PACKAGE_IDS)
        assert any("API SHA-1 mismatch" in line for line in logs), logs

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
        except GuestToolchainError as error:
            assert "missing package" in str(error), error
        else:
            raise AssertionError("incomplete package manifest was accepted")

        bad_package = root / "bad.pkg"
        bad_package.write_bytes(package_payloads["https://swcdn.apple.com/0.pkg"] + b"tampered")
        package = guest_toolchain_module.CommandLineToolsPackage(
            COMMAND_LINE_TOOLS_PACKAGE_IDS[0],
            "https://swcdn.apple.com/0.pkg",
            bad_package.stat().st_size,
            hashlib.sha1(bad_package.read_bytes()).hexdigest(),
        )
        try:
            guest_toolchain_module._verify_package(bad_package, package, logs.append)
        except GuestToolchainError as error:
            assert error.kind == "download", error.kind
            assert "unreviewed SHA-256" in str(error), error
        else:
            raise AssertionError("tampered CommandLineTools payload was accepted")

        guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256.clear()
        guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256.update(reviewed_digests)

    print("PASS guest-toolchain-contract")


if __name__ == "__main__":
    main()

"""Behavior contract for the declarative guest CommandLineTools provider."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from pathlib import Path

from guest_toolchain import (
    COMMAND_LINE_TOOLS_MANIFEST_URL,
    COMMAND_LINE_TOOLS_PACKAGE_IDS,
    GuestToolchainError,
    ensure_command_line_tools,
)
from test_execution import ProcessResult
from prefix_repair import guest_c_fixture_prerequisite_problems


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
            payload = f"pkg-{index}".encode()
            digest = hashlib.sha1(payload).hexdigest()
            url = f"https://example.test/{index}.pkg"
            package_payloads[url] = payload
            package_entries.append(
                {
                    "id": package_id,
                    "url": url,
                    "size": len(payload),
                    "digest": digest,
                }
            )
        manifest = json.dumps([{"packages": package_entries}]).encode()

        def opener(url, **_):
            if url == COMMAND_LINE_TOOLS_MANIFEST_URL:
                return Response(manifest)
            return Response(package_payloads[url])

        calls = []

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
            log=lambda _: None,
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

    print("PASS guest-toolchain-contract")


if __name__ == "__main__":
    main()

"""Negative cache contracts for external CommandLineTools payloads."""

from __future__ import annotations

import hashlib
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import guest_toolchain as guest_toolchain_module
from guest_toolchain import CommandLineToolsPackage, GuestToolchainError


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


def package_for(payload: bytes) -> CommandLineToolsPackage:
    _, header_size, _, toc_size, _, _ = struct.unpack(">4sHHQQI", payload[:28])
    return CommandLineToolsPackage(
        "com.apple.pkg.CLTools_Executables",
        "https://swcdn.apple.com/contract.pkg",
        len(payload),
        hashlib.sha1(payload[header_size:header_size + toc_size]).hexdigest(),
    )


def valid_payload() -> bytes:
    toc = b'<xar><toc><checksum style="sha1"><offset>0</offset><size>20</size></checksum></toc></xar>'
    compressed = zlib.compress(toc)
    return (
        struct.pack(">4sHHQQI", b"xar!", 28, 1, len(compressed), len(toc), 1)
        + compressed + hashlib.sha1(compressed).digest() + b"contract-payload"
    )


def assert_cached_mismatch_redownloads(
    root: Path, name: str, good: bytes, bad: bytes
) -> None:
    cache = root / f"cache-{name}"
    package = package_for(good)
    guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256[package.package_id] = (
        hashlib.sha256(good).hexdigest()
    )
    cached = cache / package.cache_name
    cache.mkdir()
    cached.write_bytes(bad)
    downloads = []

    def opener(url, **_):
        assert not cached.exists(), f"{name} cache entry survived validation"
        downloads.append(url)
        return Response(good)

    result = guest_toolchain_module._cached_package(
        package, cache, opener=opener, log=lambda _: None
    )
    assert result == cached
    assert cached.read_bytes() == good
    assert len(downloads) == 1, downloads
    assert not list(cache.glob("*.part")), list(cache.glob("*.part"))


def assert_download_mismatch_fails(root: Path, name: str, good: bytes, bad: bytes) -> None:
    cache = root / f"failed-cache-{name}"
    package = package_for(good)
    guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256[package.package_id] = (
        hashlib.sha256(good).hexdigest()
    )

    def opener(url, **_):
        return Response(bad)

    try:
        guest_toolchain_module._cached_package(
            package, cache, opener=opener, log=lambda _: None
        )
    except GuestToolchainError:
        pass
    else:
        raise AssertionError(f"{name} download mismatch was accepted")
    assert not list(cache.glob("*.pkg")), list(cache.glob("*.pkg"))
    assert not list(cache.glob("*.part")), list(cache.glob("*.part"))


def assert_catalog_digest_required(root: Path, good: bytes) -> None:
    package = replace(package_for(good), sha1=hashlib.sha1(good).hexdigest())
    guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256[package.package_id] = (
        hashlib.sha256(good).hexdigest()
    )
    cache = root / "wrong-digest-domain"
    cache.mkdir()
    cached = cache / package.cache_name
    cached.write_bytes(good)
    downloads = []

    def opener(url, **_):
        assert not cached.exists()
        downloads.append(url)
        return Response(good)

    try:
        guest_toolchain_module._cached_package(
            package, cache, opener=opener, log=lambda _: None
        )
    except GuestToolchainError as error:
        assert error.kind == "download", error.kind
    else:
        raise AssertionError("full-payload SHA-1 was accepted as a catalog TOC digest")
    assert downloads == [package.url], downloads
    assert not list(cache.iterdir())


def main() -> None:
    good = valid_payload()
    bad_sha256 = good[:-1] + bytes([good[-1] ^ 1])
    bad_size = good + b"wrong-size"
    bad_xar = b"not-xar" + good[7:]
    original = dict(guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256)
    try:
        with tempfile.TemporaryDirectory(prefix="west-guest-toolchain-cache-contract-") as raw:
            root = Path(raw)
            for name, bad in (
                ("sha256", bad_sha256),
                ("size", bad_size),
                ("xar", bad_xar),
                ("toc", good[:28] + bytes([good[28] ^ 1]) + good[29:]),
                ("toc-boundary", good[:8] + struct.pack(">Q", len(good)) + good[16:]),
                ("checksum-algorithm", good[:24] + struct.pack(">I", 0) + good[28:]),
            ):
                assert_cached_mismatch_redownloads(root, name, good, bad)
                assert_download_mismatch_fails(root, name, good, bad)
            assert_catalog_digest_required(root, good)
    finally:
        guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256.clear()
        guest_toolchain_module.REVIEWED_COMMAND_LINE_TOOLS_SHA256.update(original)
    print("PASS guest-toolchain-cache-contract")


if __name__ == "__main__":
    main()

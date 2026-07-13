"""Provision external toolchains required by guest compatibility tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    from .prefix_repair import (
        guest_c_fixture_prerequisite_problems,
        repair_prefix_prerequisites,
    )
    from .test_execution import ProcessResult
    from .test_guest_execution import run_guest_argv
except ImportError:  # Loaded as a West extension module, not a package.
    from prefix_repair import (
        guest_c_fixture_prerequisite_problems,
        repair_prefix_prerequisites,
    )
    from test_execution import ProcessResult
    from test_guest_execution import run_guest_argv


COMMAND_LINE_TOOLS_RESOURCE = "darling-command-line-tools"
COMMAND_LINE_TOOLS_MANIFEST_URL = (
    "https://swdistcache.darlinghq.org/api/v1/products/by-tag?tag=DTCommandLineTools"
)
COMMAND_LINE_TOOLS_PACKAGE_IDS = (
    "com.apple.pkg.CLTools_SDK_OSX1012",
    "com.apple.pkg.DevSDK_OSX1012",
    "com.apple.pkg.CLTools_SDK_macOSSDK",
    "com.apple.pkg.CLTools_SDK_macOS1013",
    "com.apple.pkg.CLTools_Executables",
)
DEFAULT_GUEST_CC = "/Library/Developer/CommandLineTools/usr/bin/clang"
DEFAULT_GUEST_CFLAGS = (
    "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
)


class GuestToolchainError(RuntimeError):
    """Raised when a declared guest toolchain cannot be made usable."""


@dataclass(frozen=True)
class CommandLineToolsPackage:
    """One package from Darling's CommandLineTools distribution manifest."""

    package_id: str
    url: str
    size: int
    sha1: str

    @property
    def cache_name(self) -> str:
        return f"{self.package_id}.{self.sha1}.pkg"


def _package_from_json(value: object) -> CommandLineToolsPackage:
    if not isinstance(value, dict):
        raise GuestToolchainError("CommandLineTools manifest package must be a mapping")
    package_id = value.get("id")
    url = value.get("url")
    size = value.get("size")
    sha1 = value.get("digest")
    if (
        not isinstance(package_id, str)
        or not package_id
        or not isinstance(url, str)
        or not url.startswith(("https://", "http://"))
        or type(size) is not int
        or size <= 0
        or not isinstance(sha1, str)
        or len(sha1) != 40
        or any(character not in "0123456789abcdefABCDEF" for character in sha1)
    ):
        raise GuestToolchainError("invalid CommandLineTools package metadata")
    return CommandLineToolsPackage(package_id, url, size, sha1.lower())


def command_line_tools_packages(payload: object) -> tuple[CommandLineToolsPackage, ...]:
    """Select the complete ordered package set from Darling's API response."""

    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise GuestToolchainError("CommandLineTools manifest must contain one product")
    values = payload[0].get("packages")
    if not isinstance(values, list):
        raise GuestToolchainError("CommandLineTools manifest has no package list")
    packages: dict[str, CommandLineToolsPackage] = {}
    for value in values:
        package = _package_from_json(value)
        packages[package.package_id] = package
    missing = [
        package_id
        for package_id in COMMAND_LINE_TOOLS_PACKAGE_IDS
        if package_id not in packages
    ]
    if missing:
        raise GuestToolchainError(
            "CommandLineTools manifest is missing package(s): " + ", ".join(missing)
        )
    return tuple(packages[package_id] for package_id in COMMAND_LINE_TOOLS_PACKAGE_IDS)


def _read_manifest(*, opener: Callable[..., object] = urllib.request.urlopen) -> object:
    try:
        with opener(COMMAND_LINE_TOOLS_MANIFEST_URL, timeout=30) as response:
            return json.loads(response.read())
    except Exception as error:
        raise GuestToolchainError(f"cannot read CommandLineTools manifest: {error}") from error


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_package(
    package: CommandLineToolsPackage,
    cache_dir: Path,
    *,
    opener: Callable[..., object],
    log: Callable[[str], None],
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / package.cache_name
    if (
        cached.is_file()
        and cached.stat().st_size == package.size
        and _sha1(cached) == package.sha1
    ):
        log(f"guest toolchain cache hit: {package.package_id}")
        return cached

    if cached.exists():
        cached.unlink()
    partial = cache_dir / f".{package.cache_name}.part"
    partial.unlink(missing_ok=True)
    log(f"guest toolchain download: {package.package_id} ({package.size} bytes)")
    try:
        with opener(package.url, timeout=120) as response, partial.open(
            "wb"
        ) as output:
            copied = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                copied += len(chunk)
                if copied % (32 * 1024 * 1024) < len(chunk):
                    log(f"guest toolchain download: {package.package_id}: {copied} bytes")
        if partial.stat().st_size != package.size:
            raise GuestToolchainError(
                f"downloaded {package.package_id} has size {partial.stat().st_size}, "
                f"expected {package.size}"
            )
        digest = _sha1(partial)
        if digest != package.sha1:
            raise GuestToolchainError(
                f"downloaded {package.package_id} has SHA-1 {digest}, "
                f"expected {package.sha1}"
            )
        partial.replace(cached)
        return cached
    except GuestToolchainError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as error:
        partial.unlink(missing_ok=True)
        raise GuestToolchainError(
            f"cannot download {package.package_id}: {error}"
        ) from error


def ensure_command_line_tools(
    *,
    prefix: Path,
    launcher: str,
    cwd: Path,
    env: dict[str, str],
    cache_dir: Path | None = None,
    timeout_seconds: int = 900,
    opener: Callable[..., object] = urllib.request.urlopen,
    guest_runner: Callable[..., ProcessResult] = run_guest_argv,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Make the default guest C compiler and SDK available in *prefix*.

    The provider is idempotent. It installs Apple's packages through Darling's
    own guest ``installer`` so paths, symlinks, and package payload semantics
    are the same as a normal Darling installation. Package bytes live only in
    the host cache and prefix-owned temporary storage, never in patch metadata.
    """

    missing = guest_c_fixture_prerequisite_problems(
        prefix, DEFAULT_GUEST_CC, DEFAULT_GUEST_CFLAGS
    )
    if not missing:
        return ["guest CommandLineTools already provisioned"]
    log("guest CommandLineTools missing: " + "; ".join(missing))

    packages = command_line_tools_packages(_read_manifest(opener=opener))
    resolved_cache = cache_dir or Path(
        os.environ.get("DARLING_CLT_CACHE", "~/.cache/west/darling-command-line-tools")
    ).expanduser()
    staged_dir = prefix / "private/var/tmp"
    staged_dir.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    try:
        for package in packages:
            cached = _cached_package(package, resolved_cache, opener=opener, log=log)
            staged = staged_dir / f"west-{package.cache_name}"
            shutil.copyfile(cached, staged)
            guest_path = f"/private/var/tmp/{staged.name}"
            log(f"guest toolchain install: {package.package_id}")
            result = guest_runner(
                launcher,
                prefix,
                ("/usr/bin/installer", "-pkg", guest_path, "-target", "/"),
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
                capture_output=True,
                heartbeat_seconds=30,
                output_line=lambda stream, line: log(
                    f"guest installer {stream}: {line}"
                ),
            )
            if result.returncode != 0 or result.timed_out:
                detail = _result_output(result)
                raise GuestToolchainError(
                    f"guest installer failed for {package.package_id} "
                    f"(rc={result.returncode}, timed_out={result.timed_out}): "
                    f"{detail[-1000:]}"
                )
            changed.append(package.package_id)
            staged.unlink(missing_ok=True)
    finally:
        for staged in staged_dir.glob("west-com.apple.pkg.*.pkg"):
            staged.unlink(missing_ok=True)

    repair = repair_prefix_prerequisites(prefix)
    if repair.problems:
        raise GuestToolchainError(
            "CommandLineTools installed but prefix repair failed: "
            + "; ".join(repair.problems)
        )
    remaining = guest_c_fixture_prerequisite_problems(
        prefix, DEFAULT_GUEST_CC, DEFAULT_GUEST_CFLAGS
    )
    if remaining:
        raise GuestToolchainError(
            "CommandLineTools installation did not satisfy guest C contract: "
            + "; ".join(remaining)
        )
    return changed


def _result_output(result: ProcessResult) -> str:
    def text(value: str | bytes) -> str:
        return value.decode(errors="replace") if isinstance(value, bytes) else value

    return text(result.stdout) + text(result.stderr)

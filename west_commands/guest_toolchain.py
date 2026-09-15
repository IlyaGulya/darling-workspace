"""Provision external toolchains required by guest compatibility tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

try:
    from .prefix_repair import (
        guest_c_fixture_prerequisite_problems,
        repair_prefix_prerequisites,
    )
    from .test_execution import ProcessResult
    from .test_guest_execution import run_guest_shell_argv
except ImportError:  # Loaded as a West extension module, not a package.
    from prefix_repair import (
        guest_c_fixture_prerequisite_problems,
        repair_prefix_prerequisites,
    )
    from test_execution import ProcessResult
    from test_guest_execution import run_guest_shell_argv


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
# Fixed full-payload pins for the exact CLT package set used by this fork;
# these pins are not publisher authentication. The Darling catalog's SHA-1
# identifies the compressed XAR TOC, not the full .pkg bytes (all five packages
# in product 041-90419 match). Require both digests, covering different bytes.
REVIEWED_COMMAND_LINE_TOOLS_SHA256 = {
    "com.apple.pkg.CLTools_SDK_OSX1012":
        "b1257b424bc743bfd17348f93bb0a1823a1455e3a3982db2176cc51a27180285",
    "com.apple.pkg.DevSDK_OSX1012":
        "30ea9857e79adb7ed03d089015e6cdd72407ed3307780ecf58038db33419b88f",
    "com.apple.pkg.CLTools_SDK_macOSSDK":
        "27678b01141739175992a9b027c875b4c4ffe704de95f743c1f3b90374029f49",
    "com.apple.pkg.CLTools_SDK_macOS1013":
        "6320cc77a7e2e9b429c21c2dcc82fee91992a9ecfb2ebaca870a4b96e759ccc8",
    "com.apple.pkg.CLTools_Executables":
        "95df96bfc8369bbd9ecf9acccd36b4020e2885777524a97b3aa288f660be5d32",
}
# Locally supplied CLT13.2 bytes; this pin is not publisher authentication.
SELECTED_COMMAND_LINE_TOOLS_ID = "Command_Line_Tools_for_Xcode_13.2"
SELECTED_COMMAND_LINE_TOOLS_SHA256 = (
    "7d45d981d51dce2b91de369eba56dbaf77ae175ff89bcaf6a885c3095587f8a8"
)
SELECTED_COMMAND_LINE_TOOLS_CLANG = "clang-1300.0.29.30"
COMMAND_LINE_TOOLS_RECEIPT = ".west-command-line-tools.json"
# The entry the selected installer creates and refuses to replace. A
# prefix bootstrapped with an older CommandLineTools layout holds a
# directory or a symlink at this path, and the installer stops on it.
COMMAND_LINE_TOOLS_SDK_ENTRY = "Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
DEFAULT_GUEST_CC = "/Library/Developer/CommandLineTools/usr/bin/clang"
DEFAULT_GUEST_CFLAGS = (
    "-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
)


class GuestToolchainError(RuntimeError):
    """Raised when a declared guest toolchain cannot be made usable."""

    def __init__(self, message: str, *, kind: str = "setup"):
        super().__init__(message)
        self.kind = kind


def guest_toolchain_provisioning_forbidden(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the caller explicitly requires a no-CLT execution path."""

    values = os.environ if environ is None else environ
    return values.get("WEST_TEST_FORBID_GUEST_TOOLCHAIN") == "1"


def require_guest_toolchain_provisioning_allowed(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Reject guest toolchain provisioning in an explicitly no-CLT tier."""

    if guest_toolchain_provisioning_forbidden(environ):
        raise GuestToolchainError(
            "guest toolchain provisioning is forbidden in a no-CLT tier",
            kind="policy",
        )


@dataclass(frozen=True)
class CommandLineToolsPackage:
    """One package from Darling's CommandLineTools distribution manifest."""

    package_id: str
    url: str
    size: int
    sha1: str  # Catalog digest of the compressed XAR TOC, not the full payload.

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
        or not url.startswith(
            ("https://swcdn.apple.com/", "http://swcdn.apple.com/")
        )
        or type(size) is not int
        or size <= 0
        or not isinstance(sha1, str)
        or len(sha1) != 40
        or any(character not in "0123456789abcdefABCDEF" for character in sha1)
    ):
        raise GuestToolchainError("invalid CommandLineTools package metadata")
    if url.startswith("http://"):
        url = "https://" + url.removeprefix("http://")
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
    packages: list[CommandLineToolsPackage] = []
    package_ids: set[str] = set()
    for value in values:
        package = _package_from_json(value)
        if package.package_id in package_ids:
            raise GuestToolchainError(
                f"CommandLineTools manifest repeats package {package.package_id}"
            )
        package_ids.add(package.package_id)
        if package.package_id not in REVIEWED_COMMAND_LINE_TOOLS_SHA256:
            raise GuestToolchainError(
                f"CommandLineTools package is not in the reviewed SHA-256 allowlist: "
                f"{package.package_id}",
                kind="setup",
            )
        packages.append(package)
    missing = [
        package_id
        for package_id in COMMAND_LINE_TOOLS_PACKAGE_IDS
        if package_id not in package_ids
    ]
    if missing:
        raise GuestToolchainError(
            "CommandLineTools manifest is missing package(s): " + ", ".join(missing)
        )
    return tuple(packages)


def _read_manifest(*, opener: Callable[..., object] = urllib.request.urlopen) -> object:
    try:
        with opener(COMMAND_LINE_TOOLS_MANIFEST_URL, timeout=30) as response:
            return json.loads(response.read())
    except Exception as error:
        raise GuestToolchainError(
            f"cannot read CommandLineTools manifest: {error}", kind="setup"
        ) from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_xar(path: Path, package: CommandLineToolsPackage) -> str:
    """Validate the envelope and hash the compressed TOC named by the catalog."""

    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(28)
            if size != package.size:
                raise GuestToolchainError(
                    f"downloaded {package.package_id} has size {size}, expected {package.size}",
                    kind="download",
                )
            if len(header) != 28 or header[:4] != b"xar!":
                raise GuestToolchainError(
                    f"downloaded {package.package_id} is not a XAR package",
                    kind="download",
                )
            _, header_size, version, toc_compressed, toc_uncompressed, checksum = (
                struct.unpack(">4sHHQQI", header)
            )
            if (
                version != 1
                or header_size < 28
                or toc_compressed == 0
                or toc_uncompressed == 0
                or checksum != 1
                or header_size + toc_compressed + 20 > size
            ):
                raise GuestToolchainError(
                    f"downloaded {package.package_id} has an invalid XAR header",
                    kind="download",
                )
            # XAR hashes the compressed bytes immediately following its header.
            # Stream them without allocating/decompressing an untrusted TOC.
            digest = hashlib.sha1()
            stream.seek(header_size)
            remaining = toc_compressed
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise GuestToolchainError(
                        f"downloaded {package.package_id} has a truncated XAR TOC",
                        kind="download",
                    )
                digest.update(chunk)
                remaining -= len(chunk)
            return digest.hexdigest()
    except OSError as error:
        raise GuestToolchainError(
            f"cannot inspect downloaded {package.package_id}: {error}",
            kind="download",
        ) from error


def _verify_package(path: Path, package: CommandLineToolsPackage, log: Callable[[str], None]) -> None:
    actual_sha1 = _validate_xar(path, package)
    if actual_sha1 != package.sha1:
        raise GuestToolchainError(
            f"downloaded {package.package_id} has compressed XAR TOC SHA-1 "
            f"{actual_sha1}, expected catalog digest {package.sha1}",
            kind="download",
        )
    actual_sha256 = _sha256(path)
    expected_sha256 = REVIEWED_COMMAND_LINE_TOOLS_SHA256[package.package_id]
    if actual_sha256 != expected_sha256:
        raise GuestToolchainError(
            f"downloaded {package.package_id} has unreviewed SHA-256 "
            f"{actual_sha256}, expected {expected_sha256}",
            kind="download",
        )


def _cached_package(
    package: CommandLineToolsPackage,
    cache_dir: Path,
    *,
    opener: Callable[..., object],
    log: Callable[[str], None],
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / package.cache_name
    if cached.is_file():
        try:
            _verify_package(cached, package, log)
        except GuestToolchainError:
            cached.unlink()
        else:
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
        _verify_package(partial, package, log)
        partial.replace(cached)
        return cached
    except GuestToolchainError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as error:
        partial.unlink(missing_ok=True)
        raise GuestToolchainError(
            f"cannot download {package.package_id}: {error}", kind="download"
        ) from error


def _verify_selected_package(path: Path) -> None:
    """Authenticate the one supported local package, including staged bytes."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise GuestToolchainError(
                f"selected CommandLineTools package is not a regular file: {path}",
                kind="download",
            )
        actual = _sha256(path)
    except OSError as error:
        raise GuestToolchainError(
            f"cannot read selected CommandLineTools package {path}: {error}",
            kind="download",
        ) from error
    if actual != SELECTED_COMMAND_LINE_TOOLS_SHA256:
        raise GuestToolchainError(
            f"selected CommandLineTools package has SHA-256 {actual}, "
            f"expected {SELECTED_COMMAND_LINE_TOOLS_SHA256}",
            kind="download",
        )


def _selected_prerequisite_problems(
    prefix: Path,
    launcher: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    guest_runner: Callable[..., ProcessResult],
) -> list[str]:
    problems = guest_c_fixture_prerequisite_problems(
        prefix,
        DEFAULT_GUEST_CC,
        DEFAULT_GUEST_CFLAGS
        + " -isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX12.1.sdk",
    )
    if problems:
        return problems
    result = guest_runner(
        launcher, prefix, (DEFAULT_GUEST_CC, "--version"),
        cwd=cwd, env=env, timeout_seconds=timeout_seconds, capture_output=True,
    )
    if (
        result.returncode != 0
        or result.timed_out
        or SELECTED_COMMAND_LINE_TOOLS_CLANG not in _result_output(result)
    ):
        problems.append(
            "selected CommandLineTools compiler version is not "
            + SELECTED_COMMAND_LINE_TOOLS_CLANG
        )
    return problems


def conflicting_command_line_tools_sdk(prefix: Path) -> str | None:
    """Return the existing SDK directory the selected installer cannot replace.

    The installer creates ``SDKs/MacOSX.sdk`` and stops if that name is taken by
    a real directory, which is what a prefix bootstrapped with an older
    CommandLineTools catalogue holds. Detecting it here reports a named conflict,
    where the installer's own message is a symlink error buried in its output.

    A symlink at that path is not reported: every validated prefix has one, and
    prefix repair is what creates it.
    """

    entry = prefix / COMMAND_LINE_TOOLS_SDK_ENTRY
    if entry.is_symlink():
        return None
    if entry.exists():
        return f"{COMMAND_LINE_TOOLS_SDK_ENTRY} already exists as a directory"
    return None


def _ensure_selected_command_line_tools(
    package: Path,
    *,
    prefix: Path,
    launcher: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    guest_runner: Callable[..., ProcessResult],
    log: Callable[[str], None],
) -> list[str]:
    # Authenticate even on reuse, before repair, staging, or receipt mutation.
    _verify_selected_package(package)
    receipt = prefix / COMMAND_LINE_TOOLS_RECEIPT
    identity = {
        "package": SELECTED_COMMAND_LINE_TOOLS_ID,
        "sha256": SELECTED_COMMAND_LINE_TOOLS_SHA256,
    }
    probe_args = dict(
        cwd=cwd, env=env, timeout_seconds=timeout_seconds, guest_runner=guest_runner,
    )
    try:
        installed = json.loads(receipt.read_text())
    except (OSError, ValueError):
        installed = None
    if installed == identity and not _selected_prerequisite_problems(
        prefix, launcher, **probe_args
    ):
        return ["guest CommandLineTools already provisioned"]

    conflict = conflicting_command_line_tools_sdk(prefix)
    if conflict is not None:
        raise GuestToolchainError(
            f"this prefix already holds an older CommandLineTools SDK layout: "
            f"{conflict}. The {SELECTED_COMMAND_LINE_TOOLS_ID} installer cannot "
            "replace it and stops inside its own output, so provisioning reports the "
            "conflict here instead. Bootstrap a fresh prefix with DARLING_CLT_PACKAGE "
            "set (the darling-boot skill documents the sequence), or remove that entry "
            "first if this prefix is disposable.",
            kind="conflict",
        )

    # A failed reinstall must not leave a previous success claim behind.
    receipt.unlink(missing_ok=True)
    staged_dir = prefix / "private/var/tmp"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="west-clt13.2-", suffix=".pkg", dir=staged_dir, delete=False,
        ) as output:
            staged = Path(output.name)
            with package.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        # Catch source replacement or modification between verification and copy.
        _verify_selected_package(staged)
        log(f"guest toolchain install: {SELECTED_COMMAND_LINE_TOOLS_ID}")
        result = guest_runner(
            launcher, prefix,
            ("/usr/bin/installer", "-pkg", f"/private/var/tmp/{staged.name}", "-target", "/"),
            cwd=cwd, env=env, timeout_seconds=timeout_seconds,
            capture_output=True, heartbeat_seconds=30,
            output_line=lambda stream, line: log(f"guest installer {stream}: {line}"),
        )
        if result.returncode != 0 or result.timed_out:
            raise GuestToolchainError(
                f"guest installer failed for {SELECTED_COMMAND_LINE_TOOLS_ID} "
                f"(rc={result.returncode}, timed_out={result.timed_out}): "
                f"{_result_output(result)[-1000:]}",
                kind="install",
            )
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)

    repair = repair_prefix_prerequisites(prefix)
    problems = repair.problems + _selected_prerequisite_problems(
        prefix, launcher, **probe_args
    )
    if problems:
        raise GuestToolchainError(
            "selected CommandLineTools installation did not satisfy prerequisites: "
            + "; ".join(problems),
            kind="setup",
        )
    pending = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=COMMAND_LINE_TOOLS_RECEIPT + ".", dir=prefix, delete=False,
        ) as output:
            pending = Path(output.name)
            json.dump(identity, output)
            output.write("\n")
        pending.replace(receipt)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return [SELECTED_COMMAND_LINE_TOOLS_ID]


def ensure_command_line_tools(
    *,
    prefix: Path,
    launcher: str,
    cwd: Path,
    env: dict[str, str],
    cache_dir: Path | None = None,
    timeout_seconds: int = 900,
    opener: Callable[..., object] = urllib.request.urlopen,
    guest_runner: Callable[..., ProcessResult] = run_guest_shell_argv,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Make the default guest C compiler and SDK available in *prefix*.

    The provider is idempotent. It installs Apple's packages through Darling's
    own guest ``installer`` so paths, symlinks, and package payload semantics
    are the same as a normal Darling installation. Package bytes live only in
    the host cache and prefix-owned temporary storage, never in patch metadata.
    ``DARLING_CLT_PACKAGE`` opts into the pinned local CLT13.2 package instead
    of the unchanged mirrored package set.
    """

    require_guest_toolchain_provisioning_allowed()
    require_guest_toolchain_provisioning_allowed(env)
    selected = env.get("DARLING_CLT_PACKAGE", os.environ.get("DARLING_CLT_PACKAGE"))
    if selected is not None:
        return _ensure_selected_command_line_tools(
            Path(selected).expanduser(),
            prefix=prefix, launcher=launcher, cwd=cwd, env=env,
            timeout_seconds=timeout_seconds, guest_runner=guest_runner, log=log,
        )

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
    staged_paths: list[Path] = []
    try:
        for package in packages:
            cached = _cached_package(package, resolved_cache, opener=opener, log=log)
            with tempfile.NamedTemporaryFile(
                prefix=f"west-{package.package_id}.", suffix=".pkg",
                dir=staged_dir, delete=False,
            ) as output:
                staged = Path(output.name)
            staged_paths.append(staged)
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
                    f"{detail[-1000:]}",
                    kind="install",
                )
            changed.append(package.package_id)
            staged.unlink(missing_ok=True)
    finally:
        for staged in staged_paths:
            staged.unlink(missing_ok=True)

    repair = repair_prefix_prerequisites(prefix)
    if repair.problems:
        raise GuestToolchainError(
            "CommandLineTools installed but prefix repair failed: "
            + "; ".join(repair.problems),
            kind="setup",
        )
    remaining = guest_c_fixture_prerequisite_problems(
        prefix, DEFAULT_GUEST_CC, DEFAULT_GUEST_CFLAGS
    )
    if remaining:
        raise GuestToolchainError(
            "CommandLineTools installation did not satisfy guest C contract: "
            + "; ".join(remaining),
            kind="setup",
        )
    return changed


def _result_output(result: ProcessResult) -> str:
    def text(value: str | bytes) -> str:
        return value.decode(errors="replace") if isinstance(value, bytes) else value

    return text(result.stdout) + text(result.stderr)

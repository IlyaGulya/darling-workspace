"""Validation and execution for checked-in guest Mach-O corpus fixtures."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shlex import quote
from typing import Any, Mapping

import yaml

try:
    from .guest_toolchain import (
        COMMAND_LINE_TOOLS_PACKAGE_IDS,
        REVIEWED_COMMAND_LINE_TOOLS_SHA256,
        guest_toolchain_provisioning_forbidden,
    )
    from .test_guest_execution import run_guest_shell
except ImportError:  # Loaded as a West extension module, not a package.
    from guest_toolchain import (
        COMMAND_LINE_TOOLS_PACKAGE_IDS,
        REVIEWED_COMMAND_LINE_TOOLS_SHA256,
        guest_toolchain_provisioning_forbidden,
    )
    from test_guest_execution import run_guest_shell


_MACHO_MAGIC_64 = {
    b"\xcf\xfa\xed\xfe": ("<", "MH_MAGIC_64"),
    b"\xfe\xed\xfa\xcf": (">", "MH_CIGAM_64"),
}
_CPU_ARCHITECTURES = {0x01000007: "x86_64"}
_FILE_TYPES = {2: "MH_EXECUTE"}
_LC_RPATH = 0x8000001C
_DYLIB_COMMANDS = {
    0x0000000C: "LC_LOAD_DYLIB",
    0x00000018 | 0x80000000: "LC_LOAD_WEAK_DYLIB",
    0x0000001F | 0x80000000: "LC_REEXPORT_DYLIB",
    0x00000020: "LC_LAZY_LOAD_DYLIB",
    0x00000023 | 0x80000000: "LC_LOAD_UPWARD_DYLIB",
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_PREFIX_NAME = re.compile(
    r"^darling-rootless-(?P<kind>[A-Za-z0-9_]+)(?:[.-][A-Za-z0-9_]+)*$"
)


class GuestMachoFixtureError(ValueError):
    """Raised when a corpus fixture cannot be trusted or executed."""


@dataclass(frozen=True)
class MachOHeader:
    magic: str
    architecture: str
    filetype: str
    dylib_load_commands: tuple[dict[str, str], ...]
    rpath_load_commands: tuple[str, ...]


@dataclass(frozen=True)
class GuestMachoFixture:
    name: str
    corpus_path: Path
    source_path: Path
    artifact_path: Path
    artifact_sha256: str
    header: MachOHeader
    expected_returncode: int
    expected_stdout: tuple[str, ...]
    runtime: Mapping[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_repo_path(root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise GuestMachoFixtureError(f"{field} must be a non-empty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise GuestMachoFixtureError(f"{field} must stay below the workspace root")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise GuestMachoFixtureError(f"{field} escapes the workspace root") from error
    return resolved


def _require_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise GuestMachoFixtureError(f"{field} must be a lowercase SHA-256")
    return value


def _macho_header(path: Path) -> MachOHeader:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise GuestMachoFixtureError(f"cannot read Mach-O artifact {path}: {error}") from error
    if len(payload) < 32:
        raise GuestMachoFixtureError(f"Mach-O artifact is shorter than its 64-bit header: {path}")
    try:
        endian, magic = _MACHO_MAGIC_64[payload[:4]]
    except KeyError as error:
        raise GuestMachoFixtureError(f"unsupported or non-64-bit Mach-O header: {path}") from error
    if magic != "MH_MAGIC_64":
        raise GuestMachoFixtureError(f"Mach-O must use MH_MAGIC_64: {path}")
    _, cputype, _, filetype, ncmds, sizeofcmds, _, _ = struct.unpack_from(
        f"{endian}IiiIIIII", payload, 0
    )
    architecture = _CPU_ARCHITECTURES.get(cputype)
    if architecture is None:
        raise GuestMachoFixtureError(f"unsupported Mach-O CPU type {cputype:#x}: {path}")
    if architecture != "x86_64":
        raise GuestMachoFixtureError(f"Mach-O must use x86_64: {path}")
    if filetype != 2:
        raise GuestMachoFixtureError(f"Mach-O must use MH_EXECUTE: {path}")
    if 32 + sizeofcmds > len(payload):
        raise GuestMachoFixtureError(f"Mach-O load commands exceed artifact size: {path}")

    commands: list[dict[str, str]] = []
    rpaths: list[str] = []
    offset = 32
    for index in range(ncmds):
        if offset + 8 > len(payload):
            raise GuestMachoFixtureError(f"Mach-O load command {index} has no header: {path}")
        command, command_size = struct.unpack_from(f"{endian}II", payload, offset)
        if command_size < 8 or offset + command_size > 32 + sizeofcmds:
            raise GuestMachoFixtureError(f"invalid Mach-O load command {index}: {path}")
        command_name = _DYLIB_COMMANDS.get(command)
        if command_name is not None:
            if command_size < 24:
                raise GuestMachoFixtureError(f"short dylib load command {index}: {path}")
            name_offset = struct.unpack_from(f"{endian}I", payload, offset + 8)[0]
            if name_offset >= command_size:
                raise GuestMachoFixtureError(f"dylib name escapes load command {index}: {path}")
            name_start = offset + name_offset
            name_end = payload.find(b"\0", name_start, offset + command_size)
            if name_end < 0:
                raise GuestMachoFixtureError(f"unterminated dylib name in command {index}: {path}")
            try:
                name = payload[name_start:name_end].decode("utf-8")
            except UnicodeDecodeError as error:
                raise GuestMachoFixtureError(
                    f"invalid UTF-8 in dylib name command {index}: {path}"
                ) from error
            commands.append(
                {"command": command_name, "name": name}
            )
        elif command == _LC_RPATH:
            if command_size < 12:
                raise GuestMachoFixtureError(f"short LC_RPATH load command {index}: {path}")
            path_offset = struct.unpack_from(f"{endian}I", payload, offset + 8)[0]
            if path_offset >= command_size:
                raise GuestMachoFixtureError(f"LC_RPATH path escapes load command {index}: {path}")
            path_start = offset + path_offset
            path_end = payload.find(b"\0", path_start, offset + command_size)
            if path_end < 0:
                raise GuestMachoFixtureError(f"unterminated LC_RPATH path {index}: {path}")
            try:
                rpaths.append(payload[path_start:path_end].decode("utf-8"))
            except UnicodeDecodeError as error:
                raise GuestMachoFixtureError(
                    f"invalid UTF-8 in LC_RPATH path {index}: {path}"
                ) from error
        offset += command_size

    return MachOHeader(
        magic=magic,
        architecture=architecture,
        filetype=_FILE_TYPES.get(filetype, f"UNKNOWN({filetype})"),
        dylib_load_commands=tuple(commands),
        rpath_load_commands=tuple(rpaths),
    )


def _require_reviewed_toolchain(root: Path, toolchain: Mapping[str, Any]) -> None:
    if toolchain.get("product-id") != "041-90419":
        raise GuestMachoFixtureError("corpus toolchain must use reviewed product 041-90419")
    provenance = _safe_repo_path(root, toolchain.get("provenance"), "toolchain.provenance")
    try:
        provenance_text = provenance.read_text()
    except OSError as error:
        raise GuestMachoFixtureError(f"cannot read toolchain provenance {provenance}: {error}") from error
    for marker in ("review_status: reviewed", "evidence_run:", "product_id: 041-90419"):
        if marker not in provenance_text:
            raise GuestMachoFixtureError(f"toolchain provenance lacks reviewed marker {marker!r}")
    package_digests = toolchain.get("package-sha256")
    if not isinstance(package_digests, dict):
        raise GuestMachoFixtureError("toolchain.package-sha256 must be a mapping")
    if package_digests != dict(REVIEWED_COMMAND_LINE_TOOLS_SHA256):
        raise GuestMachoFixtureError("corpus package digests differ from the reviewed CLT allowlist")
    for package_id in COMMAND_LINE_TOOLS_PACKAGE_IDS:
        digest = package_digests.get(package_id)
        if digest not in provenance_text:
            raise GuestMachoFixtureError(
                f"toolchain provenance does not contain reviewed package {package_id}"
            )


def _require_build_provenance(root: Path, fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    build = fixture.get("build")
    if not isinstance(build, dict):
        raise GuestMachoFixtureError("fixture.build must be a mapping")
    for field in ("compiler-path", "compiler-version", "flags"):
        if not build.get(field):
            raise GuestMachoFixtureError(f"fixture.build.{field} is required")
    if not isinstance(build["flags"], list) or not all(
        isinstance(value, str) and value for value in build["flags"]
    ):
        raise GuestMachoFixtureError("fixture.build.flags must be a non-empty string list")
    builds = fixture.get("independent-builds")
    if not isinstance(builds, list) or len(builds) != 2:
        raise GuestMachoFixtureError("fixture.independent-builds must contain exactly two builds")
    result = []
    for index, item in enumerate(builds):
        if not isinstance(item, dict):
            raise GuestMachoFixtureError(f"independent-builds[{index}] must be a mapping")
        digest = _require_hex(item.get("artifact-sha256"), f"independent-builds[{index}].artifact-sha256")
        if item.get("compiler-path") != build["compiler-path"]:
            raise GuestMachoFixtureError(f"independent-builds[{index}] compiler path differs from build")
        if item.get("flags") != build["flags"]:
            raise GuestMachoFixtureError(f"independent-builds[{index}] flags differ from build")
        result.append({**item, "artifact-sha256": digest})
    return result


def load_guest_macho_fixture(root: Path, corpus: str, fixture_name: str) -> GuestMachoFixture:
    """Load and fully validate one corpus entry before any guest launch."""

    corpus_path = _safe_repo_path(root, corpus, "corpus")
    try:
        document = yaml.safe_load(corpus_path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise GuestMachoFixtureError(f"cannot read corpus manifest {corpus_path}: {error}") from error
    if not isinstance(document, dict) or document.get("schema") != 1:
        raise GuestMachoFixtureError("corpus manifest must have schema: 1")
    _require_reviewed_toolchain(root, document.get("toolchain", {}))
    fixtures = document.get("fixtures")
    if not isinstance(fixtures, dict) or fixture_name not in fixtures:
        raise GuestMachoFixtureError(f"corpus fixture is missing: {fixture_name}")
    fixture = fixtures[fixture_name]
    if not isinstance(fixture, dict) or not _SAFE_NAME.fullmatch(fixture_name):
        raise GuestMachoFixtureError(f"invalid corpus fixture name: {fixture_name}")
    source_path = _safe_repo_path(root, fixture.get("source"), "fixture.source")
    artifact_path = _safe_repo_path(root, fixture.get("artifact"), "fixture.artifact")
    if not source_path.is_file():
        raise GuestMachoFixtureError(f"fixture source is missing: {source_path}")
    if not artifact_path.is_file() or not os.access(artifact_path, os.X_OK):
        raise GuestMachoFixtureError(f"fixture artifact is missing or not executable: {artifact_path}")
    source_sha256 = _require_hex(fixture.get("source-sha256"), "fixture.source-sha256")
    artifact_sha256 = _require_hex(fixture.get("artifact-sha256"), "fixture.artifact-sha256")
    if _sha256(source_path) != source_sha256:
        raise GuestMachoFixtureError(f"fixture source SHA-256 mismatch: {source_path}")
    actual_artifact_sha256 = _sha256(artifact_path)
    if actual_artifact_sha256 != artifact_sha256:
        raise GuestMachoFixtureError(f"fixture artifact SHA-256 mismatch: {artifact_path}")
    independent_builds = _require_build_provenance(root, fixture)
    if any(item["artifact-sha256"] != artifact_sha256 for item in independent_builds):
        raise GuestMachoFixtureError("independent build SHA-256 values do not match the fixture artifact")

    header = _macho_header(artifact_path)
    expected_header = fixture.get("macho")
    if not isinstance(expected_header, dict):
        raise GuestMachoFixtureError("fixture.macho must be a mapping")
    for field, actual in (
        ("magic", header.magic),
        ("architecture", header.architecture),
        ("filetype", header.filetype),
    ):
        if expected_header.get(field) != actual:
            raise GuestMachoFixtureError(
                f"Mach-O {field} mismatch: expected {expected_header.get(field)!r}, got {actual!r}"
            )
    expected_load_commands = expected_header.get("dylib-load-commands")
    if expected_load_commands != list(header.dylib_load_commands):
        raise GuestMachoFixtureError(
            f"Mach-O dylib load commands mismatch: expected {expected_load_commands!r}, "
            f"got {list(header.dylib_load_commands)!r}"
        )
    expected_rpaths = expected_header.get("rpath-load-commands")
    if expected_rpaths != list(header.rpath_load_commands):
        raise GuestMachoFixtureError(
            f"Mach-O LC_RPATH mismatch: expected {expected_rpaths!r}, "
            f"got {list(header.rpath_load_commands)!r}"
        )

    expected = fixture.get("expected")
    markers = expected.get("stdout-contains") if isinstance(expected, dict) else None
    if not isinstance(expected, dict) or not isinstance(markers, list) or not markers:
        raise GuestMachoFixtureError("fixture.expected needs returncode and stdout-contains")
    if not all(isinstance(marker, str) and marker.strip() for marker in markers):
        raise GuestMachoFixtureError("fixture.expected.stdout-contains must contain non-empty strings")
    if not isinstance(expected.get("returncode"), int) or isinstance(expected.get("returncode"), bool):
        raise GuestMachoFixtureError("fixture.expected.returncode must be an integer")
    runtime = fixture.get("runtime")
    if not isinstance(runtime, dict):
        raise GuestMachoFixtureError("fixture.runtime must be a mapping")
    if runtime.get("rootless") is not True or runtime.get("guest-toolchain") != "forbidden":
        raise GuestMachoFixtureError("fixture runtime must require rootless execution with guest toolchain forbidden")
    if runtime.get("prefix") != "tier-owned-fresh":
        raise GuestMachoFixtureError("fixture runtime must require a tier-owned fresh prefix")
    return GuestMachoFixture(
        name=fixture_name,
        corpus_path=corpus_path,
        source_path=source_path,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        header=header,
        expected_returncode=expected["returncode"],
        expected_stdout=tuple(markers),
        runtime=runtime,
    )


def _prefix_contains_guest_toolchain(prefix: Path) -> bool:
    return any(
        (prefix / relative).exists() or (prefix / relative).is_symlink()
        for relative in (
            "Library/Developer/CommandLineTools",
            "libexec/darling/Library/Developer/CommandLineTools",
        )
    )


def _trusted_root() -> Path:
    root_value = os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp"
    root = Path(os.path.realpath(root_value))
    repo_value = os.environ.get("ROOTLESS_TIER_REPO")
    if not repo_value:
        raise GuestMachoFixtureError("ROOTLESS_TIER_REPO is required for prefix ownership validation")
    repo = Path(os.path.realpath(repo_value))
    home = Path(os.path.realpath(os.environ.get("HOME", str(Path.home()))))
    if not root.is_dir():
        raise GuestMachoFixtureError(f"trusted root does not exist: {root}")
    if root == Path("/") or root == home or root == repo or repo in root.parents:
        raise GuestMachoFixtureError(f"trusted root is unsafe: {root}")
    if "RUNNER_TEMP" not in os.environ and home in root.parents:
        raise GuestMachoFixtureError(f"trusted root under HOME requires RUNNER_TEMP: {root}")
    return root


def _process_parent(pid: int) -> int | None:
    if pid == os.getpid():
        return os.getppid()
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _is_live_ancestor(pid: int) -> bool:
    if pid <= 1:
        return False
    current = _process_parent(os.getpid())
    if current is None:
        return False
    visited: set[int] = set()
    while current > 1 and current not in visited:
        if current == pid:
            return True
        visited.add(current)
        parent = _process_parent(current)
        if parent is None:
            return False
        current = parent
    return False


def _require_tier_owned_fresh_prefix(prefix: Path, env: Mapping[str, str]) -> None:
    if not prefix.is_dir() or prefix.is_symlink():
        raise GuestMachoFixtureError(f"prefix is not a tier-owned directory: {prefix}")
    trusted_root = _trusted_root()
    resolved_prefix = prefix.resolve(strict=False)
    if resolved_prefix.parent != trusted_root:
        raise GuestMachoFixtureError(
            f"prefix must be directly under trusted root {trusted_root}: {prefix}"
        )
    match = _PREFIX_NAME.fullmatch(prefix.name)
    if match is None:
        raise GuestMachoFixtureError(f"prefix has no lifecycle-owned name: {prefix}")
    owner = prefix.with_name(f"{prefix.name}.west-tier-owner")
    try:
        if not owner.is_file() or owner.is_symlink():
            raise GuestMachoFixtureError(f"tier-owned prefix owner is missing: {owner}")
        owner_lines = owner.read_text().splitlines()
    except OSError as error:
        raise GuestMachoFixtureError(f"cannot read tier prefix owner {owner}: {error}") from error
    fields: dict[str, str] = {}
    for line in owner_lines:
        if not line:
            continue
        if "=" not in line:
            raise GuestMachoFixtureError(f"malformed tier prefix owner: {owner}")
        key, value = line.split("=", 1)
        if key in fields or key not in {"schema", "kind", "pid"} or not value:
            raise GuestMachoFixtureError(f"invalid tier prefix owner: {owner}")
        fields[key] = value
    if set(fields) != {"schema", "kind", "pid"}:
        raise GuestMachoFixtureError(f"incomplete tier prefix owner: {owner}")
    if fields["schema"] != "1" or fields["kind"] != match.group("kind"):
        raise GuestMachoFixtureError(f"tier prefix owner does not match prefix: {prefix}")
    if not fields["pid"].isdigit() or int(fields["pid"]) <= 1:
        raise GuestMachoFixtureError(f"tier prefix owner has an invalid pid: {owner}")
    if not _is_live_ancestor(int(fields["pid"])):
        raise GuestMachoFixtureError(
            f"tier prefix owner pid is not a live ancestor of this West process: {owner}"
        )
    for name in ("DARLING_ROOTLESS", "DARLING_NOOVERLAYFS", "DARLING_EUNION"):
        if env.get(name) != "1":
            raise GuestMachoFixtureError(f"guest Mach-O runner requires {name}=1")
    if _prefix_contains_guest_toolchain(prefix):
        raise GuestMachoFixtureError(f"guest Mach-O prefix already contains CommandLineTools: {prefix}")


def run_guest_macho_fixture(command: Any, invocation: dict, env: dict[str, str] | None = None) -> int:
    """Run one validated prebuilt fixture without provisioning guest CLT."""

    run_env = env if env is not None else command._execution_env(invocation)
    if not run_env:
        run_env = os.environ.copy()
    if not guest_toolchain_provisioning_forbidden(run_env):
        command.die(f"{invocation['name']}: guest Mach-O runner requires no-CLT policy")
    if invocation.get("requires_profile") or invocation.get("runtime_profile"):
        command.die(f"{invocation['name']}: guest Mach-O runner cannot use a provisioning profile")

    fixture = load_guest_macho_fixture(Path(command.topdir), invocation["corpus"], invocation["fixture"])
    prefix_text = run_env.get("DPREFIX") or getattr(command, "_prefix", None)
    if not prefix_text:
        command.die(f"{invocation['name']}: guest Mach-O runner needs a prefix")
    prefix = Path(prefix_text)
    try:
        _require_tier_owned_fresh_prefix(prefix, run_env)
    except GuestMachoFixtureError as error:
        command.die(f"{invocation['name']}: {error}")
    guest = command._resolve_darling_launcher(str(prefix))
    if not guest:
        command.die(f"{invocation['name']}: guest Mach-O runner needs a Darling launcher")

    staged = prefix / "private/var/tmp" / f"west-macho-{fixture.name}"
    temporary = staged.with_name(f".{staged.name}.{os.getpid()}.tmp")
    staged.parent.mkdir(parents=True, exist_ok=True)
    guest_path = f"/private/var/tmp/{staged.name}"
    owned_temporary = False
    owned_staged = False
    try:
        try:
            if staged.exists() or staged.is_symlink():
                raise GuestMachoFixtureError(f"staging destination already exists: {staged}")
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{staged.name}.", suffix=".tmp", dir=staged.parent
            )
            temporary = Path(temporary_name)
            owned_temporary = True
            with os.fdopen(fd, "wb") as output, fixture.artifact_path.open("rb") as source:
                shutil.copyfileobj(source, output)
            if not temporary.is_file() or temporary.is_symlink():
                raise GuestMachoFixtureError(f"staging did not create a regular file: {temporary}")
            if _sha256(temporary) != fixture.artifact_sha256:
                raise GuestMachoFixtureError(f"staged artifact SHA-256 mismatch: {temporary}")
            temporary.chmod(0o755)
            os.replace(temporary, staged)
            owned_temporary = False
            owned_staged = True
        except (GuestMachoFixtureError, OSError) as error:
            command.die(f"{invocation['name']}: atomic fixture staging failed: {error}")
        result = run_guest_shell(
            guest,
            prefix,
            f"exec {quote(guest_path)}",
            cwd=Path(command.topdir),
            env=run_env,
            timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            capture_output=True,
        )
        output = str(result.stdout) + str(result.stderr)
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        if result.timed_out:
            command._record_failure_phase(invocation, "run")
            command.err(f"{invocation['name']}: prebuilt guest Mach-O timed out")
            return 124
        if result.returncode != fixture.expected_returncode:
            command._record_failure_phase(invocation, "run")
            command.err(
                f"{invocation['name']}: expected rc {fixture.expected_returncode}, "
                f"got {result.returncode}"
            )
            return result.returncode or 1
        missing = [marker for marker in fixture.expected_stdout if marker not in output]
        if missing:
            command._record_failure_phase(invocation, "run")
            command.err(f"{invocation['name']}: missing guest output marker(s): {', '.join(missing)}")
            return 1
        if _prefix_contains_guest_toolchain(prefix):
            command._record_failure_phase(invocation, "cleanup")
            command.err(f"{invocation['name']}: guest Mach-O run introduced CommandLineTools")
            return 1
        print(f"GUEST_MACHO_FIXTURE={fixture.name}")
        print(f"GUEST_MACHO_FIXTURE_SHA256={fixture.artifact_sha256}")
        print("GUEST_MACHO_GUEST_EXECUTION_OK")
        print("GUEST_MACHO_CLT_ABSENT=1")
        return 0
    finally:
        if owned_temporary:
            temporary.unlink(missing_ok=True)
        if owned_staged:
            staged.unlink(missing_ok=True)

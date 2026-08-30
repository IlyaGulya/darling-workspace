"""Bounded, capability-relative reader for Darling typed prefix state."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_STATE_BYTES = 2048
STATE_NAMES = (".darling-prefix-state-v3", ".darling-prefix-state-v2")
LEGACY_RUNTIME_MODE_NAME = ".darling-runtime-mode-v1"
RUNTIME_MODES = frozenset({"rootless-eunion", "privileged-eunion"})


class PrefixStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrefixState:
    schema_version: int
    runtime_mode: str
    generation: int
    prefix_device: int
    prefix_inode: int


@dataclass(frozen=True)
class PrefixStateModel:
    kind: str
    runtime_mode: str
    state: PrefixState | None


def read_prefix_state_model(prefix: Path) -> PrefixStateModel:
    """Read exactly one authoritative typed or legacy prefix-state form."""
    def present(name: str) -> bool:
        try:
            (prefix / name).lstat()
            return True
        except FileNotFoundError:
            return False

    typed_present = [present(name) for name in STATE_NAMES]
    marker_present = present(LEGACY_RUNTIME_MODE_NAME)
    if any(typed_present):
        if marker_present:
            raise PrefixStateError("legacy runtime mode is ambiguous with typed state")
        state = read_prefix_state(prefix)
        return PrefixStateModel(f"v{state.schema_version}", state.runtime_mode, state)
    if marker_present:
        mode = read_legacy_runtime_mode(prefix)
        return PrefixStateModel("legacy-marker", mode, None)
    raise PrefixStateError("runtime prefix has no recognized stable state")


def read_prefix_state(prefix: Path) -> PrefixState:
    """Prefer exact v3; accept v2 only when v3 is absent."""
    prefix_fd = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        prefix_stat = os.fstat(prefix_fd)
        present = []
        for name in STATE_NAMES:
            try:
                present.append((name, os.stat(name, dir_fd=prefix_fd, follow_symlinks=False)))
            except FileNotFoundError:
                pass
        if not present:
            raise PrefixStateError("typed prefix state is missing")
        if len(present) != 1:
            raise PrefixStateError("typed prefix state is ambiguous")
        name, named = present[0]
        version = 3 if name.endswith("v3") else 2
        try:
            state_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=prefix_fd)
        except OSError as error:
            raise PrefixStateError("typed prefix state cannot be retained") from error
        try:
            opened = os.fstat(state_fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or (opened.st_uid, opened.st_gid) != (prefix_stat.st_uid, prefix_stat.st_gid)
                    or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                raise PrefixStateError("typed prefix state identity mismatch")
            data = bytearray()
            while chunk := os.read(state_fd, 256):
                data.extend(chunk)
                if len(data) > MAX_STATE_BYTES:
                    raise PrefixStateError("typed prefix state exceeds budget")
        finally:
            os.close(state_fd)
        try:
            lines = bytes(data).decode("ascii").splitlines()
        except UnicodeError as error:
            raise PrefixStateError("typed prefix state is malformed") from error
        if not lines or lines[0] != f"DARLING_PREFIX_STATE_V{version}":
            raise PrefixStateError("typed prefix state schema mismatch")
        fields: dict[str, str] = {}
        for line in lines[1:]:
            if "=" not in line:
                raise PrefixStateError("typed prefix state is malformed")
            key, value = line.split("=", 1)
            if not key or key in fields:
                raise PrefixStateError("typed prefix state is ambiguous")
            fields[key] = value
        common = {
            "schema_version", "runtime_mode", "generation", "prefix_device",
            "prefix_inode", "owner_uid", "owner_gid", "provenance",
        }
        required = common if version == 2 else common | {
            "sidecar_device", "sidecar_inode",
        }
        if set(fields) != required:
            raise PrefixStateError("typed prefix state field set is not exact")
        try:
            schema = int(fields["schema_version"]); generation = int(fields["generation"])
            device = int(fields["prefix_device"]); inode = int(fields["prefix_inode"])
        except ValueError as error:
            raise PrefixStateError("typed prefix state is malformed") from error
        if schema != version or generation <= 0:
            raise PrefixStateError("typed prefix state schema or generation is invalid")
        runtime_mode = fields["runtime_mode"]
        if runtime_mode not in RUNTIME_MODES:
            raise PrefixStateError("typed prefix runtime mode is invalid")
        if (device, inode) != (prefix_stat.st_dev, prefix_stat.st_ino):
            raise PrefixStateError("typed prefix state identity mismatch")
        try:
            owner = (int(fields["owner_uid"]), int(fields["owner_gid"]))
        except ValueError as error:
            raise PrefixStateError("typed prefix state metadata is malformed") from error
        if owner != (prefix_stat.st_uid, prefix_stat.st_gid):
            raise PrefixStateError("typed prefix state owner mismatch")
        expected_provenance = (
            "darling-runtime-prefix-lifecycle-v2"
            if version == 2 else "darling-runtime-prefix-sidecar-v1"
        )
        if fields["provenance"] != expected_provenance:
            raise PrefixStateError("typed prefix state provenance mismatch")
        if version == 3:
            try:
                sidecar_identity = (int(fields["sidecar_device"]), int(fields["sidecar_inode"]))
            except ValueError as error:
                raise PrefixStateError("typed prefix state metadata is malformed") from error
            parent_fd = os.open(
                prefix.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                sidecar_name = f"{prefix.name}.eunion-sidecar-v1"
                named_sidecar = os.stat(sidecar_name, dir_fd=parent_fd, follow_symlinks=False)
                sidecar_fd = os.open(
                    sidecar_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                try:
                    opened_sidecar = os.fstat(sidecar_fd)
                finally:
                    os.close(sidecar_fd)
            except OSError as error:
                raise PrefixStateError("typed prefix sidecar cannot be retained") from error
            finally:
                os.close(parent_fd)
            if (
                sidecar_identity != (opened_sidecar.st_dev, opened_sidecar.st_ino)
                or (named_sidecar.st_dev, named_sidecar.st_ino)
                != (opened_sidecar.st_dev, opened_sidecar.st_ino)
            ):
                raise PrefixStateError("typed prefix sidecar identity mismatch")
        return PrefixState(schema, runtime_mode, generation, device, inode)
    finally:
        os.close(prefix_fd)


def read_legacy_runtime_mode(prefix: Path) -> str:
    """Read the old marker only for a prefix with no typed state."""
    prefix_fd = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for state_name in STATE_NAMES:
            try:
                os.stat(state_name, dir_fd=prefix_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise PrefixStateError("legacy runtime mode is ambiguous with typed state")
        prefix_stat = os.fstat(prefix_fd)
        try:
            named = os.stat(
                LEGACY_RUNTIME_MODE_NAME, dir_fd=prefix_fd, follow_symlinks=False
            )
            marker_fd = os.open(
                LEGACY_RUNTIME_MODE_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=prefix_fd,
            )
        except OSError as error:
            raise PrefixStateError("legacy runtime mode marker cannot be retained") from error
        try:
            opened = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_uid, opened.st_gid)
                != (prefix_stat.st_uid, prefix_stat.st_gid)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise PrefixStateError("legacy runtime mode marker identity mismatch")
            data = os.read(marker_fd, 128)
            if os.read(marker_fd, 1):
                raise PrefixStateError("legacy runtime mode marker exceeds budget")
        finally:
            os.close(marker_fd)
        try:
            text = data.decode("ascii")
        except UnicodeError as error:
            raise PrefixStateError("legacy runtime mode marker is malformed") from error
        prefix_text = "DARLING_RUNTIME_MODE_V1="
        if not text.endswith("\n") or not text.startswith(prefix_text):
            raise PrefixStateError("legacy runtime mode marker is malformed")
        runtime_mode = text[len(prefix_text) : -1]
        if runtime_mode not in RUNTIME_MODES:
            raise PrefixStateError("legacy prefix runtime mode is invalid")
        return runtime_mode
    finally:
        os.close(prefix_fd)

#!/usr/bin/env python3
"""Allowlisted source/build contract for the Phase 3B Mach-O batch."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath


COMMON_COMPILE_FLAGS = (
    "-isysroot",
    "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
    "-std=gnu11",
    "-Wall",
    "-Wextra",
    "-Werror",
)
ULOCK_COMPILE_FLAGS = COMMON_COMPILE_FLAGS + ("-pthread",)
COMMON_LINK_FLAGS: tuple[str, ...] = ()
ANCHOR_FIXTURE = "select_fdset_guest"
ANCHOR_ARTIFACT_SHA256 = (
    "de9e7097a60f7f0aaf31bc6be0bac760bccf9f6d2a412d5b16aa14ec5685eab6"
)
ALLOWED_SOURCE_PROJECTS = frozenset({"darling-workspace", "darling-src-external-xnu"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class GuestMachoBuildSpec:
    name: str
    source_project: str
    source_path: str
    source_sha256: str
    compile_flags: tuple[str, ...]
    link_flags: tuple[str, ...]
    expected_marker: str


def _spec(
    name: str,
    source_project: str,
    source_path: str,
    source_sha256: str,
    marker: str,
    compile_flags: tuple[str, ...] = COMMON_COMPILE_FLAGS,
) -> GuestMachoBuildSpec:
    return GuestMachoBuildSpec(
        name,
        source_project,
        source_path,
        source_sha256,
        compile_flags,
        COMMON_LINK_FLAGS,
        marker,
    )


FIXTURE_SPECS = (
    _spec(
        "abort_with_payload_no_group_broadcast",
        "darling-workspace",
        "tests/abort_with_payload_no_group_broadcast.c",
        "2a793010639a128f92228a32e1d708a5dd95554238547b11eac835f32fca50cd",
        "GREEN: abort_with_payload stayed local to the child; parent survived",
    ),
    _spec(
        "select_fdset_guest",
        "darling-workspace",
        "tests/select_fdset_guest.c",
        "ed64b81c8643e46e1bcb49ee4c93b558476b57a73021beed8d36cdf893b11e5f",
        "SELECT_FDSET_GUEST_OK",
    ),
    _spec(
        "getattrlist_name_objtype_guest",
        "darling-workspace",
        "tests/getattrlist_name_objtype_guest.c",
        "ec5182ffdb57cf9651023216d08bed23df1030525e60737aeeede666de4aad1e",
        "GETATTRLIST_NAME_OBJTYPE_GUEST_OK",
    ),
    _spec(
        "darwin_priority_guest",
        "darling-workspace",
        "tests/darwin_priority_guest.c",
        "f00656f78d6d82489323b95548205e0148922c2c8598a124b87f1a05bca50e66",
        "DARWIN_PRIORITY_GUEST_OK",
    ),
    _spec(
        "socket_siocgifconf_guest",
        "darling-workspace",
        "tests/socket_siocgifconf_guest.c",
        "1522c74a503508f6dddfb7f9b3d0572ef34e7d8cb571b28b3e4bad2c957b5a64",
        "SOCKET_SIOCGIFCONF_GUEST_OK",
    ),
    _spec(
        "bzero_return_register_guest",
        "darling-workspace",
        "tests/bzero_return_register_guest.c",
        "2abb7c2368d54a88edf1636efce579c73f06fc80cf83fab071aa2530580accfd",
        "BZERO_RETURN_REGISTER_GUEST_OK",
    ),
    _spec(
        "sigexc_sa_restart_guest",
        "darling-workspace",
        "tests/sigexc_sa_restart_guest.c",
        "6255cd066dfe494d06205f6a6da6d00716d82589a13214800e5ac89b9637c7e8",
        "SIGEXC_SA_RESTART_GUEST_OK",
    ),
    _spec(
        "sigexc_default_resend_self_guest",
        "darling-workspace",
        "tests/sigexc_default_resend_self_guest.c",
        "25fc4aa2838d56a3f0a153e55a431d6eedcd1e28962f9e91c29f8ae529ff39f0",
        "SIGEXC_DEFAULT_RESEND_SELF_GUEST_OK",
    ),
    _spec(
        "ulock_eintr_retry_guest",
        "darling-workspace",
        "tests/ulock_eintr_retry_guest.c",
        "8c8c24aabb8052e061f4c5022048089403bd8aab5a4f770a763471223e46e63a",
        "ULOCK_EINTR_RETRY_GUEST_OK",
        ULOCK_COMPILE_FLAGS,
    ),
    _spec(
        "vchroot_pathnull_guard_guest",
        "darling-workspace",
        "tests/vchroot_pathnull_guard_guest.c",
        "439d82ad19feff8d975adbbc6bebf0ff96652dcd843db06349d14721e186e5c1",
        "VCHROOT_PATHNULL_GUARD_GUEST_OK",
    ),
    _spec(
        "chown_disabled_null_guard_guest",
        "darling-workspace",
        "tests/chown_disabled_null_guard_guest.c",
        "407fe1640a395e4918417960c3f0d0b2f164374745ad1599742d533324c0a418",
        "CHOWN_DISABLED_NULL_GUARD_GUEST_OK",
    ),
    _spec(
        "fd_guard_ebadf_guest",
        "darling-workspace",
        "tests/fd_guard_ebadf_guest.c",
        "042dab4cb0b1dfb7320ba66096b14c90669eb05d4046dd86c9e200fbd40fb02f",
        "FD_GUARD_EBADF_GUEST_OK",
    ),
    _spec(
        "fork_checkin_signal_storm_guest",
        "darling-workspace",
        "tests/fork_checkin_signal_storm_guest.c",
        "e97046e766633260192d870a3864dac12d26c1f4c39cbaf92550749f1c4df504",
        "FORK_CHECKIN_SIGNAL_STORM_OK",
    ),
    _spec(
        "rootless_no_mount_guest",
        "darling-workspace",
        "tests/rootless_no_mount_guest.c",
        "9017235080c86914b475f94586581dce707218f8fc70fab5b7cfd115e8e19d2c",
        "ROOTLESS_NO_MOUNT_GUEST_OK",
    ),
)



def validate_specs() -> None:
    expected_names = [
        "abort_with_payload_no_group_broadcast",
        "select_fdset_guest",
        "getattrlist_name_objtype_guest",
        "darwin_priority_guest",
        "socket_siocgifconf_guest",
        "bzero_return_register_guest",
        "sigexc_sa_restart_guest",
        "sigexc_default_resend_self_guest",
        "ulock_eintr_retry_guest",
        "vchroot_pathnull_guard_guest",
        "chown_disabled_null_guard_guest",
        "fd_guard_ebadf_guest",
        "fork_checkin_signal_storm_guest",
        "rootless_no_mount_guest",
    ]
    names = [spec.name for spec in FIXTURE_SPECS]
    if names != expected_names or len(set(names)) != 14:
        raise ValueError("Phase 3B requires the reviewed 14 fixture order")
    for spec in FIXTURE_SPECS:
        source_parts = PurePosixPath(spec.source_path).parts
        if spec.source_project not in ALLOWED_SOURCE_PROJECTS:
            raise ValueError(f"unallowlisted source project for {spec.name}")
        if (
            not spec.source_path
            or PurePosixPath(spec.source_path).is_absolute()
            or ".." in source_parts
        ):
            raise ValueError(f"unsafe project-relative path for {spec.name}")
        if not _SHA256_RE.fullmatch(spec.source_sha256):
            raise ValueError(f"invalid source SHA-256 for {spec.name}")
        if not spec.compile_flags or any("\t" in flag for flag in spec.compile_flags):
            raise ValueError(f"missing compile flags for {spec.name}")
        if any("\t" in flag for flag in spec.link_flags):
            raise ValueError(f"invalid link flags for {spec.name}")
        if not spec.expected_marker.strip() or "\t" in spec.expected_marker:
            raise ValueError(f"invalid expected marker for {spec.name}")
    anchor = next(spec for spec in FIXTURE_SPECS if spec.name == ANCHOR_FIXTURE)
    if anchor.compile_flags != COMMON_COMPILE_FLAGS or anchor.link_flags:
        raise ValueError("select_fdset_guest no longer uses the reviewed pilot flags")
    ulock = next(spec for spec in FIXTURE_SPECS if spec.name == "ulock_eintr_retry_guest")
    if ulock.compile_flags != ULOCK_COMPILE_FLAGS:
        raise ValueError("ulock_eintr_retry_guest must use -pthread")


def emit_tsv() -> None:
    print(
        "name\tsource_project\tsource_path\tsource_sha256\tcompile_flags\t"
        "link_flags\texpected_marker"
    )
    for spec in FIXTURE_SPECS:
        print(
            "\t".join(
                (
                    spec.name,
                    spec.source_project,
                    spec.source_path,
                    spec.source_sha256,
                    json.dumps(spec.compile_flags, separators=(",", ":")),
                    json.dumps(spec.link_flags, separators=(",", ":")),
                    spec.expected_marker,
                )
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit-tsv", action="store_true")
    args = parser.parse_args()
    try:
        validate_specs()
    except ValueError as error:
        print(f"invalid guest Mach-O batch specs: {error}", file=sys.stderr)
        return 1
    if args.emit_tsv:
        emit_tsv()
    else:
        print(json.dumps([asdict(spec) for spec in FIXTURE_SPECS], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

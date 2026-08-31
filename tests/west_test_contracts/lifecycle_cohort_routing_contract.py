#!/usr/bin/env python3
"""Fail-closed source/registry binding for the first .7 writer cohort."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


COHORT = {
    "rust-controller.transport-socket",
    "darling.startup.init-pid",
    "darling.startup.shellspawn-preflight",
    "darling.shellspawn.socket",
    "darlingserver.control-socket",
    "darlingserver.runtime-main-log",
    "darlingserver.preinit-var-run-generation",
    "darlingserver.user-home",
    "launchd.system-ipc-socket",
    "launchd.per-user-ipc-socket",
}

REQUIRED_ROUTING = {
    "src/startup/darling.c": (
        "lifecycleCohortEnabled",
        "putInitPid(pidInit)",
    ),
    "src/shellspawn/shellspawn.c": (
        "darling_lifecycle_publish_endpoint(DARLING_LIFECYCLE_ENDPOINT_SHELLSPAWN)",
        "darling_lifecycle_retire_endpoint(DARLING_LIFECYCLE_ENDPOINT_SHELLSPAWN)",
    ),
    "src/launchd/src/ipc.c": (
        "darling_lifecycle_publish_and_activate_endpoint(",
        "DARLING_LIFECYCLE_ENDPOINT_PER_USER_LAUNCHD",
        "darling_lifecycle_publish_and_activate_dynamic_endpoint(",
        "darling_lifecycle_retire_endpoint(ipc_lifecycle_kind)",
        "lifecycle_reserved_environment_key",
    ),
    "src/launchd/src/core.c": (
        'strcmp(j->label, "org.darlinghq.shellspawn")',
        'strncmp(j->label, "com.apple.launchd.peruser."',
        'setenv("DARLING_LAUNCHD_PER_USER_CONTEXT", "1", 1)',
        'unsetenv("DARLING_LAUNCHD_PER_USER_CONTEXT")',
        "lifecycle_reserved_environment_key",
    ),
    "src/launchd/src/runtime.c": (
        'getenv("DARLING_LAUNCHD_PER_USER_CONTEXT")',
        "pid1_magic = false",
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--darling-root", type=Path, required=True)
    parser.add_argument("--darlingserver-root", type=Path, required=True)
    args = parser.parse_args()

    workspace = args.workspace_root.resolve()
    darling = args.darling_root.resolve()
    darlingserver = args.darlingserver_root.resolve()
    inventory = json.loads(
        (workspace / "lifecycle/namespace-writer-inventory-v1.json").read_text()
    )
    writers = {writer["id"]: writer for writer in inventory["writers"]}
    if not COHORT <= writers.keys():
        raise SystemExit(f"missing cohort records: {sorted(COHORT - writers.keys())}")
    cohort_ready = {
        writer_id
        for writer_id, writer in writers.items()
        if writer["compatibility"] == "cohort-ready"
    }
    if cohort_ready != COHORT:
        raise SystemExit(
            f"cohort boundary drift: expected={sorted(COHORT)} actual={sorted(cohort_ready)}"
        )
    for writer_id in COHORT:
        lock = writers[writer_id]["lock"]
        if lock != {
            "required": True,
            "path": ".lifecycle.lock",
            "acquisition": "rust-cohort-controller-retained-session-lease",
            "retained_fd": True,
            "status": "exact-exclusive-flock",
        }:
            raise SystemExit(f"{writer_id}: wrong exact lease evidence")
    incompatible = {
        writer_id
        for writer_id, writer in writers.items()
        if writer["compatibility"] == "incompatible"
    }
    if len(incompatible) != len(writers) - len(COHORT):
        raise SystemExit("non-cohort writers must remain incompatible")
    if any(writer["compatibility"] == "compatible" for writer in writers.values()):
        raise SystemExit("global production routing must remain disabled")

    for relative, markers in REQUIRED_ROUTING.items():
        source = (darling / relative).read_text(encoding="utf-8")
        for marker in markers:
            if marker not in source:
                raise SystemExit(f"{relative}: missing routed marker {marker}")
    dserver = darlingserver / "src/server.cpp"
    dserver_source = dserver.read_text(encoding="utf-8")
    for marker in ("lifecycleListenerSocket", "_lifecycleRoutedSocket"):
        if marker not in dserver_source:
            raise SystemExit(f"Darlingserver route missing {marker}")
    dserver_main = (darlingserver / "src/darlingserver.cpp").read_text(encoding="utf-8")
    for marker in (
        "collectLifecycleUserHomePlan",
        "darling_lifecycle_cohort_prepare_user_home",
        "setupUserHomeLegacy(prefixFD, originalUID)",
    ):
        if marker not in dserver_main:
            raise SystemExit(f"Darlingserver user-home route missing {marker}")

    rust = (workspace / "lifecycle/operation-boundary/src/cohort_routing.rs").read_text()
    for marker in (
        "SessionAuthority",
        "acquire_lock",
        "revalidate_lock",
        "SCM_RIGHTS",
        "EndpointExists",
        "SO_PEERCRED",
        "SO_PEERPIDFD",
        "peer_pidfd",
        "endpoint_owners",
        "OwnerDeathTransition",
        "MAX_REJECTED_REQUESTS_PER_SLICE",
        "DYNAMIC_DIRECTORY_ATTEMPTS",
        "EndpointKey::PerUser",
        "prepare_user_home",
    ):
        if marker not in rust:
            raise SystemExit(f"Rust cohort authority missing {marker}")
    print(
        "LIFECYCLE_COHORT_SOURCE_VALID "
        f"cohort_ready={len(COHORT)} incompatible={len(incompatible)} authority=rust"
    )


if __name__ == "__main__":
    main()

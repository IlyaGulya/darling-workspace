#!/usr/bin/env python3
"""Focused contract for the bounded Darlingserver main-log cohort."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def text(path: pathlib.Path) -> str:
    require(path.is_file(), f"missing source: {path}")
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=pathlib.Path, required=True)
    parser.add_argument("--darlingserver", type=pathlib.Path, required=True)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    dserver = args.darlingserver.resolve()
    inventory = json.loads(
        (workspace / "lifecycle/namespace-writer-inventory-v1.json").read_text()
    )
    writers = {writer["id"]: writer for writer in inventory["writers"]}
    main_log = writers["darlingserver.runtime-main-log"]
    aux_log = writers["darlingserver.runtime-aux-log"]
    require(main_log["targets"] == ["/private/var/log/dserver.log"], "main target drift")
    require(main_log["compatibility"] == "cohort-ready", "main route is not cohort-ready")
    require(main_log["lock"]["status"] == "exact-exclusive-flock", "main lease drift")
    require(aux_log["targets"] == ["/private/var/log/dserver-auxlog.txt"], "aux target drift")
    require(aux_log["compatibility"] == "incompatible", "Perf aux route was promoted")

    header = text(workspace / "lifecycle/operation-boundary/include/darling_lifecycle_cohort.h")
    rust = text(workspace / "lifecycle/operation-boundary/src/cohort_routing.rs")
    server_h = text(dserver / "internal-include/darlingserver/server.hpp")
    server_cpp = text(dserver / "src/server.cpp")
    logging_cpp = text(dserver / "src/logging.cpp")
    main_cpp = text(dserver / "src/darlingserver.cpp")

    require("int dserver_log_fd;" in header, "bootstrap lacks dserver_log_fd")
    require("dserver_aux_log_fd" not in header, "aux writer leaked into the main-log ABI")
    require("prefixFD, prefix, getpid(), &lifecycleBootstrap" in main_cpp,
            "controller acquisition is not bound to retained prefix FD")
    require("lifecycleBootstrap.dserver_log_fd" in main_cpp,
            "main log FD is not transferred into Server")
    require("dserver_aux_log_fd" not in main_cpp,
            "incompatible aux writer leaked into Homebrew routing")
    require("lifecycleLogFD()" in server_h and "lifecycleLogFD() const" in server_cpp,
            "Server lacks typed main-log ownership")
    require("FD _lifecycleLogFD;" in server_h and
            server_h.index("FD _lifecycleLogFD;") < server_h.index("int _listenerSocket;"),
            "RAII log owner is not the last-destroyed Server member")
    require("close(_lifecycleLogFD)" not in server_cpp,
            "Server manually closes its RAII lifecycle log owner")
    require("std::move(lifecycleLogOwner)" in main_cpp,
            "caller does not transfer unique lifecycle log ownership")
    require("Server::sharedInstance().lifecycleLogFD()" in logging_cpp,
            "production logger does not prefer the controller FD")
    require("openLogDirectoryAt(Server::sharedInstance().prefixFD())" in logging_cpp,
            "legacy OFF fallback is no longer retained-prefix-relative")
    require("fn publish_log(&mut self)" in rust,
            "Rust is not the log publication authority")
    require("validate_owned(expected, libc::S_IFREG, Some(0o644))" in rust,
            "existing log is not validated before writer open")
    require("libc::O_APPEND | libc::O_NOFOLLOW | libc::O_CLOEXEC" in rust,
            "existing append-only/no-follow writer flags drifted")
    require("| libc::O_EXCL" in rust and "fchmod(new Darlingserver log)" in rust,
            "new log lacks exclusive inode-bound mode normalization")
    require("named_identity(log.parent.as_raw_fd(), &log.name)? != Some(log.identity)" in rust,
            "finish lacks exact named identity validation")

    tests = subprocess.run(
        [
            "cargo", "test", "--quiet", "--manifest-path",
            str(workspace / "lifecycle/operation-boundary/Cargo.toml"),
            "dserver_log_",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    sys.stdout.write(tests.stdout)
    require(tests.returncode == 0, "Rust dserver-log behavioral tests failed")
    print("LIFECYCLE_DSERVER_LOG_ROUTING_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

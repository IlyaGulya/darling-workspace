#!/usr/bin/env python3
"""Focused retained-runtime artifact-root contract for the .5.2 lane."""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "guest_ready", ROOT / "tests/west_test_contracts/lifecycle_guest_ready_contract.py"
)
assert SPEC is not None and SPEC.loader is not None
guest_ready = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guest_ready)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="guest-ready-artifacts-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        prefix = root / "prefix"
        workspace.mkdir()
        git(workspace, "init", "-q")
        git(workspace, "config", "user.name", "contract")
        git(workspace, "config", "user.email", "contract@example.invalid")
        (workspace / "west.lock.yml").write_text("manifest: {}\n")
        profile = workspace / "patches/homebrew/patches.yml"
        profile.parent.mkdir(parents=True)
        profile.write_text("patches: []\n")
        git(workspace, "add", ".")
        git(workspace, "commit", "-qm", "fixture")
        prefix.mkdir()
        identity = prefix.stat()
        sidecar = prefix.with_name(f"{prefix.name}.eunion-sidecar-v1"); sidecar.mkdir()
        sidecar_identity = sidecar.stat()
        state = prefix / ".darling-prefix-state-v3"
        state.write_text(
            "DARLING_PREFIX_STATE_V3\n"
            "schema_version=3\n"
            "runtime_mode=rootless-eunion\n"
            "generation=1\n"
            f"prefix_device={identity.st_dev}\n"
            f"prefix_inode={identity.st_ino}\n"
            f"sidecar_device={sidecar_identity.st_dev}\n"
            f"sidecar_inode={sidecar_identity.st_ino}\n"
            f"owner_uid={identity.st_uid}\nowner_gid={identity.st_gid}\n"
            "provenance=darling-runtime-prefix-sidecar-v1\n"
        )
        state.chmod(0o600)
        for relative in guest_ready.HOST_ARTIFACTS.values():
            path = prefix / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"host\n")
        for relative in guest_ready.GUEST_BOOTCHAIN_ARTIFACTS.values():
            path = prefix / "libexec/darling" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"guest\n")
        verifier = {"closure_digest": "0" * 64}
        value, _source, _runtime = guest_ready._source_identity(workspace, prefix, verifier)
        assert value["runtime"]["artifacts"]["launchd"]["relative"] == (
            "libexec/darling/sbin/launchd"
        )

        nested = prefix / "libexec/darling/sbin/launchd"
        nested.unlink()
        flat = prefix / "sbin/launchd"
        flat.parent.mkdir(parents=True, exist_ok=True)
        flat.write_bytes(b"flat decoy\n")
        try:
            guest_ready._source_identity(workspace, prefix, verifier)
        except guest_ready.ContractError as error:
            assert "libexec/darling/sbin/launchd" in str(error)
        else:
            raise AssertionError("flat launchd decoy satisfied retained runtime identity")

        failing_launcher = root / "failing-launcher"
        failing_launcher.write_text(
            "#!/bin/sh\nprintf 'holder stdout checkpoint\\n'\n"
            "printf 'holder stderr checkpoint\\n' >&2\nexit 23\n"
        )
        failing_launcher.chmod(0o755)
        evidence = root / "holder-evidence"
        evidence.mkdir()
        try:
            guest_ready._start_holder(
                failing_launcher, prefix, evidence, "holder-negative", "exit 0"
            )
        except guest_ready.ContractError as error:
            message = str(error)
            assert "rc=23" in message
            assert "holder stdout checkpoint" in message
            assert "holder stderr checkpoint" in message
        else:
            raise AssertionError("early holder exit lost its bounded diagnostics")

        source = (ROOT / "tests/west_test_contracts/lifecycle_guest_ready_contract.py").read_text()
        assert ".guest52-late-trigger" not in source
        assert ".guest52-late-pid" not in source
        assert "GUEST52_LATE_PID=" in source and "interactive=True" in source

        child = subprocess.Popen(["/bin/sh", "-c", "exec sleep 30"])
        identity = guest_ready._process_identity(child.pid)
        assert identity is not None
        retained = guest_ready._retain_pidfd(identity)
        signal.pidfd_send_signal(retained, signal.SIGKILL)
        child.wait(timeout=5)
        os.close(retained)
        try:
            guest_ready._retain_pidfd(identity)
        except guest_ready.ContractError as error:
            assert "exited" in str(error) or "identity changed" in str(error)
        else:
            raise AssertionError("terminal PID identity was accepted after reuse boundary")
    print("lifecycle guest-ready artifact-root contract: PASS")


if __name__ == "__main__":
    main()

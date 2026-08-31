"""Run the production C++ → C ABI → Rust lower-binding path."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.deploy_transaction import DeploymentTransaction  # noqa: E402


parser = argparse.ArgumentParser()
parser.add_argument("--fixture", type=Path, required=True)
parser.add_argument("--task-root", type=Path, required=True)
args = parser.parse_args()

prefix = args.task_root / "prefix"
prefix.mkdir(parents=True)
metadata = prefix.stat()
state = (
    "DARLING_PREFIX_STATE_V2\n"
    "schema_version=2\n"
    "runtime_mode=rootless-eunion\n"
    "generation=23\n"
    f"prefix_device={metadata.st_dev}\n"
    f"prefix_inode={metadata.st_ino}\n"
    f"owner_uid={os.geteuid()}\n"
    f"owner_gid={os.getegid()}\n"
    "provenance=darling-runtime-prefix-lifecycle-v2\n"
)
(prefix / ".darling-prefix-state-v2").write_text(state, encoding="utf-8")
(prefix / ".darling-prefix-state-v2").chmod(0o600)
(prefix / ".lifecycle.lock").write_bytes(b"")
(prefix / ".lifecycle.lock").chmod(0o600)
(prefix / "libexec/darling").mkdir(parents=True)
(prefix / "libexec/darling").chmod(0o755)
(prefix / "bin").mkdir()
os.link(args.fixture, prefix / "bin/darlingserver")
transaction = DeploymentTransaction(args.task_root / "deployment.json", prefix)
transaction.bind_runtime_lower_root(prefix_generation=23)
result = subprocess.run(
    [str(args.fixture), str(prefix)],
    check=False,
    capture_output=True,
    text=True,
    timeout=20,
)
if result.returncode != 0 or "RUNTIME_LOWER_BINDING_ABI_VALID" not in result.stdout:
    raise SystemExit(
        f"ABI fixture failed rc={result.returncode}: {result.stdout}\n{result.stderr}"
    )
home = prefix / "Users/abi-user"
linux_home = home / "LinuxHome"
if (home.stat().st_mode & 0o777) != 0o755:
    raise SystemExit("Rust ABI home directory mode mismatch")
if os.readlink(linux_home) != "/Volumes/SystemRoot/home/abi-user":
    raise SystemExit("Rust ABI LinuxHome target mismatch")
before = (linux_home.lstat().st_dev, linux_home.lstat().st_ino)
if (linux_home.lstat().st_uid, linux_home.lstat().st_gid) != (
    os.geteuid(),
    os.getegid(),
):
    raise SystemExit("Rust ABI LinuxHome owner mismatch")
# The fixture invokes the phase once; a second process/restart must accept the
# same persistent exact-valid layout without changing its identity.
result = subprocess.run(
    [str(args.fixture), str(prefix)],
    check=False,
    capture_output=True,
    text=True,
    timeout=20,
)
if result.returncode != 0:
    raise SystemExit(f"ABI restart failed rc={result.returncode}: {result.stderr}")
after = (linux_home.lstat().st_dev, linux_home.lstat().st_ino)
if after != before:
    raise SystemExit("persistent exact-valid LinuxHome was replaced")
print("RUNTIME_LOWER_BINDING_ABI_INTEGRATION_VALID")

"""Build and select the lane provider from the exact West-owned source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml

WORKSPACE = Path(__file__).resolve().parents[1]
BUILD_COMMAND = "mise run lane-provider-build"


class ProviderError(RuntimeError):
    pass


def specification(workspace: Path = WORKSPACE) -> tuple[Path, str, Path]:
    manifest = yaml.safe_load((workspace / "west.yml").read_text())["manifest"]
    project = next(p for p in manifest["projects"] if p["name"] == "fuse-overlayfs")
    revision = project["revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ProviderError("fuse-overlayfs West revision must be an exact commit")
    source = workspace.parent / project["path"]
    install = workspace.parent / ".west-state" / "workspace-tools" / "fuse-overlayfs" / revision
    return source, revision, install


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def resolve_provider(workspace: Path = WORKSPACE) -> Path:
    source, revision, install = specification(workspace)
    binary = install / "bin" / "fuse-overlayfs"
    try:
        receipt = json.loads((install / "provenance.json").read_text())
        if (receipt["revision"] != revision or receipt["source"] != str(source)
                or receipt["sha256"] != digest(binary) or not os.access(binary, os.X_OK)):
            raise ValueError("binary provenance does not match the West pin")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProviderError(
            f"Owned lane provider is absent or stale at {install}: {error}. "
            f"Provide the reviewed West source at {source} revision {revision}, then run "
            f"`{BUILD_COMMAND}`. No PATH/system provider fallback is supported."
        ) from error
    return binary


def build_provider(workspace: Path = WORKSPACE) -> Path:
    source, revision, install = specification(workspace)
    if install.exists():
        # Never replace an executable that may serve an existing mounted lane.
        return resolve_provider(workspace)
    try:
        actual = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProviderError(f"Provide the reviewed West source at {source} revision {revision}") from error
    if actual != revision:
        raise ProviderError(f"{source} is at {actual}; West requires {revision}. Commit and pin accepted fixes before building.")
    install.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=install.parent) as temporary:
        staging = Path(temporary)
        snapshot = staging / "source"
        snapshot.mkdir()
        # Archive the exact commit, never mutable working bytes or a Cargo Git cache.
        with tempfile.TemporaryFile() as archive:
            subprocess.run(["git", "-C", str(source), "archive", revision], stdout=archive, check=True)
            archive.seek(0)
            subprocess.run(["tar", "-xf", "-", "-C", str(snapshot)], stdin=archive, check=True)
        target = staging / "target"
        subprocess.run([
            "cargo", "build", "--locked", "--release", "--bin", "fuse-overlayfs",
            "--manifest-path", str(snapshot / "Cargo.toml"), "--target-dir", str(target),
        ], cwd=snapshot, check=True)
        publish = staging / "install"
        (publish / "bin").mkdir(parents=True)
        binary = publish / "bin" / "fuse-overlayfs"
        shutil.copy2(target / "release" / "fuse-overlayfs", binary)
        receipt = {
            "revision": revision, "source": str(source), "sha256": digest(binary),
            "cargo_lock_sha256": digest(snapshot / "Cargo.lock"),
            "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip(),
            "cargo": subprocess.check_output(["cargo", "--version"], text=True).strip(),
            "build": "cargo build --locked --release --bin fuse-overlayfs",
        }
        (publish / "provenance.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        os.rename(publish, install)
    return resolve_provider(workspace)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "resolve"))
    args = parser.parse_args()
    try:
        print(build_provider() if args.action == "build" else resolve_provider())
    except (ProviderError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()

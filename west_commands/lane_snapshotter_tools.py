"""Explicitly install and resolve the pinned upstream lane snapshotter tools."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request

from lane_provider import WORKSPACE, ProviderError, digest, resolve_provider, workspace_topdir

RELEASE = "v2.1.7"
REVISION = "da57796c7d0a2b608abf173651cf148706e33337"
ARCHIVE_URL = (
    "https://github.com/containerd/fuse-overlayfs-snapshotter/releases/download/"
    "v2.1.7/containerd-fuse-overlayfs-2.1.7-linux-amd64.tar.gz"
)
ARCHIVE_SHA256 = "d54148043c22381af89cec2a167431e40668716404a1eb682ca69dfb890376f3"
BINARY_NAME = "containerd-fuse-overlayfs-grpc"
BINARY_SHA256 = "e048f9cc616299544fcdcd647cfecba10f60169537721c57e3628ac6c8a0c5c7"
CLIENT = Path("/usr/bin/ctr")
CLIENT_VERSION = "ctr containerd.io v2.2.2"
INSTALL_COMMAND = "mise run lane-snapshotter-install"


class SnapshotterToolsError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotterTools:
    server: Path
    client: Path
    provider: Path
    provenance: dict


def _host() -> None:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise SnapshotterToolsError(
            "The pinned lane snapshotter requires a Linux amd64 host. "
            "Other platforms need a separately reviewed upstream release and hash pin."
        )


def _installation(workspace: Path) -> Path:
    try:
        topdir = workspace_topdir(workspace)
    except ProviderError as error:
        raise SnapshotterToolsError(str(error)) from error
    return (topdir / ".west-state" / "workspace-tools"
            / "containerd-fuse-overlayfs" / REVISION)


def _server_provenance() -> dict:
    return {
        "release": RELEASE,
        "revision": REVISION,
        "url": ARCHIVE_URL,
        "archive_sha256": ARCHIVE_SHA256,
        "member": BINARY_NAME,
        "sha256": BINARY_SHA256,
    }


def _client_provenance() -> dict:
    if not CLIENT.is_file() or not os.access(CLIENT, os.X_OK):
        raise SnapshotterToolsError(
            f"Install the host containerd ctr client v2.2.2 at {CLIENT}; "
            "lane setup never installs or replaces host containerd tools."
        )
    before = digest(CLIENT)
    version = subprocess.check_output(
        [str(CLIENT), "--version"], text=True, timeout=30
    ).strip()
    if version != CLIENT_VERSION:
        raise SnapshotterToolsError(
            f"{CLIENT} reports {version!r}; the reviewed client is {CLIENT_VERSION!r}. "
            "Provide the reviewed host client before explicit lane tool setup."
        )
    if digest(CLIENT) != before:
        raise SnapshotterToolsError(f"{CLIENT} changed while identifying it; stop host tool updates first")
    return {"path": str(CLIENT), "version": version, "sha256": before}


def resolve_tools(workspace: Path = WORKSPACE) -> SnapshotterTools:
    """Verify an existing installation; never download, build or repair tools."""
    _host()
    install = _installation(workspace)
    server = install / "bin" / BINARY_NAME
    try:
        if install.is_symlink() or server.is_symlink() or not server.is_file():
            raise ValueError("snapshotter must be an installed regular executable, not a symlink")
        receipt_path = install / "provenance.json"
        if receipt_path.is_symlink():
            raise ValueError("snapshotter receipt must not be a symlink")
        receipt = json.loads(receipt_path.read_text())
        if (receipt["schema"] != 1 or receipt["snapshotter"] != _server_provenance()
                or digest(server) != BINARY_SHA256 or not os.access(server, os.X_OK)):
            raise ValueError("snapshotter release, revision, archive or executable provenance mismatch")
        client = _client_provenance()
        if receipt["client"] != client:
            raise ValueError("host ctr path, version or executable digest differs from the installation receipt")
        provider = resolve_provider(workspace)
        provenance = {**receipt, "provider": {"path": str(provider), "sha256": digest(provider)}}
    except (OSError, ValueError, KeyError, TypeError, ProviderError,
            subprocess.SubprocessError, SnapshotterToolsError) as error:
        raise SnapshotterToolsError(
            f"Lane snapshotter tools are absent or stale at {install}: {error}. "
            f"Run `{INSTALL_COMMAND}` after satisfying the host prerequisites. "
            "Existing installations are never replaced: investigate changed provenance "
            "with the installation owner; do not overwrite tools serving mounted lanes."
        ) from error
    return SnapshotterTools(server, CLIENT, provider, provenance)


def _extract_binary(archive: Path, destination: Path) -> None:
    if digest(archive) != ARCHIVE_SHA256:
        raise SnapshotterToolsError(f"Snapshotter archive SHA-256 mismatch: {archive}")
    # Never extract archive paths: accept only the one reviewed regular member
    # and stream its bytes into an independently chosen destination.
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if (len(members) != 1 or members[0].name != BINARY_NAME
                or not members[0].isfile() or members[0].size != 12079288):
            raise SnapshotterToolsError(
                "Snapshotter archive must contain only the expected regular binary member"
            )
        with bundle.extractfile(members[0]) as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
    if digest(destination) != BINARY_SHA256:
        raise SnapshotterToolsError("Extracted snapshotter executable SHA-256 mismatch")
    destination.chmod(0o555)


def install_tools(workspace: Path = WORKSPACE, *, archive: Path | None = None) -> SnapshotterTools:
    """Explicit setup only; optionally reuse a locally downloaded pinned archive."""
    _host()
    install = _installation(workspace)
    install.parent.mkdir(parents=True, exist_ok=True)
    # Serialize publication, including concurrent explicit setup invocations.
    # The revision directory is never updated, repaired or removed here.
    with (install.parent / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if install.exists() or install.is_symlink():
            return resolve_tools(workspace)
        resolve_provider(workspace)
        client = _client_provenance()
        with tempfile.TemporaryDirectory(prefix="install-", dir=install.parent) as temporary:
            staging = Path(temporary)
            if archive is None:
                selected = staging / "release.tar.gz"
                with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response, selected.open("xb") as target:
                    shutil.copyfileobj(response, target)
            else:
                # Copy into owned staging before hashing and extracting, so a
                # caller's mutable download cannot change between those steps.
                selected = staging / "release.tar.gz"
                shutil.copyfile(archive, selected)
            publish = staging / "install"
            (publish / "bin").mkdir(parents=True)
            _extract_binary(selected, publish / "bin" / BINARY_NAME)
            receipt = {"schema": 1, "snapshotter": _server_provenance(), "client": client}
            receipt_path = publish / "provenance.json"
            receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            receipt_path.chmod(0o444)
            # All readers see either no revision or its complete receipt+binary.
            # Atomic rename never mutates any older revision or active binary.
            os.rename(publish, install)
    return resolve_tools(workspace)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "resolve"))
    parser.add_argument("--archive", type=Path, help="Reuse a local release archive (install only; SHA-256 checked)")
    args = parser.parse_args()
    if args.archive is not None and args.action != "install":
        parser.error("--archive is only supported by explicit install")
    try:
        tools = install_tools(archive=args.archive) if args.action == "install" else resolve_tools()
        print(tools.server)
    except (SnapshotterToolsError, ProviderError, OSError, ValueError,
            tarfile.TarError, subprocess.SubprocessError, urllib.error.URLError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()

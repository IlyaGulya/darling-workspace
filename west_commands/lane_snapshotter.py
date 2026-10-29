"""Host-UID adapter to containerd's existing FUSE snapshot store.

Containerd owns layer IDs, ancestry, commit and referenced-layer removal. The
workspace owns tool provenance, its user services and Darling mount lifetimes.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Iterator

from lane_snapshotter_tools import SnapshotterToolsError, resolve_tools
from runtime_install import prefix_path_is_safe

STORE_DIRNAME = ".snapshotter"
STORE_LABEL = "io.darling.lanes.store"
KIND_LABEL = "io.darling.lanes.kind"
TREE_LABEL = "io.darling.lanes.tree"


class SnapshotStoreError(RuntimeError):
    pass


def write_json(path: Path, value: dict) -> None:
    """Publish records without exposing a partially written JSON document."""
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SnapshotStore:
    def __init__(self, lane_root: Path):
        self.lane_root = Path(lane_root).expanduser()
        if (not prefix_path_is_safe(self.lane_root)
                or any(character in str(self.lane_root) for character in ",:\\\n\r\t")):
            raise SnapshotStoreError(f"unsafe lane storage root: {self.lane_root}")
        self.root = self.lane_root / STORE_DIRNAME
        self.data = self.root / "data"
        token = hashlib.sha256(os.fsencode(self.root)).hexdigest()[:20]
        self.unit = f"darling-lane-store-{token}.service"
        self.description = f"Darling lane snapshot store {self.root}"
        self.socket = Path(f"/run/user/{os.geteuid()}/dw-lane-store-{token}.sock")
        try:
            self.tools = resolve_tools()
        except SnapshotterToolsError as error:
            raise SnapshotStoreError(str(error)) from error
        self.environment = {
            **os.environ,
            "PATH": str(self.tools.provider.parent)
                    + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }

    def _run(self, arguments: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(arguments, env=self.environment, capture_output=True,
                                  text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SnapshotStoreError(f"cannot run {arguments[0]}: {error}") from error

    def _unit(self, unit: str) -> dict[str, str]:
        result = self._run(["systemctl", "--user", "show", "--no-pager",
                            "--property=LoadState,ActiveState,Description,MainPID", unit])
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if "LoadState" not in properties:
            raise SnapshotStoreError(f"cannot inspect owned user service {unit}: {result.stderr.strip()}")
        return properties

    def _require_service(self, unit: str, description: str, properties: dict[str, str]) -> None:
        if properties.get("Description") != description:
            raise SnapshotStoreError(f"{unit} is not owned by {description!r}; refusing to touch it")

    @contextmanager
    def locked(self, *, create: bool = False) -> Iterator[SnapshotStore]:
        if not self.root.exists() and not create:
            raise SnapshotStoreError(f"lane snapshot store is missing: {self.root}")
        if create:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not prefix_path_is_safe(self.root) or self.root.stat().st_uid != os.geteuid():
            raise SnapshotStoreError(f"lane snapshot store is not an owned real directory: {self.root}")
        descriptor = os.open(self.root / "operation.lock",
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise SnapshotStoreError("lane store operation lease is not an owned regular file")
            try:
                acquired = subprocess.run(
                    ["flock", "--exclusive", "--timeout", "30", str(descriptor)],
                    pass_fds=(descriptor,), env=self.environment,
                    capture_output=True, text=True, timeout=31,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise SnapshotStoreError(f"cannot acquire bounded store lease: {error}") from error
            if acquired.returncode:
                raise SnapshotStoreError(f"timed out acquiring lane storage lease: {self.root}")
            try:
                self._start(create=create)
                self._finish_commit()
                yield self
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _start(self, *, create: bool) -> None:
        marker = self.root / "store.json"
        expected = {"schema": 1, "root": str(self.root), "uid": os.geteuid(),
                    "tools": self.tools.provenance}
        if marker.exists():
            try:
                actual = json.loads(marker.read_text())
            except (OSError, ValueError) as error:
                raise SnapshotStoreError(f"cannot read store ownership: {error}") from error
            if actual != expected:
                raise SnapshotStoreError(f"store ownership or tool provenance changed: {marker}")
        elif create and not self.data.exists():
            write_json(marker, expected)
        else:
            raise SnapshotStoreError(f"refusing to adopt unrecorded snapshot store: {self.root}")
        runtime = self.socket.parent
        if (not prefix_path_is_safe(runtime) or not runtime.is_dir()
                or runtime.stat().st_uid != os.geteuid()):
            raise SnapshotStoreError(f"owned systemd user runtime directory is unavailable: {runtime}")
        properties = self._unit(self.unit)
        if properties.get("LoadState") != "not-found":
            self._require_service(self.unit, self.description, properties)
            if properties.get("ActiveState") != "active":
                raise SnapshotStoreError(f"owned storage service is {properties.get('ActiveState')}: {self.unit}")
            pid = int(properties.get("MainPID", "0"))
            try:
                process = Path(f"/proc/{pid}")
                arguments = [part.decode() for part in (process / "cmdline").read_bytes().split(b"\0") if part]
                valid = (process.stat().st_uid == os.geteuid()
                         and (process / "exe").samefile(self.tools.server)
                         and arguments == [str(self.tools.server), str(self.socket), str(self.data)])
            except (OSError, ValueError):
                valid = False
            if not valid:
                raise SnapshotStoreError(f"storage service process identity is uninspectable or changed: {self.unit}")
            return
        if self.socket.exists():
            raise SnapshotStoreError(f"refusing an orphaned storage socket without its owner: {self.socket}")
        started = self._run([
            "systemd-run", "--user", "--collect", "--unit=" + self.unit,
            "--service-type=notify", "--property=TimeoutStartSec=30s",
            "--property=StandardOutput=journal", "--property=StandardError=journal",
            "--description=" + self.description, "--setenv=PATH=" + self.environment["PATH"],
            str(self.tools.server), str(self.socket), str(self.data),
        ], timeout=40)
        if started.returncode:
            raise SnapshotStoreError(f"cannot start {self.unit}: {started.stderr.strip()}; inspect journalctl --user -u {self.unit}")

    def command(self, *arguments: str, missing_ok: bool = False) -> subprocess.CompletedProcess:
        result = self._run([str(self.tools.client), "--address", str(self.socket),
                            "--timeout", "30s", "snapshots", *arguments], timeout=40)
        if result.returncode:
            if missing_ok and ": not found" in result.stderr:
                return result
            raise SnapshotStoreError(f"snapshotter {' '.join(arguments)} failed: {result.stderr.strip()}")
        if result.stderr:
            # Standalone upstream serves snapshots, not containerd introspection.
            # Preserve that client's warning rather than silently hiding it.
            print(result.stderr.rstrip(), file=sys.stderr)
        return result

    def info(self, key: str) -> dict | None:
        result = self.command("info", key, missing_ok=True)
        return None if result.returncode else json.loads(result.stdout)

    def snapshots(self) -> list[dict[str, str]]:
        result = self.command("list", missing_ok=True)
        if result.returncode:
            if "bucket does not exist" in result.stderr:
                return []  # The upstream metastore has not had its first Prepare.
            raise SnapshotStoreError(result.stderr.strip())
        entries = []
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) == 2:
                key, kind = fields
                parent = ""
            elif len(fields) == 3:
                key, parent, kind = fields
            else:
                raise SnapshotStoreError(f"unrecognized pinned ctr snapshot row: {line!r}")
            entries.append({"Name": key, "Parent": parent, "Kind": kind})
        return entries

    def children(self, key: str) -> list[str]:
        return [entry["Name"] for entry in self.snapshots() if entry["Parent"] == key]

    def prepare(self, key: str, parent: str | None = None) -> dict:
        arguments = ["prepare", "--mounts", key]
        if parent is not None:
            arguments.append(parent)
        mounts = json.loads(self.command(*arguments).stdout)
        if len(mounts) != 1:
            raise SnapshotStoreError(f"snapshotter returned unsupported mount recipe: {mounts!r}")
        recipe = mounts[0]
        if parent is None:
            if recipe.get("Type") != "bind":
                raise SnapshotStoreError("initial snapshot must expose a physical bind source")
            self.layer_path(recipe["Source"])
        else:
            if recipe.get("Type") != "fuse3.fuse-overlayfs" or recipe.get("Source") != "overlay":
                raise SnapshotStoreError(f"snapshotter did not return its FUSE mount recipe: {recipe!r}")
            self.recipe_paths(recipe)
        self.label(key, {STORE_LABEL: str(self.root)})
        return recipe

    def layer_path(self, value: str) -> Path:
        path = Path(value)
        relative = path.relative_to(self.data / "snapshots")
        if (len(relative.parts) != 2 or not relative.parts[0].isdigit()
                or relative.parts[1] not in ("fs", "work") or not prefix_path_is_safe(path)):
            raise SnapshotStoreError(f"snapshotter returned an unsafe layer path: {path}")
        return path

    def recipe_paths(self, recipe: dict) -> dict[str, list[Path]]:
        options = dict(option.split("=", 1) for option in recipe["Options"])
        if set(options) != {"upperdir", "workdir", "lowerdir"}:
            raise SnapshotStoreError(f"unsupported snapshotter options: {options!r}")
        return {key: [self.layer_path(path) for path in value.split(":")]
                for key, value in options.items()}

    def label(self, key: str, labels: dict[str, str]) -> None:
        self.command("label", key, *(name + "=" + value for name, value in labels.items()))

    def commit(self, name: str, active: str) -> None:
        info = self.info(active)
        if (info is None or info.get("Kind") != "Active"
                or info.get("Labels", {}).get(STORE_LABEL) != str(self.root)):
            raise SnapshotStoreError(f"cannot commit an absent, non-active or foreign snapshot: {active}")
        if self.info(name) is not None:
            raise SnapshotStoreError(f"commit destination already exists: {name}")
        # ctr Commit replaces labels with its gc.root label. Preserve our
        # ownership/drift metadata separately, with a durable intent across the
        # two upstream API calls; graph transitions still belong to containerd.
        write_json(self.root / "commit.json", {
            "name": name, "active": active, "parent": info.get("Parent", ""),
            "labels": info["Labels"],
        })
        self._finish_commit()

    def _finish_commit(self) -> None:
        intent = self.root / "commit.json"
        if not intent.exists():
            return
        pending = json.loads(intent.read_text())
        labels = pending["labels"]
        if labels.get(STORE_LABEL) != str(self.root):
            raise SnapshotStoreError("pending commit does not belong to this store")
        committed = self.info(pending["name"])
        if committed is None:
            active = self.info(pending["active"])
            if (active is None or active.get("Kind") != "Active"
                    or active.get("Parent", "") != pending["parent"]
                    or active.get("Labels") != labels):
                raise SnapshotStoreError("pending commit's active snapshot identity changed")
            self.command("commit", pending["name"], pending["active"])
            committed = self.info(pending["name"])
        if (committed is None or committed.get("Kind") != "Committed"
                or committed.get("Parent", "") != pending["parent"]):
            raise SnapshotStoreError("pending commit's destination identity changed")
        self.label(pending["name"], labels)
        intent.unlink()
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def remove(self, key: str) -> None:
        info = self.info(key)
        if info is None or info.get("Labels", {}).get(STORE_LABEL) != str(self.root):
            raise SnapshotStoreError(f"snapshot is absent or belongs to another store owner: {key}")
        self.command("remove", key)

    def mount(self, recipe: dict, target: Path, log: Path, source: Path) -> None:
        upper = self.recipe_paths(recipe)["upperdir"][0]
        metadata = source.stat()
        os.chown(upper, metadata.st_uid, metadata.st_gid)
        os.chmod(upper, stat.S_IMODE(metadata.st_mode))
        unit = self.mount_unit(target)
        description = f"Darling lane FUSE view {target}"
        properties = self._unit(unit)
        if properties.get("LoadState") != "not-found":
            raise SnapshotStoreError(f"FUSE view service already exists: {unit}")
        started = self._run([
            "systemd-run", "--user", "--collect", "--unit=" + unit,
            "--service-type=forking", "--property=TimeoutStartSec=30s",
            "--property=StandardOutput=append:" + str(log),
            "--property=StandardError=append:" + str(log),
            "--description=" + description,
            "--setenv=PATH=" + self.environment["PATH"],
            str(self.tools.provider), "-o", ",".join(recipe["Options"]), str(target),
        ], timeout=40)
        if started.returncode or not target.is_mount():
            raise SnapshotStoreError(f"cannot mount snapshotter view {target}: {started.stderr.strip()}; inspect {log}")

    @staticmethod
    def mount_unit(target: Path) -> str:
        digest = hashlib.sha256(os.fsencode(target)).hexdigest()[:20]
        return f"darling-lane-fuse-{digest}.service"

    def release_mount_service(self, target: Path) -> None:
        unit = self.mount_unit(target)
        properties = self._unit(unit)
        if properties.get("LoadState") == "not-found":
            return
        self._require_service(unit, f"Darling lane FUSE view {target}", properties)
        if target.is_mount():
            raise SnapshotStoreError(f"refusing to stop provider beneath a mounted view: {target}")
        result = self._run(["systemctl", "--user", "stop", unit])
        if result.returncode:
            raise SnapshotStoreError(f"cannot release {unit}: {result.stderr.strip()}")

    def stop_if_empty(self) -> None:
        if self.snapshots():
            return
        properties = self._unit(self.unit)
        self._require_service(self.unit, self.description, properties)
        stopped = self._run(["systemctl", "--user", "stop", self.unit])
        if stopped.returncode:
            raise SnapshotStoreError(f"cannot stop empty owned store: {stopped.stderr.strip()}")
        # The upstream server has no graceful socket-unlink handler. Only this
        # owned endpoint is removed, after its unit has stopped.
        self.socket.unlink(missing_ok=True)

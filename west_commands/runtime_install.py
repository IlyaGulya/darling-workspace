"""Production CMake installations, independent of writable Darling guest state."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

try:
    from .deploy_receipt import read_receipt, receipt_path
    from .runtime_retention import (
        RuntimeRetentionError, begin_publication, complete_publication, fail_publication,
        require_publication_budget,
    )
except ImportError:
    from deploy_receipt import read_receipt, receipt_path
    from runtime_retention import (
        RuntimeRetentionError, begin_publication, complete_publication, fail_publication,
        require_publication_budget,
    )


class RuntimeInstallationError(ValueError):
    """The selected production installation cannot safely serve this operation."""


def prefix_path_is_safe(prefix: str | Path) -> bool:
    """Reject raw path aliases before any observer can clean another prefix."""

    raw = os.fspath(prefix)
    if not raw.startswith("/") or raw == "/" or any(part in {".", ".."} for part in raw.split("/")):
        return False
    current = Path("/")
    for part in raw.split("/"):
        if not part:
            continue
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return False
    return True

def cmake_cache(build_dir: str | Path) -> dict[str, str]:
    path = Path(build_dir) / "CMakeCache.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeInstallationError(f"not a configured CMake build: {path}: {error}") from error
    values = {}
    for line in lines:
        if not line or line.startswith(("#", "//")) or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if ":" in name:
            values[name.split(":", 1)[0]] = value
    return values


def configured_runtime_root(build_dir: str | Path) -> Path:
    raw = cmake_cache(build_dir).get("CMAKE_INSTALL_PREFIX")
    if not raw or not Path(raw).is_absolute():
        raise RuntimeInstallationError("CMake build has no absolute CMAKE_INSTALL_PREFIX")
    return Path(raw).resolve()


@dataclass(frozen=True)
class InstalledRuntime:
    root: Path
    launcher: Path
    system_root: Path
    identity: dict | None
    retention: tuple[dict[str, object], ...] = ()

    def summary(self) -> dict[str, object]:
        """Return an operational identity reference, not the complete file inventory."""
        if self.identity is None:
            return {"runtime_root": str(self.root), "provenance": "unknown"}
        path = receipt_path(self.root)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        source = self.identity.get("source")
        return {
            "runtime_root": str(self.root),
            "receipt": str(path),
            "receipt_sha256": digest,
            "artifact_count": len(self.identity.get("artifacts", [])),
            "source_revision": source.get("revision") if isinstance(source, dict) else None,
            "source_dirty": source.get("dirty") if isinstance(source, dict) else None,
            "provenance": "installed-receipt",
        }

    def environment(
        self, prefix: str | Path, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        # Keep the caller's prefix spelling: the product's retained-FD walk,
        # not pathlib.resolve(), owns symlink and '..' rejection.
        raw = os.fspath(prefix)
        guest = Path(raw)
        if not prefix_path_is_safe(raw):
            raise RuntimeInstallationError(f"unsafe Darling guest prefix path: {raw}")
        if guest == self.root or self.root in guest.parents or guest in self.root.parents:
            raise RuntimeInstallationError(
                f"runtime installation {self.root} and writable guest prefix {raw} must be disjoint"
            )
        if (
            (guest / "bin" / "darling").is_file()
            and (guest / "libexec" / "darling").is_dir()
        ):
            raise RuntimeInstallationError(
                f"legacy combined runtime/prefix at {raw}; select an installed --runtime-root "
                "and create a fresh guest prefix instead of reusing runtime copies in its upper layer"
            )
        env = dict(os.environ if base is None else base)
        env.update({
            "DARLING_RUNTIME_ROOT": str(self.root),
            "DARLING_LAUNCHER": str(self.launcher),
            "DARLING": str(self.launcher),
            "DPREFIX": raw,
            "DARLING_PREFIX": raw,
        })
        return env


def resolve_runtime(runtime_root: str | Path | None = None) -> InstalledRuntime:
    selected = runtime_root or os.environ.get("DARLING_RUNTIME_ROOT")
    if selected is None:
        launcher = os.environ.get("DARLING_LAUNCHER") or shutil.which("darling")
        if launcher:
            path = Path(launcher).expanduser().resolve()
            if path.parent.name != "bin" or path.name != "darling":
                raise RuntimeInstallationError(
                    f"cannot identify an installation from {launcher}; pass --runtime-root"
                )
            selected = path.parent.parent
    if selected is None:
        raise RuntimeInstallationError(
            "no installed Darling selected; pass --runtime-root ROOT or set DARLING_RUNTIME_ROOT"
        )
    root = Path(selected).expanduser().resolve()
    launcher = root / "bin" / "darling"
    system_root = root / "libexec" / "darling"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeInstallationError(f"installed Darling launcher is missing or not executable: {launcher}")
    if not (root / "bin" / "darlingserver").is_file() or not system_root.is_dir():
        raise RuntimeInstallationError(f"incomplete Darling installation: {root}")
    try:
        identity = read_receipt(root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeInstallationError(f"invalid installed-runtime receipt at {root}: {error}") from error
    return InstalledRuntime(root, launcher, system_root, identity)


@contextmanager
def build_lock(build_dir: str | Path) -> Iterator[None]:
    build = Path(build_dir).expanduser().resolve()
    cmake_cache(build)
    with (build / ".west-darling-build.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def build_runtime(
    build_dir: str | Path, *, targets: Sequence[str] = (), jobs: int | None = None
) -> None:
    build = Path(build_dir).expanduser().resolve()
    cmake_cache(build)
    command = ["cmake", "--build", str(build)]
    if jobs is not None:
        if jobs < 1:
            raise RuntimeInstallationError("--jobs must be positive")
        command.extend(["--parallel", str(jobs)])
    if targets:
        command.extend(["--target", *targets])
    print(f"BUILD: {shlex.join(command)}", file=sys.stderr, flush=True)
    subprocess.run(command, check=True, stdout=sys.stderr, stderr=sys.stderr)


def install_runtime(
    build_dir: str | Path, runtime_root: str | Path, *, manifest_repo: str | Path,
    with_test_data: bool = False,
) -> InstalledRuntime:
    """Publish a fresh normal installation without changing an existing image.

    Published installations are not overwritten, even when apparently idle:
    ordinary product users need not participate in West's lifecycle locks.
    A new candidate uses a new configured root, not a per-test rebuild.
    """
    build = Path(build_dir).expanduser().resolve()
    root = Path(runtime_root).expanduser().absolute()
    if root.is_symlink():
        raise RuntimeInstallationError(f"installation destination must not be a symlink: {root}")
    root = root.resolve()
    configured = configured_runtime_root(build)
    if configured != root:
        raise RuntimeInstallationError(
            f"build installs into {configured}, not {root}; configure CMAKE_INSTALL_PREFIX={root} "
            "before building (the product embeds its installation path)"
        )
    if root == root.parent:
        raise RuntimeInstallationError("refusing a filesystem root as an installation destination")
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root.with_name(f".{root.name}.west-install.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(descriptor).st_uid != os.getuid():
            raise RuntimeInstallationError(f"installation lock belongs to another owner: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if root.exists() and any(root.iterdir()):
            raise RuntimeInstallationError(
                f"will not overwrite published or partial installation {root}; "
                "select that installation to run it, or configure a fresh runtime root for new code"
            )
        source_raw = cmake_cache(build).get("CMAKE_HOME_DIRECTORY")
        source_directory = (
            Path(source_raw) if source_raw and Path(source_raw).is_absolute() else None
        )
        try:
            require_publication_budget(root)
        except RuntimeRetentionError as error:
            raise RuntimeInstallationError(str(error)) from error
        try:
            managed = begin_publication(
                root,
                build_directory=build,
                manifest_directory=Path(manifest_repo),
                source_directory=source_directory,
            )
        except RuntimeRetentionError as error:
            raise RuntimeInstallationError(str(error)) from error
        try:
            command = ["cmake", "--install", str(build)]
            print(f"INSTALL: {shlex.join(command)}", file=sys.stderr, flush=True)
            subprocess.run(
                command, check=True, stdout=sys.stderr, stderr=sys.stderr,
                pass_fds=(descriptor,),
            )
            manifests = [build / "install_manifest.txt"]
            if with_test_data:
                component = ["cmake", "--install", str(build), "--component", "tests-data"]
                print(f"INSTALL: {shlex.join(component)}", file=sys.stderr, flush=True)
                subprocess.run(
                    component, check=True, stdout=sys.stderr, stderr=sys.stderr,
                    pass_fds=(descriptor,),
                )
                data_manifest = build / "install_manifest_tests-data.txt"
                if not data_manifest.is_file() or not data_manifest.read_text(encoding="utf-8").strip():
                    raise RuntimeInstallationError("product tests-data component did not install its declared input data")
                manifests.append(data_manifest)
            try:
                from .deploy_receipt import publish_install_receipt
            except ImportError:
                from deploy_receipt import publish_install_receipt
            publish_install_receipt(root, build, Path(manifest_repo), install_manifests=tuple(manifests))
            published = resolve_runtime(root)
            retention_rows: tuple[dict[str, object], ...] = ()
            if managed is not None:
                try:
                    completed = complete_publication(root)
                except RuntimeRetentionError as error:
                    raise RuntimeInstallationError(str(error)) from error
                retention_rows = tuple(completed.get("retention", ())) if completed else ()
                for row in retention_rows:
                    if row.get("action") not in ("keep",):
                        print(
                            f"RETENTION: {row.get('action')}: {row.get('path')} "
                            f"({row.get('reason')}, {row.get('bytes')} bytes)",
                            file=sys.stderr, flush=True,
                        )
            return InstalledRuntime(
                published.root, published.launcher, published.system_root,
                published.identity, retention_rows,
            )
        except BaseException as error:
            if managed is not None:
                try:
                    fail_publication(root, error)
                except (OSError, ValueError):
                    pass
            raise
    finally:
        os.close(descriptor)

"""Lease-bound scratch ownership and fail-closed garbage collection."""

from __future__ import annotations

import ctypes
import fcntl
import os
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


MARKER = ".darling-scratch-v1"
LEASE = ".darling-scratch-lease"
WORKTREES = ".darling-scratch-worktrees-v1"
DEFAULT_TTL_SECONDS = 72 * 3600
DEFAULT_KEEP = 2
DEFAULT_CANDIDATES = 64
DEFAULT_SECONDS = 0.25


class ScratchSafetyError(RuntimeError):
    """The root cannot be mutated with proven ownership."""


class ScratchQuarantinedError(ScratchSafetyError):
    """The public root is isolated but bounded collection is incomplete."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{reason}: {path}")
        self.path = path


@dataclass
class ProcessCensus:
    references: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    overflows: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScratchSafetyError("scratch operation time budget exceeded")
    return remaining


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_root(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as error:
        raise ScratchSafetyError(f"cannot retain scratch root: {error}") from error


def _regular_at(directory_fd: int, name: str, *, mode: int = 0o600) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        if error.errno == getattr(os, "ELOOP", 40):
            raise ScratchSafetyError(f"hostile ownership metadata: {name}") from error
        raise ScratchSafetyError(f"cannot open {name}: {error}") from error
    try:
        opened = os.fstat(fd)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_object(opened, named):
            raise ScratchSafetyError(f"{name} replaced while opening")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != mode
        ):
            raise ScratchSafetyError(f"hostile ownership metadata: {name}")
        return fd, opened
    except BaseException:
        os.close(fd)
        raise


def _remove_tree_at(parent_fd: int, name: str, *, deadline: float | None = None) -> None:
    """Remove one exact entry without following any component below parent_fd."""
    _remaining(deadline)
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    child_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        if not _same_object(info, os.fstat(child_fd)):
            raise ScratchSafetyError("disposable entry replaced while opening")
        with os.scandir(child_fd) as entries:
            for entry in entries:
                _remaining(deadline)
                _remove_tree_at(child_fd, entry.name, deadline=deadline)
    finally:
        os.close(child_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_object(info, current):
        raise ScratchSafetyError("disposable entry replaced before removal")
    os.rmdir(name, dir_fd=parent_fd)


def _remove_relative_at(root_fd: int, relative: Path, *, deadline: float | None = None) -> None:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ScratchSafetyError("unsafe relative removal path")
    current_fd = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        _remove_tree_at(current_fd, relative.name, deadline=deadline)
    except OSError as error:
        raise ScratchSafetyError(f"cannot remove registered relative path: {error}") from error
    finally:
        os.close(current_fd)


def _rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ScratchSafetyError("renameat2 is unavailable for safe scratch isolation")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(directory_fd, os.fsencode(source), directory_fd, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise ScratchSafetyError(f"cannot isolate scratch root: {os.strerror(error)}")


def default_namespace() -> Path:
    return Path(os.environ.get("DARLING_SCRATCH_ROOT", Path(tempfile.gettempdir()) / "darling-scratch-v1"))


def _validated_namespace(path: Path, *, create: bool = False) -> Path:
    path = path.expanduser().absolute()
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise ScratchSafetyError(f"cannot inspect scratch namespace: {error}") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ScratchSafetyError("hostile scratch namespace")
    return path


def _lstat_regular_owned(path: Path, *, mode: int = 0o600) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ScratchSafetyError(f"cannot inspect {path}: {error}") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ScratchSafetyError(f"hostile ownership metadata: {path}")
    if stat.S_IMODE(info.st_mode) != mode:
        raise ScratchSafetyError(f"unexpected mode on {path}: {stat.S_IMODE(info.st_mode):04o}")
    return info


def _write_marker_at(root_fd: int, kind: str, identifier: str, created_ns: int) -> None:
    fd = os.open(
        MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600, dir_fd=root_fd,
    )
    try:
        os.write(
            fd,
            f"version=1\nkind={kind}\nid={identifier}\ncreated_ns={created_ns}\n".encode(),
        )
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_marker(root: Path) -> dict[str, str]:
    root_fd = _open_root(root)
    try:
        return _read_marker_at(root_fd)
    finally:
        os.close(root_fd)


def _read_marker_at(root_fd: int) -> dict[str, str]:
    fd, _ = _regular_at(root_fd, MARKER)
    try:
        raw = os.read(fd, 8193)
        if len(raw) > 8192:
            raise ScratchSafetyError("oversized scratch marker")
        raw_text = raw.decode("ascii")
    except (OSError, UnicodeError) as error:
        raise ScratchSafetyError(f"cannot read scratch marker: {error}") from error
    finally:
        os.close(fd)
    values: dict[str, str] = {}
    for line in raw_text.splitlines():
        if line.count("=") != 1:
            raise ScratchSafetyError("malformed scratch marker")
        key, value = line.split("=", 1)
        if key in values or not key or not value:
            raise ScratchSafetyError("malformed scratch marker")
        values[key] = value
    if set(values) != {"version", "kind", "id", "created_ns"} or values["version"] != "1":
        raise ScratchSafetyError("unsupported scratch marker")
    try:
        int(values["created_ns"])
        uuid.UUID(values["id"])
    except ValueError as error:
        raise ScratchSafetyError("malformed scratch marker identity") from error
    return values


def _open_lease_at(root_fd: int, *, blocking: bool) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LEASE, flags, dir_fd=root_fd)
    try:
        opened = os.fstat(fd)
        named = os.stat(LEASE, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise ScratchSafetyError("hostile scratch lease metadata")
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ScratchSafetyError("scratch lease replaced while opening")
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, operation)
        except BlockingIOError as error:
            raise ScratchSafetyError("scratch lease is active") from error
        named_after = os.stat(LEASE, dir_fd=root_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named_after.st_dev, named_after.st_ino):
            raise ScratchSafetyError("scratch lease replaced after locking")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _direct_child(namespace: Path, root: Path) -> tuple[Path, Path]:
    namespace = _validated_namespace(namespace)
    root = root.expanduser().absolute()
    try:
        parent = root.parent.resolve(strict=True)
    except OSError as error:
        raise ScratchSafetyError(f"cannot resolve scratch parent: {error}") from error
    if parent != namespace or root == namespace:
        raise ScratchSafetyError("scratch root is not a direct namespace child")
    try:
        info = root.lstat()
    except OSError as error:
        raise ScratchSafetyError(f"cannot inspect scratch root: {error}") from error
    if not stat.S_ISDIR(info.st_mode) or root.is_symlink() or info.st_uid != os.getuid():
        raise ScratchSafetyError("scratch root is not an owned directory")
    return namespace, root


def _path_inside(path: str, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _live_process_references(
    root: Path, *, limit: int = 65536, fd_limit: int = 4096,
    ignored_fds: set[tuple[int, int]] | None = None, deadline: float | None = None,
    proc_entries: Iterable[Path] | None = None,
) -> ProcessCensus:
    census = ProcessCensus()
    proc = Path("/proc")
    entries = proc.iterdir() if proc_entries is None else proc_entries
    for index, entry in enumerate(entries):
        _remaining(deadline)
        if index >= limit:
            raise ScratchSafetyError("process census budget exceeded")
        if not entry.name.isdigit():
            continue
        try:
            process_info = entry.stat()
        except FileNotFoundError:
            continue
        except OSError as error:
            diagnostic = f"pid={entry.name}:stat:{error}"
            if error.errno in {13, 1}:
                census.unreadable.append(diagnostic)
            else:
                census.ambiguous.append(diagnostic)
            continue
        if process_info.st_uid != os.getuid():
            continue
        vanished = False
        for label in ("cwd", "root", "exe"):
            try:
                target = os.readlink(entry / label)
            except FileNotFoundError:
                vanished = True
                break
            except OSError as error:
                diagnostic = f"pid={entry.name}:{label}:{error}"
                if error.errno in {13, 1}:
                    census.unreadable.append(diagnostic)
                else:
                    census.ambiguous.append(diagnostic)
                vanished = True
                break
            if _path_inside(target.removesuffix(" (deleted)"), root):
                census.references.append(f"pid={entry.name}:{label}")
        if vanished:
            continue
        fds = entry / "fd"
        try:
            names = list(fds.iterdir())
        except FileNotFoundError:
            continue
        except OSError as error:
            diagnostic = f"pid={entry.name}:fd-census:{error}"
            if error.errno in {13, 1}:
                census.unreadable.append(diagnostic)
            else:
                census.ambiguous.append(diagnostic)
            continue
        if len(names) > fd_limit:
            census.overflows.append(f"pid={entry.name}:fd-count={len(names)}>{fd_limit}")
            continue
        for descriptor in names:
            _remaining(deadline)
            if ignored_fds and (int(entry.name), int(descriptor.name)) in ignored_fds:
                continue
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            except OSError as error:
                diagnostic = f"pid={entry.name}:fd={descriptor.name}:{error}"
                if error.errno in {13, 1}:
                    census.unreadable.append(diagnostic)
                else:
                    census.ambiguous.append(diagnostic)
                continue
            if _path_inside(target.removesuffix(" (deleted)"), root):
                census.references.append(f"pid={entry.name}:fd={descriptor.name}")
    return census


def _mounts_inside(root: Path, *, deadline: float | None = None) -> list[str]:
    hits: list[str] = []
    try:
        with Path("/proc/self/mountinfo").open("r") as handle:
            payload = handle.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise ScratchSafetyError("mount census budget exceeded")
        lines = payload.splitlines()
    except OSError as error:
        raise ScratchSafetyError(f"cannot inspect mounts: {error}") from error
    for line in lines:
        _remaining(deadline)
        fields = line.split()
        if len(fields) < 5:
            raise ScratchSafetyError("malformed mountinfo")
        mountpoint = fields[4].replace("\\040", " ")
        if _path_inside(mountpoint, root):
            hits.append(mountpoint)
    return hits


def _git_output(repo: Path, *args: str, deadline: float | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False,
            timeout=_remaining(deadline),
        )
    except subprocess.TimeoutExpired as error:
        raise ScratchSafetyError(f"git operation time budget exceeded for {repo}") from error
    if result.returncode:
        raise ScratchSafetyError(
            f"git {' '.join(args)} failed for {repo}: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def _repositories(namespace: Path, *, budget: int = 20000, deadline: float | None = None) -> list[Path]:
    repositories: list[Path] = []
    seen = 0
    def unreadable(error: OSError) -> None:
        raise ScratchSafetyError(f"unreadable repository census: {error}") from error

    for base, dirs, files in os.walk(namespace, onerror=unreadable):
        _remaining(deadline)
        seen += 1
        if seen > budget:
            raise ScratchSafetyError("repository census budget exceeded")
        dirs[:] = [name for name in dirs if name not in {"objects", "target", "build"}]
        if ".git" in dirs or ".git" in files:
            repositories.append(Path(base))
            if ".git" in dirs:
                dirs.remove(".git")
    return repositories


def _alternate_targets(repo: Path, *, deadline: float | None = None) -> list[Path]:
    git_path = Path(_git_output(repo, "rev-parse", "--git-path", "objects/info/alternates", deadline=deadline))
    if not git_path.is_absolute():
        git_path = repo / git_path
    try:
        fd = os.open(
            git_path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ScratchSafetyError(f"cannot open repository alternates: {error}") from error
    try:
        opened = os.fstat(fd)
        named = os.stat(git_path, follow_symlinks=False)
        if (
            not _same_object(opened, named)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise ScratchSafetyError("hostile repository alternates metadata")
        payload = os.read(fd, 1_048_577)
        if len(payload) > 1_048_576:
            raise ScratchSafetyError("repository alternates budget exceeded")
        text = payload.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ScratchSafetyError(f"cannot read repository alternates: {error}") from error
    finally:
        os.close(fd)
    targets = []
    for line in text.splitlines():
        if line:
            try:
                target = (git_path.parent / line).resolve() if not Path(line).is_absolute() else Path(line).resolve()
            except OSError as error:
                raise ScratchSafetyError(f"cannot resolve repository alternate: {error}") from error
            targets.append(target)
    return targets


def _publish_alternates(
    path: Path,
    payload: bytes | None,
    *,
    backup_name: str,
) -> tuple[int, str]:
    """Atomically replace/remove alternates while retaining the old file."""
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".alternates-new-{uuid.uuid4().hex}"
    isolated = False
    try:
        if payload is not None:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise ScratchSafetyError("short repository alternates write")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        os.rename(path.name, backup_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        isolated = True
        if payload is not None:
            os.rename(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
        return parent_fd, temporary
    except BaseException:
        if isolated:
            try:
                os.unlink(path.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            try:
                os.rename(backup_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                # Keep the exact backup in place; the caller must retain the
                # donor rather than claiming a completed dissociation.
                pass
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)
        raise


def _alternate_survivors(namespace: Path, donor: Path, *, deadline: float | None = None) -> list[Path]:
    survivors: list[Path] = []
    for repo in _repositories(namespace, deadline=deadline):
        if _path_inside(str(repo), donor):
            continue
        if not any(_path_inside(str(target), donor) for target in _alternate_targets(repo, deadline=deadline)):
            continue
        survivors.append(repo)
    return survivors


def dissociate_repository(namespace: Path, repository: Path, donor: Path) -> None:
    """Explicitly copy objects and remove one verified alternate dependency."""
    namespace = _validated_namespace(namespace)
    repository = repository.resolve(strict=True)
    donor = donor.resolve(strict=True)
    if not _path_inside(str(repository), namespace) or not _path_inside(str(donor), namespace):
        raise ScratchSafetyError("repository and donor must be inside the scratch namespace")
    if not any(_path_inside(str(target), donor) for target in _alternate_targets(repository)):
        raise ScratchSafetyError("repository does not depend on the requested donor")
    _git_output(repository, "repack", "-a", "-d")
    alternates = Path(_git_output(repository, "rev-parse", "--git-path", "objects/info/alternates"))
    if not alternates.is_absolute():
        alternates = repository / alternates
    original_targets = _alternate_targets(repository)
    try:
        alternate_fd = os.open(
            alternates,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(alternate_fd)
            named = os.stat(alternates, follow_symlinks=False)
            if not _same_object(opened, named):
                raise ScratchSafetyError("repository alternates replaced before dissociation")
            original_payload = os.read(alternate_fd, 1_048_577)
            if len(original_payload) > 1_048_576:
                raise ScratchSafetyError("repository alternates budget exceeded")
            original_payload.decode("utf-8")
        finally:
            os.close(alternate_fd)
    except ScratchSafetyError:
        raise
    except (OSError, UnicodeError) as error:
        raise ScratchSafetyError(f"cannot retain repository alternates: {error}") from error
    remaining = [
        line for line in original_payload.decode("utf-8").splitlines()
        if line and not _path_inside(
            str((alternates.parent / line).resolve() if not Path(line).is_absolute() else Path(line).resolve()), donor
        )
    ]
    if not any(_path_inside(str(target), donor) for target in original_targets):
        raise ScratchSafetyError("repository alternate changed before dissociation")
    replacement = ("\n".join(remaining) + "\n").encode() if remaining else None
    backup_name = f".alternates-old-{uuid.uuid4().hex}"
    parent_fd, temporary = _publish_alternates(alternates, replacement, backup_name=backup_name)
    try:
        try:
            _git_output(repository, "fsck", "--connectivity-only")
        except BaseException:
            try:
                os.unlink(alternates.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.rename(backup_name, alternates.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            raise
        os.unlink(backup_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _remove_nested_worktrees(
    root: Path,
    *,
    force_dirty: bool,
    mutate: bool,
    skip: set[Path] | None = None,
    deadline: float | None = None,
) -> list[str]:
    diagnostics: list[str] = []
    for repo in _repositories(root, deadline=deadline):
        if skip and repo in skip:
            continue
        status_text = _git_output(repo, "status", "--porcelain=v1", "--untracked-files=all", deadline=deadline)
        if status_text and not force_dirty:
            raise ScratchSafetyError(f"dirty worktree retained: {repo}")
        git_file = repo / ".git"
        if not git_file.is_file():
            continue
        common = Path(_git_output(repo, "rev-parse", "--git-common-dir", deadline=deadline))
        if not common.is_absolute():
            common = (repo / common).resolve()
        owner_repo = Path(_git_output(repo, "rev-parse", "--show-toplevel", deadline=deadline))
        # Worktree removal is issued through the common repository. The
        # current worktree's top-level is not necessarily that repository, so
        # use --git-dir directly and prune the same registration afterward.
        diagnostics.append(str(repo))
        if mutate:
            repair = subprocess.run(
                ["git", f"--git-dir={common}", "worktree", "repair", str(owner_repo)],
                capture_output=True, text=True, check=False, timeout=_remaining(deadline),
            )
            if repair.returncode:
                raise ScratchSafetyError(f"cannot repair isolated worktree {repo}: {(repair.stderr or repair.stdout).strip()}")
            result = subprocess.run(
                ["git", f"--git-dir={common}", "worktree", "remove", *( ["--force"] if force_dirty else []), str(owner_repo)],
                capture_output=True, text=True, check=False,
                timeout=_remaining(deadline),
            )
            if result.returncode:
                raise ScratchSafetyError(f"cannot remove registered worktree {repo}: {(result.stderr or result.stdout).strip()}")
            _git_output(common, "worktree", "prune", deadline=deadline)
    return diagnostics


def _registered_worktrees(root: Path) -> list[tuple[Path, Path]]:
    root_fd = _open_root(root)
    try:
        try:
            fd, _ = _regular_at(root_fd, WORKTREES)
        except ScratchSafetyError as error:
            try:
                os.stat(WORKTREES, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return []
            raise error
        try:
            payload = os.read(fd, 1_048_577)
            if len(payload) > 1_048_576:
                raise ScratchSafetyError("worktree registry budget exceeded")
            text = payload.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise ScratchSafetyError(f"cannot read worktree registry: {error}") from error
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    records: list[tuple[Path, Path]] = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 2:
            raise ScratchSafetyError("malformed worktree registry")
        repo = Path(fields[0])
        relative = Path(fields[1])
        if not repo.is_absolute() or relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ScratchSafetyError("unsafe worktree registry")
        records.append((repo, root / relative))
    return records


def _remove_registered_worktrees(
    root: Path, *, force_dirty: bool, mutate: bool, registered_at_root: Path | None = None,
    deadline: float | None = None,
) -> None:
    for repo, worktree in reversed(_registered_worktrees(root)):
        if not repo.is_dir():
            if not force_dirty:
                raise ScratchSafetyError(f"registered worktree owner is unavailable: {repo}")
            if mutate and worktree.exists():
                root_fd = _open_root(root)
                try:
                    _remove_relative_at(root_fd, worktree.relative_to(root), deadline=deadline)
                finally:
                    os.close(root_fd)
            continue
        original_worktree = worktree
        if registered_at_root is not None:
            original_worktree = registered_at_root / worktree.relative_to(root)
        listing = _git_output(repo, "worktree", "list", "--porcelain", deadline=deadline)
        registered = f"worktree {original_worktree}" in listing.splitlines()
        if worktree.exists():
            if registered:
                status_text = _git_output(worktree, "status", "--porcelain=v1", "--untracked-files=all", deadline=deadline)
            else:
                marker = worktree / ".git"
                _lstat_regular_owned(marker, mode=stat.S_IMODE(marker.stat().st_mode))
                result = subprocess.run(
                    ["git", f"--git-dir={_git_output(repo, 'rev-parse', '--git-dir', deadline=deadline)}", f"--work-tree={worktree}", "status", "--porcelain=v1", "--untracked-files=all"],
                    cwd=repo, capture_output=True, text=True, check=False, timeout=_remaining(deadline),
                )
                if result.returncode:
                    raise ScratchSafetyError(f"cannot inspect stale worktree {worktree}")
                status_text = result.stdout.strip()
            if status_text and not force_dirty:
                raise ScratchSafetyError(f"dirty worktree retained: {worktree}")
            if mutate:
                if registered:
                    repair = subprocess.run(
                        ["git", "-C", str(repo), "worktree", "repair", str(worktree)],
                        capture_output=True, text=True, check=False, timeout=_remaining(deadline),
                    )
                    if repair.returncode:
                        raise ScratchSafetyError(f"cannot repair isolated worktree {worktree}: {(repair.stderr or repair.stdout).strip()}")
                    result = subprocess.run(
                        ["git", "-C", str(repo), "worktree", "remove", *( ["--force"] if force_dirty else []), str(worktree)],
                        capture_output=True, text=True, check=False,
                        timeout=_remaining(deadline),
                    )
                    if result.returncode:
                        raise ScratchSafetyError(f"cannot remove registered worktree {worktree}: {(result.stderr or result.stdout).strip()}")
                else:
                    root_fd = _open_root(root)
                    try:
                        _remove_relative_at(root_fd, worktree.relative_to(root), deadline=deadline)
                    finally:
                        os.close(root_fd)
        if mutate:
            _git_output(repo, "worktree", "prune", deadline=deadline)


@dataclass
class GCResult:
    removed: list[Path] = field(default_factory=list)
    quarantined: list[Path] = field(default_factory=list)
    retained: list[tuple[Path, str]] = field(default_factory=list)
    bytes_freed: int = 0
    scanned: int = 0
    bounded: bool = False
    diagnostics: list[str] = field(default_factory=list)


def directory_size(root: Path, *, budget: int = 1_000_000, deadline: float | None = None) -> int:
    total = 0
    count = 0
    def unreadable(error: OSError) -> None:
        raise ScratchSafetyError(f"unreadable size census: {error}") from error

    for base, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        _remaining(deadline)
        count += len(dirs) + len(files)
        if count > budget:
            raise ScratchSafetyError("directory size budget exceeded")
        for name in files:
            _remaining(deadline)
            try:
                total += (Path(base) / name).lstat().st_size
            except OSError as error:
                raise ScratchSafetyError(f"cannot size scratch entry: {error}") from error
    return total


class OwnedScratchRoot:
    """One exact scratch root with an exclusive lifetime lease."""

    def __init__(
        self, namespace: Path, root: Path, lease_fd: int, root_fd: int,
        marker_identity: tuple[str, str, str, str],
    ):
        self.namespace = namespace
        self.path = root
        self._lease_fd = lease_fd
        self._root_fd = root_fd
        self._marker_identity = marker_identity
        self._closed = False
        self._disposable: set[Path] = set()

    @classmethod
    def create(cls, *, namespace: Path | None = None, kind: str, prefix: str | None = None) -> "OwnedScratchRoot":
        namespace = _validated_namespace(namespace or default_namespace(), create=True)
        root = Path(tempfile.mkdtemp(prefix=prefix or f"{kind}-", dir=namespace))
        created_root = root.lstat()
        identifier = str(uuid.uuid4())
        created_ns = time.time_ns()
        try:
            root_fd = _open_root(root)
            _write_marker_at(root_fd, kind, identifier, created_ns)
            lease_fd = os.open(
                LEASE, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600, dir_fd=root_fd,
            )
            os.fsync(lease_fd)
            namespace_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(namespace_fd)
            finally:
                os.close(namespace_fd)
            fcntl.flock(lease_fd, fcntl.LOCK_EX)
            return cls(
                namespace, root, lease_fd, root_fd,
                ("1", kind, identifier, str(created_ns)),
            )
        except BaseException:
            if "lease_fd" in locals():
                os.close(lease_fd)
            if "root_fd" in locals():
                os.close(root_fd)
            namespace_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                try:
                    named = os.stat(root.name, dir_fd=namespace_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if _same_object(created_root, named):
                        _remove_tree_at(namespace_fd, root.name)
            finally:
                os.close(namespace_fd)
            raise

    @classmethod
    def acquire(cls, namespace: Path, root: Path) -> "OwnedScratchRoot":
        namespace, root = _direct_child(namespace, root)
        root_fd = _open_root(root)
        try:
            marker = _read_marker_at(root_fd)
            return cls(
                namespace, root, _open_lease_at(root_fd, blocking=False), root_fd,
                (marker["version"], marker["kind"], marker["id"], marker["created_ns"]),
            )
        except BaseException:
            os.close(root_fd)
            raise

    def register_disposable(self, path: Path) -> None:
        candidate = path.absolute()
        try:
            relative = candidate.relative_to(self.path)
        except ValueError as error:
            raise ScratchSafetyError("disposable path escapes scratch root") from error
        if len(relative.parts) != 1 or any(part in {"", ".", ".."} for part in relative.parts):
            raise ScratchSafetyError("disposable path must be one exact direct child")
        self._disposable.add(relative)

    def register_worktree(self, repo: Path, path: Path) -> None:
        repo = repo.resolve(strict=True)
        path = path.absolute()
        try:
            relative = path.relative_to(self.path)
        except ValueError as error:
            raise ScratchSafetyError("worktree escapes scratch root") from error
        try:
            existing_fd, _ = _regular_at(self._root_fd, WORKTREES)
        except ScratchSafetyError:
            try:
                os.stat(WORKTREES, dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise
        else:
            os.close(existing_fd)
        fd = os.open(
            WORKTREES,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=self._root_fd,
        )
        try:
            os.write(fd, f"{repo}\t{relative}\n".encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_failure_log(self, payload: str | bytes) -> Path:
        encoded = payload.encode() if isinstance(payload, str) else payload
        if len(encoded) > 1_048_576:
            raise ScratchSafetyError("failure log exceeds bounded size")
        try:
            fd = os.open(
                "failure.raw.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600, dir_fd=self._root_fd,
            )
        except FileExistsError:
            existing_fd, _ = _regular_at(self._root_fd, "failure.raw.log")
            os.close(existing_fd)
            fd = os.open(
                "failure.raw.log", os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        return self.path / "failure.raw.log"

    def retain(self, *, raw_log: Path | None = None) -> None:
        """Keep review/source state while discarding registered generated state."""
        for relative in sorted(self._disposable, reverse=True):
            try:
                _remove_tree_at(self._root_fd, relative.name)
            except FileNotFoundError:
                continue
        if raw_log is not None:
            source_fd = os.open(raw_log, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
            try:
                payload = os.read(source_fd, 1_048_577)
                if len(payload) > 1_048_576:
                    raise ScratchSafetyError("failure log exceeds bounded size")
            finally:
                os.close(source_fd)
            self.write_failure_log(payload)
        os.utime(self.path, None, follow_symlinks=False)
        self.close()

    def discard(self) -> None:
        if self._closed:
            return
        os.close(self._lease_fd)
        os.close(self._root_fd)
        self._closed = True
        discard_exact(self.namespace, self.path)

    def close(self) -> None:
        if not self._closed:
            os.close(self._lease_fd)
            os.close(self._root_fd)
            self._closed = True

    def relocated(self, path: Path) -> None:
        """Update the exact path after an atomic same-parent evidence rename."""
        if path.parent.resolve() != self.namespace:
            raise ScratchSafetyError("relocated scratch escapes its namespace")
        new_root_fd = _open_root(path)
        try:
            marker = _read_marker_at(new_root_fd)
            identity = (marker["version"], marker["kind"], marker["id"], marker["created_ns"])
            if identity != self._marker_identity:
                raise ScratchSafetyError("relocated scratch marker identity mismatch")
            lease = os.stat(LEASE, dir_fd=new_root_fd, follow_symlinks=False)
            if not _same_object(os.fstat(self._lease_fd), lease):
                raise ScratchSafetyError("relocated scratch lease identity mismatch")
        except BaseException:
            os.close(new_root_fd)
            raise
        os.close(self._root_fd)
        self._root_fd = new_root_fd
        self.path = path

    def __enter__(self) -> "OwnedScratchRoot":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.discard()
        else:
            payload = f"{exc_type.__name__}: {exc}\n".encode()
            self.write_failure_log(payload)
            self.retain()


def inspect_exact(
    namespace: Path,
    root: Path,
    *,
    force_dirty: bool = False,
    lease_fd: int | None = None,
    root_fd: int | None = None,
    deadline: float | None = None,
) -> tuple[int, list[str], ProcessCensus]:
    namespace, root = _direct_child(namespace, root)
    _read_marker(root)
    census = _live_process_references(
        root,
        ignored_fds={
            (os.getpid(), descriptor)
            for descriptor in (lease_fd, root_fd)
            if descriptor is not None
        },
        deadline=deadline,
    )
    if census.references:
        raise ScratchSafetyError(
            "live process references scratch: " + ", ".join(census.references[:8])
        )
    if census.overflows:
        raise ScratchSafetyError("fd census overflow: " + ", ".join(census.overflows[:8]))
    if census.ambiguous:
        raise ScratchSafetyError("ambiguous process census: " + ", ".join(census.ambiguous[:8]))
    mounts = _mounts_inside(root, deadline=deadline)
    if mounts:
        raise ScratchSafetyError("mounted subtree retained: " + ", ".join(mounts[:8]))
    _remaining(deadline)
    size = directory_size(root, deadline=deadline)
    registered = {path for _repo, path in _registered_worktrees(root)}
    try:
        _remove_registered_worktrees(root, force_dirty=force_dirty, mutate=False, deadline=deadline)
        worktrees = _remove_nested_worktrees(
            root, force_dirty=force_dirty, mutate=False, skip=registered, deadline=deadline
        )
    except subprocess.TimeoutExpired as error:
        raise ScratchSafetyError("git census time budget exceeded") from error
    return size, worktrees, census


def discard_exact(
    namespace: Path,
    root: Path,
    *,
    force_dirty: bool = False,
    dry_run: bool = False,
    max_seconds: float | None = None,
    diagnostics: list[str] | None = None,
) -> int:
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    namespace, root = _direct_child(namespace, root)
    original = root.lstat()
    root_fd = _open_root(root)
    try:
        opened_root = os.fstat(root_fd)
        if not _same_object(original, opened_root):
            raise ScratchSafetyError("scratch root replaced while retaining")
        _read_marker_at(root_fd)
        lease_fd = _open_lease_at(root_fd, blocking=False)
    except BaseException:
        os.close(root_fd)
        raise
    try:
        size, _, census = inspect_exact(
            namespace, root, force_dirty=force_dirty, lease_fd=lease_fd, root_fd=root_fd,
            deadline=deadline
        )
        if diagnostics is not None:
            diagnostics.extend(census.unreadable)
        survivors = _alternate_survivors(namespace, root, deadline=deadline)
        if survivors:
            raise ScratchSafetyError(
                "scratch donor retained by repository alternates: "
                + ", ".join(str(path) for path in survivors[:4])
            )
        _remaining(deadline)
        if not dry_run:
            # Revalidate marker/lease names after all fallible preparation.
            named_root = root.lstat()
            if not _same_object(original, named_root):
                raise ScratchSafetyError("scratch root replaced before isolation")
            _read_marker_at(root_fd)
            opened = os.fstat(lease_fd)
            named = os.stat(LEASE, dir_fd=root_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise ScratchSafetyError("scratch lease replaced before deletion")
            namespace_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            quarantine_name = f".gc-{uuid.uuid4().hex}"
            quarantine = namespace / quarantine_name
            try:
                _rename_noreplace(namespace_fd, root.name, quarantine_name)
                isolated = quarantine.lstat()
                if (isolated.st_dev, isolated.st_ino) != (original.st_dev, original.st_ino):
                    try:
                        _rename_noreplace(namespace_fd, quarantine_name, root.name)
                    except ScratchSafetyError:
                        pass
                    raise ScratchSafetyError("scratch root changed during isolation")
                _read_marker(quarantine)
                isolated_lease = (quarantine / LEASE).stat(follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (isolated_lease.st_dev, isolated_lease.st_ino):
                    raise ScratchSafetyError("scratch lease changed during isolation")
                os.fsync(namespace_fd)
                try:
                    _remove_registered_worktrees(
                        quarantine, force_dirty=force_dirty, mutate=True,
                        registered_at_root=root,
                        deadline=deadline,
                    )
                    _remove_nested_worktrees(
                        quarantine,
                        force_dirty=force_dirty,
                        mutate=True,
                        skip={path for _repo, path in _registered_worktrees(quarantine)},
                        deadline=deadline,
                    )
                    _remove_tree_at(namespace_fd, quarantine_name, deadline=deadline)
                except BaseException as error:
                    raise ScratchQuarantinedError(
                        quarantine, f"isolated scratch retained after collection failure: {error}"
                    ) from error
                os.fsync(namespace_fd)
            finally:
                os.close(namespace_fd)
        return size
    finally:
        os.close(lease_fd)
        os.close(root_fd)


def garbage_collect(
    namespace: Path | None = None,
    *,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    max_candidates: int = DEFAULT_CANDIDATES,
    max_seconds: float = DEFAULT_SECONDS,
) -> GCResult:
    result = GCResult()
    configured = (namespace or default_namespace()).expanduser().absolute()
    if not configured.exists():
        return result
    namespace = _validated_namespace(configured)
    if not namespace.is_dir() or namespace.is_symlink():
        return result
    started = time.monotonic()
    candidates = []
    for child in namespace.iterdir():
        if len(candidates) >= max_candidates or time.monotonic() - started >= max_seconds:
            result.bounded = True
            break
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            _read_marker(child)
            retained_ns = child.stat(follow_symlinks=False).st_mtime_ns
        except ScratchSafetyError as error:
            result.retained.append((child, str(error)))
            continue
        candidates.append((retained_ns, child))
    candidates.sort(reverse=True)
    cutoff_ns = time.time_ns() - int(ttl_seconds * 1_000_000_000)
    for index, (retained_ns, child) in enumerate(candidates):
        result.scanned += 1
        if index < keep:
            result.retained.append((child, "keep-newest"))
            continue
        if retained_ns > cutoff_ns:
            result.retained.append((child, "fresh"))
            continue
        try:
            remaining = _remaining(started + max_seconds)
            candidate_diagnostics: list[str] = []
            freed = discard_exact(
                namespace, child, dry_run=dry_run, max_seconds=remaining,
                diagnostics=candidate_diagnostics,
            )
            result.diagnostics.extend(f"{child}: {item}" for item in candidate_diagnostics)
        except ScratchQuarantinedError as error:
            result.quarantined.append(error.path)
            result.retained.append((error.path, str(error)))
            result.bounded = "time budget exceeded" in str(error)
            if result.bounded:
                break
            continue
        except ScratchSafetyError as error:
            result.retained.append((child, str(error)))
            if "time budget exceeded" in str(error):
                result.bounded = True
                break
            continue
        result.removed.append(child)
        result.bytes_freed += freed
    return result

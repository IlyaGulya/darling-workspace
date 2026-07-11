"""Darling prefix lifecycle helpers for ``west test``."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

ProcessEntry = tuple[int, int, str]


@dataclass
class RootlessPrefixCleanupResult:
    changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems


def rootless_prefix_process_snapshot(
    prefix: Path,
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
) -> list[str]:
    """List rootless guest processes that explicitly belong to ``prefix``.

    A rootless guest can re-parent itself to init and therefore disappear from
    the darlingserver process tree. The launcher-provided environment remains
    its stable ownership token, unlike a process name such as ``launchd``.
    """

    current_pid = os.getpid() if current_pid is None else current_pid
    prefix_marker = f"DARLING_PREFIX={prefix}".encode()
    rootless_marker = b"DARLING_ROOTLESS=1"
    entries: list[str] = []
    try:
        process_dirs = sorted(proc_root.iterdir(), key=lambda path: int(path.name) if path.name.isdigit() else -1)
    except OSError:
        return entries
    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid == current_pid:
            continue
        try:
            environment = set((process_dir / "environ").read_bytes().split(b"\0"))
        except OSError:
            continue
        if prefix_marker not in environment or rootless_marker not in environment:
            continue
        try:
            argv = [part.decode(errors="replace") for part in (process_dir / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            argv = []
        entries.append(f"{pid} {' '.join(argv) if argv else '[unknown]'}")
    return entries


def cleanup_rootless_prefix_processes(
    prefix: Path,
    *,
    proc_root: Path = Path("/proc"),
    current_pid: int | None = None,
    kill_func=os.kill,
    sleep_func=time.sleep,
) -> RootlessPrefixCleanupResult:
    """Terminate only rootless descendants carrying this prefix's env token."""

    result = RootlessPrefixCleanupResult()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        entries = rootless_prefix_process_snapshot(
            prefix, proc_root=proc_root, current_pid=current_pid
        )
        pids = [int(entry.split(" ", 1)[0]) for entry in entries]
        if not pids:
            return result
        for pid in pids:
            try:
                kill_func(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                result.problems.append(
                    f"cannot stop rootless Darling prefix process {pid}: {error}"
                )
        result.changed.append(
            f"sent {signal.Signals(sig).name} to rootless Darling prefix process(es): "
            f"{', '.join(str(pid) for pid in pids)}"
        )
        sleep_func(1)
    leftovers = rootless_prefix_process_snapshot(
        prefix, proc_root=proc_root, current_pid=current_pid
    )
    if leftovers:
        result.problems.extend(f"rootless Darling prefix process survived: {entry}" for entry in leftovers)
    return result


def prefix_process_snapshot(prefix: Path, entries: Iterable[ProcessEntry]) -> list[str]:
    """Return the darlingserver process tree rooted at ``prefix``.

    ``darlingserver`` is launched with the prefix as argv[1].  Children do not
    carry that argv, so snapshot discovery first finds matching server roots and
    then walks the process parent graph.
    """

    prefix_text = str(prefix)
    children: dict[int, list[int]] = {}
    args_by_pid: dict[int, str] = {}
    roots: list[int] = []
    for pid, ppid, args in entries:
        args_by_pid[pid] = args
        children.setdefault(ppid, []).append(pid)
        argv = args.split()
        if len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and argv[1] == prefix_text:
            roots.append(pid)
    if not roots:
        return []
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return [f"{pid} {args_by_pid[pid]}" for pid in sorted(seen) if pid in args_by_pid]


def darlingserver_pids_for_prefix(prefix: Path, entries: Iterable[ProcessEntry]) -> list[int]:
    prefix_text = str(prefix)
    pids: list[int] = []
    for pid, _, args in entries:
        argv = args.split()
        if len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and argv[1] == prefix_text:
            pids.append(pid)
    return pids


def remove_stale_init_pid(
    prefix: Path,
    *,
    pid_is_usable: Callable[[int], bool],
) -> bool:
    """Remove an unusable ``.init.pid`` file.  Returns true when removed."""

    init_pid = prefix / ".init.pid"
    try:
        text = init_pid.read_text().strip()
    except FileNotFoundError:
        return False
    if not text.isdigit():
        return False
    pid = int(text)
    if pid_is_usable(pid):
        return False
    init_pid.unlink(missing_ok=True)
    return True

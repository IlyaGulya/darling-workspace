"""Darling prefix lifecycle helpers for ``west test``."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

ProcessEntry = tuple[int, int, str]


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

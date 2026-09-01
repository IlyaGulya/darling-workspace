#!/usr/bin/env python3
"""Run Cargo and kill its process group if the provisioning parent dies."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys

PR_SET_PDEATHSIG = 1


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != parent:
        return 125
    termination_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, termination_signals)
    child: subprocess.Popen[bytes] | None = None

    def terminate(_signum: int, _frame: object) -> None:
        if child is None:
            return
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        child = subprocess.Popen(sys.argv[1:], start_new_session=True)
        for signum in termination_signals:
            signal.signal(signum, terminate)
    finally:
        # A parent-death signal pending across Popen is delivered only after
        # child and handlers are both fully published.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    try:
        return child.wait()
    finally:
        if child.poll() is None:
            terminate(signal.SIGKILL, None)
            child.wait()


if __name__ == "__main__":
    raise SystemExit(main())

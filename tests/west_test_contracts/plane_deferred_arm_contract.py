"""Execute the retained saved-delta oracle; log models are not guest-runtime proof."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/run-plane-deferred-arm-contract.sh"
PID = "4242"


def arm(*, pending: int = 0, ordinal: bool = True, pid: str = PID) -> str:
    return (
        "call-deferred " + ("n=1 " if ordinal else "")
        + f"site=setPendingCall-parked tid={pid} number=16 active_call=38 "
        + f"pending={pending} suspended=1\n"
    )


def disarm(*, pid: str = PID, sequence: int = 7) -> str:
    return f"plane-call-disarmed tid={pid} seq={sequence} call=16 continuation=1 running=0\n"


with tempfile.TemporaryDirectory(prefix="plane-saved-delta-") as temporary:
    state = Path(temporary)
    delta = state / "server-delta.log"
    forbidden_prefix = state / "must-not-provision"
    environment = dict(
        os.environ,
        DARLING_RUNTIME_ROOT=str(state / "must-not-resolve-runtime"),
        DPREFIX=str(forbidden_prefix),
        DARLING_PREFIX=str(forbidden_prefix),
        WEST_TEST_PREFIX_LEASE=str(forbidden_prefix),
        WEST_TEST_PREFIX_LEASE_FD="-1",
    )

    def check(name: str, payload: str, expected: int, *, expect: str = "green",
              oracle: str = "product") -> str:
        delta.write_text(payload)
        before = delta.read_bytes()
        result = subprocess.run(
            ["bash", str(RUNNER), "--analyse", str(delta), "--pid", PID,
             "--expect", expect, "--oracle", oracle],
            env=environment, text=True, capture_output=True, timeout=15,
        )
        assert result.returncode == expected, (
            name, result.returncode, expected, result.stdout, result.stderr,
        )
        assert delta.read_bytes() == before, name
        assert not forbidden_prefix.exists(), name
        assert not list(state.glob(".west-prefix-lease-*")), name
        assert "prerequisite darling-command-line-tools" not in result.stderr, name
        return result.stdout + result.stderr

    check("current bounded-ordinal GREEN", arm() + disarm(), 0)
    check("historical unordinal saved GREEN", arm(ordinal=False) + disarm(), 0)
    check("absent engagement is INVALID", arm(), 2)
    check("another PID cannot establish engagement", arm() + disarm(pid="42420"), 2)
    check("another PID cannot establish arming", arm(pid="42420") + disarm(), 1)
    check("retained refused slot remains failure", arm() + arm(pending=1) + disarm(), 1)
    check("refused sequence must not execute", arm() + disarm()
          + "plane-deferred-completion tid=4242 seq=7 call=16\n", 1)
    check("completion without an owner remains failure", arm() + disarm()
          + "plane-call-unowned tid=4242 call=16 status=0\n", 1)

    red = (arm() + arm(pending=1)
           + "setPendingCall rejected call (number 16)\n"
           + "call-deferred n=2 site=doWork-parked tid=4242 number=16 active_call=38\n")
    check("bounded-ordinal defective signature", red, 0, expect="red", oracle="existing")
    check("incomplete defective signature", arm(), 1, expect="red", oracle="existing")
    check("GREEN requires product observability", arm() + disarm(), 2, oracle="existing")

print("PASS plane-deferred-arm-saved-delta-contract (log models only; no guest coverage)")

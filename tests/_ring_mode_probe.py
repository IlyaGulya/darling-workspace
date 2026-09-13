#!/usr/bin/env python3
"""One-off matched transport observation; West owns the prefix lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace / "west_commands"))
from test_guest_execution import run_guest_shell_argv

prefix = Path(os.environ["DPREFIX"])
mode = os.environ["RING_EXPECTED_MODE"]
if mode not in {"ON", "OFF"}:
    raise SystemExit("invalid expected ring mode")
result = run_guest_shell_argv(
    os.environ["DARLING_LAUNCHER"], prefix,
    ("/usr/libexec/rack_region_generation",),
    cwd=workspace, env=dict(os.environ), timeout_seconds=60,
)
if result.returncode != 0:
    raise SystemExit(result.returncode)
snapshot = json.loads(subprocess.check_output(
    [sys.executable, os.environ["RING_STAT_TOOL"], str(prefix)],
    text=True, timeout=10,
))
per_call = snapshot.get("per_call", {})
attach = per_call.get("dserver_callnum_ring_attach", {}).get("count", 0)
host_calls = per_call.get("dserver_callnum_host_self_trap", {}).get("count", 0)
ring_keys = sorted(key for key in snapshot if key.startswith("ring_"))
serviced = snapshot.get("ring_serviced", 0)
if host_calls <= 0:
    raise SystemExit("no observed host-self RPC execution")
if mode == "ON" and not (attach > 0 and serviced > 0):
    raise SystemExit("ON runtime did not attach and service ring requests")
if mode == "OFF" and (ring_keys or attach != 0):
    raise SystemExit("OFF runtime exposed ring transport activity or counters")
proof = {
    "mode": mode, "prefix": str(prefix), "host_self_calls": host_calls,
    "ring_attach": attach, "ring_serviced": serviced, "ring_keys": ring_keys,
    "server_sha256": hashlib.sha256((prefix / "bin/darlingserver").read_bytes()).hexdigest(),
}
print("RING_MODE_PROOF " + json.dumps(proof, sort_keys=True))
print("RING_MODE_PROOF_OK mode=" + mode)

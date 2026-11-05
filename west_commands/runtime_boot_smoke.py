"""Exercise a selected normal installation, a real guest shell and clean product shutdown."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from runtime_retention import leased_runtime

MARKER = "SELFCHECK-BOOT-OK"


def run_boot_smoke(
    topdir: Path, prefix: Path, wait_seconds: int = 45, *, runtime_root: Path | None = None,
) -> tuple[bool, str]:
    """Keep complete durable evidence; artifact integrity alone is never guest readiness."""
    with leased_runtime(runtime_root) as runtime:
        return _run_boot_smoke(topdir, prefix, wait_seconds, runtime=runtime)


def _run_boot_smoke(topdir: Path, prefix: Path, wait_seconds: int, *, runtime) -> tuple[bool, str]:
    environment = runtime.environment(prefix)
    topdir = Path(topdir)
    script = next((candidate for candidate in (
        topdir / "scripts/darling-boot-run.sh",
        topdir / "darling-workspace/scripts/darling-boot-run.sh",
    ) if candidate.is_file()), None)
    if script is None:
        return False, f"no darling-boot-run.sh under {topdir}"
    state = Path(os.environ.get("DW_STATE_ROOT", str(topdir / ".west-state")))
    evidence_root = state / "boot-smoke"
    evidence_root.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="run-", dir=evidence_root))
    log_path = evidence / "guest.log"
    bundle = evidence / "bundle"
    command = [
        str(script), "--runtime-root", str(runtime.root), "--prefix", str(prefix),
        "--wait", str(wait_seconds), "--marker", MARKER, "--poll", MARKER,
        "--cmd", f"test -d /Volumes/SystemRoot && test -d /private/var/tmp && printf '%s\\n' '{MARKER}'",
        "--log", str(log_path), "--bundle", str(bundle),
    ]
    (evidence / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    timed_out = False
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True, check=False,
            timeout=wait_seconds + 240, start_new_session=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        returncode = result.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        returncode = None
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        output = (stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout)
        output += (stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr)
        # Safety after observer failure is not successful smoke execution.
        try:
            rescue = subprocess.run(
                [str(runtime.launcher), "shutdown"], env=environment,
                capture_output=True, text=True, check=False, timeout=60,
            )
            (evidence / "timeout-shutdown.txt").write_text(
                f"returncode={rescue.returncode}\n{rescue.stdout}{rescue.stderr}", encoding="utf-8",
            )
        except (OSError, subprocess.TimeoutExpired) as shutdown_error:
            (evidence / "timeout-shutdown.txt").write_text(str(shutdown_error), encoding="utf-8")
    (evidence / "harness.txt").write_text(output, encoding="utf-8")
    try:
        guest_log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        guest_log = ""
    try:
        cleanup = json.loads((bundle / "cleanup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cleanup = None
    clean = isinstance(cleanup, dict) and all(
        cleanup.get(key) == expected for key, expected in (
            ("state", "clean"), ("processes", 0), ("mounts", 0), ("shutdown_rc", 0),
        )
    )
    complete = (
        not timed_out and returncode == 0 and "VERDICT: PASS" in output
        and MARKER in guest_log.splitlines() and clean
    )
    (evidence / "result.json").write_text(json.dumps({
        "passed": complete, "returncode": returncode, "timed_out": timed_out,
        "runtime_root": str(runtime.root), "launcher": str(runtime.launcher),
        "prefix": str(prefix), "cleanup": cleanup,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if complete:
        return True, f"guest shell completed through {runtime.launcher}; product shutdown left zero processes/mounts; evidence: {evidence}"
    interesting = [line for line in (output + "\n" + guest_log).splitlines() if any(
        token in line for token in (
            "shellspawn", "VERDICT", "Rootless", "cleanup", "shutdown", "sigexc",
            "sigexit", "sig=", "SIG", "trap", "panic", "abort", "Assertion",
        )
    )]
    reason = "boot smoke timed out" if timed_out else "guest oracle or clean product shutdown was not completed"
    return False, f"{reason}\n" + "\n".join(interesting[-6:]) + f"\nevidence: {evidence}"

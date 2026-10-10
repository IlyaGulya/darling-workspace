"""Start a Darling prefix and require a guest shell.

One implementation, two callers: the doctor's runtime scope and the deploy path. Both need the one
property no artifact comparison can see -- that the deployed chain boots. MEASURED: a targeted deploy
matched every receipt digest and still never reached a shell ("Rootless shellspawn did not become
ready within 30000ms"), and that failure was mistaken for a fixture problem for hours.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

MARKER = "SELFCHECK-BOOT-OK"


def run_boot_smoke(topdir, prefix, wait_seconds: int = 45) -> tuple[bool, str]:
    """Run the boot harness with one trivial guest command.

    Returns ``(ok, detail)``. ``detail`` quotes the reason the harness stopped, taken from its
    console AND from its run log: the console alone carries only an ownership census, and MEASURED
    that census was the only line a caller could quote.

    The harness runs in its own session because it signals its own process group when a guest run
    fails, which MEASURED killed the caller (the doctor exited 143 with an empty report, and the
    same kill took the strand contract's driver).
    """
    script = Path(topdir) / "scripts" / "darling-boot-run.sh"
    if not script.is_file():
        return False, f"no {script}"
    with tempfile.TemporaryDirectory(prefix="boot-smoke-") as tmp:
        log_path = Path(tmp) / "run.log"
        try:
            result = subprocess.run(
                [
                    str(script),
                    "--prefix",
                    str(prefix),
                    "--wait",
                    str(wait_seconds),
                    "--marker",
                    MARKER,
                    "--cmd",
                    f"/bin/sh -c 'echo {MARKER}'",
                    "--log",
                    str(log_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=wait_seconds + 240,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return False, "the boot smoke did not finish"
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "VERDICT: PASS" in output and MARKER in output:
            return True, "the prefix reached a guest shell"
        lines = [line for line in output.splitlines() if line.strip()]
        if log_path.is_file():
            try:
                lines.extend(
                    line
                    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip()
                )
            except OSError:
                pass
        interesting = [
            line
            for line in lines
            if "shellspawn" in line or "VERDICT" in line or "Rootless" in line
        ]
        return False, "\n".join((interesting or lines)[-4:])

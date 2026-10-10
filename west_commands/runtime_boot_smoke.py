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
    script = None
    for candidate in (
        Path(topdir) / "scripts" / "darling-boot-run.sh",
        Path(topdir) / "darling-workspace" / "scripts" / "darling-boot-run.sh",
    ):
        # Callers pass the west topdir (the workspace root) or the manifest checkout; both are seen
        # in this workspace, and MEASURED a mismatch made the check fail in 0.22s with "no ...script"
        # while the prefix was healthy.
        if candidate.is_file():
            script = candidate
            break
    if script is None:
        return False, f"no darling-boot-run.sh under {topdir}"
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
                    # The harness judges by its poll line, not by --marker: without --poll it waits
                    # for another test's pattern (RING_MACH_TEST mode=.*pass=) and reports FAIL after
                    # the full wait, which MEASURED cost 45s and looked like an unbootable chain.
                    "--poll",
                    MARKER,
                    "--cmd",
                    # A guest shell BUILTIN, not a program: MEASURED, a minimal bootstrap prefix has
                    # no /bin/echo and no /bin/sh, so "/bin/sh -c 'echo …'" and "/bin/echo …" both
                    # failed with "No such file or directory" and the harness reported FAIL after its
                    # full wait, which looked exactly like an unbootable chain. The builtin makes the
                    # healthy run cost ~14s instead of a full timeout.
                    f"echo {MARKER}",
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

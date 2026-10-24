#!/usr/bin/env python3
"""Host contract for the real guest helper's license/setup state transitions.

The controlled executable models Xcode's external license state; it does not
claim Apple runtime coverage. The real helper decides whether a workload may
start and preserves the separate preparation diagnostics.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "testkit/scripts/xcode-license.sh"
APP = r'''
import os
from pathlib import Path
import sys

state = Path(os.environ["LICENSE_TEST_STATE"])
mode = os.environ.get("LICENSE_TEST_MODE", "normal")
if sys.argv[1:] == ["-license", "check"]:
    if state.exists():
        sys.exit(0)
    print("You have not agreed to the Xcode license agreements.", file=sys.stderr)
    sys.exit(69)
if sys.argv[1:] == ["-license", "accept"]:
    if os.environ.get("LICENSE_TEST_GUEST_ROOT") != "1":
        print("Agreeing to the Xcode and SDK licenses requires admin privileges.", file=sys.stderr)
        sys.exit(69)
    if mode == "refuse":
        print("license acceptance could not be committed", file=sys.stderr)
        sys.exit(77)
    if mode != "volatile":
        state.write_text("accepted\n")
    sys.exit(0)
print("unsupported operation", file=sys.stderr)
sys.exit(2)
'''
GUEST_SUDO = r'''
import os
import sys

if sys.argv[1:2] != ["-n"] or len(sys.argv) < 3:
    sys.exit(2)
os.environ["LICENSE_TEST_GUEST_ROOT"] = "1"
os.execv(sys.argv[2], sys.argv[2:])
'''
CALLER = r'''
. "$1"
if darling_xcode_license_prepare "$2" "$3" "$4"; then
    printf 'workload started\n' > "$5"
else
    exit "$?"
fi
'''


class XcodeLicenseContract(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="xcode-license-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "Selected Xcode.app" / "xcodebuild"
        self.app.parent.mkdir()
        self.app.write_text(f"#!{sys.executable}\n" + APP)
        self.app.chmod(0o755)
        self.guest_sudo = self.root / "guest sudo"
        self.guest_sudo.write_text(f"#!{sys.executable}\n" + GUEST_SUDO)
        self.guest_sudo.chmod(0o755)
        self.state = self.root / "backend-license-state"
        self.evidence = self.root / "phase evidence"
        self.workload = self.root / "workload-started"

    def prepare(self, mode: str = "normal") -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.pop("LICENSE_TEST_GUEST_ROOT", None)
        environment.update(LICENSE_TEST_STATE=str(self.state), LICENSE_TEST_MODE=mode)
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", CALLER, "license-contract",
             str(HELPER), str(self.app), str(self.guest_sudo),
             str(self.evidence), str(self.workload)],
            env=environment, capture_output=True, text=True, timeout=10,
        )

    def test_missing_license_is_accepted_verified_and_workload_can_start(self) -> None:
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.workload.read_text(), "workload started\n")
        self.assertEqual(self.state.read_text(), "accepted\n")
        self.assertIn("XCODE_LICENSE_READY=1", result.stdout)
        self.assertIn("check_after_rc=0", (self.evidence / "status.txt").read_text())
        self.assertNotIn("You have not agreed", result.stdout + result.stderr)
        self.assertIn("You have not agreed", (self.evidence / "check-before.log").read_text())

    def test_existing_acceptance_does_not_require_a_new_write(self) -> None:
        self.state.write_text("already accepted\n")
        result = self.prepare("refuse")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.workload.read_text(), "workload started\n")
        self.assertEqual(self.state.read_text(), "already accepted\n")
        self.assertIn("action=already-accepted", result.stdout)

    def test_failed_acceptance_prevents_workload_and_preserves_error(self) -> None:
        result = self.prepare("refuse")
        self.assertEqual(result.returncode, 77)
        self.assertFalse(self.workload.exists())
        self.assertFalse(self.state.exists())
        self.assertNotIn("XCODE_LICENSE_READY=1", result.stdout)
        self.assertIn("license acceptance could not be committed", result.stderr)
        self.assertIn("accept_rc=77", (self.evidence / "status.txt").read_text())

    def test_zero_accept_status_without_persistent_acceptance_is_not_ready(self) -> None:
        result = self.prepare("volatile")
        self.assertEqual(result.returncode, 69)
        self.assertFalse(self.workload.exists())
        self.assertNotIn("XCODE_LICENSE_READY=1", result.stdout)
        self.assertIn("action=verification-failed", result.stderr)
        self.assertIn("check_after_rc=69", (self.evidence / "status.txt").read_text())



if __name__ == "__main__":
    unittest.main()

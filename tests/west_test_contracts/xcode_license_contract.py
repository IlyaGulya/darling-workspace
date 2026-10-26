#!/usr/bin/env python3
"""Host contracts for Xcode guest privilege selection and license/setup transitions.

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
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

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



class XcodeGuestInstallerContract(unittest.TestCase):
    """Exercise provisioner decisions, not the real Apple package or guest runtime."""

    def setUp(self) -> None:
        # The focused host runner does not require West to be installed.
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        commands = ModuleType("west.commands")
        commands.WestCommand = object
        with patch.dict(sys.modules, {"west": ModuleType("west"), "west.commands": commands}):
            from west_commands import xcode_system_resources
        self.resources = xcode_system_resources
        self.temporary = tempfile.TemporaryDirectory(prefix="xcode-installer-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "guest-state"
        self.prefix.mkdir()
        self.runtime = SimpleNamespace(
            root=self.root / "installed runtime",
            launcher=self.root / "installed runtime/bin/darling",
            system_root=self.root / "installed runtime/libexec/darling",
            environment=Mock(return_value={"DPREFIX": str(self.prefix)}),
        )
        self.sudo = self.runtime.system_root / "usr/bin/sudo"
        self.sudo.parent.mkdir(parents=True)
        self.sudo.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
        self.sudo.chmod(0o755)
        self.runner = Mock(return_value=SimpleNamespace(
            returncode=0, timed_out=False, stdout="installed", stderr="",
        ))

    def install(self):
        return self.resources._run_guest_installer(
            runtime=self.runtime, prefix=self.prefix,
            guest_package="/private/var/tmp/authenticated package.pkg",
            guest_runner=self.runner, log=lambda _: None,
        )

    def test_installed_guest_sudo_runs_real_installer_exactly_once(self) -> None:
        # A writable-prefix sudo must not shadow the selected product's helper.
        shadow = self.prefix / "usr/bin/sudo"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("not the selected helper")
        receipt = self.install()
        self.runner.assert_called_once()
        args, kwargs = self.runner.call_args
        self.assertEqual(args[:2], (str(self.runtime.launcher), self.prefix))
        self.assertEqual(args[2], (
            f"/Volumes/SystemRoot{self.sudo}", "-n", "/usr/bin/installer",
            "-pkg", "/private/var/tmp/authenticated package.pkg", "-target", "/",
        ))
        self.assertEqual(kwargs["env"], {"DPREFIX": str(self.prefix)})
        self.assertEqual(len(receipt["attempts"]), 1)
        self.assertEqual(receipt["attempts"][0]["command"], list(args[2]))
        self.assertEqual(receipt["attempts"][0]["output_tail"], "installed")

    def test_missing_or_host_sudo_is_rejected_without_installer_attempt(self) -> None:
        shadow = self.prefix / "usr/bin/sudo"
        shadow.parent.mkdir(parents=True)
        shadow.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
        shadow.chmod(0o755)
        for contents, mode in ((None, 0), (b"\x7fELF", 0o755),
                               (b"#!/bin/sh\n", 0o755), (b"\xcf\xfa\xed\xfe", 0o644)):
            with self.subTest(contents=contents, mode=mode):
                self.sudo.unlink(missing_ok=True)
                if contents is not None:
                    self.sudo.write_bytes(contents)
                    self.sudo.chmod(mode)
                with self.assertRaises(self.resources.XcodeSystemResourcesError) as caught:
                    self.install()
                self.assertEqual(caught.exception.kind, "setup")
                self.assertIn("guest sudo", str(caught.exception))
                self.runner.assert_not_called()

    def test_first_installer_failure_and_timeout_are_final(self) -> None:
        for message, timed_out in (
            ("package requires a newer operating system", False),
            ("package authentication failed", False),
            ("dyld: Library not loaded", False),
            ("installer timed out", True),
        ):
            with self.subTest(message=message):
                self.runner.reset_mock()
                self.runner.return_value = SimpleNamespace(
                    returncode=71, timed_out=timed_out,
                    stdout=b"initial installer output\n", stderr=message.encode(),
                )
                with self.assertRaises(self.resources.XcodeSystemResourcesError) as caught:
                    self.install()
                self.assertEqual(caught.exception.kind, "install")
                self.assertIn("initial installer output", str(caught.exception))
                self.assertIn(message, str(caught.exception))
                self.assertIn("rc=71", str(caught.exception))
                self.assertIn(f"timed_out={timed_out}", str(caught.exception))
                self.runner.assert_called_once()

    def test_runtime_rejection_precedes_guest_execution(self) -> None:
        self.runtime.environment.side_effect = self.resources.RuntimeInstallationError("rejected prefix")
        with self.assertRaises(self.resources.RuntimeInstallationError):
            self.install()
        self.runner.assert_not_called()

    def test_package_authentication_failure_never_installs_or_writes_receipt(self) -> None:
        package = self.root / "unreviewed.pkg"
        package.write_bytes(b"unreviewed")
        for staged in (False, True):
            with self.subTest(staged=staged):
                failure = self.resources.XcodeSystemResourcesError("authentication failed", kind="integrity")
                validator = Mock(side_effect=(
                    [{"path": str(package), "size": 10, "toc_sha1": "pin", "sha256": "pin"}, failure]
                    if staged else failure
                ))
                with patch.object(self.resources, "validate_package", validator), \
                     patch.object(self.resources, "DEMANDED_FRAMEWORKS", ()):
                    with self.assertRaises(self.resources.XcodeSystemResourcesError) as caught:
                        self.resources.provision(
                            prefix=self.prefix, runtime=self.runtime, package=package,
                            guest_runner=self.runner, log=lambda _: None,
                        )
                self.assertEqual(caught.exception.kind, "integrity")
                self.runner.assert_not_called()
                self.assertFalse((self.prefix / self.resources.RECEIPT_NAME).exists())
                self.assertEqual(list(self.prefix.glob("private/var/tmp/*.pkg")), [])


if __name__ == "__main__":
    unittest.main()

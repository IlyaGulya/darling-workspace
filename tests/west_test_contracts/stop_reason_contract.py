"""Host CLI contracts for status precedence and raw/indexed stop evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from west_commands import evidence_storage as storage

CLI = ROOT / "scripts/darling-stop-reason.sh"
SCRATCH = ROOT / ".west-state/supervisor-tooling-20261025"
PADDING = "ordinary diagnostic output\n" * 8192


class StopReasonContract(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="stop-reason-cli.", dir=SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def payload(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def classify(self, log: Path, *arguments: str) -> dict:
        command = [str(CLI), "--log", str(log), *arguments]
        structured = subprocess.run([*command, "--json"], text=True, capture_output=True, check=False)
        self.assertIn(structured.returncode, (0, 3), structured.stderr)
        result = json.loads(structured.stdout)
        self.assertEqual(structured.returncode, 3 if result["kind"] == "none" else 0)
        text = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(text.returncode, structured.returncode, text.stderr)
        stop = next(line for line in text.stdout.splitlines() if line.startswith("STOP-REASON "))
        verdict = next(line for line in text.stdout.splitlines() if line.startswith("PRODUCT-VERDICT "))
        run = next(line for line in text.stdout.splitlines() if line.startswith("RUN-RECORD "))
        for key in ("kind", "guest_rc", "image", "signal"):
            if key in result:
                self.assertIn(f"{key}={result[key]}", stop)
        for text_key, key in (("PRODUCT_VERDICT", "product_verdict"), ("HARNESS_RC", "harness_rc"), ("GUEST_XCB_RC", "guest_xcb_rc"), ("RUNNER_RC", "runner_rc"), ("oracle", "oracle"), ("stop", "kind")):
            self.assertIn(f"{text_key}={result[key]}", verdict)
        for key in ("capture_count", "excluded"):
            self.assertIn(f"{key}={result[key]}", run)
        self.assertEqual(bool(result["warnings"]), "RUN-RECORD-WARNING " in text.stdout)
        if "context_kind" in result:
            context = next(line for line in text.stdout.splitlines() if line.startswith("STOP-CONTEXT "))
            for key in ("kind", "code", "path"):
                self.assertIn(f"{key}={result['context_' + key]}", context)
        return result

    def archive(self, path: Path) -> None:
        report = storage.compress_file(path, finalized=True)
        self.assertEqual(report["action"], "compressed", report)
        self.assertFalse(path.exists())

    def test_failure_statuses_dominate_generic_completion_oracles(self) -> None:
        log = self.payload("failed.log", "CONSUMER_VERDICT done\nCONSUMER_RC=1\n")
        # The caller declares which arbitrary workload status it owns via --rc;
        # the tool must not guess status semantics from every *_RC message name.
        for arguments in (("--rc", "1"), ("--runner-rc", "1"), ("--rc", "0", "--runner-rc", "1")):
            with self.subTest(arguments=arguments):
                result = self.classify(log, "--oracle", "CONSUMER_VERDICT", *arguments)
                self.assertEqual(result["product_verdict"], "FAIL")
                self.assertEqual(result["oracle"], "1")
                self.assertEqual(result["kind"], "none")
        unknown = self.classify(log)
        self.assertEqual(unknown["product_verdict"], "INCONCLUSIVE")
        self.assertEqual(unknown["guest_xcb_rc"], "absent")

    def test_failed_guest_beats_swallowed_harness_zero_and_later_zero(self) -> None:
        log = self.payload("guest.log", "IOCI_PROBE_DONE=1\nXCB_RC=0\nGUEST_XCB_RC=132\nXCB_RC=0\n")
        result = self.classify(log, "--rc", "0", "--runner-rc", "0", "--oracle", "IOCI_PROBE_DONE=1")
        self.assertEqual(result["product_verdict"], "FAIL")
        self.assertEqual(result["guest_xcb_rc"], "132")
        self.assertEqual(result["kind"], "guest-nonzero-exit")
        self.assertEqual(result["signal"], "SIGILL")

    def test_success_and_missing_status_remain_distinct(self) -> None:
        completed = self.payload("completed.log", "BUILD_OK=1\n")
        for arguments, verdict in (((), "INCONCLUSIVE"), (("--runner-rc", "0"), "INCONCLUSIVE"), (("--rc", "0", "--runner-rc", "0"), "PASS"), (("--oracle", "BUILD_OK=1"), "PASS"), (("--oracle", "not-present"), "INCONCLUSIVE"), (("--oracle", ""), "INCONCLUSIVE")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.classify(completed, *arguments)["product_verdict"], verdict)
        guest_success = self.payload("success.log", "GUEST_XCB_RC=0\n")
        self.assertEqual(self.classify(guest_success, "--rc", "0")["product_verdict"], "PASS")
        self.assertEqual(self.classify(guest_success, "--rc", "1")["product_verdict"], "FAIL")
        self.assertEqual(self.classify(guest_success, "--runner-rc", "1")["product_verdict"], "FAIL")
        absent = self.classify(self.root / "missing.log", "--rc", "0")
        self.assertEqual(absent["product_verdict"], "INCONCLUSIVE")
        self.assertTrue(absent["warnings"])

    def test_explicit_guest_status_does_not_require_legacy_status_or_oracle(self) -> None:
        log = self.payload("compile.log", "COMPILE_RC=0\n")
        for arguments, verdict in (
            (("--rc", "0"), "PASS"),
            (("--runner-rc", "0"), "INCONCLUSIVE"),
            (("--rc", "1"), "FAIL"),
            (("--rc", "0", "--runner-rc", "1"), "FAIL"),
        ):
            with self.subTest(arguments=arguments):
                result = self.classify(log, *arguments)
                self.assertEqual(result["product_verdict"], verdict)
                self.assertEqual(result["guest_xcb_rc"], "absent")
                self.assertEqual(result["oracle"], "0")

        sqlite = self.payload(
            "compile-sqlite.log",
            "error opening!: 14\n"
            "Could not create database queue for path /private/var/db/launchservices.db\n"
            "COMPILE_RC=0\n",
        )
        for arguments, verdict in (
            (("--rc", "0"), "PASS"),
            (("--rc", "0", "--runner-rc", "1"), "FAIL"),
        ):
            with self.subTest(arguments=arguments):
                result = self.classify(sqlite, *arguments)
                self.assertEqual(result["product_verdict"], verdict)
                self.assertEqual(result["kind"], "none")
                self.assertEqual(result["context_kind"], "sqlite-open-failed")
                self.assertEqual(result["context_code"], "14")
                self.assertEqual(result["context_path"], "/private/var/db/launchservices.db")

    def test_loader_cause_does_not_become_success_from_guest_zero(self) -> None:
        log = self.payload("loader.log", "Library not loaded: /System/Library/Foo\nGUEST_XCB_RC=0\n")
        result = self.classify(log, "--rc", "0")
        self.assertEqual(result["kind"], "missing-image")
        self.assertEqual(result["product_verdict"], "FAIL")

    def test_completed_zero_keeps_sqlite_warning_as_context(self) -> None:
        log = self.payload(
            "sqlite-warning.log",
            "error opening!: 14\n"
            "Could not create database queue for path /private/var/db/launchservices.db\n"
            "GUEST_XCB_RC=0\n",
        )
        for arguments, verdict in ((("--rc", "0"), "PASS"), (("--rc", "1"), "FAIL"), (("--runner-rc", "1"), "FAIL")):
            with self.subTest(arguments=arguments):
                result = self.classify(log, *arguments)
                self.assertEqual(result["product_verdict"], verdict)
                self.assertEqual(result["kind"], "none")
                self.assertEqual(result["context_kind"], "sqlite-open-failed")
                self.assertEqual(result["context_code"], "14")
                self.assertEqual(result["context_path"], "/private/var/db/launchservices.db")
                self.assertNotIn("errno", result)

        # An explicit fatal gate is not downgraded by a successful wrapper.
        fatal = self.payload(
            "sqlite-license.log",
            log.read_text() + "You have not agreed to the Xcode license agreements\n",
        )
        result = self.classify(fatal, "--rc", "0")
        self.assertEqual(result["kind"], "license-required")
        self.assertEqual(result["product_verdict"], "FAIL")

    def test_raw_and_indexed_log_abort_and_capture_match(self) -> None:
        # Test each evidence route independently: a working raw route must not
        # accidentally conceal omission of another archived logical path.
        diagnostic = 'Library not loaded: /System/Library/Foo\n  Referenced from: /product/with"quote\\name\n'
        for route in ("log", "abort", "capture", "top-out"):
            with self.subTest(route=route):
                log = self.payload(f"{route}/run.log", PADDING + "COMPLETED=1\n")
                bundle = self.root / route / "bundle"
                bundle.mkdir()
                name = {"log": log, "abort": bundle / "abort.txt", "capture": bundle / "captures/product.out", "top-out": bundle / "product.out"}[route]
                name.parent.mkdir(parents=True, exist_ok=True)
                name.write_text(PADDING + diagnostic)
                arguments = ("--bundle", str(bundle), "--rc", "132", "--oracle", "COMPLETED=1")
                raw = self.classify(log, *arguments)
                self.archive(name)
                compressed = self.classify(log, *arguments)
                for key in ("kind", "image", "referrer", "product_verdict", "guest_xcb_rc", "signal", "oracle", "captures", "capture_count", "excluded", "warnings"):
                    self.assertEqual(compressed[key], raw[key], (route, key))
                self.assertEqual(compressed["kind"], "missing-image")
                self.assertEqual(compressed["product_verdict"], "FAIL")

    def test_old_archived_capture_stays_excluded(self) -> None:
        log = self.payload("current.log", "BUILD_OK=1\n")
        old = self.payload("bundle/captures/old.out", PADDING + "Library not loaded: /old/image\nGUEST_XCB_RC=132\n")
        current = self.payload("bundle/captures/current.out", PADDING + "current evidence\n")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(current, (1_700_000_001, 1_700_000_001))
        arguments = ("--bundle", str(old.parents[1]), "--run-start", "1700000000", "--oracle", "BUILD_OK=1")
        raw = self.classify(log, *arguments)
        self.archive(old)
        self.archive(current)
        compressed = self.classify(log, *arguments)
        self.assertEqual(compressed, raw)
        self.assertEqual(compressed["kind"], "none")
        self.assertEqual(compressed["product_verdict"], "PASS")
        self.assertEqual(compressed["excluded"], "1")
        self.assertEqual(compressed["capture_count"], "1")
        self.assertNotIn(str(old), compressed["captures"])

    def test_unavailable_indexed_capture_warns_without_false_pass(self) -> None:
        log = self.payload("current.log", "BUILD_OK=1\n")
        capture = self.payload("bundle/captures/product.out", PADDING + "Library not loaded: /needed/image\n")
        self.archive(capture)
        capture.with_name(capture.name + ".zst").unlink()
        result = self.classify(log, "--bundle", str(capture.parents[1]), "--oracle", "BUILD_OK=1")
        self.assertEqual(result["product_verdict"], "INCONCLUSIVE")
        self.assertTrue(result["warnings"])
        self.assertEqual(result["capture_count"], "0")

    def test_help_is_clean_cli_usage_and_describes_status_inputs(self) -> None:
        result = subprocess.run([str(CLI), "--help"], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Options are a public interface; do not pin incidental explanatory prose.
        for option in ("--log", "--bundle", "--rc", "--runner-rc", "--oracle", "--run-start", "--json"):
            self.assertRegex(result.stdout, re.escape(option) + r"\b")
        self.assertFalse(result.stderr)


if __name__ == "__main__":
    unittest.main()

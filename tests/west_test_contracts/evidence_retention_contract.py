#!/usr/bin/env python3
"""Compact evidence remains usable after generated runtime trees are collected."""
from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
from evidence_retention import compact_runtime_unit
from evidence_storage import EvidenceError, stream_payload
from test_runtime_evidence import RuntimeEvidenceStore
from evidence_verify import verify_archive


class RetentionContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="evidence-retention-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.units = self.root / "runtime-units"
        self.archive = self.root / "archive"
        self.units.mkdir()

    def unit(self, tag="one"):
        unit = self.units / f"runtime-evidence-20000101T000000Z-{tag}"
        unit.mkdir()
        (unit / ".unit.json").write_text(json.dumps({"schema": 1, "owner": "runtime-evidence", "state": "retained"}))
        (unit / "manifest.json").write_text(json.dumps({"schema": 1, "status": "failed", "paths": {"source": "source/darling", "build": "build"}, "worktrees": []}))
        diagnostics = unit / "diagnostics"
        diagnostics.mkdir()
        (diagnostics / "server.log").write_bytes(b"fault: caller=worker\n" * 1024)
        produced = unit / "build" / "src" / "server"
        produced.mkdir(parents=True)
        image = bytearray(64)
        image[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", image, 16, 3)
        image.extend(b"runtime debug symbols\n" * 1024)
        (produced / "darlingserver").write_bytes(image)
        (produced / "thread.o").write_bytes(b"generated object" * 1000)
        compiler_logs = unit / "build" / "CMakeFiles"
        compiler_logs.mkdir()
        (compiler_logs / "CMakeError.log").write_bytes(b"compiler failure with invocation\n" * 512)
        (unit / "build" / "core.compiler").write_bytes(b"unique compiler core\x00" * 1024)
        return unit, bytes(image)

    def decoded(self, path):
        with tempfile.TemporaryFile() as output:
            stream_payload(path, output)
            output.seek(0)
            return output.read()

    def test_proof_and_runtime_image_survive_real_gc_but_objects_do_not(self):
        unit, image = self.unit()
        expected_log = (unit / "diagnostics/server.log").read_bytes()
        original_checksums = hashlib.sha256(expected_log).hexdigest() + "  diagnostics/server.log\n"
        (unit / "SHA256SUMS").write_text(original_checksums)
        retained = compact_runtime_unit(unit, self.archive)
        RuntimeEvidenceStore(self.units).gc(max_age_hours=0, keep_last=0, dry_run=False)
        self.assertFalse(unit.exists())
        archive = Path(retained["archive"])
        self.assertEqual(self.decoded(archive / "diagnostics/server.log"), expected_log)
        record = retained["runtime_images"]["src/server/darlingserver"]
        self.assertEqual(self.decoded(self.archive / record["stored_path"]), image)
        self.assertEqual(record["sha256"], hashlib.sha256(image).hexdigest())
        self.assertFalse(any(archive.rglob("thread.o")))
        self.assertTrue((archive / "SHA256SUMS").is_file())
        saved_checksums = retained["retained_files"]["SHA256SUMS"]["stored_path"]
        self.assertEqual((archive / saved_checksums).read_text(), original_checksums)
        verified = verify_archive(self.archive, recursive=True, write_manifest=True)
        self.assertFalse(verified["failed"], verified)
        self.assertTrue((self.archive / "SHA256SUMS").is_file())

    def test_legacy_checksum_follows_compressed_log_but_rejects_wrong_digest(self):
        proof = self.root / "proof"
        proof.mkdir()
        payload = b"historical fault and stack\n" * 1024
        frame = subprocess.run(
            ["/usr/bin/zstd", "-q", "-c"], input=payload, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        ).stdout
        (proof / "server.log.zst").write_bytes(frame)
        digest = hashlib.sha256(payload).hexdigest()
        (proof / "evidence-index.json").write_text(json.dumps({
            "schema": 1, "files": {"server.log": {
                "stored_path": "server.log.zst", "original_bytes": len(payload),
                "stored_bytes": len(frame), "sha256": digest,
                "stored_sha256": hashlib.sha256(frame).hexdigest(),
            }},
        }))
        checksum = proof / "SHA256SUMS"
        checksum.write_text(digest + "  server.log\n")
        verified = verify_archive(proof, recursive=True)
        self.assertFalse(verified["failed"], verified)
        self.assertEqual(verified["checksums"], 1)
        checksum.write_text(digest + "  proof/server.log\n")
        self.assertFalse(verify_archive(proof, recursive=True)["failed"])
        checksum.write_text("0" * 64 + "  server.log\n")
        self.assertTrue(verify_archive(proof, recursive=True)["failed"])

    def test_identical_runtime_images_share_one_retained_object_across_runs(self):
        first, _ = self.unit("first")
        second, _ = self.unit("second")
        one = compact_runtime_unit(first, self.archive)
        two = compact_runtime_unit(second, self.archive)
        path_one = one["runtime_images"]["src/server/darlingserver"]["stored_path"]
        path_two = two["runtime_images"]["src/server/darlingserver"]["stored_path"]
        self.assertEqual(path_one, path_two)
        self.assertEqual(len(list((self.archive / "runtime-images").glob("*.image*"))), 1)

    def test_manifest_failure_automatically_discards_only_generated_build_payloads(self):
        template, image = self.unit("template")
        session = RuntimeEvidenceStore(self.units).start("native failure", {"source-mode": "manifest"})
        shutil.copytree(template / "build", session.build_root)
        retained = session.retain(RuntimeError("installer failed"))
        self.assertFalse((retained / "build").exists())
        manifest = json.loads((retained / "manifest.json").read_text())
        artifacts = json.loads((retained / manifest["paths"]["build-artifacts"]).read_text())
        record = artifacts["runtime_images"]["src/server/darlingserver"]
        self.assertEqual(self.decoded(retained / record["stored_path"]), image)
        for relative in ("CMakeFiles/CMakeError.log", "core.compiler"):
            record = artifacts["compiler_diagnostics"][relative]
            self.assertEqual(self.decoded(retained / record["stored_path"]), (template / "build" / relative).read_bytes())
        self.assertEqual(manifest["failure"]["message"], "installer failed")

    def test_failed_automatic_compaction_retains_full_owned_build(self):
        template, image = self.unit("template")
        session = RuntimeEvidenceStore(self.units).start("native failure", {"source-mode": "manifest"})
        shutil.copytree(template / "build", session.build_root)
        images = session.directory / "runtime-images"
        images.mkdir()
        (images / (hashlib.sha256(image).hexdigest() + ".image")).write_bytes(b"corrupt previous attempt")
        with self.assertRaises(EvidenceError):
            session.retain(RuntimeError("installer failed"))
        retained = session.directory
        self.assertEqual((retained / "build/src/server/darlingserver").read_bytes(), image)
        manifest = json.loads((retained / "manifest.json").read_text())
        self.assertIn("build-compaction-error", manifest)
        self.assertEqual(manifest["failure"]["message"], "installer failed")

    def test_active_and_unowned_units_cannot_be_compacted(self):
        unit, _ = self.unit()
        with (unit / ".lock").open("w") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            with self.assertRaises(EvidenceError):
                compact_runtime_unit(unit, self.archive)
        unowned = self.units / "runtime-evidence-unowned"
        unowned.mkdir()
        with self.assertRaises(EvidenceError):
            compact_runtime_unit(unowned, self.archive)
        self.assertTrue(unit.is_dir())
        self.assertFalse((self.archive / unit.name).exists())

    def test_existing_corrupt_image_prevents_gc_approval(self):
        unit, image = self.unit()
        images = self.archive / "runtime-images"
        images.mkdir(parents=True)
        (images / (hashlib.sha256(image).hexdigest() + ".image")).write_bytes(b"interrupted copy")
        with self.assertRaises(EvidenceError):
            compact_runtime_unit(unit, self.archive)
        self.assertTrue((unit / "build/src/server/darlingserver").is_file())
        self.assertFalse((self.archive / unit.name).exists())

    def test_diagnostic_directory_links_are_not_silently_omitted(self):
        unit, _ = self.unit()
        target = self.root / "unique-capture"
        target.mkdir()
        (target / "trace.out").write_bytes(b"unique fault")
        (unit / "diagnostics/linked").symlink_to(target, target_is_directory=True)
        with self.assertRaises(EvidenceError):
            compact_runtime_unit(unit, self.archive)
        self.assertTrue((target / "trace.out").is_file())
        self.assertFalse((self.archive / unit.name).exists())


if __name__ == "__main__":
    unittest.main()

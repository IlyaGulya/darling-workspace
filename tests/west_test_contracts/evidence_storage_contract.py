"""Real-zstd contracts for finalized evidence and retained-FD finite log capture."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))

from west_commands import evidence_storage as storage
from west_commands.test_runtime_evidence import RuntimeEvidenceStore
from west_commands.test_store import durable_subdir

CLI = ROOT / "scripts/darling-evidence.py"
PAYLOAD = b"unique diagnostic witness 0123456789\n" * 8192
SCRATCH = durable_subdir(ROOT.parent, "test-scratch") / "evidence-storage"


class EvidenceStorageContract(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def payload(self, name: str = "server.log", content: bytes = PAYLOAD) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def archive(self, path: Path, **kwargs: object) -> dict:
        return storage.compress_file(path, finalized=True, **kwargs)

    def cat(self, path: Path, *, succeeds: bool = True) -> bytes:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([sys.executable, str(CLI), "cat", str(path)], stdout=output, stderr=subprocess.PIPE, check=False)
            self.assertEqual(result.returncode == 0, succeeds, result.stderr)
            output.seek(0)
            return output.read()

    def test_roundtrip_index_logical_names_and_attributes(self) -> None:
        path = self.payload()
        path.chmod(0o640)
        os.utime(path, ns=(1_700_000_000_123_456_789, 1_700_000_000_987_654_321))
        before = path.stat()
        report = self.archive(path)
        self.assertEqual(report["action"], "compressed", report)
        self.assertGreaterEqual(report["saved_bytes"], 4096)
        self.assertFalse(path.exists())
        stored = path.with_name(path.name + ".zst")
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o640)
        self.assertEqual(stored.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.cat(path), PAYLOAD)
        self.assertEqual(self.cat(stored), PAYLOAD)
        index = json.loads((self.root / storage.INDEX_NAME).read_text())
        record = index["files"][path.name]
        self.assertEqual(record["stored_path"], stored.name)
        self.assertEqual(record["original_bytes"], len(PAYLOAD))
        self.assertEqual(record["stored_bytes"], stored.stat().st_size)
        self.assertEqual(record["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(record["stored_sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())
        self.assertEqual(storage.payload_metadata(path)["bytes"], len(PAYLOAD))
        storage.verify_stored(stored, record["sha256"], len(PAYLOAD))
        with self.assertRaises(storage.EvidenceError):
            storage.verify_stored(stored, record["sha256"], len(PAYLOAD) - 1)
        self.assertFalse(list(self.root.glob("*.partial")))

    def cli(self, *arguments: object, succeeds: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, "-B", str(CLI), *(str(argument) for argument in arguments)],
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode == 0, succeeds, result.stderr or result.stdout)
        return result

    def test_receipt_coverage_distinguishes_later_files_and_selection_scope(self) -> None:
        self.payload("original.log", b"completed original workload\n")
        initial = json.loads(self.cli("verify", self.root, "--recursive").stdout)
        self.assertFalse(initial["failed"], initial)
        self.assertEqual(initial["manifest_count"], 0)
        self.assertEqual(initial["unsealed_paths"], ["original.log"])

        created = json.loads(self.cli("verify", self.root, "--recursive", "--write-manifest").stdout)
        self.assertEqual(created["created_checksums"], 1)
        self.assertEqual(created["manifest_count"], 1)
        self.assertEqual(created["unsealed_files"], 0)
        receipt = (self.root / "SHA256SUMS").read_bytes()
        self.payload("late/app.log", b"later workload outside the original seal\n")

        shallow = json.loads(self.cli("verify", self.root).stdout)
        self.assertEqual(shallow["coverage_scope"], "top-level")
        self.assertEqual(shallow["unsealed_files"], 0)
        recursive = json.loads(self.cli("verify", self.root, "--recursive").stdout)
        self.assertFalse(recursive["failed"], recursive)
        self.assertEqual(recursive["checksums"], 1)
        self.assertEqual(recursive["coverage_scope"], "recursive")
        self.assertEqual(recursive["unsealed_files"], 1)
        self.assertEqual(recursive["unsealed_paths"], ["late/app.log"])
        self.assertEqual((self.root / "SHA256SUMS").read_bytes(), receipt)

    def test_sealed_index_remains_byte_exact_after_append_and_repeat(self) -> None:
        first = self.payload("payloadA.stdout")
        second_content = PAYLOAD + b"second finalized assembly diagnostic\n"
        self.cli("compress", first)
        self.cli("verify", self.root, "--recursive", "--write-manifest")
        sealed = {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        second = self.payload("payloadB.asm.stdout", second_content)
        self.cli("compress", second)
        result = json.loads(self.cli("verify", self.root, "--recursive").stdout)
        self.assertEqual(result["indexed_payloads"], 2)
        self.assertFalse(result["failed"], result)
        for path, content in sealed.items():
            self.assertEqual(path.read_bytes(), content, path)
        self.assertEqual(self.cat(first), PAYLOAD)
        self.assertEqual(self.cat(second), second_content)
        indexes = {path: path.read_bytes() for path in storage.index_paths(self.root)}
        self.assertEqual(len(indexes), 2)
        self.cli("compress", first, second)
        self.cli("compress", self.root, "--recursive")
        self.cli("verify", self.root, "--recursive")
        self.assertEqual({path: path.read_bytes() for path in storage.index_paths(self.root)}, indexes)
        for path, content in sealed.items():
            self.assertEqual(path.read_bytes(), content, path)
        self.cli("verify", self.root, "--recursive", "--write-manifest", succeeds=False)

    def test_legacy_seal_over_raw_stdout_survives_first_compression(self) -> None:
        path = self.payload("native.asm.stdout")
        self.cli("verify", self.root, "--recursive", "--write-manifest")
        receipt = (self.root / "SHA256SUMS").read_bytes()
        self.cli("compress", self.root, "--recursive")
        self.assertFalse(path.exists())
        self.assertEqual(self.cat(path), PAYLOAD)
        self.assertEqual((self.root / "SHA256SUMS").read_bytes(), receipt)
        result = json.loads(self.cli("verify", self.root, "--recursive").stdout)
        self.assertNotIn(path.name + ".zst", result["unsealed_paths"])

    def test_conflicting_addition_rejects_read_verify_and_publication(self) -> None:
        path = self.payload()
        self.cli("compress", path)
        index = json.loads((self.root / storage.INDEX_NAME).read_text())
        index["files"][path.name]["sha256"] = "0" * 64
        conflict = self.root / "evidence-index.conflict.json"
        conflict.write_text(json.dumps(index))
        self.cat(path, succeeds=False)
        self.cli("verify", self.root, "--recursive", succeeds=False)
        pending = self.payload("pending.stdout")
        self.cli("compress", pending, succeeds=False)
        self.assertEqual(pending.read_bytes(), PAYLOAD)
        self.assertFalse(pending.with_name(pending.name + ".zst").exists())

    def test_stream_reader_validates_stored_and_decoded_integrity(self) -> None:
        path = self.payload()
        self.cli("compress", path)
        with storage.open_payload(path) as reader:
            self.assertEqual(reader.readline(), PAYLOAD.splitlines(keepends=True)[0])
            self.assertEqual(reader.read(), PAYLOAD[len(PAYLOAD.splitlines(keepends=True)[0]):])
        index_path = self.root / storage.INDEX_NAME
        index = json.loads(index_path.read_text())
        index["files"][path.name]["sha256"] = "0" * 64
        index_path.write_text(json.dumps(index))
        self.cat(path, succeeds=False)
        with self.assertRaises(storage.EvidenceError):
            with storage.open_payload(path) as reader:
                reader.read(1)
        corrupt = self.payload("corrupt.stdout.zst", b"not a zstd frame\n")
        self.cat(corrupt, succeeds=False)
        with self.assertRaises(storage.EvidenceError):
            with storage.open_payload(corrupt) as reader:
                reader.read()

    def test_optional_bundle_streams_distinguish_absence_from_lost_indexed_bytes(self) -> None:
        from west_commands.test_guest_c import failure_phase_from_debug_bundle

        stderr = self.payload("stderr.log", b"WEST_GUEST_STAGE=upload\n" + PAYLOAD)
        reference = f"BUNDLE={self.root}\n"
        self.assertEqual(failure_phase_from_debug_bundle(reference), "setup")
        with self.assertRaises(storage.PayloadNotFoundError):
            with storage.open_payload(self.root / "stdout.log"):
                self.fail("absent optional stream unexpectedly opened")
        for name in ("stdout.log", "stdout.log.zst"):
            malformed = self.root / name
            malformed.mkdir()
            with self.assertRaises(storage.EvidenceError) as invalid:
                with storage.open_payload(self.root / "stdout.log"):
                    self.fail("non-regular optional payload unexpectedly opened")
            self.assertNotIsInstance(invalid.exception, storage.PayloadNotFoundError)
            self.assertIsNone(failure_phase_from_debug_bundle(reference))
            malformed.rmdir()
        self.assertEqual(self.archive(stderr)["action"], "compressed")
        self.assertEqual(failure_phase_from_debug_bundle(reference), "setup")

        stderr.with_name(stderr.name + ".zst").unlink()
        with self.assertRaises(storage.EvidenceError) as failure:
            with storage.open_payload(stderr):
                self.fail("lost indexed stream unexpectedly opened")
        self.assertNotIsInstance(failure.exception, storage.PayloadNotFoundError)
        self.assertIsNone(failure_phase_from_debug_bundle(reference))

    def test_bundle_consumers_use_verified_logical_bytes_before_and_after_compression(self) -> None:
        from west_commands.test_diagnostics import verify_exact_capture
        from west_commands.test_guest_c import failure_phase_from_debug_bundle
        from west_commands.test_verdict_cache import bundle_stdout_digest

        stdout_bytes = b"MACHO_EXACT_GUEST_READY\nWEST_GUEST_STAGE=run\n" + PAYLOAD
        stderr_bytes = b"WEST_GUEST_STAGE=run\n" + PAYLOAD
        stdout = self.payload("stdout.log", stdout_bytes)
        stderr = self.payload("stderr.log", stderr_bytes)
        self.payload("exit-status.txt", b"42\n")
        self.payload("timeout.txt", b"expected diagnostic deadline\n")
        exact = self.root / "exact"
        exact.mkdir()
        image = b"\xcf\xfa\xed\xfe" + b"synthetic captured-image parser input\n" + PAYLOAD
        (exact / "sleep.macho").write_bytes(image)
        core = bytearray(128)
        core[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HHI", core, 16, 4, 62, 1)
        struct.pack_into("<Q", core, 32, 64)
        struct.pack_into("<HHH", core, 52, 64, 56, 1)
        struct.pack_into("<IIQQQQQQ", core, 64, 1, 4, 120, 0x1000, 0, 8, 8, 4096)
        core[120:] = b"coredata"
        core.extend(PAYLOAD)
        (exact / "sleep.core").write_bytes(core)
        (exact / "gdb.txt").write_bytes(b"rip = 0x1000\n" + PAYLOAD)
        (exact / "manifest.json").write_text(json.dumps({
            "complete": True,
            "processes": [{
                "pid": 123,
                "images": [{
                    "complete": True,
                    "path": "/bin/sleep",
                    "file": "sleep.macho",
                    "sha256": hashlib.sha256(image).hexdigest(),
                }],
                "core": {"file": "sleep.core"},
            }],
        }))
        reference = f"BUNDLE={self.root}\n"
        expected_digest = hashlib.sha256(stdout_bytes).hexdigest()
        expected_processes = [{"pid": 123, "image": "sleep.macho", "core": "sleep.core"}]
        for compressed in (False, True):
            if compressed:
                for payload in (stdout, stderr, exact / "sleep.macho", exact / "sleep.core", exact / "gdb.txt"):
                    self.assertEqual(self.archive(payload)["action"], "compressed")
                    self.assertFalse(payload.exists())
            self.assertEqual(bundle_stdout_digest(self.root), expected_digest)
            self.assertEqual(failure_phase_from_debug_bundle(reference), "run")
            self.assertEqual(verify_exact_capture(self.root)["guest_processes"], expected_processes)

        # The frame still decodes and contains every positive marker, but the
        # committed logical digest is wrong. No consumer may accept bytes
        # yielded before open_payload performs its context-exit validation.
        index_path = self.root / storage.INDEX_NAME
        index = json.loads(index_path.read_text())
        index["files"][stdout.name]["sha256"] = "0" * 64
        index_path.write_text(json.dumps(index))
        self.assertIsNone(bundle_stdout_digest(self.root))
        self.assertIsNone(failure_phase_from_debug_bundle(reference))
        with self.assertRaises(storage.EvidenceError):
            verify_exact_capture(self.root)
        self.assertIsNone(bundle_stdout_digest(None))

    def test_symlink_addition_is_rejected_without_touching_sealed_bytes(self) -> None:
        path = self.payload()
        self.cli("compress", path)
        self.cli("verify", self.root, "--recursive", "--write-manifest")
        sealed = {entry: entry.read_bytes() for entry in self.root.iterdir() if entry.is_file()}
        (self.root / "evidence-index.unsafe.json").symlink_to(self.root / storage.INDEX_NAME)
        self.cat(path, succeeds=False)
        self.cli("verify", self.root, "--recursive", succeeds=False)
        pending = self.payload("pending.asm.stdout")
        self.cli("compress", pending, succeeds=False)
        self.assertEqual(pending.read_bytes(), PAYLOAD)
        for entry, content in sealed.items():
            self.assertEqual(entry.read_bytes(), content, entry)

    def test_dry_run_reports_real_savings_without_publication(self) -> None:
        path = self.payload()
        result = subprocess.run([sys.executable, str(CLI), "compress", "--dry-run", str(path)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(reports[0]["action"], "would-compress")
        self.assertGreater(reports[-1]["saved_bytes"], 4096)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertFalse(path.with_name(path.name + ".zst").exists())
        self.assertFalse((self.root / storage.INDEX_NAME).exists())
        self.assertFalse(list(self.root.glob(".evidence-*.partial")))

    def test_bulk_censuses_and_index_publication_are_shared(self) -> None:
        paths = [self.payload(f"part-{index}.log") for index in range(4)]
        with patch.object(storage, "_writer_snapshot", wraps=storage._writer_snapshot) as census:
            with patch.object(storage, "_atomic_json", wraps=storage._atomic_json) as publication:
                reports = list(storage.compress_paths(paths))
        self.assertEqual([report["action"] for report in reports], ["compressed"] * len(paths))
        self.assertLessEqual(census.call_count, 3)
        self.assertEqual(publication.call_count, 1)
        for path in paths:
            self.assertEqual(self.cat(path), PAYLOAD)

    def test_incompressible_and_small_inputs_are_retained(self) -> None:
        random = os.urandom(128 * 1024)
        for name, content in (("random.dump", random), ("tiny.log", b"a" * 4096)):
            path = self.payload(name, content)
            report = self.archive(path)
            self.assertEqual(report["action"], "retained", report)
            self.assertEqual(report["saved_bytes"], 0)
            self.assertEqual(path.read_bytes(), content)
            self.assertFalse(path.with_name(path.name + ".zst").exists())

    def test_corrupt_compressor_output_and_command_failure_preserve_original(self) -> None:
        path = self.payload()
        def corrupt(source, destination):
            destination.write(b"not a zstd frame")
        with patch.object(storage, "_compress_zstd", corrupt):
            with self.assertRaises(storage.EvidenceError):
                self.archive(path)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        with patch.object(storage, "ZSTD", "/usr/bin/false"):
            with self.assertRaises(storage.EvidenceError):
                self.archive(path)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertFalse(path.with_name(path.name + ".zst").exists())
        self.assertFalse(list(self.root.glob(".evidence-*.partial")))

    def test_index_failure_and_existing_destination_do_not_remove_original(self) -> None:
        path = self.payload()
        real_atomic = storage._atomic_json
        def fail_index(target, value, **kwargs):
            if target.name == storage.INDEX_NAME:
                raise OSError("injected index fsync/write failure")
            return real_atomic(target, value, **kwargs)
        with patch.object(storage, "_atomic_json", fail_index):
            with self.assertRaises(OSError):
                self.archive(path)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        stored = path.with_name(path.name + ".zst")
        prior_stored = stored.read_bytes()
        with self.assertRaises(storage.EvidenceError):
            self.archive(path)
        self.assertEqual(stored.read_bytes(), prior_stored)
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_unavailable_authoritative_census_preserves_original(self) -> None:
        path = self.payload()
        with patch.object(storage, "_PRIVILEGED_CENSUS", True):
            with patch.object(storage.subprocess, "run", side_effect=FileNotFoundError("sudo unavailable")):
                with self.assertRaises(storage.EvidenceError):
                    self.archive(path)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertFalse(path.with_name(path.name + ".zst").exists())

    def test_open_or_changed_input_is_never_unlinked(self) -> None:
        path = self.payload()
        with path.open("ab"):
            report = self.archive(path)
            self.assertEqual(report["action"], "retained", report)
            self.assertIn("writer", report["reason"])
        verify = storage._verify_zstd
        def change_after_verify(stored, expected_hash, expected_size):
            verify(stored, expected_hash, expected_size)
            with path.open("ab") as writer:
                writer.write(b"late unique bytes\n")
        with patch.object(storage, "_verify_zstd", change_after_verify):
            with self.assertRaises(storage.EvidenceError):
                self.archive(path)
        self.assertEqual(path.read_bytes(), PAYLOAD + b"late unique bytes\n")
        self.assertFalse(path.with_name(path.name + ".zst").exists())

    def test_writer_opened_during_compression_is_detected(self) -> None:
        path = self.payload()
        verify = storage._verify_zstd
        writers = []
        def acquire_writer(stored, expected_hash, expected_size):
            verify(stored, expected_hash, expected_size)
            writers.append(path.open("ab"))
        try:
            with patch.object(storage, "_verify_zstd", acquire_writer):
                with self.assertRaises(storage.EvidenceError):
                    self.archive(path)
        finally:
            for writer in writers:
                writer.close()
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertFalse(path.with_name(path.name + ".zst").exists())

    def test_runtime_layout_is_protected_even_without_a_prefix_shaped_name(self) -> None:
        runtime = self.root / "ordinary-name"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/darling").write_text("launcher")
        (runtime / "libexec/darling").mkdir(parents=True)
        path = self.payload("ordinary-name/private/var/log/server.log")
        report = self.archive(path)
        self.assertEqual(report["action"], "retained")
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertFalse(path.with_name(path.name + ".zst").exists())

    def test_symlinks_source_metadata_and_tracked_recursion_are_untouched(self) -> None:
        path = self.payload("outside.log")
        link = self.root / "symlink.log"
        link.symlink_to(path)
        with self.assertRaises(storage.EvidenceError):
            self.archive(link)
        repo = self.root / "archive"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        tracked = self.payload("archive/tracked.log")
        subprocess.run(["git", "-C", str(self.root), "add", "archive/tracked.log"], check=True)
        untouched = [tracked]
        for name in (
            "source/compile.log", "build/build.log", "prefix/server.log", "cache/cache.log",
            "README.txt", "manifest.json", "proof.sha256", "source.c", "rescue.bundle",
            "command.txt", "env.txt", "pid.txt", "target-pid.txt", "exit-status.txt",
            "cleanup-status.txt", "prefix-cleanup-status.txt", "attach-strace.txt",
            "fatal-targets.txt", "evidence-manifest",
        ):
            untouched.append(self.payload("archive/" + name))
        external = self.root / "elsewhere"
        external.mkdir()
        external_log = self.payload("elsewhere/private.log")
        (repo / "linked-directory").symlink_to(external, target_is_directory=True)
        archived = self.payload("archive/old-server.log")
        executable = self.payload("archive/program.out", b"\x7fELF" + PAYLOAD)
        executable.chmod(0o755)
        untouched.extend((executable, external_log))
        nested_source = repo / "checkout"
        nested_source.mkdir()
        subprocess.run(["git", "init", "-q", str(nested_source)], check=True)
        source_input = self.payload("archive/checkout/source-input.txt")
        untouched.append(source_input)
        with self.assertRaises(storage.EvidenceError):
            list(storage.compress_paths([self.root], recursive=True))
        reports = list(storage.compress_paths([repo], recursive=True))
        self.assertTrue(any(report["action"] == "compressed" for report in reports), reports)
        self.assertFalse(archived.exists())
        for item in untouched:
            self.assertTrue(item.is_file(), item)
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_tracked_migration_requires_opt_in_and_protects_working_and_staged_edits(self) -> None:
        evidence = self.root / "evidence"
        evidence.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        clean = self.payload("evidence/clean.log")
        dirty = self.payload("evidence/dirty.log")
        staged = self.payload("evidence/staged.log")
        metadata = self.payload("evidence/manifest.json")
        subprocess.run(["git", "-C", str(self.root), "add", "evidence"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Evidence contract", "-c", "user.email=evidence@example.invalid", "commit", "-q", "-m", "retained evidence"], check=True)
        dirty.write_bytes(PAYLOAD + b"unique unstaged edit\n")
        staged.write_bytes(PAYLOAD + b"unique staged edit\n")
        subprocess.run(["git", "-C", str(self.root), "add", "evidence/staged.log"], check=True)
        staged.write_bytes(PAYLOAD)
        self.assertEqual(list(storage.compress_paths([evidence], recursive=True)), [])
        result = subprocess.run([sys.executable, str(CLI), "compress", "--recursive", "--include-tracked", str(evidence)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(clean.exists())
        self.assertEqual(self.cat(clean), PAYLOAD)
        self.assertEqual(dirty.read_bytes(), PAYLOAD + b"unique unstaged edit\n")
        self.assertEqual(staged.read_bytes(), PAYLOAD)
        self.assertTrue(metadata.exists())
        self.assertFalse(dirty.with_name(dirty.name + ".zst").exists())
        self.assertFalse(staged.with_name(staged.name + ".zst").exists())

    def test_diagnostic_names_and_explicit_binary_backups(self) -> None:
        for name in ("arm-core.1234", "server.trace_.4321", "stdout.out", "snapshots.txt"):
            path = self.payload(name)
            self.assertEqual(self.archive(path)["action"], "compressed", name)
        binary = self.payload("mldr.backup-before-diagnosis", b"\xcf\xfa\xed\xfe" + PAYLOAD)
        self.assertEqual(self.archive(binary, explicit=False)["action"], "retained")
        self.assertEqual(self.archive(binary)["action"], "compressed")
        arbitrary = self.payload("Makefile", b"all: source.c\n" * 8192)
        self.assertEqual(self.archive(arbitrary)["action"], "retained")

    def test_cat_corruption_fails_instead_of_successful_empty_capture(self) -> None:
        raw = self.payload("raw.log")
        self.assertEqual(self.cat(raw), PAYLOAD)
        corrupt = self.payload("broken.log.zst", b"not zstd")
        self.cat(corrupt, succeeds=False)
        self.archive(raw)
        stored = raw.with_name(raw.name + ".zst")
        stored.write_bytes(b"corrupted after archival")
        with self.assertRaises(storage.EvidenceError):
            storage.payload_metadata(raw)

    def test_runtime_finalizes_only_diagnostics_and_keeps_logical_references(self) -> None:
        artifact = self.payload("retained-image.backup", b"\x7fELF" + PAYLOAD)
        store = RuntimeEvidenceStore(self.root / "units")
        session = store.start("finalized diagnostics", {"provider": "contract"})
        session.source_root.mkdir()
        session.build_root.mkdir()
        (session.source_root / "source.log").write_bytes(PAYLOAD)
        (session.build_root / "build.log").write_bytes(PAYLOAD)
        session.record_failure_detail(phase="execute", summary="unique proof", output=PAYLOAD.decode(), artifacts=[artifact])
        self.assertTrue((session.directory / "diagnostics/0-output.log").exists())
        entry = session.retain(RuntimeError("retain contract failure"))
        manifest = json.loads((entry / "manifest.json").read_text())
        logical = manifest["diagnostics"][0]["output"]
        self.assertEqual(logical, "diagnostics/0-output.log")
        self.assertFalse((entry / logical).exists())
        self.assertTrue((entry / (logical + ".zst")).exists())
        self.assertTrue((entry / "source/source.log").exists())
        self.assertTrue((entry / "build/build.log").exists())
        attachments = store.replay_report(entry.name)["attachments"]
        by_path = {attachment["path"]: attachment for attachment in attachments}
        self.assertEqual(by_path[logical]["bytes"], 64 * 1024)
        self.assertIn("sha256", by_path[logical])
        self.assertEqual(self.cat(entry / logical), PAYLOAD[-64 * 1024:])

    def test_unreferenced_owned_diagnostics_are_finalized_too(self) -> None:
        store = RuntimeEvidenceStore(self.root / "units")
        session = store.start("direct collector diagnostics", {})
        diagnostics = session.directory / "diagnostics"
        diagnostics.mkdir()
        for name in ("darlingserver.log", "prefix-tmp-state.txt"):
            (diagnostics / name).write_bytes(PAYLOAD)
        (diagnostics / "capture.json").write_text('{"captured": true}\n')
        entry = session.retain(SystemExit(1))
        manifest = json.loads((entry / "manifest.json").read_text())
        self.assertNotIn("diagnostics", manifest)
        for name in ("darlingserver.log", "prefix-tmp-state.txt"):
            self.assertFalse((entry / "diagnostics" / name).exists())
            self.assertEqual(self.cat(entry / "diagnostics" / name), PAYLOAD)
        self.assertTrue((entry / "diagnostics/capture.json").exists())


class FiniteLogContract(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "live-server.log"
        self.state = self.root / "server-log-state.json"
        self.output = self.root / "server.log"

    def start(self, content: bytes):
        self.source.write_bytes(content)
        descriptor = os.open(self.source, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        original = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
        storage.log_start(self.source, self.state, original)
        return original

    def test_append_captures_only_finite_tail_not_history(self) -> None:
        original = self.start(b"large old history\n" * 8192)
        before = json.loads(self.state.read_text())["original"]["size"]
        with self.source.open("ab") as writer:
            writer.write(b"new finite witness\n")
        state = storage.log_finish(self.source, self.state, original, self.output)
        self.assertEqual(self.output.read_bytes(), b"new finite witness\n")
        self.assertTrue(state["complete"], state)
        self.assertFalse(state["gap"], state)
        self.assertEqual(state["transition"], "append")
        self.assertEqual(state["captures"][0]["start"], before)
        self.assertEqual(state["captures"][0]["bytes"], len(b"new finite witness\n"))
        self.assertLess(self.state.stat().st_size, 4096)

    def test_capture_does_not_follow_a_concurrently_growing_log(self) -> None:
        original = self.start(b"old history\n")
        with self.source.open("ab") as writer:
            writer.write(b"finite boundary\n")
        copy_slice = storage._copy_slice
        def append_after_snapshot(descriptor, destination, start, end, role):
            with self.source.open("ab") as writer:
                writer.write(b"after finite boundary\n")
            return copy_slice(descriptor, destination, start, end, role)
        with patch.object(storage, "_copy_slice", append_after_snapshot):
            state = storage.log_finish(self.source, self.state, original, self.output)
        self.assertEqual(self.output.read_bytes(), b"finite boundary\n")
        self.assertTrue(state["complete"], state)
        self.assertEqual(state["captures"][0]["end"], len(b"old history\nfinite boundary\n"))

    def test_rotation_keeps_old_fd_tail_and_new_inode_bytes(self) -> None:
        original = self.start(b"old history\n")
        rotated = self.root / "rotated.log"
        self.source.rename(rotated)
        with rotated.open("ab") as writer:
            writer.write(b"late old-inode bytes\n")
        self.source.write_bytes(b"new-inode bytes\n")
        state = storage.log_finish(self.source, self.state, original, self.output)
        self.assertEqual(self.output.read_bytes(), b"late old-inode bytes\nnew-inode bytes\n")
        self.assertTrue(state["complete"], state)
        self.assertEqual(state["transition"], "rotate")
        self.assertEqual([capture["role"] for capture in state["captures"]], ["original", "current"])

    def test_truncate_and_regrowth_capture_from_zero_with_explicit_gap(self) -> None:
        original = self.start(b"old boundary\n" * 1000)
        replacement = b"regrown unique new data\n" * 1000
        self.source.write_bytes(replacement)
        result = subprocess.run([sys.executable, str(CLI), "log-finish", "--source", str(self.source), "--state", str(self.state), "--original", str(original), "--output", str(self.output)], capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        state = json.loads(self.state.read_text())
        self.assertEqual(self.output.read_bytes(), replacement)
        self.assertEqual(state["transition"], "reset")
        self.assertTrue(state["gap"], state)
        self.assertFalse(state["complete"], state)
        self.assertEqual(state["captures"][0]["start"], 0)
        self.assertGreaterEqual(state["captures"][0]["end"], state["captures"][0]["start"])

    def test_simple_shrink_does_not_produce_negative_delta(self) -> None:
        original = self.start(b"old boundary\n" * 1000)
        self.source.write_bytes(b"short new log\n")
        state = storage.log_finish(self.source, self.state, original, self.output)
        self.assertEqual(self.output.read_bytes(), b"short new log\n")
        self.assertTrue(state["gap"])
        self.assertFalse(state["complete"])
        self.assertEqual(state["captures"][0]["bytes"], len(b"short new log\n"))

    def test_source_and_retained_fd_binding_and_no_clobber(self) -> None:
        original = self.start(b"old history\n")
        other = self.root / "other.log"
        other.write_bytes(b"unrelated source\n")
        with self.assertRaises(storage.EvidenceError):
            storage.log_finish(other, self.state, original, self.output)
        with other.open("rb") as descriptor:
            wrong_original = Path(f"/proc/{os.getpid()}/fd/{descriptor.fileno()}")
            with self.assertRaises(storage.EvidenceError):
                storage.log_finish(self.source, self.state, wrong_original, self.output)
        self.output.write_bytes(b"existing unique proof\n")
        with self.assertRaises(storage.EvidenceError):
            storage.log_finish(self.source, self.state, original, self.output)
        self.assertEqual(self.output.read_bytes(), b"existing unique proof\n")
        self.assertEqual(json.loads(self.state.read_text())["transition"], "started")

    def test_missing_source_is_not_silently_complete(self) -> None:
        original = self.start(b"old history\n")
        with self.source.open("ab") as writer:
            writer.write(b"retained tail\n")
        self.source.unlink()
        state = storage.log_finish(self.source, self.state, original, self.output)
        self.assertEqual(self.output.read_bytes(), b"retained tail\n")
        self.assertFalse(state["complete"])
        self.assertTrue(state["gap"])
        self.assertIn("source-at-finish", state["missing"])

    def test_missing_initial_log_can_be_created_without_inventing_history(self) -> None:
        original = Path(f"/proc/{os.getpid()}/fd/999999")
        start = storage.log_start(self.source, self.state, original)
        self.assertIsNone(start["original"])
        self.source.write_bytes(b"first-boot server bytes\n")
        state = storage.log_finish(self.source, self.state, original, self.output)
        self.assertEqual(state["transition"], "created")
        self.assertTrue(state["complete"])
        self.assertFalse(state["gap"])
        self.assertIn("source-at-start", state["missing"])
        self.assertEqual(self.output.read_bytes(), b"first-boot server bytes\n")

    def test_start_refuses_unrelated_fd_and_source_symlink(self) -> None:
        self.source.write_bytes(b"source\n")
        other = self.root / "other.log"
        other.write_bytes(b"unrelated\n")
        with other.open("rb") as descriptor:
            original = Path(f"/proc/{os.getpid()}/fd/{descriptor.fileno()}")
            with self.assertRaises(storage.EvidenceError):
                storage.log_start(self.source, self.state, original)
        self.assertTrue(json.loads(self.state.read_text())["gap"])
        link = self.root / "link.log"
        link.symlink_to(self.source)
        with self.assertRaises(storage.EvidenceError):
            storage.log_start(link, self.root / "link-state.json", original)


if __name__ == "__main__":
    unittest.main()

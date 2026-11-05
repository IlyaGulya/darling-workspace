#!/usr/bin/env python3
"""Finalized managed-run/job evidence survives removal and stays bounded."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
from evidence_retention import (  # noqa: E402
    ack_run_retirement, finalize_job_state, finalize_run_evidence,
    finalize_unstarted_run_evidence, mark_job_state, prune_job_states,
    prune_run_evidence,
)
from evidence_storage import EvidenceError, open_payload  # noqa: E402
from evidence_verify import verify_archive  # noqa: E402


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ManagedRunEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="run-retention-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()

    def lane(self, name: str = "r-lane", *, state: bool = True,
             managed: dict | None = None, runtime: str = "/runtime/test") -> Path:
        lane = self.root / "lane-root" / name
        lane.mkdir(parents=True)
        record = {
            "schema": 2, "source": None,
            "runtime": {"root": runtime, "launcher": runtime + "/bin/bash"},
        }
        if managed is not None:
            record["managed_run"] = managed
        write_json(lane / "lane.json", record)
        for name in ("logs", "cores", "receipts"):
            (lane / name).mkdir()
        if state:
            (lane / "state").mkdir()
        return lane

    def managed_pair(self, tag: str, *, lane_state: bool = True,
                     runtime: str = "/runtime/test"):
        """Create a started managed lane and its matching run directory."""
        run_id = (tag * 32)[:32]
        run = self.runs / ("run-" + run_id)
        run.mkdir()
        (run / ".lock").write_text("")
        info = run.stat()
        lane = self.lane(
            "r" + run_id[:12], state=lane_state, runtime=runtime,
            managed={"id": run_id, "path": str(run)},
        )
        lane_info = lane.stat()
        lane_record = json.loads((lane / "lane.json").read_text())
        lane_record["managed_run"] = {
            "id": run_id, "path": str(run),
            "device": lane_info.st_dev, "inode": lane_info.st_ino,
        }
        write_json(lane / "lane.json", lane_record)
        record = {
            "schema": 1, "kind": "managed-lane-run", "id": run_id,
            "uid": os.geteuid(), "root": str(self.root), "path": str(run),
            "directory": {"device": info.st_dev, "inode": info.st_ino},
            "created": "2026-01-01T00:00:00Z", "phase": "capturing",
            "keep": False, "pin": False, "runtime_root": runtime,
            "argv": ["west", "test", tag], "workload_rc": 7,
            "lane": {"path": str(lane), "name": lane.name, "root": str(lane.parent),
                     "token": run_id,
                     "device": lane_info.st_dev, "inode": lane_info.st_ino},
            "command_started": True,
            "cleanup": {"success": False, "problems": []},
            "evidence": {"complete": False},
        }
        write_json(run / "run.json", record)
        (run / "command.stdout").write_bytes(b"stdout bytes\n")
        (run / "command.stderr").write_bytes(b"stderr bytes\n")
        write_json(run / "invocation.json",
                   {"run_id": run_id, "argv": record["argv"]})
        write_json(run / "outcome.json",
                   {"run_id": run_id, "workload_rc": 7, "command_started": True})
        return lane, run, record

    def unstarted_run(self, tag: str) -> tuple[Path, dict]:
        run_id = (tag * 32)[:32]
        run = self.runs / ("run-" + run_id)
        run.mkdir()
        info = run.stat()
        planned = self.root / "lane-root" / ("r" + tag)
        record = {
            "schema": 1, "kind": "managed-lane-run", "id": run_id,
            "uid": os.geteuid(), "root": str(self.root), "path": str(run),
            "directory": {"device": info.st_dev, "inode": info.st_ino},
            "created": "2026-01-01T00:00:00Z", "phase": "creating",
            "keep": False, "pin": False, "runtime_root": "/runtime/test",
            "argv": ["west", "test", tag], "workload_rc": None,
            "lane": {"path": str(planned), "name": planned.name,
                     "root": str(planned.parent), "token": run_id},
            "command_started": False,
            "cleanup": {"success": False, "problems": []},
            "evidence": {"complete": False},
        }
        write_json(run / "run.json", record)
        (run / "command.stdout").write_bytes(b"stdout bytes\n")
        (run / "command.stderr").write_bytes(b"stderr bytes\n")
        write_json(run / "outcome.json",
                   {"run_id": run_id, "workload_rc": None, "command_started": False})
        return run, record

    def test_seals_closed_evidence_and_symlink_target_bytes(self):
        lane, run, record = self.managed_pair("a")
        core_target = self.root / "kernel-core"
        core = b"kernel core bytes for symbolization\n" * 64
        core_target.write_bytes(core)
        (lane / "cores" / "core.4242").symlink_to(core_target)
        log = b"fuse boot log line\n" * 1024
        (lane / "logs" / "fuse-overlayfs.log").write_bytes(log)
        (lane / "receipts" / "runtime-artifacts.txt").write_bytes(b"artifact receipt\n")
        boot = lane / "state" / "boot-runs"
        boot.mkdir()
        (boot / "darling-boot-1.log").write_bytes(b"primary boot log\n")
        (boot / "darling-boot-1.log.bundle").mkdir()
        (boot / "darling-boot-1.log.bundle" / "stdout.log").write_bytes(b"bundle bytes\n")
        result = finalize_run_evidence(lane, run)
        self.assertTrue(result["complete"])
        payload = run / "payload"
        self.assertTrue((payload / "SHA256SUMS").is_file())
        self.assertEqual((payload / "logs" / "fuse-overlayfs.log").read_bytes(), log)
        self.assertEqual((payload / "cores" / "core.4242").read_bytes(), core)
        self.assertEqual(
            (payload / "state" / "boot-runs" / "darling-boot-1.log").read_bytes(),
            b"primary boot log\n",
        )
        self.assertEqual((payload / "command" / "command.stdout").read_bytes(), b"stdout bytes\n")
        self.assertEqual(json.loads((payload / "command" / "outcome.json").read_text())["workload_rc"], 7)
        self.assertTrue((payload / "command" / "invocation.json").is_file())
        self.assertTrue((payload / "run.json").is_file())
        self.assertTrue((payload / "source-identity.json").is_file())
        verified = verify_archive(payload, recursive=True)
        self.assertFalse(verified["failed"])
        self.assertEqual(verified["unsealed_files"], 0)
        again = finalize_run_evidence(lane, run)
        self.assertTrue(again["verified"])
        shutil.rmtree(lane)
        core_target.unlink()
        self.assertEqual((payload / "cores" / "core.4242").read_bytes(), core)

    def test_missing_symlink_target_and_git_worktree_preserve_raw_lane(self):
        lane, run, _ = self.managed_pair("b")
        (lane / "cores" / "core.7").symlink_to(self.root / "missing-core")
        with self.assertRaises(EvidenceError):
            finalize_run_evidence(lane, run)
        self.assertTrue(lane.is_dir())
        self.assertFalse((run / "payload").exists())
        (lane / "cores" / "core.7").unlink()
        repository = lane / "state" / "bundles" / "repo"
        repository.mkdir(parents=True)
        (repository / ".git").write_text("gitdir: elsewhere\n")
        with self.assertRaises(EvidenceError):
            finalize_run_evidence(lane, run)
        self.assertTrue(lane.is_dir())

    def test_unstarted_run_seals_only_command_logs_and_record_identity(self):
        run, record = self.unstarted_run("c")
        encoded = (run / "run.json").read_bytes()
        result = finalize_unstarted_run_evidence(run)
        payload = run / "payload"
        self.assertTrue(result["never_started"])
        self.assertFalse((payload / "lane.json").exists())
        self.assertEqual((payload / "command" / "command.stdout").read_bytes(), b"stdout bytes\n")
        self.assertEqual((payload / "command" / "command.stderr").read_bytes(), b"stderr bytes\n")
        self.assertTrue((payload / "command" / "outcome.json").is_file())
        self.assertTrue((payload / "run.json").is_file())
        capture = json.loads((payload / "capture.json").read_text())
        self.assertIsNone(capture["lane"])
        self.assertIsNone(capture["lane_identity"])
        self.assertEqual(capture["source"], {"lane": None, "never_started": True})
        self.assertEqual(capture["record_sha256"], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(capture["run"]["id"], record["id"])
        again = finalize_unstarted_run_evidence(run)
        self.assertTrue(again["never_started"])

    def test_started_recovery_rejects_foreign_and_incomplete_payload(self):
        lane_a, run_a, _ = self.managed_pair("d")
        finalize_run_evidence(lane_a, run_a)
        lane_b, run_b, _ = self.managed_pair("e")
        finalize_run_evidence(lane_b, run_b)
        # A valid payload copied from another run must never authorize removal.
        shutil.rmtree(run_b / "payload")
        shutil.copytree(run_a / "payload", run_b / "payload")
        with self.assertRaises(EvidenceError):
            finalize_run_evidence(lane_b, run_b)
        self.assertTrue(lane_b.is_dir())
        # A checksum that covers nothing, or leaves payload bytes uncovered,
        # is not a complete capture either.
        lane_c, run_c, _ = self.managed_pair("f")
        finalize_run_evidence(lane_c, run_c)
        (run_c / "payload" / "SHA256SUMS").write_text("")
        with self.assertRaises(EvidenceError):
            finalize_run_evidence(lane_c, run_c)
        self.assertTrue(lane_c.is_dir())
        # Even a record that claims completion cannot use that payload to
        # authorize removal: the collector keeps the bytes and reports why.
        record = json.loads((run_c / "run.json").read_text())
        record.update(phase="completed", cleanup={"success": True, "problems": []},
                      evidence={"complete": True, "manifest": "payload/SHA256SUMS"})
        write_json(run_c / "run.json", record)
        rows = prune_run_evidence(self.root, keep_last=0, max_bytes=2**30)
        self.assertTrue(run_c.is_dir())
        self.assertTrue(any(row["path"] == str(run_c) and row["action"] == "retained"
                            and "manifest" in row["reason"] for row in rows))
        lane_d, run_d, _ = self.managed_pair("g")
        finalize_run_evidence(lane_d, run_d)
        (run_d / "payload" / "unrecorded.bin").write_bytes(b"not in capture.json\n")
        with self.assertRaises(EvidenceError):
            finalize_run_evidence(lane_d, run_d)
        self.assertTrue(lane_d.is_dir())

    def test_unstarted_recovery_rejects_started_payload_provenance(self):
        lane, started_run, _ = self.managed_pair("h")
        finalize_run_evidence(lane, started_run)
        run, record = self.unstarted_run("i")
        (run / "payload").mkdir()
        for item in (started_run / "payload").iterdir():
            target = run / "payload" / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        with self.assertRaises(EvidenceError):
            finalize_unstarted_run_evidence(run)
        self.assertTrue(run.is_dir())
        self.assertTrue((run / "payload" / "lane.json").is_file())

    def make_run(self, name: str, created: str, *, size: int = 128,
                 completed: str | None = None, **overrides) -> Path:
        path = self.runs / name
        path.mkdir()
        (path / ".lock").write_text("")
        info = path.stat()
        lane = {
            "path": str(self.root / "lane-root" / ("r" + name)),
            "name": "r" + name, "root": str(self.root / "lane-root"),
            "token": name, "device": 111, "inode": 222,
        }
        record = {
            "schema": 1, "kind": "managed-lane-run", "id": name, "uid": os.geteuid(),
            "root": str(self.root), "path": str(path),
            "directory": {"device": info.st_dev, "inode": info.st_ino},
            "created": created, "completed": completed or created,
            "phase": "completed", "keep": False, "pin": False,
            "runtime_root": f"/runtime/{name}", "argv": ["west", "test", name],
            "lane": lane, "workload_rc": 0, "command_started": True,
            "cleanup": {"success": True, "problems": []},
            "evidence": {"complete": True, "manifest": "payload/SHA256SUMS"},
        }
        record.update(overrides)
        write_json(path / "run.json", record)
        self.seal_completed_payload(path, record, size=size)
        return path

    def seal_completed_payload(self, path: Path, record: dict, *, size: int) -> None:
        payload = path / "payload"
        payload.mkdir()
        (payload / "job.log").write_bytes(b"x" * size)
        sealed_lane = {
            "schema": 2, "source": None,
            "runtime": {"root": record["runtime_root"], "launcher": "/runtime/bin/bash"},
            "managed_run": {
                "id": record["id"], "path": record["path"],
                "device": record["lane"]["device"], "inode": record["lane"]["inode"],
            },
        }
        write_json(payload / "lane.json", sealed_lane)
        shutil.copy2(path / "run.json", payload / "run.json")
        write_json(payload / "command" / "outcome.json",
                   {"run_id": record["id"], "workload_rc": record.get("workload_rc"),
                    "command_started": True})
        write_json(payload / "command" / "invocation.json",
                   {"run_id": record["id"], "argv": record["argv"]})
        files = []
        for relative in ("job.log", "lane.json", "run.json",
                         "command/outcome.json", "command/invocation.json"):
            item = payload / relative
            files.append({"path": relative, "sha256": digest(item),
                          "bytes": item.stat().st_size, "symlink": False})
        capture = {
            "schema": 1, "kind": "managed-run-evidence",
            "created": "2026-01-01T00:00:00Z", "complete": True,
            "lane": record["lane"]["path"],
            "lane_identity": {
                "path": record["lane"]["path"], "name": record["lane"]["name"],
                "root": record["lane"]["root"], "token": record["id"],
                "device": record["lane"]["device"], "inode": record["lane"]["inode"],
                "runtime_root": record["runtime_root"], "source_path": None,
            },
            "run": {
                "id": record["id"], "path": record["path"], "root": record["root"],
                "directory": dict(record["directory"]),
                "argv": record["argv"], "runtime_root": record["runtime_root"],
            },
            "record_sha256": digest(payload / "run.json"),
            "source": {"recorded": False}, "files": files, "omitted": [],
        }
        write_json(payload / "capture.json", capture)
        verify_archive(payload, recursive=True, write_manifest=True)

    def test_run_prune_count_bytes_pins_locks_and_unmarked_protection(self):
        old = self.make_run("run-1", "2026-01-01T00:00:01Z")
        newest = self.make_run("run-2", "2026-01-01T00:00:02Z")
        pinned = self.make_run("run-3", "2026-01-01T00:00:03Z", pin=True)
        protected = self.make_run("run-4", "2026-01-01T00:00:04Z")
        locked = self.make_run("run-5", "2026-01-01T00:00:05Z")
        unmarked = self.runs / "unmarked"
        unmarked.mkdir()
        (unmarked / "note").write_text("not ours\n")
        lock = (locked / ".lock").open("r")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rows = prune_run_evidence(
                self.root, keep_last=1, max_bytes=10**9, protect=[protected],
            )
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        deleted = {row["path"] for row in rows if row["action"] == "deleted"}
        self.assertIn(str(old), deleted)
        for path in (newest, pinned, protected, locked, unmarked):
            self.assertTrue(path.is_dir(), path)
        deleted_rows = [row for row in rows if row["action"] == "deleted"]
        self.assertTrue(all(row["runtime_pin_key"] == f"evidence:{row['path']}" for row in deleted_rows))
        self.assertTrue(all(row["runtime_root"].startswith("/runtime/") for row in deleted_rows))

    def test_run_prune_byte_bound_evicts_older_but_not_newest(self):
        first = self.make_run("run-a", "2026-01-01T00:00:01Z", size=4096)
        second = self.make_run("run-b", "2026-01-01T00:00:02Z", size=4096)
        newest = self.make_run("run-c", "2026-01-01T00:00:03Z", size=4096)
        rows = prune_run_evidence(self.root, keep_last=5, max_bytes=1)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(newest.exists())

    def test_run_prune_uses_stable_completed_not_directory_mtimes(self):
        # Mutating a pin changes run.json mtime; pin/unpin must not reorder
        # completion. The stable completed field decides which oracle survives.
        older_oracle = self.make_run(
            "run-old-oracle", "2026-01-01T00:00:09Z",
            completed="2026-01-01T00:00:01Z", size=256,
        )
        newer_oracle = self.make_run(
            "run-new-oracle", "2026-01-01T00:00:08Z",
            completed="2026-01-01T00:00:08Z", size=256,
        )
        os.utime(older_oracle / "run.json", (time.time() + 60, time.time() + 60))
        os.utime(older_oracle, (time.time() + 60, time.time() + 60))
        rows = prune_run_evidence(self.root, keep_last=1, max_bytes=10**9)
        self.assertFalse(older_oracle.exists())
        self.assertTrue(newer_oracle.exists())
        self.assertTrue(any(row["path"] == str(newer_oracle) and row["action"] == "retained"
                            for row in rows))


    def journal_value(self, path: Path, *, phase: str, trash: Path | None = None) -> dict:
        record = json.loads((path / "run.json").read_text())
        info = path.stat()
        payload = path / "payload"
        payload_info = payload.stat()
        store = self.root / "retired-evidence"
        store.mkdir(mode=0o700, exist_ok=True)
        return {
            "schema": 1, "kind": "managed-run-retirement", "phase": phase,
            "root": str(self.root), "uid": os.geteuid(),
            "run": str(path), "name": path.name,
            "directory": {"device": info.st_dev, "inode": info.st_ino},
            "record_sha256": digest(path / "run.json"),
            "payload": {
                "path": str(payload),
                "directory": {"device": payload_info.st_dev, "inode": payload_info.st_ino},
                "manifest_sha256": digest(payload / "SHA256SUMS"),
                "manifest_bytes": (payload / "SHA256SUMS").stat().st_size,
            },
            "trash": str(trash if trash is not None
                         else store / (".trash-" + path.name + "-pending")),
            "bytes": 256, "runtime_root": record.get("runtime_root"),
            "runtime_pin_key": "evidence:" + str(path),
            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
        }

    def test_retirement_journal_is_durable_until_acknowledged(self):
        old = self.make_run("run-ledger-old", "2026-01-01T00:00:01Z", size=256)
        newest = self.make_run("run-ledger-new", "2026-01-01T00:00:02Z", size=256)
        rows = prune_run_evidence(self.root, keep_last=1, max_bytes=2**20)
        deleted = [row for row in rows
                   if row["action"] == "deleted" and row["path"] == str(old)]
        self.assertEqual(len(deleted), 1)
        self.assertFalse(old.exists())
        self.assertTrue(newest.is_dir())
        journal = Path(deleted[0]["journal"])
        self.assertTrue(journal.is_file())
        self.assertEqual(deleted[0]["runtime_pin_key"], f"evidence:{old}")
        self.assertEqual(json.loads(journal.read_text())["phase"], "completed")
        # An unacknowledged completed removal is reported again so the caller
        # can release the same runtime pin idempotently after a crash.
        again = prune_run_evidence(self.root, keep_last=1, max_bytes=2**20)
        self.assertTrue(any(row["action"] == "deleted" and row["path"] == str(old)
                            for row in again))
        self.assertTrue(ack_run_retirement(self.root, deleted[0]))
        self.assertFalse(journal.exists())
        final = prune_run_evidence(self.root, keep_last=1, max_bytes=2**20)
        self.assertFalse(any(row["action"] == "deleted" and row["path"] == str(old)
                             for row in final))

    def test_interrupted_retirement_resumes_from_exact_journal_identity(self):
        planned = self.make_run("run-planned", "2026-01-01T00:00:01Z", size=256)
        value = self.journal_value(planned, phase="planned")
        write_json(self.root / "retired-evidence" / (planned.name + ".json"), value)
        rows = prune_run_evidence(self.root, keep_last=5, max_bytes=2**20)
        self.assertFalse(planned.exists())
        resumed = [row for row in rows
                   if row["action"] == "deleted" and row["path"] == str(planned)]
        self.assertEqual(len(resumed), 1)
        journal = Path(resumed[0]["journal"])
        self.assertEqual(json.loads(journal.read_text())["phase"], "completed")

        detached = self.make_run("run-detached", "2026-01-01T00:00:02Z", size=256)
        store = self.root / "retired-evidence"
        trash = store / ".trash-run-detached-resume"
        value = self.journal_value(detached, phase="detached", trash=trash)
        os.rename(detached, trash)
        write_json(store / (detached.name + ".json"), value)
        rows = prune_run_evidence(self.root, keep_last=5, max_bytes=2**20)
        self.assertFalse(trash.exists())
        self.assertTrue(any(row["action"] == "deleted" and row["path"] == str(detached)
                            for row in rows))
        self.assertEqual(json.loads((store / (detached.name + ".json")).read_text())["phase"],
                         "completed")

    def test_retirement_journal_does_not_adopt_changed_run(self):
        run = self.make_run("run-changed", "2026-01-01T00:00:01Z", size=256)
        value = self.journal_value(run, phase="planned")
        write_json(self.root / "retired-evidence" / (run.name + ".json"), value)
        replacement = run.with_name(run.name + "-replacement")
        (replacement / "payload").mkdir(parents=True)
        os.rename(run / "run.json", replacement / "run.json")
        rows = prune_run_evidence(self.root, keep_last=5, max_bytes=2**20)
        self.assertTrue(run.is_dir())
        self.assertTrue(any(row["action"] == "retained"
                            and "not resumable" in row["reason"] for row in rows))


class JobEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="job-retention-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()

    def make_job(self, name: str, *, rc: int = 0, log: bytes = b"job log\n" * 100,
                 cancel: bool = False, pin: bool = False) -> Path:
        state = self.jobs / name
        state.mkdir()
        (state / ".west-job.lock").write_text("")
        (state / ".west-job.readers.lock").write_text("")
        mark_job_state(state, self.root)
        (state / "command").write_text("west test\n")
        (state / "command-argv").write_bytes(b"west\0test\0")
        (state / "log").write_bytes(log)
        (state / "rc").write_text(f"{rc}\n")
        if cancel:
            (state / "cancel-requested").write_text("")
        if pin:
            (state / "pin").write_text("")
        return state

    def test_finalization_preserves_rc_and_compacts_older_log_for_follow(self):
        first = self.make_job("job-a", log=b"old log line\n" * 4096)
        second = self.make_job("job-b", log=b"middle log line\n" * 4096)
        current = self.make_job("job-c", rc=143, log=b"current log\n" * 32, cancel=True)
        finalize_job_state(first, workload_rc=0)
        finalize_job_state(second, workload_rc=0)
        result = finalize_job_state(current, workload_rc=143, keep_last=1)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_dir())
        self.assertFalse((second / "log").exists())
        with open_payload(second / "log") as source:
            self.assertEqual(source.read(), b"middle log line\n" * 4096)
        finalized = json.loads((current / "finalized.json").read_text())
        self.assertEqual(finalized["workload_rc"], 143)
        self.assertTrue(finalized["cancel_requested"])
        self.assertEqual((current / "log").read_bytes(), b"current log\n" * 32)
        self.assertEqual(result["workload_rc"], 143)

    def test_job_prune_protects_pin_current_lock_and_unmarked(self):
        pinned = self.make_job("job-pinned", pin=True)
        locked = self.make_job("job-locked")
        unmarked = self.jobs / "legacy"
        unmarked.mkdir()
        (unmarked / "log").write_text("legacy\n")
        for state in (pinned, locked):
            finalize_job_state(state, workload_rc=0)
        lock = (locked / ".west-job.lock").open("r")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rows = prune_job_states(self.root, store=self.jobs, keep_last=0)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.assertTrue(pinned.is_dir())
        self.assertTrue(locked.is_dir())
        self.assertTrue(unmarked.is_dir())

    def test_shared_follow_lease_blocks_finalized_job_pruning(self):
        older = self.make_job("job-followed")
        newer = self.make_job("job-newer")
        finalize_job_state(older, workload_rc=0)
        finalize_job_state(newer, workload_rc=0)
        lease = (older / ".west-job.readers.lock").open("r")
        fcntl.flock(lease, fcntl.LOCK_SH)
        try:
            rows = prune_job_states(self.root, store=self.jobs, keep_last=0)
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)
            lease.close()
        self.assertTrue(older.is_dir())
        rows = prune_job_states(self.root, store=self.jobs, keep_last=0)
        self.assertFalse(older.exists())
        self.assertTrue(newer.is_dir())


class WestJobFollow(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="job-follow-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self.state = self.jobs / "job"
        self.state.mkdir()
        (self.state / ".west-job.lock").write_text("")
        (self.state / ".west-job.readers.lock").write_text("")
        mark_job_state(self.state, self.root)
        self.script = ROOT / "scripts" / "west-job.sh"
        self.environment = dict(
            os.environ, WEST_JOB_NO_AUTODIAG="1", WEST_JOB_FOLLOW_HEARTBEAT="0",
        )

    def _start_time(self, pid: int) -> str:
        fields = (Path("/proc") / str(pid) / "stat").read_bytes().rsplit(b")", 1)[1].split()
        return fields[19].decode()

    def test_compressed_follow_delivers_exact_output_and_rc(self):
        payload = b"final output line\n" * 256
        (self.state / "log").write_bytes(payload)
        from evidence_storage import compress_file
        compress_file(self.state / "log", finalized=True, explicit=True, role="job-log")
        self.assertFalse((self.state / "log").exists())
        (self.state / "rc").write_text("7\n")
        completed = subprocess.run(
            [str(self.script), "follow", "--state-dir", str(self.state)],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 7, completed.stderr.decode(errors="replace"))
        self.assertEqual(completed.stdout[:len(payload)], payload)
        self.assertEqual(completed.stdout.count(b"final output line\n"), payload.count(b"final output line\n"))

    def test_follow_leases_state_and_observes_late_created_log(self):
        process = subprocess.Popen(["sleep", "30"])
        self.addCleanup(process.wait, timeout=10)
        self.addCleanup(process.kill)
        (self.state / "pid").write_text(f"{process.pid}\n")
        (self.state / "start-time").write_text(self._start_time(process.pid) + "\n")
        # The job's own log is the streamed one; it is deliberately absent
        # when the observer starts and appears while it is already following.
        log = self.state / "log"
        observer = subprocess.Popen(
            [str(self.script), "follow", "--state-dir", str(self.state)],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            leased = False
            while time.monotonic() < deadline and observer.poll() is None:
                with (self.state / ".west-job.readers.lock").open("r") as probe:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        leased = True
                        break
                    finally:
                        fcntl.flock(probe, fcntl.LOCK_UN)
                time.sleep(0.05)
            self.assertTrue(leased, "follow never held its shared state lease")
            # The live log does not exist yet; it must remain retryable rather
            # than becoming permanently "compressed".
            time.sleep(1.2)
            log.write_bytes(b"late created output\n")
            (self.state / "rc").write_text("0\n")
            stdout, stderr = observer.communicate(timeout=30)
        finally:
            if observer.poll() is None:
                observer.kill()
                observer.communicate()
        self.assertEqual(observer.returncode, 0, stderr.decode(errors="replace"))
        self.assertIn(b"late created output\n", stdout)


if __name__ == "__main__":
    unittest.main()

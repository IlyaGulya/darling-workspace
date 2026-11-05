"""Host behavior: real owner leases, PID lifetimes and unstarted recovery.

No Darling runtime/prefix is fabricated. The real scoped guest lifecycle is
exercised separately with an explicitly selected installed image and base.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
from managed_run import _Command, _start_time, interrupt_payload, LaneError, read_run, recover_runs, require_budget, set_run_pin
from evidence_retention import EvidenceError, interrupt_job_owners, mark_job_state, prune_run_evidence
from evidence_storage import open_payload


def refuses(call):
    try:
        call()
    except (LaneError, EvidenceError, OSError, ValueError):
        return
    raise AssertionError("unsafe operation was accepted")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


OWNER = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import managed_run as m
root=Path(sys.argv[2]);run=root/'runs'/('run-'+sys.argv[3]);run.mkdir(parents=True)
st=run.stat()
record={
 'schema':1,'kind':'managed-lane-run','id':sys.argv[3],'uid':os.geteuid(),
 'root':str(root),'path':str(run),'directory':{'device':st.st_dev,'inode':st.st_ino},
 'created':m._now(),'phase':'creating',
 'owner':{'pid':os.getpid(),'start_time':m._start_time(os.getpid())},
 'lane':{'path':str(root/'l'/('r'+sys.argv[3][:12])),'root':str(root/'l'),
         'name':'r'+sys.argv[3][:12],'token':sys.argv[3]},
 'argv':['unstarted-allocation'],'runtime_root':None,'keep':False,'pin':False,
 'command_started':False,'workload_rc':None,'final_rc':None,
 'cleanup':{'success':False,'problems':[]},'evidence':{'complete':False},
}
with m._run_lock(run,create=True):
 m._save(record)
 m.write_json(run/'invocation.json',{'argv':record['argv'],'command_started':False})
 print('LOCKED',flush=True)
 sys.stdin.buffer.read(1)
print('UNLOCKED',flush=True)
sys.stdin.buffer.read(1)
os._exit(37)
'''


def interrupted_owner(root):
    identifier = uuid.uuid4().hex
    run = root / "runs" / ("run-" + identifier)
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", OWNER, str(ROOT / "west_commands"), str(root), identifier],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        rows = recover_runs(root, run=run)
        assert len(rows) == 1 and rows[0]["action"] == "keep"
        assert read_run(run)["phase"] == "creating"
        process.stdin.write("x"); process.stdin.flush()
        assert process.stdout.readline().strip() == "UNLOCKED"
        # A free flock alone is not permission to take over a still-live owner.
        rows = recover_runs(root, run=run)
        assert len(rows) == 1 and rows[0]["action"] == "keep"
        assert read_run(run)["phase"] == "creating"
        process.stdin.write("x"); process.stdin.flush()
        assert process.wait(timeout=15) == 37, process.stderr.read()
        rows = recover_runs(root, run=run)
        assert len(rows) == 1 and rows[0]["action"] == "recovered", rows
        result = read_run(run)
        assert result["phase"] == "completed" and result["cleanup"]["success"]
        assert result["workload_rc"] is None and result["final_rc"] != 0
        with open_payload(run / "payload/command/invocation.json") as stream:
            invocation = json.load(stream)
        assert invocation == {"argv": ["unstarted-allocation"], "command_started": False}
        capture = json.loads((run / "payload/capture.json").read_text())
        assert capture["never_started"] is True and capture["lane"] is None
        seal = run / "payload/SHA256SUMS"
        before = (digest(seal), seal.stat().st_mtime_ns)
        assert recover_runs(root, run=run) == []
        assert (digest(seal), seal.stat().st_mtime_ns) == before
        return run
    finally:
        if process.poll() is None:
            process.stdin.close()
            process.wait(timeout=15)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def job_owner_domains(root):
    root = root / "job-owner-domains"
    root.mkdir()
    states = [root / "job-signal-a", root / "job-signal-b"]
    locks = []
    for state in states:
        state.mkdir()
        for name in (".west-job.lock", ".west-job.readers.lock"):
            (state / name).touch(mode=0o600)
        descriptor = os.open(state / ".west-job.lock", os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locks.append(descriptor)
        mark_job_state(state, root)
        (state / "runner-pid").write_text(str(os.getpid()))
        (state / "runner-start-time").write_text(_start_time(os.getpid()))
    program = r"""
import fcntl,json,os,signal,sys,uuid
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from evidence_retention import register_job_owner
root,state=Path(sys.argv[2]),Path(sys.argv[3])
identifier=uuid.uuid4().hex
run=root/'runs'/('run-'+identifier)
run.mkdir(parents=True)
descriptor=os.open(run/'.lock',os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(descriptor,fcntl.LOCK_EX)
fields=Path('/proc/self/stat').read_bytes().rsplit(b')',1)[1].split()
metadata=run.stat()
record={'schema':1,'kind':'managed-lane-run','id':identifier,'uid':os.geteuid(),
        'root':str(root),'path':str(run),'directory':{'device':metadata.st_dev,'inode':metadata.st_ino},
        'phase':'preparing','owner':{'pid':os.getpid(),'start_time':fields[19].decode()}}
(run/'run.json').write_text(json.dumps(record))
signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGINT})
register_job_owner(state,run)
print(str(run),flush=True)
received=signal.sigwait({signal.SIGINT})
(root/'signal-cleanup-oracle').write_text(str(received))
raise SystemExit(128+received)
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", program, str(ROOT / "west_commands"), str(root), str(states[0])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        run = Path(process.stdout.readline().strip())
        assert (run / "run.json").is_file()
        foreign = states[1] / "managed-run-owners"
        foreign.mkdir()
        reference = next((states[0] / "managed-run-owners").iterdir())
        shutil.copy2(reference, foreign / reference.name)
        refuses(lambda: interrupt_job_owners(states[1]))
        assert process.poll() is None and not (root / "signal-cleanup-oracle").exists()
        interrupt_job_owners(states[0])
        assert process.wait(timeout=10) == 128 + signal.SIGINT, process.stderr.read()
        assert (root / "signal-cleanup-oracle").read_text() == str(signal.SIGINT)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        for descriptor in locks:
            os.close(descriptor)


def payload_cancellation(root):
    owned = root / "payload-controls"
    owned.mkdir()
    identifier = uuid.uuid4().hex
    run = owned / "runs" / ("run-" + identifier)
    target = (
        "import signal,sys; from pathlib import Path; "
        "signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGINT}); "
        "print('PAYLOAD_READY',flush=True); received=signal.sigwait({signal.SIGINT}); "
        "Path(sys.argv[1]).write_text(str(received)); raise SystemExit(128+received)"
    )
    controller = r"""
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import managed_run as m
root=Path(sys.argv[2]);run=root/'runs'/('run-'+sys.argv[3]);run.mkdir(parents=True)
metadata=run.stat()
record={'schema':1,'kind':'managed-lane-run','id':sys.argv[3],'uid':os.geteuid(),
        'root':str(root),'path':str(run),'directory':{'device':metadata.st_dev,'inode':metadata.st_ino},
        'phase':'running','owner':{'pid':os.getpid(),'start_time':m._start_time(os.getpid())},
        'lane':{'path':str(root/'l'/'host-payload'),'root':str(root/'l'),
                'name':'host-payload','token':sys.argv[3]}}
with m._run_lock(run,create=True):
 m._save(record)
 environment=dict(os.environ,DW_MANAGED_RUN=record['id'])
 rc=m.run_lane_payload([sys.executable,'-B','-c',sys.argv[4],str(root/'observed-signal')],
                       environment,{'path':str(run)})
 raise SystemExit(rc)
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", controller, str(ROOT / "west_commands"), str(owned), identifier, target],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "PAYLOAD_READY"
        data = read_run(run)
        reference = run / "payload-owner.json"
        original = reference.read_bytes()
        forged = json.loads(original)
        forged["owner"] = {"pid": os.getpid(), "start_time": _start_time(os.getpid())}
        reference.write_text(json.dumps(forged))
        refuses(lambda: interrupt_payload(data, signal.SIGINT))
        assert process.poll() is None and not (owned / "observed-signal").exists()
        reference.write_bytes(original)
        interrupt_payload(data, signal.SIGINT)
        assert process.wait(timeout=10) == 128 + signal.SIGINT, process.stderr.read()
        assert (owned / "observed-signal").read_text() == str(signal.SIGINT)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()


def cancelled_after_output_eof(root):
    command_dir = root / "closed-output"
    command_dir.mkdir()
    command = _Command(
        [sys.executable, "-B", "-c", "import os,signal; os.close(1); os.close(2); signal.pause()"],
        command_dir, dict(os.environ), json_output=True,
    )
    def deadline(signum, frame):
        raise TimeoutError("cancellation was ignored after command output reached EOF")
    previous = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(10)
    try:
        # The child remains alive after closing both streams. EOF is not completion.
        observed = command.wait(lambda: signal.SIGTERM if not command.selector.get_map() else 0)
        assert observed is None, "a cancelled command must not invent an exit status"
        assert command.process.wait(timeout=5) == -signal.SIGTERM
        command.close()
        assert command.complete
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if command.process.poll() is None:
            command.process.kill()
        command.process.wait(timeout=5)
        command.close(strict=False)


def contract(root):
    cancelled_after_output_eof(root)
    job_owner_domains(root)
    payload_cancellation(root)
    source = root / "user-source.c"
    source.write_bytes(b"/* authored source is not owned run data */\n")
    root_before = set(root.iterdir())
    refuses(lambda: require_budget(root, shutil.disk_usage(root).total + 2**40))
    assert set(root.iterdir()) == root_before
    assert source.read_bytes() == b"/* authored source is not owned run data */\n"

    first = interrupted_owner(root)
    copied = root / "runs" / ("run-" + uuid.uuid4().hex)
    copied.mkdir()
    shutil.copy2(first / "run.json", copied / "run.json")
    (copied / "keeper").write_text("unowned byte must remain\n")
    refuses(lambda: read_run(copied, root=root))
    incomplete = root / "runs" / ("run-" + uuid.uuid4().hex)
    incomplete.mkdir()
    metadata = incomplete.stat()
    invalid = dict(read_run(first), id=incomplete.name.removeprefix("run-"), path=str(incomplete),
                   directory={"device": metadata.st_dev, "inode": metadata.st_ino}, phase="creating")
    invalid.pop("lane")
    marker = incomplete / "run.json"
    marker.write_text(json.dumps(invalid))
    original = marker.read_bytes()
    refuses(lambda: read_run(incomplete, root=root))
    rows = recover_runs(root)
    assert all(row.get("path") != str(copied) or row["action"] == "keep" for row in rows)
    assert (copied / "keeper").read_text() == "unowned byte must remain\n"
    assert marker.read_bytes() == original and not (incomplete / ".lock").exists()

    # Public pin/unpin protects real finalized bytes across the actual bounded collector.
    set_run_pin(root, first, True)
    second = interrupted_owner(root)
    prune_run_evidence(root, keep_last=1, max_bytes=1)
    assert first.exists() and second.exists()
    set_run_pin(root, first, False)
    rows = prune_run_evidence(root, keep_last=1, max_bytes=1)
    assert any(row["path"] == str(first) and row["action"] == "deleted" for row in rows), rows
    assert not first.exists() and second.exists()
    assert source.read_bytes() == b"/* authored source is not owned run data */\n"
    assert (copied / "keeper").read_text() == "unowned byte must remain\n"


if __name__ == "__main__":
    scratch = Path(os.environ.get("DW_STATE_ROOT", ROOT.parent / ".west-state")) / "test-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="managed-owner-", dir=scratch) as directory:
        contract(Path(directory))
    print("PASS managed-run-owner-contract (host; real process/lease/filesystem behavior)")

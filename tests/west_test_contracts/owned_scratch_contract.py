#!/usr/bin/env python3
"""Behavioral safety matrix for managed Darling scratch roots."""

from __future__ import annotations

import fcntl
import inspect
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "west_commands"))
import owned_scratch as scratch  # noqa: E402


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def expect_refused(call, needle: str) -> None:
    try:
        call()
    except scratch.ScratchSafetyError as error:
        assert needle in str(error), (needle, error)
    else:
        raise AssertionError(f"expected refusal containing {needle!r}")


def rewrite_created(root: Path, created_ns: int) -> None:
    values = scratch._read_marker(root)
    (root / scratch.MARKER).write_text(
        f"version=1\nkind={values['kind']}\nid={values['id']}\ncreated_ns={created_ns}\n"
    )
    seconds = created_ns / 1_000_000_000
    os.utime(root, (seconds, seconds), follow_symlinks=False)


def write_fake_census_helper(path: Path, payload: str) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({payload!r})\n"
    )
    path.chmod(0o700)


def write_fake_collection_helper(path: Path, payload: str) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "request=json.loads(sys.stdin.read())\n"
        f"payload={payload!r}\n"
        "sys.stdout.write(payload if 'operation' in request else "
        "'{\"protocol_version\":1,\"outcomes\":[]}')\n"
    )
    path.chmod(0o700)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="owned-scratch-contract-") as temporary:
        base = Path(temporary)
        namespace = base / "namespace"

        expect_refused(
            lambda: scratch.OwnedScratchRoot.create(
                namespace=namespace, kind="agent-review", prefix=".gc-forged-",
            ),
            "reserved scratch recovery namespace",
        )

        owner = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        (owner.path / "build").mkdir()
        (owner.path / "build/blob").write_bytes(b"x" * 4096)
        path = owner.path
        owner.discard()
        assert not path.exists()

        crash_script = f"""
import os, sys
sys.path.insert(0, {str(REPO / 'west_commands')!r})
from owned_scratch import OwnedScratchRoot
owner=OwnedScratchRoot.create(namespace=__import__('pathlib').Path(sys.argv[1]), kind='runtime-proof')
print(owner.path, flush=True)
os._exit(17)
"""
        crashed = subprocess.run([sys.executable, "-B", "-c", crash_script, str(namespace)], capture_output=True, text=True)
        assert crashed.returncode == 17
        crashed_root = Path(crashed.stdout.strip())
        outcome = scratch.garbage_collect(namespace, ttl_seconds=0, keep=0)
        assert crashed_root in outcome.removed and not crashed_root.exists()

        active = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        expect_refused(lambda: scratch.discard_exact(namespace, active.path), "active")
        active_path = active.path
        active.close()
        scratch.discard_exact(namespace, active_path)

        cwd_owner = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        cwd_path = cwd_owner.path
        cwd_owner.close()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=cwd_path)
        try:
            expect_refused(lambda: scratch.discard_exact(namespace, cwd_path), "live process")
        finally:
            child.terminate(); child.wait(timeout=5)
        scratch.discard_exact(namespace, cwd_path)

        fd_owner = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        held = fd_owner.path / "held"
        held.write_text("held")
        fd_path = fd_owner.path
        fd_owner.close()
        child = subprocess.Popen(
            [sys.executable, "-c", "import os,time,sys; f=open(sys.argv[1]); print('ready', flush=True); time.sleep(30)", str(held)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert child.stdout is not None and child.stdout.readline().strip() == "ready"
            expect_refused(lambda: scratch.discard_exact(namespace, fd_path), "live process")
        finally:
            child.terminate(); child.wait(timeout=5)
        scratch.discard_exact(namespace, fd_path)

        final_census = scratch.OwnedScratchRoot.create(
            namespace=namespace, kind="agent-review"
        )
        final_census_path = final_census.path; final_census.close()
        original_nested = scratch._remove_nested_worktrees
        final_child = None
        def activate_after_python_census(root, *, mutate, **kwargs):
            nonlocal final_child
            if mutate and final_child is None:
                final_child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"], cwd=root,
                )
            return []
        scratch._remove_nested_worktrees = activate_after_python_census
        try:
            expect_refused(
                lambda: scratch.discard_exact(namespace, final_census_path),
                "active_reference",
            )
        finally:
            scratch._remove_nested_worktrees = original_nested
            if final_child is not None:
                final_child.terminate(); final_child.wait(timeout=5)
        scratch.discard_exact(namespace, final_census_path)

        overflow = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        overflow_path = overflow.path; overflow.close()
        overflow_fds = [os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC) for _ in range(5003)]
        try:
            expect_refused(lambda: scratch.discard_exact(namespace, overflow_path), "fd census overflow")
            assert overflow_path.exists()
        finally:
            for descriptor in overflow_fds:
                os.close(descriptor)
        scratch.discard_exact(namespace, overflow_path)

        nondumpable = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        nondumpable_path = nondumpable.path; nondumpable.close()
        child = subprocess.Popen(
            [
                sys.executable, "-c",
                "import ctypes,time; assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0; print('ready',flush=True); time.sleep(30)",
            ],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert child.stdout is not None and child.stdout.readline().strip() == "ready"
            diagnostics: list[str] = []
            scratch.discard_exact(namespace, nondumpable_path, diagnostics=diagnostics)
            assert diagnostics and any(f"pid={child.pid}:" in item for item in diagnostics)
        finally:
            child.terminate(); child.wait(timeout=5)

        unavailable = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        unavailable_path = unavailable.path; unavailable.close()
        helper = os.environ["DARLING_SCRATCH_CENSUS_HELPER"]
        assert "cargo" not in inspect.getsource(scratch._process_census_helper)
        os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(base / "missing-helper")
        try:
            expect_refused(
                lambda: scratch.discard_exact(namespace, unavailable_path),
                "helper unavailable",
            )
            retained = scratch.garbage_collect(namespace, ttl_seconds=0, keep=0)
            assert unavailable_path.exists()
            assert any(
                path == unavailable_path and "helper unavailable" in reason
                for path, reason in retained.retained
            )
        finally:
            os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = helper
        scratch.discard_exact(namespace, unavailable_path)

        transport = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        transport_path = transport.path; transport.close()
        transport_sentinel = transport_path / "sentinel"
        transport_sentinel.write_bytes(b"transport-boundary-sentinel\x00")

        def transport_identity() -> tuple[int, int, int, int, bytes]:
            root_info = transport_path.lstat()
            sentinel_info = transport_sentinel.lstat()
            return (
                root_info.st_dev, root_info.st_ino,
                sentinel_info.st_dev, sentinel_info.st_ino,
                transport_sentinel.read_bytes(),
            )

        real_helper = os.environ["DARLING_SCRATCH_CENSUS_HELPER"]
        root_fd = os.open(transport_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            expect_refused(lambda: scratch._run_process_census(-1), "transport failed")
            closed_fd = os.dup(root_fd); os.close(closed_fd)
            expect_refused(lambda: scratch._run_process_census(closed_fd), "transport failed")
            for invalid in (0, 1, 2, 999_999):
                request = {
                    "protocol_version": 1, "root_fd": invalid, "process_limit": 1,
                    "fd_limit": 1, "time_limit_ms": 100, "output_limit_bytes": 256,
                    "ignored_descriptors": [],
                }
                result = subprocess.run(
                    [real_helper], input=__import__("json").dumps(request),
                    capture_output=True, text=True, check=False,
                )
                assert result.returncode == 2, (invalid, result)

            fake = base / "fake-census"
            malformed_payloads = [
                "", "{", '{"protocol_version":2,"outcomes":[]}',
                "[" * 2_000 + "]" * 2_000,
                '{"protocol_version":' + "9" * 5_000 + ',"outcomes":[]}',
                '{"protocol_version":1,"outcomes":[{"kind":"unreadable","pid":7,"tid":7,"operation":"stat","errno":5}]}',
                '{"protocol_version":1,"outcomes":[{"kind":"unreadable","pid":7,"tid":7,"operation":"stat","errno":true}]}',
                '{"protocol_version":1,"outcomes":[{"kind":"unreadable","pid":7,"tid":7,"operation":{},"errno":13}]}',
                '{"protocol_version":1,"outcomes":[{"kind":"reference","identity":{"pid":true,"tid":7,"starttime":9},"source":"cwd","descriptor":null}]}',
                '{"protocol_version":1,"outcomes":[{"kind":"reference","identity":{"pid":7,"tid":7,"starttime":9},"source":"unknown","descriptor":null}]}',
                '{"protocol_version":1,"outcomes":[{"kind":"reference","identity":{"pid":7,"tid":7,"starttime":9},"source":[],"descriptor":null}]}',
                '{"protocol_version":1,"outcomes":[{"kind":"budget_exceeded","budget":"fds"}]}',
                '{"protocol_version":1,"outcomes":[{"kind":"budget_exceeded","budget":[]}]}',
            ]
            for payload in malformed_payloads:
                write_fake_census_helper(fake, payload)
                os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(fake)
                before = transport_identity()
                expect_refused(
                    lambda: scratch.discard_exact(namespace, transport_path),
                    "Rust scratch census" if payload == "" else "scratch census",
                )
                assert transport_identity() == before
                automatic = scratch.garbage_collect(namespace, ttl_seconds=0, keep=0)
                assert transport_path not in automatic.removed
                assert any(path == transport_path for path, _reason in automatic.retained)
                assert transport_identity() == before
        finally:
            os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = real_helper
            os.close(root_fd)
        scratch.discard_exact(namespace, transport_path)

        collection_transport = scratch.OwnedScratchRoot.create(
            namespace=namespace, kind="agent-review"
        )
        collection_path = collection_transport.path; collection_transport.close()
        collection_sentinel = collection_path / "sentinel"
        collection_sentinel.write_bytes(b"collection-transport-sentinel")
        collection_identity = (
            collection_path.lstat().st_dev,
            collection_path.lstat().st_ino,
            collection_sentinel.lstat().st_dev,
            collection_sentinel.lstat().st_ino,
            collection_sentinel.read_bytes(),
        )
        fake_collection = base / "fake-collection"
        collection_payloads = [
            "",
            "{",
            '{"protocol_version":2,"result":{"outcome":"collected","entries":1}}',
            '{"protocol_version":1,"result":{"outcome":"collected","entries":true}}',
            '{"protocol_version":1,"result":{"outcome":[],"entries":1}}',
            '{"protocol_version":1,"result":{"outcome":"retained","reason":"unknown"}}',
            '{"protocol_version":1,"result":{"outcome":"collected","entries":1}}',
        ]
        for payload in collection_payloads:
            write_fake_collection_helper(fake_collection, payload)
            os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(fake_collection)
            try:
                scratch.discard_exact(namespace, collection_path)
            except scratch.ScratchSafetyError:
                pass
            else:
                raise AssertionError("malformed collection helper was accepted")
            current = (
                collection_path.lstat().st_dev,
                collection_path.lstat().st_ino,
                collection_sentinel.lstat().st_dev,
                collection_sentinel.lstat().st_ino,
                collection_sentinel.read_bytes(),
            )
            assert current == collection_identity
        os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = real_helper
        scratch.discard_exact(namespace, collection_path)

        interrupted = scratch.OwnedScratchRoot.create(
            namespace=namespace, kind="agent-review"
        )
        interrupted_path = interrupted.path; interrupted.close()
        (interrupted_path / "payload").write_bytes(b"payload")
        interrupted_helper = base / "interrupted-collection"
        interrupted_helper.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,sys\n"
            "request=json.loads(sys.stdin.read())\n"
            "if 'operation' not in request:\n"
            " print('{\"protocol_version\":1,\"outcomes\":[]}')\n"
            "else:\n"
            " root=dict(request['root_identity']); links=root.pop('links')\n"
            " record={'version':1,'public_name':request['root_name'],"
            "'quarantine_name':request['quarantine_name'],'root_identity':root,"
            "'root_links':links,'marker_identity':request['marker_identity'],"
            "'lease_identity':request['lease_identity']}\n"
            " afd=os.open(request['authority_name'],os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600,"
            "dir_fd=request['namespace_fd'])\n"
            " os.write(afd,b'P\\n'+json.dumps(record,separators=(',',':')).encode());os.fsync(afd);"
            "os.fsync(request['namespace_fd'])\n"
            " os.rename(request['root_name'],request['quarantine_name'],"
            "src_dir_fd=request['namespace_fd'],dst_dir_fd=request['namespace_fd'])\n"
            " os.fsync(request['namespace_fd']);os.pwrite(afd,b'Q',0);os.fsync(afd)\n"
            " os._exit(91)\n"
        )
        interrupted_helper.chmod(0o700)
        os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(interrupted_helper)
        try:
            scratch.discard_exact(namespace, interrupted_path)
        except scratch.ScratchQuarantinedError as error:
            interrupted_quarantine = error.path
        else:
            raise AssertionError("helper death after quarantine was not retained")
        interrupted_payload = Path(str(interrupted_quarantine).removesuffix(".authority"))
        assert not interrupted_path.exists() and interrupted_quarantine.exists()
        assert interrupted_payload.exists()
        recovery_sentinel = interrupted_payload / "payload"
        recovery_identity = (
            interrupted_quarantine.lstat().st_dev,
            interrupted_quarantine.lstat().st_ino,
            interrupted_payload.lstat().st_dev,
            interrupted_payload.lstat().st_ino,
            recovery_sentinel.read_bytes(),
        )
        for payload in [
            '{"protocol_version":true,"result":{"outcome":"retained","reason":"identity_mismatch"}}',
            '{"protocol_version":1,"result":{"outcome":"retained","reason":[]}}',
            '{"protocol_version":1,"result":{"outcome":"quarantined","name":[],"authority_name":{},"identity":{},"observed_links":0,"reason":{}}}',
        ]:
            write_fake_collection_helper(fake_collection, payload)
            os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(fake_collection)
            expect_refused(
                lambda: scratch.discard_exact(namespace, interrupted_quarantine),
                "Rust scratch recovery",
            )
            assert recovery_identity == (
                interrupted_quarantine.lstat().st_dev,
                interrupted_quarantine.lstat().st_ino,
                interrupted_payload.lstat().st_dev,
                interrupted_payload.lstat().st_ino,
                recovery_sentinel.read_bytes(),
            )
        interrupted_path.mkdir()
        (interrupted_path / "replacement").write_bytes(b"replacement")
        replacement_identity = interrupted_path.lstat().st_dev, interrupted_path.lstat().st_ino
        os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = real_helper
        recovered = scratch.garbage_collect(namespace, ttl_seconds=10**9, keep=2)
        assert interrupted_quarantine in recovered.removed, recovered
        assert not interrupted_quarantine.exists()
        assert not interrupted_payload.exists()
        assert (interrupted_path.lstat().st_dev, interrupted_path.lstat().st_ino) == replacement_identity
        assert (interrupted_path / "replacement").read_bytes() == b"replacement"
        shutil.rmtree(interrupted_path)

        phase_helper = base / "phase-death-collection"
        phase_helper.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,sys\n"
            "r=json.loads(sys.stdin.read())\n"
            "if 'operation' not in r: print('{\"protocol_version\":1,\"outcomes\":[]}');sys.exit(0)\n"
            "phase=os.environ['DARLING_FAULT_PHASE'];root=dict(r['root_identity']);links=root.pop('links')\n"
            "record={'version':1,'public_name':r['root_name'],'quarantine_name':r['quarantine_name'],"
            "'root_identity':root,'root_links':links,'marker_identity':r['marker_identity'],"
            "'lease_identity':r['lease_identity']}\n"
            "a=os.open(r['authority_name'],os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600,dir_fd=r['namespace_fd'])\n"
            "os.write(a,b'P\\n'+json.dumps(record,separators=(',',':')).encode());os.fsync(a);os.fsync(r['namespace_fd'])\n"
            "os.rename(r['root_name'],r['quarantine_name'],src_dir_fd=r['namespace_fd'],dst_dir_fd=r['namespace_fd']);os.fsync(r['namespace_fd'])\n"
            "os.pwrite(a,b'Q',0);os.fsync(a)\n"
            "if phase=='after-quarantine': os._exit(91)\n"
            "if phase=='during-payload': os.unlink('payload-a',dir_fd=r['root_fd']);os._exit(91)\n"
            "os.unlink('payload-a',dir_fd=r['root_fd']);os.unlink('payload-b',dir_fd=r['root_fd'])\n"
            "if phase=='before-marker-phase': os._exit(91)\n"
            "os.pwrite(a,b'M',0);os.fsync(a)\n"
            "if phase=='after-marker-phase': os._exit(91)\n"
            "os.unlink('.darling-scratch-v1',dir_fd=r['root_fd'])\n"
            "if phase=='after-marker-unlink': os._exit(91)\n"
            "os.pwrite(a,b'L',0);os.fsync(a)\n"
            "if phase=='after-lease-phase': os._exit(91)\n"
            "os.pwrite(a,b'R',0);os.fsync(a);os.unlink('.darling-scratch-lease',dir_fd=r['root_fd'])\n"
            "if phase=='before-final-rmdir': os._exit(91)\n"
            "os.rmdir(r['quarantine_name'],dir_fd=r['namespace_fd']);os.fsync(r['namespace_fd']);os._exit(91)\n"
        )
        phase_helper.chmod(0o700)
        for fault_phase in [
            "after-quarantine",
            "during-payload",
            "before-marker-phase",
            "after-marker-phase",
            "after-marker-unlink",
            "after-lease-phase",
            "before-final-rmdir",
            "after-final-rmdir",
        ]:
            phase_owner = scratch.OwnedScratchRoot.create(
                namespace=namespace, kind="agent-review"
            )
            phase_path = phase_owner.path
            (phase_path / "payload-a").write_bytes(b"a")
            (phase_path / "payload-b").write_bytes(b"b")
            phase_owner.close()
            before_authorities = set(namespace.glob(".gc-*.authority"))
            os.environ["DARLING_FAULT_PHASE"] = fault_phase
            os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(phase_helper)
            expect_refused(
                lambda: scratch.discard_exact(namespace, phase_path),
                "scratch",
            )
            new_authorities = set(namespace.glob(".gc-*.authority")) - before_authorities
            assert len(new_authorities) == 1, (fault_phase, new_authorities)
            authority_path = new_authorities.pop()
            os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = real_helper
            recovered = scratch.garbage_collect(namespace, ttl_seconds=0, keep=0)
            assert authority_path in recovered.removed, (fault_phase, recovered)
            assert not authority_path.exists()
            assert not Path(str(authority_path).removesuffix(".authority")).exists()
            assert not phase_path.exists()
        os.environ.pop("DARLING_FAULT_PHASE", None)

        mount_owner = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        mount_path = mount_owner.path; mount_owner.close()
        original_mounts = scratch._mounts_inside
        scratch._mounts_inside = lambda _root, **_kwargs: [str(mount_path / "mounted")]
        try:
            expect_refused(lambda: scratch.discard_exact(namespace, mount_path), "mounted subtree")
        finally:
            scratch._mounts_inside = original_mounts
        scratch.discard_exact(namespace, mount_path)

        hostile = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        hostile_path = hostile.path; hostile.close()
        (hostile_path / scratch.MARKER).chmod(0o644)
        expect_refused(lambda: scratch.discard_exact(namespace, hostile_path), "hostile")
        shutil.rmtree(hostile_path)

        hardlink = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        hardlink_path = hardlink.path; hardlink.close()
        os.link(hardlink_path / scratch.MARKER, base / "marker-link")
        expect_refused(lambda: scratch.discard_exact(namespace, hardlink_path), "hostile")
        (base / "marker-link").unlink(); scratch.discard_exact(namespace, hardlink_path)

        symlink = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        symlink_path = symlink.path; symlink.close()
        marker_copy = base / "marker-copy"; shutil.copyfile(symlink_path / scratch.MARKER, marker_copy)
        (symlink_path / scratch.MARKER).unlink(); (symlink_path / scratch.MARKER).symlink_to(marker_copy)
        expect_refused(lambda: scratch.discard_exact(namespace, symlink_path), "hostile")
        shutil.rmtree(symlink_path)

        fifo = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        fifo_path = fifo.path; fifo.close()
        (fifo_path / scratch.MARKER).unlink(); os.mkfifo(fifo_path / scratch.MARKER, 0o600)
        expect_refused(lambda: scratch.discard_exact(namespace, fifo_path), "hostile")
        shutil.rmtree(fifo_path)

        root_link = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        root_link_path = root_link.path; root_link.close()
        parked_root = namespace / "parked-root"
        root_link_path.rename(parked_root); root_link_path.symlink_to(parked_root, target_is_directory=True)
        expect_refused(lambda: scratch.discard_exact(namespace, root_link_path), "owned directory")
        root_link_path.unlink(); parked_root.rename(root_link_path); scratch.discard_exact(namespace, root_link_path)

        swapped = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        swapped_path = swapped.path; swapped.close()
        swapped_parked = namespace / "swapped-parked"
        outside_swap = base / "outside-swap"; outside_swap.mkdir(); (outside_swap / "sentinel").write_text("exact")
        real_alternates = scratch._alternate_survivors
        def swap_before_isolation(scan_namespace, donor, **kwargs):
            donor.rename(swapped_parked)
            donor.symlink_to(outside_swap, target_is_directory=True)
            return []
        scratch._alternate_survivors = swap_before_isolation
        try:
            expect_refused(lambda: scratch.discard_exact(namespace, swapped_path), "replaced")
        finally:
            scratch._alternate_survivors = real_alternates
        assert (outside_swap / "sentinel").read_text() == "exact"
        swapped_path.unlink(); swapped_parked.rename(swapped_path); scratch.discard_exact(namespace, swapped_path)

        dirty = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        dirty_repo = dirty.path / "repo"; dirty_repo.mkdir()
        git(dirty_repo, "init", "-q"); git(dirty_repo, "config", "user.email", "contract@example.invalid"); git(dirty_repo, "config", "user.name", "Contract")
        (dirty_repo / "dirty").write_text("dirty")
        dirty_path = dirty.path; dirty.close()
        expect_refused(lambda: scratch.discard_exact(namespace, dirty_path), "dirty worktree")
        scratch.discard_exact(namespace, dirty_path, force_dirty=True)

        donor = base / "canonical"; donor.mkdir()
        git(donor, "init", "-q"); git(donor, "config", "user.email", "contract@example.invalid"); git(donor, "config", "user.name", "Contract")
        (donor / "tracked").write_text("tracked"); git(donor, "add", "tracked"); git(donor, "commit", "-qm", "base")
        linked = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        linked_path = linked.path; worktree = linked_path / "review"
        git(donor, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
        linked.register_worktree(donor, worktree)
        linked.close(); scratch.discard_exact(namespace, linked_path)
        assert str(worktree) not in git(donor, "worktree", "list", "--porcelain")

        stale = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        stale_path = stale.path; stale_worktree = stale_path / "stale"
        git(donor, "worktree", "add", "--quiet", "--detach", str(stale_worktree), "HEAD")
        stale.register_worktree(donor, stale_worktree)
        shutil.rmtree(stale_worktree); stale.close(); scratch.discard_exact(namespace, stale_path)
        assert str(stale_worktree) not in git(donor, "worktree", "list", "--porcelain")

        donor_owner = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        object_donor = donor_owner.path / "objects"; object_donor.mkdir()
        git(object_donor, "init", "-q"); git(object_donor, "config", "user.email", "contract@example.invalid"); git(object_donor, "config", "user.name", "Contract")
        (object_donor / "value").write_text("value"); git(object_donor, "add", "value"); git(object_donor, "commit", "-qm", "value")
        survivor_owner = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        survivor = survivor_owner.path / "survivor"
        subprocess.run(["git", "clone", "-q", "--shared", str(object_donor), str(survivor)], check=True)
        donor_path = donor_owner.path; donor_owner.close(); survivor_owner.close()
        expect_refused(lambda: scratch.discard_exact(namespace, donor_path, dry_run=True), "alternates")
        assert donor_path.exists()
        expect_refused(lambda: scratch.discard_exact(namespace, donor_path), "alternates")
        assert donor_path.exists() and (survivor / ".git/objects/info/alternates").exists()
        real_git_output = scratch._git_output
        def fail_inspection(repo, *args, **kwargs):
            if args[:3] == ("rev-parse", "--git-path", "objects/info/alternates"):
                raise scratch.ScratchSafetyError("injected alternate inspection failure")
            return real_git_output(repo, *args, **kwargs)
        scratch._git_output = fail_inspection
        try:
            expect_refused(lambda: scratch.discard_exact(namespace, donor_path), "inspection failure")
        finally:
            scratch._git_output = real_git_output
        assert donor_path.exists() and (survivor / ".git/objects/info/alternates").exists()
        def fail_repack(repo, *args, **kwargs):
            if args[:3] == ("repack", "-a", "-d"):
                raise scratch.ScratchSafetyError("injected alternate repack failure")
            return real_git_output(repo, *args, **kwargs)
        scratch._git_output = fail_repack
        try:
            expect_refused(lambda: scratch.dissociate_repository(namespace, survivor, object_donor), "injected")
        finally:
            scratch._git_output = real_git_output
        assert donor_path.exists() and (survivor / ".git/objects/info/alternates").exists()
        original_alternates = (survivor / ".git/objects/info/alternates").read_bytes()
        def fail_connectivity(repo, *args, **kwargs):
            if args[:2] == ("fsck", "--connectivity-only"):
                raise scratch.ScratchSafetyError("injected connectivity failure")
            return real_git_output(repo, *args, **kwargs)
        scratch._git_output = fail_connectivity
        try:
            expect_refused(lambda: scratch.dissociate_repository(namespace, survivor, object_donor), "connectivity")
        finally:
            scratch._git_output = real_git_output
        assert (survivor / ".git/objects/info/alternates").read_bytes() == original_alternates
        assert donor_path.exists() and (survivor / "value").read_text() == "value"
        scratch.dissociate_repository(namespace, survivor, object_donor)
        scratch.discard_exact(namespace, donor_path)
        assert not donor_path.exists() and (survivor / "value").read_text() == "value"
        assert not (survivor / ".git/objects/info/alternates").exists()
        scratch.discard_exact(namespace, survivor_owner.path)

        retained = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        review = retained.path / "review"; generated = retained.path / "build"
        review.mkdir(); generated.mkdir(); (review / "source").write_text("keep"); (generated / "blob").write_bytes(b"z" * 8192)
        retained.register_disposable(generated); retained_path = retained.path; retained.retain()
        assert (retained_path / "review/source").read_text() == "keep" and not generated.exists()
        scratch.discard_exact(namespace, retained_path, force_dirty=True)

        outside = base / "outside"; outside.mkdir(); victim = outside / "victim"; victim.write_text("preserve")
        escaped = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        (escaped.path / "link").symlink_to(outside, target_is_directory=True)
        expect_refused(lambda: escaped.register_disposable(escaped.path / "link/victim"), "direct child")
        escaped.register_disposable(escaped.path / "link")
        escaped_path = escaped.path; escaped.retain()
        assert victim.read_text() == "preserve" and not (escaped_path / "link").exists()
        scratch.discard_exact(namespace, escaped_path)

        failed_path = None
        try:
            with scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review") as failed:
                failed_path = failed.path
                generated = failed.path / "target"; generated.mkdir(); (generated / "object").write_bytes(b"object")
                review = failed.path / "review"; review.mkdir(); (review / "source").write_text("source")
                failed.register_disposable(generated)
                raise RuntimeError("bounded failure")
        except RuntimeError:
            pass
        assert failed_path is not None
        assert (failed_path / "review/source").read_text() == "source"
        assert not (failed_path / "target").exists()
        assert (failed_path / "failure.raw.log").read_text() == "RuntimeError: bounded failure\n"
        scratch.discard_exact(namespace, failed_path)

        unmarked = namespace / "dar-looking-unmarked"; unmarked.mkdir(); sentinel = unmarked / "sentinel"; sentinel.write_bytes(b"unchanged")
        old = time.time_ns() - 100 * 3600 * 1_000_000_000
        roots = []
        for index in range(4):
            item = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
            (item.path / "payload").write_bytes(b"g" * 1024 * 1024)
            item.close(); rewrite_created(item.path, old + index * 1_000_000_000); roots.append(item.path)
        outcome = scratch.garbage_collect(namespace, ttl_seconds=72 * 3600, keep=2)
        assert len(outcome.removed) == 2 and outcome.bytes_freed >= 2 * 1024 * 1024
        assert set(outcome.removed) == set(roots[:2])
        assert all(path.exists() for path in roots[2:])
        assert sentinel.read_bytes() == b"unchanged"

        for _ in range(5):
            item = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review"); item.close()
        bounded = scratch.garbage_collect(namespace, ttl_seconds=0, keep=0, max_candidates=2, max_seconds=10)
        assert bounded.bounded and bounded.scanned <= 2
        final = scratch.garbage_collect(namespace, ttl_seconds=0, keep=0, max_candidates=64, max_seconds=10)
        assert not any(path.name != "dar-looking-unmarked" for path in namespace.iterdir())
        assert final.bytes_freed >= 0

        large = scratch.OwnedScratchRoot.create(namespace=namespace, kind="agent-review")
        for index in range(200):
            (large.path / f"entry-{index}").write_bytes(b"x")
        large_path = large.path; large.close(); rewrite_created(large_path, old)
        before = time.monotonic()
        limited = scratch.garbage_collect(namespace, ttl_seconds=0, keep=0, max_seconds=0.001)
        assert time.monotonic() - before < 0.5 and limited.bounded
        assert large_path.exists() or (
            len(limited.quarantined) == 1 and limited.quarantined[0].exists()
        ), limited
        scratch.garbage_collect(namespace, ttl_seconds=0, keep=0, max_seconds=10)

        print(f"OWNED_SCRATCH_CONTRACT_VALID cases=27 synthetic_bytes={outcome.bytes_freed}")


if __name__ == "__main__":
    main()

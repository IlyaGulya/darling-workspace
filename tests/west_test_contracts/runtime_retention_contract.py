"""Host-only retention contract over synthetic, fully receipted image records.

These fixtures exercise registration, leases, pins, replacement detection,
reference census and actual byte deletion.  They do not boot, install or claim
to be a Darling guest runtime; a separate real-CMake contract covers real normal
publication.  The temporary directory is a synthetic host state root, never a
guest prefix or acceptance archive.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import runtime_retention as retention  # noqa: E402
from deploy_receipt import write_receipt  # noqa: E402


def artifact(root: Path, relative: str) -> dict:
    path = root / relative
    metadata = path.lstat()
    entry: dict = {"path": relative, "mode": stat.S_IMODE(metadata.st_mode)}
    if stat.S_ISLNK(metadata.st_mode):
        entry.update(kind="symlink", target=os.readlink(path))
    else:
        entry.update(
            kind="file",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size=metadata.st_size,
        )
    return entry


def write_image_receipt(
    image: Path, source: Path, build: Path, manifest: Path
) -> None:
    paths = []
    for directory, _, files in os.walk(image, followlinks=False):
        for name in files:
            relative = (Path(directory) / name).relative_to(image).as_posix()
            if relative != ".darling-deploy-receipt.json":
                paths.append(relative)
    for path in sorted(image.rglob("*")):
        if path.is_symlink():
            relative = path.relative_to(image).as_posix()
            if relative not in paths:
                paths.append(relative)
    paths.sort()
    payload = {
        "schema_version": 2,
        "runtime_root": str(image.resolve()),
        "workspace": {"directory": str(manifest)},
        "source": {"directory": str(source)},
        "build": {"directory": str(build)},
        "artifacts": [artifact(image, relative) for relative in paths],
    }
    write_receipt(image, payload)


def fixture_directories(temporary: Path) -> tuple[Path, Path, Path]:
    fixtures = temporary / "fixtures"
    source = fixtures / "source"
    build = fixtures / "build"
    manifest = fixtures / "manifest"
    for directory in (source, build, manifest):
        directory.mkdir(parents=True, exist_ok=True)
    return source, build, manifest


def publish(
    temporary: Path,
    state_root: Path,
    name: str,
    *,
    payload: bytes = b"published payload\n",
    prune: bool = False,
) -> Path:
    source, build, manifest = fixture_directories(temporary)
    image = state_root / "images" / name
    image.parent.mkdir(parents=True, exist_ok=True)
    lock = retention.install_lock_path(image)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch(exist_ok=True)
    env = {"DW_STATE_ROOT": str(state_root)}
    record = retention.begin_publication(
        image,
        build_directory=build,
        manifest_directory=manifest,
        source_directory=source,
        environ=env,
    )
    assert record is not None and record["state"] == "installing", record
    (image / "bin").mkdir(parents=True)
    (image / "bin" / "darling").write_bytes(payload)
    (image / "libexec" / "darling").mkdir(parents=True)
    (image / "libexec" / "darling" / "payload.txt").write_bytes(payload)
    write_image_receipt(image, source, build, manifest)
    completed = retention.complete_publication(image, environ=env, prune=prune)
    assert completed is not None and completed["state"] == "published", completed
    return image


def census_paths(temporary: Path, state_root: Path) -> tuple[Path, Path]:
    proc = temporary / f"proc-{uuid.uuid4().hex}"
    proc.mkdir()
    mountinfo = temporary / f"mountinfo-{uuid.uuid4().hex}"
    mountinfo.write_text("", encoding="utf-8")
    return proc, mountinfo


@contextlib.contextmanager
def quiet_census(temporary: Path, state_root: Path):
    proc, mountinfo = census_paths(temporary, state_root)
    with patch.object(retention, "PROC_ROOT", proc), patch.object(
        retention, "MOUNTINFO_PATH", mountinfo
    ):
        yield proc, mountinfo


def snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    result = {}
    for item in [path, *path.rglob("*")]:
        metadata = item.lstat()
        value = (
            os.readlink(item) if stat.S_ISLNK(metadata.st_mode)
            else item.read_bytes() if stat.S_ISREG(metadata.st_mode)
            else None
        )
        result[str(item.relative_to(path))] = (
            stat.S_IMODE(metadata.st_mode), stat.S_IFMT(metadata.st_mode), value,
        )
    return result


def row_for(rows: list[dict], image: Path) -> dict:
    matches = [row for row in rows if row["path"] == str(image)]
    assert len(matches) == 1, rows
    return matches[0]


def exited_process_census(temporary: Path) -> None:
    # A real unreaped child cannot retain image mappings/files. Its proc entry
    # remains, while mountinfo is no longer a usable namespace description.
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        result = subprocess.run(
            [sys.executable, "-B", "-I", str(ROOT / "west_commands/runtime_retention.py"),
             "_reference-census", str(temporary), json.dumps([str(pid)])],
            capture_output=True, text=True, check=True, timeout=10,
        )
        observed = json.loads(result.stdout)
        assert observed["denied"] == [] and observed["references"] == [], observed
    finally:
        os.waitpid(pid, 0)

    # Linux can retain a zombie group leader while another thread is alive.
    # Such a group is not proof that the image has no remaining consumers.
    leader = temporary / "thread-group-model" / "101"
    worker = leader / "task" / "102"
    worker.mkdir(parents=True)
    leader_member = leader / "task" / "101"
    leader_member.mkdir()
    (leader_member / "stat").write_text("101 (leader) Z\n")
    (leader / "stat").write_text("101 (leader) Z\n")
    (worker / "stat").write_text("102 (worker) S\n")
    try:
        retention._exited_thread_group(leader)
    except retention.RuntimeRetentionError:
        pass
    else:
        raise AssertionError("zombie leader hid a live thread from reference protection")
    (worker / "stat").write_text("102 (worker) Z\n")
    assert retention._exited_thread_group(leader)
    (leader_member / "stat").unlink()
    leader_member.rmdir()
    try:
        retention._exited_thread_group(leader)
    except retention.RuntimeRetentionError:
        pass
    else:
        raise AssertionError("incomplete zombie membership authorized collection")
    (leader / "task").rename(leader / "unavailable-members")
    try:
        retention._exited_thread_group(leader)
    except retention.RuntimeRetentionError:
        pass
    else:
        raise AssertionError("unavailable zombie thread membership authorized collection")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="west-runtime-retention-host-") as directory:
        temporary = Path(os.path.realpath(directory))
        state = temporary / "state"
        state.mkdir()
        exited_process_census(temporary)


        # Unmanaged legacy roots are never adopted, pinned or leased.
        legacy = temporary / "legacy-image"
        legacy.mkdir()
        assert retention.begin_publication(
            legacy, environ={"DW_STATE_ROOT": str(state)}
        ) is None
        assert retention.registered_runtime(legacy) is None
        assert retention.pin_runtime(legacy, "lane:/legacy") is False
        with retention.runtime_use(legacy) as registration:
            assert registration is None

        populated = state / "images" / "populated-unmanaged"
        populated.mkdir(parents=True)
        (populated / "existing").write_text("existing image bytes\n")
        try:
            retention.begin_publication(populated, environ={"DW_STATE_ROOT": str(state)})
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("registration adopted a populated unmanaged root")

        first = publish(temporary, state, "image-one")
        second = publish(temporary, state, "image-two")
        third = publish(temporary, state, "image-three")
        status = retention.registered_runtime(third)
        assert status is not None and status["state"] == "published"
        assert status["problems"] == [], status
        with retention.runtime_use(third) as registration:
            assert registration is not None and registration["root"] == str(third)
            assert registration["state"] == "published"

        # Newest two are protected without any pins.
        with quiet_census(temporary, state):
            rows = retention.prune_runtimes(state, dry_run=True)
        assert row_for(rows, first)["action"] == "would-delete", rows
        assert row_for(rows, second)["action"] == "keep", rows
        assert row_for(rows, third)["action"] == "keep", rows
        assert first.is_dir() and second.is_dir() and third.is_dir()

        # Pins protect; unpin restores eligibility.  Unregistered keys change nothing.
        pin_key = "lane:" + str(temporary / "lane-root")
        assert retention.pin_runtime(first, pin_key) is True
        assert retention.pin_runtime(first, pin_key) is True
        with quiet_census(temporary, state):
            rows = retention.prune_runtimes(state)
        assert row_for(rows, first)["action"] == "keep"
        assert retention.unpin_runtime(first, pin_key) is True
        assert retention.unpin_runtime(first, pin_key) is False

        # A corrupt catalog must not masquerade as an unregistered image and
        # silently lose the consumer's durable reference.
        catalog = Path(retention.registered_runtime(second)["record"])
        original_catalog = catalog.read_bytes()
        damaged_catalog = "{invalid owned registration\n"
        catalog.write_text(damaged_catalog)
        for update in (retention.pin_runtime, retention.unpin_runtime):
            try:
                update(second, "lane:/catalog-corruption")
            except retention.RuntimeRetentionError:
                pass
            else:
                raise AssertionError("a damaged owned registration silently lost a reference update")
        assert catalog.read_text() == damaged_catalog
        catalog.write_bytes(original_catalog)
        catalog_directory = catalog.parent
        moved_catalog = catalog_directory.with_name("catalog-detached")
        catalog_directory.rename(moved_catalog)
        catalog_directory.symlink_to(moved_catalog, target_is_directory=True)
        try:
            with retention.runtime_use(second):
                raise AssertionError("a symlinked catalog was treated as an unmanaged image")
        except retention.RuntimeRetentionError:
            pass
        finally:
            catalog_directory.unlink()
            moved_catalog.rename(catalog_directory)

        # A held use lease blocks deletion; the released lease permits real byte removal.
        with quiet_census(temporary, state):
            with retention.runtime_use(first) as registration:
                assert registration is not None
                rows = retention.prune_runtimes(state)
                assert row_for(rows, first)["action"] == "keep"
                assert first.is_dir()
            before_bytes = sum(path.stat().st_size for path in first.rglob("*") if path.is_file())
            rows = retention.prune_runtimes(state)
        deleted = row_for(rows, first)
        assert deleted["action"] == "delete", deleted
        assert deleted["bytes"] == before_bytes > 0
        assert not first.exists(), "selected image bytes were not deleted"
        assert retention.install_lock_path(first).is_file(), "install lock sidecar was removed"
        assert retention.registered_runtime(first) is None, "retired registration survived"

        # A live reader is detected by the real read-only process census and
        # blocks removal until the process exits.
        reader_victim = publish(temporary, state, "reader-victim")
        new_second = publish(temporary, state, "newest-one")
        new_third = publish(temporary, state, "newest-two")
        held = reader_victim / "libexec" / "darling" / "payload.txt"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time\n"
                "handle = open(sys.argv[1], 'rb')\n"
                "print('ready', flush=True)\n"
                "time.sleep(60)\n",
                str(held),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
            with patch.object(retention, "_privileged_reference_census", return_value=[]):
                rows = retention.prune_runtimes(state)
            kept = row_for(rows, reader_victim)
            assert kept["action"] == "keep", kept
            assert reader_victim.is_dir()
        finally:
            process.terminate()
            process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
        with patch.object(retention, "_privileged_reference_census", return_value=[]):
            rows = retention.prune_runtimes(state)
        assert row_for(rows, reader_victim)["action"] == "delete", rows
        assert not reader_victim.exists()

        # Missing or denied native inspection preserves the image and reports why.
        inspection_victim = publish(temporary, state, "inspection-victim")
        keep_one = publish(temporary, state, "kept-one")
        keep_two = publish(temporary, state, "kept-two")
        proc, mountinfo = census_paths(temporary, state)
        try:
            retention._reference_inspection(
                inspection_victim,
                proc_root=temporary / "missing-proc",
                mountinfo_path=mountinfo,
            )
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("missing /proc census was accepted")
        try:
            retention._reference_inspection(
                inspection_victim,
                proc_root=proc,
                mountinfo_path=temporary / "missing-mountinfo",
            )
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("missing mount census was accepted")
        denied_pid = proc / "424242"
        denied_pid.mkdir()
        (denied_pid / "mountinfo").write_text("", encoding="utf-8")
        with patch.object(retention, "_process_reference", side_effect=PermissionError), patch.object(
            retention, "_privileged_reference_census", return_value=None
        ), patch.object(retention, "PROC_ROOT", proc), patch.object(
            retention, "MOUNTINFO_PATH", mountinfo
        ):
            try:
                retention._reference_inspection(inspection_victim)
            except retention.RuntimeRetentionError:
                pass
            else:
                raise AssertionError("denied census without fallback was accepted")
            with patch.object(retention, "_privileged_reference_census", return_value=[]):
                assert retention._reference_inspection(inspection_victim) == []
            with patch.object(
                retention, "_privileged_reference_census", return_value=["fallback reference"]
            ):
                assert retention._reference_inspection(inspection_victim) == ["fallback reference"]
        with patch.object(retention, "_reference_inspection", side_effect=retention.RuntimeRetentionError("denied")):
            rows = retention.prune_runtimes(state)
        assert row_for(rows, inspection_victim)["action"] == "keep"
        assert inspection_victim.is_dir()

        # A replaced inode, a symlink substitution and receipt corruption are preserved.
        replaced = publish(temporary, state, "replaced-victim")
        moved = temporary / "replaced-victim-moved"
        replaced.rename(moved)
        replaced.mkdir()
        (replaced / "impostor").write_text("not the registered image\n")
        try:
            with retention.runtime_use(replaced):
                pass
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("replaced registration was usable")

        symlinked = publish(temporary, state, "symlink-victim")
        target = temporary / "symlink-target"
        target.mkdir()
        symlinked.rename(temporary / "symlink-victim-moved")
        symlinked.symlink_to(target)
        assert retention.registered_runtime(symlinked)["state"] == "invalid"
        try:
            with retention.runtime_use(symlinked):
                pass
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("symlink-replaced registration was usable")

        corrupt = publish(temporary, state, "corrupt-victim")
        publish(temporary, state, "corrupt-filler-one")
        publish(temporary, state, "corrupt-filler-two")
        (corrupt / "libexec" / "darling" / "payload.txt").write_bytes(b"tampered\n")
        with quiet_census(temporary, state):
            rows = retention.prune_runtimes(state)
        assert row_for(rows, corrupt)["action"] == "keep"
        unreceipted = publish(temporary, state, "unreceipted-victim")
        publish(temporary, state, "unreceipted-filler-one")
        publish(temporary, state, "unreceipted-filler-two")
        (unreceipted / "libexec" / "darling" / "extra.dylib").write_bytes(b"extra\n")
        with quiet_census(temporary, state):
            rows = retention.prune_runtimes(state)
        assert row_for(rows, unreceipted)["action"] == "keep"
        preserved = {
            image: snapshot(image)
            for image in (replaced, symlinked, corrupt, unreceipted)
        }
        with quiet_census(temporary, state):
            rows = retention.prune_runtimes(state)
        assert all(
            row_for(rows, image)["action"] == "keep"
            for image in preserved
        ), rows
        assert all(snapshot(image) == value for image, value in preserved.items())

        # Failed and interrupted publications are never reclaimed, even after a
        # dead parent released the installer lock.
        source, build, manifest = fixture_directories(temporary)
        partial = state / "images" / "partial-image"
        partial.parent.mkdir(parents=True, exist_ok=True)
        env = {"DW_STATE_ROOT": str(state)}
        retention.begin_publication(
            partial,
            build_directory=build,
            manifest_directory=manifest,
            source_directory=source,
            environ=env,
        )
        (partial / "libexec").mkdir(parents=True)
        (partial / "libexec" / "half-installed").write_text("incomplete\n")
        interrupted = retention.registered_runtime(partial)
        assert interrupted is not None and interrupted["state"] == "installing", interrupted
        with quiet_census(temporary, state):
            rows = retention.prune_runtimes(state)
        assert row_for(rows, partial)["action"] == "keep"
        retention.fail_publication(partial, "installer failed", environ=env)
        failed = retention.registered_runtime(partial)
        assert failed is not None and failed["state"] == "failed", failed
        assert "installer failed" in failed["error"], failed
        retention.install_lock_path(partial).unlink(missing_ok=True)
        with quiet_census(temporary, state):
            rows = retention.prune_runtimes(state)
        assert row_for(rows, partial)["action"] == "keep"
        assert partial.is_dir() and (partial / "libexec" / "half-installed").is_file()

        # Registration refuses source/build/manifest/Git roots and metadata overlap.
        for overlap_kind in ("source", "build", "manifest"):
            parent = state / f"overlap-{overlap_kind}"
            parent.mkdir()
            destination = parent / "nested-image"
            arguments = {
                "source_directory": parent if overlap_kind == "source" else None,
                "build_directory": parent if overlap_kind == "build" else None,
                "manifest_directory": parent if overlap_kind == "manifest" else None,
            }
            try:
                retention.begin_publication(destination, environ=env, **arguments)
            except retention.RuntimeRetentionError:
                pass
            else:
                raise AssertionError(f"registration accepted {overlap_kind} overlap")
        git_root = state / "images" / "git-root"
        (git_root / ".git").mkdir(parents=True)
        try:
            retention.begin_publication(git_root, environ=env)
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("registration accepted a Git worktree root")
        try:
            retention.begin_publication(state / "runtime-images" / "nested", environ=env)
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("registration accepted a metadata-owned root")

        # Unregistered images under the declared root are never touched.
        unmanaged = state / "unmanaged" / "image"
        unmanaged.mkdir(parents=True)
        (unmanaged / "payload").write_text("not registered\n")
        unmanaged_before = snapshot(unmanaged)
        with quiet_census(temporary, state):
            retention.prune_runtimes(state)
        assert snapshot(unmanaged) == unmanaged_before

        # A bind mount in another mount namespace reaches the image under a path
        # the pruner's namespace never sees.  Filesystem identity (device plus
        # the mount's filesystem root) must translate that alias into a live
        # reference instead of authorizing deletion of actively used bytes.
        alias_state = temporary / "alias-state"
        alias_state.mkdir()
        alias_old = publish(temporary, alias_state, "alias-old")
        publish(temporary, alias_state, "alias-new-one")
        publish(temporary, alias_state, "alias-new-two")
        device = os.stat(alias_old).st_dev
        major, minor = os.major(device), os.minor(device)
        own_mounts = temporary / f"alias-own-{uuid.uuid4().hex}"
        own_mounts.write_text(
            f"1 0 {major}:{minor} / / rw - ext4 /dev/root rw\n", encoding="utf-8"
        )
        alias_proc = temporary / f"alias-proc-{uuid.uuid4().hex}"
        alias_proc.mkdir()
        alias_pid = alias_proc / "9182"
        alias_pid.mkdir()
        alias_pid.joinpath("mountinfo").write_text(
            f"1 0 {major}:{minor} / / rw - ext4 /dev/root rw\n"
            f"2 1 {major}:{minor} {alias_old} /mnt/alias rw - ext4 /dev/root rw\n",
            encoding="utf-8",
        )
        (alias_pid / "cwd").symlink_to("/mnt/alias/bin")
        (alias_pid / "maps").write_text("", encoding="utf-8")
        (alias_pid / "fd").mkdir()
        (alias_pid / "fd" / "3").symlink_to("/mnt/alias/libexec/darling/payload.txt")
        with patch.object(retention, "PROC_ROOT", alias_proc), patch.object(
            retention, "MOUNTINFO_PATH", own_mounts
        ):
            rows = retention.prune_runtimes(alias_state)
        alias_row = row_for(rows, alias_old)
        assert alias_row["action"] == "keep", alias_row
        assert alias_old.is_dir(), "bind-mounted alias reference did not preserve the image"

        # A failed deletion is journaled, remains retiring (not generic failed
        # install state), and the next prune retries the verified detached bytes.
        recovery_state = temporary / "recovery-state"
        recovery_state.mkdir()
        retire_victim = publish(temporary, recovery_state, "retire-victim")
        publish(temporary, recovery_state, "retire-new-one")
        publish(temporary, recovery_state, "retire-new-two")
        with quiet_census(temporary, recovery_state), patch.object(
            retention.shutil, "rmtree", side_effect=OSError("simulated removal failure")
        ):
            rows = retention.prune_runtimes(recovery_state)
        failed = row_for(rows, retire_victim)
        assert failed["action"] == "failed", failed
        assert not retire_victim.exists()
        blocked = retention.registered_runtime(retire_victim)
        assert blocked is not None and blocked["state"] == "retiring", blocked
        trash_dir = recovery_state / "runtime-images" / "trash"
        detached = list(trash_dir.iterdir())
        assert len(detached) == 1 and detached[0].is_dir(), detached
        with quiet_census(temporary, recovery_state):
            rows = retention.prune_runtimes(recovery_state)
        recovered = row_for(rows, retire_victim)
        assert recovered["action"] == "delete", recovered
        assert not detached[0].exists()
        assert retention.registered_runtime(retire_victim) is None

        # A crash after journaling but before rename must be re-detached by the
        # recorded trash path and inode identity, never by a name guess.
        interrupted_state = temporary / "interrupted-state"
        interrupted_state.mkdir()
        interrupted_victim = publish(temporary, interrupted_state, "interrupted-victim")
        publish(temporary, interrupted_state, "interrupted-new-one")
        publish(temporary, interrupted_state, "interrupted-new-two")
        record_path = Path(retention.registered_runtime(interrupted_victim)["record"])
        interrupted_record = json.loads(record_path.read_text(encoding="utf-8"))
        interrupted_trash = interrupted_state / "runtime-images" / "trash" / (
            f"image-{retention._record_id(interrupted_victim)}-crashed"
        )
        interrupted_trash.parent.mkdir(parents=True, exist_ok=True)
        interrupted_metadata = interrupted_victim.lstat()
        interrupted_record.update(
            state="retiring",
            previous_state="published",
            trash=str(interrupted_trash),
            trash_device=interrupted_metadata.st_dev,
            trash_inode=interrupted_metadata.st_ino,
            trash_size=1234,
        )
        retention._write_json(record_path, interrupted_record)
        with quiet_census(temporary, interrupted_state):
            rows = retention.prune_runtimes(interrupted_state)
        interrupted_row = row_for(rows, interrupted_victim)
        assert interrupted_row["action"] == "delete", interrupted_row
        assert not interrupted_victim.exists()
        assert retention.registered_runtime(interrupted_victim) is None

        # A journaled path outside the owned trash directory, or one whose
        # inode no longer matches, is never deleted as if it were image bytes.
        foreign_state = temporary / "foreign-state"
        foreign_state.mkdir()
        foreign_victim = publish(temporary, foreign_state, "foreign-victim")
        publish(temporary, foreign_state, "foreign-new-one")
        publish(temporary, foreign_state, "foreign-new-two")
        record_path = Path(retention.registered_runtime(foreign_victim)["record"])
        foreign_record = json.loads(record_path.read_text(encoding="utf-8"))
        outside = temporary / "outside-trash"
        outside.mkdir()
        (outside / "keep").write_text("keep\n")
        foreign_metadata = foreign_victim.lstat()
        foreign_record.update(
            state="retiring",
            previous_state="published",
            trash=str(outside),
            trash_device=foreign_metadata.st_dev,
            trash_inode=foreign_metadata.st_ino,
            trash_size=1,
        )
        retention._write_json(record_path, foreign_record)
        with quiet_census(temporary, foreign_state):
            rows = retention.prune_runtimes(foreign_state)
        unowned = row_for(rows, foreign_victim)
        assert unowned["action"] == "keep", unowned
        assert (outside / "keep").is_file() and foreign_victim.is_dir()
        impostor = foreign_state / "runtime-images" / "trash" / (
            f"image-{retention._record_id(foreign_victim)}-impostor"
        )
        impostor.parent.mkdir(parents=True, exist_ok=True)
        impostor.mkdir()
        (impostor / "keep").write_text("keep\n")
        foreign_record["trash"] = str(impostor)
        retention._write_json(record_path, foreign_record)
        with quiet_census(temporary, foreign_state):
            rows = retention.prune_runtimes(foreign_state)
        mismatched = row_for(rows, foreign_victim)
        assert mismatched["action"] == "keep", mismatched
        assert (impostor / "keep").is_file() and foreign_victim.is_dir()

        # Run evidence and unacknowledged retirement journals under the same
        # owner root reference the image that served them. Even with no
        # registered pin, image retention refuses those bytes; removing the
        # persisted reference releases the image to the normal policy.
        reference_state = temporary / "reference-state"
        reference_state.mkdir()

        def run_reference(state_root: Path, name: str, runtime_root: Path) -> Path:
            run = state_root / "runs" / name
            run.mkdir(parents=True, exist_ok=True)
            (run / "run.json").write_text(
                json.dumps({
                    "schema": 1, "kind": "managed-lane-run", "path": str(run),
                    "root": str(state_root), "runtime_root": str(runtime_root),
                }, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return run

        referenced = publish(temporary, reference_state, "referenced")
        publish(temporary, reference_state, "reference-new-one")
        publish(temporary, reference_state, "reference-new-two")
        evidence_run = run_reference(reference_state, "run-referenced", referenced)
        with quiet_census(temporary, reference_state):
            rows = retention.prune_runtimes(reference_state)
        referenced_row = row_for(rows, referenced)
        assert referenced_row["action"] == "keep", referenced_row
        assert referenced.is_dir()
        shutil.rmtree(evidence_run)

        journal_store = reference_state / "retired-evidence"
        journal_store.mkdir(mode=0o700, exist_ok=True)
        journal_target = reference_state / "runs" / "run-journal"
        journal = journal_store / "run-journal.json"
        journal.write_text(
            json.dumps({
                "schema": 1, "kind": "managed-run-retirement", "phase": "completed",
                "root": str(reference_state), "uid": os.geteuid(),
                "run": str(journal_target),
                "runtime_pin_key": "evidence:" + str(journal_target),
                "runtime_root": str(referenced),
                "trash": str(journal_store / ".trash-run-journal-1"), "bytes": 1,
            }, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with quiet_census(temporary, reference_state):
            rows = retention.prune_runtimes(reference_state)
        journal_row = row_for(rows, referenced)
        assert journal_row["action"] == "keep", journal_row
        assert referenced.is_dir()
        # A partial journal cannot prove that its former image reference was
        # absent. Missing runtime_root differs from an explicit null value.
        partial_journal = json.loads(journal.read_text(encoding="utf-8"))
        partial_journal.pop("runtime_root")
        journal.write_text(json.dumps(partial_journal), encoding="utf-8")
        with quiet_census(temporary, reference_state):
            rows = retention.prune_runtimes(reference_state)
        assert row_for(rows, referenced)["action"] == "keep", rows
        assert referenced.is_dir()
        journal.unlink()
        with quiet_census(temporary, reference_state):
            rows = retention.prune_runtimes(reference_state)
        assert row_for(rows, referenced)["action"] == "delete", rows
        assert not referenced.exists()

        # An unclassified run entry cannot disprove references, so it preserves
        # every candidate instead of silently ignoring its runtime root.
        unknown_state = temporary / "unknown-reference-state"
        unknown_state.mkdir()
        unknown_victim = publish(temporary, unknown_state, "unknown-victim")
        publish(temporary, unknown_state, "unknown-new-one")
        publish(temporary, unknown_state, "unknown-new-two")
        (unknown_state / "runs" / "foreign").mkdir(parents=True)
        (unknown_state / "runs" / "foreign" / "note").write_text("foreign\n", encoding="utf-8")
        with quiet_census(temporary, unknown_state):
            rows = retention.prune_runtimes(unknown_state)
        unknown_row = row_for(rows, unknown_victim)
        assert unknown_row["action"] == "keep", unknown_row
        assert unknown_victim.is_dir()
        partial_run = unknown_state / "runs" / "foreign"
        (partial_run / "run.json").write_text(json.dumps({
            "schema": 1, "kind": "managed-lane-run", "path": str(partial_run),
        }), encoding="utf-8")
        with quiet_census(temporary, unknown_state):
            rows = retention.prune_runtimes(unknown_state)
        assert row_for(rows, unknown_victim)["action"] == "keep", rows
        assert unknown_victim.is_dir()
        shutil.rmtree(unknown_state / "runs")
        with quiet_census(temporary, unknown_state):
            rows = retention.prune_runtimes(unknown_state)
        assert row_for(rows, unknown_victim)["action"] == "delete", rows

        # An interrupted retirement is not resumed over a live evidence
        # reference; removing that reference lets recovery finish instead of
        # deleting the image beneath the retained evidence.
        guarded_state = temporary / "guarded-retirement-state"
        guarded_state.mkdir()
        guarded_victim = publish(temporary, guarded_state, "guarded-victim")
        publish(temporary, guarded_state, "guarded-new-one")
        publish(temporary, guarded_state, "guarded-new-two")
        record_path = Path(retention.registered_runtime(guarded_victim)["record"])
        guarded_record = json.loads(record_path.read_text(encoding="utf-8"))
        guarded_trash = guarded_state / "runtime-images" / "trash" / (
            f"image-{retention._record_id(guarded_victim)}-guarded"
        )
        guarded_trash.parent.mkdir(parents=True, exist_ok=True)
        guarded_metadata = guarded_victim.lstat()
        guarded_record.update(
            state="retiring",
            previous_state="published",
            trash=str(guarded_trash),
            trash_device=guarded_metadata.st_dev,
            trash_inode=guarded_metadata.st_ino,
            trash_size=1234,
        )
        retention._write_json(record_path, guarded_record)
        evidence_run = run_reference(guarded_state, "run-guarded", guarded_victim)
        with quiet_census(temporary, guarded_state):
            rows = retention.prune_runtimes(guarded_state)
        guarded_row = row_for(rows, guarded_victim)
        assert guarded_row["action"] == "keep", guarded_row
        assert "referenced by retained evidence" in guarded_row["reason"], guarded_row
        assert guarded_victim.is_dir()
        current = retention.registered_runtime(guarded_victim)
        assert current is not None and current["state"] == "retiring", current
        shutil.rmtree(evidence_run)
        with quiet_census(temporary, guarded_state):
            rows = retention.prune_runtimes(guarded_state)
        assert row_for(rows, guarded_victim)["action"] == "delete", rows
        assert not guarded_victim.exists()
        assert retention.registered_runtime(guarded_victim) is None

        # A successful NEW managed publication applies the scoped bounded prune
        # while its own fresh image stays protected.
        auto_state = temporary / "auto-state"
        auto_state.mkdir()
        auto_one = publish(temporary, auto_state, "auto-one")
        auto_two = publish(temporary, auto_state, "auto-two")
        with quiet_census(temporary, auto_state):
            auto_three = publish(temporary, auto_state, "auto-three", prune=True)
        assert not auto_one.exists(), "new publication did not apply bounded retention"
        assert retention.registered_runtime(auto_one) is None
        assert auto_two.is_dir() and auto_three.is_dir()

        # Admission uses the declared owner scope and can be configured for
        # tests without inventing a fake state identity; refusal happens before
        # any candidate bytes or ownership marker are created.
        budget_candidate = state / "images" / "budget-candidate"
        assert retention.require_publication_budget(budget_candidate, environ={}) is None
        budget = retention.require_publication_budget(
            budget_candidate,
            environ={"DW_STATE_ROOT": str(state)},
            floor_bytes=0,
        )
        assert budget is not None and budget["ok"] is True, budget
        try:
            retention.require_publication_budget(
                budget_candidate,
                environ={"DW_STATE_ROOT": str(state)},
                floor_bytes=1 << 60,
            )
        except retention.RuntimeRetentionError:
            pass
        else:
            raise AssertionError("managed allocation ignored the free-space floor")
        assert not budget_candidate.exists()

        from runtime_install import RuntimeInstallationError, install_runtime

        fake_build = temporary / "budget-build"
        fake_build.mkdir()
        (fake_build / "CMakeCache.txt").write_text(
            f"CMAKE_INSTALL_PREFIX:PATH={budget_candidate}\n"
            f"CMAKE_HOME_DIRECTORY:INTERNAL={temporary / 'fake-source'}\n",
            encoding="utf-8",
        )
        fake_manifest = temporary / "budget-manifest"
        fake_manifest.mkdir()
        with patch.dict(os.environ, {"DW_STATE_ROOT": str(state)}), patch.object(
            retention, "DEFAULT_FREE_FLOOR", 1 << 60
        ):
            try:
                install_runtime(fake_build, budget_candidate, manifest_repo=fake_manifest)
            except RuntimeInstallationError:
                pass
            else:
                raise AssertionError("installer ignored the managed free-space floor")
        assert not budget_candidate.exists()
        assert retention.registered_runtime(budget_candidate) is None

        # An explicit runtime object for an unregistered root is preserved, not
        # re-resolved through a receipt requirement (host protocol fixtures).
        explicit = types.SimpleNamespace(root=legacy)
        with retention.leased_runtime(runtime=explicit) as leased:
            assert leased is explicit

        # A publication appearing during a legacy lookup must gain a real use
        # lease before the consumer runs, not become a stale no-op guard.
        late_state = temporary / "late-publication-state"
        late_state.mkdir()
        late_runtime = types.SimpleNamespace(root=late_state / "images" / "late-reader")
        original_use = retention.runtime_use
        first_lookup = True

        @contextlib.contextmanager
        def publish_during_lookup(root):
            nonlocal first_lookup
            with original_use(root) as registration:
                if first_lookup:
                    first_lookup = False
                    publish(temporary, late_state, "late-reader")
                    publish(temporary, late_state, "late-newer-one")
                    publish(temporary, late_state, "late-newer-two")
                yield registration

        installation = retention._runtime_install_module()
        with patch.object(retention, "runtime_use", publish_during_lookup), patch.object(
            installation, "resolve_runtime", lambda selected: late_runtime,
        ), quiet_census(temporary, late_state):
            with retention.leased_runtime(runtime=late_runtime):
                rows = retention.prune_runtimes(late_state)
                assert row_for(rows, late_runtime.root)["action"] == "keep"
                assert (late_runtime.root / "libexec/darling/payload.txt").read_bytes() == b"published payload\n"
            rows = retention.prune_runtimes(late_state)
            assert row_for(rows, late_runtime.root)["action"] == "delete"
            assert not late_runtime.root.exists()

    print(
        "PASS host-only runtime-retention contract "
        "(synthetic receipted images; no guest boot or installed-runtime acceptance)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Behavioral contract for the five-unit Darling handoff transaction."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import handoff_transaction as transaction
import handoff


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def init_origin(root: Path, name: str) -> tuple[Path, Path]:
    origin = root / f"{name}.git"
    work = root / f"{name}-seed"
    git(root, "init", "--bare", "-q", str(origin))
    git(root, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.name", "Contract")
    git(work, "config", "user.email", "contract@example.invalid")
    (work / "tracked").write_text(f"{name} base\n")
    git(work, "add", "tracked")
    git(work, "commit", "-qm", "base")
    git(work, "branch", "-M", "main")
    git(work, "push", "-q", "-u", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, work


def fixture(root: Path) -> tuple[Path, Path, Path]:
    child_origin, child_seed = init_origin(root, "child")
    git(child_seed, "checkout", "-qb", "fix/child")
    (child_seed / "private").write_text("child private\n")
    git(child_seed, "add", "private")
    git(child_seed, "commit", "-qm", "child private")
    git(child_seed, "push", "-q", "origin", "fix/child")

    root_origin, root_seed = init_origin(root, "root")
    source = root / "source"
    git(root, "clone", "-q", str(root_origin), str(source))
    git(source, "config", "user.name", "Contract")
    git(source, "config", "user.email", "contract@example.invalid")
    git(source, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child_origin), "modules/child")
    git(source, "commit", "-qm", "add child")
    git(source, "push", "-q", "origin", "main")
    git(source, "checkout", "-qb", "fix/root")
    (source / "private").write_text("root private\n")
    git(source, "add", "private")
    git(source, "commit", "-qm", "root private")
    child = source / "modules" / "child"
    git(child, "fetch", "-q", "origin", "fix/child")
    git(child, "checkout", "-q", "-b", "fix/child", "origin/fix/child")

    control = root / "control"
    (child / "local-only").write_text("unpublished child commit\n")
    git(child, "add", "local-only")
    git(child, "commit", "-qm", "unpublished child commit")
    (control / ".beads").mkdir(parents=True)
    (control / ".beads" / "issues.jsonl").write_text("old beads\n")
    (control / "state").mkdir()
    (control / "state" / "repos.tsv").write_text("old state\n")
    (control / "locked.xml").write_text("old locked\n")
    (control / "base.xml").write_text("old base\n")
    (control / "handoff").mkdir()
    (control / "handoff" / "manifest.json").write_text('{"old":true}\n')
    (control / "handoff" / "root.bundle").write_text("old root bundle\n")
    (control / "handoff" / "stale.bundle").write_text("must survive failed transactions\n")
    for path in transaction.PUBLICATION_PATHS:
        target = control / path
        os.chmod(target, 0o640 if target.is_file() else 0o750)
    return control, source, child


def tree_snapshot(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    result = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            result.append((relative, "symlink", mode, os.readlink(path)))
        elif path.is_dir():
            result.append((relative, "directory", mode, ""))
        else:
            result.append((relative, "file", mode, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(result)


def source_state(source: Path, child: Path) -> tuple[str, ...]:
    return (
        git(source, "rev-parse", "HEAD"),
        git(child, "rev-parse", "HEAD"),
        git(source, "show-ref"),
        git(child, "show-ref"),
        git(source, "status", "--porcelain=v1"),
        git(child, "status", "--porcelain=v1"),
    )


def assert_clean_external(control: Path) -> None:
    state = control.parent / f".{control.name}.handoff-transaction"
    assert not (state / "journal.json").exists()
    assert not (state / "journal.json.tmp").exists()
    assert not (state / "backups").exists()
    assert not (state / "displaced").exists()
    assert not list(control.parent.glob(f".{control.name}.handoff-stage-*"))


def invoke(control: Path, source: Path, **kwargs):
    return transaction.execute(
        control,
        source,
        kwargs.get("allow_dirty", False),
        kwargs.get("dry_run", False),
        kwargs.get("as_json", False),
    )


def independent_west_clones(root: Path) -> None:
    root.mkdir()
    control, source, child = fixture(root)
    saved_child = root / "saved-child"
    git(root, "clone", "-q", str(child), str(saved_child))
    git(source, "submodule", "deinit", "-q", "-f", "modules/child")
    child.rmdir()
    git(root, "clone", "-q", str(saved_child), str(child))
    git(child, "config", "user.name", "Contract")
    git(child, "config", "user.email", "contract@example.invalid")
    grand_origin, _ = init_origin(root, "grandchild")
    git(child, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(grand_origin), "nested/grandchild")
    git(child, "commit", "-qm", "declare nested repository")
    grandchild = child / "nested/grandchild"
    git(child, "submodule", "deinit", "-q", "-f", "nested/grandchild")
    grandchild.rmdir()
    git(root, "clone", "-q", str(grand_origin), str(grandchild))
    docs = source / "docs/manual"
    git(root, "clone", "-q", str(grand_origin), str(docs))
    git(docs, "branch", "manifest-rev", "main")
    repositories = {".": source, "modules/child": child,
                    "modules/child/nested/grandchild": grandchild, "docs/manual": docs}
    for repo in repositories.values():
        git(repo, "remote", "rename", "origin", "darling")
    os.environ[transaction.EXPECTED_PROJECTS_ENV] = json.dumps(list(repositories))
    before = tree_snapshot(control)
    source_before = source_state(source, child)
    plan = transaction.build_plan(control, source, False)
    assert {item.relative: item.head for item in plan.projects} == {
        name: git(repo, "rev-parse", "HEAD") for name, repo in repositories.items()
    }
    assert all(not item.dirty for item in plan.projects)
    assert tree_snapshot(control) == before
    assert source_state(source, child) == source_before

    # Registration is not existence: a real unlisted nested checkout must not
    # vanish from the handoff when recursion stops at an unregistered parent.
    os.environ[transaction.EXPECTED_PROJECTS_ENV] = json.dumps(
        [name for name in repositories if name != "modules/child/nested/grandchild"]
    )
    try:
        transaction.build_plan(control, source, False)
        raise AssertionError("populated nested repository omitted from handoff")
    except transaction.HandoffError:
        pass
    os.environ[transaction.EXPECTED_PROJECTS_ENV] = json.dumps(list(repositories))
    saved_grandchild = root / "saved-grandchild"
    grandchild.rename(saved_grandchild)
    grandchild.mkdir()
    try:
        transaction.build_plan(control, source, False)
        raise AssertionError("parent repository accepted as missing child worktree")
    except transaction.HandoffError:
        pass
    grandchild.rmdir()
    saved_grandchild.rename(grandchild)
    (source / "docs/unmanaged").write_text("not a managed repository\n")
    try:
        transaction.build_plan(control, source, False)
        raise AssertionError("unmanaged sibling dirt hidden by managed repository")
    except transaction.HandoffError:
        pass
    (source / "docs/unmanaged").unlink()
    assert tree_snapshot(control) == before
    with contextlib.redirect_stdout(io.StringIO()):
        invoke(control, source)
    manifest = json.loads((control / "handoff/manifest.json").read_text())
    assert [item["path"] for item in manifest["projects"]] == [
        ".", "modules/child", "docs/manual"
    ]
    assert {
        item.attrib["path"]: item.attrib["revision"]
        for item in ET.parse(control / "locked.xml").getroot().findall("project")
    } == {
        ("darling" if name == "." else f"darling/{name}"): git(repo, "rev-parse", "HEAD")
        for name, repo in repositories.items()
    }
    assert handoff.bundle_heads(control / "handoff/modules__child.bundle")[
        "refs/heads/fix/child"
    ] == git(child, "rev-parse", "HEAD")
    assert handoff.bundle_heads(control / "handoff/docs__manual.bundle")[
        "refs/heads/manifest-rev"
    ] == git(docs, "rev-parse", "main")
    assert source_state(source, child) == source_before
    assert_clean_external(control)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        control, source, child = fixture(root)
        tools = root / "tools"
        tools.mkdir()
        br_log = root / "br.log"
        br = tools / "br"
        br.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "[[ ${1:-} == sync && ${2:-} == --flush-only ]]\n"
            "[[ -n ${BEADS_DIR:-} && $BEADS_DIR != */control/.beads ]]\n"
            "if [[ -n ${BR_LOG:-} ]]; then printf 'flush:%s\\n' \"$BEADS_DIR\" >>\"$BR_LOG\"; fi\n"
            "if [[ ${BR_FAIL:-0} == 1 ]]; then exit 23; fi\n"
            "printf 'flushed\\n' >>\"$BEADS_DIR/issues.jsonl\"\n"
        )
        br.chmod(0o755)
        os.environ["PATH"] = f"{tools}:{os.environ['PATH']}"
        independent_west_clones(root / "west-clones")
        os.environ[transaction.EXPECTED_PROJECTS_ENV] = json.dumps([".", "modules/child"])
        baseline = tree_snapshot(control)
        refs_before = source_state(source, child)

        # Authoritative closure rejects both the historical root-only/old-many-bundles
        # case and a deinitialized partial forest before Beads or a destination changes.
        os.environ[transaction.EXPECTED_PROJECTS_ENV] = json.dumps([".", "missing/project"])
        try:
            invoke(control, source)
            raise AssertionError("missing authoritative project accepted")
        except transaction.HandoffError:
            pass
        assert tree_snapshot(control) == baseline
        os.environ[transaction.EXPECTED_PROJECTS_ENV] = json.dumps([".", "modules/child"])
        git(source, "submodule", "deinit", "-q", "-f", "modules/child")
        try:
            invoke(control, source)
            raise AssertionError("uninitialized project accepted")
        except transaction.HandoffError:
            pass
        assert tree_snapshot(control) == baseline
        git(source, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init", "modules/child")
        child = source / "modules" / "child"
        git(child, "checkout", "-q", "fix/child")

        # Human and JSON plans are deterministic and do not invoke br or touch the
        # Pure planning does not even create the external lock/state directory.
        external_state = control.parent / f".{control.name}.handoff-transaction"
        if external_state.exists():
            import shutil
            shutil.rmtree(external_state)
        state_link_target = root / "hostile-state-target"
        state_link_target.mkdir()
        external_state.symlink_to(state_link_target, target_is_directory=True)
        try:
            invoke(control, source, dry_run=True)
            raise AssertionError("symlinked transaction state accepted")
        except transaction.HandoffError:
            pass
        external_state.unlink()
        external_state.mkdir(mode=0o755)
        external_state.chmod(0o755)
        try:
            invoke(control, source, dry_run=True)
            raise AssertionError("group/world-accessible transaction state accepted")
        except transaction.HandoffError:
            pass
        external_state.chmod(0o700)
        victim = root / "journal-victim"
        victim.write_text("untouched\n")
        (external_state / "journal.json").symlink_to(victim)
        try:
            invoke(control, source, dry_run=True)
            raise AssertionError("symlinked transaction journal accepted")
        except transaction.HandoffError:
            pass
        assert victim.read_text() == "untouched\n"
        (external_state / "journal.json").unlink()
        hostile_entries = [
            {
                "relative": relative,
                "backup": str(root / "attacker-backup"),
                "displaced": str(root / "attacker-displaced"),
            }
            for relative in transaction.PUBLICATION_PATHS
        ]
        try:
            transaction._journal_entries(
                {"version": 1, "control": str(control), "entries": hostile_entries},
                control,
                external_state,
            )
            raise AssertionError("hostile absolute journal paths accepted")
        except transaction.HandoffError:
            pass
        import shutil
        shutil.rmtree(external_state)

        index = source / ".git" / "index"
        index_before = (index.read_bytes(), index.stat().st_mtime_ns)


        # tracked or untracked control-tree snapshot.
        os.environ["BR_LOG"] = str(br_log)
        human = io.StringIO()
        with contextlib.redirect_stdout(human):
            invoke(control, source, dry_run=True)
        assert "projects: 2" in human.getvalue()
        assert "bundles: 2" in human.getvalue()
        assert "dirty override: disabled" in human.getvalue()
        assert "remove: handoff/stale.bundle" in human.getvalue()
        assert "next safe action: west dw handoff" in human.getvalue()
        outputs = []
        for _ in range(2):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                invoke(control, source, as_json=True)
            outputs.append(stream.getvalue())
        assert outputs[0] == outputs[1]
        payload = json.loads(outputs[0])
        assert payload["project_count"] == 2 and payload["bundle_count"] == 2
        assert payload["actions"] == {
            "add": ["handoff/modules__child.bundle"],
            "replace": [
                ".beads/issues.jsonl",
                "state/repos.tsv",
                "locked.xml",
                "base.xml",
                "handoff/manifest.json",
                "handoff/root.bundle",
            ],
            "remove": ["handoff/stale.bundle"],
        }
        assert not br_log.exists() and tree_snapshot(control) == baseline
        assert (index.read_bytes(), index.stat().st_mtime_ns) == index_before

        # State creation is durably ordered before the lock can be opened.
        ordering = []
        original_fsync_dir = transaction._fsync_dir
        original_open_lock = transaction._open_lock
        def record_fsync(path):
            if path == control.parent:
                ordering.append("parent-fsync")
            return original_fsync_dir(path)
        def reject_lock(path):
            assert ordering == ["parent-fsync"]
            raise transaction.HandoffError("stop after durable state setup")
        transaction._fsync_dir = record_fsync
        transaction._open_lock = reject_lock
        try:
            try:
                invoke(control, source)
            except transaction.HandoffError:
                pass
        finally:
            transaction._fsync_dir = original_fsync_dir
            transaction._open_lock = original_open_lock
        shutil.rmtree(external_state)

        bounded = transaction.run_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * (5 * 1024 * 1024))",
            ]
        )
        assert bounded.overflow
        large_artifact = root / "large-child-artifact"
        artifact_result = transaction.run_bounded(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib,sys; "
                    "pathlib.Path(sys.argv[1]).write_bytes(b'x' * (5 * 1024 * 1024)); "
                    "print('artifact-written')"
                ),
                str(large_artifact),
            ]
        )
        assert not artifact_result.overflow
        assert artifact_result.returncode == 0
        assert artifact_result.stdout == "artifact-written\n"
        assert large_artifact.stat().st_size == 5 * 1024 * 1024
        assert not external_state.exists()

        # Symlinked mutable roots and parent components are rejected read-only.
        beads_real = control / ".beads-real"
        (control / ".beads").rename(beads_real)
        (control / ".beads").symlink_to(beads_real, target_is_directory=True)
        try:
            invoke(control, source, dry_run=True)
            raise AssertionError("symlinked Beads root accepted")
        except transaction.HandoffError:
            pass
        (control / ".beads").unlink()
        beads_real.rename(control / ".beads")
        state_real = control / "state-real"
        (control / "state").rename(state_real)
        (control / "state").symlink_to(state_real, target_is_directory=True)
        try:
            invoke(control, source, dry_run=True)
            raise AssertionError("symlinked publication parent accepted")
        except transaction.HandoffError:
            pass
        (control / "state").unlink()
        state_real.rename(control / "state")
        assert tree_snapshot(control) == baseline


        # A real br failure and a strict bundle-generation failure preserve every
        # old bundle, including stale files, and leave no staging/backup state.
        os.environ["BR_FAIL"] = "1"
        try:
            invoke(control, source)
            raise AssertionError("br failure accepted")
        except transaction.HandoffError:
            pass
        del os.environ["BR_FAIL"]
        original_writer = transaction.write_package
        transaction.write_package = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected git bundle create failure")
        )
        try:
            try:
                invoke(control, source)
                raise AssertionError("bundle failure accepted")
            except RuntimeError:
                pass
        finally:
            transaction.write_package = original_writer
        assert tree_snapshot(control) == baseline
        assert (control / "handoff" / "stale.bundle").read_text() == "must survive failed transactions\n"
        assert_clean_external(control)
        # With no journal, lock-protected orphan scratch/backups are provably
        # pre-publication and are removed before a new staging attempt.
        external_state.mkdir(exist_ok=True)
        (external_state / "backups").mkdir()
        (external_state / "displaced").mkdir()
        orphan = control.parent / f".{control.name}.handoff-stage-orphan"
        orphan.mkdir()
        os.environ["DW_HANDOFF_FAIL_AT"] = "stage:beads-copy"
        try:
            invoke(control, source)
            raise AssertionError("injected staging failure accepted")
        except transaction.HandoffError:
            pass
        finally:
            del os.environ["DW_HANDOFF_FAIL_AT"]
        assert_clean_external(control)

        # Source identity and refs are re-read after staging; any drift aborts
        # before a publication journal exists.
        original_stage = transaction.stage
        def drifting_stage(plan, scratch):
            result = original_stage(plan, scratch)
            (source / "stage-drift").write_text("drift\n")
            return result
        transaction.stage = drifting_stage
        try:
            try:
                invoke(control, source)
                raise AssertionError("source drift accepted")
            except transaction.HandoffError:
                pass
        finally:
            transaction.stage = original_stage
            (source / "stage-drift").unlink()
        assert tree_snapshot(control) == baseline
        assert_clean_external(control)

        # Standalone pack retains the prior directory if the staged publication
        # rename fails after displacement.
        original_replace = handoff.os.replace
        replace_calls = 0
        def failing_second_replace(source_path, destination_path):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 3:
                raise OSError("injected pack rename failure")
            return original_replace(source_path, destination_path)
        handoff.os.replace = failing_second_replace
        try:
            try:
                handoff.pack(source, control / "handoff")
                raise AssertionError("standalone pack rename failure accepted")
            except OSError:
                pass
        finally:
            handoff.os.replace = original_replace
        assert tree_snapshot(control) == baseline
        crash_pack_env = os.environ.copy()
        crash_pack_env["DW_HANDOFF_PACK_CRASH_AFTER_DISPLACE"] = "1"
        crashed_pack = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "handoff.py"),
                "pack",
                "--source",
                str(source),
                "--bundles",
                str(control / "handoff"),
            ],
            env=crash_pack_env,
            check=False,
        )
        assert crashed_pack.returncode == 87
        pack_state = handoff._pack_transaction_paths(control / "handoff")[0]
        assert (pack_state / "journal.json").exists()
        original_writer = handoff.write_package
        handoff.write_package = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after pack recovery")
        )
        try:
            try:
                handoff.pack(source, control / "handoff")
            except RuntimeError:
                pass
        finally:
            handoff.write_package = original_writer
        assert tree_snapshot(control) == baseline
        assert not (pack_state / "journal.json").exists()
        assert not (pack_state / "backup").exists()
        assert not (pack_state / "staged").exists()
        pack_order = []
        original_pack_fsync = handoff._fsync_directory
        original_pack_recover = handoff._recover_pack
        def record_pack_fsync(path):
            if path == pack_state.parent:
                pack_order.append("state-parent-fsync")
            return original_pack_fsync(path)
        def stop_at_pack_recovery(*_args):
            assert pack_order == ["state-parent-fsync"]
            raise RuntimeError("stop after durable existing-state setup")
        handoff._fsync_directory = record_pack_fsync
        handoff._recover_pack = stop_at_pack_recovery
        try:
            try:
                handoff.pack(source, control / "handoff")
            except RuntimeError:
                pass
        finally:
            handoff._fsync_directory = original_pack_fsync
            handoff._recover_pack = original_pack_recover
        assert tree_snapshot(control) == baseline


        # Every pre-commit publication boundary reverses both ordinary exceptions
        # and KeyboardInterrupt without changing paths, types, modes, or hashes.
        boundaries = ["publish:prepared"]
        boundaries += [f"publish:before:{index}" for index in range(5)]
        boundaries += [f"publish:displaced:{index}" for index in range(5)]
        boundaries += [f"publish:after:{index}" for index in range(5)]
        for variable, expected in (
            ("DW_HANDOFF_FAIL_AT", transaction.HandoffError),
            ("DW_HANDOFF_INTERRUPT_AT", KeyboardInterrupt),
        ):
            for boundary in boundaries:
                os.environ[variable] = boundary
                try:
                    invoke(control, source)
                    raise AssertionError(f"{variable} at {boundary} accepted")
                except expected:
                    pass
                finally:
                    del os.environ[variable]
                assert tree_snapshot(control) == baseline
                assert source_state(source, child) == refs_before
                assert_clean_external(control)

        # A destination changed after backup is never overwritten. Once the
        # concurrent value is removed, the prepared journal remains recoverable.
        original_fault = transaction._fault
        def destination_drift(label):
            if label == "publish:before:0":
                (control / ".beads" / "issues.jsonl").write_text("concurrent\n")
            original_fault(label)
        transaction._fault = destination_drift
        try:
            try:
                invoke(control, source)
                raise AssertionError("destination drift accepted")
            except transaction.HandoffError:
                pass
        finally:
            transaction._fault = original_fault
        assert (external_state / "journal.json").exists()
        (control / ".beads" / "issues.jsonl").write_text("old beads\n")
        os.chmod(control / ".beads" / "issues.jsonl", 0o640)
        transaction.recover(control, external_state, external_state / "journal.json")
        assert tree_snapshot(control) == baseline
        assert_clean_external(control)

        # An uncatchable interruption leaves a durable pre-commit journal. The next
        # invocation recovers idempotently before planning and preserves old state.
        crash_env = os.environ.copy()
        crash_env["DW_HANDOFF_CRASH_AT"] = "publish:after:2"
        crashed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "handoff_transaction.py"),
                "--control-root",
                str(control),
                "--source",
                str(source),
            ],
            env=crash_env,
            check=False,
        )
        assert crashed.returncode == 86
        state = control.parent / f".{control.name}.handoff-transaction"
        journal = json.loads((state / "journal.json").read_text())
        (state / "journal.json.tmp").write_text("stale interrupted write\n")
        try:
            invoke(control, source, dry_run=True)
            raise AssertionError("read-only invocation recovered a prepared journal")
        except transaction.HandoffError as error:
            assert "requires recovery" in str(error)
        assert (state / "journal.json").exists()
        os.environ["DW_HANDOFF_FAIL_AT"] = "stage:beads-copy"
        try:
            invoke(control, source)
            raise AssertionError("post-recovery injected failure accepted")
        except transaction.HandoffError:
            pass
        finally:
            del os.environ["DW_HANDOFF_FAIL_AT"]
        assert tree_snapshot(control) == baseline
        assert_clean_external(control)
        with contextlib.redirect_stdout(io.StringIO()):
            invoke(control, source, dry_run=True)
        assert_clean_external(control)

        # Success replaces the complete package, removes stale bundles, keeps all
        # source refs/status unchanged, and reports the compatibility lines.
        success = io.StringIO()
        with contextlib.redirect_stdout(success):
            invoke(control, source)
        text = success.getvalue()
        assert "packed 2 repositories into" in text
        assert f"wrote {control / 'state' / 'repos.tsv'}" in text
        assert f"wrote {control / 'locked.xml'}" in text
        assert f"wrote {control / 'base.xml'}" in text
        assert not (control / "handoff" / "stale.bundle").exists()
        manifest = json.loads((control / "handoff" / "manifest.json").read_text())
        assert [item["path"] for item in manifest["projects"]] == [".", "modules/child"]
        assert {path.name for path in (control / "handoff").iterdir()} == {
            "manifest.json", "root.bundle", "modules__child.bundle"
        }
        published = tree_snapshot(control)
        assert published != baseline
        assert all(record[1] in {"file", "directory"} for record in published)
        assert source_state(source, child) == refs_before
        assert_clean_external(control)

        # Real file dirt is rejected by default; the explicit override is visible
        # and records dirty state without altering the source worktree.
        (source / "untracked-contract-file").write_text("dirty\n")
        before_dirty_attempt = tree_snapshot(control)
        try:
            invoke(control, source)
            raise AssertionError("dirty source accepted without override")
        except transaction.HandoffError as error:
            assert "--allow-dirty" in str(error)
        assert tree_snapshot(control) == before_dirty_attempt
        override = io.StringIO()
        with contextlib.redirect_stdout(override):
            invoke(control, source, allow_dirty=True, dry_run=True)
        assert "dirty override: enabled" in override.getvalue()
        assert "worktree" in (control / "state" / "repos.tsv").read_text()
        assert_clean_external(control)
    print("PASS handoff-transaction-contract")


if __name__ == "__main__":
    main()

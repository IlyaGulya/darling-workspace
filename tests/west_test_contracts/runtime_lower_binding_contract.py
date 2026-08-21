"""Production deployment contract for the retained runtime lower-root binding."""

from __future__ import annotations

import json
import fcntl
import errno
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.deploy_transaction import (  # noqa: E402
    RUNTIME_LOWER_BINDING_NAME,
    DeploymentTransaction,
    DeploymentTransactionError,
)


def prefix_fixture(root: Path) -> tuple[Path, Path]:
    prefix = root / "prefix"
    lower = prefix / "libexec/darling"
    controller = prefix / "bin/darlingserver"
    lower.mkdir(parents=True)
    controller.parent.mkdir(parents=True)
    controller.write_bytes(b"exact deployed darlingserver\n")
    controller.chmod(0o755)
    (prefix / ".lifecycle.lock").write_bytes(b"")
    (prefix / ".lifecycle.lock").chmod(0o600)
    return prefix, lower


def parse_binding(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "DARLING_RUNTIME_LOWER_BINDING_V1"
    return dict(line.split("=", 1) for line in lines[1:])


def run_umask_case(value: int) -> None:
    os.umask(value)
    with tempfile.TemporaryDirectory(prefix="runtime-lower-umask-") as temp:
        root = Path(temp)
        prefix = root / "prefix"
        prefix.mkdir()
        lower_source = root / "dyld"
        controller_source = root / "darlingserver"
        lower_source.write_bytes(b"exact deployed dyld\n")
        controller_source.write_bytes(b"exact deployed darlingserver\n")
        controller_source.chmod(0o755)
        transaction = DeploymentTransaction(
            root / "manifest.json", prefix, normalize_modes=True
        )
        transaction.replace(lower_source, prefix / "libexec/darling/usr/lib/dyld")
        transaction.replace(controller_source, prefix / "bin/darlingserver")
        (prefix / ".lifecycle.lock").write_bytes(b"")
        (prefix / ".lifecycle.lock").chmod(0o600)
        lower = prefix / "libexec/darling"
        binding = transaction.bind_runtime_lower_root(prefix_generation=41)
        fields = parse_binding(binding)
        lower_stat = lower.stat()
        assert int(fields["lower_uid"]) == os.geteuid()
        assert int(fields["lower_gid"]) == os.getegid()
        assert int(fields["lower_mode"]) == stat.S_IMODE(lower_stat.st_mode)
        assert int(fields["lower_mode"]) == 0o755
        assert fields["destination"] == "libexec/darling"
        assert fields["controller_destination"] == "bin/darlingserver"
        assert binding.stat().st_mode & 0o777 == 0o600
        manifest = json.loads((root / "manifest.json").read_text())
        assert manifest["version"] == 2
        assert manifest["transaction_id"] == fields["transaction_id"]
        assert manifest["runtime_lower_binding"] == {
            key: int(value) if key in {
                "schema_version", "prefix_generation", "prefix_device", "prefix_inode",
                "lower_device", "lower_inode", "lower_mode", "lower_uid", "lower_gid",
                "controller_device", "controller_inode", "controller_mode",
                "controller_uid", "controller_gid",
            } else value
            for key, value in fields.items()
            if key != "transaction_id"
        } | {"transaction_id": fields["transaction_id"]}
        transaction.rollback()
        assert not binding.exists()


if len(sys.argv) == 3 and sys.argv[1] == "--umask-case":
    run_umask_case(int(sys.argv[2], 8))
    raise SystemExit(0)

for mask in (0o002, 0o077):
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--umask-case", oct(mask)],
        check=True,
        timeout=20,
    )

with tempfile.TemporaryDirectory(prefix="runtime-lower-negatives-") as temp:
    root = Path(temp)
    prefix, lower = prefix_fixture(root)
    transaction = DeploymentTransaction(root / "manifest.json", prefix)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=0)
    except DeploymentTransactionError:
        pass
    else:
        raise AssertionError("zero generation accepted")

    retained = prefix / "libexec/lower-retained"
    lower.rename(retained)
    lower.symlink_to(retained, target_is_directory=True)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=1)
    except OSError:
        pass
    else:
        raise AssertionError("symlink runtime lower root accepted")

with tempfile.TemporaryDirectory(prefix="runtime-lower-rollback-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)
    manifest = root / "manifest.json"
    transaction = DeploymentTransaction(manifest, prefix)
    binding = transaction.bind_runtime_lower_root(prefix_generation=9)
    transaction.commit()
    retained = root / "retained-binding"
    binding.rename(retained)
    replacement = binding
    replacement.write_bytes(retained.read_bytes())
    replacement.chmod(0o600)
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    try:
        DeploymentTransaction.restore(manifest, prefix)
    except DeploymentTransactionError as error:
        assert "replacement preserved" in str(error)
        assert len(error.recovery_obligations) == 1
        obligation = error.recovery_obligations[0]
        assert (obligation.device, obligation.inode) == replacement_identity
        assert error.recovery_owner is not None
        error.recovery_owner.close()
    else:
        raise AssertionError("rollback removed a replacement inode")
    # No namespace mutation is needed for an already-replaced destination;
    # the exact public replacement stays retained by the recovery owner.
    assert (replacement.stat().st_dev, replacement.stat().st_ino) == replacement_identity
    assert replacement.read_bytes() == retained.read_bytes()

with tempfile.TemporaryDirectory(prefix="runtime-lower-post-publish-fault-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)

    class PostPublishFault(DeploymentTransaction):
        def _write(self, state: str, **kwargs: object) -> None:
            if (
                not getattr(self, "_faulted", False)
                and self.entries
                and self.entries[-1].destination.endswith(RUNTIME_LOWER_BINDING_NAME)
                and any(
                    obligation.phase == "final"
                    for obligation in self.runtime_lower_recovery
                )
            ):
                self._faulted = True
                raise DeploymentTransactionError("injected post-publish manifest fault")
            super()._write(state, **kwargs)

    transaction = PostPublishFault(root / "manifest.json", prefix)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=12)
    except DeploymentTransactionError as error:
        assert "post-publish" in str(error)
        transaction.rollback()
    else:
        raise AssertionError("post-publish fault did not fire")
    assert not (prefix / RUNTIME_LOWER_BINDING_NAME).exists()


def assert_preserved(path: Path, identity: tuple[int, int], content: bytes) -> None:
    observed = path.stat()
    assert (observed.st_dev, observed.st_ino) == identity
    assert path.read_bytes() == content


def fd_census() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


def assert_contender(prefix: Path, *, blocked: bool) -> None:
    contender = os.open(
        prefix / ".lifecycle.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            assert blocked
        else:
            assert not blocked
            fcntl.flock(contender, fcntl.LOCK_UN)
    finally:
        os.close(contender)


def close_recovery(transaction: DeploymentTransaction) -> None:
    owner = transaction.runtime_lower_recovery_owner
    assert owner is not None
    owner.close()
    owner.close()


class NamespaceRace(DeploymentTransaction):
    checkpoint: str
    replacement: bytes
    saved: Path | None
    split_lock: bool

    def _runtime_lower_fault(self, checkpoint: str, parent_fd: int, name: str) -> None:
        if checkpoint != self.checkpoint or getattr(self, "_race_fired", False):
            return
        self._race_fired = True
        prefix = self.prefix
        if self.split_lock:
            (prefix / ".lifecycle.lock").rename(prefix / ".lifecycle.lock.retained")
            (prefix / ".lifecycle.lock").write_bytes(b"split\n")
            return
        target = prefix / (
            RUNTIME_LOWER_BINDING_NAME if checkpoint == "before_backup_restore" else name
        )
        if target.exists():
            self.saved = prefix / f"{name}.retained-race"
            target.rename(self.saved)
        target.write_bytes(self.replacement)
        target.chmod(0o600)


def race_transaction(root: Path, checkpoint: str, *, existing: bytes | None = None,
                     split_lock: bool = False) -> NamespaceRace:
    prefix, _ = prefix_fixture(root)
    if existing is not None:
        binding = prefix / RUNTIME_LOWER_BINDING_NAME
        binding.write_bytes(existing)
        binding.chmod(0o600)
    transaction = NamespaceRace(root / "manifest.json", prefix)
    transaction.checkpoint = checkpoint
    transaction.replacement = f"replacement:{checkpoint}\n".encode()
    transaction.saved = None
    transaction.split_lock = split_lock
    return transaction


# Absent destination occupied immediately before the no-clobber publication.
with tempfile.TemporaryDirectory(prefix="runtime-lower-race-publish-") as temp:
    transaction = race_transaction(Path(temp), "before_publish")
    try:
        transaction.bind_runtime_lower_root(prefix_generation=51)
    except DeploymentTransactionError as error:
        assert "recovery required" in str(error)
    else:
        raise AssertionError("publication replacement race accepted")
    replacement = transaction.prefix / RUNTIME_LOWER_BINDING_NAME
    identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    assert_preserved(replacement, identity, transaction.replacement)
    assert transaction.runtime_lower_recovery
    close_recovery(transaction)

# Existing binding is replaced after opening; the replacement is quarantined,
# retained by exact FD, and never overwritten by the candidate.
with tempfile.TemporaryDirectory(prefix="runtime-lower-race-existing-") as temp:
    transaction = race_transaction(Path(temp), "after_existing_validation", existing=b"old\n")
    try:
        transaction.bind_runtime_lower_root(prefix_generation=52)
    except DeploymentTransactionError as error:
        assert "recovery required" in str(error)
    else:
        raise AssertionError("existing-binding replacement race accepted")
    assert transaction.saved is not None
    assert transaction.saved.read_bytes() == b"old\n"
    obligation = transaction.runtime_lower_recovery[-1]
    recovered = transaction.prefix / obligation.name
    assert_preserved(recovered, (obligation.device, obligation.inode), transaction.replacement)
    close_recovery(transaction)

# Rollback validates the staged deployed inode, then a replacement arrives at
# the private name. The replacement remains owned by a recovery obligation.
with tempfile.TemporaryDirectory(prefix="runtime-lower-race-unlink-") as temp:
    root = Path(temp)
    transaction = race_transaction(root, "before_quarantine_unlink")
    transaction.bind_runtime_lower_root(prefix_generation=53)
    try:
        transaction.rollback()
    except DeploymentTransactionError as error:
        assert "recovery required" in str(error)
    else:
        raise AssertionError("quarantine replacement race accepted")
    obligation = transaction.runtime_lower_recovery[-1]
    recovered = transaction.prefix / obligation.name
    assert_preserved(recovered, (obligation.device, obligation.inode), transaction.replacement)
    close_recovery(transaction)

# A destination replacement before backup restoration is never clobbered.
with tempfile.TemporaryDirectory(prefix="runtime-lower-race-restore-") as temp:
    root = Path(temp)
    transaction = race_transaction(root, "before_backup_restore", existing=b"old-binding\n")
    transaction.bind_runtime_lower_root(prefix_generation=54)
    try:
        transaction.rollback()
    except DeploymentTransactionError as error:
        assert "recovery required" in str(error)
    else:
        raise AssertionError("backup restore replacement race accepted")
    replacement = transaction.prefix / RUNTIME_LOWER_BINDING_NAME
    identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    assert_preserved(replacement, identity, transaction.replacement)
    assert transaction.runtime_lower_recovery
    close_recovery(transaction)

# Replacing the named lock after exclusive flock invalidates the authority and
# preserves the exact candidate staging inode for recovery.
with tempfile.TemporaryDirectory(prefix="runtime-lower-race-split-lock-") as temp:
    transaction = race_transaction(Path(temp), "before_publish", split_lock=True)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=55)
    except DeploymentTransactionError as error:
        assert "recovery required" in str(error)
    else:
        raise AssertionError("split-lock publication accepted")
    assert transaction.runtime_lower_recovery
    close_recovery(transaction)

# A non-recovery failure after lease acquisition releases both capabilities.
with tempfile.TemporaryDirectory(prefix="runtime-lower-lease-release-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)
    (prefix / RUNTIME_LOWER_BINDING_NAME).mkdir()
    baseline = fd_census()
    transaction = DeploymentTransaction(root / "manifest.json", prefix)
    try:
        transaction.bind_runtime_lower_root(prefix_generation=61)
    except DeploymentTransactionError:
        pass
    else:
        raise AssertionError("non-regular existing binding accepted")
    assert_contender(prefix, blocked=False)
    assert fd_census() == baseline

# Recovery owns the exact lease until its single idempotent owner is closed.
with tempfile.TemporaryDirectory(prefix="runtime-lower-owning-recovery-") as temp:
    root = Path(temp)
    transaction = race_transaction(root, "before_publish")
    baseline = fd_census()
    try:
        transaction.bind_runtime_lower_root(prefix_generation=62)
    except DeploymentTransactionError:
        pass
    else:
        raise AssertionError("recovery fixture unexpectedly published")
    assert_contender(transaction.prefix, blocked=True)
    close_recovery(transaction)
    assert_contender(transaction.prefix, blocked=False)
    assert fd_census() == baseline

# A restarted restorer reconstructs persisted exact ownership and never
# rewrites an unresolved recovery manifest.
with tempfile.TemporaryDirectory(prefix="runtime-lower-restart-recovery-") as temp:
    root = Path(temp)
    transaction = race_transaction(root, "before_publish")
    try:
        transaction.bind_runtime_lower_root(prefix_generation=63)
    except DeploymentTransactionError:
        pass
    else:
        raise AssertionError("restart recovery fixture unexpectedly published")
    close_recovery(transaction)
    manifest = root / "manifest.json"
    before = manifest.read_bytes()
    baseline = fd_census()
    try:
        DeploymentTransaction.restore(manifest, transaction.prefix)
    except DeploymentTransactionError as error:
        assert error.recovery_owner is not None
        assert error.recovery_obligations
        assert manifest.read_bytes() == before
        assert_contender(transaction.prefix, blocked=True)
        error.recovery_owner.close()
    else:
        raise AssertionError("unresolved persisted recovery was restored")
    assert_contender(transaction.prefix, blocked=False)
    assert fd_census() == baseline

# Hostile metadata is rejected both before and after flock. Hard-linking also
# proves nlink=1 is enforced rather than inferred from pathname equality.
for hostile in ("mode", "nlink"):
    with tempfile.TemporaryDirectory(prefix=f"runtime-lower-hostile-{hostile}-") as temp:
        root = Path(temp)
        prefix, _ = prefix_fixture(root)
        lock = prefix / ".lifecycle.lock"
        if hostile == "mode":
            lock.chmod(0o640)
        else:
            os.link(lock, prefix / ".lifecycle.lock.alias")
        baseline = fd_census()
        transaction = DeploymentTransaction(root / "manifest.json", prefix)
        try:
            transaction.bind_runtime_lower_root(prefix_generation=64)
        except DeploymentTransactionError:
            pass
        else:
            raise AssertionError(f"hostile lock {hostile} accepted")
        assert fd_census() == baseline


class OwnershipFault(DeploymentTransaction):
    runtime_checkpoint: str | None = None
    manifest_checkpoint: str | None = None
    interrupt = False
    restore_checkpoint: str | None = None
    manifest_phase: str | None = None
    source_cleanup_failure = False

    def _write(self, state: str, **kwargs: object) -> None:
        self._fault_manifest_state = state
        super()._write(state, **kwargs)

    def _manifest_fault(self, checkpoint: str) -> None:
        if (
            checkpoint == self.manifest_checkpoint
            and getattr(self, "_fault_manifest_state", None) == "recovery_required"
            and (
                self.manifest_phase is None
                or any(
                    obligation.phase == self.manifest_phase
                    for obligation in self.runtime_lower_recovery
                )
            )
            and not getattr(self, "_manifest_faulted", False)
        ):
            self._manifest_faulted = True
            raise OSError(errno.EIO, f"injected manifest {checkpoint}")

    def _runtime_lower_fault(self, checkpoint: str, parent_fd: int, name: str) -> None:
        selected = self.runtime_checkpoint or type(self).restore_checkpoint
        if checkpoint == selected and not getattr(self, "_runtime_faulted", False):
            self._runtime_faulted = True
            if self.interrupt:
                raise KeyboardInterrupt("injected interruption after namespace mutation")
            raise OSError(errno.ENOSPC, f"injected runtime {checkpoint}")

    def _collect_runtime_lower_source(self, source: Path) -> None:
        if self.source_cleanup_failure:
            raise OSError(errno.EIO, "injected post-commit source cleanup")
        super()._collect_runtime_lower_source(source)


def assert_owned_failure(
    transaction: DeploymentTransaction,
    action: object,
    *,
    label: str,
) -> None:
    baseline = fd_census()
    try:
        action()  # type: ignore[operator]
    except DeploymentTransactionError as error:
        owner = error.recovery_owner
        assert owner is transaction.runtime_lower_recovery_owner
        assert owner is not None and error.recovery_obligations
        payload = json.loads(transaction.manifest_path.read_text(encoding="utf-8"))
        assert payload["state"] in {"active", "recovery_required"}
        for obligation in error.recovery_obligations:
            observed = os.fstat(obligation.object_fd)
            assert (observed.st_dev, observed.st_ino) == (
                obligation.device,
                obligation.inode,
            )
            named = transaction.prefix / obligation.name
            if named.exists():
                named_stat = named.stat()
                assert (named_stat.st_dev, named_stat.st_ino) == (
                    obligation.device,
                    obligation.inode,
                )
        assert_contender(transaction.prefix, blocked=True)
        owner.close()
        owner.close()
    else:
        raise AssertionError(f"{label} did not fail")
    assert_contender(transaction.prefix, blocked=False)
    assert fd_census() == baseline


def close_and_restart(
    transaction: DeploymentTransaction,
    error: DeploymentTransactionError,
    *,
    expected_content: bytes | None,
) -> None:
    owner = error.recovery_owner
    assert owner is not None
    owner.close()
    owner.close()
    OwnershipFault.restore_checkpoint = None
    fresh_restore(transaction.manifest_path, transaction.prefix)
    payload = json.loads(transaction.manifest_path.read_text(encoding="utf-8"))
    assert payload["state"] == "restored"
    binding = transaction.prefix / RUNTIME_LOWER_BINDING_NAME
    if expected_content is None:
        assert not binding.exists()
    else:
        assert binding.read_bytes() == expected_content


def fresh_restore(manifest: Path, prefix: Path) -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "from pathlib import Path; "
                "from west_commands.deploy_transaction import DeploymentTransaction; "
                "DeploymentTransaction.restore(Path(__import__('sys').argv[1]), "
                "Path(__import__('sys').argv[2]))"
            ),
            str(manifest),
            str(prefix),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0, (child.stdout, child.stderr)


# Every post-mutation failure transfers the exact capability and lease before
# returning, including asynchronous interruption.
for checkpoint, existing, interrupt in (
    ("after_quarantine", True, False),
    ("before_staging_write", False, False),
    ("before_staging_fsync", False, False),
    ("after_publication", False, False),
    ("after_publication", False, True),
):
    with tempfile.TemporaryDirectory(prefix=f"runtime-lower-fault-{checkpoint}-") as temp:
        root = Path(temp)
        prefix, _ = prefix_fixture(root)
        if existing:
            existing_path = prefix / RUNTIME_LOWER_BINDING_NAME
            existing_path.write_bytes(b"previous binding\n")
            existing_path.chmod(0o600)
        transaction = OwnershipFault(root / "manifest.json", prefix)
        transaction.runtime_checkpoint = checkpoint
        transaction.interrupt = interrupt
        assert_owned_failure(
            transaction,
            lambda: transaction.bind_runtime_lower_root(prefix_generation=71),
            label=f"runtime-{checkpoint}-interrupt={interrupt}",
        )


# Durable recovery-manifest write, rename, and directory-fsync failures all
# retain one reachable owner; the last fully written manifest stays valid.
for checkpoint in (
    "before_manifest_fsync",
    "before_manifest_rename",
    "before_manifest_parent_fsync",
):
    with tempfile.TemporaryDirectory(prefix=f"runtime-lower-manifest-{checkpoint}-") as temp:
        root = Path(temp)
        prefix, _ = prefix_fixture(root)
        transaction = OwnershipFault(root / "manifest.json", prefix)
        transaction.manifest_checkpoint = checkpoint
        assert_owned_failure(
            transaction,
            lambda: transaction.bind_runtime_lower_root(prefix_generation=72),
            label=f"manifest-{checkpoint}",
        )


# Standalone restore also hands off exact post-quarantine authority instead of
# emitting a raw exception or rewriting the manifest to restored.
with tempfile.TemporaryDirectory(prefix="runtime-lower-restore-fault-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)
    manifest = root / "manifest.json"
    transaction = DeploymentTransaction(manifest, prefix)
    binding = transaction.bind_runtime_lower_root(prefix_generation=73)
    deployed_identity = (binding.stat().st_dev, binding.stat().st_ino)
    transaction.commit()
    before = manifest.read_bytes()
    baseline = fd_census()
    OwnershipFault.restore_checkpoint = "after_restore_quarantine"
    try:
        OwnershipFault.restore(manifest, prefix)
    except DeploymentTransactionError as error:
        assert error.recovery_owner is not None
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert payload["state"] == "recovery_required"
        assert manifest.read_bytes() != b"" and manifest.read_bytes() != before
        obligation = error.recovery_obligations[0]
        assert (obligation.device, obligation.inode) == deployed_identity
        assert_contender(prefix, blocked=True)
        error.recovery_owner.close()
        error.recovery_owner.close()
    else:
        raise AssertionError("standalone post-mutation restore failure was accepted")
    finally:
        OwnershipFault.restore_checkpoint = None
    assert_contender(prefix, blocked=False)
    assert fd_census() == baseline


# A durable active commit is not rewritten.  A later source-collection failure
# transfers the still-held lease into a typed owner, then restart observes the
# honest active manifest without a raw descriptor or flock leak.
with tempfile.TemporaryDirectory(prefix="runtime-lower-post-commit-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)
    baseline = fd_census()
    transaction = OwnershipFault(root / "manifest.json", prefix)
    transaction.source_cleanup_failure = True
    try:
        transaction.bind_runtime_lower_root(prefix_generation=74)
    except DeploymentTransactionError as error:
        assert error.recovery_owner is not None
        assert not error.recovery_obligations
        assert json.loads(transaction.manifest_path.read_text())["state"] == "active"
        assert_contender(prefix, blocked=True)
        error.recovery_owner.close()
        error.recovery_owner.close()
    else:
        raise AssertionError("post-commit cleanup failure was accepted")
    assert_contender(prefix, blocked=False)
    assert fd_census() == baseline


# Crash immediately after each publication rename is recoverable from the
# durable two-name intent even though the post-syscall manifest write has not
# happened yet.
for checkpoint, existing in (
    ("after_quarantine_before_manifest", True),
    ("after_publish_before_manifest", False),
):
    with tempfile.TemporaryDirectory(prefix=f"runtime-lower-intent-{checkpoint}-") as temp:
        root = Path(temp)
        prefix, _ = prefix_fixture(root)
        previous = b"previous binding\n"
        if existing:
            old = prefix / RUNTIME_LOWER_BINDING_NAME
            old.write_bytes(previous)
            old.chmod(0o600)
            expected_identity = (old.stat().st_dev, old.stat().st_ino)
        transaction = OwnershipFault(root / "manifest.json", prefix)
        transaction.runtime_checkpoint = checkpoint
        baseline = fd_census()
        try:
            transaction.bind_runtime_lower_root(prefix_generation=741)
        except DeploymentTransactionError as error:
            owner = error.recovery_owner
            assert owner is not None
            assert_contender(prefix, blocked=True)
            if not existing:
                published = prefix / RUNTIME_LOWER_BINDING_NAME
                expected_identity = (published.stat().st_dev, published.stat().st_ino)
            owner.close()
            owner.close()
        else:
            raise AssertionError(f"intent checkpoint {checkpoint} did not fail")
        assert_contender(prefix, blocked=False)
        fresh_restore(transaction.manifest_path, prefix)
        payload = json.loads(transaction.manifest_path.read_text(encoding="utf-8"))
        assert payload["state"] == "active"
        binding = prefix / RUNTIME_LOWER_BINDING_NAME
        observed = binding.stat()
        assert (observed.st_dev, observed.st_ino) == expected_identity
        if existing:
            assert binding.read_bytes() == previous
            assert payload["runtime_lower_binding"] is None
        assert fd_census() == baseline


# Every restore mutation checkpoint is recoverable by a fresh process after
# the exact owner is closed.  Collection records make an absent quarantine an
# expected durable phase rather than a FileNotFoundError.
for checkpoint, existing, expected in (
    ("after_restore_quarantine", False, None),
    ("after_backup_restore", True, b"previous binding\n"),
    ("after_quarantine_unlink", False, None),
):
    with tempfile.TemporaryDirectory(prefix=f"runtime-lower-restart-{checkpoint}-") as temp:
        root = Path(temp)
        prefix, _ = prefix_fixture(root)
        if existing:
            old = prefix / RUNTIME_LOWER_BINDING_NAME
            old.write_bytes(expected)
            old.chmod(0o600)
        manifest = root / "manifest.json"
        transaction = DeploymentTransaction(manifest, prefix)
        transaction.bind_runtime_lower_root(prefix_generation=75)
        transaction.commit()
        baseline = fd_census()
        OwnershipFault.restore_checkpoint = checkpoint
        try:
            OwnershipFault.restore(manifest, prefix)
        except DeploymentTransactionError as error:
            assert error.recovery_owner is not None
            assert_contender(prefix, blocked=True)
            close_and_restart(transaction, error, expected_content=expected)
        else:
            raise AssertionError(f"restore checkpoint {checkpoint} did not fail")
        assert_contender(prefix, blocked=False)
        assert fd_census() == baseline


# The three durability checkpoints after quarantine collection all replay in
# a fresh process, including the state where unlink succeeded but the final
# recovery record did not reach stable storage.
for manifest_checkpoint in (
    "before_manifest_fsync",
    "before_manifest_rename",
    "before_manifest_parent_fsync",
):
    with tempfile.TemporaryDirectory(prefix=f"runtime-lower-collect-{manifest_checkpoint}-") as temp:
        root = Path(temp)
        prefix, _ = prefix_fixture(root)
        manifest = root / "manifest.json"
        transaction = DeploymentTransaction(manifest, prefix)
        transaction.bind_runtime_lower_root(prefix_generation=76)
        transaction.commit()
        baseline = fd_census()
        OwnershipFault.manifest_checkpoint = manifest_checkpoint
        OwnershipFault.manifest_phase = "collected"
        try:
            OwnershipFault.restore(manifest, prefix)
        except DeploymentTransactionError as error:
            assert error.recovery_owner is not None
            assert_contender(prefix, blocked=True)
            OwnershipFault.manifest_checkpoint = None
            OwnershipFault.manifest_phase = None
            close_and_restart(transaction, error, expected_content=None)
        else:
            raise AssertionError(
                f"collection manifest checkpoint {manifest_checkpoint} did not fail"
            )
        assert_contender(prefix, blocked=False)
        assert fd_census() == baseline

print("RUNTIME_LOWER_BINDING_DEPLOYMENT_VALID")

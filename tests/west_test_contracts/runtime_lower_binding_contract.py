"""Production deployment contract for the retained runtime lower-root binding."""

from __future__ import annotations

import json
import fcntl
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
    # The standalone restore owns its recovery FDs until process exit; the
    # exact replacement is preserved under the typed quarantine name.
    quarantined = next(prefix.glob(f".{RUNTIME_LOWER_BINDING_NAME}.*.rollback"))
    assert (quarantined.stat().st_dev, quarantined.stat().st_ino) == replacement_identity
    assert quarantined.read_bytes() == retained.read_bytes()

with tempfile.TemporaryDirectory(prefix="runtime-lower-post-publish-fault-") as temp:
    root = Path(temp)
    prefix, _ = prefix_fixture(root)

    class PostPublishFault(DeploymentTransaction):
        def _write(self, state: str) -> None:
            if (
                not getattr(self, "_faulted", False)
                and self.entries
                and self.entries[-1].destination.endswith(RUNTIME_LOWER_BINDING_NAME)
            ):
                self._faulted = True
                raise DeploymentTransactionError("injected post-publish manifest fault")
            super()._write(state)

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
    obligation = transaction.runtime_lower_recovery[0]
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
    obligation = transaction.runtime_lower_recovery[0]
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

print("RUNTIME_LOWER_BINDING_DEPLOYMENT_VALID")

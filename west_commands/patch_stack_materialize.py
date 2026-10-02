"""Fail-closed schema-v2 lock materialization into a local result ref.

Every Git subprocess this module owns is non-interactive (standard input is
``/dev/null``, terminal prompting is disabled and ssh/passphrase prompting is
forced off) and bounded by :data:`IMMUTABLE_FETCH_TIMEOUT_SECONDS`.  Git has no
connect timeout, so a blackholed mirror would otherwise wait in ``pipe_read``
forever instead of failing the patch gate.  A transfer that exceeds the bound
kills the whole process tree and raises a named unreachable-mirror error that
names the mirror URL and the elapsed seconds; a timeout is never reported as
success.  ``WEST_PATCH_IMMUTABLE_FETCH_TIMEOUT`` (seconds) overrides the bound
for a slow but working mirror, and the pre-flight reachability probe uses
``WEST_PATCH_MIRROR_PROBE_TIMEOUT``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from patch_stack_preflight import inspect, load_lock


class MaterializeError(RuntimeError):
    pass


class GitTimeout(MaterializeError):
    """A bounded Git subprocess exceeded its bound and was killed."""

    def __init__(self, argv: Sequence[str], timeout: float, elapsed: float):
        self.argv = tuple(argv)
        self.timeout = timeout
        self.elapsed = elapsed
        super().__init__(f"git {' '.join(self.argv[1:])} exceeded its {timeout:g}s bound after {elapsed:.1f}s")


class MirrorUnreachableError(MaterializeError):
    """An immutable mirror did not answer, so no verification could run."""


# One bounded default for the immutable mirror transfer.  ``git fetch`` has no
# connect timeout, so an unreachable or blackholed mirror waits forever; this
# bound turns that wait into a named failure.  A slow but working mirror can
# raise it with ``WEST_PATCH_IMMUTABLE_FETCH_TIMEOUT=<seconds>``.
IMMUTABLE_FETCH_TIMEOUT_SECONDS = 300.0
IMMUTABLE_FETCH_TIMEOUT_ENV = "WEST_PATCH_IMMUTABLE_FETCH_TIMEOUT"
# The short first attempt at a FULL transfer, before the blobless fallback in
# fetch_immutable() takes over.  See immutable_fetch_probe_seconds().
IMMUTABLE_FULL_FETCH_PROBE_SECONDS = 120.0
IMMUTABLE_FULL_FETCH_PROBE_ENV = "WEST_PATCH_IMMUTABLE_FULL_FETCH_PROBE"
# Reachability probes run once per distinct mirror before any replay, so their
# bound stays short: a blackholed mirror must fail the gate almost immediately.
MIRROR_PROBE_TIMEOUT_SECONDS = 20.0
MIRROR_PROBE_TIMEOUT_ENV = "WEST_PATCH_MIRROR_PROBE_TIMEOUT"


def _bounded_seconds(env_name: str, default: float) -> float:
    """Resolve one bounded subprocess timeout from the environment."""
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not value > 0 or value == float("inf"):
        raise MaterializeError(f"{env_name}={raw!r} must be a positive finite number of seconds")
    return value


def immutable_fetch_timeout() -> float:
    """Return the bounded immutable-mirror transfer timeout in seconds."""
    return _bounded_seconds(IMMUTABLE_FETCH_TIMEOUT_ENV, IMMUTABLE_FETCH_TIMEOUT_SECONDS)


def mirror_probe_timeout() -> float:
    """Return the bounded mirror-reachability probe timeout in seconds."""
    return _bounded_seconds(MIRROR_PROBE_TIMEOUT_ENV, MIRROR_PROBE_TIMEOUT_SECONDS)


def mirror_unreachable_message(
    url: str,
    *,
    operation: str,
    elapsed: float,
    limit: float,
    env_name: str,
    detail: str = "",
) -> str:
    """Name the mirror, the wait it did not survive, and the consequence."""
    observed = f" {detail}" if detail else ""
    return (
        f"immutable mirror {url} is unreachable: {operation}{observed} after "
        f"{elapsed:.1f}s, within its {limit:g}s bound ({env_name}); the immutable "
        "mirror could not be contacted, so the patch applicability gate did not run"
    )


def _git_environment() -> dict[str, str]:
    """Environment that forbids every interactive Git prompt.

    Standard input is ``/dev/null``, so a credential or passphrase prompt could
    only read EOF or block a terminal this process cannot reach.  Disabling the
    prompts makes the failure immediate and explicit instead of an open wait.
    """
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["SSH_ASKPASS_REQUIRE"] = "never"
    environment["GCM_INTERACTIVE"] = "never"
    environment.setdefault("GIT_ASKPASS", "/bin/false")
    environment.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return environment


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a timed-out Git and the helpers it spawned (ssh, index-pack).

    The subprocess starts its own session, so the process group covers every
    descendant; killing only the direct child would leave helpers holding the
    pipes and the wait would continue.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _run(repo: Path, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run one Git subprocess: non-interactive, and bounded by default.

    ``timeout`` defaults to :func:`immutable_fetch_timeout`.  The immutable
    mirror transfer is the only network operation here, so its bound covers
    every subprocess this module owns and no call site can forget it.
    """
    limit = immutable_fetch_timeout() if timeout is None else timeout
    process = subprocess.Popen(
        ["git", *args],
        cwd=repo,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_environment(),
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # A helper outside the killed process group still holds the pipes.
            # The bound is what matters, so stop waiting and report the timeout.
            pass
        raise GitTimeout(["git", *args], limit, time.monotonic() - started) from None
    return subprocess.CompletedProcess(["git", *args], process.returncode, stdout, stderr)


def _git(repo: Path, *args: str, timeout: float | None = None) -> str:
    result = _run(repo, *args, timeout=timeout)
    if result.returncode:
        raise MaterializeError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def immutable_fetch_probe_seconds() -> float:
    """Bound the FULL immutable transfer before switching to a blobless one.

    MEASURED (dar-b5pe): a mirror whose pack for a single ref is far larger than
    that ref needs does not fail fast -- it starts serving, then drops the
    connection, and the observed failure took three minutes eighteen seconds,
    while the same ref fetched ``--filter=blob:none`` completed in 3.9s.  A
    replay contacts several mirrors, so paying the full 600s bound per mirror
    makes one replay spend an hour learning what a probe can learn in two
    minutes.  A working full transfer answers in seconds, so a short first
    attempt loses nothing that a retry below does not recover.
    """
    raw = os.environ.get(IMMUTABLE_FULL_FETCH_PROBE_ENV)
    if raw is None or not raw.strip():
        return IMMUTABLE_FULL_FETCH_PROBE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not value > 0 or value == float("inf"):
        return IMMUTABLE_FULL_FETCH_PROBE_SECONDS
    return value


# Mirrors whose full transfer already failed once in THIS process.  A replay
# fetches many refs from the same handful of mirrors, and without this memo each
# ref would pay the probe bound again before falling back.
_FULL_FETCH_FAILED_MIRRORS: set[str] = set()

# Mirrors already announced as fetched blobless in THIS process: the announcement
# is one line per mirror, not one per ref, so a replay's output stays readable.
_BLOBLESS_ANNOUNCED_MIRRORS: set[str] = set()


def fetch_immutable(repo: Path, remote: str, specs: Sequence[str], *, url: str | None = None) -> None:
    """Fetch immutable refs through one bounded, non-interactive transfer.

    ``remote`` is either a configured remote name (``immutable`` in the
    disposable canonical ODB) or the mirror URL itself; ``url`` is the mirror
    named in the error when the transfer cannot finish.  The bound and the
    no-prompt environment come from ``_run``; a transfer that exceeds the bound
    is reported as an unreachable mirror rather than as a bare Git failure.
    """
    limit = immutable_fetch_timeout()
    probe = immutable_fetch_probe_seconds()
    mirror_key = url or remote

    # A BLOBLESS FETCH IS TRIED FIRST, BECAUSE IT IS THE ONE THAT WORKS HERE. MEASURED (dar-b5pe), same tag and
    # mirror minutes apart: the unfiltered transfer dies mid-pack with "unexpected disconnect while reading
    # sideband packet", then "early EOF" and "invalid index-pack output", after three minutes eighteen seconds,
    # while the same ref with --filter=blob:none completes in 3.9s and leaves the commit present. A replay reads
    # commits and trees before it writes anything, so metadata is what it needs; if a later checkout wants a
    # blob, Git asks the same mirror on demand. The order matters beyond speed: a full transfer that is KILLED
    # mid-pack leaves the destination holding a partial transfer, and the retry that used to run second then had
    # to succeed on top of that state -- which is exactly how a fallback that works in isolation reported the
    # mirror unreachable inside a replay. Trying the metadata-only transfer first never creates that state.
    try:
        _git(repo, "fetch", "--no-tags", "--filter=blob:none", remote, *specs)
        if mirror_key not in _BLOBLESS_ANNOUNCED_MIRRORS:
            _BLOBLESS_ANNOUNCED_MIRRORS.add(mirror_key)
            print(
                f"patch-stack: fetched immutable refs from {mirror_key} with --filter=blob:none "
                "(the transfer shape this mirror serves reliably; a later blob request fetches on demand)"
            )
        return
    except GitTimeout as error:
        first_error: BaseException = error
        first_kind = "immutable blobless fetch timed out"
    except MaterializeError as error:
        first_error = error
        first_kind = "immutable blobless fetch failed"

    # The full transfer is the fallback, tried once per mirror per process: a replay fetches many refs from the
    # same few mirrors, and a mirror that already failed a full transfer does not need to fail again per ref.
    if mirror_key in _FULL_FETCH_FAILED_MIRRORS:
        raise MirrorUnreachableError(
            mirror_unreachable_message(
                mirror_key,
                operation=f"{first_kind}, and a full transfer from this mirror already failed in this run",
                elapsed=0.0,
                limit=limit,
                env_name=IMMUTABLE_FETCH_TIMEOUT_ENV,
            )
        ) from first_error
    try:
        _git(repo, "fetch", "--no-tags", remote, *specs, timeout=probe)
    except GitTimeout as error:
        _FULL_FETCH_FAILED_MIRRORS.add(mirror_key)
        raise MirrorUnreachableError(
            mirror_unreachable_message(
                mirror_key,
                operation=f"{first_kind}, and the full fallback did not complete within its {probe:g}s probe bound",
                elapsed=error.elapsed,
                limit=limit,
                env_name=IMMUTABLE_FETCH_TIMEOUT_ENV,
            )
        ) from first_error
    except MaterializeError as error:
        _FULL_FETCH_FAILED_MIRRORS.add(mirror_key)
        raise MirrorUnreachableError(
            mirror_unreachable_message(
                mirror_key,
                operation=f"{first_kind}, and the full fallback failed too",
                elapsed=0.0,
                limit=limit,
                env_name=IMMUTABLE_FETCH_TIMEOUT_ENV,
            )
        ) from first_error
    print(
        f"patch-stack: the blobless fetch from {mirror_key} did not complete; "
        "the full transfer did (this mirror serves the larger transfer reliably)"
    )


def probe_immutable_mirror(url: str) -> None:
    """Prove one immutable mirror answers before a long replay begins.

    One bounded, non-interactive ``git ls-remote`` per distinct mirror: the same
    remote helper, credentials and URL rewriting the transfer will use, but a
    single round trip instead of a full transfer.  A mirror that does not answer
    raises the same named unreachable-mirror error the bounded fetch raises, so
    a blackholed mirror fails the gate before any per-module replay.
    """
    limit = mirror_probe_timeout()
    started = time.monotonic()
    try:
        result = _run(Path(tempfile.gettempdir()), "ls-remote", "--quiet", url, "HEAD", timeout=limit)
    except GitTimeout as error:
        raise MirrorUnreachableError(
            mirror_unreachable_message(
                url,
                operation="reachability probe timed out",
                elapsed=error.elapsed,
                limit=limit,
                env_name=MIRROR_PROBE_TIMEOUT_ENV,
            )
        ) from error
    if result.returncode:
        diagnostic = result.stderr.strip().splitlines()
        raise MirrorUnreachableError(
            mirror_unreachable_message(
                url,
                operation="reachability probe failed",
                detail=f"(git ls-remote exit {result.returncode}: {diagnostic[0] if diagnostic else 'no diagnostic'})",
                elapsed=time.monotonic() - started,
                limit=limit,
                env_name=MIRROR_PROBE_TIMEOUT_ENV,
            )
        )


def lock_mirror_url(lock_path: Path) -> str:
    """Return the immutable mirror URL one lock declares."""
    try:
        lock = load_lock(lock_path)
    except (OSError, ValueError) as error:
        raise MaterializeError(f"invalid immutable lock {lock_path}: {error}") from error
    mirror = lock.get("mirror")
    url = mirror.get("url") if isinstance(mirror, dict) else None
    if not isinstance(url, str) or not url:
        raise MaterializeError(f"{lock_path}: immutable lock declares no mirror URL")
    return url


def _common_dir(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def _oid(repo: Path, revision: str) -> str:
    return _git(repo, "rev-parse", f"{revision}^{{commit}}")


def _ref_state(repo: Path, ref: str) -> bool:
    result = _run(repo, "show-ref", "--verify", "--quiet", ref)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise MaterializeError(f"git show-ref failed ({result.returncode}): {result.stderr.strip()}")


def _delete_ref(repo: Path, ref: str) -> str:
    result = _run(repo, "update-ref", "-d", ref)
    if result.returncode == 0:
        return "removed"
    if result.returncode == 1:
        return "already-absent"
    else:
        raise MaterializeError(f"git update-ref -d {ref} failed ({result.returncode}): {result.stderr.strip()}")


def _check_clean_shape(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1"):
        raise MaterializeError("worktree is dirty")
    common = _common_dir(repo)
    if (common / "objects" / "info" / "alternates").exists():
        raise MaterializeError("alternates are forbidden")
    if _git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise MaterializeError("shallow clone is forbidden")
    partial = _run(repo, "config", "--get", "extensions.partialClone")
    if partial.returncode == 0 and partial.stdout.strip():
        raise MaterializeError("partial clone is forbidden")
    if partial.returncode not in (0, 1):
        raise MaterializeError(f"git config failed ({partial.returncode}): {partial.stderr.strip()}")
    if _git(repo, "replace", "-l"):
        raise MaterializeError("replace objects are forbidden")


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup(repo: Path, worktree: Path | None, root: Path | None, refs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    if worktree is not None:
        try:
            _git(repo, "worktree", "remove", "--force", str(worktree))
            result["worktree"] = "removed"
        except Exception as error:
            result["worktree"] = f"failed: {error}"
    if root is not None and result.get("worktree", "removed") == "removed":
        try:
            if root.exists():
                shutil.rmtree(root)
            result["worktree_directory"] = "removed"
        except Exception as error:
            result["worktree_directory"] = f"failed: {error}"
    elif root is not None:
        result["worktree_directory"] = "skipped: worktree removal failed"
    for ref in refs:
        try:
            result[ref] = _delete_ref(repo, ref)
        except Exception as error:
            result[ref] = f"failed: {error}"
    return result


def _cleanup_ok(cleanup: dict[str, str]) -> bool:
    return bool(cleanup) and all(value in ("removed", "already-absent") for value in cleanup.values())


def _rollback_result(repo: Path, ref: str, source: str) -> str:
    """Delete only the ref this transaction created; never overwrite another result."""
    try:
        if not _ref_state(repo, ref):
            return "already-absent"
        if _oid(repo, ref) != source:
            return "preserved: no longer points at transaction source"
        return _delete_ref(repo, ref)
    except Exception as error:
        return f"failed: {error}"


def _fetchable_preflight(preflight: dict[str, Any]) -> bool:
    """Permit only a clean preflight whose sole gap is fetched immutable input."""
    if preflight["overall_verdict"] == "VALID":
        return True
    if preflight["overall_verdict"] != "INCOMPLETE":
        return False
    allowed = {
        "declared_objects": {"UNKNOWN"},
        "immutable_base_tag": {"INCOMPLETE"},
        "immutable_source_tag": {"INCOMPLETE"},
    }
    saw_incomplete = False
    for check in preflight["checks"]:
        name, status = check["name"], check["status"]
        if status == "PASS":
            continue
        if status in allowed.get(name, set()):
            saw_incomplete = True
            continue
        return False
    return saw_incomplete


def validate_fetched_lock(repo: Path, lock: dict[str, Any], base_ref: str, source_ref: str) -> dict[str, Any]:
    """Validate one schema-v2 lock against immutable refs already fetched.

    Batch callers deliberately fetch a union of immutable refs once per
    repository.  Keeping this graph/metadata/tree proof here prevents that
    optimization from weakening the single-lock materializer's invariants.
    """
    if lock.get("schema_version") != 2:
        raise MaterializeError("materialize-lock accepts schema_version 2 only")
    base, source = _oid(repo, base_ref), _oid(repo, source_ref)
    if base != lock["upstream"]["base_commit"] or source != lock["source_commit"]:
        raise MaterializeError("fetched immutable ref OID differs from lock")
    ordered = _git(repo, "rev-list", "--reverse", f"{base}..{source}").splitlines()
    if ordered != lock["ordered_commits"]:
        raise MaterializeError("ordered commits are not the exact linear range")
    for index, commit in enumerate(ordered):
        parents = _git(repo, "show", "-s", "--format=%P", commit).split()
        if parents != ([base] if index == 0 else [ordered[index - 1]]):
            raise MaterializeError("merge or nonlinear ordered stack")
        metadata = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI", commit).split("\x00")
        if len(metadata) != 6 or not all(metadata):
            raise MaterializeError("incomplete author/committer metadata")
    tree = _git(repo, "show", "-s", "--format=%T", source)
    if tree != lock["expected_tree"]:
        raise MaterializeError("expected tree differs from source commit")
    return {"base_oid": base, "source_oid": source, "ordered_commits": ordered, "resulting_tree": tree}


def materialize(repo: Path, lock_path: Path, result_ref: str | None = None, evidence_path: Path | None = None) -> dict[str, Any]:
    """Materialize a canonical graph without applying mbox patches or touching HEAD.

    The command is transactional: disposable refs/worktree are removed before
    the create-only result ref update. A failed cleanup or evidence write rolls
    back a newly-created result ref by comparing it to the expected source OID.
    """
    repo, lock_path = repo.resolve(), lock_path.resolve()
    try:
        lock_bytes, lock = lock_path.read_bytes(), load_lock(lock_path)
    except (OSError, ValueError) as error:
        raise MaterializeError(f"invalid lock input: {error}") from error
    if lock["schema_version"] != 2:
        raise MaterializeError("materialize-lock accepts schema_version 2 only")
    source = lock["source_commit"]
    result_ref = result_ref or f"refs/west/patch-stack-results/{source}"
    if not result_ref.startswith("refs/west/patch-stack-results/") or _run(repo, "check-ref-format", result_ref).returncode:
        raise MaterializeError("invalid result ref")
    common = _common_dir(repo)
    evidence_path = evidence_path or common / "patch-stack-materialization" / f"{source}.json"
    transaction = uuid.uuid4().hex
    temporary = f"refs/west/patch-stack-materialize/{transaction}"
    base_ref, source_ref = temporary + "/base", temporary + "/source"
    evidence: dict[str, Any] = {"status": "ERROR", "verdict": "ERROR", "transaction_id": transaction,
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(), "source_commit": source,
        "ordered_commits": lock["ordered_commits"], "result_ref": result_ref,
        "result_ref_status": "not-created", "fetched": {}, "resulting_tree": None,
        "cleanup": {}, "evidence": "pending"}
    worktree: Path | None = None
    root: Path | None = None
    created_result = False
    primary_error: BaseException | None = None
    try:
        _check_clean_shape(repo)
        if _ref_state(repo, result_ref):
            raise MaterializeError(f"result ref already exists: {result_ref}")
        preflight = inspect(repo, lock_path)
        if not _fetchable_preflight(preflight):
            raise MaterializeError(f"pre-fetch preflight {preflight['overall_verdict']}")
        mirror = lock["mirror"]
        fetch_immutable(
            repo, mirror["url"],
            (f"{mirror['base_ref']}:{base_ref}", f"{mirror['source_ref']}:{source_ref}"),
        )
        validated = validate_fetched_lock(repo, lock, base_ref, source_ref)
        base, fetched = validated["base_oid"], validated["source_oid"]
        ordered, tree = validated["ordered_commits"], validated["resulting_tree"]
        evidence["fetched"] = {"base_oid": base, "source_oid": fetched}
        # The root is recoverable from transaction_id without globbing over
        # other concurrent materializers' disposable worktrees.
        root = Path(tempfile.gettempdir()) / f"west-lock-materialize-{transaction}"
        root.mkdir()
        worktree = root / "source"
        _git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), source_ref)
        if _oid(worktree, "HEAD") != source or _git(worktree, "rev-parse", "HEAD^{tree}") != tree:
            raise MaterializeError("disposable worktree result mismatch")
        evidence["resulting_tree"] = tree
        evidence["cleanup"] = _cleanup(repo, worktree, root, [base_ref, source_ref])
        worktree = None
        root = None
        if not _cleanup_ok(evidence["cleanup"]):
            raise MaterializeError("cleanup failed before result publication")
        try:
            _git(repo, "update-ref", result_ref, source, "")
            created_result = True
        except BaseException as error:
            # Detect an interrupted Git invocation that may have created the
            # ref before Python receives SIGINT. Ordinary create-only failure
            # must not inspect/delete a concurrent result ref.
            if not isinstance(error, Exception):
                created_result = _ref_state(repo, result_ref) and _oid(repo, result_ref) == source
            raise
        evidence["result_ref_status"] = "created"
        evidence["status"] = "VALID"
        evidence["verdict"] = "VALID"
    except BaseException as error:
        primary_error = error
        evidence["error"] = str(error)
        if not evidence["cleanup"]:
            evidence["cleanup"] = _cleanup(repo, worktree, root, [base_ref, source_ref])
            worktree = None
            root = None
        if created_result:
            evidence["result_ref_status"] = f"rollback: {_rollback_result(repo, result_ref, source)}"
            created_result = False
        evidence["verdict"] = "ERROR"
    try:
        if created_result and (not _cleanup_ok(evidence["cleanup"])):
            evidence["result_ref_status"] = f"rollback: {_rollback_result(repo, result_ref, source)}"
        evidence["evidence"] = "written"
        _write_evidence(evidence_path, evidence)
    except Exception as error:
        evidence["evidence"] = f"failed: {error}"
        if created_result:
            evidence["result_ref_status"] = f"rollback: {_rollback_result(repo, result_ref, source)}"
        if primary_error is None:
            primary_error = MaterializeError(f"evidence write failed: {error}")
    if primary_error is not None:
        raise primary_error
    return evidence

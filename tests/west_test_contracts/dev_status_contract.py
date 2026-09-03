"""Behavioral contract for bounded, read-only ``west dev status`` collection."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands import dev_status


class FakeProject:
    def __init__(
        self,
        name: str,
        path: Path,
        revision: str,
        *,
        active: bool = True,
        groups: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.abspath = str(path)
        self.path = path.name
        self.revision = revision
        self.active = active
        self.groups = groups


class FakeManifest:
    def __init__(self, projects: list[FakeProject]) -> None:
        self.projects = projects

    @staticmethod
    def is_active(project: FakeProject) -> bool:
        return project.active


def write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def git(env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def make_repository(path: Path, env: dict[str, str], files: dict[str, bytes]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(env, "init", "-b", "main", str(path))
    for relative, content in files.items():
        destination = path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    git(env, "-C", str(path), "add", "--all")
    git(env, "-C", str(path), "commit", "-m", "contract fixture")
    return git(env, "-C", str(path), "rev-parse", "HEAD")


def byte_snapshot(root: Path) -> tuple[tuple[str, str, int, bytes], ...]:
    """Capture names, kinds, modes, links, and file bytes without following links."""

    records: list[tuple[str, str, int, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        metadata = path.lstat()
        relative = str(path.relative_to(root))
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            records.append((relative, "link", mode, os.readlink(path).encode()))
        elif path.is_file():
            records.append((relative, "file", mode, path.read_bytes()))
        elif path.is_dir():
            records.append((relative, "directory", mode, b""))
        else:
            records.append((relative, "other", mode, b""))
    return tuple(records)


with tempfile.TemporaryDirectory(prefix="dev-status-contract-") as temporary:
    root = Path(temporary)
    home = root / "isolated-home"
    config_home = root / "isolated-config"
    workspace = root / "workspace"
    manifest_repo = workspace / "manifest"
    clean_repo = workspace / "clean-project"
    dirty_repo = workspace / "dirty-project"
    prefix = root / "deploy-prefix"
    build_dir = root / "build-output"
    job_root = root / "job-root"
    oversized_root = root / "oversized-discovery"
    home.mkdir()
    config_home.mkdir()
    workspace.mkdir()
    prefix.mkdir()
    build_dir.mkdir()
    job_root.mkdir()

    isolated_env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "West Status Contract",
        "GIT_AUTHOR_EMAIL": "status-contract@example.invalid",
        "GIT_COMMITTER_NAME": "West Status Contract",
        "GIT_COMMITTER_EMAIL": "status-contract@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "TMPDIR": str(job_root),
    }

    job_state = job_root / "state-001"
    job_script = manifest_repo / "scripts" / "west-job.sh"
    job_program = f"""#!{sys.executable}
import os
import sys
expected = [\"status\", \"--state-dir\", {str(job_state)!r}]
if os.getcwd() != {str(manifest_repo)!r} or sys.argv[1:] != expected:
    print(\"unexpected west-job invocation\", file=sys.stderr)
    raise SystemExit(97)
print(\"job-status: complete\")
"""
    write_executable(job_script, job_program)

    manifest_head = make_repository(
        manifest_repo,
        isolated_env,
        {
            "README": b"manifest repository\n",
            "scripts/west-job.sh": job_script.read_bytes(),
        },
    )
    clean_head = make_repository(clean_repo, isolated_env, {"tracked.txt": b"clean\n"})
    dirty_head = make_repository(dirty_repo, isolated_env, {"tracked.txt": b"tracked\n"})

    dirty_count = dev_status.MAX_STATUS_ENTRIES + 3
    for index in range(dirty_count):
        (dirty_repo / f"untracked-{index:03d}.txt").write_text(
            f"dirty fixture {index}\n", encoding="utf-8"
        )

    temporary_worktree = root / "west-profile-contract-worktree"
    git(
        isolated_env,
        "-C",
        str(clean_repo),
        "worktree",
        "add",
        "-b",
        "contract-temporary",
        str(temporary_worktree),
        "HEAD",
    )

    source_record_path = workspace / ".west-source-worktree-contract.json"
    source_record = {
        "version": 1,
        "gitlinks": [{"path": "clean-project", "head": clean_head}],
    }
    source_record_path.write_text(
        json.dumps(source_record, sort_keys=True) + "\n", encoding="utf-8"
    )

    deploy_manifest_path = workspace / "deploy-contract.json"
    deploy_manifest = {
        "version": 1,
        "state": "committed",
        "prefix": str(prefix),
        "roots": [str(prefix)],
        "entries": [],
        "directories": [],
    }
    deploy_manifest_path.write_text(
        json.dumps(deploy_manifest, sort_keys=True) + "\n", encoding="utf-8"
    )

    registry_entry = job_root / ".west-job-registry" / "job-001"
    registry_entry.mkdir(parents=True)
    (registry_entry.parent / ".lock").touch()
    job_state.mkdir()
    (registry_entry / "state-dir").write_text(str(job_state) + "\n", encoding="utf-8")
    (registry_entry / "pid").write_text("4242\n", encoding="utf-8")
    (registry_entry / "start-time").write_text("2026-01-02T03:04:05Z\n", encoding="utf-8")
    (registry_entry / "command").write_text("west test contract\n", encoding="utf-8")
    (job_state / "rc").write_text("0\n", encoding="utf-8")

    fake_west = root / "bin" / "fake-west"
    bead_payload = {"id": "B-42", "state": "open", "cwd": str(manifest_repo)}
    handoff_payload = {"ready": True, "changes": [], "cwd": str(manifest_repo)}
    patch_payload = {
        "schema_version": 1,
        "operation": "status",
        "state": "clean",
        "profile": "contract-profile",
    }
    doctor_payload = {
        "schema_version": 1,
        "operation": "doctor",
        "state": "healthy",
        "returncode": 0,
    }
    fake_program = f"""#!{sys.executable}
import json
import os
import sys
args = sys.argv[1:]
if os.getcwd() != {str(manifest_repo)!r}:
    print(\"unexpected west cwd\", file=sys.stderr)
    raise SystemExit(96)
if args == [\"dw\", \"beads\", \"show\", \"B-42\", \"--json\"]:
    print(json.dumps({bead_payload!r}, sort_keys=True, separators=(\",\", \":\")))
elif args == [\"dw\", \"handoff\", \"--dry-run\", \"--json\"]:
    print(json.dumps({handoff_payload!r}, sort_keys=True, separators=(\",\", \":\")))
elif args == [\"patch\", \"status\", \"--profile=contract-profile\", \"--strict\", \"--json\"]:
    print(json.dumps({patch_payload!r}, sort_keys=True, separators=(\",\", \":\")))
elif args == [\"patch\", \"status\", \"--profile=overflow-profile\", \"--strict\", \"--json\"]:
    sys.stdout.write(\"X\" * ({dev_status.MAX_CAPTURE_BYTES} + 17))
elif args == [\"darling-doctor\", \"--prefix={str(prefix)}\", \"--build-dir={str(build_dir)}\", \"--json\"]:
    print(json.dumps({doctor_payload!r}, sort_keys=True, separators=(\",\", \":\")))
elif args == [\"darling-doctor\", \"--prefix={str(oversized_root)}\", \"--json\"]:
    print(json.dumps({doctor_payload!r}, sort_keys=True, separators=(\",\", \":\")))
elif args == [\"darling-doctor\", \"--json\"]:
    print(json.dumps({doctor_payload!r}, sort_keys=True, separators=(\",\", \":\")))
else:
    print(\"unexpected fake west invocation: \" + repr(args), file=sys.stderr)
    raise SystemExit(97)
"""
    write_executable(fake_west, fake_program)

    manifest = FakeManifest(
        [
            FakeProject("dirty", dirty_repo, "dirty-revision", groups=("zeta", "alpha")),
            FakeProject("clean", clean_repo, "clean-revision", groups=("core",)),
        ]
    )
    west_argv = [str(fake_west)]
    before = byte_snapshot(root)

    with mock.patch.dict(os.environ, isolated_env, clear=True):
        status = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            "B-42",
            prefix,
            build_dir,
            west_argv,
        )
        repeated = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            "B-42",
            prefix,
            build_dir,
            west_argv,
        )
        optional = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            None,
            None,
            None,
            west_argv,
        )
        overflow = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "overflow-profile",
            None,
            prefix,
            build_dir,
            west_argv,
        )

    assert byte_snapshot(root) == before, "collect_status mutated an inspected fixture"

    serialized = json.dumps(status, sort_keys=True, separators=(",", ":"))
    assert serialized == json.dumps(repeated, sort_keys=True, separators=(",", ":"))
    assert status == repeated
    assert json.loads(serialized) == status
    assert status["schema_version"] == dev_status.SCHEMA_VERSION == 1
    assert status["operation"] == "status"
    assert status["transaction_id"] is None
    assert status["state"] == "healthy"
    assert status["degraded_sections"] == []
    assert status["in_progress_sections"] == []
    assert status["next_safe_action"] == "west dev check quick --profile contract-profile"
    assert status["inputs"] == {
        "topdir": str(workspace),
        "manifest_repo": str(manifest_repo),
        "profile": "contract-profile",
        "bead": "B-42",
        "prefix": str(prefix),
        "build_dir": str(build_dir),
        "west_argv": west_argv,
    }
    assert status["limits"]["capture_bytes"] == dev_status.MAX_CAPTURE_BYTES
    assert status["limits"]["discovery_entries_scanned"] == (
        dev_status.MAX_DISCOVERY_ENTRIES_SCANNED
    )
    assert status["limits"]["registry_entries_scanned"] == (
        dev_status.MAX_REGISTRY_ENTRIES_SCANNED
    )
    assert status["limits"]["status_entries"] == dev_status.MAX_STATUS_ENTRIES

    results = status["results"]
    expected_authorities = {
        "manifest": "West manifest",
        "git": "Git porcelain v2",
        "bead": "west dw beads routed Bead JSON",
        "handoff": "west dw handoff --dry-run --json",
        "patch": "west patch status typed profile composition",
        "doctor": "west darling-doctor authoritative defaults with explicit overrides",
        "source_worktrees": "west_commands.source_worktree record version 1",
        "deployment_transactions": "west_commands.deploy_transaction manifest version 1",
        "temporary_worktrees": "git worktree list --porcelain and west test temporary-worktree naming",
        "long_jobs": "scripts/west-job.sh global registry and status command",
    }
    assert {name: section["authority"] for name, section in results.items()} == expected_authorities
    assert all(section["health"] == "healthy" for section in results.values())

    manifest_result = results["manifest"]
    assert manifest_result["manifest_repo"] == str(manifest_repo)
    assert manifest_result["project_count"] == 2
    assert manifest_result["truncated"] is False
    projects = {project["name"]: project for project in manifest_result["projects"]}
    assert projects["clean"] == {
        "name": "clean",
        "path": str(clean_repo),
        "revision": "clean-revision",
        "active": True,
        "groups": ["core"],
    }
    assert projects["dirty"]["groups"] == ["alpha", "zeta"]

    repositories = {item["name"]: item for item in results["git"]["repositories"]}
    assert set(repositories) == {"manifest", "clean", "dirty"}
    assert repositories["manifest"]["head"] == manifest_head
    assert repositories["manifest"]["branch"] == "main"
    assert repositories["manifest"]["dirty"] is False
    assert repositories["clean"]["head"] == clean_head
    assert repositories["clean"]["branch"] == "main"
    assert repositories["clean"]["dirty"] is False
    assert repositories["dirty"]["head"] == dirty_head
    assert repositories["dirty"]["branch"] == "main"
    assert repositories["dirty"]["dirty"] is True
    assert repositories["dirty"]["entry_count"] == dirty_count
    assert len(repositories["dirty"]["entries"]) == dev_status.MAX_STATUS_ENTRIES
    assert repositories["dirty"]["entries_truncated"] is True
    for item in repositories.values():
        assert item["argv"] == [
            "git",
            "-C",
            item["path"],
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=normal",
        ]

    assert results["bead"]["argv"] == west_argv + [
        "dw",
        "beads",
        "show",
        "B-42",
        "--json",
    ]
    assert results["bead"]["data"] == bead_payload
    assert results["handoff"]["argv"] == west_argv + [
        "dw",
        "handoff",
        "--dry-run",
        "--json",
    ]
    assert results["handoff"]["data"] == handoff_payload
    assert results["patch"]["argv"] == west_argv + [
        "patch",
        "status",
        "--profile=contract-profile",
        "--strict",
        "--json",
    ]
    assert results["patch"]["data"] == patch_payload
    assert results["doctor"]["argv"] == west_argv + [
        "darling-doctor",
        f"--prefix={prefix}",
        f"--build-dir={build_dir}",
        "--json",
    ]
    assert results["doctor"]["data"] == doctor_payload

    assert results["source_worktrees"] == {
        "authority": "west_commands.source_worktree record version 1",
        "health": "healthy",
        "records": [{"path": str(source_record_path), "record": source_record}],
        "invalid": [],
        "truncated": False,
    }
    assert results["deployment_transactions"] == {
        "authority": "west_commands.deploy_transaction manifest version 1",
        "health": "healthy",
        "in_progress": False,
        "active": [],
        "transactions": [
            {
                "authority": "west_commands.deploy_transaction manifest version 1",
                "health": "healthy",
                "state": "committed",
                "path": str(deploy_manifest_path),
                "manifest": deploy_manifest,
            }
        ],
        "invalid": [],
        "truncated": False,
    }
    temporary_result = results["temporary_worktrees"]
    assert temporary_result["failures"] == []
    assert temporary_result["truncated"] is False
    assert temporary_result["worktrees"] == [
        {
            "path": str(temporary_worktree),
            "head": clean_head,
            "branch": "contract-temporary",
            "repository": str(clean_repo),
        }
    ]

    jobs = results["long_jobs"]
    assert jobs["registries"] == [str(job_root / ".west-job-registry")]
    assert jobs["invalid"] == []
    assert jobs["truncated"] is False
    assert jobs["in_progress"] is False
    assert jobs["active"] == []
    assert jobs["coverage"] == "complete"
    assert jobs["coverage_health"] == "healthy"
    assert len(jobs["jobs"]) == 1
    job = jobs["jobs"][0]
    assert job["authority"] == "scripts/west-job.sh registry entry and status command"
    assert job["health"] == "healthy"
    assert job["state"] == "completed"
    assert job["entry"] == str(registry_entry)
    assert job["state_dir"] == str(job_state)
    assert job["pid"] == 4242
    assert job["start_time"] == "2026-01-02T03:04:05Z"
    assert job["command"] == "west test contract"
    assert job["recorded_rc"] == 0
    assert job["status"]["argv"] == [
        str(job_script),
        "status",
        "--state-dir",
        str(job_state),
    ]
    assert job["status"]["stdout"] == "job-status: complete\n"

    assert optional["state"] == "healthy"
    assert optional["in_progress_sections"] == []
    assert optional["degraded_sections"] == []
    assert optional["results"]["bead"] == {
        "authority": "west dw beads show --json",
        "health": "unavailable",
        "reason": "no bead was requested",
    }
    assert optional["results"]["doctor"]["authority"] == (
        "west darling-doctor authoritative defaults with explicit overrides"
    )
    assert optional["results"]["doctor"]["health"] == "healthy"
    assert optional["results"]["doctor"]["argv"] == west_argv + [
        "darling-doctor",
        "--json",
    ]
    assert optional["results"]["doctor"]["data"] == doctor_payload

    overflow_patch = overflow["results"]["patch"]
    assert overflow_patch["health"] == "degraded"
    assert overflow_patch["rc"] == 0
    assert overflow_patch["stdout"] == "X" * dev_status.MAX_CAPTURE_BYTES
    assert overflow_patch["stdout_truncated"] is True
    assert overflow_patch["stderr_truncated"] is False
    assert overflow["state"] == "degraded"
    assert overflow["in_progress_sections"] == []
    assert overflow["degraded_sections"] == ["patch"]
    assert overflow["next_safe_action"] == (
        "resolve degraded status authorities, then rerun west dev status"
    )

    registry_lock_path = registry_entry.parent / ".lock"
    writer_lock_before = byte_snapshot(root)
    with registry_lock_path.open("rb") as writer_lock:
        fcntl.flock(writer_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.dict(os.environ, isolated_env, clear=True):
            writer_locked_status = dev_status.collect_status(
                workspace,
                manifest_repo,
                manifest,
                "contract-profile",
                None,
                None,
                None,
                west_argv,
            )
            repeated_writer_locked_status = dev_status.collect_status(
                workspace,
                manifest_repo,
                manifest,
                "contract-profile",
                None,
                None,
                None,
                west_argv,
            )
    assert byte_snapshot(root) == writer_lock_before
    writer_locked_jobs = writer_locked_status["results"]["long_jobs"]
    assert writer_locked_jobs == repeated_writer_locked_status["results"]["long_jobs"]
    assert writer_locked_jobs["health"] == "busy"
    assert writer_locked_jobs["in_progress"] is True
    assert writer_locked_jobs["active"] == [str(registry_entry.parent)]
    assert writer_locked_jobs["busy_registries"] == [str(registry_entry.parent)]
    assert writer_locked_jobs["coverage"] == "incomplete"
    assert writer_locked_jobs["coverage_health"] == "busy"
    assert writer_locked_jobs["registries"] == []
    assert writer_locked_jobs["jobs"] == []
    assert writer_locked_jobs["invalid"] == []
    assert writer_locked_status["state"] == "in_progress"
    assert writer_locked_status["in_progress_sections"] == ["long_jobs"]

    rc_path = job_state / "rc"
    rc_path.write_text("7\n", encoding="utf-8")
    failed_job_before = byte_snapshot(root)
    with mock.patch.dict(os.environ, isolated_env, clear=True):
        failed_job_status = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            None,
            None,
            None,
            west_argv,
        )
    assert byte_snapshot(root) == failed_job_before
    failed_jobs = failed_job_status["results"]["long_jobs"]
    assert failed_jobs["health"] == "degraded"
    assert failed_jobs["in_progress"] is False
    assert failed_jobs["jobs"][0]["state"] == "completed"
    assert failed_jobs["jobs"][0]["recorded_rc"] == 7
    assert failed_jobs["jobs"][0]["health"] == "degraded"
    assert failed_job_status["state"] == "degraded"
    assert failed_job_status["in_progress_sections"] == []
    assert failed_job_status["degraded_sections"] == ["long_jobs"]
    rc_path.write_text("0\n", encoding="utf-8")

    live_start_time = dev_status._proc_start_time(os.getpid())
    assert live_start_time is not None
    pid_path = registry_entry / "pid"
    start_time_path = registry_entry / "start-time"
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    start_time_path.write_text(live_start_time + "\n", encoding="utf-8")
    live_job_before = byte_snapshot(root)
    with mock.patch.dict(os.environ, isolated_env, clear=True):
        live_job_status = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            None,
            None,
            None,
            west_argv,
        )
    assert byte_snapshot(root) == live_job_before
    live_jobs = live_job_status["results"]["long_jobs"]
    assert live_jobs["health"] == "busy"
    assert live_jobs["in_progress"] is True
    assert live_jobs["active"] == [str(job_state)]
    assert live_jobs["jobs"][0]["state"] == "in_progress"
    assert live_jobs["jobs"][0]["health"] == "busy"
    assert live_job_status["state"] == "in_progress"
    assert live_job_status["in_progress_sections"] == ["long_jobs"]
    assert live_job_status["degraded_sections"] == []
    assert live_job_status["next_safe_action"] == (
        "wait for registered long jobs and active deployments; recover interrupted "
        "deployment transactions; then rerun west dev status"
    )
    pid_path.write_text("4242\n", encoding="utf-8")
    start_time_path.write_text("2026-01-02T03:04:05Z\n", encoding="utf-8")

    proof_directory = job_root / "west-red-proof-deploy-contract"
    proof_directory.mkdir()
    proof_manifest_path = proof_directory / "manifest.json"
    proof_manifest = {
        "version": 1,
        "state": "active",
        "prefix": str(prefix),
        "roots": [str(prefix)],
        "entries": [],
        "directories": [],
    }
    proof_manifest_path.write_text(
        json.dumps(proof_manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    active_deploy_before = byte_snapshot(root)
    with mock.patch.dict(os.environ, isolated_env, clear=True):
        active_deploy_status = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            None,
            prefix,
            build_dir,
            west_argv,
        )
    assert byte_snapshot(root) == active_deploy_before
    active_deployments = active_deploy_status["results"]["deployment_transactions"]
    assert active_deployments["health"] == "busy"
    assert active_deployments["in_progress"] is True
    assert active_deployments["active"] == [str(proof_manifest_path)]
    proof_transaction = next(
        transaction
        for transaction in active_deployments["transactions"]
        if transaction["path"] == str(proof_manifest_path)
    )
    assert proof_transaction["authority"] == (
        "west_commands.deploy_transaction manifest version 1"
    )
    assert proof_transaction["health"] == "busy"
    assert proof_transaction["state"] == "in_progress"
    assert proof_transaction["manifest"] == proof_manifest
    assert active_deploy_status["state"] == "in_progress"
    assert active_deploy_status["in_progress_sections"] == ["deployment_transactions"]
    assert active_deploy_status["degraded_sections"] == []
    assert active_deploy_status["next_safe_action"] == (
        "wait for registered long jobs and active deployments; recover interrupted "
        "deployment transactions; then rerun west dev status"
    )
    proof_manifest_path.unlink()
    proof_directory.rmdir()
    missing_job_root = root / "missing-job-state-root"
    root_error_env = {
        **isolated_env,
        "WEST_DEV_JOB_STATE_ROOTS": str(missing_job_root),
    }
    root_error_before = byte_snapshot(root)
    with mock.patch.dict(os.environ, root_error_env, clear=True):
        root_error_status = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            None,
            None,
            None,
            west_argv,
        )
    assert byte_snapshot(root) == root_error_before
    root_error_jobs = root_error_status["results"]["long_jobs"]
    assert root_error_jobs["health"] == "degraded"
    assert root_error_jobs["coverage"] == "incomplete"
    assert root_error_jobs["coverage_health"] == "degraded"
    assert [item["path"] for item in root_error_jobs["invalid"]] == [
        str(missing_job_root)
    ]
    assert root_error_jobs["invalid"][0]["error"]


    oversized_root.mkdir()
    discovery_files: list[Path] = []
    for index in range(dev_status.MAX_DISCOVERY_FILES + 1):
        path = oversized_root / f".west-source-worktree-overflow-{index:03d}.json"
        path.write_text(
            json.dumps({"version": 1, "gitlinks": []}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        discovery_files.append(path)
    extra_registry_entries: list[Path] = []
    for index in range(dev_status.MAX_REGISTRY_ENTRIES + 1):
        entry = job_root / ".west-job-registry" / f"overflow-{index:03d}"
        entry.mkdir()
        extra_registry_entries.append(entry)
    bounded_before = byte_snapshot(root)
    with mock.patch.dict(os.environ, isolated_env, clear=True):
        bounded_status = dev_status.collect_status(
            workspace,
            manifest_repo,
            manifest,
            "contract-profile",
            None,
            oversized_root,
            None,
            west_argv,
        )
    assert byte_snapshot(root) == bounded_before
    bounded_sources = bounded_status["results"]["source_worktrees"]
    assert bounded_sources["health"] == "degraded"
    assert bounded_sources["truncated"] is True
    assert len(bounded_sources["records"]) <= dev_status.MAX_DISCOVERY_FILES
    assert bounded_sources["records"] == []
    bounded_jobs = bounded_status["results"]["long_jobs"]
    assert bounded_jobs["health"] == "degraded"
    assert bounded_jobs["truncated"] is True
    assert bounded_jobs["coverage"] == "incomplete"
    assert bounded_jobs["coverage_health"] == "degraded"
    assert bounded_jobs["jobs"] == []
    assert (
        len(bounded_jobs["jobs"]) + len(bounded_jobs["invalid"])
        <= dev_status.MAX_REGISTRY_ENTRIES
    )
    assert bounded_status["state"] == "degraded"
    assert "source_worktrees" in bounded_status["degraded_sections"]
    assert "long_jobs" in bounded_status["degraded_sections"]
    for entry in extra_registry_entries:
        entry.rmdir()
    for path in discovery_files:
        path.unlink()
    oversized_root.rmdir()
    assert byte_snapshot(root) == before

print("PASS dev-status-contract")

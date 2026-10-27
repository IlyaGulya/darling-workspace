"""Contract for parallel-experiment isolation lanes (`west_commands/lane.py`).

Every assertion here is behavioral: it either mounts a real lane over a real
base and observes what happened to both, or it drives the refusal path and
observes that nothing was removed. Nothing asserts source text.

Why a real FUSE layer and not a mock: the property under test is that a lane's
writes never reach its base and that a sibling lane survives its neighbour's
removal. A mock would answer both by construction. `/dev/fuse` plus
`fuse-overlayfs` is therefore a prerequisite of this contract, and its absence
is a failure that names what is missing rather than a silent pass.

What is proven:
  * copy-on-write: a write through the lane lands in the lane's upper layer and
    the base keeps its bytes, and the lane's prefix-state record names the
    lane's own directory identity;
  * the base is checkable: `verify` notices a mutated base;
  * lanes are independent: destroying one leaves the other's mount, daemon,
    payload and record intact;
  * removals are refusals where they should be: a directory that is not a lane
    is left alone;
  * the isolation a runner needs is present: each lane's prefix sits in its own
    directory (its own `west test` lock domain), with a private state root,
    temporary directory and runtime directory.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "west_commands"))

import lane as lane_module
import dev_lane as dev_lane_module
from lane import (
    UNSAFE_NO_SCOPE_NOTICE,
    LaneError,
    create_lane,
    destroy_lane,
    fuse_daemon_pids,
    lane_env,
    lane_path,
    lane_prefix,
    lane_status,
    list_lanes,
    mount_targets,
    read_record,
    recorded_source_path,
    record_problems,
    require_scope,
    scope_description,
    scope_ref,
    scope_unit,
    source_identity,
    tree_identity,
    validate_lane_name,
    verify_lane,
)
from prefix_repair import prefix_mount_targets
from runtime_install import InstalledRuntime

PREFIX_STATE = ".darling-prefix-state-v2"

with tempfile.TemporaryDirectory(prefix="west-lane-state-only-") as temporary:
    admission_root = Path(temporary)
    admission_base = admission_root / "base"
    admission_base.mkdir()
    admission_lane = admission_root / "isolated"
    admission_prefix = lane_prefix(admission_lane)
    admission_prefix.mkdir(parents=True)
    admission_record = {"name": admission_lane.name, "base": str(admission_base)}
    assert record_problems(admission_lane, admission_record) == []
    assert not (admission_prefix / "bin/darling").exists()
    admission_prefix.rmdir()
    assert record_problems(admission_lane, admission_record)


def expect_refusal(description, call, expected):
    try:
        call()
    except expected:
        return
    raise AssertionError(f"{description}: expected {expected.__name__}, none was raised")


def make_base(root: Path, name: str = "base") -> Path:
    """A stable writable guest-state prefix; the runtime is a separate installation."""

    base = root / name
    (base / "private" / "var" / "tmp").mkdir(parents=True)
    (base / "payload.txt").write_text("base payload\n")
    (base / PREFIX_STATE).write_text(
        "DARLING_PREFIX_STATE_V2\n"
        "schema_version=2\n"
        "runtime_mode=rootless-eunion\n"
        "generation=7\n"
        "prefix_device=1\n"
        "prefix_inode=2\n"
        "owner_uid=3\n"
        "owner_gid=4\n"
        "provenance=darling-runtime-prefix-lifecycle-v2\n"
    )
    return base


def make_runtime(root: Path, name: str = "runtime") -> InstalledRuntime:
    """A real launcher-shaped bind-clone stand-in for the lane/prefix contracts."""

    runtime_root = root / name
    (runtime_root / "bin").mkdir(parents=True)
    system_root = runtime_root / "libexec" / "darling"
    system_root.mkdir(parents=True)
    # The content baseline knows these deployed paths; provide them so the runtime
    # artifact receipt exercises a real save/check rather than an empty manifest.
    (runtime_root / "bin" / "darlingserver").write_bytes(b"synthetic darlingserver\n")
    (system_root / "usr" / "lib").mkdir(parents=True)
    (system_root / "usr" / "lib" / "dyld").write_bytes(b"synthetic dyld\n")
    launcher = runtime_root / "bin" / "darling"
    launcher.write_text(
        "#!/bin/sh\n"
        'if [ "$1" != "bind-clone" ] || [ -z "$2" ]; then\n'
        '  echo "unsupported launcher operation" >&2\n'
        "  exit 2\n"
        "fi\n"
        'test -f "$2/.darling-prefix-state-v2" || { echo "base has no lifecycle record" >&2; exit 3; }\n'
        'printf "DARLING_PREFIX_STATE_V2\\nschema_version=2\\nruntime_mode=rootless-eunion\\n'
        'generation=1\\nprefix_device=%s\\nprefix_inode=%s\\nowner_uid=%s\\nowner_gid=%s\\n'
        'provenance=test-bind-clone\\n" "$(stat -c %d "$DPREFIX")" "$(stat -c %i "$DPREFIX")" '
        '"$(id -u)" "$(id -g)" > "$DPREFIX/.darling-prefix-state-v2"\n'
    )
    launcher.chmod(0o755)
    return InstalledRuntime(
        root=runtime_root,
        launcher=launcher,
        system_root=system_root,
        identity={"schema": "contract"},
    )


def make_repo(path: Path) -> Path:
    """A one-commit Git checkout, so a lane can record an exact manifest revision."""

    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "west.yml").write_text("manifest:\n  projects: []\n")
    commit(path, "one")
    return path


def commit(path: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=contract@example.invalid",
            "-c",
            "user.name=contract",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def pure_contract(root: Path) -> None:
    for good in ("laneA", "a", "lane-1.2", "L" * 48):
        assert validate_lane_name(good) == good, good
    for bad in ("", ".", "..", "a/b", "../x", "-lead", "L" * 49, "a b", "/abs"):
        expect_refusal(f"lane name {bad!r}", lambda bad=bad: validate_lane_name(bad), LaneError)

    guarded_base = make_base(root, "guarded-base")
    guarded_runtime = make_runtime(root, "guarded-runtime")
    guarded_lanes = root / "guarded-lanes"
    base_alias = root / "guarded-base-alias"
    base_alias.symlink_to(guarded_base, target_is_directory=True)
    for unsafe in (
        str(base_alias),
        f"{root}/./guarded-base",
        f"{root}/guarded-base/../guarded-base",
    ):
        expect_refusal(
            f"unsafe base {unsafe}",
            lambda unsafe=unsafe: create_lane(
                unsafe, guarded_lanes, "rejected", runtime=guarded_runtime
            ),
            LaneError,
        )
        assert not guarded_lanes.exists(), "unsafe base admission created lane state"
    (guarded_base / "bin").mkdir()
    (guarded_base / "bin" / "darling").write_text("obsolete launcher\n")
    (guarded_base / "bin" / "darlingserver").write_text("obsolete server\n")
    (guarded_base / "libexec" / "darling").mkdir(parents=True)
    expect_refusal(
        "legacy combined base",
        lambda: create_lane(guarded_base, guarded_lanes, "rejected", runtime=guarded_runtime),
        LaneError,
    )
    assert not guarded_lanes.exists(), "legacy base admission created a lane or copied runtime"

    unit = scope_unit(lane_path(root, "laneA"))
    assert unit.startswith("darling-lane-laneA-"), unit
    assert scope_ref(unit) == f"{unit}.scope"
    assert scope_ref(f"{unit}.scope") == f"{unit}.scope"
    # systemd unit names are global to the user manager while lanes are not: two lane roots
    # choosing one name must not end up claiming one unit and stopping each other's experiments.
    assert scope_unit(root / "other-root" / "laneA") != unit

    # A lane's prefix must sit in its own directory: `west test` serializes concurrent prefix
    # runs by flocking the prefix's PARENT directory, so a shared parent re-serializes lanes.
    first = lane_path(root, "one")
    second = lane_path(root, "two")
    assert lane_prefix(first).parent == first
    assert lane_prefix(second).parent == second
    assert lane_prefix(first).parent != lane_prefix(second).parent
    assert scope_description(first) != scope_description(second)

    environment = lane_env(
        first, {"name": "one", "source": {"path": "/src/one", "manifest_head": "deadbeef"}}
    )
    assert environment["DPREFIX"] == str(lane_prefix(first))
    assert environment["DARLING_PREFIX"] == str(lane_prefix(first))
    assert environment["DW_STATE_ROOT"] == str(first / "state")
    assert environment["TMPDIR"] == str(first / "tmp")
    assert environment["XDG_RUNTIME_DIR"] == str(first / "run")
    assert environment["DW_LANE"] == str(first)
    # A recorded source is an identity object; the environment must carry its PATH. Exporting the
    # object published a Python dict literal, which is not a path any tool can use.
    assert environment["DW_LANE_SOURCE"] == "/src/one"
    assert environment["DARLING_BUILD_DIR"] == str(first / "build")
    assert environment["DW_LANE_BUILD_DIR"] == str(first / "build")
    assert recorded_source_path({"source": "/plain/string"}) == "/plain/string"
    assert recorded_source_path({"source": {"path": "/from/dict"}}) == "/from/dict"
    assert recorded_source_path({"source": {"manifest_head": "only-an-identity"}}) is None
    assert recorded_source_path({}) is None

    # The lane record carries the selected runtime; Python never parses or rewrites
    # the lifecycle record.
    assert "DARLING_RUNTIME_ROOT" not in environment
    # Fail closed. Process ownership IS the isolation, so a lane run without a working scope must be
    # refused rather than started unowned.
    real_scope_available = lane_module.scope_available
    try:
        lane_module.scope_available = lambda: True
        require_scope()  # must not raise
        lane_module.scope_available = lambda: False
        expect_refusal("a lane run with no scope", require_scope, LaneError)
        try:
            require_scope()
        except LaneError as error:
            assert "no per-lane process isolation available" in str(error), str(error)
    finally:
        lane_module.scope_available = real_scope_available

    # The unisolated mode is labelled, and the label is the spec: it must say who it is unsafe for.
    assert "UNSAFE FOR CONCURRENT AGENTS" in UNSAFE_NO_SCOPE_NOTICE
    assert "cleanup" in UNSAFE_NO_SCOPE_NOTICE
    assert "single-agent" in UNSAFE_NO_SCOPE_NOTICE
    # Read from the REAL parser, not from a helper that could drift away from it.
    import argparse

    parser = argparse.ArgumentParser(prog="west dev")
    subparsers = parser.add_subparsers(dest="action", required=True)
    dev_lane_module.add_lane_parsers(subparsers)
    lane_parser = subparsers.choices["lane"]
    exec_parser = next(
        candidate
        for action in lane_parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for name, candidate in action.choices.items()
        if name == "exec"
    )
    no_scope_help = exec_parser.format_help()
    assert "UNSAFE FOR CONCURRENT AGENTS" in no_scope_help, no_scope_help
    assert "single-agent" in no_scope_help

    # A mount at the prefix path is the prefix's own layer only when it is a FUSE overlay;
    # a kernel overlay there is the privileged runtime's own mount and stays counted.
    prefix = root / "mountinfo-prefix"
    prefix.mkdir()
    mountinfo = root / "mountinfo"
    mountinfo.write_text(
        f"40 30 0:42 / {prefix} rw,relatime - fuse.fuse-overlayfs fuse-overlayfs rw\n"
        f"41 30 0:43 / {prefix}/private/var/tmp rw,relatime - tmpfs tmpfs rw\n"
    )
    deep = prefix / "private" / "var" / "tmp"
    assert prefix_mount_targets(prefix, mountinfo_path=mountinfo) == [deep]
    assert mount_targets(prefix, mountinfo_path=mountinfo) == [deep, prefix]
    mountinfo.write_text(f"42 30 0:44 / {prefix} rw,relatime - overlay overlay rw\n")
    assert prefix_mount_targets(prefix, mountinfo_path=mountinfo) == [prefix]


def mounted_contract(root: Path) -> None:
    lanes = root / "lanes"
    base = make_base(root)
    runtime = make_runtime(root)
    before = tree_identity(base)
    runtime_before = tree_identity(runtime.root)
    state_before = (base / PREFIX_STATE).read_bytes()

    created, first = create_lane(base, lanes, "one", runtime=runtime)
    assert first is not None
    assert created.success, created.problems
    assert created.changed, "create must report what it did"
    assert read_record(first) is not None, "create must leave a lane record"
    assert read_record(first)["base"]["identity"] == before
    assert read_record(first)["runtime"]["root"] == str(runtime.root)

    merged = lane_prefix(first)
    assert (merged / "payload.txt").read_text() == "base payload\n"
    assert (merged / PREFIX_STATE).read_text().startswith("DARLING_PREFIX_STATE_V2\n")
    assert "provenance=test-bind-clone" in (merged / PREFIX_STATE).read_text()

    # Copy-on-write, the whole point: the write lands in the lane and the base keeps its bytes.
    (merged / "payload.txt").write_text("lane payload\n")
    assert (merged / "payload.txt").read_text() == "lane payload\n"
    assert (base / "payload.txt").read_text() == "base payload\n"
    assert (first / "upper" / "payload.txt").read_text() == "lane payload\n"
    assert tree_identity(base) == before, "a lane write reached its base"
    assert (base / PREFIX_STATE).read_bytes() == state_before
    verified = verify_lane(first)
    assert verified.success, verified.problems
    assert any("content matches its sha256 baseline" in message for message in verified.changed), (
        verified.changed
    )
    receipt = first / read_record(first)["runtime"]["artifact_manifest"]["path"]
    assert receipt.is_file(), "a lane must keep the runtime content baseline it recorded"
    assert tree_identity(runtime.root) == runtime_before

    # `verify` must notice a base that someone else wrote to. A separate base, because
    # rewriting a file changes its mtime as well as its bytes, and that is the point.
    drift_base = make_base(root, "drift-base")
    _, drift = create_lane(drift_base, lanes, "drift", runtime=runtime)
    assert drift is not None
    assert verify_lane(drift).success, verify_lane(drift).problems
    (drift_base / "payload.txt").write_text("mutated elsewhere\n")
    drifted = verify_lane(drift)
    assert not drifted.success, "verify accepted a mutated base"
    assert any("changed since the lane was created" in problem for problem in drifted.problems), (
        drifted.problems
    )

    # The tree digest and the content baseline are DIFFERENT claims. A same-size, same-mtime rewrite
    # of a base artifact leaves the stat identity identical and changes the content: the sha256
    # baseline must catch what a stat walk cannot.
    content_base = make_base(root, "content-base")
    content_runtime = make_runtime(root, "content-runtime")
    _, content_lane = create_lane(content_base, lanes, "content", runtime=content_runtime)
    assert content_lane is not None
    identity_before = tree_identity(content_runtime.root)
    artifact = content_runtime.root / "bin" / "darlingserver"
    artifact_stat = os.stat(artifact)
    original = artifact.read_bytes()
    artifact.write_bytes(b"E" * len(original))
    os.utime(artifact, ns=(artifact_stat.st_atime_ns, artifact_stat.st_mtime_ns))
    assert tree_identity(content_runtime.root) == identity_before, (
        "this fixture must keep the stat identity identical"
    )
    content_report = verify_lane(content_lane)
    assert not content_report.success, content_report.problems
    assert any("changed content" in problem for problem in content_report.problems), (
        content_report.problems
    )

    # A second lane over the same base must have its own layer, record and daemon.
    _, second = create_lane(base, lanes, "two", runtime=runtime)
    assert second is not None
    assert (lane_prefix(second) / "payload.txt").read_text() == "base payload\n"
    assert (second / "upper" / PREFIX_STATE).is_file(), (
        "the product bind-clone record must land in the lane's own upper layer"
    )
    assert sorted(entry.name for entry in list_lanes(lanes)) == [
        "content",
        "drift",
        "one",
        "two",
    ]
    second_daemon_before = sorted(fuse_daemon_pids(second))
    assert second_daemon_before, "the second lane has no fuse-overlayfs daemon of its own"

    # Removing one lane must not reach the other: same mount, same payload, same daemon.
    removed = destroy_lane(first)
    assert removed.success, removed.problems
    assert not first.exists()
    # The lane's layer daemon must be gone afterwards, and that is asserted as the outcome rather
    # than as a message: a graceful unmount asks the daemon to release the connection and it exits by
    # itself, so reaping a survivor is only the fallback and says nothing about the normal path.
    assert fuse_daemon_pids(first) == [], fuse_daemon_pids(first)
    assert (lane_prefix(second) / "payload.txt").read_text() == "base payload\n"
    assert mount_targets(lane_prefix(second)), "removing one lane unmounted another"
    assert sorted(fuse_daemon_pids(second)) == second_daemon_before
    assert read_record(second) is not None
    assert tree_identity(base) == before, "a lane removal touched the base"

    # Status reports a usable lane without changing it.
    status = lane_status(second)
    assert status["mounted"] is True and status["prefix"] == str(lane_prefix(second))
    assert status["state_root"] == str(second / "state")
    assert status["base_same_identity"] is True
    assert status["processes"] == []
    assert status["upper_files"] >= 1

    # A directory that is not a lane is refused, and left alone.
    impostor = lanes / "not-a-lane"
    impostor.mkdir(parents=True)
    (impostor / "keep.txt").write_text("keep\n")
    expect_refusal("removing a non-lane directory", lambda: destroy_lane(impostor), LaneError)
    assert (impostor / "keep.txt").is_file()
    expect_refusal(
        "a lane name collision", lambda: create_lane(base, lanes, "two", runtime=runtime), LaneError
    )
    empty_base = root / "empty-base"
    empty_base.mkdir()
    expect_refusal(
        "a base without a lifecycle record",
        lambda: create_lane(empty_base, lanes, "three", runtime=runtime),
        LaneError,
    )

    # A lane whose base vanished is reported, not used.
    vanished = make_base(root, "vanished-base")
    _, orphan = create_lane(vanished, lanes, "orphan", runtime=runtime)
    assert orphan is not None
    shutil.rmtree(vanished)
    problems = record_problems(orphan, read_record(orphan))
    assert any("base" in problem for problem in problems), problems

    # An exact manifest revision is recorded, and a moved source is reported.
    source = make_repo(root / "source")
    _, fourth = create_lane(
        make_base(root, "fourth-base"), lanes, "four", source=source, runtime=runtime
    )
    assert fourth is not None
    recorded = read_record(fourth)["source"]
    assert recorded["manifest_head"] == source_identity(source)["manifest_head"]
    assert recorded["west_yml_sha256"] != "absent"
    assert verify_lane(fourth).success, verify_lane(fourth).problems
    (source / "west.yml").write_text("manifest:\n  projects: [extra]\n")
    commit(source, "two")
    moved = verify_lane(fourth)
    assert not moved.success, "verify accepted a source whose manifest moved"
    assert any("manifest moved" in problem for problem in moved.problems), moved.problems

    for name in ("two", "drift", "content", "orphan", "four"):
        cleanup = destroy_lane(lanes / name)
        assert cleanup.success, cleanup.problems
    shutil.rmtree(impostor)
    assert list_lanes(lanes) == []
    assert mount_targets(merged) == [], "a lane layer survived its removal"
    assert tree_identity(runtime.root) == runtime_before


def main() -> None:
    for tool in ("fuse-overlayfs", "fusermount3"):
        if not shutil.which(tool):
            raise AssertionError(
                "this contract mounts real isolation lanes and needs fuse-overlayfs and "
                f"fusermount3 on PATH; {tool} was not found"
            )
    if not Path("/dev/fuse").exists():
        raise AssertionError(
            "this contract mounts real isolation lanes and needs /dev/fuse; it is absent"
        )
    temp = Path(tempfile.mkdtemp(prefix="darling-lane-contract-"))
    try:
        pure_contract(temp)
        mounted_contract(temp)
    finally:
        # Discover rather than remember: a failure inside the mounted contract must not leave a
        # mount behind, and `rmtree` of a still-mounted lane would delete through the layer.
        for lane in list_lanes(temp):
            try:
                cleanup = destroy_lane(lane)
                if not cleanup.success:
                    print(f"cleanup: destroy {lane} refused: {cleanup.problems}", file=sys.stderr)
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the real failure
                print(f"cleanup: destroy {lane} raised {error!r}", file=sys.stderr)
        leftovers = mount_targets(temp)
        if leftovers:
            raise AssertionError(f"contract left mounted lanes behind: {leftovers}")
        shutil.rmtree(temp, ignore_errors=True)
    print("PASS darling-lane-contract")


if __name__ == "__main__":
    main()

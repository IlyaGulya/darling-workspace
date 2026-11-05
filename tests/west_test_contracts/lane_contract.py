"""Real host snapshot-store inheritance, isolation and graph-lifetime regression.

Requires the provisioned lane snapshotter/client/reviewed FUSE driver, /dev/fuse,
fusermount3 and a working systemd user manager. No Darling runtime is fabricated
or launched: guest clone identity and runtime execution belong to the two
prefix-backed shell gates. Failure retains the owned scratch and mount evidence.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from lane import _collect_base, LaneError, destroy_lane, lane_path, mount_targets, validate_lane_name
from lane_provider import workspace_topdir
from lane_snapshotter import KIND_LABEL, STORE_LABEL, SnapshotStore, SnapshotStoreError


def refuses(call, exception):
    try:
        call()
    except exception:
        return
    raise AssertionError(f"expected {exception.__name__}")


def identity(path):
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def service_stopped(unit):
    result = subprocess.run(
        ["systemctl", "--user", "show", "--property=ActiveState,MainPID", unit],
        capture_output=True, text=True, timeout=30,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert properties["ActiveState"] == "inactive", properties
    assert properties["MainPID"] == "0", properties


def contract(root):
    for good in ("laneA", "a", "lane-1.2", "L" * 48):
        assert validate_lane_name(good) == good
    for bad in ("", ".", "..", "a/b", "../x", "-lead", "L" * 49, "a b", "/abs"):
        refuses(lambda: lane_path(root, bad), LaneError)
    unknown = root / "not-a-lane"
    unknown.mkdir()
    (unknown / "keeper").write_text("not owned by lanes\n")
    refuses(lambda: destroy_lane(unknown), LaneError)
    assert (unknown / "keeper").read_text() == "not owned by lanes\n"
    for unsafe in (root / ".." / root.name, root / "alias"):
        if unsafe.name == "alias":
            unsafe.symlink_to(root, target_is_directory=True)
        refuses(lambda: SnapshotStore(unsafe), SnapshotStoreError)

    base = root / "base"
    base.mkdir(mode=0o750)
    (base / "payload").write_text("base\n")
    (base / "deleted-by-parent").write_text("base deletion candidate\n")
    (base / "deleted-by-child").write_text("child deletion candidate\n")
    (base / "nested").mkdir()
    (base / "nested" / "inherited").write_text("nested base\n")
    store = SnapshotStore(root / "lanes")
    views = {name: root / name for name in ("parent", "child", "sibling", "grandchild")}
    mounted_identities = {}

    # A real owner dies during base import. Recovery uses the persisted native
    # graph, and a matching key is not authority to delete another owner's node.
    interrupted = "a" * 64
    child = subprocess.run([
        sys.executable, "-B", "-c",
        "import os,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n"
        "from lane_snapshotter import SnapshotStore,KIND_LABEL\n"
        "s=SnapshotStore(Path(sys.argv[2]))\n"
        "with s.locked(create=True):\n"
        "    r=s.prepare('import-'+sys.argv[3])\n"
        "    s.label('import-'+sys.argv[3],{KIND_LABEL:'base-import'})\n"
        "    (s.layer_path(r['Source'])/'partial').write_bytes(b'interrupted import')\n"
        "    os._exit(29)\n",
        str(ROOT / "west_commands"), str(store.lane_root), interrupted,
    ], capture_output=True, text=True, timeout=30)
    assert child.returncode == 29, child.stdout + child.stderr
    with store.locked():
        pending = store.info("import-" + interrupted)
        assert pending is not None and pending["Kind"] == "Active"
        _collect_base(store, "base-" + interrupted)
        assert store.info("import-" + interrupted) is None
        foreign_key = "base-" + "b" * 64
        store.prepare("foreign-import")
        store.label("foreign-import", {KIND_LABEL: "base"})
        store.commit(foreign_key, "foreign-import")
        store.label(foreign_key, {STORE_LABEL: "/unrelated-owner"})
        foreign_before = store.info(foreign_key)
        _collect_base(store, foreign_key)
        assert store.info(foreign_key) == foreign_before
        # Undo only this fixture's deliberate label mutation before teardown.
        store.label(foreign_key, {STORE_LABEL: str(store.root)})
        store.remove(foreign_key)

    with store.locked(create=True):
        assert store.snapshots() == []
        recipe = store.prepare("import")
        # The backend, not the fixture, chooses the real physical layer Source.
        source = store.layer_path(recipe["Source"])
        shutil.copytree(base, source, dirs_exist_ok=True, symlinks=True)
        assert identity(source) != identity(base)
        store.commit("base", "import")
        assert store.info("import") is None
        assert store.info("base") is not None
        # An imported base is independent of the original directory after copying.
        (base / "payload").write_text("external base changed after import\n")

        def mount(key, parent, inherited_source):
            target = views[key]
            target.mkdir()
            store.mount(store.prepare(key, parent), target, root / f"{key}.log", inherited_source)
            assert target.is_mount()
            mounted_identities[key] = identity(target)
            return target

        parent = mount("parent", "base", source)
        assert parent.stat().st_mode & 0o777 == 0o750
        assert (parent / "payload").read_text() == "base\n"
        refuses(lambda: store.commit("base", "parent"), SnapshotStoreError)
        assert store.info("parent")["Kind"] == "Active"
        (parent / "payload").write_text("frozen parent\n")
        (parent / "deleted-by-parent").unlink()
        (parent / "parent-only").write_text("parent addition\n")
        store.commit("frozen-parent", "parent")
        assert store.info("parent") is None
        assert parent.is_mount() and identity(parent) == mounted_identities["parent"]
        assert (source / "payload").read_text() == "base\n"
        assert (source / "deleted-by-parent").is_file()

        child = mount("child", "frozen-parent", parent)
        sibling = mount("sibling", "frozen-parent", parent)
        for view in (child, sibling):
            assert (view / "payload").read_text() == "frozen parent\n"
            assert not (view / "deleted-by-parent").exists()
            assert (view / "parent-only").read_text() == "parent addition\n"
            assert (view / "nested" / "inherited").read_text() == "nested base\n"
        (child / "payload").write_text("child update\n")
        (child / "deleted-by-child").unlink()
        (child / "child-only").write_text("child addition\n")
        (sibling / "sibling-only").write_text("sibling addition\n")
        for view in (parent, sibling):
            assert (view / "payload").read_text() == "frozen parent\n"
            assert (view / "deleted-by-child").is_file()
            assert not (view / "child-only").exists()
        assert not (child / "sibling-only").exists()
        assert not (parent / "sibling-only").exists()

        store.commit("frozen-child", "child")
        assert child.is_mount() and identity(child) == mounted_identities["child"]
        grandchild = mount("grandchild", "frozen-child", child)
        assert (grandchild / "payload").read_text() == "child update\n"
        assert (grandchild / "child-only").read_text() == "child addition\n"
        assert (grandchild / "parent-only").is_file()
        assert not (grandchild / "deleted-by-parent").exists()
        assert not (grandchild / "deleted-by-child").exists()
        assert not (grandchild / "sibling-only").exists()
        (grandchild / "payload").write_text("grandchild update\n")
        assert (child / "payload").read_text() == "child update\n"
        assert (sibling / "payload").read_text() == "frozen parent\n"

        graph_before = sorted(store.snapshots(), key=lambda entry: entry["Name"])
        refuses(lambda: store.remove("frozen-parent"), SnapshotStoreError)
        refuses(lambda: store.remove("frozen-child"), SnapshotStoreError)
        assert sorted(store.snapshots(), key=lambda entry: entry["Name"]) == graph_before
        for name, view in views.items():
            assert view.is_mount() and identity(view) == mounted_identities[name]
        assert (grandchild / "payload").read_text() == "grandchild update\n"
        assert (sibling / "sibling-only").read_text() == "sibling addition\n"

        # Reverse ancestry: remove no layer until its retained view is unmounted.
        for name, key in (("grandchild", "grandchild"), ("child", "frozen-child"),
                          ("sibling", "sibling"), ("parent", "frozen-parent")):
            target = views[name]
            subprocess.run(["fusermount3", "-u", str(target)], check=True, timeout=10)
            assert not target.is_mount()
            store.release_mount_service(target)
            service_stopped(store.mount_unit(target))
            store.remove(key)
            assert store.info(key) is None
            if name == "child":
                assert sibling.is_mount()
                assert (sibling / "payload").read_text() == "frozen parent\n"
        store.remove("base")
        assert store.snapshots() == []
        assert mount_targets(root) == []
        store.stop_if_empty()
    service_stopped(store.unit)
    assert not store.socket.exists()


state = Path(os.environ.get("DW_STATE_ROOT", workspace_topdir(ROOT) / ".west-state"))
if not state.is_absolute():
    raise AssertionError("DW_STATE_ROOT must be absolute")
scratch = state / "lane-contracts"
scratch.mkdir(parents=True, exist_ok=True)
root = Path(tempfile.mkdtemp(prefix="host-", dir=scratch))
try:
    contract(root)
except BaseException:
    print(f"lane contract failed; owned scratch/mount evidence retained: {root}", file=sys.stderr)
    raise
else:
    shutil.rmtree(root)
    print("lane snapshotter host contract: PASS")

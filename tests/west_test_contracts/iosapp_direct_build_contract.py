"""Host input/provenance and safety boundaries; never a positive guest oracle."""
from __future__ import annotations

import copy
import importlib.util
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("iosapp_direct", ROOT / "scripts/iosapp-direct-build.py")
direct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(direct)
from west_commands.test_store import durable_subdir


def refuses(call):
    try:
        call()
    except (ValueError, OSError, TypeError, KeyError):
        return
    raise AssertionError("unsafe input accepted")


def metadata():
    return ({"CFBundleShortVersionString": "26.3", "ProductBuildVersion": "17C529"},
            {"Version": "26.2", "CanonicalName": "iphoneos26.2", "DefaultProperties": {"PLATFORM_NAME": "iphoneos"},
             "SupportedTargets": {"iphoneos": {"LLVMTargetTripleSys": "ios", "LLVMTargetTripleVendor": "apple",
                "LLVMTargetTripleEnvironment": "", "BuildVersionPlatformID": "2", "Archs": ["arm64"],
                "ValidDeploymentTargets": ["17.0", "26.2"]}}})


xcode, sdk = metadata()
for change in (
    lambda value: value.update(Version="16.4", CanonicalName="iphoneos16.4"),
    lambda value: value["DefaultProperties"].update(PLATFORM_NAME="iphonesimulator"),
    lambda value: value["SupportedTargets"]["iphoneos"].update(LLVMTargetTripleEnvironment="simulator"),
    lambda value: value["SupportedTargets"]["iphoneos"].update(Archs=["x86_64"]),
    lambda value: value["SupportedTargets"]["iphoneos"].update(ValidDeploymentTargets=["26.2"]),
    lambda value: value.update(CanonicalName="iphoneos26.3"),
    lambda value: value.update(DefaultProperties=[]),
    lambda value: value.update(SupportedTargets=[]),
    lambda value: value["SupportedTargets"].update(iphoneos=None),
    lambda value: value["SupportedTargets"]["iphoneos"].update(Archs="arm64"),
    lambda value: value["SupportedTargets"]["iphoneos"].update(ValidDeploymentTargets="17.0"),
):
    changed = copy.deepcopy(sdk)
    change(changed)
    refuses(lambda: direct.validate_metadata(xcode, changed))

scratch = durable_subdir(ROOT.parent, "test-scratch") / "iosapp-direct"
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="contract-", dir=scratch) as temporary:
    base = Path(temporary)
    tree = base / "Xcode.app"
    tree.mkdir()
    source = tree / "payload"
    source.write_bytes(b"operator bytes")
    original_mode = stat.S_IMODE(source.stat().st_mode)
    outside = base / "outside"
    outside.write_bytes(b"not in supplied tree")
    (tree / "external").symlink_to(outside)
    original = direct.tree_inventory(tree)
    outside.write_bytes(b"external change must not be followed")
    assert direct.tree_inventory(tree) == original
    os.utime(source, (1, 1))
    assert direct.tree_inventory(tree) == original
    source.write_bytes(b"different bytes")
    assert direct.tree_inventory(tree) != original
    source.write_bytes(b"operator bytes")
    source.chmod(original_mode ^ stat.S_IXUSR)
    assert direct.tree_inventory(tree) != original
    source.chmod(original_mode)
    (tree / "external").unlink()
    (tree / "external").symlink_to("payload")
    assert direct.tree_inventory(tree) != original
    refuses(lambda: direct.inside(tree.resolve(), "../outside"))
    tool = tree / "compiler"
    # A deliberately malformed/non-host-CPU Mach-O input is not execution proof.
    tool.write_bytes(struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0))
    tool.chmod(0o755)
    refuses(lambda: direct.tool_identity(tree, "compiler"))
    tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    refuses(lambda: direct.tool_identity(tree, "compiler"))
    output = base / "new-evidence"
    output.mkdir()
    refuses(lambda: direct.fresh_output(output, [tree]))
    refuses(lambda: direct.fresh_output(tree / "new-output", [tree]))
    refuses(lambda: direct.fresh_output(base / "absent" / ".." / "Xcode.app" / "new", [tree]))
    lock = {"schema": "iosapp-direct-input-lock", "schema_version": 1,
            "profile": dict(direct.PROFILE), "tree": {"entries": original}}
    for field, value in (("schema_version", True), ("schema_version", 2), ("profile", {"target": "x86_64-apple-ios17.0-simulator"})):
        changed = copy.deepcopy(lock)
        changed[field] = value
        refuses(lambda: direct.validate_lock(changed, lock))
    changed = copy.deepcopy(lock)
    changed["tree"]["entries"][0]["mode"] ^= 0o100
    refuses(lambda: direct.validate_lock(changed, lock))

# Model load commands exercise ABI/layout validation, not compiled runtime proof.
def model_runtime(*, cpu=0x0100000C, platform=2, minimum=13 << 16):
    command = struct.pack("<6I", 0x32, 24, platform, minimum, (26 << 16) | (2 << 8), 0)
    return struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 0, 6, 1, len(command), 0, 0) + command


with tempfile.TemporaryDirectory(prefix="runtime-", dir=scratch) as temporary:
    app = Path(temporary) / "iosapp.app"
    frameworks = app / "Frameworks"
    frameworks.mkdir(parents=True)
    library = frameworks / "libswiftModel.dylib"
    library.write_bytes(model_runtime())
    abi = direct.runtime_metadata(app)[0]["slices"][0]
    assert abi["cpu"] == 0x0100000C and abi["deployment"]["platform"] == 2
    assert abi["deployment"]["minimum"] == "13.0.0"
    for change in ({"cpu": 0x01000007}, {"platform": 7}, {"minimum": 18 << 16}):
        library.write_bytes(model_runtime(**change))
        refuses(lambda: direct.runtime_metadata(app))
    library.write_bytes(model_runtime())
    nested = frameworks / "iphoneos"
    nested.mkdir()
    library.rename(nested / library.name)
    refuses(lambda: direct.runtime_metadata(app))

print("iosapp direct build input contracts: PASS")

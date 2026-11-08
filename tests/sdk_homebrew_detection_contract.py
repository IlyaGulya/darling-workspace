#!/usr/bin/env python3
"""Check the SDK metadata consumers actually read, independently of SDK vintage.

The unversioned SDK is a supported discovery entrypoint. Historical alias names,
symlink text and display wording are not the SDK version API.
"""
import json
import os
import plistlib
import re
import sys
import tempfile
from pathlib import Path


def discover_sdk(sdk):
    data = json.loads((sdk / "SDKSettings.json").read_text())
    with (sdk / "SDKSettings.plist").open("rb") as stream:
        plist = plistlib.load(stream)
    version = data.get("Version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)+", version):
        raise ValueError("SDK Version must be a dotted numeric version")
    if data.get("CanonicalName") != f"macosx{version}":
        raise ValueError("SDK canonical identity does not match its version")
    if (data.get("DefaultProperties") or {}).get("PLATFORM_NAME") != "macosx":
        raise ValueError("SDK is not a macOS SDK")
    for key in ("Version", "CanonicalName"):
        if plist.get(key) != data[key]:
            raise ValueError(f"JSON/plist SDK identity differs: {key}")
    if (plist.get("DefaultProperties") or {}).get("PLATFORM_NAME") != "macosx":
        raise ValueError("plist platform differs from JSON platform")
    return version


def fixture_regressions():
    # Exercise accepted identities across versions and equivalent path layouts;
    # reject consumer-visible inconsistency rather than pinning source spelling.
    with tempfile.TemporaryDirectory(prefix="sdk-discovery-contract-") as raw:
        root = Path(raw)
        sdk = root / "SDK with spaces"
        sdk.mkdir()
        alias = root / "MacOSX.sdk"
        alias.symlink_to(sdk.name, target_is_directory=True)
        for version in ("11.3", "14.0", "15.6.1"):
            data = {"Version": version, "CanonicalName": f"macosx{version}",
                    "DefaultProperties": {"PLATFORM_NAME": "macosx"}}
            (sdk / "SDKSettings.json").write_text(json.dumps(data))
            (sdk / "SDKSettings.plist").write_bytes(plistlib.dumps(data))
            assert discover_sdk(alias) == version
            broken = dict(data, CanonicalName="macosx0.0")
            (sdk / "SDKSettings.json").write_text(json.dumps(broken))
            try:
                discover_sdk(alias)
            except ValueError:
                pass
            else:
                raise AssertionError("inconsistent SDK canonical identity was accepted")
            (sdk / "SDKSettings.json").write_text(json.dumps(data))
            (sdk / "SDKSettings.plist").write_bytes(plistlib.dumps(broken))
            try:
                discover_sdk(alias)
            except ValueError:
                pass
            else:
                raise AssertionError("inconsistent plist identity was accepted")


def main():
    fixture_regressions()
    root = Path(os.environ["DARLING_SRC_ROOT"])
    sdk = root / "Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
    try:
        version = discover_sdk(sdk)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        print(f"FAIL: SDK discovery: {exc}", file=sys.stderr)
        return 1
    print(f"SDK_DISCOVERY_OK sdk={sdk} version={version} platform=macosx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

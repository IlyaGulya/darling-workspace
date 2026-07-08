#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


root = Path(os.environ["DARLING_SRC_ROOT"])
sdk_dir = root / "Developer/Platforms/MacOSX.platform/Developer/SDKs"
sdk = sdk_dir / "MacOSX.sdk"
settings = sdk / "SDKSettings.json"
versioned = sdk_dir / "MacOSX11.sdk"
old_versioned = sdk_dir / "MacOSX10.13.sdk"

if not sdk.is_dir():
    fail(f"missing SDK directory: {sdk}")
if not settings.is_file():
    fail(f"missing Homebrew-readable SDKSettings.json: {settings}")

try:
    data = json.loads(settings.read_text())
except json.JSONDecodeError as exc:
    fail(f"SDKSettings.json is not valid JSON: {exc}")

version = data.get("Version")
canonical = data.get("CanonicalName")
platform = (data.get("DefaultProperties") or {}).get("PLATFORM_NAME")

if version != "11.3":
    fail(f"unexpected SDK Version {version!r}, expected '11.3'")
if canonical != "macosx11.3":
    fail(f"unexpected CanonicalName {canonical!r}, expected 'macosx11.3'")
if platform != "macosx":
    fail(f"unexpected PLATFORM_NAME {platform!r}, expected 'macosx'")

if old_versioned.exists() or old_versioned.is_symlink():
    fail(f"old mismatched Homebrew SDK symlink still exists: {old_versioned}")
if not versioned.is_symlink():
    fail(f"missing version-matched Homebrew SDK symlink: {versioned}")
if os.readlink(versioned) != "MacOSX.sdk/":
    fail(f"{versioned} points to {os.readlink(versioned)!r}, expected 'MacOSX.sdk/'")

print("GREEN: Homebrew-visible SDKSettings.json and versioned SDK symlink are consistent")

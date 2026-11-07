"""Host packaging contracts with model Mach-Os, not compiler/runtime acceptance."""
from __future__ import annotations

import datetime
import importlib.util
import os
from pathlib import Path
import plistlib
import stat
import struct
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from west_commands.test_store import durable_subdir

spec = importlib.util.spec_from_file_location("iosapp_archive_package", ROOT / "scripts/iosapp-archive-package.py")
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)
SCRATCH = durable_subdir(ROOT.parent, "test-scratch") / "iosapp-package"


def model_macho(*, cpu=0x100000C, platform=2, entry=512, minimum=17 << 16,
                sdk=26 << 16, signature=False, weak=False, filetype=2,
                frameworks=("UIKit", "Foundation")):
    """Construct only the Mach-O structures the validator consumes; no compiled code."""
    commands = [struct.pack("<6I", 0x32, 24, platform, minimum, sdk, 0),
                struct.pack("<II16s4Q4I", 0x19, 72, b"__TEXT", 0, 1024, 0, 1024, 7, 5, 0, 0),
                struct.pack("<II2Q", 0x80000028, 24, entry, 0)]
    for framework in frameworks:
        name = f"/System/Library/Frameworks/{framework}.framework/{framework}".encode() + b"\0"
        size = (24 + len(name) + 7) & ~7
        commands.append(struct.pack("<6I", 0x80000018 if weak else 0xC, size, 24, 0, 0, 0)
                        + name + bytes(size - 24 - len(name)))
    if signature:
        commands.append(struct.pack("<4I", 0x1D, 16, 1024, 0))
    body = b"".join(commands)
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 0, filetype, len(commands), len(body), 0, 0)
    return (header + body).ljust(1024, b"\0")


class IOSAppPackageContract(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "iosapp.app"
        self.app.mkdir()
        self.metadata = {"CFBundlePackageType": "APPL", "LSRequiresIPhoneOS": True,
                         "CFBundleSupportedPlatforms": ["iPhoneOS"], "DTPlatformName": "iphoneos",
                         "CFBundleExecutable": "iosapp", "CFBundleIdentifier": "com.example.iosapp",
                         "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
                         "MinimumOSVersion": "17.0"}
        self.write_info()
        self.executable = self.app / "iosapp"
        self.executable.write_bytes(model_macho())
        self.executable.chmod(0o755)

    def write_info(self):
        (self.app / "Info.plist").write_bytes(plistlib.dumps(self.metadata))

    def test_standalone_app_checks_device_macho_without_archive_metadata(self):
        result = pack.validate_app(self.app)
        self.assertEqual(result["macho"]["architecture"], "arm64")
        self.assertEqual(result["macho"]["deployment"]["platform"], "iOS")
        self.assertEqual(result["macho"]["deployment"]["minimum_os"], "17.0.0")
        self.assertFalse((self.root / "Info.plist").exists())

    def test_swiftui_and_uikit_require_their_own_framework_contracts(self):
        self.executable.write_bytes(model_macho(frameworks=("SwiftUI",)))
        with self.assertRaises(ValueError):
            pack.validate_app(self.app)
        result = pack.validate_app(self.app, kind="swiftui")
        self.assertEqual(result["macho"]["architecture"], "arm64")
        self.assertEqual(result["macho"]["dylibs"],
                         [{"command": 0xC, "name": "/System/Library/Frameworks/SwiftUI.framework/SwiftUI"}])
        for payload in (model_macho(), model_macho(frameworks=("SwiftUI",), weak=True)):
            self.executable.write_bytes(payload)
            with self.assertRaises(ValueError):
                pack.validate_app(self.app, kind="swiftui")
        with self.assertRaises(ValueError):
            pack.validate_app(self.app, kind="unrestricted")

    def test_cpu_platform_entrypoint_linkage_and_signature_boundaries(self):
        for change in ({"cpu": 0x1000007}, {"platform": 7}, {"platform": 6},
                       {"entry": 1024}, {"minimum": 16 << 16}, {"sdk": 16 << 16},
                       {"signature": True}, {"weak": True}, {"filetype": 6}):
            with self.subTest(change=change):
                self.executable.write_bytes(model_macho(**change))
                with self.assertRaises(ValueError):
                    pack.validate_app(self.app)

    def test_malformed_load_commands_are_rejected(self):
        original = model_macho()
        for offset, value in ((20, 2048), (16, 99), (36, 7), (52, 1)):
            with self.subTest(offset=offset):
                malformed = bytearray(original)
                struct.pack_into("<I", malformed, offset, value)
                self.executable.write_bytes(malformed)
                with self.assertRaises(ValueError):
                    pack.validate_app(self.app)

    def test_expanded_metadata_platform_and_deployment_are_required(self):
        original = dict(self.metadata)
        for field, value in (("CFBundleIdentifier", "$(PRODUCT_BUNDLE_IDENTIFIER)"),
                             ("CFBundleShortVersionString", ""), ("CFBundleVersion", 1),
                             ("CFBundleExecutable", "other"), ("CFBundlePackageType", "FMWK"),
                             ("CFBundleSupportedPlatforms", ["iPhoneSimulator"]),
                             ("DTPlatformName", "iphonesimulator"), ("LSRequiresIPhoneOS", False),
                             ("MinimumOSVersion", "18.0")):
            with self.subTest(field=field):
                self.metadata = {**original, field: value}
                self.write_info()
                with self.assertRaises(ValueError):
                    pack.validate_app(self.app)

    def test_executable_must_be_physical_and_executable(self):
        self.executable.chmod(0o644)
        with self.assertRaises(ValueError):
            pack.validate_app(self.app)
        self.executable.rename(self.app / "real")
        (self.app / "real").chmod(0o755)
        self.executable.symlink_to("real")
        with self.assertRaises(ValueError):
            pack.validate_app(self.app)

    def test_signing_and_provisioning_material_is_rejected_at_any_depth(self):
        nested = self.app / "Frameworks"
        nested.mkdir()
        for name in ("_CodeSignature", "embedded.mobileprovision"):
            path = nested / name
            path.write_bytes(b"not authorized signing material")
            with self.assertRaises(ValueError):
                pack.validate_app(self.app)
            path.unlink()

    def test_zip_preserves_bytes_symlinks_modes_and_is_deterministic(self):
        resources = self.app / "Resources"
        resources.mkdir(mode=0o750)
        data = resources / "binary"
        data.write_bytes(bytes(range(256)))
        data.chmod(0o640)
        (resources / "link").symlink_to("binary")
        rows = pack.validate_app(self.app)["app_entries"]
        first, second = self.root / "first.ipa", self.root / "second.ipa"
        pack.package(self.app, rows, first)
        os.utime(data, (1234567890, 1234567890))
        pack.package(self.app, rows, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        paths = {
            "Payload/": None,
            "Payload/iosapp.app/": self.app,
            "Payload/iosapp.app/Info.plist": self.app / "Info.plist",
            "Payload/iosapp.app/Resources/": resources,
            "Payload/iosapp.app/Resources/binary": data,
            "Payload/iosapp.app/Resources/link": resources / "link",
            "Payload/iosapp.app/iosapp": self.executable,
        }
        with zipfile.ZipFile(first) as archive:
            self.assertEqual(sorted(archive.namelist()), sorted(paths))
            for name, source in paths.items():
                info = archive.getinfo(name)
                mode = info.external_attr >> 16
                if source is None:
                    self.assertTrue(stat.S_ISDIR(mode) and mode & stat.S_IXUSR)
                else:
                    self.assertEqual(mode, source.lstat().st_mode)
                if source is not None and source.is_symlink():
                    self.assertEqual(archive.read(info), os.fsencode(os.readlink(source)))
                elif source is not None and source.is_file():
                    self.assertEqual(archive.read(info), source.read_bytes())
                else:
                    self.assertEqual(archive.read(info), b"")
        with self.assertRaises(FileExistsError):
            pack.package(self.app, rows, first)

    def test_inventory_refuses_escaping_or_dangling_resources(self):
        (self.root / "outside").write_bytes(b"outside the app")
        link = self.app / "bad"
        for target in ("missing", "../outside", str(self.root)):
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                pack.inventory(self.app)
            link.unlink()

    def test_packaging_refuses_resource_changed_since_inventory(self):
        rows = pack.inventory(self.app)
        self.executable.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            pack.package(self.app, rows, self.root / "changed.ipa")

    def test_archive_metadata_and_property_crosschecks_remain_strict(self):
        # This directory is a validator model, never evidence of xcodebuild success.
        archive = self.root / "model.xcarchive"
        applications = archive / "Products/Applications"
        applications.mkdir(parents=True)
        self.app.rename(applications / "iosapp.app")
        props = {field: self.metadata[field] for field in
                 ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion")}
        props.update(ApplicationPath="Applications/iosapp.app", Architectures=["arm64"])
        info = {"ArchiveVersion": 2, "CreationDate": datetime.datetime(2026, 1, 1),
                "Name": "iosapp", "SchemeName": "iosapp", "ApplicationProperties": props}
        def store(value):
            (archive / "Info.plist").write_bytes(plistlib.dumps(value))
        store(info)
        self.assertEqual(pack.validate_archive(archive)["archive_version"], 2)
        for field, value in (("CFBundleIdentifier", "other"), ("CFBundleVersion", "2"),
                             ("CFBundleShortVersionString", "2.0"), ("Architectures", ["x86_64"]),
                             ("SigningIdentity", "Developer"), ("ApplicationPath", "elsewhere")):
            with self.subTest(property=field):
                store({**info, "ApplicationProperties": {**props, field: value}})
                with self.assertRaises(ValueError):
                    pack.validate_archive(archive)
        for field, value in (("ArchiveVersion", 1), ("CreationDate", "today"),
                             ("Name", "other"), ("SchemeName", "other"),
                             ("ApplicationProperties", [])):
            with self.subTest(metadata=field):
                store({**info, field: value})
                with self.assertRaises(ValueError):
                    pack.validate_archive(archive)
        store(info)
        (applications / "other.app").mkdir()
        with self.assertRaises(ValueError):
            pack.validate_archive(archive)


if __name__ == "__main__":
    unittest.main()

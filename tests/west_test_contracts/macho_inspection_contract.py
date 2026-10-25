#!/usr/bin/env python3
"""Host CLI pointer-resolution contracts and static classifier decision contracts.

Fixtures contain real Mach-O headers, segments and on-disk chains; no LLVM or guest
is required. Classifier tests isolate collection from decisions, not runtime behavior.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
STATE = ROOT / ".west-state/supervisor-tooling-20261025/macho-inspection"
spec = importlib.util.spec_from_file_location("macho_inspection_analyzer", SCRIPTS / "closure_analyze.py")
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)


def image(*, cpu=0x01000007, base=0x100000000, fmt=6, multi=False,
          chained=True, selector=b"measuredSelector:"):
    """Nonlinear VM/file geometry, with a next value requiring all twelve bits."""
    data = bytearray(0x6000)
    commands = []
    for name, vm, vmsize, offset, size in (
        (b"__TEXT", base, 0x1000, 0, 0x1000),
        (b"__DATA", base + 0x8000, 0x4000, 0x1000, 0x4000),
        (b"__LINKEDIT", base + 0x10000, 0x1000, 0x5000, 0x1000),
    ):
        commands.append(struct.pack("<II16s4Q4I", 0x19, 72, name, vm, vmsize,
                                    offset, size, 7, 3, 0, 0))
    uuid = bytes(range(16))
    commands.append(struct.pack("<II16s", 0x1B, 24, uuid))
    if chained:
        table = [0x8001, 0x100, 0x8300] if multi else [0x100]
        segment_starts = struct.pack("<IHHQIH", 22 + 2 * len(table), 0x4000,
                                     fmt, 0x8000, 0, 1) + struct.pack(f"<{len(table)}H", *table)
        starts = struct.pack("<4I", 3, 0, 16, 0) + segment_starts
        fixups = struct.pack("<7I", 0, 28, 28 + len(starts), 28 + len(starts), 0, 1, 0) + starts
        data[0x5000:0x5000 + len(fixups)] = fixups
        commands.append(struct.pack("<4I", 0x80000034, 16, 0x5000, len(fixups)))
    command_bytes = b"".join(commands)
    struct.pack_into("<IiiIIIII", data, 0, ca.MH_MAGIC_64, cpu,
                     3 if cpu == 0x01000007 else 0, 6, len(commands), len(command_bytes), 0x80, 0)
    data[32:32 + len(command_bytes)] = command_bytes
    target = base + 0x800
    encoded_target = target if fmt == 2 or not chained else target - base
    if chained:
        encoded_target = (encoded_target & ((1 << 36) - 1)) | ((encoded_target >> 56) << 36)
    struct.pack_into("<Q", data, 0x1100, encoded_target | ((0x802 << 51) if chained else 0))
    struct.pack_into("<Q", data, 0x3108, encoded_target)
    if multi:
        struct.pack_into("<Q", data, 0x1300, encoded_target)
    data[0x800:0x801 + len(selector)] = selector + b"\0"
    return data


def universal(x86, *, fat64=False):
    arm = image(cpu=0x0100000C, selector=b"wrongArchitecture:")
    blob = bytearray(0x8000 + len(x86))
    struct.pack_into(">II", blob, 0, ca.FAT_MAGIC_64 if fat64 else ca.FAT_MAGIC, 2)
    fmt = ">iiQQII" if fat64 else ">iiIII"
    stride = struct.calcsize(fmt)
    for index, (cpu, sub, offset, content) in enumerate((
        (0x0100000C, 0, 0x1000, arm), (0x01000007, 3, 0x8000, x86)
    )):
        values = (cpu, sub, offset, len(content), 12)
        struct.pack_into(fmt, blob, 8 + index * stride, *(values + (0,) if fat64 else values))
        blob[offset:offset + len(content)] = content
    return blob


class PointerCLI(unittest.TestCase):
    def setUp(self):
        STATE.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="pointer-", dir=STATE)
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def query(self, content, address=0x10000A108, *, wrapper=True, extra=(), success=True):
        path = self.root / "fixture.macho"
        path.write_bytes(content)
        if wrapper:
            argv = ["sh", str(SCRIPTS / "darling-describe-artifact.sh"),
                    "--pointer", hex(address), "--json", *extra, str(path)]
        else:
            argv = [sys.executable, "-B", str(SCRIPTS / "closure_analyze.py"),
                    "--resolve-pointer", str(path), "--vm-address", hex(address), "--json", *extra]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip(), "failure must explain the unresolved input")
        self.assertEqual(result.stdout, "", "an error must not publish a plausible success result")
        return result

    def assert_resolution(self, result, bias=0, base=0x100000000):
        self.assertEqual(result["arch"], "x86_64")
        self.assertEqual(result["uuid"], "00010203-0405-0607-0809-0a0b0c0d0e0f")
        self.assertEqual(result["slice_offset"], bias)
        self.assertEqual(result["image_base"], base)
        self.assertEqual(result["slot"]["vm_address"], base + 0xA108)
        self.assertEqual(result["slot"]["slice_file_offset"], 0x3108)
        self.assertEqual(result["slot"]["file_offset"], bias + 0x3108)
        self.assertEqual(result["target"]["vm_address"], base + 0x800)
        self.assertEqual(result["target"]["file_offset"], bias + 0x800)
        self.assertEqual(result["cstring"]["status"], "utf8")
        self.assertEqual(result["cstring"]["value"], "measuredSelector:")
        self.assertEqual(result["evidence_tier"], "static")

    def test_thin_next12_stride4_and_selector(self):
        # The selected slot is reached only by current + 0x802*4, not +4+next*4
        # and not an 11-bit next mask. The VM/file geometry is intentionally nonlinear.
        self.assert_resolution(self.query(image()))
        self.assert_resolution(self.query(image(fmt=2), wrapper=False))

    def test_fat32_and_fat64_bias_and_arch_selection(self):
        for fat64 in (False, True):
            with self.subTest(fat64=fat64):
                data = universal(image(), fat64=fat64)
                self.assert_resolution(self.query(data), bias=0x8000)
                arm = self.query(data, extra=("--arch", "arm64"))
                self.assertEqual(arm["arch"], "arm64")
                self.assertEqual(arm["slice_offset"], 0x1000)
                self.assertEqual(arm["cstring"]["value"], "wrongArchitecture:")

    def test_multi_start_page(self):
        self.assert_resolution(self.query(image(multi=True)))
        second = self.query(image(multi=True), address=0x100008300)
        self.assertEqual(second["cstring"]["value"], "measuredSelector:")

    def test_high8_target_and_plain_pointer(self):
        base = 0xAB00000100000000
        self.assert_resolution(self.query(image(base=base, fmt=2), address=base + 0xA108), base=base)
        result = self.query(image(chained=False))
        self.assert_resolution(result)
        self.assertEqual(result["pointer_format"], "plain-64")

    def test_bounded_cstring(self):
        result = self.query(image(), extra=("--max-cstring", "4"))
        self.assertEqual(result["cstring"]["status"], "unterminated")
        self.assertEqual(result["cstring"]["bytes_read"], 4)
        self.assertNotIn("value", result["cstring"])

    def test_unsupported_and_invalid_inputs(self):
        cases = [(image(fmt=1), 0x10000A108), (image(), 0xDEADBEEF),
                 (image(), 0x100008108), (b"not a Mach-O", 0),
                 (image(cpu=0x0100000C), 0x10000A108)]
        for content, address in cases:
            with self.subTest(address=address, size=len(content)):
                self.query(content, address, success=False)
        for raw in (1 << 63, 1 << 44, 0xFFFFF):
            broken = image()
            struct.pack_into("<Q", broken, 0x3108, raw)
            self.query(broken, success=False)
        broken = image()
        struct.pack_into("<Q", broken, 0x1100, (0xFFF << 51) | 0x800)
        self.query(broken, success=False)
        broken = image(multi=True)
        # Multi-start index is relative to page_start[], not the overflow pool.
        struct.pack_into("<H", broken, 0x5000 + 28 + 16 + 22, 0x8007)
        self.query(broken, success=False)
        broken = image()
        struct.pack_into("<H", broken, 0x5000 + 28 + 16 + 22, 0xFFFF)
        self.query(broken, success=False)
        broken = image()
        struct.pack_into("<I", broken, 0x5000, 1)
        self.query(broken, success=False)
        self.query(image(), extra=("--max-cstring", "0"), success=False)
        self.query(universal(image())[:100], success=False)


class StaticClassification(unittest.TestCase):
    def analyze(self, body, *, demanded=True, marker=False, data=False, empty_class=False):
        STATE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="classifier-", dir=STATE) as directory:
            source = Path(directory) / "provider.c"
            symbol = "_OBJC_CLASS_$_Marker" if empty_class else "_ProviderAvailable"
            source.write_text("Marker" if empty_class else "int ProviderAvailable(void) { return 0; }\n")
            demand = {symbol: "bind"} if demanded else {}
            refs = [{"image": "/client", "weak": False, "upward": False}]
            # These are collection boundaries. The real analyze_component makes the
            # classification decision and preserves the independent demand/coverage result.
            with patch.multiple(ca,
                resolve_artifact=lambda *a: "/artifact",
                demand_tables=lambda *a: (demand, {"/client": demand}),
                exports=lambda *a: {symbol},
                stub_evidence=lambda *a: {"stub_names": ["ProviderAvailable"] if marker else []},
                objc_classes=lambda *a: {"Marker": None} if empty_class else {},
                symbol_types=lambda *a: {symbol: "D" if data else "T"},
                disassemble_symbol=lambda *a: body,
                source_dir_for=lambda *a: directory,
                walk_source_changes=lambda *a: ("base", [str(source)])):
                return ca.analyze_component("provider", {"install_name": "/provider",
                    "cmake_target": "provider"}, None, directory, "base", refs)

    def test_constant_absence_is_not_certified(self):
        result = self.analyze([("xorl", "%eax, %eax"), ("retq", "")])
        self.assertEqual(result["klass"], "STATIC_NONCALLING")
        self.assertEqual(result["runtime_status"], "unverified")
        self.assertEqual(result["evidence_tier"], "static")
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["noncalling"]), 1)

    def test_unqueried_provider_is_not_certified(self):
        result = self.analyze([("xorl", "%eax, %eax"), ("retq", "")], demanded=False)
        self.assertEqual(result["klass"], "STATIC_UNATTRIBUTED")
        self.assertEqual(result["runtime_status"], "unverified")
        self.assertEqual(result["noncalling"], [])
        self.assertEqual(result["demanded"], {})

    def test_calls_constants_data_and_marker_classes_remain_unverified(self):
        cases = [([("callq", "_something"), ("retq", "")], {}, "STATIC_CALLING"),
                 ([("movl", "$0x1, %eax"), ("retq", "")], {}, "STATIC_NONCALLING"),
                 ([], {"data": True}, "STATIC_NONCALLING"),
                 ([], {"empty_class": True}, "STATIC_NONCALLING"),
                 ([], {}, "STATIC_UNATTRIBUTED"),
                 ([("retq", "")], {"marker": True}, "STUB")]
        for body, kwargs, klass in cases:
            with self.subTest(klass=klass, kwargs=kwargs):
                result = self.analyze(body, **kwargs)
                self.assertEqual(result["klass"], klass)
                self.assertEqual(result["runtime_status"], "unverified")
                self.assertEqual(result["missing"], [])


if __name__ == "__main__":
    unittest.main()

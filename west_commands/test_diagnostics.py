"""Live diagnostic acceptance through an initialized West test command."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import time


def _valid_core(path: Path) -> bool:
    """Require a complete ELF64 core, not just a partially written magic header."""
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            return False
        if struct.unpack_from("<H", header, 16)[0] != 4:
            return False
        offset = struct.unpack_from("<Q", header, 32)[0]
        entry_size, count = struct.unpack_from("<HH", header, 54)
        size = path.stat().st_size
        if entry_size != 56 or not count or offset + entry_size * count > size:
            return False
        stream.seek(offset)
        for _ in range(count):
            entry = stream.read(entry_size)
            file_offset = struct.unpack_from("<Q", entry, 8)[0]
            file_size = struct.unpack_from("<Q", entry, 32)[0]
            if file_offset + file_size > size:
                return False
    return True


def verify_exact_capture(bundle: Path) -> dict:
    if not (bundle / "timeout.txt").is_file():
        raise ValueError("diagnostic payload did not reach its expected capture deadline")
    if "MACHO_EXACT_GUEST_READY" not in (bundle / "stdout.log").read_text():
        raise ValueError("guest never reached the diagnostic payload")
    exact = bundle / "exact"
    metadata = json.loads((exact / "manifest.json").read_text())
    matched = []
    for process in metadata["processes"]:
        for image in process.get("images", []):
            if not image.get("complete") or not image.get("path", "").endswith("/bin/sleep"):
                continue
            path = exact / image["file"]
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                magic = stream.read(4)
                if magic not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
                    raise ValueError("captured guest sleep image is not Mach-O")
                digest.update(magic)
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != image["sha256"]:
                raise ValueError("captured guest sleep image checksum mismatch")
            core = process.get("core", {})
            if not core.get("file") or not _valid_core(exact / core["file"]):
                raise ValueError("guest sleep process has no complete ELF core payload")
            matched.append({"pid": process["pid"], "image": image["file"], "core": core["file"]})
    if not matched:
        raise ValueError("capture omitted the live guest sleep process")
    if "rip" not in (exact / "gdb.txt").read_text():
        raise ValueError("capture omitted thread registers")
    return {"bundle": str(bundle), "exact_complete": metadata["complete"], "guest_processes": matched}


def run_exact_capture(host, runtime_profile: str) -> int:
    deployment = host._retained_runtime_profile(runtime_profile)
    root = Path(host.manifest.repo_abspath)
    invocation = {
        "name": "exact-macho-live", "cwd": root, "diag": "forensic",
        "timeout_seconds": 15, "shell": False,
        "args": ["/bin/bash", str(root / "tests/run-exact-capture-guest.sh")],
        "requires_resources": ["darling-prefix"],
    }
    started = time.time()
    with host._prefix_resource_context(True):
        result = host._run_invocation(invocation, env=deployment.env)
    if host._prefix_cleanup_failed:
        host.die("exact-capture diagnostic prefix cleanup failed")
    if result == 0:
        host.die("exact-capture payload unexpectedly completed without its deadline")
    bundle = host._latest_debug_bundle(invocation, since=started)
    if bundle is None:
        host.die("exact-capture diagnostic produced no bundle")
    try:
        report = verify_exact_capture(bundle)
    except (OSError, ValueError, KeyError, TypeError) as error:
        host.die(f"exact-capture acceptance failed in {bundle}: {error}")
    host.inf(json.dumps(report, indent=2))
    host.inf("PASS exact-capture: guest Mach-O, matching core, registers and prefix cleanup")
    return 0

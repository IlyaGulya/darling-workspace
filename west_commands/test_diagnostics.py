"""Live diagnostic acceptance through an initialized West test command."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
import struct
import time

try:
    from .evidence_storage import EvidenceError, PayloadNotFoundError, open_payload
    from .test_prefix import validate_borrowed_prefix_lease
except ImportError:
    from evidence_storage import EvidenceError, PayloadNotFoundError, open_payload
    from test_prefix import validate_borrowed_prefix_lease


def _valid_core(path: Path) -> bool:
    """Require a complete ELF64 core, not just a partially written magic header."""
    with open_payload(path) as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            return False
        if struct.unpack_from("<H", header, 16)[0] != 4:
            return False
        offset = struct.unpack_from("<Q", header, 32)[0]
        entry_size, count = struct.unpack_from("<HH", header, 54)
        if entry_size != 56 or not count:
            return False
        size = os.fstat(stream.fileno()).st_size if stream.seekable() else None
        required_size = offset + entry_size * count
        if size is not None and required_size > size:
            return False
        consumed = len(header)
        pending = b""
        if size is not None:
            stream.seek(offset)
        elif offset < consumed:
            pending = header[offset:]
        else:
            while consumed < offset:
                chunk = stream.read(min(1024 * 1024, offset - consumed))
                if not chunk:
                    return False
                consumed += len(chunk)
        for _ in range(count):
            entry = pending[:entry_size]
            pending = pending[entry_size:]
            if len(entry) < entry_size:
                rest = stream.read(entry_size - len(entry))
                consumed += len(rest)
                entry += rest
            if len(entry) != entry_size:
                return False
            file_offset = struct.unpack_from("<Q", entry, 8)[0]
            file_size = struct.unpack_from("<Q", entry, 32)[0]
            required_size = max(required_size, file_offset + file_size)
        if size is None:
            # Decoding already has to consume and verify the complete payload.
            # Count that single stream rather than extracting or decoding twice.
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                consumed += len(chunk)
            size = consumed
        if required_size > size:
            return False
    return True


def verify_exact_capture(bundle: Path) -> dict:
    try:
        with open_payload(bundle / "timeout.txt") as payload:
            payload.read(1)
    except PayloadNotFoundError as error:
        raise ValueError("diagnostic payload did not reach its expected capture deadline") from error
    with open_payload(bundle / "stdout.log") as payload:
        stdout = payload.read().decode("utf-8")
    if "MACHO_EXACT_GUEST_READY" not in stdout:
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
            with open_payload(path) as stream:
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
    with open_payload(exact / "gdb.txt") as payload:
        registers = payload.read().decode("utf-8")
    if "rip" not in registers:
        raise ValueError("capture omitted thread registers")
    return {"bundle": str(bundle), "exact_complete": metadata["complete"], "guest_processes": matched}


def run_exact_capture(host) -> int:
    runtime = host._installed_runtime()
    root = Path(host.manifest.repo_abspath)
    invocation = {
        "name": "exact-macho-live", "cwd": root, "diag": "forensic",
        "timeout_seconds": 15, "shell": False,
        "args": ["/bin/bash", str(root / "tests/run-exact-capture-guest.sh")],
        "requires_resources": ["darling-prefix"],
    }
    started = time.time()
    try:
        borrowed = validate_borrowed_prefix_lease(host._prefix)
    except (OSError, ValueError) as error:
        host.die(f"invalid diagnostic prefix lease: {error}")
    lease = nullcontext() if borrowed is not None else host._prefix_resource_context(True)
    with lease:
        result = host._run_invocation(invocation, env=runtime.environment(host._prefix))
        if borrowed is not None and not host._shutdown_test_prefix():
            host._prefix_cleanup_failed = True
    if host._prefix_cleanup_failed:
        host.die("exact-capture diagnostic prefix cleanup failed")
    if result == 0:
        host.die("exact-capture payload unexpectedly completed without its deadline")
    bundle = host._latest_debug_bundle(invocation, since=started)
    if bundle is None:
        host.die("exact-capture diagnostic produced no bundle")
    try:
        report = verify_exact_capture(bundle)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as error:
        host.die(f"exact-capture acceptance failed in {bundle}: {error}")
    host.inf(json.dumps(report, indent=2))
    host.inf("PASS exact-capture: guest Mach-O, matching core, registers and prefix cleanup")
    return 0

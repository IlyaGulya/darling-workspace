#!/usr/bin/env python3
"""Turn a Darling guest core into the facts a diagnosis needs, without hand-written gdb.

SUPERSEDED (2026-10-22): use `scripts/dwdiag core --core FILE --prefix PREFIX`. It owns this exact job
-- the core's own NT_FILE table, the FAULTING thread (not thread 1), the faulting image and function,
the frame-pointer chain with symbols, the registers and the signal's own semantics -- and it is
maintained in the tool repository. This script is kept only because the milestone evidence cites the
runs that used it; do not add callers.

WHY THIS EXISTS. Diagnosing a guest fault by hand kept costing whole sessions, and each manual pass
made the same four mistakes:

  * it read THREAD 1, while the fatal signal belongs to whichever thread gdb selects -- the faulting
    thread is often not thread 1 and its registers are the only real evidence;
  * it symbolized a FAT Mach-O with a bare `llvm-nm`, which walks the FIRST slice: every offset was
    reported against the wrong code, so `symbol + 0x2aa41` looked like a real answer;
  * it computed an image offset from the mapping's START, which is only correct for the segment whose
    file offset is 0 -- for every later segment the answer was silently off by that segment's offset;
  * it treated a zeroed `%rip`/`%rax` as evidence instead of suspecting the signal plumbing.

This script performs all four steps once, prints the faulting thread, the image+offset and
symbol+offset of the PC, the registers, the frame-pointer chain symbolized the same way, and the raw
stack words that look like return addresses. Everything is derived from the core; nothing is assumed
about which thread or which slice.

Usage:
    scripts/guest-core-report.py CORE [--frames N] [--stack-words N] [--json]

Exit status is 0 when a report was produced (whatever it says), 2 when the core cannot be read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

GDB = shutil.which("gdb")
NM = shutil.which("llvm-nm") or shutil.which("nm")
OBJDUMP = shutil.which("llvm-objdump") or shutil.which("objdump")

REGISTERS = (
    "rip", "rsp", "rbp", "rax", "rbx", "rcx", "rdx", "rsi", "rdi",
    "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
)

MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce": "32-be",
    b"\xce\xfa\xed\xfe": "32-le",
    b"\xfe\xed\xfa\xcf": "64-be",
    b"\xcf\xfa\xed\xfe": "64-le",
    b"\xca\xfe\xba\xbe": "fat-be",
    b"\xbe\xba\xfe\xca": "fat-le",
}


def run(argv: list[str], timeout: int = 240) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - environment failure
        return f"__error__: {exc}"
    return proc.stdout + proc.stderr


def gdb_batch(core: Path, commands: list[str]) -> str:
    argv = [GDB, "-q", "-batch", "-nx", "-c", str(core)]
    for command in commands:
        argv += ["-ex", command]
    return run(argv)


def parse_signal(text: str) -> str:
    match = re.search(r"Program terminated with signal ([A-Z0-9]+), (.*)", text)
    if match:
        return f"{match.group(1)} ({match.group(2).strip()})"
    return "unknown"


def parse_registers(text: str) -> tuple[dict[str, int], str | None]:
    registers: dict[str, int] = {}
    thread = None
    match = re.search(r"\[Switching to thread [^\]]*\]", text)
    if match:
        thread = match.group(0).strip("[]")
    elif re.search(r"#0\s", text):
        thread = "default (gdb-selected) thread"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in REGISTERS:
            try:
                registers[parts[0]] = int(parts[1], 16)
            except ValueError:
                continue
    return registers, thread


def parse_mappings(text: str) -> list[tuple[int, int, int, str]]:
    """(start, end, file_offset, path) for every mapped region gdb printed.

    gdb's columns are `start end SIZE OFFSET path` -- the size is not the offset, and reading it as one
    is what made a fault inside `_CFHash` look like `CoreFoundation+0x2adafb`.
    """
    regions = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or not fields[0].startswith("0x"):
            continue
        try:
            start = int(fields[0], 16)
            end = int(fields[1], 16)
            offset = int(fields[3], 16)
        except ValueError:
            continue
        path = " ".join(fields[4:])
        if path.startswith("/"):
            regions.append((start, end, offset, path))
    return regions


def parse_frames(text: str) -> list[tuple[int, str]]:
    """(frame address, symbol line) from a gdb `bt`."""
    frames = []
    for line in text.splitlines():
        match = re.match(r"#(\d+)\s+(0x[0-9a-f]+)\s+in\s+(.*)", line)
        if match:
            frames.append((int(match.group(2), 16), match.group(3).strip()))
    return frames


def parse_stack_words(text: str) -> list[int]:
    words = []
    for line in text.splitlines():
        if not line.startswith("0x"):
            continue
        address, _, rest = line.partition(":")
        values = re.findall(r"0x[0-9a-f]{6,}", rest)
        words.extend(int(value, 16) for value in values)
    return words


@lru_cache(maxsize=None)
def segment_vmaddrs(path: str) -> list[tuple[int, int]]:
    """(file offset, vmaddr) per segment, so an address can be turned into a SYMBOL vmaddr.

    A FAT Mach-O is mapped from its slice, so the mapping's file offset is slice-relative while the
    symbols are vmaddr-relative; adding the file offset to the address (the obvious arithmetic) is wrong.
    """
    if not is_macho(path):
        return []
    text = run([OBJDUMP, "--macho", "--private-headers", path], timeout=600)
    segments, current = [], None
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "segname":
            current = {}
            segments.append(current)
            continue
        if current is None:
            continue
        if fields[0] == "fileoff" and len(fields) > 1:
            try:
                current["off"] = int(fields[1], 16)
            except ValueError:
                pass
        elif fields[0] == "vmaddr" and len(fields) > 1:
            try:
                current["va"] = int(fields[1], 16)
            except ValueError:
                pass
    return [(entry["off"], entry["va"]) for entry in segments if "off" in entry and "va" in entry]


def locate(regions: list[tuple[int, int, int, str]], address: int) -> tuple[str, int] | None:
    """Map an address to (path, SYMBOL vmaddr)."""
    for start, end, file_offset, path in regions:
        if not (start <= address < end):
            continue
        for segment_offset, segment_vmaddr in segment_vmaddrs(path):
            if segment_offset == file_offset:
                return path, segment_vmaddr + (address - start)
        # No segment table (or no match): the slice's first segment starts at vmaddr 0.
        return path, address - start
    return None


def is_macho(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


@lru_cache(maxsize=None)
def symbol_table(path: str) -> list[tuple[int, str]]:
    argv = [NM, "-n"]
    # A FAT Mach-O is symbolized per slice, and the wrong slice reports wrong symbols at plausible
    # offsets. The guest runs x86_64.
    if is_macho(path):
        argv.append("--arch=x86_64")
    argv.append(path)
    text = run(argv, timeout=600)
    table = []
    for line in text.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) < 3:
            continue
        try:
            table.append((int(fields[0], 16), fields[2]))
        except ValueError:
            continue
    table.sort()
    return table


def symbolize(path: str, offset: int) -> tuple[str, int] | None:
    if not os.path.exists(path):
        return None
    table = symbol_table(path)
    best = None
    for address, name in table:
        if address > offset:
            break
        best = (address, name)
    if best is None:
        return None
    return best[1], offset - best[0]


def describe(regions, address: int) -> str:
    spot = locate(regions, address)
    if spot is None:
        return f"{address:#x}  <unmapped>"
    path, offset = spot
    symbol = symbolize(path, offset)
    name = f"{path.rsplit('/', 1)[-1]}+{offset:#x}"
    if symbol:
        name += f"  {symbol[0]}+{symbol[1]:#x}"
    return f"{address:#x}  {name}"


@lru_cache(maxsize=None)
def call_return_sites(path: str) -> set[int]:
    """Every address a `call` can return to, so a stack word can be checked against real call sites.

    A stack scan without this check reports stale data as frames: this very diagnosis read
    `libswiftCore+0x16768` as the caller when 0x16768 is the MIDDLE of `movq %rdx,%rax` -- the fault was a
    tail jump and the word was leftover. A word is only a frame when the instruction before it is a call.
    """
    if not OBJDUMP or not os.path.exists(path):
        return set()
    argv = [OBJDUMP, "-d"]
    if is_macho(path):
        argv.append("--arch=x86_64")
    argv.append(path)
    text = run(argv, timeout=900)
    sites = set()
    for line in text.splitlines():
        match = re.match(r"\s+([0-9a-f]+):\s+((?:[0-9a-f]{2} )+)\s*(.*)", line)
        if not match:
            continue
        address = int(match.group(1), 16)
        length = len(match.group(2).split())
        mnemonic = match.group(3).strip()
        if mnemonic.startswith("call"):
            sites.add(address + length)
    return sites


@lru_cache(maxsize=None)
def looks_like_return_address(path: str, offset: int) -> bool:
    """Cheap, objdump-free test: is there a call instruction ending exactly at `offset`?

    A direct call is `e8 rel32` (5 bytes); an indirect call is `ff /2` or `ff /3`, i.e. 2 bytes through
    `%rax` or 3 bytes through a memory operand. Checking those four positions covers every x86_64 call
    shape this tree emits, and it works even when a full disassembly of the image is too slow to build.
    """
    try:
        with open(path, "rb") as handle:
            for back, expected in ((5, 0xE8), (2, 0xFF), (3, 0xFF), (6, 0xFF)):
                if offset - back < 0:
                    continue
                handle.seek(offset - back)
                if handle.read(1) == bytes([expected]):
                    return True
    except OSError:
        return False
    return False


@lru_cache(maxsize=None)
def disassemble_around(path: str, offset: int, width: int = 8) -> str:
    if not OBJDUMP:
        return ""
    argv = [OBJDUMP, "-d"]
    if is_macho(path):
        argv.append("--arch=x86_64")
    argv.append(path)
    text = run(argv, timeout=900)
    lines = [line for line in text.splitlines() if re.match(r"^\s+[0-9a-f]+:", line)]
    target = f"{offset:x}"
    picked = []
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{target}:"):
            start = max(0, index - width)
            picked = lines[start:index + width + 1]
            break
    return "\n".join(picked)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("core", type=Path)
    parser.add_argument("--frames", type=int, default=16, help="frame-pointer chain depth")
    parser.add_argument("--stack-words", type=int, default=256, help="raw stack words to inspect")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--disassemble", action="store_true", help="show instructions around the fault")
    parser.add_argument("--all-threads", action="store_true",
                        help="walk every thread's frame-pointer chain (a deadlocked guest has no faulting thread)")
    args = parser.parse_args(argv)

    if not GDB:
        print("guest-core-report: gdb not found", file=sys.stderr)
        return 2
    if not args.core.exists():
        print(f"guest-core-report: no such core: {args.core}", file=sys.stderr)
        return 2

    text = gdb_batch(
        args.core,
        [
            "set pagination off",
            "info registers",
            f"bt {args.frames}",
            "info proc mappings",
            "x/256gx $rsp",
        ],
    )
    if "__error__" in text and "info proc mappings" not in text:
        print(text, file=sys.stderr)
        return 2

    signal = parse_signal(text)
    registers, thread = parse_registers(text)
    mappings = parse_mappings(text)
    frames = parse_frames(text)
    words = parse_stack_words(text)[: args.stack_words]

    report = {
        "core": str(args.core),
        "signal": signal,
        "thread": thread,
        "registers": {name: hex(value) for name, value in registers.items()},
        "fault": describe(mappings, registers["rip"]) if "rip" in registers else None,
        "frames": [describe(mappings, address) for address, _ in frames],
        "frame_pointer_chain": [],
        "stack_return_addresses": [],
    }

    # Frame-pointer chain: the only reliable chain when the core has no unwind information, which is
    # the normal case for a guest Mach-O core.
    rbp = registers.get("rbp")
    seen = 0
    while rbp and seen < args.frames:
        next_text = gdb_batch(args.core, [f"x/2gx {rbp:#x}"])
        values = parse_stack_words(next_text)
        if len(values) < 2:
            break
        next_frame, ret = values[0], values[1]
        if not ret:
            break
        report["frame_pointer_chain"].append(describe(mappings, ret))
        seen += 1
        if next_frame <= rbp or next_frame - rbp > 0x100000:
            break
        rbp = next_frame

    for word in words:
        spot = locate(mappings, word)
        if spot is None:
            continue
        path, offset = spot
        if path.endswith(".so") or path.endswith(".so.6"):
            continue
        if offset not in call_return_sites(path) and not looks_like_return_address(path, offset):
            continue
        report["stack_return_addresses"].append(describe(mappings, word))

    if args.all_threads:
        listing = gdb_batch(args.core, ["set pagination off", "info threads"])
        threads = []
        for line in listing.splitlines():
            match = re.match(r"\s*\*?\s*(\d+)\s+(LWP \d+|Thread \S+)\s+0x", line)
            if match:
                threads.append((int(match.group(1)), match.group(2).strip()))
        report["threads"] = []
        for number, label in threads:
            regs_text = gdb_batch(args.core, [f"thread {number}", "info registers rip rbp rsp rdi rsi"])
            thread_regs, _ = parse_registers(regs_text)
            entry = {
                "thread": number,
                "label": label,
                "rip": describe(mappings, thread_regs["rip"]) if "rip" in thread_regs else None,
                "chain": [],
            }
            frame = thread_regs.get("rbp")
            seen = 0
            while frame and seen < 12:
                values = parse_stack_words(gdb_batch(args.core, [f"thread {number}", f"x/2gx {frame:#x}"]))
                if len(values) < 2 or not values[1]:
                    break
                entry["chain"].append(describe(mappings, values[1]))
                if values[0] <= frame or values[0] - frame > 0x100000:
                    break
                frame = values[0]
                seen += 1
            report["threads"].append(entry)
        if not args.json:
            print("all threads")
            for entry in report["threads"]:
                print(f"  thread {entry['thread']} ({entry['label']})")
                print(f"    pc  {entry['rip']}")
                for index, frame in enumerate(entry["chain"]):
                    print(f"    #{index + 1:<2} {frame}")
            return 0

    if args.disassemble and "rip" in registers:
        spot = locate(mappings, registers["rip"])
        if spot:
            report["disassembly"] = disassemble_around(spot[0], spot[1])

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"core     {args.core}")
    print(f"signal   {signal}")
    print(f"thread   {thread}")
    print("registers")
    for name in REGISTERS:
        if name in registers:
            print(f"  {name:<4} {registers[name]:#018x}")
    print(f"fault    {report['fault']}")
    if report["frame_pointer_chain"]:
        print("frame-pointer chain")
        for index, frame in enumerate(report["frame_pointer_chain"]):
            print(f"  #{index + 1:<2} {frame}")
    elif frames:
        print("gdb frames")
        for index, frame in enumerate(report["frames"]):
            print(f"  #{index:<2} {frame}")
    else:
        print("no frame chain: rbp=0 and gdb has no unwind information")
    if not report["stack_return_addresses"] and args.stack_words:
        print("no VERIFIED return address in the first %d stack words: the fault is a tail jump to a bad "
              "address, so no return address was pushed. The caller has to come from the register dump."
              % args.stack_words)
    if report["stack_return_addresses"]:
        print(f"VERIFIED return addresses on the stack ({len(report['stack_return_addresses'])} shown, outermost last)")
        print("  (a word is listed only when the instruction before it is a call, so stale data is not a frame)")
        for frame in report["stack_return_addresses"][: args.frames]:
            print(f"  {frame}")
    if "disassembly" in report and report["disassembly"]:
        print("disassembly around rip")
        for line in report["disassembly"].splitlines():
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

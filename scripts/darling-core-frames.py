#!/usr/bin/env python3
"""Resolve a guest process core's fault site and stack frames to image + offset + export.

WHY THIS EXISTS. Analysing the class-D crash by hand produced two wrong answers before the right one,
both from the same mistake: `gdb -batch -ex 'info proc mappings'` on a core prints
`Start End Size Offset objfile`, and the image offset of an address is `addr - Start`, NOT
`addr - Start + Offset`. Darling maps the Mach-O at its section VMAs, so adding the file offset puts the
fault thousands of pages past `__TEXT` and the "symbol" that comes out belongs to another function (or to
none). That produced `method_invoke + 0x1d` from a crash that is really `_objc_msgSend + 0x1d`, and it cost
a whole rung of investigation.

This tool owns that arithmetic, the register read, the stack scan and the export-symbol lookup, so the answer
is one command instead of four hand-written pipelines.

Usage:
    scripts/darling-core-frames.py --core CORE --prefix PREFIX [--mldr PATH] [--frames JSON] [--json]

The core is a guest process core (the loader/live process that mldr runs in). The binary used by gdb is the
loader from the prefix; only its mappings matter, so any gdb-readable binary works.
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAPPING_RE = re.compile(r"^\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(0x[0-9a-f]+)\s+(\S.*)$")
REG_RE = re.compile(r"^(rip|rsp|rbp|rdi|rsi)\s+(0x[0-9a-f]+)")


@dataclass(frozen=True)
class Mapping:
    start: int
    end: int
    size: int
    file_offset: int
    path: str

    def contains(self, addr: int) -> bool:
        return self.start <= addr < self.end


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stdout:
        raise SystemExit(f"{cmd[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def read_gdb(core: Path, loader: Path, stack_words: int) -> tuple[dict[str, int], list[Mapping], list[int]]:
    """Return the registers of the faulting thread, the mapping table, and raw stack words."""
    script = [
        "-batch",
        "-ex", "info registers rip rsp rbp rdi rsi",
        "-ex", "info proc mappings",
        "-ex", f"x/{stack_words}gx $rsp",
    ]
    args = ["gdb", "-q", *script, str(loader), str(core)]
    text = run(args)

    regs: dict[str, int] = {}
    mappings: list[Mapping] = []
    words: list[int] = []
    for line in text.splitlines():
        m = REG_RE.match(line.strip())
        if m:
            regs[m.group(1)] = int(m.group(2), 16)
            continue
        m = MAPPING_RE.match(line)
        if m:
            mappings.append(
                Mapping(
                    start=int(m.group(1), 16),
                    end=int(m.group(2), 16),
                    size=int(m.group(3), 16),
                    file_offset=int(m.group(4), 16),
                    path=m.group(5).strip(),
                )
            )
            continue
        if line.lstrip().startswith("0x") and ":" in line:
            for token in re.findall(r"0x[0-9a-f]{6,16}", line.split(":", 1)[1]):
                words.append(int(token, 16))
    return regs, mappings, words


def image_for(mappings: list[Mapping], addr: int) -> tuple[str, int] | None:
    """The image file and the offset INSIDE it for an address.

    The offset is `addr - mapping.start`: Darling maps the Mach-O at its section VMAs, so the mapping's
    file offset must not be added (that mistake is what this tool exists to prevent).
    """
    for m in mappings:
        if m.contains(addr) and not m.path.startswith("["):
            return m.path, addr - m.start
    return None


_EXPORT_CACHE: dict[str, list[tuple[int, str]]] = {}


def exports(image: str) -> list[tuple[int, str]]:
    cached = _EXPORT_CACHE.get(image)
    if cached is not None:
        return cached
    if not Path(image).is_file() or not shutil.which("llvm-objdump"):
        _EXPORT_CACHE[image] = []
        return []
    out = subprocess.run(
        ["llvm-objdump", "--macho", "--exports-trie", image], capture_output=True, text=True
    ).stdout
    syms: list[tuple[int, str]] = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].startswith("0x"):
            try:
                syms.append((int(fields[0], 16), fields[1]))
            except ValueError:
                continue
    syms.sort()
    _EXPORT_CACHE[image] = syms
    return syms


def symbolize(image: str, offset: int) -> str:
    """`symbol + delta` for the nearest exported symbol, or an honest statement that there is none.

    A large delta is reported as `far:` because an export table holds only the exported names: in a dylib
    whose static functions outnumber its exports, the nearest export can be genuinely unrelated, and
    presenting it as the containing function is how a symbol gets read as proof of a call that never
    happened. `far:` says "the name is not evidence here"; `disassemble` it instead.
    """
    syms = exports(image)
    if not syms:
        return "no-export-table"
    i = bisect.bisect_right(syms, (offset, "\uffff")) - 1
    if i < 0:
        return "before-first-export"
    base, name = syms[i]
    delta = offset - base
    text = name if delta == 0 else f"{name}+{delta:#x}"
    if i + 1 < len(syms):
        next_delta = syms[i + 1][0] - base
        if delta > next_delta:
            text += " (far: export table only; disassemble)"
    return text


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--loader", type=Path, default=None,
                        help="loader binary for gdb (default: PREFIX/libexec/darling/usr/libexec/darling/mldr)")
    parser.add_argument("--stack-words", type=int, default=256)
    parser.add_argument("--max-frames", type=int, default=24)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    loader = args.loader or args.prefix / "libexec/darling/usr/libexec/darling/mldr"
    if not loader.is_file():
        raise SystemExit(f"loader not found: {loader}")
    if not args.core.is_file():
        raise SystemExit(f"core not found: {args.core}")

    regs, mappings, words = read_gdb(args.core, loader, args.stack_words)

    result: dict[str, object] = {"core": str(args.core), "registers": {}}
    for name, value in regs.items():
        entry: dict[str, object] = {"value": f"{value:#x}"}
        located = image_for(mappings, value) if name in {"rip", "rsp", "rbp"} else None
        if located:
            image, offset = located
            entry.update(image=image, offset=f"{offset:#x}", symbol=symbolize(image, offset))
        elif name == "rip":
            entry["note"] = "not inside a file-backed mapping"
        result["registers"][name] = entry  # type: ignore[index]

    frames = []
    seen: set[tuple[str, int]] = set()
    for index, value in enumerate(words):
        located = image_for(mappings, value)
        if not located:
            continue
        image, offset = located
        if not Path(image).is_file():
            continue
        key = (image, offset)
        if key in seen:
            continue
        seen.add(key)
        frames.append({"stack_index": index, "value": f"{value:#x}", "image": image,
                       "offset": f"{offset:#x}", "symbol": symbolize(image, offset)})
        if len(frames) >= args.max_frames:
            break
    result["frames"] = frames

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"core: {args.core}")
    for name, entry in result["registers"].items():  # type: ignore[union-attr]
        line = f"  {name:4} {entry['value']}"
        if "image" in entry:
            line += f"  {Path(entry['image']).name} + {entry['offset']}  {entry['symbol']}"
        elif entry.get("note"):
            line += f"  ({entry['note']})"
        print(line)
    print("frames (stack words inside a file-backed image, nearest first):")
    for frame in frames:
        print(f"  stack[{frame['stack_index']:>3}] {Path(frame['image']).name} + {frame['offset']}  {frame['symbol']}")
    if not frames:
        print("  (none: the stack carries no image addresses -- an unwound or corrupted stack)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

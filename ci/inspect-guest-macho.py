#!/usr/bin/env python3
"""Write typed Mach-O evidence for one guest corpus pilot artifact."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from test_guest_macho import _macho_header


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} ARTIFACT JSON_OUTPUT TSV_OUTPUT", file=sys.stderr)
        return 2
    artifact, json_output, tsv_output = map(Path, sys.argv[1:])
    header = _macho_header(artifact)
    payload = {
        "schema": 1,
        "magic": header.magic,
        "architecture": header.architecture,
        "filetype": header.filetype,
        "dylib-load-commands": list(header.dylib_load_commands),
        "rpath-load-commands": list(header.rpath_load_commands),
    }
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    with tsv_output.open("w", encoding="utf-8") as stream:
        for key in (
            "schema",
            "magic",
            "architecture",
            "filetype",
            "dylib-load-commands",
            "rpath-load-commands",
        ):
            value = payload[key]
            if isinstance(value, list):
                value = json.dumps(value, separators=(",", ":"), sort_keys=True)
            stream.write(f"{key}\t{value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

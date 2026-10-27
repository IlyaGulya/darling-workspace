#!/usr/bin/env bash
# Consumer contract: repetition, normalization, short tails and parseable JSON.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$ROOT/scripts/dwdiag" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
import tempfile

runner = sys.argv[1]
with tempfile.TemporaryDirectory(prefix="dwdiag-loops-") as temporary:
    root = Path(temporary)

    def diagnose(name, lines):
        path = root / (name + ".log")
        path.write_text("\n".join(lines) + "\n")
        result = subprocess.run(
            [runner, "loops", "--log", str(path), "--min", "8", "--json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return json.loads(result.stdout)

    spin = diagnose("normalized", [
        f"[{marker} sp=0x{index:x}]"
        for index in range(1, 41)
        for marker in ("pc-entry", "pc-nothreads", "pc-postplane", "pc-return")
    ])
    assert spin["kind"] == "spin", spin
    assert spin["tail_distinct"] == 4, spin
    assert {row["pattern"]: row["count"] for row in spin["patterns"]} == {
        f"[{marker} sp=#]": 40
        for marker in ("pc-entry", "pc-nothreads", "pc-postplane", "pc-return")
    }, spin

    special = 'waiting for "reply" \\ peer Ω'
    escaped = diagnose("escaped", [special] * 16)
    assert escaped["kind"] == "spin", escaped
    assert escaped["patterns"][0]["pattern"] == special, escaped
    assert escaped["patterns"][0]["count"] == 16, escaped

    short = diagnose("short", ["alpha", "beta"])
    assert short["kind"] == "nonrepeating" and short["patterns"] == [], short
    assert short["tail_len"] == short["tail_distinct"] == 2, short

    insufficient = diagnose("below-threshold", ["same observation"] * 7)
    assert insufficient["kind"] != "spin" and insufficient["patterns"] == [], insufficient

print("PASS loops: normalized repetition, JSON round-trip, short and subthreshold tails")
PY

"""The `dwdiag` roster in the documents must match the tool.

MEASURED 2026-10-22: a session hand-rolled a core-frame resolver and a preloaded fault reporter next to
`dwdiag`, because `docs/tooling.md` listed 11 of the tool's 31 subcommands and `AGENTS.md` listed 5.
`dwdiag core` already printed the fault image, the symbolized frame-pointer walk and the registers;
`dwdiag unwind --catch SIGSEGV` already did the live fault capture. Nothing was missing except the index.

`scripts/dwdiag-roster.py` generates the two marked blocks from the tool's own `--help`, so this contract
only has to prove that the checked-in text equals what the generator produces. A new subcommand that nobody
documents therefore fails here rather than costing another session.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "dwdiag-roster.py"


def check_roster_is_current() -> None:
    proc = subprocess.run(
        [sys.executable, "-B", str(GENERATOR), "--check"], capture_output=True, text=True, cwd=ROOT
    )
    assert proc.returncode == 0, (
        "dwdiag roster is stale (the documents do not list the tool's commands):\n"
        f"{proc.stdout}{proc.stderr}\n"
        "Run scripts/dwdiag-roster.py --write and commit the result."
    )
    print("A ok: the dwdiag roster in docs/tooling.md and AGENTS.md is generated and current")


def check_the_roster_is_not_empty() -> None:
    # A generator that silently emits nothing would satisfy the check above.
    docs = (ROOT / "docs" / "tooling.md").read_text()
    assert "| `dwdiag core` |" in docs, "docs/tooling.md lost the `dwdiag core` row"
    assert "| `dwdiag unwind` |" in docs, "docs/tooling.md lost the `dwdiag unwind` row"
    agents = (ROOT / "AGENTS.md").read_text()
    inline = agents[agents.index("dwdiag-roster-inline:begin") : agents.index("dwdiag-roster-inline:end")]
    for command in ("core", "mappings", "unwind", "trap", "processes", "prefix"):
        assert f"|{command}|" in inline, f"AGENTS.md inline roster lost `{command}`"
    print("B ok: the generated blocks name the commands this session needed")


def main() -> int:
    check_roster_is_current()
    check_the_roster_is_not_empty()
    print("PASS dwdiag-roster-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

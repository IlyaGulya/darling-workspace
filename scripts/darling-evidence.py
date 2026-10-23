#!/usr/bin/env python3
"""Archive finalized diagnostics or stream raw/zstd evidence and finite log slices."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))

from evidence_storage import main


if __name__ == "__main__":
    raise SystemExit(main())

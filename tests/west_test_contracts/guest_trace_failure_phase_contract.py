"""Guest fixture trace-oracle failures retain their real run phase."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_guest_c import failure_phase_from_debug_bundle


with tempfile.TemporaryDirectory() as temp:
    bundle = Path(temp) / "bundle"
    bundle.mkdir()
    (bundle / "stderr.log").write_text(
        "WEST_GUEST_STAGE=upload\nWEST_GUEST_STAGE=run\n"
        "WEST_GUEST_TRACE_ORACLE_FAILED\n"
    )
    assert failure_phase_from_debug_bundle(f"BUNDLE={bundle}\nRESULT=failed\n") == "run"

    (bundle / "stderr.log").write_text("WEST_GUEST_STAGE=upload\n")
    assert failure_phase_from_debug_bundle(f"BUNDLE={bundle}\n") == "setup"

assert failure_phase_from_debug_bundle("RESULT=failed\n") is None
print("PASS guest-trace-failure-phase-contract")

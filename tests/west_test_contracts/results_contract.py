from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_results import InvocationResult


result = InvocationResult(17, "RED_OUTPUT\n", "run")
assert result.returncode == 17
assert result.output == "RED_OUTPUT\n"
assert result.failure_phase == "run"

print("PASS west-test-results-contract")

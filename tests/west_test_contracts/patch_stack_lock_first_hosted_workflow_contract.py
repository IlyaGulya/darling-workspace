#!/usr/bin/env python3
"""Keep the lock-first acceptance tier manual and structurally exact."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
workflow = (ROOT / ".github/workflows/patch-stack-lock-first.yml").read_text()
assert "on:\n  workflow_dispatch:" in workflow
assert "push:" not in workflow and "schedule:" not in workflow
assert "if: github.event_name == 'workflow_dispatch'" in workflow
assert "runs-on: ubuntu-latest" in workflow and "timeout-minutes: 25" in workflow
assert workflow.count("west patch apply --profile homebrew\n") == 1
assert workflow.count("west patch apply --profile homebrew --lock-first") == 1
assert "--lock-first-evidence \"$LOCK_FIRST_ROOT/evidence/lock-first-evidence.json\"" in workflow
assert "--shadow-lock" not in workflow
assert "git clone --no-local --no-hardlinks" in workflow and "fetch-depth: 0" in workflow
assert "cleanup_status=" in workflow and "if: always()" in workflow
assert "west-patch-shadow-* west-lock-materialize-* west-patch-lock-first-*" in workflow
assert "patch-stack-lock-first-acceptance" in workflow
print("patch-stack lock-first hosted-workflow contract: PASS")

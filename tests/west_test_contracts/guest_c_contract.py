"""Host orchestration proof; the launcher is simulated, not a Darling runtime."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from west_commands.test_guest_c import run_guest_c_fixture


class Command:
    def die(self, message):
        raise AssertionError(message)

    def err(self, message):
        print(message, file=sys.stderr)

    def _resolve_darling_launcher(self, prefix):
        raise AssertionError("explicit test launcher was not selected")

    def _debug_runner_args(self, invocation, env=None):
        return invocation["args"]

    def _debug_runner_timeout_seconds(self, invocation):
        return invocation["debug_timeout_seconds"]

    def _record_failure_phase(self, invocation, phase):
        self.phase = phase


state = Path(os.environ.get("DW_STATE_ROOT", ROOT.parent / ".west-state"))
scratch = state / "test-scratch"
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="metadata-guest-c-", dir=scratch) as temporary:
    root = Path(temporary)
    prefix = root / "prefix"
    prefix.mkdir()
    launcher = root / "launcher"
    launcher.write_text(
        "#!" + sys.executable + "\n"
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "prefix = Path(os.environ['DPREFIX'])\n"
        "try:\n"
        "    with (prefix / 'booted').open('x'):\n"
        "        time.sleep(11)\n"
        "except FileExistsError:\n"
        "    pass\n"
        "body = sys.argv[-1].replace('/tmp/', str(prefix) + '/')\n"
        "raise SystemExit(subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', body]).returncode)\n"
    )
    launcher.chmod(0o755)
    source = root / "oracle.c"
    source.write_text(
        '#include <stdio.h>\n'
        'int main(void) { int result = 6 * 7; '
        'printf("DECLARED_SETUP_BUDGET_OK %d\\n", result); return result != 42; }\n'
    )
    compiler = shutil.which("cc")
    assert compiler is not None, "host C compiler is required"
    environment = {
        **os.environ,
        "DPREFIX": str(prefix),
        "DARLING_LAUNCHER": str(launcher),
        "DW_STATE_ROOT": str(root / "state"),
    }
    invocation = {
        "name": "declared_setup_budget",
        "key": "declared-setup-budget",
        "cwd": str(root),
        "script_path": source,
        "guest_cc": compiler,
        "guest_cflags": "",
        "timeout_seconds": 20,
        "ok_marker": "DECLARED_SETUP_BUDGET_OK 42",
    }
    transcript = io.StringIO()
    with contextlib.redirect_stdout(transcript):
        result = run_guest_c_fixture(Command(), invocation, env=environment)
    assert result == 0, (result, transcript.getvalue())
    assert "DECLARED_SETUP_BUDGET_OK 42" in transcript.getvalue(), transcript.getvalue()
    assert not list(prefix.glob("declared_setup_budget.*")), "fixture artifacts survived cleanup"

print("PASS west-test-guest-c-contract: declared cold-start budget, compile/run oracle, cleanup")

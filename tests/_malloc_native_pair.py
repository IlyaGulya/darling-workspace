#!/usr/bin/env python3
"""One-off native source-base proof; West owns the prefix lifecycle."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace / "west_commands"))
from test_guest_execution import run_guest_shell_argv

prefix = Path(os.environ["DPREFIX"])
source = Path(os.environ["MALLOC_NATIVE_BASE_BINARY"])
expected = os.environ["MALLOC_NATIVE_BASE_SHA256"]
if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
    raise SystemExit("source-base native executable checksum mismatch")
with tempfile.TemporaryDirectory(prefix="malloc-native-pair-", dir=prefix / "private/var/tmp") as temporary:
    binary = Path(temporary) / "rack_region_generation_base"
    shutil.copyfile(source, binary)
    binary.chmod(0o755)
    guest_binary = "/" + binary.relative_to(prefix).as_posix()
    program = '''
set +e
output=$("$1" 2>&1)
red=$?
set -e
test "$red" -eq 1
/usr/libexec/rack_region_generation
printf '%s\\n' "$output"
test "$output" = 'RACK_REGION_GENERATION_LOST retained_lookup=0'
printf 'MALLOC_NATIVE_SOURCE_BASE_RED_GREEN_OK red_rc=%s green_rc=0\n' "$red"
'''
    result = run_guest_shell_argv(
        os.environ["DARLING_LAUNCHER"], prefix,
        ("/bin/bash", "-c", program, "malloc-native-pair", guest_binary),
        cwd=workspace, env=dict(os.environ), timeout_seconds=60,
    )
    raise SystemExit(result.returncode)

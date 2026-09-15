"""Parallel linker admission probe for the runtime under test.

The historical form of this probe replayed a captured CPack link command with
its relative object paths and its session-specific build root, so it could only
run on a prefix that still held that build tree, and it read its argv from a
host debug directory. This version states the same claim with inputs it creates
itself: four linkers run concurrently against one SDK with separate outputs, and
all four must succeed with a Mach-O executable. That exercises what the probe is
for - concurrent linkers sharing the runtime's transport and file machinery -
without depending on any captured session artifact.
"""
import os
from pathlib import Path
import sys
import tempfile

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace / "west_commands"))
from test_guest_execution import run_guest_shell_argv

prefix = Path(os.environ["DPREFIX"])

GUEST_PROGRAM = r'''
set -euo pipefail
work=$1
cd "$work"
clang=/Library/Developer/CommandLineTools/usr/bin/clang
ld=/Library/Developer/CommandLineTools/usr/bin/ld
sdk=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk
test -x "$clang"
test -x "$ld"
test -d "$sdk"

# Four independent objects, each with its own main, linked by four concurrent
# linkers into four separate executables.
for index in 0 1 2 3; do
    printf 'int main(void){return %s;}\n' "$index" > "probe-$index.c"
    "$clang" -isysroot "$sdk" -std=gnu11 -c "probe-$index.c" -o "probe-$index.o"
done

pids=""
for index in 0 1 2 3; do
    "$ld" -demangle -dynamic -arch x86_64 -platform_version macos 11.0.0 11.3 \
        -syslibroot "$sdk" -w -o "linked-$index" \
        -search_paths_first -headerpad_max_install_names \
        -oso_prefix "$work" \
        -mllvm -disable-aligned-alloc-awareness=1 \
        "probe-$index.o" -lc++ -lSystem &
    pids="$pids $!"
done

result=0
for pid in $pids; do
    wait "$pid" || result=1
done

for index in 0 1 2 3; do
    test -s "linked-$index" || result=1
    magic=$(od -An -tx1 -N4 "linked-$index" | tr -d ' \n')
    if [ "$magic" != "cffaedfe" ]; then
        printf 'PARALLEL_LINK_MAGIC_MISMATCH index=%s magic=%s\n' "$index" "$magic"
        result=1
    fi
done

test "$result" = 0
printf 'PARALLEL_LINK_RESULT rc=0 outputs=4 magic=cffaedfe\n'
'''

with tempfile.TemporaryDirectory(prefix="parallel-link-probe-", dir=prefix / "private/var/tmp") as temporary:
    guest_directory = "/" + Path(temporary).relative_to(prefix).as_posix()
    result = run_guest_shell_argv(
        os.environ["DARLING_LAUNCHER"],
        prefix,
        (
            "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "TMPDIR=/private/var/tmp",
            "/bin/bash", "-c", GUEST_PROGRAM, "parallel-link-probe", guest_directory,
        ),
        cwd=workspace,
        env=dict(os.environ),
        timeout_seconds=900,
    )
raise SystemExit(result.returncode)

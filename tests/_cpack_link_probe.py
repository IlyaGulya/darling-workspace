"""Disposable parallel linker diagnostic, not a CMake acceptance verdict."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace / "west_commands"))
from test_guest_execution import run_guest_shell_argv

prefix = Path(os.environ["DPREFIX"])
argv = json.loads(Path("/home/ilyagulya/work/darling-debug/ring-off-cpack-ld-argv.json").read_text())
output_index = argv.index("-o") + 1
with tempfile.TemporaryDirectory(prefix="cpack-link-admission-", dir=prefix / "private/var/tmp") as temporary:
    directory = Path(temporary)
    program = ['set -e', 'cd /private/tmp/cmake-20260910-1193500-9bdk94/cmake-4.4.3/Source', 'pids=""']
    for index in range(4):
        fifo = directory / str(index)
        os.mkfifo(fifo)
        guest_fifo = "/" + fifo.relative_to(prefix).as_posix()
        command = list(argv)
        command[output_index] = f"/private/var/tmp/cpack-link-probe-{index}"
        child = 'printf "CPACK_LINK_READY label=%s pid=%s\\n" "$0" "$$"; IFS= read -r token < "$1"; shift; exec "$@"'
        program.append(shlex.join(["/bin/bash", "-c", child, f"cpack-link-probe-{index}", guest_fifo, *command]) + ' &')
        program.append('pids="$pids $!"')
    program += ['result=0', 'for pid in $pids; do wait "$pid" || result=1; done', 'printf "CPACK_PARALLEL_LINK_RESULT rc=%s\\n" "$result"', 'exit "$result"']
    result = run_guest_shell_argv(
        os.environ["DARLING_LAUNCHER"], prefix,
        ("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "TMPDIR=/private/tmp", "/bin/bash", "-c", "\n".join(program)),
        cwd=workspace, env=dict(os.environ), timeout_seconds=900,
    )
raise SystemExit(result.returncode)

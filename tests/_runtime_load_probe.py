"""Parameterised runtime load fixture for the guest runtime under test.

The stock replays are the only tests that exercise the runtime under a real
parallel build workload, and they cost tens of minutes to hours because they
build cmake, wget and openssl from source.  What they provoke is smaller than a
package build: many concurrent fork/exec, parallel compiler and linker
invocations, and heavy file and descriptor churn across the runtime's
transport, all against one shared darlingserver.  That essence is reproduced
here in seconds, from parameters the test declaration chooses:

  RUNTIME_LOAD_SOURCES      translation units generated and built per round
  RUNTIME_LOAD_CONCURRENCY  concurrent guest CLT clang/ld processes
  RUNTIME_LOAD_ROUNDS       repetitions of the whole cycle

The generated guest program compiles the units with M concurrent clang
processes, links them with M concurrent linkers, runs a fork/exec storm over
the linked units (each one a pipe round trip through read/write), and churns a
set of inherited descriptors.  Every stage must succeed and prints its own
marker; the terminal ``RUNTIME_LOAD_OK sources=N concurrency=M rounds=R`` line
is the verdict.

Host smoke overrides (never set in a guest run):

  RUNTIME_LOAD_CLT_ROOT   CommandLineTools root, default
                          /Library/Developer/CommandLineTools
  RUNTIME_LOAD_WORK_ROOT  host directory to create the work tree under; the
                          default is the prefix's private/var/tmp, mapped into
                          the guest as /private/var/tmp/...

The fixture reports failure by exit status; a parameter that cannot be used is
rejected before any transport work starts.
"""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace / "west_commands"))
from test_guest_execution import run_guest_shell_argv  # noqa: E402


DEFAULT_SOURCES = 8
DEFAULT_CONCURRENCY = 4
DEFAULT_ROUNDS = 2
GUEST_CLT_ROOT = "/Library/Developer/CommandLineTools"
DESCRIPTOR_COUNT = 8
SOURCES_LIMIT = 256
CONCURRENCY_LIMIT = 64
ROUNDS_LIMIT = 16
PARAMETERS = (
    "RUNTIME_LOAD_SOURCES",
    "RUNTIME_LOAD_CONCURRENCY",
    "RUNTIME_LOAD_ROUNDS",
)


class InvalidParameter(ValueError):
    """A declared load parameter cannot be used as written."""


@dataclass(frozen=True)
class LoadPlan:
    sources: int
    concurrency: int
    rounds: int
    clt_root: str
    descriptor_count: int = DESCRIPTOR_COUNT

    def verdict(self) -> str:
        return (
            "RUNTIME_LOAD_OK "
            f"sources={self.sources} concurrency={self.concurrency} "
            f"rounds={self.rounds}"
        )


def _bounded_int(environment: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    raw = environment.get(name)
    if raw is None or str(raw) == "":
        return default
    text = str(raw).strip()
    if not text.lstrip("+").isdigit():
        raise InvalidParameter(f"{name}={text!r} is not a positive integer")
    value = int(text)
    if not 1 <= value <= maximum:
        raise InvalidParameter(f"{name}={value} is outside 1..{maximum}")
    return value


def load_plan(environment: Mapping[str, str] | None = None) -> LoadPlan:
    """Normalize and validate the declared load parameters."""

    environment = os.environ if environment is None else environment
    return LoadPlan(
        sources=_bounded_int(environment, "RUNTIME_LOAD_SOURCES", DEFAULT_SOURCES, SOURCES_LIMIT),
        concurrency=_bounded_int(
            environment, "RUNTIME_LOAD_CONCURRENCY", DEFAULT_CONCURRENCY, CONCURRENCY_LIMIT
        ),
        rounds=_bounded_int(environment, "RUNTIME_LOAD_ROUNDS", DEFAULT_ROUNDS, ROUNDS_LIMIT),
        clt_root=str(environment.get("RUNTIME_LOAD_CLT_ROOT") or GUEST_CLT_ROOT),
    )


UNIT_TEMPLATE = r"""#include <stdio.h>
#include <unistd.h>

int main(void) {
    char buffer[512];
    ssize_t count;
    while ((count = read(0, buffer, sizeof buffer)) > 0) {
        if (write(1, buffer, (size_t) count) != count) {
            return 1;
        }
    }
    if (count < 0) {
        return 1;
    }
    printf("unit __UNIT_INDEX__\n");
    fflush(stdout);
    return 0;
}
"""

PROGRAM_BODY = r"""
clang="$clt/usr/bin/clang"
ld="$clt/usr/bin/ld"
sdk="$clt/SDKs/MacOSX.sdk"

toolchain_rc=0
test -x "$clang" || toolchain_rc=1
test -x "$ld" || toolchain_rc=1
test -d "$sdk" || toolchain_rc=1
if [ "$toolchain_rc" != 0 ]; then
    printf 'RUNTIME_LOAD_TOOLCHAIN_MISSING clt=%s\n' "$clt" >&2
    exit 1
fi
printf 'RUNTIME_LOAD_TOOLCHAIN_OK clt=%s\n' "$clt"

cat > unit-template.c <<'UNIT_SOURCE'
__UNIT_TEMPLATE__UNIT_SOURCE

# Build chunk_size units starting at chunk_start with chunk_size concurrent
# compiler processes, then wait for all of them.
compile_chunk() {
    chunk_start=$1
    chunk_size=$2
    chunk_pids=""
    chunk_offset=0
    while [ "$chunk_offset" -lt "$chunk_size" ]; do
        chunk_unit=$((chunk_start + chunk_offset))
        "$clang" -isysroot "$sdk" -std=gnu11 -c "unit-$chunk_unit.c" -o "unit-$chunk_unit.o" &
        chunk_pids="$chunk_pids $!"
        chunk_offset=$((chunk_offset + 1))
    done
    chunk_rc=0
    for chunk_pid in $chunk_pids; do
        wait "$chunk_pid" || chunk_rc=1
    done
    return "$chunk_rc"
}

# Link the same chunk window concurrently into one executable per unit.
link_chunk() {
    link_start=$1
    link_size=$2
    link_pids=""
    link_offset=0
    while [ "$link_offset" -lt "$link_size" ]; do
        link_unit=$((link_start + link_offset))
        "$ld" -demangle -dynamic -arch x86_64 -platform_version macos 11.0.0 11.3 \
            -syslibroot "$sdk" -w -o "linked-$link_unit" \
            -search_paths_first -headerpad_max_install_names \
            -oso_prefix "$work" \
            -mllvm -disable-aligned-alloc-awareness=1 \
            "unit-$link_unit.o" -lc++ -lSystem &
        link_pids="$link_pids $!"
        link_offset=$((link_offset + 1))
    done
    link_rc=0
    for link_pid in $link_pids; do
        wait "$link_pid" || link_rc=1
    done
    return "$link_rc"
}

# Fork/exec storm: every linked unit runs concurrently, each one fed through a
# pipe, so the runtime sees one burst of execve and descriptor inheritance.
burst_wave() {
    burst_size=$1
    burst_pids=""
    burst_index=0
    while [ "$burst_index" -lt "$burst_size" ]; do
        ( printf 'payload-%s\n' "$burst_index" | "./linked-$burst_index" > "exec-$burst_index.out" ) &
        burst_pids="$burst_pids $!"
        burst_index=$((burst_index + 1))
    done
    burst_rc=0
    for burst_pid in $burst_pids; do
        wait "$burst_pid" || burst_rc=1
    done
    return "$burst_rc"
}

verify_exec() {
    verify_rc=0
    verify_index=0
    while [ "$verify_index" -lt "$sources" ]; do
        if ! grep -qx "payload-$verify_index" "exec-$verify_index.out"; then
            printf 'RUNTIME_LOAD_EXEC_MISMATCH unit=%s line=payload\n' "$verify_index" >&2
            verify_rc=1
        fi
        if ! grep -qx "unit $verify_index" "exec-$verify_index.out"; then
            printf 'RUNTIME_LOAD_EXEC_MISMATCH unit=%s line=unit\n' "$verify_index" >&2
            verify_rc=1
        fi
        verify_index=$((verify_index + 1))
    done
    return "$verify_rc"
}

rc=0
round=1
while [ "$round" -le "$rounds" ]; do
    index=0
    while [ "$index" -lt "$sources" ]; do
        sed "s/__UNIT_INDEX__/$index/" unit-template.c > "unit-$index.c"
        index=$((index + 1))
    done
    printf 'RUNTIME_LOAD_SOURCES_OK round=%s sources=%s\n' "$round" "$sources"

    index=0
    while [ "$index" -lt "$sources" ]; do
        remaining=$((sources - index))
        if [ "$remaining" -gt "$concurrency" ]; then
            remaining=$concurrency
        fi
        compile_chunk "$index" "$remaining" || rc=1
        index=$((index + remaining))
    done
    printf 'RUNTIME_LOAD_COMPILE_OK round=%s sources=%s concurrency=%s\n' "$round" "$sources" "$concurrency"

    index=0
    while [ "$index" -lt "$sources" ]; do
        remaining=$((sources - index))
        if [ "$remaining" -gt "$concurrency" ]; then
            remaining=$concurrency
        fi
        link_chunk "$index" "$remaining" || rc=1
        index=$((index + remaining))
    done
    index=0
    while [ "$index" -lt "$sources" ]; do
        test -s "unit-$index.o" || rc=1
        test -x "linked-$index" || rc=1
        index=$((index + 1))
    done
    printf 'RUNTIME_LOAD_LINK_OK round=%s outputs=%s\n' "$round" "$sources"

    burst_wave "$sources" || rc=1
    verify_exec || rc=1
    printf 'RUNTIME_LOAD_EXEC_OK round=%s spawns=%s\n' "$round" "$sources"

__DESCRIPTOR_STAGE__
    printf 'RUNTIME_LOAD_DESCRIPTOR_OK round=%s descriptors=%s\n' "$round" "$descriptor_count"

    round=$((round + 1))
done

if [ "$rc" != 0 ]; then
    printf 'RUNTIME_LOAD_FAILED rounds=%s\n' "$rounds" >&2
    exit 1
fi
printf 'RUNTIME_LOAD_OK sources=%s concurrency=%s rounds=%s\n' "$sources" "$concurrency" "$rounds"
"""


def descriptor_stage(plan: LoadPlan) -> str:
    """Emit the descriptor round trip for one round, one explicit fd at a time.

    Explicit fd numbers keep the generated program portable to the bash 3.2
    the guest ships; `{fd}` allocation and associative arrays are not used.
    """

    descriptors = list(range(3, 3 + plan.descriptor_count))
    lines = ["    # Descriptor round trip: open, write through, close, reopen and read back."]
    for descriptor in descriptors:
        lines.append(f'    exec {descriptor}>"$work/descriptor-$round-{descriptor}"')
    for descriptor in descriptors:
        lines.append(f"    printf 'descriptor-{descriptor}\\n' >&{descriptor}")
    for descriptor in descriptors:
        lines.append(f"    exec {descriptor}>&-")
    for descriptor in descriptors:
        lines.append(f'    exec {descriptor}<"$work/descriptor-$round-{descriptor}"')
        lines.append(f"    read -r descriptor_line <&{descriptor} || rc=1")
        lines.append(
            "    if [ \"$descriptor_line\" != "
            f"'descriptor-{descriptor}' ]; then"
        )
        lines.append(
            "        printf 'RUNTIME_LOAD_DESCRIPTOR_MISMATCH fd="
            f"{descriptor} value=%s\\n' \"$descriptor_line\" >&2"
        )
        lines.append("        rc=1")
        lines.append("    fi")
        lines.append(f"    exec {descriptor}<&-")
    return "\n".join(lines)


def guest_program(plan: LoadPlan) -> str:
    """Render the guest-side program for one validated load plan."""

    header = (
        "set -euo pipefail\n"
        f"clt={shlex.quote(plan.clt_root)}\n"
        f"sources={plan.sources}\n"
        f"concurrency={plan.concurrency}\n"
        f"rounds={plan.rounds}\n"
        f"descriptor_count={plan.descriptor_count}\n"
        'work="$1"\n'
        'cd "$work"\n'
    )
    body = PROGRAM_BODY.replace("__UNIT_TEMPLATE__", UNIT_TEMPLATE).replace(
        "__DESCRIPTOR_STAGE__", descriptor_stage(plan)
    )
    return header + body


def guest_argv(plan: LoadPlan, work_directory: str) -> tuple[str, ...]:
    """Build the argv vector carried by the shared guest transport."""

    return (
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "TMPDIR=/private/var/tmp",
        f"RUNTIME_LOAD_CLT_ROOT={plan.clt_root}",
        "/bin/bash",
        "-c",
        guest_program(plan),
        "runtime-load-probe",
        work_directory,
    )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ if env is None else env)
    if arguments:
        sys.stderr.write(
            "runtime-load-probe: unexpected arguments: " + " ".join(arguments) + "\n"
        )
        return 2
    try:
        plan = load_plan(environment)
    except InvalidParameter as error:
        sys.stderr.write(f"RUNTIME_LOAD_INVALID {error}\n")
        return 2

    prefix_text = environment.get("DPREFIX") or environment.get("DARLING_PREFIX")
    launcher = environment.get("DARLING_LAUNCHER") or environment.get("DARLING")
    if not prefix_text or not launcher:
        sys.stderr.write(
            "RUNTIME_LOAD_INVALID guest run needs DPREFIX and DARLING_LAUNCHER\n"
        )
        return 2
    prefix = Path(prefix_text)

    work_root = environment.get("RUNTIME_LOAD_WORK_ROOT")
    if work_root:
        work_root_path = Path(work_root)
        host_work_root = True
    else:
        work_root_path = prefix / "private/var/tmp"
        host_work_root = False
    work_root_path.mkdir(parents=True, exist_ok=True)
    timeout_seconds = int(environment.get("RUNTIME_LOAD_TIMEOUT_SECONDS") or "600")

    with tempfile.TemporaryDirectory(prefix="runtime-load-", dir=work_root_path) as temporary:
        temporary_path = Path(temporary)
        if host_work_root:
            work_directory = str(temporary_path)
        else:
            work_directory = "/" + temporary_path.relative_to(prefix).as_posix()
        result = run_guest_shell_argv(
            launcher,
            prefix,
            guest_argv(plan, work_directory),
            cwd=workspace,
            env=environment,
            timeout_seconds=timeout_seconds,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

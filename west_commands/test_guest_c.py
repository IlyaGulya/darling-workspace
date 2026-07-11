"""Metadata guest-C fixture runner for west test."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from shlex import quote

try:
    from .test_execution import run_bounded
    from .test_guest_execution import resolve_guest_execution
except ImportError:  # Loaded as a West extension module, not a package.
    from test_execution import run_bounded
    from test_guest_execution import resolve_guest_execution


def failure_phase_from_debug_bundle(output: str) -> str | None:
    """Classify a guest fixture failure from the runner bundle's own stage markers."""
    match = re.search(r"^BUNDLE=(.+)$", output, flags=re.MULTILINE)
    if match is None:
        return None
    log_path = Path(match.group(1)) / "stderr.log"
    try:
        content = log_path.read_text(errors="replace")
    except OSError:
        return None
    if "WEST_GUEST_TRACE_ORACLE_FAILED" in content:
        return "run"
    stages = re.findall(r"^WEST_GUEST_STAGE=([a-z-]+)$", content, flags=re.MULTILINE)
    if not stages:
        return None
    return {
        "compile": "compile",
        "run": "run",
        "upload": "setup",
        "cleanup": "setup",
        "namespace-retry": "setup",
    }.get(stages[-1])


def run_guest_c_fixture(command, invocation, env=None) -> int:
    run_env = env if env is not None else invocation.get("env")
    if not run_env:
        run_env = command._execution_env(invocation)
    if not run_env:
        run_env = os.environ.copy()

    guest = resolve_guest_execution(
        name=invocation["name"],
        env=run_env,
        fallback_prefix=getattr(command, "_prefix", None),
        resolve_launcher=command._resolve_darling_launcher,
        die=command.die,
    )
    prefix = guest.prefix
    launcher = guest.launcher

    with tempfile.TemporaryDirectory(prefix=f"west-guest-c-fixture-{invocation['name']}-") as temp:
        tempdir = Path(temp)
        host_runner = tempdir / "run.sh"
        verdict = tempdir / "verdict.txt"
        guest_shell_helper = (
            Path(__file__).resolve().parents[1]
            / "testkit/scripts/darling-guest-shell.sh"
        )
        if not guest_shell_helper.is_file():
            command.die(f"missing shared guest shell helper: {guest_shell_helper}")
        name = invocation["name"]
        run_id = run_env.get("WEST_GUEST_C_FIXTURE_ID") or f"{os.getpid()}.{int(time.time() * 1000)}"
        guest_src = f"/tmp/{name}.{run_id}.c"
        guest_bin = f"/tmp/{name}.{run_id}"
        compile_parts = [
            '"$guest_cc"',
            *[quote(arg) for arg in invocation.get("guest_cflags", "").split() if arg],
            *[quote(arg) for arg in invocation.get("compile_flags", [])],
            "-o",
            quote(guest_bin),
            quote(guest_src),
            *[quote(arg) for arg in invocation.get("link_flags", [])],
        ]
        run_parts = [
            quote(guest_bin),
            *[quote(arg) for arg in invocation.get("run_args", [])],
        ]
        guest_prelude = invocation.get("guest_prelude", "")
        if not guest_prelude:
            guest_prelude = ":"
        guest_env_setup = "\n".join(
            f"export {key}={quote(value)}"
            for key, value in invocation.get("guest_env_vars", {}).items()
        ) or ":"
        trace_setup_lines = []
        trace_check_lines = []
        trace_dump_lines = []
        for index, temp_file in enumerate(invocation.get("host_temp_files", [])):
            if not isinstance(temp_file, dict):
                command.die(f"{invocation['name']}: host-temp-files entries must be mappings")
            env_name = str(temp_file.get("env", ""))
            rel_path = str(temp_file.get("prefix-relative-path", ""))
            if not env_name or not rel_path:
                command.die(
                    f"{invocation['name']}: host-temp-files[{index}] needs env "
                    "and prefix-relative-path"
                )
            if rel_path.startswith("/") or ".." in Path(rel_path).parts:
                command.die(
                    f"{invocation['name']}: host-temp-files[{index}] path must "
                    "be prefix-relative"
                )
            temp_var = f"host_temp_{index}"
            trace_setup_lines.extend(
                [
                    f"{temp_var}=\"$DPREFIX/{rel_path}\"",
                    f"rm -f \"${temp_var}\"",
                    f"mkdir -p \"$(dirname \"${temp_var}\")\"",
                    f"export {env_name}=\"${temp_var}\"",
                ]
            )
            if "contents" in temp_file:
                trace_setup_lines.append(
                    f"printf %s {quote(str(temp_file['contents']))} > \"${temp_var}\""
                )
            trace_dump_lines.extend(
                [
                    f"if [ -f \"${temp_var}\" ]; then",
                    f"\tprintf '%s\\n' \"--- host temp file: ${temp_var} ---\" >&2",
                    f"\tcat \"${temp_var}\" >&2 || true",
                    "fi",
                ]
            )
        for index, trace in enumerate(invocation.get("host_trace_files", [])):
            if not isinstance(trace, dict):
                command.die(f"{invocation['name']}: host-trace-files entries must be mappings")
            env_name = str(trace.get("env", ""))
            rel_path = str(trace.get("prefix-relative-path", ""))
            if not env_name or not rel_path:
                command.die(
                    f"{invocation['name']}: host-trace-files[{index}] needs env "
                    "and prefix-relative-path"
                )
            if rel_path.startswith("/") or ".." in Path(rel_path).parts:
                command.die(
                    f"{invocation['name']}: host-trace-files[{index}] path must "
                    "be prefix-relative"
                )
            contains = [str(item) for item in trace.get("contains", [])]
            trace_var = f"host_trace_{index}"
            trace_setup_lines.extend(
                [
                    f"{trace_var}=\"$DPREFIX/{rel_path}\"",
                    f"rm -f \"${trace_var}\"",
                    f"mkdir -p \"$(dirname \"${trace_var}\")\"",
                    f"export {env_name}=\"${trace_var}\"",
                ]
            )
            trace_dump_lines.extend(
                [
                    f"if [ -f \"${trace_var}\" ]; then",
                    f"\tprintf '%s\\n' \"--- host trace file: ${trace_var} ---\" >&2",
                    f"\tcat \"${trace_var}\" >&2 || true",
                    "fi",
                ]
            )
            trace_check_lines.extend(
                [
                    f"if [ ! -f \"${trace_var}\" ]; then",
                    f"\tprintf 'missing host trace file: %s\\n' \"${trace_var}\" >&2",
                    "\tprintf '%s\\n' 'WEST_GUEST_TRACE_ORACLE_FAILED' >&2",
                    "\texit 1",
                    "fi",
                    f"cat \"${trace_var}\"",
                ]
            )
            for expected in contains:
                trace_check_lines.extend(
                    [
                        f"if ! grep -F -q {quote(expected)} \"${trace_var}\"; then",
                        f"\tprintf 'missing host trace content: %s\\n' {quote(expected)} >&2",
                        "\tprintf '%s\\n' 'WEST_GUEST_TRACE_ORACLE_FAILED' >&2",
                        "\texit 1",
                        "fi",
                    ]
                )
        trace_setup = "\n".join(trace_setup_lines) or ":"
        trace_check = "\n".join(trace_check_lines) or ":"
        trace_dump = "\n".join(trace_dump_lines) or ":"
        trace_settle = "sleep 0.25" if invocation.get("host_trace_files") else ":"
        host_stat_deltas = invocation.get("host_stat_deltas", [])
        host_stat_specs = quote(json.dumps(host_stat_deltas))
        host_stat_tool = quote(
            str(invocation.get("_host_stat_tool", invocation.get("host_stat_tool", "darling-stat")))
        )
        host_stat_setup = ":"
        host_stat_before = ":"
        host_stat_after = ":"
        host_stat_dump = ":"
        host_stat_check = ":"
        if host_stat_deltas:
            host_stat_setup = f"""host_stat_tool={host_stat_tool}
host_stat_before={quote(str(tempdir / "stat-before.json"))}
host_stat_after={quote(str(tempdir / "stat-after.json"))}
host_stat_specs={host_stat_specs}
if [ ! -x "$host_stat_tool" ]; then
\tprintf 'missing darling stat tool: %s\\n' "$host_stat_tool" >&2
\texit 1
fi"""
            host_stat_before = '"$host_stat_tool" "$DPREFIX" > "$host_stat_before"'
            host_stat_after = '"$host_stat_tool" "$DPREFIX" > "$host_stat_after"'
            host_stat_dump = """if [ -f "$host_stat_before" ]; then
\tprintf '%s\\n' '--- host stat before ---' >&2
\tcat "$host_stat_before" >&2 || true
fi
if [ -f "$host_stat_after" ]; then
\tprintf '%s\\n' '--- host stat after ---' >&2
\tcat "$host_stat_after" >&2 || true
fi"""
            host_stat_check = """python3 - "$host_stat_specs" "$host_stat_before" "$host_stat_after" <<'PY'
import json
import sys

specs = json.loads(sys.argv[1])
with open(sys.argv[2], encoding="utf-8") as handle:
before = json.load(handle)
with open(sys.argv[3], encoding="utf-8") as handle:
after = json.load(handle)

def value_at(snapshot, path):
current = snapshot
for part in path.split("."):
    if not isinstance(current, dict) or part not in current:
        raise KeyError(path)
    current = current[part]
if not isinstance(current, (int, float)):
    raise TypeError(path)
return current

failed = False
for spec in specs:
path = str(spec["path"])
minimum = float(spec.get("min-delta", 1))
old = value_at(before, path)
new = value_at(after, path)
delta = new - old
print(f"HOST_STAT_DELTA {path} {delta:g}")
if delta < minimum:
    print(
        f"host stat delta too small for {path}: {delta:g} < {minimum:g}",
        file=sys.stderr,
    )
    failed = True
if failed:
sys.exit(1)
PY"""
        needs_server_env_restart = bool(
            invocation.get("host_temp_files")
            or invocation.get("host_trace_files")
            or host_stat_deltas
        )
        server_env_restart = (
            "\"$launch\" shutdown >/dev/null 2>&1 || true"
            if needs_server_env_restart
            else ":"
        )
        guest_compile_body = f"""
{guest_prelude}
{guest_env_setup}
guest_cc={quote(invocation["guest_cc"])}
if [ ! -x "$guest_cc" ]; then guest_cc=clang; fi
{' '.join(compile_parts)}
compile_rc=$?
if [ "$compile_rc" -ne 0 ]; then
\tprintf 'ORACLE_RC=%s\\n' "$compile_rc"
\texit "$compile_rc"
fi
"""
        guest_run_body = f"""
{guest_prelude}
{guest_env_setup}
{' '.join(run_parts)}
run_rc=$?
printf 'ORACLE_RC=%s\\n' "$run_rc"
exit "$run_rc"
"""
        if host_stat_deltas:
            guest_workload = f"""
set +e
printf 'WEST_GUEST_STAGE=compile\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_compile_body)} > "$verdict" 2>&1
compile_rc=$?
set -e
if [ "$compile_rc" -ne 0 ]; then
\tcat "$verdict" 2>/dev/null || true
\texit "$compile_rc"
fi
{host_stat_before}
set +e
printf 'WEST_GUEST_STAGE=run\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_run_body)} >> "$verdict" 2>&1
rc=$?
set -e
{host_stat_after}
"""
        else:
            guest_workload = f"""
set +e
printf 'WEST_GUEST_STAGE=compile\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_compile_body)} > "$verdict" 2>&1
compile_rc=$?
set -e
if [ "$compile_rc" -ne 0 ]; then
\tcat "$verdict" 2>/dev/null || true
\texit "$compile_rc"
fi
set +e
printf 'WEST_GUEST_STAGE=run\\n' >&2
guest_shell "$timeout_seconds" {quote(guest_run_body)} >> "$verdict" 2>&1
rc=$?
set -e
"""
        script = f"""#!/usr/bin/env bash
set -euo pipefail
: "${{DPREFIX:?set DPREFIX}}"
launch={quote(str(launcher))}
guest_shell_helper={quote(str(guest_shell_helper))}
host_src={quote(str(invocation["script_path"]))}
verdict={quote(str(verdict))}
guest_src={quote(guest_src)}
guest_bin={quote(guest_bin)}
timeout_seconds={int(invocation.get("timeout_seconds", 600))}
ok_marker={quote(invocation["ok_marker"])}
host_trace_oracle={quote("1" if invocation.get("host_trace_oracle") else "0")}
prepare_only="${{WEST_GUEST_C_FIXTURE_PREPARE_ONLY:-0}}"
run_only="${{WEST_GUEST_C_FIXTURE_RUN_ONLY:-0}}"

# shellcheck source=darling-guest-shell.sh
source "$guest_shell_helper"

{trace_setup}
{host_stat_setup}
{server_env_restart}

dump_namespace_state() {{
\tlocal init_pid_file="$DPREFIX/.init.pid"
\tlocal init_pid=""
\tif [ -r "$init_pid_file" ]; then
\t\tinit_pid="$(tr -d '[:space:]' < "$init_pid_file" || true)"
\t\tprintf 'WEST_GUEST_NAMESPACE_INIT_PID=%s\\n' "${{init_pid:-<empty>}}" >&2
\telse
\t\tprintf 'WEST_GUEST_NAMESPACE_INIT_PID=<missing>\\n' >&2
\tfi
\tif [ -n "$init_pid" ]; then
\t\tif [ -e "/proc/$init_pid/ns/mnt" ]; then
\t\t\tprintf 'WEST_GUEST_NAMESPACE_MNT=%s\\n' "$(readlink "/proc/$init_pid/ns/mnt" 2>/dev/null || printf '<unreadable>')" >&2
\t\telse
\t\t\tprintf 'WEST_GUEST_NAMESPACE_MNT=<missing:/proc/%s/ns/mnt>\\n' "$init_pid" >&2
\t\tfi
\tfi
}}

dump_file_sha() {{
\tlocal label="$1"
\tlocal path="$2"
\tif [ -e "$path" ]; then
\t\tsha256sum "$path" 2>/dev/null | sed "s#^#WEST_GUEST_FILE_SHA256 $label #; s#  # #g" >&2 || true
\telse
\t\tprintf 'WEST_GUEST_FILE_MISSING %s %s\\n' "$label" "$path" >&2
\tfi
}}

dump_runtime_file_state() {{
\tlocal launcher_dir install_root
\tlauncher_dir="$(dirname "$launch")"
\tinstall_root="$(cd "$launcher_dir/.." && pwd -P)"
\tdump_file_sha launcher "$launch"
\tdump_file_sha launcher_server "$launcher_dir/darlingserver"
\tdump_file_sha prefix_server "$DPREFIX/bin/darlingserver"
\tdump_file_sha install_mldr "$install_root/usr/libexec/darling/mldr"
\tdump_file_sha install_nested_mldr "$install_root/libexec/darling/usr/libexec/darling/mldr"
\tdump_file_sha prefix_mldr "$DPREFIX/usr/libexec/darling/mldr"
\tdump_file_sha prefix_nested_mldr "$DPREFIX/libexec/darling/usr/libexec/darling/mldr"
\tdump_file_sha install_dyld "$install_root/usr/lib/dyld"
\tdump_file_sha install_nested_dyld "$install_root/libexec/darling/usr/lib/dyld"
\tdump_file_sha install_libsystem_kernel "$install_root/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha install_nested_libsystem_kernel "$install_root/libexec/darling/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha prefix_libsystem_kernel "$DPREFIX/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha prefix_nested_libsystem_kernel "$DPREFIX/libexec/darling/usr/lib/system/libsystem_kernel.dylib"
\tdump_file_sha prefix_dyld "$DPREFIX/usr/lib/dyld"
\tdump_file_sha prefix_nested_dyld "$DPREFIX/libexec/darling/usr/lib/dyld"
}}

dump_rpc_client_log() {{
\tlocal log=/tmp/dserver-client-rpc.log
\tif [ -s "$log" ]; then
\t\tprintf 'WEST_GUEST_RPC_CLIENT_LOG_BEGIN\\n' >&2
\t\ttail -80 "$log" >&2 || true
\t\tprintf 'WEST_GUEST_RPC_CLIENT_LOG_END\\n' >&2
\tfi
}}

dump_runtime_process_state() {{
\tlocal snapshot pid comm args exe found=0
\tsnapshot="$(mktemp /tmp/west-dserver-ps.XXXXXX)"
\tps -eo pid=,comm=,args= > "$snapshot" 2>/dev/null || true
\twhile read -r pid comm args; do
\t\tif [ "$comm" != "darlingserver" ]; then
\t\t\tcontinue
\t\tfi
\t\tcase "$args" in
\t\t\t*"$DPREFIX"*)
\t\t\t\tfound=1
\t\t\t\tprintf 'WEST_GUEST_DSERVER_PID=%s\\n' "$pid" >&2
\t\t\t\tprintf 'WEST_GUEST_DSERVER_ARGS=%s\\n' "$args" >&2
\t\t\t\texe="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
\t\t\t\tprintf 'WEST_GUEST_DSERVER_EXE=%s\\n' "${{exe:-<unreadable>}}" >&2
\t\t\t\tsha256sum "/proc/$pid/exe" 2>/dev/null | sed 's/^/WEST_GUEST_DSERVER_EXE_SHA256=/' >&2 || true
\t\t\t\t;;
\t\tesac
\tdone < "$snapshot"
\tif [ "$found" -eq 0 ]; then
\t\twhile read -r pid comm args; do
\t\t\tif [ "$comm" != "darlingserver" ]; then
\t\t\t\tcontinue
\t\t\tfi
\t\t\tprintf 'WEST_GUEST_DSERVER_OTHER_PID=%s\\n' "$pid" >&2
\t\t\tprintf 'WEST_GUEST_DSERVER_OTHER_ARGS=%s\\n' "$args" >&2
\t\t\texe="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
\t\t\tprintf 'WEST_GUEST_DSERVER_OTHER_EXE=%s\\n' "${{exe:-<unreadable>}}" >&2
\t\t\tsha256sum "/proc/$pid/exe" 2>/dev/null | sed 's/^/WEST_GUEST_DSERVER_OTHER_EXE_SHA256=/' >&2 || true
\t\tdone < "$snapshot"
\tfi
\trm -f "$snapshot"
}}

clear_stale_init_pid() {{
\tlocal init_pid_file="$DPREFIX/.init.pid"
\tlocal init_pid=""
\tif [ ! -r "$init_pid_file" ]; then
\t\treturn 0
\tfi
\tinit_pid="$(tr -d '[:space:]' < "$init_pid_file" || true)"
\tif [ -z "$init_pid" ] || [ ! -e "/proc/$init_pid/ns/mnt" ]; then
\t\trm -f "$init_pid_file"
\tfi
}}

guest_shell() {{
\tlocal seconds="$1"
\tshift
\tlocal ns_log
\tlocal restore_errexit=0
\tcase "$-" in
\t\t*e*)
\t\t\trestore_errexit=1
\t\t\tset +e
\t\t\t;;
\tesac
\tclear_stale_init_pid
\tns_log="$(mktemp /tmp/west-guest-shell-stderr.XXXXXX)"
\tdarling_guest_shell "$launch" "$DPREFIX" "$seconds" "$@" 2> "$ns_log"
\tlocal rc=$?
\tcat "$ns_log" >&2 || true
\tif [ "$rc" -ne 0 ] && grep -q 'Cannot open mnt namespace file' "$ns_log"; then
\t\tdump_namespace_state
\t\tprintf 'WEST_GUEST_STAGE=namespace-retry\\n' >&2
\t\t"$launch" shutdown >/dev/null 2>&1 || true
\t\tclear_stale_init_pid
\t\tdarling_guest_shell "$launch" "$DPREFIX" "$seconds" "$@" 2> "$ns_log"
\t\trc=$?
\t\tcat "$ns_log" >&2 || true
\t\tif [ "$rc" -ne 0 ] && grep -q 'Cannot open mnt namespace file' "$ns_log"; then
\t\t\tdump_namespace_state
\t\tfi
\tfi
\tif [ "$rc" -ne 0 ]; then
\t\tdump_runtime_file_state
\t\tdump_runtime_process_state
\t\tdump_rpc_client_log
\t\tprintf 'WEST_GUEST_SHELL_RC=%s\\n' "$rc" >&2
\tfi
\trm -f "$ns_log"
\tif [ "$restore_errexit" -eq 1 ]; then
\t\tset -e
\tfi
\treturn "$rc"
}}

cleanup_guest_artifacts() {{
\t# A prepare-only RED phase deliberately leaves the compiled fixture for the
\t# following bad-runtime run. Every other path must leave the guest /tmp clean.
\tif [ "$prepare_only" = 1 ]; then
\t\treturn
\tfi
\tguest_shell 10 "rm -f '$guest_src' '$guest_bin'" >/dev/null 2>&1 || true
}}
trap cleanup_guest_artifacts EXIT

: > /tmp/dserver-client-rpc.log 2>/dev/null || true
dump_runtime_file_state
if [ "$run_only" != 1 ]; then
\tprintf 'WEST_GUEST_STAGE=cleanup\\n' >&2
\tguest_shell 10 "rm -f '$guest_src' '$guest_bin'" >/dev/null 2>&1 || true
\t"$launch" shutdown >/dev/null 2>&1 || true
\tclear_stale_init_pid
\tprintf 'WEST_GUEST_STAGE=upload\\n' >&2
\tguest_shell 10 "cat > '$guest_src'" < "$host_src"
fi

if [ "$prepare_only" = 1 ]; then
\tset +e
\tprintf 'WEST_GUEST_STAGE=compile\\n' >&2
\tguest_shell "$timeout_seconds" {quote(guest_compile_body)} > "$verdict" 2>&1
\tcompile_rc=$?
\tset -e
\tcat "$verdict" 2>/dev/null || true
\texit "$compile_rc"
fi

if [ "$run_only" = 1 ]; then
\tset +e
\tprintf 'WEST_GUEST_STAGE=run\\n' >&2
\tguest_shell "$timeout_seconds" {quote(guest_run_body)} > "$verdict" 2>&1
\trc=$?
\tset -e
else
\t{guest_workload}
fi

{trace_settle}
cat "$verdict" 2>/dev/null || true
if [ "$rc" -ne 0 ] && [ "$host_trace_oracle" != 1 ]; then
\t{trace_dump}
\t{host_stat_dump}
\texit "$rc"
fi
if [ "$host_trace_oracle" != 1 ]; then
\tgrep -F -x -q -- "$ok_marker" "$verdict"
\tgrep -q '^ORACLE_RC=0$' "$verdict"
fi
{trace_check}
{host_stat_check}
"""
        host_runner.write_text(script)
        host_runner.chmod(0o755)
        child = dict(invocation)
        child.pop("guest_c_fixture", None)
        child.update(
            {
                "key": f"guest-c-fixture-runner:{invocation['key']}",
                "display": str(host_runner),
                "cwd": invocation["cwd"],
                "args": [str(host_runner)],
                "shell": False,
                "debug_timeout_seconds": int(invocation.get("timeout_seconds", 600)) + 15,
            }
        )
        result = run_bounded(
            command._debug_runner_args(child),
            cwd=invocation["cwd"],
            env=run_env,
            timeout_seconds=int(child["debug_timeout_seconds"]) + 15,
            capture_output=True,
        )
        output = result.stdout + result.stderr
        if output:
            sys.stdout.write(output)
        if result.timed_out:
            command.err(
                f"{invocation['name']}: guest C fixture timed out after "
                f"{invocation.get('timeout_seconds', 600)}s"
            )
            command._record_failure_phase(invocation, "run")
        elif result.returncode:
            phase = failure_phase_from_debug_bundle(output)
            if phase is not None:
                command._record_failure_phase(invocation, phase)
        return result.returncode

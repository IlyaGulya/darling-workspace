#!/bin/sh
# Shared shell entrypoint to the installed-runtime authority; guest state is separate.
darling_resolve_runtime() {
	_runtime_assignments=$(python3 -B - "$DARLING_WORKSPACE_ROOT" "$1" "${2:-}" <<'PY'
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "west_commands"))
from runtime_install import resolve_runtime

try:
    runtime = resolve_runtime(sys.argv[2] or None)
    prefix_environment = runtime.environment(sys.argv[3], base={}) if sys.argv[3] else {}
except (OSError, RuntimeError, ValueError) as error:
    print(f"installed runtime: {error}", file=sys.stderr)
    raise SystemExit(2)
for key, value in (
    ("DARLING_RUNTIME_ROOT", runtime.root),
    ("DARLING_LAUNCHER", runtime.launcher),
    ("DARLING", runtime.launcher),
    ("DARLING_SYSTEM_ROOT", runtime.system_root),
):
    print(f"{key}={shlex.quote(str(value))}")
for key, value in prefix_environment.items():
    print(f"{key}={shlex.quote(value)}")
PY
	) || return $?
	eval "$_runtime_assignments"
	export DARLING_RUNTIME_ROOT DARLING_LAUNCHER DARLING DARLING_SYSTEM_ROOT
	[ -z "${2:-}" ] || export DPREFIX DARLING_PREFIX
}

darling_prefix_owned_pids() {
	python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" \
		--prefix "$1" --list-owned
}

# Residue includes unverified markers and is never authority for kill().
darling_prefix_residual_pids() {
	python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" \
		--prefix "$1" --list-residual
}

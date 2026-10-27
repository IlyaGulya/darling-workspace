#!/bin/sh
# Prefix-scoped cleanup; external rescue never counts as production shutdown.
# Usage: prefix-cleanup.sh --prefix PATH [--runtime-root ROOT] [--settle SECONDS] [--dry-run]
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DARLING_WORKSPACE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
. "$SCRIPT_DIR/darling-runtime.sh"
PREFIX=""
RUNTIME_ROOT=""
SETTLE=3
DRY=0

parse_args() {
	while [ $# -gt 0 ]; do
		case "$1" in
			--prefix) PREFIX="$2"; shift 2 ;;
			--runtime-root) RUNTIME_ROOT="$2"; shift 2 ;;
			--settle) SETTLE="$2"; shift 2 ;;
			--dry-run) DRY=1; shift ;;
			-h|--help)
				printf 'usage: %s --prefix PATH [--runtime-root ROOT] [--settle SECONDS] [--dry-run]\n' "$0"
				exit 0 ;;
			*) echo "unknown argument: $1" >&2; exit 2 ;;
		esac
	done
}
parse_args "$@"
[ -n "$PREFIX" ] && [ -d "$PREFIX" ] || {
	echo "--prefix must name an existing absolute guest directory" >&2
	exit 2
}

if [ "$DRY" != 1 ]; then
	if [ -z "${WEST_TEST_PREFIX_LEASE:-}" ]; then
		exec python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" \
			--prefix "$PREFIX" -- "$0" "$@"
	fi
	[ "$WEST_TEST_PREFIX_LEASE" = "$PREFIX" ] || {
		echo "borrowed prefix lease does not match --prefix" >&2
		exit 2
	}
	python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" \
		--prefix "$PREFIX" --validate-lease || exit $?
	darling_resolve_runtime "$RUNTIME_ROOT" "$PREFIX" || exit $?
fi

snapshot() {
	OWNED_PIDS=$(darling_prefix_owned_pids "$PREFIX") || exit $?
	if [ -z "$OWNED_PIDS" ]; then
		OWNED_COUNT=0
	else
		OWNED_COUNT=$(printf '%s\n' "$OWNED_PIDS" | wc -l)
	fi
}

snapshot
echo "prefix: $PREFIX"
echo "prefix-owned processes before: $OWNED_COUNT"
if [ "$DRY" = 1 ]; then
	for pid in $OWNED_PIDS; do
		echo "  would stop pid=$pid exe=$(readlink "/proc/$pid/exe" 2>/dev/null)"
	done
	echo "dry run: no action taken"
	exit 0
fi

shutdown_rc=0
if [ "$OWNED_COUNT" -gt 0 ]; then
	timeout 30 "$DARLING_LAUNCHER" shutdown 8<&-
	shutdown_rc=$?
	printf 'product shutdown rc: %s\n' "$shutdown_rc"
else
	echo "product shutdown: not needed (no owned runtime)"
fi
sleep "$SETTLE"
snapshot
rescue=$OWNED_COUNT
if [ "$OWNED_COUNT" -gt 0 ]; then
	python3 -B "$DARLING_WORKSPACE_ROOT/west_commands/test_prefix.py" \
		--prefix "$PREFIX" --rescue
	rescue_rc=$?
	[ "$rescue_rc" -eq 0 ] || exit "$rescue_rc"
fi
snapshot
mount_counts=$(darling_prefix_mount_counts "$PREFIX") || exit $?
read mounts layer_mounts <<EOF
$mount_counts
EOF
echo "prefix-owned processes after: $OWNED_COUNT"
echo "mounts under prefix: $mounts (plus $layer_mounts prefix layer mount(s))"
if [ "$OWNED_COUNT" -gt 0 ] || [ "$mounts" -gt 0 ]; then
	echo "prefix is NOT clean" >&2
	exit 1
fi
residual=$(darling_prefix_residual_pids "$PREFIX") || exit $?
if [ -n "$residual" ]; then
	printf 'prefix has unverified runtime residue; not authorized to signal: %s\n' "$residual" >&2
	exit 1
fi
if [ "$shutdown_rc" -ne 0 ] || [ "$rescue" -gt 0 ]; then
	echo "prefix is clean after failed shutdown/external rescue; this is NOT a production PASS" >&2
	exit 1
fi
echo "prefix is clean"

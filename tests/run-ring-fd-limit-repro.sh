#!/usr/bin/env bash
# Build and run the reserved-descriptor limit reproducer inside a Darling
# prefix, and report its verdict.
#
#   tests/run-ring-fd-limit-repro.sh PREFIX [plain|activate] [TIMEOUT_SECONDS]
#
# Exit status: the reproducer's own verdict (0 correct, 1 defect, 2 indecisive),
# or 3 when the prefix, the source or the guest compile is at fault. The source
# transport mirrors the guest CTest fixtures: the file is written by an explicit
# guest command into the prefix-owned persistent temp directory, because
# `darling shell` does not carry stdin into the guest and the guest may have a
# fresh /tmp namespace per invocation.
#
# This is not a contract and is not part of a tier: it needs a bootstrapped
# prefix and it is a diagnostic, so run it directly.

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace="$(cd "$here/.." && pwd)"
DARLING_WORKSPACE_ROOT="$workspace"
source "$workspace/scripts/darling-runtime.sh"
source "$workspace/testkit/scripts/darling-guest-shell.sh"

prefix="${1:-}"
mode="${2:-plain}"
timeout_seconds="${3:-300}"

if [[ -z "$prefix" ]]; then
	printf 'usage: %s PREFIX [plain|activate] [TIMEOUT_SECONDS]\n' "$0" >&2
	printf 'Select an installation with DARLING_RUNTIME_ROOT; PREFIX selects writable guest state.\n' >&2
	exit 3
fi
darling_resolve_runtime "${DARLING_RUNTIME_ROOT:-}" "$prefix" || exit 3

case "$mode" in
plain) extra=() ;;
activate) extra=(-DREPRO_ACTIVATE_RING) ;;
*)
	printf 'mode must be plain or activate\n' >&2
	exit 3
	;;
esac

source_file="$here/ring_fd_limit_repro.c"
if [[ ! -r "$source_file" ]]; then
	printf 'missing reproducer source: %s\n' "$source_file" >&2
	exit 3
fi


guest_dir=/private/var/tmp
guest_src="$guest_dir/ring_fd_limit_repro.c"
guest_bin="$guest_dir/ring_fd_limit_repro"
guest_cc="/Library/Developer/CommandLineTools/usr/bin/clang"
guest_sdk="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"

source_literal="$(printf '%q' "$(< "$source_file")")"
flags_literal=""
for flag in "${extra[@]}"; do
	flags_literal+=" $(printf '%q' "$flag")"
done

darling_guest_shell "$DARLING_LAUNCHER" "$prefix" "$timeout_seconds" \
	"umask 077; printf '%s' $source_literal > $guest_src" || exit 3
darling_guest_shell "$DARLING_LAUNCHER" "$prefix" "$timeout_seconds" \
	"$guest_cc -isysroot $guest_sdk $flags_literal -o $guest_bin $guest_src" || exit 3

set +e
darling_guest_shell "$DARLING_LAUNCHER" "$prefix" "$timeout_seconds" "$guest_bin"
rc=$?
set -e
printf 'REPRO_EXIT=%s\n' "$rc"
exit "$rc"

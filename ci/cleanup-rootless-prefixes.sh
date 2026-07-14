#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
(( $# >= 2 && $# % 2 == 0 )) || {
	echo "usage: $0 KIND PREFIX [KIND PREFIX ...]" >&2
	exit 2
}
ROOTLESS_TIER_REPO="$root"
. "$root/ci/rootless-prefix.sh"

status=0
set +e
for ((index = 1; index <= $#; index += 2)); do
	kind="${!index}"
	prefix_index=$((index + 1))
	prefix="${!prefix_index}"
	[[ -n "$prefix" ]] || continue
	owner="${prefix}.west-tier-owner"
	if [[ ! -e "$prefix" && ! -L "$prefix" && ! -e "$owner" && ! -L "$owner" ]]; then
		continue
	fi
	if ! rootless_prefix_assert_owned "$kind" "$prefix"; then
		echo "refusing to clean an unowned rootless prefix: $prefix" >&2
		status=1
		continue
	fi
	west test --prefix "$prefix" --cleanup-prefix
	cleanup_rc=$?
	if (( cleanup_rc == 0 )); then
		rootless_prefix_remove "$kind" "$prefix"
		cleanup_rc=$?
	fi
	if (( cleanup_rc != 0 )); then
		echo "preserving unclean rootless prefix: $prefix" >&2
		status=1
	fi
done
west test --gc --gc-runtime-evidence
gc_rc=$?
"$root/scripts/west-job.sh" assert-no-live-west-test --state-root "${TMPDIR:-/tmp}"
jobs_rc=$?
(( gc_rc == 0 && jobs_rc == 0 )) || status=1
exit "$status"

#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

export RUNNER_TEMP="$tmp/runner"
export ROOTLESS_TIER_REPO="$repo"
mkdir -p "$tmp/bin"
cat >"$tmp/bin/west" <<'WEST'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$WEST_CONTRACT_LOG"
WEST
chmod +x "$tmp/bin/west"
export WEST_CONTRACT_LOG="$tmp/west.log"
export PATH="$tmp/bin:$PATH"

. "$repo/ci/rootless-prefix.sh"
owned="$(rootless_prefix_create smoke UNUSED_VARIABLE)"
"$repo/ci/cleanup-rootless-prefixes.sh" smoke "$owned"
[ ! -e "$owned" ]
grep -F -x -q "test --prefix $owned --cleanup-prefix" "$WEST_CONTRACT_LOG"

: >"$WEST_CONTRACT_LOG"
removed="$(rootless_prefix_create diagnostic UNUSED_VARIABLE)"
rootless_prefix_remove diagnostic "$removed"
"$repo/ci/cleanup-rootless-prefixes.sh" diagnostic "$removed"
if grep -F -q -- "test --prefix $removed --cleanup-prefix" "$WEST_CONTRACT_LOG"; then
	echo 'cleanup retried an already removed owned prefix' >&2
	exit 1
fi

: >"$WEST_CONTRACT_LOG"
if "$repo/ci/cleanup-rootless-prefixes.sh" smoke "$HOME" 2>"$tmp/unsafe.err"; then
	echo 'cleanup accepted HOME as a rootless prefix' >&2
	exit 1
fi
if grep -F -q -- "test --prefix $HOME --cleanup-prefix" "$WEST_CONTRACT_LOG"; then
	echo 'cleanup passed HOME to west before ownership validation' >&2
	exit 1
fi

printf 'PASS rootless-cleanup-contract\n'

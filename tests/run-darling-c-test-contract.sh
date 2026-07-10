#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat >"$tmp/launcher" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[ "$1" = shell ] || exit 64
shift
exec "$@"
SH
chmod +x "$tmp/launcher"

cat >"$tmp/guest.c" <<'C'
#include <stdio.h>
int main(void) {
	puts("GUEST_C_EXACT_OK");
	return 0;
}
C

name="west_guest_c_contract_${RANDOM}_$$"
mkdir -p "$tmp/prefix"
if env -u DPREFIX -u DARLING_PREFIX "$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "$name" --source "$tmp/guest.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' >"$tmp/missing-prefix.out" 2>&1; then
	cat "$tmp/missing-prefix.out" >&2
	exit 1
fi
grep -F -q 'DPREFIX or DARLING_PREFIX is unset' "$tmp/missing-prefix.out"

DPREFIX="$tmp/prefix" "$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "$name" --source "$tmp/guest.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' --ok-marker GUEST_C_EXACT_OK

if find /tmp -maxdepth 1 -name "${name}.*" -print | grep -q .; then
	find /tmp -maxdepth 1 -name "${name}.*" -print >&2
	exit 1
fi

grep -F -q 'darling_guest_shell "$launcher" "$prefix" "${DARLING_GUEST_TIMEOUT_SECONDS:-60}"' \
	"$repo/testkit/scripts/run-darling-c-test.sh"

if DPREFIX="$tmp/prefix" "$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "$name" --source "$tmp/guest.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' --ok-marker GUEST_C_EXACT_OK_EXTRA >"$tmp/bad.out" 2>&1; then
	cat "$tmp/bad.out" >&2
	exit 1
fi
grep -F -x -q GUEST_C_EXACT_OK "$tmp/bad.out" || {
	cat "$tmp/bad.out" >&2
	exit 1
}

if find /tmp -maxdepth 1 -name "${name}.*" -print | grep -q .; then
	find /tmp -maxdepth 1 -name "${name}.*" -print >&2
	exit 1
fi

printf 'PASS darling-c-test-contract\n'

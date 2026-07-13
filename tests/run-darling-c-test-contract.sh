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

cat >"$tmp/no-stdin-launcher" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[ "$1" = shell ] || exit 64
shift
exec "$@" </dev/null
SH
chmod +x "$tmp/no-stdin-launcher"

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
	--cc cc --cflags '' --ok-marker GUEST_C_EXACT_OK >"$tmp/green.out" 2>&1
grep -F -x -q WEST_GUEST_STAGE=compile "$tmp/green.out"
grep -F -x -q WEST_GUEST_STAGE=run "$tmp/green.out"
grep -F -x -q ORACLE_RC=0 "$tmp/green.out"
grep -F -x -q GUEST_C_EXACT_OK "$tmp/green.out"

# A real Darling launcher does not promise to preserve stdin for `shell -c`.
# The fixture must therefore remain green when the transport drops stdin.
DPREFIX="$tmp/prefix" "$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "${name}_no_stdin" --source "$tmp/guest.c" \
	--launcher "$tmp/no-stdin-launcher" --cc cc --cflags '' \
	--ok-marker GUEST_C_EXACT_OK >"$tmp/no-stdin.out" 2>&1
grep -F -x -q WEST_GUEST_STAGE=upload "$tmp/no-stdin.out"
grep -F -x -q WEST_GUEST_STAGE=compile "$tmp/no-stdin.out"
grep -F -x -q WEST_GUEST_STAGE=run "$tmp/no-stdin.out"
grep -F -x -q GUEST_C_EXACT_OK "$tmp/no-stdin.out"

if find /tmp -maxdepth 1 -name "${name}.*" -print | grep -q .; then
	find /tmp -maxdepth 1 -name "${name}.*" -print >&2
	exit 1
fi
if find /tmp -maxdepth 1 -name "west-ctest-guest-c.${name}.*" -print | grep -q .; then
	find /tmp -maxdepth 1 -name "west-ctest-guest-c.${name}.*" -print >&2
	exit 1
fi

cat >"$tmp/compile-fail.c" <<'C'
int main(void) { return this_does_not_compile; }
C
printf 'darlingserver pid=42 exec-mldr\n' >"$tmp/rootless-boot.trace"
printf 'launchd pid=43 runtime-loop\n' >"$tmp/prefix/.west-rootless-guest-fd.log"
mkdir -p "$tmp/prefix/private/var/tmp"
printf 'launchd pid=43 runtime-loop\n' >"$tmp/prefix/private/var/tmp/.west-rootless-boot.log"
if DARLING_HOST_BOOT_TRACE="$tmp/rootless-boot.trace" DPREFIX="$tmp/prefix" \
	"$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "${name}_compile_fail" --source "$tmp/compile-fail.c" \
	--launcher "$tmp/launcher" --cc cc --cflags '' >"$tmp/compile-fail.out" 2>&1; then
	cat "$tmp/compile-fail.out" >&2
	exit 1
fi
grep -F -x -q WEST_GUEST_STAGE=compile "$tmp/compile-fail.out"
grep -E -q '^ORACLE_RC=[1-9][0-9]*$' "$tmp/compile-fail.out"
grep -F -q 'WEST_GUEST_FILE_SHA256 launcher ' "$tmp/compile-fail.out"
grep -F -q 'WEST_GUEST_FILE_MISSING prefix_libsystem_kernel ' "$tmp/compile-fail.out"
grep -F -q -- '--- rootless host boot trace: ' "$tmp/compile-fail.out"
grep -F -x -q 'darlingserver pid=42 exec-mldr' "$tmp/compile-fail.out"
grep -F -q -- '--- rootless guest boot trace: ' "$tmp/compile-fail.out"
grep -F -x -q 'launchd pid=43 runtime-loop' "$tmp/compile-fail.out"
grep -F -q -- '--- rootless guest FD trace: ' "$tmp/compile-fail.out"
if grep -F -x -q WEST_GUEST_STAGE=run "$tmp/compile-fail.out"; then
	cat "$tmp/compile-fail.out" >&2
	exit 1
fi

cat >"$tmp/hang.c" <<'C'
#include <unistd.h>
int main(void) {
	sleep(30);
	return 0;
}
C
if DARLING_GUEST_TIMEOUT_SECONDS=1 DPREFIX="$tmp/prefix" \
	"$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "${name}_timeout" --source "$tmp/hang.c" \
	--launcher "$tmp/launcher" --cc cc --cflags '' >"$tmp/timeout.out" 2>&1; then
	cat "$tmp/timeout.out" >&2
	exit 1
fi
grep -F -x -q WEST_GUEST_STAGE=run "$tmp/timeout.out"
grep -F -q 'WEST_GUEST_FILE_SHA256 launcher ' "$tmp/timeout.out"

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

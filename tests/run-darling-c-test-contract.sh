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
command="$4"
command="${command//\/private\/var\/tmp/$DPREFIX\/private\/var\/tmp}"
# Record the exact guest command line so the contract can inspect what the
# runner would compile with. Host-visible, outside the guest's fd 2.
printf '%s\n' "$command" >>"$DPREFIX/.west-ctest-contract-commands.log"
"$1" "$2" "$3" "$command"
rc=$?
# Model a real launcher namespace: guest /tmp is not persistent between shell
# invocations, while the prefix-owned private/var/tmp directory is.
exit "$rc"
SH
chmod +x "$tmp/launcher"

cat >"$tmp/no-stdin-launcher" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[ "$1" = shell ] || exit 64
shift
command="$4"
command="${command//\/private\/var\/tmp/$DPREFIX\/private\/var\/tmp}"
"$1" "$2" "$3" "$command" </dev/null
rc=$?
exit "$rc"
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
mkdir -p "$tmp/prefix/private/var/tmp"
commands_log="$tmp/prefix/.west-ctest-contract-commands.log"
if env -u DPREFIX -u DARLING_PREFIX "$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "$name" --source "$tmp/guest.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' >"$tmp/missing-prefix.out" 2>&1; then
	cat "$tmp/missing-prefix.out" >&2
	exit 1
fi
grep -F -q 'DPREFIX or DARLING_PREFIX is unset' "$tmp/missing-prefix.out"

# Flag absent: the compile line must carry only the source, exactly as before.
green_rid="ctest-noflag-${RANDOM}"
: >"$commands_log"
DPREFIX="$tmp/prefix" WEST_GUEST_C_FIXTURE_ID="$green_rid" \
	"$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "$name" --source "$tmp/guest.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' --ok-marker GUEST_C_EXACT_OK >"$tmp/green.out" 2>&1
grep -F -x -q WEST_GUEST_STAGE=compile "$tmp/green.out"
grep -F -x -q WEST_GUEST_STAGE=run "$tmp/green.out"
grep -F -x -q ORACLE_RC=0 "$tmp/green.out"
grep -F -x -q GUEST_C_EXACT_OK "$tmp/green.out"
green_compile="$(grep -m1 -E -- "-o '" "$commands_log" || true)"
if [[ -z "$green_compile" ]]; then
	printf 'no compile command captured for the no-flag case\n' >&2
	cat "$commands_log" >&2
	exit 1
fi
if [[ "$green_compile" != *"'$tmp/prefix/private/var/tmp/${name}.${green_rid}.c'; rc="* ]]; then
	printf 'no-flag compile line does not match the source-only form: %s\n' "$green_compile" >&2
	exit 1
fi

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

if compgen -G "/tmp/${name}.*" >/dev/null; then
	compgen -G "/tmp/${name}.*" >&2
	exit 1
fi
if compgen -G "$tmp/prefix/private/var/tmp/${name}.*" >/dev/null; then
	compgen -G "$tmp/prefix/private/var/tmp/${name}.*" >&2
	exit 1
fi
if compgen -G "/tmp/west-ctest-guest-c.${name}.*" >/dev/null; then
	compgen -G "/tmp/west-ctest-guest-c.${name}.*" >&2
	exit 1
fi

# Declared extra link inputs must be staged in the guest and appended to the
# compile line after the source, in declaration order. The fixture calls a
# symbol only each input provides, so a green run proves they were really
# linked; the captured command proves they reached the compile line.
cat >"$tmp/guest_extra.c" <<'C'
int guest_extra_answer(void) { return 42; }
const char guest_extra_filler[120000] = { 1 };
C
cat >"$tmp/guest_extra_second.c" <<'C'
int guest_extra_second(void) { return 7; }
C
cat >"$tmp/link-fixture.c" <<'C'
#include <stdio.h>
int guest_extra_answer(void);
int guest_extra_second(void);
int main(void) {
	int answer = guest_extra_answer();
	int second = guest_extra_second();
	printf("GUEST_C_EXTRA_OK %d %d\n", answer, second);
	return answer == 42 && second == 7 ? 0 : 1;
}
C
cc -c -o "$tmp/guest_extra.o" "$tmp/guest_extra.c"
ar rcs "$tmp/libguest_extra.a" "$tmp/guest_extra.o"
cc -c -o "$tmp/guest_extra_second.o" "$tmp/guest_extra_second.c"

link_name="${name}_link"
link_rid="ctest-link-${RANDOM}"
link_guest_archive="$tmp/prefix/private/var/tmp/${link_name}.${link_rid}.0.libguest_extra.a"
link_guest_object="$tmp/prefix/private/var/tmp/${link_name}.${link_rid}.1.guest_extra_second.o"
: >"$commands_log"
DPREFIX="$tmp/prefix" WEST_GUEST_C_FIXTURE_ID="$link_rid" \
	"$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "$link_name" --source "$tmp/link-fixture.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' --link-file "$tmp/libguest_extra.a" \
	--link-file "$tmp/guest_extra_second.o" \
	--ok-marker 'GUEST_C_EXTRA_OK 42 7' >"$tmp/link.out" 2>&1
grep -F -x -q WEST_GUEST_STAGE=upload "$tmp/link.out"
grep -F -x -q WEST_GUEST_STAGE=compile "$tmp/link.out"
grep -F -x -q WEST_GUEST_STAGE=run "$tmp/link.out"
grep -F -x -q ORACLE_RC=0 "$tmp/link.out"
grep -F -x -q 'GUEST_C_EXTRA_OK 42 7' "$tmp/link.out"
grep -F -q 'WEST_GUEST_FILE_SHA256 link_file_0 ' "$tmp/link.out"
grep -F -q 'WEST_GUEST_FILE_SHA256 link_file_1 ' "$tmp/link.out"
link_compile="$(grep -m1 -E -- "-o '" "$commands_log" || true)"
if [[ "$link_compile" != *"'$tmp/prefix/private/var/tmp/${link_name}.${link_rid}.c' '$link_guest_archive' '$link_guest_object'"* ]]; then
	printf 'link inputs are not appended after the source in order: %s\n' "$link_compile" >&2
	exit 1
fi
# The inputs must be staged by a host-side copy into the prefix tree -- which is host-visible -- rather than
# transferred through the guest, because the real guest has NO base64 decoder at all (measured:
# WEST_GUEST_NO_BASE64_DECODER and no base64/uudecode in the prefix's usr/bin, a fact a fake launcher cannot
# reveal). Byte-identity is asserted rather than the shape of the transfer: a copy that silently truncated would
# otherwise still link and pass.
cmp -s "$tmp/libguest_extra.a" "$link_guest_archive" || {
	printf 'staged archive is not byte-identical to its source\n' >&2
	exit 1
}
cmp -s "$tmp/guest_extra_second.o" "$link_guest_object" || {
	printf 'staged object is not byte-identical to its source\n' >&2
	exit 1
}
if grep -F -q -- "base64" "$commands_log"; then
	printf 'staging still asks the guest for a decoder: %s\n' "$(grep -F -- base64 "$commands_log" | head -1)" >&2
	exit 1
fi

# A declared link input that does not exist must fail on the host, naming the
# path, before any guest command runs.
: >"$commands_log"
if DPREFIX="$tmp/prefix" "$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "${name}_missing" --source "$tmp/guest.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' --link-file "$tmp/no-such-archive.a" \
	>"$tmp/missing-link.out" 2>&1; then
	cat "$tmp/missing-link.out" >&2
	exit 1
fi
grep -F -q "link file not found: $tmp/no-such-archive.a" "$tmp/missing-link.out"
if grep -F -q 'WEST_GUEST_STAGE=' "$tmp/missing-link.out"; then
	cat "$tmp/missing-link.out" >&2
	exit 1
fi
if [[ -s "$commands_log" ]]; then
	printf 'a missing link file still reached the guest:\n' >&2
	cat "$commands_log" >&2
	exit 1
fi

# A present archive that still does not resolve the symbol must fail at the
# compile stage, not later at run time.
cat >"$tmp/unresolved.c" <<'C'
int guest_extra_absent(void);
int main(void) { return guest_extra_absent(); }
C
if DPREFIX="$tmp/prefix" WEST_GUEST_C_FIXTURE_ID="ctest-unresolved-${RANDOM}" \
	"$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "${name}_unresolved" --source "$tmp/unresolved.c" \
	--launcher "$tmp/launcher" --cc cc --cflags '' \
	--link-file "$tmp/libguest_extra.a" >"$tmp/unresolved.out" 2>&1; then
	cat "$tmp/unresolved.out" >&2
	exit 1
fi
grep -F -x -q WEST_GUEST_STAGE=compile "$tmp/unresolved.out"
grep -F -x -q WEST_TEST_FAILURE_PHASE=compile "$tmp/unresolved.out"
grep -E -q '^ORACLE_RC=[1-9][0-9]*$' "$tmp/unresolved.out"
if grep -F -x -q WEST_GUEST_STAGE=run "$tmp/unresolved.out"; then
	cat "$tmp/unresolved.out" >&2
	exit 1
fi

if compgen -G "$tmp/prefix/private/var/tmp/${link_name}.*" >/dev/null; then
	compgen -G "$tmp/prefix/private/var/tmp/${link_name}.*" >&2
	exit 1
fi
if compgen -G "$tmp/prefix/private/var/tmp/${name}_unresolved.*" >/dev/null; then
	compgen -G "$tmp/prefix/private/var/tmp/${name}_unresolved.*" >&2
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
grep -F -x -q WEST_TEST_FAILURE_PHASE=compile "$tmp/compile-fail.out"
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
grep -F -x -q WEST_TEST_FAILURE_PHASE=timeout "$tmp/timeout.out"
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
grep -F -x -q WEST_TEST_FAILURE_PHASE=run "$tmp/bad.out"

if compgen -G "/tmp/${name}.*" >/dev/null; then
	compgen -G "/tmp/${name}.*" >&2
	exit 1
fi
if compgen -G "$tmp/prefix/private/var/tmp/${name}.*" >/dev/null; then
	compgen -G "$tmp/prefix/private/var/tmp/${name}.*" >&2
	exit 1
fi

if [[ "${WEST_GUEST_C_CONTRACT_CHILD:-}" != 1 ]]; then
	parallel_a="$tmp/parallel-a.out"
	parallel_b="$tmp/parallel-b.out"
	set +e
	WEST_GUEST_C_CONTRACT_CHILD=1 "$0" >"$parallel_a" 2>&1 &
	parallel_a_pid=$!
	WEST_GUEST_C_CONTRACT_CHILD=1 "$0" >"$parallel_b" 2>&1 &
	parallel_b_pid=$!
	wait "$parallel_a_pid"
	parallel_a_rc=$?
	wait "$parallel_b_pid"
	parallel_b_rc=$?
	set -e
	if (( parallel_a_rc != 0 || parallel_b_rc != 0 )); then
		cat "$parallel_a" "$parallel_b" >&2
		exit 1
	fi
fi

printf 'compile(no --link-file): %s\n' "$green_compile"
printf 'compile(with --link-file): %s\n' "$link_compile"
printf 'PASS darling-c-test-contract\n'

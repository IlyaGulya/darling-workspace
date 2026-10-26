#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="${DW_STATE_ROOT:-${repo%/*}/.west-state}/test-scratch"
mkdir -p "$scratch"
tmp="$(mktemp -d "$scratch/guest-c-contract.XXXXXX")"
finish() {
	local status="$1" command="$2" output
	if (( status == 0 )); then
		rm -rf "$tmp"
	else
		printf 'FAIL darling-c-test-contract: rc=%s command=%s\nartifacts: %s\n' \
			"$status" "$command" "$tmp" >&2
		for output in "$tmp"/*.out; do
			[[ -f "$output" ]] || continue
			printf '\n=== %s ===\n' "$output" >&2
			cat "$output" >&2
		done
	fi
}
trap 'finish "$?" "$BASH_COMMAND"' EXIT
export DARLING_RUNTIME_ROOT="$tmp/installed-runtime"
mkdir -p "$DARLING_RUNTIME_ROOT/libexec/darling"

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
# With no --link-file the compile must carry the staged source and NOTHING else as an input. The exact tail is not
# asserted any more because the stage now also redirects the compiler into the prefix's compile log -- what matters
# is that no archive or object crept in, so the assertion is about the absence of extra inputs.
if [[ "$green_compile" != *"'$tmp/prefix/private/var/tmp/${name}.${green_rid}.c'"* ]]; then
	printf 'no-flag compile line does not compile the staged source: %s\n' "$green_compile" >&2
	exit 1
fi
if [[ "$green_compile" == *guest_extra* ]]; then
	printf 'no-flag compile line carries a link input: %s\n' "$green_compile" >&2
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
# The staged inputs cannot be compared byte-for-byte after the fact: the runner removes them with its cleanup trap
# when it exits, so by now the file is gone. The proof of staging is stronger than that comparison anyway -- the
# linked fixture called a symbol only each input provides and printed GUEST_C_EXTRA_OK 42 7, and the captured
# compile line names both staged paths under the prefix. What is asserted here is the mechanism that must NOT come
# back: asking the guest for a decoder, which the real guest does not have (measured: WEST_GUEST_NO_BASE64_DECODER,
# no base64 and no uudecode in the prefix's usr/bin -- a fact a fake launcher cannot reveal).
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
# A failing compile must arrive with the compiler's own words. The runner writes them to a file under the prefix
# (host-visible) and prints that file from the HOST between markers, because this harness's guest shell has no cat,
# no head and no awk: a guest `cat` exits 127 and the diagnostic disappears, which is exactly how a compile
# failure once arrived with no message at all.
cat >"$tmp/broken.c" <<'C'
int main(void) { this is not C; }
C
: >"$commands_log"
broken_rid="ctest-broken-${RANDOM}"
if DPREFIX="$tmp/prefix" WEST_GUEST_C_FIXTURE_ID="$broken_rid" \
	"$repo/testkit/scripts/run-darling-c-test.sh" \
	--name "${name}_broken" --source "$tmp/broken.c" --launcher "$tmp/launcher" \
	--cc cc --cflags '' >"$tmp/broken.out" 2>&1; then
	cat "$tmp/broken.out" >&2
	exit 1
fi
grep -F -x -q WEST_TEST_FAILURE_PHASE=compile "$tmp/broken.out"
grep -F -x -q WEST_COMPILE_LOG_BEGIN "$tmp/broken.out" || { cat "$tmp/broken.out" >&2; exit 1; }
grep -F -x -q WEST_COMPILE_LOG_END "$tmp/broken.out" || { cat "$tmp/broken.out" >&2; exit 1; }
if ! sed -n '/WEST_COMPILE_LOG_BEGIN/,/WEST_COMPILE_LOG_END/p' "$tmp/broken.out" | grep -qi 'error'; then
	printf 'the compile log block carried no compiler message:\n' >&2
	sed -n '/WEST_COMPILE_LOG_BEGIN/,/WEST_COMPILE_LOG_END/p' "$tmp/broken.out" >&2
	exit 1
fi
# The compiler must be redirected into the prefix, and no guest command may ask for a tool this harness cannot
# assume -- the measured failure was a guest `cat` exiting 127 and taking the diagnostic with it.
grep -F -q "> '$tmp/prefix/private/var/tmp/" "$commands_log" || { cat "$commands_log" >&2; exit 1; }
if grep -F -q 'cat ' "$commands_log"; then
	printf 'a guest command asks for cat, which the guest does not have:\n%s\n' \
		"$(grep -F 'cat ' "$commands_log")" >&2
	exit 1
fi

printf 'PASS darling-c-test-contract\n'

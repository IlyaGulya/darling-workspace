#!/usr/bin/env bash
#
# run-relay-topology-proof.sh -- build the relay topology falsification harness
# and run it on the host and inside ordinary Docker.
#
# The harness (relay-topology-proof.c) proves or breaks the relay-based
# descriptor architecture:
#   guest descriptors stay native and direct, every loader/transport object
#   lives in a companion process created with CLONE_VM but WITHOUT CLONE_FILES,
#   the guest parks on a futex, the relay coalesces cold wakes into ONE eventfd
#   that the server's epoll waits on, and the hot path is shared-memory only.
#
# It prints one line per assertion (A1..A7) in each environment, then this
# runner prints RELAY_TOPOLOGY_OK.  Any failure exits non-zero.
#
# The harness must be able to FAIL: after the clean runs this script builds two
# deliberate mutations of the premise in its temporary directory (never in the
# repository) and requires the expected assertions to fail:
#   1. relay created WITH CLONE_FILES        -> A1 and A2 must fail
#   2. futex word sampled at the last moment -> A4 must fail
#
# Usage: tests/relay_topology_proof/run-relay-topology-proof.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/relay-topology-proof.c"
work="$(mktemp -d /tmp/relay-topology-proof.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector)
bin="$work/relay-topology-proof"
image="${RELAY_PROOF_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${RELAY_PROOF_TIMEOUT:-900}"

note() { printf 'INFO %s\n' "$*"; }
fail() {
	printf 'INFO FAIL %s\n' "$*" >&2
	exit 1
}

[ -r "$src" ] || fail "missing harness source: $src"

# --------------------------------------------------------------------------
# 1. compile on the host (static, so the same artifact runs in the container)
# --------------------------------------------------------------------------
note "compile: $cc ${cflags[*]} -o $bin $src"
"$cc" "${cflags[@]}" -o "$bin" "$src"

if readelf -lW "$bin" | grep -q 'INTERP'; then
	fail "binary carries PT_INTERP; it must be static"
fi
note "static: program headers contain no PT_INTERP"

calls="$(objdump -d --disassemble=relay_entry "$bin" | grep -cE '\bcall' || true)"
[ "$calls" = "0" ] || fail "relay_entry contains $calls call instruction(s)"
note "relay_entry: 0 call instructions (raw syscalls only, no libc)"
note "sha256: $(sha256sum "$bin" | awk '{print $1}')"

# --------------------------------------------------------------------------
# 2. A3's syscall-count evidence on the host: strace -f -c over an otherwise
#    identical hot phase, at N=1000000 and at N=0.  The setup/teardown syscalls
#    cancel and the difference is the hot path's syscall count.
# --------------------------------------------------------------------------
total_calls() {
	# strace -c summary: columns are
	#   %time seconds usecs/call calls [errors] syscall
	awk '$NF == "total" { if (NF >= 6) print $(NF - 2); else print $(NF - 1) }' "$1"
}

note "measuring the hot phase under: strace -f -c -o <file> $bin hot N"
strace -f -c -o "$work/host_hot_n.txt" "$bin" hot 1000000 >/dev/null 2>&1
strace -f -c -o "$work/host_hot_z.txt" "$bin" hot 0 >/dev/null 2>&1
host_n="$(total_calls "$work/host_hot_n.txt")"
host_z="$(total_calls "$work/host_hot_z.txt")"
host_delta="$((host_n - host_z))"
note "strace -f -c: N=1000000 -> $host_n syscalls, N=0 -> $host_z syscalls, delta=$host_delta"
[ "$host_delta" = "0" ] || fail "hot phase issued $host_delta syscall(s) beyond setup"

# --------------------------------------------------------------------------
# 3. run the assertions on the host
# --------------------------------------------------------------------------
run_leg() {
	local label="$1"
	shift
	local log="$work/$label.log"
	local rc=0 i

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e
	cat "$log"

	[ "$rc" = "0" ] || fail "$label: harness exited $rc"
	for i in 1 2 3 4 5 6 7; do
		if ! grep -qE "^A$i PASS " "$log"; then
			fail "$label: assertion A$i did not pass"
		fi
	done
	note "$label: A1..A7 all PASS"
}

run_leg host env RELAY_PROOF_STRACE_DELTA="$host_delta" "$bin" all

# --------------------------------------------------------------------------
# 4. run the same binary inside ordinary Docker.  The container image is
#    present locally (no network pull); the statically linked binary and the
#    host's strace (plus its shared libraries) are mounted read-only so the
#    same strace -f -c mechanism is available inside the container.
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"

docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private -v "$work:/proof:ro")
if command -v strace >/dev/null 2>&1; then
	strace_bin="$(command -v strace)"
	docker_args+=(-v "$strace_bin:$strace_bin:ro")
	while read -r lib; do
		case "$lib" in
		/*) docker_args+=(-v "$lib:$lib:ro") ;;
		esac
	done < <(ldd "$strace_bin" | awk '/=>/ { print $3 }')
else
	note "strace not found on the host; the container leg cannot measure A3"
fi

note "docker: docker ${docker_args[*]} $image /proof/relay-topology-proof all"
run_leg docker docker "${docker_args[@]}" "$image" /proof/relay-topology-proof all

if ! grep -qE '^A3 PASS .*delta=0' "$work/docker.log"; then
	fail "docker: A3 was not established with a zero syscall delta in the container"
fi
if ! grep -qE '^A2 PASS .*close_range' "$work/docker.log"; then
	fail "docker: A2 did not exercise close_range in the container"
fi

# --------------------------------------------------------------------------
# 5. the harness must be able to fail: two deliberate mutations of the premise,
#    built only in the temporary directory, with the expected assertions
#    failing in both environments.
# --------------------------------------------------------------------------
mut_clone_files="$work/mut_clone_files.c"
mut_late_arm="$work/mut_late_arm.c"

sed 's@^#define CLONE_VM_ONLY 0x00000100UL.*@#define CLONE_VM_ONLY (0x00000100UL | 0x00000400UL) /* MUTATED: adds CLONE_FILES */@' \
	"$src" >"$mut_clone_files"
grep -q '0x00000400UL' "$mut_clone_files" ||
	fail "mutation 1 (CLONE_FILES) did not apply to the source"

sed -e 's@/\*MUT2-ARM\*/ epoch = ARM_EPOCH(s);@epoch = 0; (void)epoch; /* MUTATED: sampled late */@' \
	-e 's@(long)epoch@(long)ARM_EPOCH(s)@g' \
	"$src" >"$mut_late_arm"
grep -q 'MUTATED: sampled late' "$mut_late_arm" ||
	fail "mutation 2 (late epoch sample) did not apply to the source"
if grep -q '(long)epoch' "$mut_late_arm"; then
	fail "mutation 2 left an early epoch sample behind"
fi

"$cc" "${cflags[@]}" -o "$work/mut_clone_files" "$mut_clone_files"
"$cc" "${cflags[@]}" -o "$work/mut_late_arm" "$mut_late_arm"

expect_failure() {
	local label="$1"
	shift
	local wants=()
	while [ "$1" != "--" ]; do
		wants+=("$1")
		shift
	done
	shift

	local log="$work/$label.log"
	local rc=0 want

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e
	printf 'MUTATION %s: exit=%s\n' "$label" "$rc"
	sed 's/^/MUTATION /' "$log" || true

	[ "$rc" != "0" ] || fail "$label: mutated harness still exited 0"
	for want in "${wants[@]}"; do
		if ! grep -qE "^$want FAIL " "$log"; then
			fail "$label: expected $want to fail"
		fi
	done
	note "$label: ${wants[*]} failed as designed"
}

expect_failure mut-clone-files A1 A2 -- "$work/mut_clone_files" all
expect_failure mut-late-arm A4 -- "$work/mut_late_arm" all
expect_failure docker-mut-clone-files A1 A2 -- \
	docker "${docker_args[@]}" "$image" /proof/mut_clone_files all
expect_failure docker-mut-late-arm A4 -- \
	docker "${docker_args[@]}" "$image" /proof/mut_late_arm all

printf 'RELAY_TOPOLOGY_OK\n'

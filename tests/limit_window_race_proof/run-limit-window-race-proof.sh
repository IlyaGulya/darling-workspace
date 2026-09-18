#!/usr/bin/env bash
#
# run-limit-window-race-proof.sh -- build the RLIMIT_NOFILE window harness and
# run it on the host and inside ordinary Docker.
#
# The harness (limit-window-race-proof.c) proves or breaks the drafted
# "private allocation window" in source-fixes/ring-fd-ownership's mldr.c: the
# loader temporarily RAISES the process-wide soft descriptor limit to the hard
# limit around every private allocation, publishes a clamped LOW value to the
# guest, and its guard predicate tests bitmap membership only.
#
# It prints one line per claim (R1..R6, S1..S6) in each environment; this runner
# prints LIMIT_WINDOW_RACE_OK.  Any failure exits non-zero.
#
#   R1 MUST PASS  the window exists: thread B's open() during the window
#                 returns a descriptor, the soft limit B reads is the HARD
#                 limit, and the published value stayed the LOW one.
#   R2 MUST PASS  dup2() to an explicit target above the published limit
#                 succeeds inside the window and the same call fails outside.
#   R3 MUST PASS  fcntl(F_DUPFD_CLOEXEC) with an explicit min above the
#                 published limit does the same.
#   R4 MUST PASS  open/socket/pipe/dup (lowest free number, no explicit target)
#                 stay below the published limit in BOTH windows.
#   R5 MUST PASS  a descriptor obtained inside the window is NOT reported
#                 internal by the draft's guard predicate, the loader's own
#                 allocator cannot take that number while the guest holds it,
#                 and takes it as an internal descriptor once the guest closes
#                 it -- the number crosses ownership domains unrecorded.
#   R6 MUST PASS  an unrelated thread reads the raw soft limit at the HARD value
#                 inside the window while the published value stays low.
#   S1..S6 MUST PASS  the lowered-soft-limit semantics a truthful-limit design
#                 must preserve (see the harness header).
#
# The harness must be able to FAIL: after the clean runs this runner builds four
# deliberate mutations of the model (only in its temporary directory, never in
# the repository) and requires the named claims to go red:
#   M1 the temporary raise is removed, the private allocation happens at the
#      published limit as the deployed loader does  -> R2, R3 and R6 red
#      (this is the negative control the design's claim rests on; it runs in
#      both environments)
#   M2 the guard protects the reserved band          -> R5 red
#   M3 the window never closes (no restore)          -> R2, R3 red
#   M4 the guest-visible query follows the raw limit -> R1, R6 red
# Each mutation is a single marked #define or statement; the runner refuses to
# continue if the marker did not apply, if the mutated binary is identical to
# the clean one, or if the mutant's critical section still calls setrlimit.
#
# Usage: tests/limit_window_race_proof/run-limit-window-race-proof.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/limit-window-race-proof.c"
work="$(mktemp -d /tmp/limit-window-race-proof.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector -pthread)
bin="$work/limit-window-race-proof"
image="${LWR_PROOF_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${LWR_PROOF_TIMEOUT:-300}"

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
note "sha256: $(sha256sum "$bin" | awk '{print $1}')"

# symbol under a name pattern, then its disassembly: -O2 constant-propagates
# clones (e.g. socket_bitmap_adopt_locked.constprop.0)
region() {  # $1 = binary, $2 = symbol regex
	local sym
	sym="$(nm --defined-only "$1" | awk -v p="$2" '$NF ~ p {print $NF; exit}')"
	[ -n "$sym" ] || return 1
	objdump -d --disassemble="$sym" "$1"
}

# the raise and the restore are the syscalls under test, and they must be in the
# model's critical section, not somewhere else
for fn in '^model_private_fd_allocation_begin$' '^model_private_fd_allocation_end$'; do
	calls="$(region "$bin" "$fn" | grep -cE 'call.*__setrlimit' || true)"
	[ "$calls" = "1" ] ||
		fail "$fn contains $calls setrlimit call(s); the model must perform exactly one"
	note "$fn: exactly 1 setrlimit call"
done

# the private allocation uses the draft's own primitive (F_DUPFD_CLOEXEC)
adopt="$(region "$bin" '^socket_bitmap_adopt_locked' | grep -cE 'call.*fcntl' || true)"
[ "$adopt" -ge 1 ] ||
	fail "socket_bitmap_adopt_locked contains no fcntl call; the draft's allocation primitive is not modeled"
note "socket_bitmap_adopt_locked: $adopt fcntl call(s) (the draft's F_DUPFD_CLOEXEC primitive)"

# the window is barrier-synchronized, not a race
barriers="$(region "$bin" '^loader_thread$' | grep -cE 'call.*pthread_barrier_wait' || true)"
[ "$barriers" -ge 2 ] ||
	fail "loader_thread waits on $barriers barrier(s); the window must be created deterministically"
note "loader_thread: $barriers pthread_barrier_wait calls (deterministic window)"

# --------------------------------------------------------------------------
# 2. run every claim
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
	for i in R1 R2 R3 R4 R5 R6 S1 S2 S3 S4 S5 S6; do
		if ! grep -qE "^$i PASS " "$log"; then
			fail "$label: claim $i did not pass"
		fi
	done
	grep -q '^HARNESS OK ' "$log" ||
		fail "$label: the harness did not print its own OK marker"
	# both environments must measure the same regime
	grep -q 'reserved band=\[4096,8192) mid=6144 top=8191' "$log" ||
		fail "$label: the harness did not measure the expected regime"
	grep -qE '^R4 PASS .*below the published limit in both windows' "$log" ||
		fail "$label: R4 did not measure the lowest-free vectors"
	grep -qE '^R5 PASS .*as soon as the guest closed it' "$log" ||
		fail "$label: R5 did not measure the number crossing ownership domains"
	grep -qE '^S4 PASS .*REQUIRES the real soft to differ' "$log" ||
		fail "$label: S4 did not state the condition the private allocation needs"
	note "$label: R1..R6, S1..S6 all PASS"
}

run_leg host env LWR_ENV=host "$bin" all

# --------------------------------------------------------------------------
# 3. the same binary inside ordinary Docker: UID 1000, every capability
#    dropped, private IPC namespace, default seccomp/AppArmor.  The harness
#    creates its own scratch file under /tmp inside the container, so the
#    read-only mount of the build directory is enough.
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"

docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private -v "$work:/proof:ro")

note "docker: docker ${docker_args[*]} $image /proof/limit-window-race-proof all"
run_leg docker docker "${docker_args[@]}" "$image" env LWR_ENV=docker /proof/limit-window-race-proof all

grep -qE '^R1 PASS .*inside the window real soft=8192 \(hard=8192\) published=4096' "$work/docker.log" ||
	fail "docker: R1 did not observe the raised soft limit with the low published value"

# --------------------------------------------------------------------------
# 4. the harness must be able to fail.
#
# Four deliberate mutations of the model, built in this temporary directory and
# never in the repository.  Each one flips a single marked line; the runner
# verifies that the marker applied, that the binary changed, and that the
# expected claim lines go red.
# --------------------------------------------------------------------------
mutate() {  # $1 = name, $2 = marker that must disappear, $3 = sed expression
	local name="$1" marker="$2" expr="$3"
	local out="$work/mut_$name.c"

	sed "$expr" "$src" >"$out"
	if grep -q -- "$marker" "$out"; then
		fail "mutation $name did not apply ($marker still present)"
	fi
	"$cc" "${cflags[@]}" -o "$work/mut_$name" "$out"
	if [ "$(sha256sum "$work/mut_$name" | cut -d' ' -f1)" = \
	     "$(sha256sum "$bin" | cut -d' ' -f1)" ]; then
		fail "mutation $name produced an identical binary"
	fi
}

# M1: the temporary raise is removed; the private allocation happens at the
# published limit, as the deployed loader does (top-down from rlim_cur - 1).
mutate raise-removed '#define MODEL_RAISE_SOFT_LIMIT 1' \
	's@#define MODEL_RAISE_SOFT_LIMIT 1@#define MODEL_RAISE_SOFT_LIMIT 0 /* MUTATED: no temporary raise */@'
# the mutant's critical section must no longer raise the limit at all
m1_calls="$(region "$work/mut_raise-removed" '^model_private_fd_allocation_begin$' |
	grep -cE 'call.*__setrlimit' || true)"
[ "$m1_calls" = "0" ] ||
	fail "the raise-removed mutant still calls setrlimit $m1_calls time(s) in its critical section"
note "raise-removed: the critical section no longer calls setrlimit"

# M2: the guard predicate protects the reserved band
mutate guard-protects-band '#define MODEL_GUARD_PROTECTS_BAND 0' \
	's@#define MODEL_GUARD_PROTECTS_BAND 0@#define MODEL_GUARD_PROTECTS_BAND 1 /* MUTATED: the guard protects the band */@'

# M3: the window never closes (the restore is gone)
mutate window-never-closes '#define MODEL_RESTORE_SOFT_LIMIT 1' \
	's@#define MODEL_RESTORE_SOFT_LIMIT 1@#define MODEL_RESTORE_SOFT_LIMIT 0 /* MUTATED: no restore */@'

# M4: the guest-visible query follows the raw soft limit
mutate query-follows-raw '#define MODEL_PUBLISH_RAW_LIMIT 0' \
	's@#define MODEL_PUBLISH_RAW_LIMIT 0@#define MODEL_PUBLISH_RAW_LIMIT 1 /* MUTATED: the query follows the raw limit */@'

expect_failure() {  # $1 = label, $2 = comma-separated claims, $3.. = command
	local label="$1"
	local want="$2"
	shift 2
	local log="$work/$label.log"
	local rc=0 claim

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e

	[ "$rc" != "0" ] || fail "$label: the mutated harness still exited 0"
	for claim in ${want//,/ }; do
		if ! grep -qE "^$claim FAIL " "$log"; then
			sed 's/^/MUTATION /' "$log" || true
			fail "$label: expected $claim to fail"
		fi
		printf 'MUTATION %s: %s\n' "$label" "$(grep -E "^$claim FAIL " "$log" | head -1)"
	done
	printf 'MUTATION PASS %s: exit=%s, %s red as designed\n' "$label" "$rc" "$want"
	note "$label: $want failed as designed"
}

# (a) the mutation the design's claim rests on, in both environments
expect_failure host-raise-removed R2,R3,R6 env LWR_ENV=host "$work/mut_raise-removed" all
expect_failure docker-raise-removed R2,R3,R6 docker "${docker_args[@]}" "$image" \
	env LWR_ENV=docker /proof/mut_raise-removed all

# (b) the other three premises, host only
expect_failure host-guard-protects-band R5 "$work/mut_guard-protects-band" all
expect_failure host-window-never-closes R2,R3 "$work/mut_window-never-closes" all
expect_failure host-query-follows-raw R1,R6 "$work/mut_query-follows-raw" all

printf 'LIMIT_WINDOW_RACE_OK\n'

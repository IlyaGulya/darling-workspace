#!/usr/bin/env bash
#
# run-direct-process-doorbell-proof.sh -- build the direct-process doorbell
# falsification harness and run it on the host and inside ordinary Docker.
#
# The harness (direct-process-doorbell-proof.c) proves or breaks the proposed
# descriptor architecture "Candidate B: constant-anchor direct transport,
# WITHOUT any helper/relay process":
#   * the guest descriptor table holds application descriptors, ONE
#     process-level wake eventfd, ONE process-level control endpoint
#     (SCM_RIGHTS) and fixed anchors - nothing per thread;
#   * N per-thread SPSC lanes, ONE process-wide hierarchical pending bitmap and
#     ONE per-thread reply futex word live in shared memory;
#   * the server is a SEPARATE PROCESS that epoll-waits on the process eventfd
#     plus the control socket, actively polls while busy, drains the pending
#     lanes it locates from the bitmap and wakes the exact parked guest thread
#     by futex on that thread's reply word;
#   * there is NO relay/companion process in this topology.
#
# It prints one line per claim (D1..D8) in each environment, then this runner
# prints D9 (the mutation verdict) and DIRECT_PROCESS_DOORBELL_OK.  Any failure
# exits non-zero.
#
# The harness must be able to FAIL: after the clean runs this script builds
# three deliberate mutations of the premise in its temporary directory (never
# in the repository) and requires the named claim to turn red, in BOTH
# environments:
#   M1 the fork child uses the inherited process eventfd (and therefore the
#      parent's generation) as its own doorbell      -> D6 must fail
#   M2 publication before the pending bit is set      -> D3 must fail
#   M3 the lane generation is omitted from the
#      completion check                               -> D7 (and D6) must fail
#
# The comparison block at the end states plainly that the harness models the
# mechanism and does not execute mldr, darlingserver or any Darling code.
#
# Usage: tests/direct_process_doorbell_proof/run-direct-process-doorbell-proof.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/direct-process-doorbell-proof.c"
work="$(mktemp -d /tmp/direct-process-doorbell-proof.XXXXXX)"
cleanup() {
	# an interrupted run must not leave the harness's server behind: it is a
	# separate process, and in active-polling mode it would spin forever, so
	# kill anything still running out of $work before removing it
	local p
	for p in $(pgrep -f "^$work/" 2>/dev/null); do
		kill -9 -- "$p" 2>/dev/null || true
	done
	rm -rf -- "$work"
}
trap cleanup EXIT INT TERM

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector -pthread)
bin="$work/direct-doorbell-proof"
image="${DDB_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${DDB_TIMEOUT:-900}"

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

# --------------------------------------------------------------------------
# 2. the clean legs: host, then ordinary Docker
# --------------------------------------------------------------------------
docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private
	-e DIRECT_PROOF_TMP=/tmp -v "$work:/proof:ro")

leg_pids=()

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
	for i in 1 2 3 4 5 6 7 8; do
		grep -qE "^D$i PASS " "$log" ||
			fail "$label: claim D$i did not pass"
	done
	grep -qE '^D5 PASS .*0 eventfd writes' "$log" ||
		fail "$label: D5 did not report a zero-write active window"
	grep -qE '^D4 PASS .*hierarchy touched [0-9]+ word' "$log" ||
		fail "$label: D4 did not measure the lookup cost"
	grep -qE '^D7 PASS .*E1 .*E2 ' "$log" ||
		fail "$label: D7 did not report both exec backing options"
	note "$label: D1..D8 all PASS"
}

run_leg host env DIRECT_PROOF_TMP="$work" "$bin" all

command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"

note "docker: docker ${docker_args[*]} $image /proof/direct-doorbell-proof all"
run_leg docker docker "${docker_args[@]}" "$image" \
	/proof/direct-doorbell-proof all

# the container leg must really have run unprivileged with no capabilities
grep -qE '^INFO env uid=1000 ' "$work/docker.log" ||
	fail "docker: the harness did not run as uid 1000 in the container"
grep -qE '^INFO env uid=1000 cap_eff=0+ ' "$work/docker.log" ||
	fail "docker: the container leg did not run with an empty capability set"

# --------------------------------------------------------------------------
# 3. mutations of the premise (built in $work, never in the repository)
# --------------------------------------------------------------------------
mut_attach="$work/mut_inherited_doorbell.c"
mut_arm="$work/mut_late_arm.c"
mut_gen="$work/mut_no_gen_check.c"

# M1: the fork child adopts the inherited process eventfd as its own doorbell.
# Because the doorbell IS the generation in this design, that also makes the
# child adopt the parent's generation.
sed 's@^\(\t/\*MUT1-ATTACH\*/\) my_gen = child_attach_fresh(&child_efd);$@\1 my_gen = G_GEN; child_efd = GEFD_INHERITED; HH->child_inherited_doorbell = 1; (void)child_attach_fresh; /* MUTATED: inherited doorbell+generation adopted */@' \
	"$src" >"$mut_attach"
grep -q 'MUTATED: inherited doorbell+generation adopted' "$mut_attach" ||
	fail "mutation M1 (inherited doorbell) did not apply to the source"
if grep -q 'child_attach_fresh(&child_efd)' "$mut_attach"; then
	fail "mutation M1 left the fresh-generation attach in place"
fi

# M2: set the pending bit AFTER the doorbell decision instead of before it.
sed -e '/\/\*MUT2-ARM\*\//d' \
	-e 's@^\(\t\/\*MUT2-WRITE\*\/ if (need) { doorbell_write(); }\)$@\1\n\t/*MUT2-ARM*/ bitmap_set(lane); fence(); /* MUTATED: armed after the doorbell decision */@' \
	"$src" >"$mut_arm"
grep -q 'MUTATED: armed after the doorbell decision' "$mut_arm" ||
	fail "mutation M2 (late pending bit) did not apply to the source"
arm_line="$(grep -n 'MUT2-ARM' "$mut_arm" | cut -d: -f1)"
write_line="$(grep -n 'MUT2-WRITE' "$mut_arm" | cut -d: -f1)"
[ -n "$arm_line" ] && [ -n "$write_line" ] && [ "$arm_line" -gt "$write_line" ] ||
	fail "mutation M2 did not move the pending bit after the decision"

# M3: drop the lane generation from the completion check.
sed -e 's@/\*MUT3-GEN\*/ if (ld_acq(&r->gen) != my_gen)@/* MUTATED: the generation check is omitted */ if (0) {@' \
	-e 's@\t/\*MUT3-GEN\*/ return 1;.*$@\t\t(void)my_gen; return 1; } /* unreachable */@' \
	"$src" >"$mut_gen"
grep -q 'MUTATED: the generation check is omitted' "$mut_gen" ||
	fail "mutation M3 (no generation check) did not apply to the source"
if grep -q 'r->gen) != my_gen' "$mut_gen"; then
	fail "mutation M3 left the generation check in place"
fi

"$cc" "${cflags[@]}" -o "$work/mut_inherited_doorbell" "$mut_attach"
"$cc" "${cflags[@]}" -o "$work/mut_late_arm" "$mut_arm"
"$cc" "${cflags[@]}" -o "$work/mut_no_gen_check" "$mut_gen"
for m in mut_inherited_doorbell mut_late_arm mut_no_gen_check; do
	if readelf -lW "$work/$m" | grep -q 'INTERP'; then
		fail "mutation binary $m carries PT_INTERP"
	fi
done

mut_evidence=()

expect_failure() {
	local label="$1" evidence="$2"
	shift 2
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
		grep -qE "^$want FAIL " "$log" ||
			fail "$label: expected $want to fail"
	done
	grep -qE "$evidence" "$log" ||
		fail "$label: the expected failure evidence ('$evidence') is not in the log"
	note "$label: ${wants[*]} failed as designed"
	mut_evidence+=("$label: ${wants[*]} FAIL / $(grep -oE "$evidence" "$log" | head -1)")
}

# M1: D6 must fail because a child consumed the parent's completion
expect_failure mut-inherited-doorbell "[1-9][0-9]* consumed by a child" D6 -- \
	env DIRECT_PROOF_TMP="$work" "$work/mut_inherited_doorbell" all
expect_failure docker-mut-inherited-doorbell "[1-9][0-9]* consumed by a child" D6 -- \
	docker "${docker_args[@]}" "$image" /proof/mut_inherited_doorbell all

# M2: D3 must fail because interleavings strand a request
expect_failure mut-late-arm "[1-9][0-9]* left unserviced within 50 ms" D3 -- \
	env DIRECT_PROOF_TMP="$work" "$work/mut_late_arm" all
expect_failure docker-mut-late-arm "[1-9][0-9]* left unserviced within 50 ms" D3 -- \
	docker "${docker_args[@]}" "$image" /proof/mut_late_arm all

# M3: D7 must fail (its exec generations are no longer rejected); D6 fails too
# because the fork-generation rejection is the same check.
expect_failure mut-no-gen-check "generation check \(rc=0, CONSUMED\)" D7 D6 -- \
	env DIRECT_PROOF_TMP="$work" "$work/mut_no_gen_check" all
expect_failure docker-mut-no-gen-check "generation check \(rc=0, CONSUMED\)" D7 D6 -- \
	docker "${docker_args[@]}" "$image" /proof/mut_no_gen_check all

d9_evidence=""
for e in "${mut_evidence[@]}"; do
	d9_evidence="$d9_evidence$e; "
done
printf 'D9 PASS the three mutations each turned the named claim red in both environments: M1 (fork child uses the inherited eventfd+generation as its own doorbell) turned D6 FAIL; M2 (publication before the pending bit is set) turned D3 FAIL; M3 (the lane generation omitted from the completion check) turned D7 FAIL and D6 FAIL (the same check rejects a fork child). Evidence: %s\n' "$d9_evidence"

note "the harness models the mechanism: it executes fork/execve, eventfd,"
note "epoll_wait, futex, SCM_RIGHTS, memfd and SysV shm itself and checks the"
note "claims the design makes about them; it does not run mldr, darlingserver,"
note "Mach or any Darling code, and a PASS here is not a statement about the"
note "product's implementation."

printf 'DIRECT_PROCESS_DOORBELL_OK\n'

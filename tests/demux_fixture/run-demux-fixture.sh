#!/usr/bin/env bash
#
# run-demux-fixture.sh -- build the process-level demultiplexer integration
# fixture and run it on the host and inside ordinary Docker.
#
# The harness (demux-fixture.c) models ONE process-level AF_UNIX datagram
# control/transport socket shared by N guest threads, per-thread completion
# slots in shared memory, an added request id, process generation, target
# kernel tid and lane generation, a futex wake on the target's slot, and
# SCM_RIGHTS on that same socket -- built from the product's REAL request/reply
# structures.  It implements BOTH dispatcher shapes:
#
#   variant 1 (DEMUX_VARIANT=1): a permanent demultiplexer thread owns recvmsg.
#   variant 2 (DEMUX_VARIANT=2): no permanent thread; ONE reader token is held
#                               by one of the waiting threads.
#
# Each environment prints one line per claim (D1..D10); this runner prints
# DEMUX_FIXTURE_OK.  Any failure exits non-zero.
#
#   D1 MUST PASS  32 concurrent blocking receives complete on the one endpoint.
#   D2 MUST PASS  replies delivered in the REVERSE of the arrival order still
#                 reach the right thread (the product matches by call number
#                 only), and the demux's actual delivery order is that reversal.
#   D3 MUST PASS  one waiter times out; the others continue; the late reply for
#                 the abandoned lane is rejected by the lane generation.
#   D4 MUST PASS  one waiter is interrupted; the others continue; in variant 2
#                 the token holder releases the token before returning.
#   D5 MUST PASS  SCM_RIGHTS in both directions, bound to the right logical
#                 request (identity read out of the descriptor itself), and
#                 closed rather than leaked when rejected.
#   D6 MUST PASS  one slow waiter does not head-of-line-block the others.
#   D7 MUST PASS  a stale completion produced before fork is rejected by the
#                 single-threaded child, which re-creates the dispatcher.
#   D8 MUST PASS  a stale completion produced before exec is rejected by the
#                 post-exec image (same tid, new generation).
#   D9 MUST PASS  a caller-local operation executes on the TARGET thread, never
#                 on the dispatcher.
#   D10 MEASUREMENT  thread count, RSS/stack, idle CPU, signal masks, latency
#                 and CPU per request, and the fork/exec consequences.
#
# The harness must be able to FAIL: after the clean runs this runner builds
# seven deliberate mutations of the premise (only in its temporary directory,
# never in the repository) and requires the named claim to go red:
#   M1 match by arrival order                        -> D2 must fail
#   M2 drop the lane generation                      -> D3 must fail
#   M3 dispatch a caller-local op from the dispatcher-> D9 must fail
#   M4 the token holder exits on its interrupt without releasing -> D4 must fail
#   M5 the fork child accepts the parent's generation-> D7 must fail
#   M6 the post-exec image accepts the pre-exec generation -> D8 must fail
#   M7 the token holder does not dispatch others' datagrams -> D6 must fail
# Each mutation is a single marked line; the runner refuses to continue if the
# marker did not apply.
#
# The clean legs run the full-length workload.  The mutated legs set
# DEMUX_FAST=1, which only shortens the deadlines -- the claims and the
# mutation targets are unchanged.
#
# Usage: tests/demux_fixture/run-demux-fixture.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/demux-fixture.c"
work="$(mktemp -d /tmp/demux-fixture-run.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector -pthread)
bin="$work/demux-fixture"
image="${DEMUX_FIXTURE_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${DEMUX_FIXTURE_TIMEOUT:-600}"

# The materialized product forest the matched prefixes were built from (never
# the drifting West tree): every struct the fixture copies and every anchor it
# cites is taken from here, and the generated header is re-created by the
# product's own generator so a drift is visible.
forest="${DEMUX_FOREST:-/home/ilyagulya/work/darling-gwn-resume/darling-workspace/.west-test/runtime-build-cache/source/51a0a5f1fa8c5cf68f013d983fef11d1e4adafe564cf49e8b3626be026b01643/darling}"
gen="$forest/src/external/darlingserver/scripts/generate-rpc-wrappers.py"
inc="$forest/src/external/darlingserver/include"
supp="$inc/darlingserver/rpc-supplement.h"

note() { printf 'INFO %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

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

# The demultiplexer core and every entry point that runs after fork or after
# exec must be libc-free: they are re-created in a single-threaded fork child
# and in a post-exec image, where no libc lock may be relied on.
libc_calls() { objdump -d --disassemble="$1" "$bin" | grep -E '\bcall' || true; }
for fn in dmx_dispatch dmx_s2c_execute d7_child_entry exec_child_entry; do
	n="$(libc_calls "$fn" | wc -l)"
	[ "$n" = "0" ] || fail "$fn contains $n call instruction(s); it must be a raw-syscall path"
	note "$fn: 0 call instructions (raw syscalls only, no libc, no malloc)"
done
for fn in dmx_demux_loop dmx_server_entry d8_child_entry; do
	targets="$(libc_calls "$fn" | sed 's/.*<\(.*\)>.*/\1/' | sort -u | tr '\n' ' ')"
	bad="$(printf '%s' "$targets" | tr ' ' '\n' | grep -vE '^(dmx_|srv_|d7_|d8_|exec_|$)' || true)"
	[ -z "$bad" ] || fail "$fn calls something outside this translation unit: $bad"
	note "$fn: calls only its own libc-free helpers [$targets]"
done
note "raw clone stub: $(objdump -d --disassemble=dmx_raw_clone_thread "$bin" | grep -c syscall) syscall instruction(s), one indirect call to the thread function"

# --------------------------------------------------------------------------
# 2. struct-layout drift check against the product source
#
#    The fixture MECHANICALLY COPIES the product's structs.  The runner
#    re-creates the generated ABI with the product's own generator, compiles a
#    probe that includes the REAL headers, and diffs its sizeof/offsetof table
#    against the fixture's.  A change in the product's structs or in the
#    generated enum therefore shows up as a diff, not as a silent divergence.
# --------------------------------------------------------------------------
[ -r "$gen" ] || fail "missing the product's generator: $gen"
[ -r "$supp" ] || fail "missing the product's rpc-supplement.h: $supp"

python3 "$gen" "$work/rpc.h" "$work/rpc-internal.h" "$work/rpc.c" \
	"darlingserver/rpc-supplement.h"
note "re-generated the product ABI with $gen ($(wc -l <"$work/rpc.h") lines of rpc.h)"

cat >"$work/layout-probe.c" <<'PROBE'
/* the REAL product structures, included rather than copied */
#include <sys/types.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include "rpc.h"
#include <darlingserver/rpc-supplement.h>
#define LP(TY) \
	printf("LAYOUT %-46s size=%zu align=%zu\n", #TY, sizeof(TY), _Alignof(TY))
#define LPF(TY, FLD) \
	printf("LAYOUT %-46s.%s off=%zu size=%zu\n", #TY, #FLD, \
	       offsetof(TY, FLD), sizeof(((TY *)0)->FLD))
int main(void)
{
	LP(dserver_rpc_callhdr_t);
	LPF(dserver_rpc_callhdr_t, number);
	LPF(dserver_rpc_callhdr_t, pid);
	LPF(dserver_rpc_callhdr_t, tid);
	LPF(dserver_rpc_callhdr_t, architecture);
	LP(dserver_rpc_replyhdr_t);
	LPF(dserver_rpc_replyhdr_t, number);
	LPF(dserver_rpc_replyhdr_t, code);
	LP(dserver_rpc_call_thread_self_trap_t);
	LP(dserver_reply_thread_self_trap_t);
	LPF(dserver_reply_thread_self_trap_t, port_name);
	LP(dserver_rpc_reply_thread_self_trap_t);
	LP(dserver_call_mach_msg_overwrite_t);
	LP(dserver_rpc_call_mach_msg_overwrite_t);
	LP(dserver_call_ring_attach_t);
	LP(dserver_reply_ring_attach_t);
	LP(dserver_rpc_call_ring_attach_t);
	LP(dserver_rpc_reply_ring_attach_t);
	LP(dserver_s2c_callhdr_t);
	LP(dserver_s2c_replyhdr_t);
	LP(dserver_s2c_call_mmap_t);
	LP(dserver_s2c_reply_mmap_t);
	LP(dserver_s2c_call_munmap_t);
	LP(dserver_s2c_reply_munmap_t);
	LP(dserver_s2c_call_mprotect_t);
	LP(dserver_s2c_reply_mprotect_t);
	LP(dserver_s2c_call_msync_t);
	LP(dserver_s2c_reply_msync_t);
	LP(dserver_s2c_call_t);
	printf("LAYOUT %-46s size=%zu unsigned=%d\n", "dserver_callnum_t",
	       sizeof(dserver_callnum_t), (int)((dserver_callnum_t)-1 > 0));
	printf("LAYOUT const dserver_callnum_s2c=%d thread_self_trap=%d "
	       "ring_attach=%d mach_msg_overwrite=%d\n",
	       (int)dserver_callnum_s2c, (int)dserver_callnum_thread_self_trap,
	       (int)dserver_callnum_ring_attach,
	       (int)dserver_callnum_mach_msg_overwrite);
	return 0;
}
PROBE

"$cc" -std=gnu11 -O0 -I"$work" -I"$inc" -o "$work/layout-probe" "$work/layout-probe.c" \
	2>/dev/null || fail "the layout probe does not compile against the product headers"
"$work/layout-probe" >"$work/real-layout.txt"

"$bin" layout | grep -v '^LAYOUT fixture ' >"$work/fixture-layout.txt"
if ! diff -u "$work/real-layout.txt" "$work/fixture-layout.txt" >"$work/layout.diff"; then
	sed 's/^/LAYOUT-DRIFT /' "$work/layout.diff"
	fail "the fixture's struct copies have drifted from the product source"
fi
note "layout: $(wc -l <"$work/real-layout.txt") sizeof/offsetof lines identical to the product's own headers"
note "layout: $(grep -c '^LAYOUT ' "$work/real-layout.txt") lines checked, 0 differences"
grep '^LAYOUT const' "$work/real-layout.txt" | sed 's/^/INFO layout: /'
grep '^LAYOUT dserver_callnum_t' "$work/real-layout.txt" | sed 's/^/INFO layout: /'

# --------------------------------------------------------------------------
# 3. the claims, on the host and in ordinary Docker, for both shapes
# --------------------------------------------------------------------------
run_leg() {	# $1 = label, rest = command
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
	for i in 1 2 3 4 5 6 7 8 9 10; do
		grep -qaE "^D$i PASS " "$log" ||
			fail "$label: claim D$i did not pass"
	done
	grep -q '^HARNESS OK ' "$log" ||
		fail "$label: the harness did not print its own OK marker"
	note "$label: D1..D10 all PASS"
}

command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"
docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private -v "$work:/proof:ro")

run_leg host-v1 env DEMUX_ENV=host DEMUX_VARIANT=1 "$bin" all
run_leg host-v2 env DEMUX_ENV=host DEMUX_VARIANT=2 "$bin" all

note "docker: docker ${docker_args[*]} $image /proof/demux-fixture all"
run_leg docker-v1 docker "${docker_args[@]}" "$image" \
	env DEMUX_ENV=docker DEMUX_VARIANT=1 /proof/demux-fixture all
run_leg docker-v2 docker "${docker_args[@]}" "$image" \
	env DEMUX_ENV=docker DEMUX_VARIANT=2 /proof/demux-fixture all

# the decisive Docker-side checks, on the raw lines
grep -qaE '^D1 PASS .*32/32' "$work/docker-v1.log" ||
	fail "docker-v1: D1 did not complete 32/32 blocking receives"
grep -qaE '^D9 PASS .*4/4 executed by the addressed tid, 0 executed by the dispatcher' "$work/docker-v1.log" ||
	fail "docker-v1: D9 did not establish the caller-local execution site"
grep -qaE '^D8 PASS .*REJECTED it' "$work/docker-v1.log" ||
	fail "docker-v1: D8 did not establish the post-exec rejection"
grep -qaE '^D4 PASS .*token before=[0-9]+ after=[0-9]+' "$work/docker-v2.log" ||
	fail "docker-v2: D4 did not report the token state"

# --------------------------------------------------------------------------
# 4. the thread census the decision needs (32 and 64 guest threads)
# --------------------------------------------------------------------------
for v in 1 2; do
	for w in 32 64; do
		env DEMUX_ENV=host DEMUX_VARIANT=$v DEMUX_WORKERS=$w \
			"$bin" census | tee "$work/census-v$v-w$w.log" | sed 's/^/INFO census: /'
	done
done
grep -qaE '^CENSUS variant=1 workers=32 threads=34 ' "$work/census-v1-w32.log" ||
	fail "census: variant 1 at 32 threads did not report 34 tasks"
grep -qaE '^CENSUS variant=1 workers=64 threads=66 ' "$work/census-v1-w64.log" ||
	fail "census: variant 1 at 64 threads did not report 66 tasks"
grep -qaE '^CENSUS variant=2 workers=32 threads=33 ' "$work/census-v2-w32.log" ||
	fail "census: variant 2 at 32 threads did not report 33 tasks"
grep -qaE '^CENSUS variant=2 workers=64 threads=65 ' "$work/census-v2-w64.log" ||
	fail "census: variant 2 at 64 threads did not report 65 tasks"

# --------------------------------------------------------------------------
# 5. the harness must be able to fail.
#
# Seven deliberate mutations of the premise, built in this temporary directory
# and never in the repository.  Each one edits a single marked line; the runner
# verifies that the marker applied and that the expected claim line goes red.
# --------------------------------------------------------------------------
mutate() {	# $1 = name, $2 = marker that must disappear, $3 = sed expression
	local name="$1" marker="$2" expr="$3"
	local out="$work/mut_$name.c"

	sed "$expr" "$src" >"$out"
	if grep -q -- "$marker" "$out"; then
		fail "mutation $name did not apply ($marker still present)"
	fi
	"$cc" "${cflags[@]}" -o "$work/mut_$name" "$out"
}

mutate arrival-order 'MUT1-ORDER' \
	's@idx = dmx_resolve_target(e);[[:space:]]*/\*MUT1-ORDER\*/@idx = (int)((uint32_t)__atomic_fetch_add(\&S->rr_next, 1, __ATOMIC_RELAXED) % (uint32_t)S->nthreads); /* MUTATED: arrival order */@'
mutate no-lane-generation 'MUT2-LANE' \
	's@DMX_LANE_CHECK_ENABLED[[:space:]]*/\*MUT2-LANE\*/@0 /* MUTATED: the lane generation is dropped */@'
mutate op-on-dispatcher 'MUT3-S2C' \
	's@DMX_OP_ON_DISPATCHER[[:space:]]*/\*MUT3-S2C\*/@1 /* MUTATED: the dispatcher executes the caller-local op */@'
mutate token-not-released 'MUT4-TOKEN' \
	's@DMX_RELEASE_ON_INTERRUPT[[:space:]]*/\*MUT4-TOKEN\*/@0 /* MUTATED: the interrupted holder keeps the token */@'
mutate child-accepts-parent 'MUT5-FORK' \
	's@DMX_FORK_GEN_CHECK[[:space:]]*/\*MUT5-FORK\*/@0 /* MUTATED: the child accepts the parent generation */@'
mutate exec-accepts-pre 'MUT6-EXEC' \
	's@DMX_EXEC_GEN_CHECK[[:space:]]*/\*MUT6-EXEC\*/@0 /* MUTATED: the new image accepts the pre-exec generation */@'
mutate holder-own-only 'MUT7-HOL' \
	's@DMX_DISPATCH_OTHERS[[:space:]]*/\*MUT7-HOL\*/@0 /* MUTATED: the holder drops everyone else */@'

expect_failure() {	# $1 = label, $2 = claim that must fail, $3.. = command
	local label="$1"
	local want="$2"
	shift 2
	local log="$work/$label.log"
	local rc=0 line=""

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e

	[ "$rc" != "0" ] || fail "$label: the mutated harness still exited 0"
	if ! grep -qaE "^$want FAIL " "$log"; then
		sed 's/^/MUTATION /' "$log" || true
		fail "$label: expected $want to fail"
	fi
	line="$(grep -aE "^$want FAIL " "$log" | head -1)"
	printf 'MUTATION %s: exit=%s\n' "$label" "$rc"
	grep -aE "^D[0-9]+ FAIL " "$log" | sed 's/^/MUTATION /'
	printf 'D11 PASS %s: %s\n' "$label" "$line"
	note "$label: $want failed as designed"
}

expect_failure host-arrival-order D2 env DEMUX_ENV=host DEMUX_FAST=1 DEMUX_VARIANT=1 "$work/mut_arrival-order" all
expect_failure host-no-lane-generation D3 env DEMUX_ENV=host DEMUX_FAST=1 DEMUX_VARIANT=1 "$work/mut_no-lane-generation" all
expect_failure host-op-on-dispatcher D9 env DEMUX_ENV=host DEMUX_FAST=1 DEMUX_VARIANT=1 "$work/mut_op-on-dispatcher" all
expect_failure host-token-not-released D4 env DEMUX_ENV=host DEMUX_FAST=1 DEMUX_VARIANT=2 "$work/mut_token-not-released" all
expect_failure host-child-accepts-parent D7 env DEMUX_ENV=host DEMUX_FAST=1 DEMUX_VARIANT=1 "$work/mut_child-accepts-parent" all
expect_failure host-exec-accepts-pre D8 env DEMUX_ENV=host DEMUX_FAST=1 DEMUX_VARIANT=1 "$work/mut_exec-accepts-pre" all
expect_failure host-holder-own-only D6 env DEMUX_ENV=host DEMUX_FAST=1 DEMUX_VARIANT=2 "$work/mut_holder-own-only" all

# three of the mutations also have to break inside the container
expect_failure docker-arrival-order D2 docker "${docker_args[@]}" "$image" \
	env DEMUX_ENV=docker DEMUX_FAST=1 DEMUX_VARIANT=1 /proof/mut_arrival-order all
expect_failure docker-no-lane-generation D3 docker "${docker_args[@]}" "$image" \
	env DEMUX_ENV=docker DEMUX_FAST=1 DEMUX_VARIANT=1 /proof/mut_no-lane-generation all
expect_failure docker-op-on-dispatcher D9 docker "${docker_args[@]}" "$image" \
	env DEMUX_ENV=docker DEMUX_FAST=1 DEMUX_VARIANT=1 /proof/mut_op-on-dispatcher all

printf 'DEMUX_FIXTURE_OK\n'

#!/usr/bin/env bash
#
# run-direct-process-control-proof.sh -- build the direct process-level control
# harness and run it on the host and inside ordinary Docker.
#
# The harness (direct-process-control-proof.c) proves or breaks the proposal
# that ONE process-level control descriptor per guest process can carry all
# slow-path traffic for a process with 32 or 64 guest threads, replacing the
# per-thread RPC sockets, while the hot path stays shared-memory only.
#
# It prints one line per claim (C1..C9) in each environment; this runner prints
# DIRECT_PROCESS_CONTROL_OK.  Any failure exits non-zero.
#
#   C1 MUST PASS  the guest's descriptor count is identical at 32 and at 64
#                 guest threads and the guest owns exactly ONE control socket.
#   C2 MUST PASS  64 threads, requests tagged {request id, lane id}: every
#                 request completes exactly once and no thread ever consumes
#                 another thread's completion (per-thread check + ledger).
#   C3 MUST PASS  SCM_RIGHTS in both directions under 64-thread concurrency,
#                 identity proved by a token read out of the descriptor itself,
#                 with no descriptor reaching the wrong request and no leak on
#                 either side.
#   C4 MUST PASS  a descriptor-returning application-table open lands in the
#                 application table of the requesting process, not another.
#   C5 MUST PASS  a forked child neither steals nor interleaves with the
#                 parent's completions and establishes its own endpoint.
#   C6 MUST PASS  after a successful exec the endpoint is re-established, a
#                 request over the superseded generation is refused, the reply
#                 still owed on the pre-exec connection is rejected rather than
#                 consumed, and a failed exec leaves the old endpoint usable.
#   C7 MUST PASS  the hot path is not serialized by the control lock: the lock
#                 is held for a whole hot phase by a deliberate holder, no
#                 control operation completes while it is held, and the hot path
#                 keeps running (throughput printed with and without control
#                 traffic).
#   C8 MEASUREMENT  style 1 (all control traffic behind one lock, reply on the
#                 socket) versus style 2 (multiplexed messages, completion
#                 through the per-thread shared-memory lane): messages/s, mean
#                 and p99 completion latency, and the contention cost of each.
#   C9 MEASUREMENT  the harness's own control:hot ratio, explicitly NOT a
#                 product number.
#
# The harness must be able to FAIL: after the clean runs this runner builds six
# deliberate mutations of the premise (only in its temporary directory, never in
# the repository) and requires the named claim to go red:
#   M1 completion routed to the neighbouring lane        -> C2 (and C3) red
#   M2 transferred descriptor deposited in the wrong lane-> C3 red
#   M3 fork child reuses the parent's endpoint           -> C5 red
#   M4 one control endpoint per guest thread             -> C1 red
#   M5 the hot path takes the control lock               -> C7 red
#   M6 the post-exec image accepts the stale reply       -> C6 red
# Each mutation is a single marked line; the runner refuses to continue if the
# marker did not apply.  M1..M3 (the three the claim list names) run in both
# environments; M4..M6 run on the host.  Every mutation prints a `C10 PASS ...`
# line carrying the claim line it turned red.
#
# The clean legs run the full-length workload (64 threads x 64 requests per
# style, 300 ms hot phases).  The mutated legs set DCP_FAST=1, which only
# shortens the phases and the request counts -- the claims, the 32/64-thread
# descriptor census and the mutation targets are unchanged -- so that the
# runner proves all six premises can break in about a minute.
#
# Usage: tests/direct_process_control_proof/run-direct-process-control-proof.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/direct-process-control-proof.c"
work="$(mktemp -d /tmp/direct-process-control-proof.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector)
bin="$work/direct-process-control-proof"
image="${DCP_PROOF_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${DCP_PROOF_TIMEOUT:-900}"
fast_env=(DCP_FAST=1)

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

# every entry point that runs after fork or after exec must be libc-free: no
# call instruction at all (raw syscalls only, no errno, no stdio, no malloc)
for fn in server_entry c5_child_entry c6_child_entry exec_child_entry; do
	calls="$(objdump -d --disassemble="$fn" "$bin" | grep -cE '\bcall' || true)"
	[ "$calls" = "0" ] ||
		fail "$fn contains $calls call instruction(s); it must be a raw-syscall path"
	note "$fn: 0 call instructions (raw syscalls only, no libc)"
done

# the hot publish path must not touch the control lock
hot_lock="$(objdump -d --disassemble=hot_publish "$bin" |
	grep -cE 'lock_acquire|futex|hot_slot_guard' || true)"
[ "$hot_lock" = "0" ] ||
	fail "hot_publish references the control lock $hot_lock time(s)"
note "hot_publish: 0 references to the control lock (hot path is lock-free)"

# --------------------------------------------------------------------------
# 2. run every claim on the host
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
	for i in 1 2 3 4 5 6 7 8 9; do
		if ! grep -qE "^C$i PASS " "$log"; then
			fail "$label: claim C$i did not pass"
		fi
	done
	grep -q '^HARNESS OK ' "$log" ||
		fail "$label: the harness did not print its own OK marker"
	note "$label: C1..C9 all PASS"
}

run_leg host env DCP_ENV=host "$bin" all

# --------------------------------------------------------------------------
# 3. the same binary inside ordinary Docker: UID 1000, every capability
#    dropped, private IPC namespace, default seccomp/AppArmor.  The harness
#    creates its own scratch directory under /tmp inside the container, so the
#    read-only mount of the build directory is enough.
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"

docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private -v "$work:/proof:ro")

note "docker: docker ${docker_args[*]} $image /proof/direct-process-control-proof all"
run_leg docker docker "${docker_args[@]}" "$image" env DCP_ENV=docker /proof/direct-process-control-proof all

grep -qE '^C1 PASS .*control sockets 1' "$work/docker.log" ||
	fail "docker: C1 did not establish the single-control-socket census"
grep -qE '^C3 PASS .*no leak' "$work/docker.log" ||
	fail "docker: C3 did not establish the no-leak condition"
grep -qE '^C6 PASS .*rejected instead of consumed' "$work/docker.log" ||
	fail "docker: C6 did not establish the stale-reply rejection"

# --------------------------------------------------------------------------
# 4. the harness must be able to fail.
#
# Six deliberate mutations of the premise, built in this temporary directory
# and never in the repository.  Each one edits a single marked line; the runner
# verifies that the marker applied and that the expected claim line goes red.
# --------------------------------------------------------------------------
mutate() {  # $1 = name, $2 = marker that must disappear, $3 = sed expression
	local name="$1" marker="$2" expr="$3"
	local out="$work/mut_$name.c"

	sed "$expr" "$src" >"$out"
	if grep -q -- "$marker" "$out"; then
		fail "mutation $name did not apply ($marker still present)"
	fi
	"$cc" "${cflags[@]}" -o "$work/mut_$name" "$out"
}

mutate wrong-lane 'MUT1-ROUTE' \
	's@return lane;[[:space:]]*/\*MUT1-ROUTE\*/@return lane ^ 1u; /* MUTATED: neighbour lane */@'
mutate fd-wrong-request 'MUT2-FD' \
	's@return lane;[[:space:]]*/\*MUT2-FD\*/@return lane ^ 1u; /* MUTATED: descriptor to the wrong lane */@'
mutate child-reuses-parent 'MUT3-FORK' \
	's@/\*MUT3-FORK\*/ RSYS1(SYS_close, inherited);@/* MUTATED: reuses the parent control socket */ return inherited;@'
mutate per-thread-endpoints 'MUT4-PERTHREAD' \
	's@/\*MUT4-PERTHREAD\*/ if (g_per_thread_sockets)@if (1)@'
mutate hot-path-locked 'MUT5-HOTLOCK' \
	's@hot_slot_guard(0);[[:space:]]*/\*MUT5-HOTLOCK[^*]*\*/@hot_slot_guard(1); /* MUTATED: the hot path takes the control lock */@'
mutate stale-accepted 'MUT6-STALE' \
	's@/\*MUT6-STALE\*/ rep.stale_rejected = 1;@/* MUTATED: the stale reply is accepted */ rep.stale_rejected = 0;@'

expect_failure() {  # $1 = label, $2 = claim that must fail, $3.. = command
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
	if ! grep -qE "^$want FAIL " "$log"; then
		sed 's/^/MUTATION /' "$log" || true
		fail "$label: expected $want to fail"
	fi
	line="$(grep -E "^$want FAIL " "$log" | head -1)"
	printf 'MUTATION %s: exit=%s\n' "$label" "$rc"
	sed 's/^/MUTATION /' "$log" || true
	# the failing claim line, in the per-environment C10 form
	printf 'C10 PASS %s: %s\n' "$label" "$line"
	note "$label: $want failed as designed"
}

# (a) the three the claim list names, in both environments
expect_failure host-wrong-lane C2 env DCP_ENV=host DCP_FAST=1 "$work/mut_wrong-lane" all
expect_failure host-fd-wrong-request C3 env DCP_ENV=host DCP_FAST=1 "$work/mut_fd-wrong-request" all
expect_failure host-child-reuses-parent C5 env DCP_ENV=host DCP_FAST=1 "$work/mut_child-reuses-parent" all

expect_failure docker-wrong-lane C2 docker "${docker_args[@]}" "$image" \
	env DCP_ENV=docker DCP_FAST=1 /proof/mut_wrong-lane all
expect_failure docker-fd-wrong-request C3 docker "${docker_args[@]}" "$image" \
	env DCP_ENV=docker DCP_FAST=1 /proof/mut_fd-wrong-request all
expect_failure docker-child-reuses-parent C5 docker "${docker_args[@]}" "$image" \
	env DCP_ENV=docker DCP_FAST=1 /proof/mut_child-reuses-parent all

# (b) three more premises, host only
expect_failure host-per-thread-endpoints C1 env DCP_ENV=host DCP_FAST=1 \
	"$work/mut_per-thread-endpoints" all
expect_failure host-hot-path-locked C7 env DCP_ENV=host DCP_FAST=1 \
	"$work/mut_hot-path-locked" all
expect_failure host-stale-accepted C6 env DCP_ENV=host DCP_FAST=1 \
	"$work/mut_stale-accepted" all

printf 'DIRECT_PROCESS_CONTROL_OK\n'

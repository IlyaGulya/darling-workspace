#!/usr/bin/env bash
#
# run-generation-aba-proof.sh -- build the generation/ABA harness and run it on
# the host and inside ordinary Docker.
#
# The harness (generation-aba-proof.c) proves or breaks the requirement that a
# transport slot reused by a new occupant never accepts a completion produced
# for its previous occupant, for the four completion classes the transport has,
# and under repeated reuse:
#
#   G1 MUST PASS  a delayed ring completion produced for occupant A of slot X
#                 (whose TID is recycled) is REJECTED when it is delivered into
#                 the slot B now owns; the identity compared is
#                 {slot, generation, owner_tid, seq, callnum} and the rejection
#                 reason is printed.  B's OWN delayed completion is still
#                 accepted, so the verdict cannot be earned by rejecting
#                 everything.
#   G2 MUST PASS  a delayed SERVER-INITIATED (S2C) upcall produced for the
#                 pre-fork occupant is refused by the fork child that re-claimed
#                 the slot (the postfork reset restarts the epoch counter, so the
#                 child's first claim carries the SAME generation and only the
#                 thread token separates the two); the compared identity is
#                 {slot, generation, owner_tid, parent_id}, the upcall is
#                 reported rejected/undeliverable, and the munmap(2) it names
#                 does NOT run (mincore still reports the page mapped).
#   G3 MUST PASS  a completion for an ABANDONED request id, arriving on the one
#                 process-level control endpoint, does not satisfy the request a
#                 different occupant is blocked on; the compared identity is
#                 {request_id, slot, generation, owner_tid, token}, B is observed
#                 still blocked when it arrives, and B's own completion is
#                 delivered to B.
#   G4 MUST PASS  an SCM_RIGHTS descriptor sent for A's logical request is not
#                 installed into B's outstanding request and is CLOSED rather
#                 than leaked (both processes' real fd counts, before/after).
#   G5 MUST PASS  >= 1000 slot acquisitions, every completion delayed between
#                 production and delivery, zero wrong consumptions, and the
#                 legitimate deliveries still accepted.
#
# The harness must be able to FAIL: after the clean runs this runner builds four
# deliberate mutations of the model (only in its temporary directory, never in
# the repository) and requires the named claim - and only the named claim - to go
# red, in both environments:
#   M1 the generation is dropped from the identity comparison (the deployed
#      state: `generation' is bumped on every (re)claim and nothing reads it)
#      -> G1 red (and G5 red: the repeated reuse then consumes 300 stale
#      completions).  G2/G3/G4 stay green: G2's epochs collide by construction
#      so the generation check was never what caught it, and G3/G4 do not use
#      the lane epoch at all.
#   M2 the lane comparison is reduced to the numeric slot id, without the thread
#      identity -> G2 red (and G5 red: the 64 fork-epoch cycles then execute the
#      upcall's munmap in the child).  G1 stays green: a same-process reuse bumps
#      the generation, so the generation check still separates the epochs.
#   M3 a control completion is matched by arrival order instead of by request id
#      -> G3 red (and only G3).
#   M4 a descriptor is installed without checking the request identity -> G4 red
#      (and only G4).
# Each mutation flips a single marked #define; the runner verifies that the
# marker disappeared, that the mutant binary differs from the clean one, and
# that the expected claim lines are red and the others green.
#
# The harness is a MODEL of the transport (see its header for the anchors in
# dserver-ring.c and for the fidelity limits): it executes real syscalls (fork,
# AF_UNIX SOCK_SEQPACKET, SCM_RIGHTS, memfd_create/pipe2, munmap, mincore,
# futex, /proc fd accounting) but the lane table, the completion stamps and the
# identity predicates are transcriptions of the contract, not mldr or
# darlingserver themselves.
#
# Usage: tests/generation_aba_proof/run-generation-aba-proof.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/generation-aba-proof.c"
work="$(mktemp -d /tmp/generation-aba-proof.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector -pthread)
bin="$work/generation-aba-proof"
image="${GAP_PROOF_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${GAP_PROOF_TIMEOUT:-300}"

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

# symbol under a name pattern, then its disassembly (a clone may carry a
# .constprop/.isra suffix, so the pattern is a prefix match)
region() {  # $1 = binary, $2 = symbol regex
	local sym
	sym="$(nm --defined-only "$1" | awk -v p="$2" '$NF ~ p {print $NF; exit}')"
	[ -n "$sym" ] || return 1
	objdump -d --disassemble="$sym" "$1"
}

# The server is a fork child that must run libc-free; the fork-under-test child
# must not touch the allocator, stdio or the pthread locks after the fork.
region "$bin" '^server_entry$' >/dev/null ||
	fail "server_entry is not a separate symbol"
srv_calls="$(region "$bin" '^server_entry$' | grep -cE '[[:space:]]call' || true)"
[ "$srv_calls" = "0" ] ||
	fail "server_entry contains $srv_calls call instruction(s); it must be libc-free"
srv_syscalls="$(region "$bin" '^server_entry$' | grep -c 'syscall' || true)"
[ "$srv_syscalls" -ge 5 ] ||
	fail "server_entry issues $srv_syscalls raw syscall(s); the raw-syscall server is not modeled"
note "server_entry: 0 calls, $srv_syscalls raw syscalls (libc-free server)"

region "$bin" '^fork_child_entry$' >/dev/null ||
	fail "fork_child_entry is not a separate symbol"
child_calls="$(region "$bin" '^fork_child_entry$' |
	grep -E '[[:space:]]call' |
	grep -cE 'malloc|calloc|realloc|free|printf|fprintf|vfprintf|puts|fwrite|fopen|abort|exit|pthread_|__errno_location|dlopen|dlsym|getenv|sysconf' || true)"
[ "$child_calls" = "0" ] ||
	fail "fork_child_entry calls $child_calls allocator/stdio/locking libc function(s)"
child_syscalls="$(region "$bin" '^fork_child_entry$' | grep -c 'syscall' || true)"
[ "$child_syscalls" -ge 4 ] ||
	fail "fork_child_entry issues $child_syscalls raw syscall(s); the post-fork path is not modeled"
note "fork_child_entry: 0 allocator/stdio/locking calls, $child_syscalls raw syscalls"

# --------------------------------------------------------------------------
# 2. run every claim
# --------------------------------------------------------------------------
run_leg() {
	local label="$1"
	shift
	local log="$work/$label.log"
	local rc=0 min_delay

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e
	cat "$log"

	[ "$rc" = "0" ] || fail "$label: harness exited $rc"
	grep -q '^HARNESS OK ' "$log" ||
		fail "$label: the harness did not print its own OK marker"
	grep -q '^G1 PASS .*reason=generation-mismatch' "$log" ||
		fail "$label: G1 did not reject the stale completion by generation"
	grep -q '^G2 PASS .*reason=owner-tid-mismatch' "$log" ||
		fail "$label: G2 did not refuse the S2C upcall by thread identity"
	grep -q '^G3 PASS .*reason=no-live-request-with-that-id' "$log" ||
		fail "$label: G3 did not refuse the incomplete request's completion"
	grep -q '^G4 PASS .*was rejected and closed, so not leaked' "$log" ||
		fail "$label: G4 did not reject and close the stale descriptor"
	grep -q '^G5 PASS .*0 wrong consumptions' "$log" ||
		fail "$label: G5 did not report zero wrong consumptions"
	# the identity tuple each claim compares, printed by the harness
	grep -q '^IDENT G1 .*compared=slot,generation,owner_tid,seq,callnum' "$log" ||
		fail "$label: G1 did not print the compared identity fields"
	grep -q '^IDENT G2 .*compared=slot,generation,owner_tid,parent_id' "$log" ||
		fail "$label: G2 did not print the compared identity fields"
	grep -q '^IDENT G3 .*compared=request_id,slot,generation,owner_tid,token' "$log" ||
		fail "$label: G3 did not print the compared identity fields"
	grep -q '^IDENT G4 .*compared=descriptor_token,request_id' "$log" ||
		fail "$label: G4 did not print the compared identity fields"
	grep -q '^IDENT G5 load: .*wrong_consumptions=0' "$log" ||
		fail "$label: G5 did not report a zero wrong-consumption load"
	# the delay between production and delivery is real and measured
	min_delay="$(grep -oE 'delay_ns\{min=[0-9]+' "$log" | head -1 | cut -d= -f2)"
	[ -n "$min_delay" ] || fail "$label: G5 did not report the injected delay"
	[ "$min_delay" -ge 500000 ] ||
		fail "$label: G5 measured a minimum injected delay of ${min_delay}ns, below 500us"
	note "$label: G1..G5 all PASS"
}

run_leg host env GAP_ENV=host "$bin" all

# --------------------------------------------------------------------------
# 3. the same binary inside ordinary Docker: UID 1000, every capability
#    dropped, private IPC namespace, default seccomp/AppArmor -- no privileged,
#    no host sysctl changes.  The harness needs no filesystem at all (its
#    scratch is anonymous shared memory and a socketpair), so a read-only mount
#    of the build directory is enough.
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"

docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private -v "$work:/proof:ro")

note "docker: docker ${docker_args[*]} $image /proof/generation-aba-proof all"
run_leg docker docker "${docker_args[@]}" "$image" \
	env GAP_ENV=docker /proof/generation-aba-proof all

grep -q '^HARNESS OK env=docker' "$work/docker.log" ||
	fail "docker: the harness did not run as the container leg"

# --------------------------------------------------------------------------
# 4. the harness must be able to fail.
#
# Four deliberate mutations of the model, built in this temporary directory and
# never in the repository.  Each one flips a single marked #define; the runner
# verifies that the marker applied, that the binary changed, and that exactly
# the expected claims go red.
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

expect_matrix() {  # $1 = label, $2 = "G1=FAIL,G2=PASS,...", $3.. = command
	local label="$1"
	local matrix="$2"
	shift 2
	local log="$work/$label.log"
	local rc=0 pair claim want got

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e

	[ "$rc" != "0" ] || fail "$label: the mutated harness still exited 0"
	for pair in ${matrix//,/ }; do
		claim="${pair%%=*}"
		want="${pair##*=}"
		got="$(grep -oE "^$claim (PASS|FAIL)" "$log" | head -1 | awk '{print $2}')"
		if [ "$got" != "$want" ]; then
			sed 's/^/MUTATION /' "$log" || true
			fail "$label: expected $claim=$want but measured ${got:-<missing>}"
		fi
		if [ "$want" = "FAIL" ]; then
			printf 'MUTATION %s: %s\n' "$label" \
				"$(grep -E "^$claim FAIL " "$log" | head -1)"
		fi
	done
	printf 'MUTATION PASS %s: exit=%s, %s\n' "$label" "$rc" "$matrix"
}

# M1: the generation is dropped from the identity comparison.  This is the
# deployed state (nothing reads `generation'), so it is the mutation the claim
# rests on and it runs in BOTH environments.
mutate generation-dropped '#define MODEL_CHECK_GENERATION 1' \
	's@#define MODEL_CHECK_GENERATION 1@#define MODEL_CHECK_GENERATION 0 /* MUTATED: nothing reads the generation */@'

# M2: the lane comparison is reduced to the numeric slot id (no thread identity).
mutate owner-tid-dropped '#define MODEL_CHECK_OWNER_TID 1' \
	's@#define MODEL_CHECK_OWNER_TID 1@#define MODEL_CHECK_OWNER_TID 0 /* MUTATED: no thread identity */@'

# M3: a control completion is matched by arrival order, not by request id.
mutate control-arrival-order '#define MODEL_CTL_MATCH_BY_REQUEST_ID 1' \
	's@#define MODEL_CTL_MATCH_BY_REQUEST_ID 1@#define MODEL_CTL_MATCH_BY_REQUEST_ID 0 /* MUTATED: arrival order */@'

# M4: a descriptor is installed without checking the request identity.
mutate descriptor-unchecked '#define MODEL_DESC_CHECK_REQUEST_ID 1' \
	's@#define MODEL_DESC_CHECK_REQUEST_ID 1@#define MODEL_DESC_CHECK_REQUEST_ID 0 /* MUTATED: no identity check */@'

m1_matrix='G1=FAIL,G2=PASS,G3=PASS,G4=PASS,G5=FAIL'
m2_matrix='G1=PASS,G2=FAIL,G3=PASS,G4=PASS,G5=FAIL'
m3_matrix='G1=PASS,G2=PASS,G3=FAIL,G4=PASS,G5=PASS'
m4_matrix='G1=PASS,G2=PASS,G3=PASS,G4=FAIL,G5=PASS'

# (a) the mutation the claim rests on, in both environments
expect_matrix host-generation-dropped "$m1_matrix" \
	env GAP_ENV=host "$work/mut_generation-dropped" all
expect_matrix docker-generation-dropped "$m1_matrix" docker "${docker_args[@]}" "$image" \
	env GAP_ENV=docker /proof/mut_generation-dropped all

# (b) the other three premises, in both environments
expect_matrix host-owner-tid-dropped "$m2_matrix" \
	env GAP_ENV=host "$work/mut_owner-tid-dropped" all
expect_matrix docker-owner-tid-dropped "$m2_matrix" docker "${docker_args[@]}" "$image" \
	env GAP_ENV=docker /proof/mut_owner-tid-dropped all
expect_matrix host-control-arrival-order "$m3_matrix" \
	env GAP_ENV=host "$work/mut_control-arrival-order" all
expect_matrix docker-control-arrival-order "$m3_matrix" docker "${docker_args[@]}" "$image" \
	env GAP_ENV=docker /proof/mut_control-arrival-order all
expect_matrix host-descriptor-unchecked "$m4_matrix" \
	env GAP_ENV=host "$work/mut_descriptor-unchecked" all
expect_matrix docker-descriptor-unchecked "$m4_matrix" docker "${docker_args[@]}" "$image" \
	env GAP_ENV=docker /proof/mut_descriptor-unchecked all

printf 'GENERATION_ABA_OK\n'

#!/usr/bin/env bash
#
# run-fd-semantics-proof.sh -- build the descriptor-creator semantics harness
# and run it on the host and inside ordinary Docker.
#
# The harness (fd-semantics-proof.c) answers one question per descriptor
# creating class: is the drafted "virtual soft limit with post-filter" scheme
# (design doc docs/direct-transport-descriptor-architecture.md, option A4.2)
# observably equivalent to the kernel's own behaviour at a real soft limit?
#
# It prints one line per claim (S1, C1..C18) in each environment; this runner
# prints FD_SEMANTICS_PROOF_OK.  Any failure exits non-zero.
#
#   S1 PASS   the regime was really measured: the native children ran at
#             soft == V == 64 and the wrapper children at soft == 96.
#   C1..C3    openat(O_CREAT) / openat(O_TRUNC) / openat(O_CREAT|O_EXCL):
#             native EMFILE stops the operation BEFORE the file side effect;
#             the post-filter leaves a created or truncated file behind.
#   C4        accept4 with a queued connection: native leaves it queued (a
#             retry returns it, the peer is still connected); the post-filter
#             accepted and closed it (the peer sees EOF/RST).
#   C5/C6     recvmsg + SCM_RIGHTS (dgram, stream): native SUCCEEDS, delivers
#             the payload, sets MSG_CTRUNC and drops/closes the descriptors;
#             the post-filter returns EMFILE after the payload was copied and
#             the message consumed.
#   C7..C14   pipe2, socket, socketpair, eventfd, epoll_create (libkqueue's
#             kqueue backend), inotify_init1, signalfd, timerfd_create: the
#             post-filter is observably equivalent (object exists only inside
#             the wrapper call, nothing leaked).
#   C15/C16   SCM_RIGHTS through a pre-call bounded-delivery wrapper instead of
#             a post-filter: equivalent, including MSG_CTRUNC and the cmsg
#             descriptor count and number.
#   C17/C18   a pre-check scheme under a deterministic check-to-act
#             interleaving: the side effect (truncation, consumed connection)
#             leaks even though the pre-check passed.
#
# The harness must be able to FAIL.  After the clean runs this runner builds
# three deliberate mutations of the model (only in its temporary directory,
# never in the repository) and requires the named claims to go red:
#   M1 the post-filter forgets close()                -> C1..C14, C17, C18 red
#   M2 the native leg never lowers the real limit     -> C1..C18 red
#   M3 the wrapper pre-checks instead of filtering    -> C1..C4 red only
#      (C5..C18 stay green: the mutation is surgical, the SCM_RIGHTS and
#       interleaving claims are untouched)
#
# Usage: tests/fd_semantics_proof/run-fd-semantics-proof.sh
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/fd-semantics-proof.c"
work="$(mktemp -d /tmp/fd-semantics-proof-run.XXXXXX)"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

cc="${CC:-gcc}"
cflags=(-std=gnu11 -O2 -Wall -Wextra -Werror -static -fno-stack-protector -pthread)
bin="$work/fd-semantics-proof"
image="${FSP_PROOF_DOCKER_IMAGE:-ubuntu:24.04}"
run_timeout="${FSP_PROOF_TIMEOUT:-300}"

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

# every mutation marker must exist in the source, so the sed mutations below
# cannot silently no-op
for marker in \
	'#define MUT_NO_CLOSE_AFTER_REJECT 0' \
	'#define MUT_NATIVE_LIMIT_RAISED 0' \
	'#define MUT_PRECHECK_FOR_SINGLE 0'; do
	grep -qF -- "$marker" "$src" || fail "mutation marker missing from source: $marker"
done
note "source carries all three mutation markers"

# the harness must exercise every creator under test, by name, in the source
for sym in setrlimit dup pipe2 socketpair socket accept4 recvmsg eventfd \
	epoll_create inotify_init1 signalfd timerfd_create open; do
	grep -qE "(^|[^A-Za-z_])$sym *\(" "$src" ||
		fail "the harness source never calls $sym"
done
note "source exercises every creator under test"

# ... and the built artifact must actually reach them
objdump -d "$bin" >"$work/disasm"
calls_to() { # $1 = symbol regex
	local addr
	addr="$(nm --defined-only "$bin" | awk -v p="$1" '$NF ~ p {print $1; exit}' | sed 's/^0*//')"
	[ -n "$addr" ] || return 1
	grep -cE "call +$addr " "$work/disasm" || true
}
for sym in '^accept4$' '^recvmsg$' '^pipe2$' '^socketpair$' '^socket$' '^eventfd$' \
	'^epoll_create$' '^inotify_init1$' '^signalfd$' '^timerfd_create$' \
	'^setrlimit$' '^pthread_create$'; do
	n="$(calls_to "$sym")"
	[ -n "$n" ] && [ "$n" -ge 1 ] ||
		fail "the built harness contains no call to $sym"
done
note "artifact reaches every creator, setrlimit and pthread_create"

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
	grep -qE '^S1 PASS regime: .*V=64, REAL_LIMIT=96, native child soft=64, wrapper child soft=96' "$log" ||
		fail "$label: S1 did not measure the expected regime"
	for i in $(seq 1 18); do
		grep -qE "^C$i PASS " "$log" || fail "$label: claim C$i did not pass"
	done
	grep -q '^HARNESS OK ' "$log" ||
		fail "$label: the harness did not print its own OK marker"

	# the verdict direction each case must reach
	for i in 1 2 3 4 5 6 17 18; do
		grep -qE "^C$i PASS .*NOT EQUIVALENT" "$log" ||
			fail "$label: C$i did not report NOT EQUIVALENT"
	done
	for i in 7 8 9 10 11 12 13 14 15; do
		grep -qE "^C$i PASS .*=> EQUIVALENT" "$log" ||
			fail "$label: C$i did not report EQUIVALENT"
	done
	c16="$(grep -E '^C16 PASS ' "$log")"
	case "$c16" in
	*"bounded EQUIVALENT"*) ;;
	*) fail "$label: C16 did not report the bounded-delivery scheme as EQUIVALENT" ;;
	esac
	case "$c16" in
	*"naive post-filter is NOT EQUIVALENT"*) ;;
	*) fail "$label: C16 did not report the naive post-filter as NOT EQUIVALENT" ;;
	esac

	# the named observable side effect of each non-equivalent class
	grep -qE '^C1 PASS .*empty regular file was created and left behind' "$log" ||
		fail "$label: C1 did not observe the created file left behind"
	grep -qE '^C2 PASS .*contents were destroyed \(native leaves size 4096\)' "$log" ||
		fail "$label: C2 did not observe the destroyed contents"
	grep -qE '^C3 PASS .*created the file \(a retry now fails EEXIST\)' "$log" ||
		fail "$label: C3 did not observe the exclusive create"
	grep -qE '^C4 PASS .*accepted and closed' "$log" ||
		fail "$label: C4 did not observe the consumed connection"
	grep -qE '^C5 PASS .*native SUCCEEDS' "$log" ||
		fail "$label: C5 did not observe the native MSG_CTRUNC success"
	grep -qE '^C6 PASS .*native SUCCEEDS' "$log" ||
		fail "$label: C6 did not observe the native MSG_CTRUNC success"
	grep -qE '^C16 PASS .*rejected a message native partially delivered' "$log" ||
		fail "$label: C16 did not observe the partial delivery"
	grep -qE '^C17 PASS .*truncation happened while EMFILE was returned' "$log" ||
		fail "$label: C17 did not observe the leaked truncation"
	grep -qE '^C18 PASS .*accepted and closed while EMFILE was returned' "$log" ||
		fail "$label: C18 did not observe the leaked accept"

	# the kernel's own errno for the exhausted cases, in both legs
	grep -qE '^C1 PASS .*native\{rc=-1 errno=Too many open files.*postfilter\{rc=-1 errno=Too many open files' "$log" ||
		fail "$label: C1 did not measure EMFILE in both legs"
	grep -qE '^C5 PASS .*native\{rc=5 errno=- .*ctrunc=1 ctrlfds=0 ctrlbytes=0 data=5 free=0->0' "$log" ||
		fail "$label: C5 did not measure the native truncated delivery"
	grep -qE '^C16 PASS .*native\{rc=5 errno=- .*ctrunc=1 ctrlfds=1 ctrlbytes=24 data=5 free=1->1' "$log" ||
		fail "$label: C16 did not measure the native partial delivery"

	note "$label: S1, C1..C18 all PASS"
}

run_leg host env FSP_PROOF_ENV=host "$bin"

# --------------------------------------------------------------------------
# 3. the same binary inside ordinary Docker: UID 1000, every capability
#    dropped, private IPC namespace, default seccomp/AppArmor.  The harness
#    creates its own scratch tree under /tmp inside the container, so the
#    read-only mount of the build directory is enough, and it needs no network
#    (its sockets are a socketpair and an AF_UNIX listener in that scratch
#    tree).
# --------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "docker is required for the container leg"
docker image inspect "$image" >/dev/null 2>&1 ||
	fail "docker image $image is not present locally (this harness never pulls)"

docker_args=(run --rm -u 1000:1000 --cap-drop=ALL --ipc=private -v "$work:/proof:ro")

note "docker: docker ${docker_args[*]} $image /proof/fd-semantics-proof"
run_leg docker docker "${docker_args[@]}" "$image" env FSP_PROOF_ENV=docker /proof/fd-semantics-proof

# --------------------------------------------------------------------------
# 4. the harness must be able to fail.
#
# Three deliberate mutations of the model, built in this temporary directory and
# never in the repository.  Each one flips a single marked line; the runner
# verifies that the marker applied and that the binary changed.
# --------------------------------------------------------------------------
mutate() { # $1 = name, $2 = marker that must disappear, $3 = sed expression
	local name="$1" marker="$2" expr="$3"
	local out="$work/mut_$name.c"

	sed "$expr" "$src" >"$out"
	if grep -qF -- "$marker" "$out"; then
		fail "mutation $name did not apply ($marker still present)"
	fi
	"$cc" "${cflags[@]}" -o "$work/mut_$name" "$out"
	if [ "$(sha256sum "$work/mut_$name" | cut -d' ' -f1)" = \
	     "$(sha256sum "$bin" | cut -d' ' -f1)" ]; then
		fail "mutation $name produced an identical binary"
	fi
	note "mutation $name built: $(sha256sum "$work/mut_$name" | cut -c1-16)"
}

expect_failure() { # $1 = label, $2 = comma-separated claims, $3.. = command
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
		printf 'MUTATION %s: %s\n' "$label" "$(grep -E "^$claim FAIL " "$log" | head -1 | cut -c1-200)"
	done
	printf 'MUTATION PASS %s: exit=%s, %s red as designed\n' "$label" "$rc" "$want"
	note "$label: $want failed as designed"
}

expect_clean() { # $1 = label, $2 = comma-separated claims that must stay green, $3.. = command
	local label="$1"
	local keep="$2"
	shift 2
	local log="$work/$label.log"
	local rc=0 claim

	set +e
	timeout "$run_timeout" "$@" >"$log" 2>&1
	rc=$?
	set -e
	for claim in ${keep//,/ }; do
		grep -qE "^$claim PASS " "$log" ||
			fail "$label: $claim did not stay green (mutation not surgical)"
	done
	printf 'MUTATION SURGICAL %s: exit=%s, %s stayed green\n' "$label" "$rc" "$keep"
}

# M1: the post-filter forgets the close(), so every rejected descriptor leaks.
mutate no-close '#define MUT_NO_CLOSE_AFTER_REJECT 0' \
	's@#define MUT_NO_CLOSE_AFTER_REJECT 0@#define MUT_NO_CLOSE_AFTER_REJECT 1 /* MUTATED: no close */@'
expect_failure host-no-close C1,C4,C5,C7,C14,C17 env "$work/mut_no-close"
expect_failure docker-no-close C1,C7 env FSP_PROOF_ENV=docker docker "${docker_args[@]}" "$image" \
	/proof/mut_no-close

# M2: the native leg never lowers the real limit, so it is no longer the
# kernel's own EMFILE.
mutate native-limit-raised '#define MUT_NATIVE_LIMIT_RAISED 0' \
	's@#define MUT_NATIVE_LIMIT_RAISED 0@#define MUT_NATIVE_LIMIT_RAISED 1 /* MUTATED: limit not lowered */@'
expect_failure host-native-limit-raised C1,C5,C7,C14,C15,C17 env "$work/mut_native-limit-raised"

# M3: the wrapper pre-checks for a free below-limit slot instead of
# post-filtering.  That makes the single/two-descriptor classes equivalent, so
# their NOT EQUIVALENT claims go red -- and only those.
mutate precheck '#define MUT_PRECHECK_FOR_SINGLE 0' \
	's@#define MUT_PRECHECK_FOR_SINGLE 0@#define MUT_PRECHECK_FOR_SINGLE 1 /* MUTATED: pre-check */@'
expect_failure host-precheck C1,C2,C3,C4 env "$work/mut_precheck"
expect_clean host-precheck C5,C6,C7,C14,C16,C17,C18 env "$work/mut_precheck"

printf 'FD_SEMANTICS_PROOF_OK\n'

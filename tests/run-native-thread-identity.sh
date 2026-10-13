#!/usr/bin/env bash
# run-native-thread-identity.sh -- permanent oracle for the guest false-identity defect (bead dar-liaq).
#
# WHAT IT MEASURES. A NATIVE (host ELF) library -- built here on the HOST, because a guest clang produces Mach-O
# and cannot build it -- is loaded into a guest process through the loader's own native-library API
# (_elfcalls->dlopen) and creates a thread with the HOST libc's pthread_create. For BOTH the creating and the
# created thread the oracle reports the raw GS base (arch_prctl ARCH_GET_GS) and the 8 bytes at gs_base+24
# (__TSD_MACH_THREAD_SELF), read twice and independently: natively with `movq %gs:24` and by the GUEST through a
# callback. tests/native_thread_identity_native.c and tests/native_thread_identity_guest.c hold the two halves.
#
# RED (the defect, measured on an unfixed runtime): the child's gs_base EQUALS the creator's and its slot 3 EQUALS
# the creator's live token -- the child answers another live thread's identity to libdispatch's ownership test
# (_dispatch_lock_is_locked_by(word, self), self = slot3 & 0xfffffffc), so an in-flight dispatch_once gate owned by
# a DIFFERENT thread reads as "mine" and libdispatch traps by design ("trying to lock recursively").
# GREEN (fixed): the child has its OWN gs_base and reads slot 3 as no identity (0) or its own token, never the
# creator's.
#
# The guest probe is compiled and run through the sanctioned guest-C runner (testkit/scripts/run-darling-c-test.sh),
# so upload, in-guest compilation, execution, timeout and cleanup stay structured and this script owns only the
# host-side native build, the staging of the two test assets, and the verdict.
#
# Exit status: 0 GREEN (and NATIDENT-OK is printed), 1 RED, 2 the guest instrument could not measure,
# 3 the harness could not get a verdict at all (infrastructure -- never reported as RED).
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$here/.." && pwd)"

prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
if [ -z "$prefix" ]; then
	echo "NATIDENT-HARNESS-VERDICT INFRASTRUCTURE no DPREFIX/DARLING_PREFIX" >&2
	exit 3
fi
launcher="${DARLING_LAUNCHER:-${DARLING:-$prefix/bin/darling}}"
src_root="${DARLING_SRC_ROOT:-$(cd "$workspace_root/.." && pwd)/darling}"
native_src="$here/native_thread_identity_native.c"
guest_src="$here/native_thread_identity_guest.c"
guest_runner="$workspace_root/testkit/scripts/run-darling-c-test.sh"
elfcalls_header="$src_root/src/startup/mldr/elfcalls/elfcalls.h"

for required in "$native_src" "$guest_src" "$guest_runner" "$elfcalls_header" "$launcher"; do
	if [ ! -e "$required" ]; then
		echo "NATIDENT-HARNESS-VERDICT INFRASTRUCTURE missing input: $required" >&2
		exit 3
	fi
done

# Guest compiler and SDK. The prefix may carry the CommandLineTools itself, or (as this workspace's rootless
# prefixes do) the guest may reach the host's ~/.darling tree through the launcher's /Volumes/SystemRoot mapping.
# Both are tried in the order the guest can actually resolve them, and the resolved pair is printed.
guest_cc="${DARLING_GUEST_CC:-}"
guest_sysroot="${DARLING_GUEST_SYSROOT:-}"
if [ -z "$guest_cc" ]; then
	if [ -x "$HOME/.darling/Library/Developer/CommandLineTools/usr/bin/clang" ]; then
		guest_cc="/Volumes/SystemRoot$HOME/.darling/Library/Developer/CommandLineTools/usr/bin/clang"
		guest_sysroot="${guest_sysroot:-/Volumes/SystemRoot$HOME/.darling/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk}"
	else
		guest_cc="/Library/Developer/CommandLineTools/usr/bin/clang"
		guest_sysroot="${guest_sysroot:-/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk}"
	fi
fi
guest_sysroot="${guest_sysroot:-/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk}"

guest_tmpdir="$prefix/private/var/tmp"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/natident-build.XXXXXX")"
run_log="$guest_tmpdir/native-thread-identity.run.log"
cleanup() { rm -rf "$build_dir"; }
trap cleanup EXIT

echo "NATIDENT-HARNESS prefix=$prefix"
echo "NATIDENT-HARNESS launcher=$launcher"
echo "NATIDENT-HARNESS src_root=$src_root"
echo "NATIDENT-HARNESS guest_cc=$guest_cc sysroot=$guest_sysroot"

# ---- 1. build the native half on the HOST ---------------------------------------------------------------
if ! "${CC:-cc}" -shared -fPIC -O0 -g -pthread -o "$build_dir/libnatident.so" "$native_src"; then
	echo "NATIDENT-HARNESS-VERDICT INFRASTRUCTURE the native library did not build" >&2
	exit 3
fi

# ---- 2. stage both assets where the guest (and the loader's host-side dlopen) can see them ----------------
mkdir -p "$guest_tmpdir"
cp "$build_dir/libnatident.so" "$guest_tmpdir/libnatident.so"
cp "$elfcalls_header" "$guest_tmpdir/natident-elfcalls.h"
native_path="$guest_tmpdir/libnatident.so"
sha256sum "$native_path" "$guest_tmpdir/natident-elfcalls.h"

# ---- 3. compile and run the guest probe through the sanctioned guest-C runner ----------------------------
echo "NATIDENT-HARNESS runner=$guest_runner"
DPREFIX="$prefix" DARLING_PREFIX="$prefix" DARLING_LAUNCHER="$launcher" \
	DARLING_GUEST_TIMEOUT_SECONDS="${NATIDENT_GUEST_TIMEOUT_SECONDS:-120}" \
	"$guest_runner" --name native_thread_identity --source "$guest_src" \
	--launcher "$launcher" \
	--cc "$guest_cc" \
	--cflags "-isysroot $guest_sysroot -Wl,-undefined,dynamic_lookup" \
	--ok-marker 'NATIDENT-GUEST-DONE=1' \
	-- "$native_path" > "$run_log" 2>&1
runner_rc=$?
cat "$run_log"

# ---- 4. verdict -----------------------------------------------------------------------------------------
verdict="$(grep -a -o 'NATIDENT-VERDICT [A-Za-z-]*' "$run_log" | head -1 | awk '{print $2}')"
if [ -z "$verdict" ]; then
	echo "NATIDENT-HARNESS-VERDICT INFRASTRUCTURE no NATIDENT-VERDICT line in $run_log (runner rc=$runner_rc)" >&2
	echo "ORACLE_RC=3"
	exit 3
fi

case "$verdict" in
	INSTRUMENT-FAILED)
		echo "NATIDENT-HARNESS-VERDICT INSTRUMENT-FAILED the guest instrument could not measure (runner rc=$runner_rc)" >&2
		echo "ORACLE_RC=2"
		exit 2
		;;
	RED-false-identity)
		echo "NATIDENT-HARNESS-VERDICT RED the child thread answers the creator's live identity (runner rc=$runner_rc)"
		echo "ORACLE_RC=1"
		exit 1
		;;
	GREEN-*)
		if [ "$runner_rc" -ne 0 ]; then
			echo "NATIDENT-HARNESS-VERDICT INFRASTRUCTURE the oracle reported $verdict but the guest runner returned $runner_rc" >&2
			echo "ORACLE_RC=3"
			exit 3
		fi
		echo "NATIDENT-HARNESS-VERDICT GREEN $verdict"
		echo "NATIDENT-OK"
		echo "ORACLE_RC=0"
		exit 0
		;;
	*)
		echo "NATIDENT-HARNESS-VERDICT INFRASTRUCTURE unrecognised verdict $verdict" >&2
		echo "ORACLE_RC=3"
		exit 3
		;;
esac

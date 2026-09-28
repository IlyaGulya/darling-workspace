#!/usr/bin/env bash
# Section 3E, last mile: prove the CLEAN-BUILT artifacts, not the working tree.
#
# The prefix is bootstrapped with the standard profile (which installs a runtime built from the session source), and then
# the FOUR artifacts under test are installed over it from the clean build tree, sha256-checked. The boot and the courier
# gate that follow therefore exercise the clean-built binaries.
set -u
CLEAN_BUILD=/home/ilyagulya/work/r1-clean-build
PREFIX=/tmp/r1-clean-prefix
WS=/home/ilyagulya/work/darling-gwn-resume/darling-workspace
LOG=/tmp/r1-clean-prefix.log

echo "== 1. bootstrap the separate prefix (absent or empty is required) =="
rm -rf "$PREFIX"; mkdir -p "$PREFIX"
( cd "$WS" && mise run west test --prefix "$PREFIX" --bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal ) > "$LOG" 2>&1
rc=$?
echo "bootstrap rc=$rc (log: $LOG)"
[ $rc -ne 0 ] && { tail -12 "$LOG"; echo "CLEAN-PREFIX-DONE rc=$rc"; exit $rc; }
echo "prefix launcher: $(ls -1 "$PREFIX/bin/darling" 2>/dev/null || echo MISSING)"
echo "runtime profile marker: $(ls -1 "$PREFIX/.west-runtime-profile.json" 2>/dev/null || echo MISSING)"

echo "== 2. install the CLEAN-BUILT artifacts over the bootstrapped runtime =="
install_checked() { # $1 built file, $2 destination
	[ -f "$1" ] || { echo "  MISSING build artifact $1"; return 1; }
	install -m 0755 "$1" "$2" || return 1
	a=$(sha256sum "$1" | cut -c1-16); b=$(sha256sum "$2" | cut -c1-16)
	echo "  $( [ "$a" = "$b" ] && echo MATCH || echo MISMATCH ) $a $2"
}
install_checked "$CLEAN_BUILD/src/external/darlingserver/darlingserver" "$PREFIX/bin/darlingserver"
for d in "$PREFIX/usr/libexec/darling/mldr" "$PREFIX/libexec/darling/usr/libexec/darling/mldr"; do
	[ -d "$(dirname "$d")" ] && install_checked "$CLEAN_BUILD/src/startup/mldr/mldr" "$d"
done
for d in "$PREFIX/usr/lib/system/libsystem_kernel.dylib" "$PREFIX/libexec/darling/usr/lib/system/libsystem_kernel.dylib"; do
	[ -d "$(dirname "$d")" ] && install_checked "$CLEAN_BUILD/src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib" "$d"
done
for d in "$PREFIX/usr/lib/dyld" "$PREFIX/libexec/darling/usr/lib/dyld"; do
	[ -d "$(dirname "$d")" ] && install_checked "$CLEAN_BUILD/src/external/dyld/dyld" "$d"
done

echo "== 3. boot + courier purity on the CLEAN prefix =="
( cd "$WS" && scripts/dwdiag verdict --prefix "$PREFIX" --mode sem_ready --args 2 --wait 180 --repeat 1 ) 2>&1 | tail -4
( cd "$WS" && scripts/dwdiag courier ) 2>&1 | grep -E "legacy ordinary|zero-fd|COURIER-VERDICT|SCM_RIGHTS" | head -6
echo "CLEAN-PREFIX-DONE rc=0"

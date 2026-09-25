#!/bin/sh
# darling-deploy-verify.sh -- install built components into a Darling prefix and VERIFY every copy.
#
# WHY THIS EXISTS. Two frictions cost real time in one investigation cycle:
#
#   * a component is loaded from a path that is not the one that was deployed. The loader takes dyld from
#     INSTALL_PREFIX "/libexec/usr/lib/dyld"; guest processes are launched as `mldr <program>` where the program
#     path is passed by darlingserver; and several components exist in two copies (usr/... and
#     libexec/darling/usr/...). Deploying to the "obvious" path only is silent: the run keeps using the old file.
#   * an install can succeed yet be useless (ETXTBSY on a running binary, or a copy that never gets read).
#
# So: install, then compare sha256 of the BUILT file against EVERY deployed copy and fail loudly on any mismatch.
#
# Usage:
#   darling-deploy-verify.sh --prefix PATH --build DIR [--component NAME]...
#   components: mldr dyld libsystem_kernel darlingserver shellspawn vchroot launchd (default: all present in DIR)
#
# The build directory is expected to be a Darling build tree; each component is looked up at its known output
# path. This script never invents paths: an unknown component is an error, not a guess.

set -u

PREFIX=""
BUILD=""
COMPONENTS=""

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--build) BUILD="$2"; shift 2 ;;
		# --component ACCUMULATES. It used to assign, so `--component a --component b` silently deployed
		# only b -- the tool quietly doing less than it was asked, which is the same class of defect as a
		# probe that is not in the artifact.
		--component) COMPONENTS="$COMPONENTS $2"; shift 2 ;;
		-h|--help) sed -n '2,20p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[ -n "$PREFIX" ] && [ -n "$BUILD" ] || { echo "usage: $0 --prefix PATH --build DIR [--component NAME]..." >&2; exit 2; }
[ -d "$PREFIX" ] || { echo "not a directory: $PREFIX" >&2; exit 2; }
[ -d "$BUILD" ] || { echo "not a directory: $BUILD" >&2; exit 2; }
[ -n "$COMPONENTS" ] || COMPONENTS="mldr dyld libsystem_kernel darlingserver shellspawn vchroot launchd"

built_path() {
	case "$1" in
		mldr)              echo "$BUILD/src/startup/mldr/mldr" ;;
		dyld)              echo "$BUILD/src/external/dyld/dyld" ;;
		libsystem_kernel)  echo "$BUILD/src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib" ;;
		darlingserver)     echo "$BUILD/src/external/darlingserver/darlingserver" ;;
		shellspawn)        echo "$BUILD/src/shellspawn/shellspawn" ;;
		vchroot)           echo "$BUILD/src/vchroot/vchroot" ;;
		launchd)           echo "$BUILD/src/launchd/src/launchd" ;;
		*) return 1 ;;
	esac
}

dest_paths() {
	case "$1" in
		mldr)              echo "libexec/darling/usr/libexec/darling/mldr"; echo "usr/libexec/darling/mldr" ;;
		dyld)              echo "usr/lib/dyld"; echo "libexec/darling/usr/lib/dyld" ;;
		libsystem_kernel)  echo "usr/lib/system/libsystem_kernel.dylib"; echo "libexec/darling/usr/lib/system/libsystem_kernel.dylib" ;;
		darlingserver)     echo "bin/darlingserver" ;;
		shellspawn)        echo "usr/libexec/shellspawn"; echo "libexec/darling/usr/libexec/shellspawn" ;;
		vchroot)           echo "usr/libexec/darling/vchroot"; echo "libexec/darling/usr/libexec/darling/vchroot" ;;
		launchd)           echo "sbin/launchd" ;;
		*) return 1 ;;
	esac
}

rc=0
for c in $COMPONENTS; do
	src=$(built_path "$c") || { echo "unknown component: $c" >&2; rc=2; continue; }
	if [ ! -f "$src" ]; then
		echo "$c: NOT BUILT: $src" >&2
		rc=2
		continue
	fi
	want=$(sha256sum "$src" | cut -c1-16)
	dest_paths "$c" | while read -r rel; do
		dst="$PREFIX/$rel"
		if [ ! -d "$(dirname "$dst")" ]; then
			echo "$c: skip (no directory): $rel"
			continue
		fi
		install -m755 "$src" "$dst" 2>/dev/null || { echo "$c: INSTALL FAILED: $rel" >&2; exit 1; }
		got=$(sha256sum "$dst" | cut -c1-16)
		if [ "$got" = "$want" ]; then
			echo "$c: ok   $want  $rel"
		else
			echo "$c: MISMATCH built=$want deployed=$got  $rel" >&2
			exit 1
		fi
	done || rc=1
done

exit $rc

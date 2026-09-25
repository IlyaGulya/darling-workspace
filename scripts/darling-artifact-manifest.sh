#!/bin/sh
# darling-artifact-manifest.sh -- a CONTENT baseline over the runtime artifacts, so a bisect is possible without
# git history.
#
# WHY THIS EXISTS. The working tree used for this investigation has a single empty commit and every path untracked,
# so `git diff` is empty and no GOOD/CURRENT substitution matrix can be built from history. A runtime experiment
# therefore cannot answer "did this artifact change since the run that worked?" -- which is the first question asked
# whenever a run turns RED after an edit. Recording sha256 of every component in the build tree AND every copy the
# runtime can load answers it, and does so for the artifact that is actually executed rather than for the source.
#
# Usage:
#   darling-artifact-manifest.sh --build DIR --prefix PATH --save FILE
#   darling-artifact-manifest.sh --build DIR --prefix PATH --check FILE
#   darling-artifact-manifest.sh --prefix PATH --probe '[my-tag]' [--probe TAG]...
#
# --save writes an ordered, diffable manifest; --check reports every difference and exits non-zero.
# --probe answers, across all runtime copies, whether a probe tag is present -- the check that would have caught a
# probe compiled into a unit the image does not link.

set -u

PREFIX=""
BUILD=""
SAVE=""
CHECK=""
PROBES=""

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--build) BUILD="$2"; shift 2 ;;
		--save) SAVE="$2"; shift 2 ;;
		--check) CHECK="$2"; shift 2 ;;
		--probe) PROBES="$PROBES
$2"; shift 2 ;;
		-h|--help) sed -n '2,18p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

COMPONENTS="mldr dyld libsystem_kernel darlingserver shellspawn vchroot launchd"

built_path() {
	case "$1" in
		mldr)             echo "$BUILD/src/startup/mldr/mldr" ;;
		dyld)             echo "$BUILD/src/external/dyld/dyld" ;;
		libsystem_kernel) echo "$BUILD/src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib" ;;
		darlingserver)    echo "$BUILD/src/external/darlingserver/darlingserver" ;;
		shellspawn)       echo "$BUILD/src/shellspawn/shellspawn" ;;
		vchroot)          echo "$BUILD/src/vchroot/vchroot" ;;
		launchd)          echo "$BUILD/src/launchd/src/launchd" ;;
		*) return 1 ;;
	esac
}

dest_paths() {
	case "$1" in
		mldr)             echo "libexec/darling/usr/libexec/darling/mldr"; echo "usr/libexec/darling/mldr" ;;
		dyld)             echo "usr/lib/dyld"; echo "libexec/darling/usr/lib/dyld" ;;
		libsystem_kernel) echo "usr/lib/system/libsystem_kernel.dylib"; echo "libexec/darling/usr/lib/system/libsystem_kernel.dylib" ;;
		darlingserver)    echo "bin/darlingserver" ;;
		shellspawn)       echo "usr/libexec/shellspawn"; echo "libexec/darling/usr/libexec/shellspawn" ;;
		vchroot)          echo "usr/libexec/darling/vchroot"; echo "libexec/darling/usr/libexec/darling/vchroot" ;;
		launchd)          echo "sbin/launchd" ;;
		*) return 1 ;;
	esac
}

emit() {
	if [ -n "$BUILD" ]; then
		for c in $COMPONENTS; do
			src=$(built_path "$c") || continue
			if [ -f "$src" ]; then
				printf '%s\tbuilt\t%s\n' "$(sha256sum "$src" | cut -c1-64)" "$src"
			else
				printf '%s\tbuilt\t%s\n' "ABSENT" "$src"
			fi
		done
	fi
	if [ -n "$PREFIX" ]; then
		for c in $COMPONENTS; do
			dest_paths "$c" | while read -r rel; do
				dst="$PREFIX/$rel"
				if [ -f "$dst" ]; then
					printf '%s\tdeployed\t%s\n' "$(sha256sum "$dst" | cut -c1-64)" "$rel"
				else
					printf '%s\tdeployed\t%s\n' "ABSENT" "$rel"
				fi
			done
		done
	fi
}

if [ -n "$SAVE" ]; then
	emit > "$SAVE"
	echo "manifest written: $SAVE ($(wc -l < "$SAVE") entries)"
	exit 0
fi

if [ -n "$CHECK" ]; then
	[ -f "$CHECK" ] || { echo "no such manifest: $CHECK" >&2; exit 2; }
	cur=$(mktemp); emit > "$cur"
	if diff -u "$CHECK" "$cur" > /tmp/manifest-diff.$$ 2>&1; then
		echo "manifest MATCHES ($(wc -l < "$cur") entries): no artifact changed"
		rm -f "$cur" /tmp/manifest-diff.$$
		exit 0
	fi
	echo "manifest DIFFERS:"
	# Compare by path, reporting hash changes, removals and additions. Written with temp files rather than
	# process substitution: /bin/sh here is dash and supports neither <(...) nor $'...'.
	a=$(mktemp); b=$(mktemp)
	cut -f2 "$CHECK" | sort > "$a"
	cut -f2 "$cur" | sort > "$b"
	# changed hashes
	while IFS="$(printf '\t')" read -r want where; do
		now=$(grep -F -- "$(printf '\t')$where" "$cur" 2>/dev/null | cut -f1)
		[ "$now" = "$want" ] && continue
		if [ -z "$now" ]; then
			echo "  GONE    $where (was $want)"
		else
			echo "  CHANGED $where"
			echo "    was $want"
			echo "    now $now"
		fi
	done < "$CHECK"
	# additions
	while IFS="$(printf '\t')" read -r now where; do
		grep -F -q -- "$(printf '\t')$where" "$CHECK" || echo "  NEW     $where ($now)"
	done < "$cur"
	echo "  (full diff in /tmp/manifest-diff.$$)"
	rm -f "$a" "$b" "$cur"
	exit 1
fi

if [ -n "$PREFIX" ] && [ -n "$PROBES" ]; then
	echo "$PROBES" | while IFS= read -r t; do
		[ -n "$t" ] || continue
		echo "probe $t:"
		for c in $COMPONENTS; do
			dest_paths "$c" | while read -r rel; do
				dst="$PREFIX/$rel"
				[ -f "$dst" ] || continue
				# -F is mandatory: a tag contains '[' and ']', so as a regex it is a bracket expression and
				# grep rejects it outright ("Invalid range end"). An earlier version wrote
				# `|| echo 0` around this, which turned that ERROR into the finding "the probe is not in the
				# artifact" -- a silent false negative, the exact failure mode this tool exists to remove.
				out=$(LC_ALL=C grep -a -c -F -- "$t" "$dst" 2>&1)
				rc=$?
				# rc=1 means "no match" (not an error); rc>=2 means grep could not do its job.
				if [ "$rc" -ge 2 ]; then
					echo "  grep ERROR on $rel (rc=$rc): $out" >&2
					exit 1
				fi
				[ "$rc" -eq 0 ] && echo "  PRESENT x$out  $rel"
			done
		done
	done
	exit 0
fi

echo "nothing to do: give --save, --check, or --probe" >&2
exit 2

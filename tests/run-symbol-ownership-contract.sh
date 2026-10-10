#!/usr/bin/env bash
# Exactly one library may define a public concurrency primitive.
#
# A guest process dispatches os_unfair_lock_lock to whichever loaded library defines it first, and a
# library that defines a PUBLIC name with a stub body silently takes the primitive away from its real
# owner: the lock stops locking while every caller still sees success. That happened here -- the
# emulation inside libsystem_kernel defined os_unfair_lock_lock/unlock as empty functions, the guest
# bound to them, and a shared unfair lock admitted every thread that asked (measured: 2798 of 2800
# rounds). Reading the source would have shown a `weak` attribute that was supposed to lose the
# binding; reading the ARTIFACTS is what shows a strong `T` definition in two libraries.
#
# This contract therefore inspects artifacts, never source text, and it refuses to pass when it cannot
# see them: an unmeasured ownership is not a clean ownership.
set -euo pipefail

fail=0
unmeasured=0
note() { printf '%s\n' "$*"; }

# Every public primitive whose implementation must have exactly one owner. Add here when a new
# primitive is shared across component boundaries.
SYMBOLS=(
	_os_unfair_lock_lock
	_os_unfair_lock_unlock
	_os_unfair_lock_trylock
	_os_unfair_lock_lock_with_options
)

artifacts=("$@")
if ((${#artifacts[@]} == 0)); then
	# Default to the deployed prefix when one is configured; otherwise the caller must name the files.
	prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
	if [[ -n "$prefix" ]]; then
		artifacts=(
			"$prefix/usr/lib/system/libsystem_kernel.dylib"
			"$prefix/usr/lib/system/libsystem_platform.dylib"
		)
	fi
fi

if ((${#artifacts[@]} == 0)); then
	note "SYMBOL-OWNERSHIP: UNMEASURED no artifact paths given and no DPREFIX; this contract inspects built or deployed libraries and cannot judge source text"
	exit 77
fi

command -v llvm-nm >/dev/null 2>&1 || {
	note "SYMBOL-OWNERSHIP: UNMEASURED llvm-nm is not installed; artifact symbol tables cannot be read"
	exit 77
}

readable=0
for artifact in "${artifacts[@]}"; do
	[[ -f "$artifact" ]] && readable=$((readable + 1))
done
if ((readable == 0)); then
	note "SYMBOL-OWNERSHIP: UNMEASURED none of the named artifacts exists: ${artifacts[*]}"
	exit 77
fi

for symbol in "${SYMBOLS[@]}"; do
	definers=()
	for artifact in "${artifacts[@]}"; do
		[[ -f "$artifact" ]] || continue
		# nm letters: T/t = text definition (external/local), W/w = weak definition, U = undefined.
		# Only definitions count as owners; an undefined reference is not one.
		if llvm-nm "$artifact" 2>/dev/null | awk -v s="$symbol" '$3 == s && $2 ~ /^[TtWw]$/ { found = 1 } END { exit !found }'; then
			definers+=("$artifact")
		fi
	done
	case ${#definers[@]} in
	0)
		note "SYMBOL-OWNERSHIP: $symbol has NO definer among ${artifacts[*]} -- either it is not built, or every library only references it"
		unmeasured=$((unmeasured + 1))
		;;
	1)
		note "SYMBOL-OWNERSHIP: $symbol defined by ${definers[0]}"
		;;
	*)
		note "SYMBOL-OWNERSHIP: VIOLATION $symbol is defined by ${#definers[@]} libraries: ${definers[*]}"
		note "  a guest binds one of them, and the other silently stops being the implementation"
		fail=1
		;;
	esac
done

if ((unmeasured > 0 && fail == 0)); then
	note "SYMBOL-OWNERSHIP: UNMEASURED $unmeasured symbol(s) had no definer: ownership is not established, and absence of a definition is not the same as one owner"
	exit 77
fi
((fail == 0)) || exit 1
note "SYMBOL-OWNERSHIP: OK every primitive has exactly one definer"

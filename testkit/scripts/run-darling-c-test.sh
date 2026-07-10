#!/usr/bin/env bash
set -euo pipefail

name=
source=
guest_cc="${DARLING_GUEST_CC:-/Library/Developer/CommandLineTools/usr/bin/clang}"
guest_cflags="${DARLING_GUEST_CFLAGS:--isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk}"
launcher="${DARLING_LAUNCHER:-}"
args=()

while (($#)); do
	case "$1" in
		--name)
			name="$2"
			shift 2
			;;
		--source)
			source="$2"
			shift 2
			;;
		--launcher)
			launcher="$2"
			shift 2
			;;
		--cc)
			guest_cc="$2"
			shift 2
			;;
		--cflags)
			guest_cflags="$2"
			shift 2
			;;
		--)
			shift
			args=("$@")
			break
			;;
		*)
			echo "unknown argument: $1" >&2
			exit 2
			;;
	esac
done

if [[ -z "$name" || -z "$source" ]]; then
	echo "usage: run-darling-c-test.sh --name NAME --source PATH [--launcher PATH] [-- ARGS...]" >&2
	exit 2
fi
if [[ -z "$launcher" ]]; then
	echo "$name: DARLING_LAUNCHER is unset" >&2
	exit 2
fi
if [[ ! -f "$source" ]]; then
	echo "$name: source not found: $source" >&2
	exit 2
fi

safe_name="${name//[^A-Za-z0-9_.-]/_}"
guest_src="/tmp/${safe_name}.c"
guest_bin="/tmp/${safe_name}"

quoted_args=()
for arg in "${args[@]}"; do
	quoted_args+=("$(printf '%q' "$arg")")
done

"$launcher" shell /bin/bash --login -c "
set -euo pipefail
cat > '$guest_src'
$guest_cc $guest_cflags -o '$guest_bin' '$guest_src'
'$guest_bin' ${quoted_args[*]}
" <"$source"

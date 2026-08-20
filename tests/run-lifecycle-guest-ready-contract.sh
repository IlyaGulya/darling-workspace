#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
owned_root="${DARLING_LIFECYCLE_GUEST_ROOT:-}"
remove_owned_root=0
if [[ -z "$owned_root" ]]; then
	owned_root="$(mktemp -d "${TMPDIR:-/tmp}/darling-lifecycle-guest-ready.XXXXXX")"
	remove_owned_root=1
else
	mkdir -p -- "$owned_root"
	owned_root="$(realpath -- "$owned_root")"
fi
prefix="${DARLING_LIFECYCLE_GUEST_PREFIX:-$owned_root/darling-rootless-guest-ready}"
evidence="${DARLING_LIFECYCLE_GUEST_EVIDENCE:-$owned_root/evidence}"
report="${DARLING_LIFECYCLE_GUEST_REPORT:-$owned_root/report.json}"
owned_prefix=0

cleanup() {
	local rc="$?"
	set +e
	if [[ -d "$prefix" ]]; then
		env -u DARLING_LIFECYCLE_COHORT_V1 \
			DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
			mise -C "$repo" exec -- west test --prefix "$prefix" --cleanup-prefix >/dev/null 2>&1
	fi
	if (( owned_prefix == 1 )); then
		rm -rf -- "$prefix"
	fi
	if (( remove_owned_root == 1 )); then
		rm -rf -- "$owned_root"
	fi
	exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$prefix" in
	"$owned_root"/*) ;;
	*)
		echo "guest-ready prefix must be inside the task-owned root" >&2
		exit 2
		;;
esac

mkdir -p -- "$owned_root/tmp"
if [[ ! -x "$prefix/bin/darling" ]]; then
	[[ ! -e "$prefix" ]] || {
		echo "refusing to bootstrap over a partial guest-ready prefix: $prefix" >&2
		exit 2
	}
	mkdir -- "$prefix"
	owned_prefix=1
	env -u DARLING_LIFECYCLE_COHORT_V1 TMPDIR="$owned_root/tmp" \
		DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
		WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 \
		mise -C "$repo" exec -- west test --prefix "$prefix" \
		--bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal \
		--runtime-build-timeout-seconds 600 --bootstrap-timeout-seconds 180
fi

export CARGO_TARGET_DIR="$owned_root/cargo-target"
env CARGO_NET_OFFLINE=true cargo build \
	--manifest-path "$repo/lifecycle/operation-boundary/Cargo.toml" \
	--bin lifecycle-fuzz >/dev/null

python3 -B "$repo/tests/west_test_contracts/lifecycle_guest_ready_contract.py" \
	--workspace "$repo" \
	--prefix "$prefix" \
	--launcher "$prefix/bin/darling" \
	--verifier "$CARGO_TARGET_DIR/debug/lifecycle-fuzz" \
	--evidence "$evidence" \
	--report "$report"

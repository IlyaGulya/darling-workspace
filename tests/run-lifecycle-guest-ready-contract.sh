#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cell_workspace="${DARLING_LIFECYCLE_GUEST_WORKSPACE:-$repo}"
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
build_dir="${DARLING_LIFECYCLE_GUEST_BUILD_DIR:-${DARLING_BUILD_DIR:-}}"
forest="${DARLING_LIFECYCLE_GUEST_FOREST:-$(dirname "$cell_workspace")}"
cohort="${DARLING_LIFECYCLE_GUEST_COHORT:-OFF}"
owned_prefix=0

cleanup() {
	local rc="$?"
	set +e
	if [[ -d "$prefix" ]]; then
		env -u DARLING_LIFECYCLE_COHORT_V1 \
			DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
			mise -C "$cell_workspace" exec -- west test --prefix "$prefix" --cleanup-prefix >/dev/null 2>&1
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
[[ -n "$build_dir" ]] || {
	echo "guest-ready RuntimeCell requires DARLING_LIFECYCLE_GUEST_BUILD_DIR" >&2
	exit 2
}
[[ -x "$prefix/bin/darling" ]] || {
	echo "RuntimeCell requires an exact product-owned deployed prefix" >&2
	exit 2
}

python3 -B "$repo/tests/west_test_contracts/lifecycle_guest_ready_contract.py" \
	--workspace "$cell_workspace" --forest "$forest" --build-dir "$build_dir" \
	--cohort "$cohort" --prefix "$prefix" --launcher "$prefix/bin/darling" \
	--verifier "$prefix/bin/darling" --evidence "$evidence" --report "$report" \
	--preflight-only

export CARGO_TARGET_DIR="$owned_root/cargo-target"
env CARGO_NET_OFFLINE=true cargo build \
	--manifest-path "$cell_workspace/lifecycle/operation-boundary/Cargo.toml" \
	--bin lifecycle-fuzz >/dev/null

python3 -B "$repo/tests/west_test_contracts/lifecycle_guest_ready_contract.py" \
	--workspace "$cell_workspace" \
	--forest "$forest" \
	--build-dir "$build_dir" \
	--cohort "$cohort" \
	--prefix "$prefix" \
	--launcher "$prefix/bin/darling" \
	--verifier "$CARGO_TARGET_DIR/debug/lifecycle-fuzz" \
	--evidence "$evidence" \
	--report "$report"

#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

case "${1:-}" in
	host)
		exec west test --env host "${@:2}"
		;;
	guest-smoke)
		exec west test --profile homebrew --env darling --label 'smoke:true' \
			--prefix-profile homebrew "${@:2}"
		;;
	guest-full)
		exec west test --profile homebrew --env darling \
			--prefix-profile homebrew "${@:2}"
		;;
	macos)
		build="${DARLING_TESTKIT_BUILD:-$root/.west-test/macos-build}"
		cmake -S testkit -B "$build" -DBUILD_TESTING=ON
		cmake --build "$build" --parallel
		exec ctest --test-dir "$build" --output-on-failure -L 'env:macos'
		;;
	macos-package)
		output="${2:?macos-package requires an output directory}"
		build="${DARLING_TESTKIT_BUILD:-$root/.west-test/macos-build}"
		cmake -S testkit -B "$build" -DBUILD_TESTING=ON \
			-DCMAKE_INSTALL_PREFIX="$output"
		cmake --build "$build" --parallel
		exec cmake --install "$build"
		;;
	macos-installed)
		exec "$root/ci/run-macos-installed-tests.sh" \
			"${2:?macos-installed requires an installed bundle}"
		;;
	*)
		echo "usage: $0 host|guest-smoke|guest-full|macos|macos-package|macos-installed" >&2
		exit 2
		;;
esac

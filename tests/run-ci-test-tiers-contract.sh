#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"

for tool in west cmake ctest; do
	cat >"$tmp/bin/$tool" <<'TOOL'
#!/usr/bin/env bash
printf '%s %s\n' "$(basename "$0")" "$*" >>"$CI_CONTRACT_LOG"
if [ "$(basename "$0")" = west ] && [ "${1:-}" = topdir ]; then
	exit "${CI_WEST_TOPDIR_RC:-0}"
fi
TOOL
	chmod +x "$tmp/bin/$tool"
done

export PATH="$tmp/bin:$PATH"
export CI_CONTRACT_LOG="$tmp/commands"

"$repo/ci/run-test-tier.sh" host
"$repo/ci/run-test-tier.sh" guest-smoke
"$repo/ci/run-test-tier.sh" guest-full
DARLING_TESTKIT_BUILD="$tmp/macos-build" "$repo/ci/run-test-tier.sh" macos
DARLING_TESTKIT_BUILD="$tmp/package-build" \
	"$repo/ci/run-test-tier.sh" macos-package "$tmp/oracle"

grep -F -x -q 'west test --profile homebrew --env host --materialize-profile' "$tmp/commands"
grep -F -x -q 'west test --prefix-profile homebrew --bootstrap-runtime-profile homebrew-prefix-baseline' "$tmp/commands"
grep -F -x -q "west test --env darling --label smoke:true --prefix-profile homebrew" "$tmp/commands"
grep -F -x -q 'west test --profile homebrew --env darling --prefix-profile homebrew' "$tmp/commands"
grep -F -x -q 'west test --env darling --prefix-profile homebrew' "$tmp/commands"
grep -F -x -q "cmake -S testkit -B $tmp/macos-build -DBUILD_TESTING=ON" "$tmp/commands"
grep -F -x -q "ctest --test-dir $tmp/macos-build --output-on-failure -L env:macos" "$tmp/commands"
grep -F -x -q "cmake --install $tmp/package-build" "$tmp/commands"

mkdir -p "$tmp/installed/testcase"
cat >"$tmp/installed/testcase/compat.sample" <<'SAMPLE'
#!/usr/bin/env bash
printf 'SAMPLE_OK\n'
SAMPLE
chmod +x "$tmp/installed/testcase/compat.sample"
printf 'sample\tcompat.sample\tSAMPLE_OK\n' >"$tmp/installed/compat-install-manifest.tsv"
"$repo/ci/run-test-tier.sh" macos-installed "$tmp/installed" |
	grep -F -x -q 'PASS macos/sample'

: >"$tmp/commands"
CI_WEST_TOPDIR_RC=1 "$repo/ci/bootstrap-west.sh"
grep -F -x -q "west init -l $repo" "$tmp/commands"
grep -F -x -q 'west update' "$tmp/commands"

printf 'PASS ci-test-tiers-contract\n'

#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

prefix="$tmp/prefix"
output="$tmp/output"
mkdir -p "$prefix/private/var/tmp" "$prefix/private/var/log" "$prefix/var/run"
printf 'boot trace\n' >"$prefix/.west-rootless-boot.log"
printf 'fd trace\n' >"$prefix/.west-rootless-guest-fd.log"
printf 'tmp trace\n' >"$prefix/private/var/tmp/.west-rootless-boot.log"
printf 'rpc trace\n' >"$prefix/private/var/log/dserver-rpc-trace.log"
printf 'Apple clang version 9.0.0 (clang-900.0.39.2)\nTarget: x86_64-apple-darwin\n' \
	>"$prefix/private/var/tmp/west-clt-proof.clang-version"
printf 'execution-context=guest\nexecutable=/Library/Developer/CommandLineTools/usr/bin/clang\n' \
	>"$prefix/private/var/tmp/west-clt-proof.clang-origin"
ln -s /etc/shadow "$prefix/.west-rootless-shellspawn-fast-exit.trace"

"$repo/ci/collect-rootless-diagnostics.sh" "$output" "$prefix"

grep -F -q 'prefix:' "$output/host-summary.txt"
grep -F -q 'boot trace' "$output/prefix-files/.west-rootless-boot.log"
grep -F -q 'fd trace' "$output/prefix-files/.west-rootless-guest-fd.log"
grep -F -q 'tmp trace' "$output/prefix-files/private/var/tmp/.west-rootless-boot.log"
grep -F -q 'rpc trace' "$output/prefix-files/private/var/log/dserver-rpc-trace.log"
[ ! -e "$output/prefix-files/.west-rootless-shellspawn-fast-exit.trace" ]
cmp -s \
	"$prefix/private/var/tmp/west-clt-proof.clang-version" \
	"$output/guest-clang-version.txt"
grep -F -x -q 'execution-context=guest' <(head -n 1 "$output/guest-clang-origin.txt")
grep -F -x -q \
	'executable=/Library/Developer/CommandLineTools/usr/bin/clang' \
	< <(tail -n 1 "$output/guest-clang-origin.txt")

proof="$repo/tests/run-guest-toolchain-proof.sh"
grep -F -q 'cc=/Library/Developer/CommandLineTools/usr/bin/clang' "$proof"
grep -F -q 'version=/private/var/tmp/west-clt-proof.clang-version' "$proof"
grep -F -q '"$cc" --version > "$version"' "$proof"
if grep -F -q '$(clang --version)' "$proof"; then
	echo 'guest clang proof unexpectedly used host command substitution' >&2
	exit 1
fi

printf 'PASS rootless-diagnostics-contract\n'

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
ln -s /etc/shadow "$prefix/.west-rootless-shellspawn-fast-exit.trace"

"$repo/ci/collect-rootless-diagnostics.sh" "$output" "$prefix"

grep -F -q 'prefix:' "$output/host-summary.txt"
grep -F -q 'boot trace' "$output/prefix-files/.west-rootless-boot.log"
grep -F -q 'fd trace' "$output/prefix-files/.west-rootless-guest-fd.log"
grep -F -q 'tmp trace' "$output/prefix-files/private/var/tmp/.west-rootless-boot.log"
grep -F -q 'rpc trace' "$output/prefix-files/private/var/log/dserver-rpc-trace.log"
[ ! -e "$output/prefix-files/.west-rootless-shellspawn-fast-exit.trace" ]

printf 'PASS rootless-diagnostics-contract\n'

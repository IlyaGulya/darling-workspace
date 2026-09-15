#!/usr/bin/env bash
set -euo pipefail

: "${DPREFIX:?west must provide the disposable test prefix}"
: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"
: "${DARLING_HOMEBREW_LZ4_WORK:?west homebrew-lz4 resource must stage the inputs}"
: "${DARLING_HOMEBREW_LZ4_INPUTS:?west homebrew-lz4 resource must provide the provenance record}"

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo/testkit/scripts/darling-guest-shell.sh"
cat "$DARLING_HOMEBREW_LZ4_INPUTS"

guest_program=$(cat <<'GUEST'
set -euo pipefail
work=$1
phase=$2
brew=/usr/local/bin/brew
ruby=/usr/local/Homebrew/Library/Homebrew/vendor/portable-ruby/current/bin/ruby
cc=/Library/Developer/CommandLineTools/usr/bin/clang
export HOME="$work/home" HOMEBREW_CACHE="$work/cache" HOMEBREW_LOGS="$work/logs"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_FROM_API=1 HOMEBREW_NO_ANALYTICS=1
export HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_FORCE_VENDOR_RUBY=1
export PATH=/usr/local/Homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
export DEVELOPER_DIR=/Library/Developer/CommandLineTools

printf 'WEST_GUEST_STAGE=homebrew-lz4-%s\n' "$phase"
printf 'HOMEBREW_LZ4 execution-context=guest phase=%s\n' "$phase"
if [ "$phase" = install ]; then
    "$brew" --version
    test "$("$brew" --prefix)" = /usr/local
    "$ruby" --version
    "$cc" --version
    "$brew" config
    # The fresh resource contains neither a Cellar nor an installed lz4 bottle.
    test ! -e /usr/local/Cellar/lz4
    "$brew" install --build-from-source lz4
    "$brew" list --versions lz4
elif [ "$phase" = reuse ]; then
    test -f "$work/install-roundtrip.ok"
else
    printf 'unknown Homebrew lz4 scenario phase: %s\n' "$phase" >&2
    exit 1
fi

# Inspect the actual installation receipt and Mach-O executable, using the same
# official portable Ruby that stock brew selected. A bottle is not source proof.
"$ruby" -rjson -e '
    receipt = JSON.parse(File.read("/usr/local/Cellar/lz4/1.10.0/INSTALL_RECEIPT.json"))
    abort "lz4 was poured from a bottle" unless receipt.fetch("poured_from_bottle") == false
    abort "lz4 was built as a bottle" unless receipt.fetch("built_as_bottle") == false
    header = File.binread("/usr/local/bin/lz4", 8).unpack("V2")
    abort "installed lz4 is not x86_64 Mach-O" unless header == [0xfeedfacf, 0x01000007]
    puts "LZ4_SOURCE_RECEIPT_OK version=1.10.0 poured_from_bottle=false built_as_bottle=false arch=x86_64"
'
"/usr/local/bin/lz4" --version
if [ "$phase" = install ]; then
    "$ruby" -e 'File.binwrite(ARGV.fetch(0), (0..255).to_a.pack("C*") * 1024)' "$work/original.bin"
fi
"/usr/local/bin/lz4" -f "$work/original.bin" "$work/$phase.lz4"
"/usr/local/bin/lz4" -d -f "$work/$phase.lz4" "$work/$phase.decoded.bin"
cmp "$work/original.bin" "$work/$phase.decoded.bin"
"$ruby" -rdigest -e 'puts "LZ4_ROUNDTRIP_OK phase=#{ARGV.fetch(0)} sha256=#{Digest::SHA256.file(ARGV.fetch(1)).hexdigest}"' "$phase" "$work/$phase.decoded.bin"
printf '%s\n' "$phase" > "$work/$phase-roundtrip.ok"
GUEST
)

# Guest tool identities and policy come only from the staged inputs and deployed
# runtime, not an inherited host Homebrew, Ruby, compiler or package-manager env.
transport='exec /usr/bin/env -i /bin/bash -c "$3" west-homebrew-lz4 "$1" "$2"'
if [ "${DARLING_HOMEBREW_LZ4_RESTORED:-0}" = 1 ]; then
    # The stock-stack cache restored a completed stack: the Cellar already holds
    # the source-built lz4, the receipt and the install round trip marker, so the
    # from-source install phase is unnecessary and its fresh-Cellar precondition
    # no longer holds. The reuse phase below is the check that still applies.
    printf '%s\n' 'HOMEBREW_LZ4_STOCK_STACK_RESTORED source-install-skipped'
else
    darling_guest_shell "$DARLING_LAUNCHER" "$DPREFIX" 1200 \
        "$transport" west-homebrew-lz4 "$DARLING_HOMEBREW_LZ4_WORK" install "$guest_program"

    # Use the West-provided launcher lifecycle, never kill processes or repair
    # state. A failed install cannot reach shutdown/reuse success or the marker.
    timeout --kill-after=5 60 "$DARLING_LAUNCHER" shutdown
fi
darling_guest_shell "$DARLING_LAUNCHER" "$DPREFIX" 300 \
    "$transport" west-homebrew-lz4 "$DARLING_HOMEBREW_LZ4_WORK" reuse "$guest_program"
timeout --kill-after=5 60 "$DARLING_LAUNCHER" shutdown
printf '%s\n' HOMEBREW_LZ4_SOURCE_INSTALL_ROUNDTRIP_REUSE_OK

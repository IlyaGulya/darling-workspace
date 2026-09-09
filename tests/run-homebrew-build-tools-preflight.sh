#!/usr/bin/env bash
set -euo pipefail

: "${DPREFIX:?west must provide the test prefix}"
: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo/testkit/scripts/darling-guest-shell.sh"

guest_program=$(cat <<'GUEST'
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
printf '%s\n' 'WEST_GUEST_STAGE=homebrew-build-tools-preflight'
test "$(printf '1.2.3\n' | /usr/bin/cut -d . -f 2)" = 2
for perl in /usr/bin/perl /usr/bin/perl5.18 /usr/bin/perl5.28; do
    "$perl" -Mstrict -Mwarnings -MConfig -MPOSIX -MFcntl -MSocket -MDigest::SHA=sha256_hex -e '
        die "bad SHA256\n" unless sha256_hex("abc") eq
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        die "bad POSIX floor\n" unless POSIX::floor(3.75) == 3;
        die "bad Socket conversion\n" unless
            Socket::inet_ntoa(Socket::inet_aton("127.0.0.1")) eq "127.0.0.1";
        print "HOMEBREW_PERL_XS_OK executable=$^X version=$] arch=$Config{archname}\n";
    '
done
/usr/bin/openssl version
/usr/bin/openssl x509 -in /etc/ssl/cert.pem -noout -fingerprint -sha256 |
    /usr/bin/perl -e '
        local $/; my $value = <STDIN>;
        die "missing certificate fingerprint\n" unless defined($value) &&
            $value =~ /\ASHA256 Fingerprint=(?:[0-9a-f]{2}:){31}[0-9a-f]{2}\s*\z/i;
        print "HOMEBREW_SYSTEM_CERTIFICATE_OK $value";
    '
printf '%s\n' HOMEBREW_BUILD_TOOLS_PREFLIGHT_OK
GUEST
)
log=$(mktemp)
trap 'rm -f -- "$log"' EXIT
status=0
# Prefix daemons may retain stdout after the launcher exits; do not wait for
# their pipe EOF in command substitution.
darling_guest_shell "$DARLING_LAUNCHER" "$DPREFIX" 120 \
    'exec /usr/bin/env -i /bin/bash -c "$1"' west-homebrew-preflight "$guest_program" >"$log" 2>&1 || status=$?
output=$(<"$log")
printf '%s\n' "$output"
# Do not accept a launcher exit status as proof that the guest reached the end.
case $'\n'"$output"$'\n' in
    *$'\nHOMEBREW_BUILD_TOOLS_PREFLIGHT_OK\n'*) ;;
    *) printf '%s\n' 'Homebrew build-tools preflight did not reach its guest verdict' >&2; exit 1 ;;
esac
exit "$status"

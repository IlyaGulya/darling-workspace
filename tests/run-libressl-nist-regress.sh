#!/usr/bin/env bash
set -euo pipefail

root="${LIBRESSL_SRC_ROOT:-}"
if [ -z "$root" ]; then
	root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)/../darling/src/external/libressl-2.8.3"
fi

fail() {
	printf 'libressl_nist_regress: %s\n' "$*" >&2
	exit 1
}

[ -d "$root" ] || fail "source root not found: $root"
[ -f "$root/tests/darling_ec_tls_regress.c" ] ||
	fail "darling_ec_tls_regress.c is missing from source tree"

tmp="$(mktemp -d /tmp/libressl-nist-regress.XXXXXX)"
cleanup() {
	rm -rf "$tmp"
}
trap cleanup EXIT

git -C "$root" archive --format=tar HEAD | tar -C "$tmp" -xf -
cd "$tmp"

./configure --disable-shared --enable-static --disable-asm \
	CFLAGS="${LIBRESSL_NIST_CFLAGS:--O2 -fno-strict-aliasing}" >"$tmp/configure.log"
make -j"${LIBRESSL_NIST_JOBS:-2}" >"$tmp/make.log"
cc -O2 tests/darling_ec_tls_regress.c -Iinclude -Icrypto -Lcrypto/.libs -lcrypto \
	-o "$tmp/darling_ec_tls_regress"
"$tmp/darling_ec_tls_regress"

printf 'LIBRESSL_NIST_REGRESS_OK\n'

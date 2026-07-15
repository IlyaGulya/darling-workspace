#!/usr/bin/env bash
set -euo pipefail

: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"

"$DARLING_LAUNCHER" shell /bin/bash -c '
set -eu
cc=/Library/Developer/CommandLineTools/usr/bin/clang
sdk=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk
source=/private/var/tmp/west-clt-proof.c
binary=/private/var/tmp/west-clt-proof
version=/private/var/tmp/west-clt-proof.clang-version
origin=/private/var/tmp/west-clt-proof.clang-origin
marker=/private/var/tmp/west-clt-proof.marker
clang_version="$("$cc" --version)"
printf "%s\n" "$clang_version" > "$version"
printf "%s\n" "execution-context=guest" "executable=$cc" > "$origin"
printf "%s\n" "$clang_version"
printf "%s\n" "int main(void) { return 0; }" > "$source"
"$cc" -isysroot "$sdk" "$source" -o "$binary"
"$binary"
printf "%s\n" GUEST_TOOLCHAIN_PROOF_OK > "$marker"
'

# Shutdown is a host-side launcher operation.  The guest shell must not be
# trusted to know the host's launcher path or to report a successful shutdown.
"$DARLING_LAUNCHER" shutdown
printf "%s\n" GUEST_TOOLCHAIN_PROOF_OK

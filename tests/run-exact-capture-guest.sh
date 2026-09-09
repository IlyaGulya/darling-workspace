#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo/testkit/scripts/darling-guest-shell.sh"
darling_guest_shell "${DARLING_LAUNCHER:?}" "${DPREFIX:?}" 180 \
    'printf "WEST_GUEST_STAGE=exact-capture\nMACHO_EXACT_GUEST_READY\n"; exec /bin/sleep 120'

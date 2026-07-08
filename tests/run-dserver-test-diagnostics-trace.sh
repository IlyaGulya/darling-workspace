#!/usr/bin/env bash
set -euo pipefail

: "${DPREFIX:?set DPREFIX}"
darling="${DARLING:-$DPREFIX/bin/darling}"

timeout --kill-after=5 30 env DPREFIX="$DPREFIX" "$darling" shell /usr/bin/true

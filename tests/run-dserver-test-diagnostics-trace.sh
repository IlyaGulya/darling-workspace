#!/usr/bin/env bash
set -euo pipefail

: "${DPREFIX:?set DPREFIX}"
DARLING_WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$DARLING_WORKSPACE_ROOT/scripts/darling-runtime.sh"
darling_resolve_runtime "${DARLING_RUNTIME_ROOT:-}" "$DPREFIX"
darling="$DARLING_LAUNCHER"

timeout --kill-after=5 30 env DPREFIX="$DPREFIX" "$darling" shell /bin/bash --login -c /usr/bin/true

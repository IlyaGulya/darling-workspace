#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 -B "$repo/ci/verify_clt_provenance.py" "$@"

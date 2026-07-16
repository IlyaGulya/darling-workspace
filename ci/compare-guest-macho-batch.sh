#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
evidence_root="${1:?usage: $0 DOWNLOADED_BATCH_ARTIFACT_ROOT}"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root/ci:$root/west_commands" python3 -B \
	"$root/ci/compare-guest-macho-batch.py" "$evidence_root"


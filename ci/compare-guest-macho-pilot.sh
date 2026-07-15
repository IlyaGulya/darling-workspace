#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
evidence_root="${1:?usage: $0 DOWNLOADED_ARTIFACT_ROOT}"
PYTHONPATH="$root/west_commands" python3 -B \
	"$root/ci/compare-guest-macho-pilot.py" "$evidence_root"

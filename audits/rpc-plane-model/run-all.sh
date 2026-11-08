#!/bin/bash
# Shared isolated TLC runner; no generated files are written into the model tree.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$HERE/run_models.py" "$@"

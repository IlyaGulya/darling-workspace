#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runner="$here/native-transport.py"
[[ -f "$runner" ]] || runner="$here/native-runner.py"
command -v python3 >/dev/null 2>&1 || {
	echo "native infrastructure error: Python 3 is required" >&2
	exit 2
}
exec python3 "$runner" local "$@"

#!/usr/bin/env bash
set -euo pipefail

EUNION_NESTED_LOWER_ROOT=1 exec "$(cd "$(dirname "$0")" && pwd)/run.sh"

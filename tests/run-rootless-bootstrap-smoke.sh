#!/usr/bin/env bash
set -euo pipefail

: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"
: "${DPREFIX:?west must provide the test prefix}"

exec "$DARLING_LAUNCHER" shell /bin/bash --login -c \
	"printf '%s\\n' WEST_ROOTLESS_BOOTSTRAP_OK"

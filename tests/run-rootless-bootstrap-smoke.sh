#!/usr/bin/env bash
set -euo pipefail

: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"
: "${DPREFIX:?west must provide the test prefix}"

# The rootless launcher can start far enough to report readiness while launchd
# still has no daemon plist to load. Check the deployed guest-visible resource
# before invoking the shell so RED names that packaging failure deterministically
# instead of turning it into a long shellspawn timeout.
shellspawn_plist="${DPREFIX}/libexec/darling/System/Library/LaunchDaemons/org.darlinghq.shellspawn.plist"
if [[ ! -f "$shellspawn_plist" ]]; then
	printf 'ROOTLESS_LAUNCHD_RESOURCE_MISSING path=%s\n' "$shellspawn_plist" >&2
	exit 42
fi

exec "$DARLING_LAUNCHER" shell /bin/bash --login -c \
	"printf '%s\\n' WEST_ROOTLESS_BOOTSTRAP_OK"

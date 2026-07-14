#!/usr/bin/env bash
set -euo pipefail

: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"
: "${DPREFIX:?west must provide the test prefix}"

# The launcher must create these directories as part of the product bootstrap.
# This deliberately runs after a clean guest start, so a West-side directory
# preflight cannot make the test pass accidentally.
"$DARLING_LAUNCHER" shell /bin/bash -c '
set -eu
for path in \
  /private/var/db/launchd.db/com.apple.launchd \
  /private/tmp \
  /private/var/tmp \
  /var/run \
  /var/tmp \
  /tmp; do
  if ! test -d "$path"; then
    printf "ROOTLESS_PREFIX_INIT_MISSING %s\n" "$path"
    exit 1
  fi
done
'

for path in private/tmp private/var/tmp var/tmp tmp; do
  test "$(stat -c %a "$DPREFIX/$path")" = 1777
done
printf '%s\n' ROOTLESS_PREFIX_INIT_GUEST_OK

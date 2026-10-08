#!/usr/bin/env bash
# Focused guest-runtime contract for the per-call BSD return value invariant.
#
# One dispatched Darling call must report its OWN return value: Darwin's BSD dispatch creates a fresh
# `int32_t retval = 0` per syscall, so a trap body that writes the out-param only on its error path --
# the ULF_NO_ERRNO convention in duct-tape's ulock_wake -- must not hand the guest the previous call's
# value. The real Xcode 26.3 ld died of SIGILL on exactly that (libplatform's
# _os_unfair_lock_unlock_slow accepts only {0, -ENOENT, -EINTR}), which is why this gate exists.
#
# Mechanism: none of its own. It narrows the already-registered unprefix-lock contract to the single
# fixture that isolates the invariant, so both use the same host-recorded-command build, prefix
# lifecycle and verdict matching (see tests/run-ios-unfair-lock-contract.sh).
#
# Inputs (required): DARLING_BUILD_DIR (a configured product build) and DPREFIX/DARLING_PREFIX
# (a booted prefix whose darlingserver is under test). Same lane requirements as the unfair-lock
# contract: a prefix-backed lane, not the host tier.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
export IOS_UNFAIR_LOCK_FIXTURE="$here/ios_call_retval_guest.c"
exec bash "$here/run-ios-unfair-lock-contract.sh" "$@"

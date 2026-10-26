#!/usr/bin/env bash
# Shared low-level Darling guest command transport.

darling_guest_shell() (
	local launcher="$1"
	local prefix="$2"
	local timeout_seconds="$3"
	shift 3
	# Keep the controller's lease alive, but never let a runtime descendant
	# retain it after the controller exits.
	local lease_fd="${WEST_TEST_PREFIX_LEASE_FD:-8}"
	if [[ -n "${WEST_TEST_PREFIX_LEASE:-}" ]]; then
		[[ "$lease_fd" =~ ^[0-9]+$ ]] && [ "$lease_fd" -ge 3 ] || return 2
		exec {lease_fd}>&-
	fi
	unset WEST_TEST_PREFIX_LEASE WEST_TEST_PREFIX_LEASE_FD

	timeout --kill-after=5 "$timeout_seconds" \
		env "DPREFIX=$prefix" "DARLING_PREFIX=$prefix" \
		"$launcher" shell /bin/bash --login -c "$@"
)

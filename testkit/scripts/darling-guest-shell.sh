#!/usr/bin/env bash
# Shared low-level Darling guest command transport.

darling_guest_shell() {
	local launcher="$1"
	local prefix="$2"
	local timeout_seconds="$3"
	shift 3

	timeout --kill-after=5 "$timeout_seconds" \
		env "DPREFIX=$prefix" "DARLING_PREFIX=$prefix" \
		"$launcher" shell /bin/bash --login -c "$@"
}

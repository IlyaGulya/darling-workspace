#!/usr/bin/env bash
# Shared Xcode test setup. The caller authorizes license acceptance for the selected
# Xcode installation. Use Xcode itself; never manufacture preference/receipt files.

darling_xcode_license_prepare() {
	if [ "$#" -ne 3 ]; then
		echo "usage: darling_xcode_license_prepare XCODEBUILD GUEST_SUDO EVIDENCE_DIR" >&2
		return 2
	fi
	local xcodebuild="$1" guest_sudo="$2" evidence_dir="$3" check_rc accept_rc verify_rc
	mkdir -p "$evidence_dir" || return 2
	printf 'xcodebuild=%s\nguest_sudo=%s\n' "$xcodebuild" "$guest_sudo" > "$evidence_dir/status.txt" || return 2

	if "$xcodebuild" -license check > "$evidence_dir/check-before.log" 2>&1; then
		check_rc=0
	else
		check_rc=$?
	fi
	printf 'check_before_rc=%s\n' "$check_rc" >> "$evidence_dir/status.txt" || return 2
	if [ "$check_rc" -eq 0 ]; then
		echo "XCODE-LICENSE action=already-accepted"
		echo "XCODE_LICENSE_READY=1"
		return 0
	fi

	# Darling's sudo changes guest credentials only and preserves DEVELOPER_DIR.
	# Never run a host sudo or synthesize Xcode's license preferences.
	if "$guest_sudo" -n "$xcodebuild" -license accept > "$evidence_dir/accept.log" 2>&1; then
		accept_rc=0
	else
		accept_rc=$?
	fi
	printf 'accept_rc=%s\n' "$accept_rc" >> "$evidence_dir/status.txt" || return 2
	if [ "$accept_rc" -ne 0 ]; then
		cat "$evidence_dir/accept.log" >&2
		echo "XCODE-LICENSE action=accept-failed rc=$accept_rc" >&2
		return "$accept_rc"
	fi

	if "$xcodebuild" -license check > "$evidence_dir/check-after.log" 2>&1; then
		verify_rc=0
	else
		verify_rc=$?
	fi
	printf 'check_after_rc=%s\n' "$verify_rc" >> "$evidence_dir/status.txt" || return 2
	if [ "$verify_rc" -ne 0 ]; then
		cat "$evidence_dir/check-after.log" >&2
		echo "XCODE-LICENSE action=verification-failed rc=$verify_rc" >&2
		return "$verify_rc"
	fi

	echo "XCODE-LICENSE action=accepted-and-verified"
	echo "XCODE_LICENSE_READY=1"
}

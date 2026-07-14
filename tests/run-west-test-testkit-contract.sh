#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

scope_build="$(mktemp -d)"
trap 'rm -rf "$scope_build"' EXIT

export PYTHONDONTWRITEBYTECODE=1

tests/run-darling-c-test-contract.sh
tests/run-west-test-runtime-build-contract.sh
tests/run-west-guest-toolchain-contract.sh

# Exercise West's real extension loader.  It does not import command modules
# like ordinary Python packages, so this catches loader-incompatible module
# declarations before any CTest discovery runs.
west test --help | grep -q -- '--bootstrap-runtime-profile NAME'
west patch verify --help | grep -q -- '--applicability-only'

# Guest CTest builds configure the unpatched West source tree before a runtime
# profile is materialized. The source-bound E-UNION host harness must therefore
# stay out of the default build; metadata eunion-host invocations enable it in
# their separate source-override build.
cmake -S testkit -B "$scope_build" -G Ninja \
	-DDARLING_ENABLE_EUNION_HOST_SUITE=OFF >/dev/null
if ctest --test-dir "$scope_build" --show-only=json-v1 2>/dev/null |
	grep -F -q 'host/eunion_hardening_host_suite'; then
	printf 'E-UNION host suite leaked into the default guest build\n' >&2
	exit 1
fi

# This is the semantic boundary of the CLT-backed rootless regression tier.
# Keep the exact set here so a broad label or CMake conditional cannot silently
# turn the full job into a one-test smoke.
ctest --test-dir "$scope_build" --show-only=json-v1 -L 'env:darling' |
	python3 -c '
import json
import sys
from pathlib import Path

payload = json.load(sys.stdin)
expected = [
    "darling/abort_with_payload_no_group_broadcast",
    "darling/select_fdset_guest",
    "darling/getattrlist_name_objtype_guest",
    "darling/darwin_priority_guest",
    "darling/socket_siocgifconf_guest",
    "darling/bzero_return_register_guest",
    "darling/sigexc_sa_restart_guest",
    "darling/sigexc_default_resend_self_guest",
    "darling/ulock_eintr_retry_guest",
    "darling/vchroot_pathnull_guard_guest",
    "darling/chown_disabled_null_guard_guest",
    "darling/fd_guard_ebadf_guest",
    "darling/fork_checkin_signal_storm_guest",
    "darling/rootless_no_mount_guest",
]
tests = payload.get("tests", [])
actual = [test["name"] for test in tests]
assert actual == expected, (actual, expected)
profiles = {}
for test in tests:
    labels = next(
        item["value"]
        for item in test.get("properties", [])
        if item.get("name") == "LABELS"
    )
    profiles[test["name"]] = next(
        label.removeprefix("runtime-profile:")
        for label in labels
        if label.startswith("runtime-profile:")
    )
assert len(profiles) == 14
runtime_profiles = __import__("yaml").safe_load(
    Path("testkit/runtime-profiles.yml").read_text()
)["runtime-profiles"]
for profile in {"homebrew", "homebrew-libplatform", "perf-darlingserver", "homebrew-rootless-no-mount"}:
    assert runtime_profiles[profile]["guest-toolchain"] == "darling-command-line-tools"
assert "guest-toolchain" not in runtime_profiles["homebrew-rootless-bootstrap-minimal"]
print("PASS rootless-regression-selection-contract")
'

if patch_profile_error="$(west test --patch darling/rootless-shellspawn-lifecycle.patch \
	--prefix-profile homebrew --list 2>&1)"; then
	printf '%s\n' "$patch_profile_error" >&2
	exit 1
fi
printf '%s\n' "$patch_profile_error" | grep -F -q -- \
	'--patch selects patch metadata and requires --profile; --prefix-profile selects only a Darling prefix' ||
	{ printf '%s\n' "$patch_profile_error" >&2; exit 1; }

bead="dar-dar6x4-perf-5dq.1"

list_by_bead="$(west test --bead "$bead" --list)"
printf '%s\n' "$list_by_bead" | grep -q 'host/mldr_thread_create_checkin_wait' ||
	{ printf '%s\n' "$list_by_bead" >&2; exit 1; }
printf '%s\n' "$list_by_bead" | grep -q 'host/mldr_thread_create_checkin_wait_spin_red' ||
	{ printf '%s\n' "$list_by_bead" >&2; exit 1; }

west test --bead "$bead"

list_by_submodule="$(west test --submodule darling --list)"
printf '%s\n' "$list_by_submodule" | grep -q 'host/mldr_thread_create_checkin_wait' ||
	{ printf '%s\n' "$list_by_submodule" >&2; exit 1; }
printf '%s\n' "$list_by_submodule" | grep -q 'host/mldr_thread_create_checkin_wait_spin_red' ||
	{ printf '%s\n' "$list_by_submodule" >&2; exit 1; }

list_guest="$(west test --bead dar-cps --env darling --list)"
printf '%s\n' "$list_guest" | grep -q 'darling/abort_with_payload_no_group_broadcast' ||
	{ printf '%s\n' "$list_guest" >&2; exit 1; }
dar_cps_json="$(ctest --test-dir testkit/build --show-only=json-v1 \
	-L 'bead:dar-cps' -L 'env:darling')"
printf '%s\n' "$dar_cps_json" | grep -q 'runtime-profile:homebrew' ||
	{ printf '%s\n' "$dar_cps_json" >&2; exit 1; }

list_select_guest="$(west test --bead dar-q95.3 --env darling --list)"
printf '%s\n' "$list_select_guest" | grep -q 'darling/select_fdset_guest' ||
	{ printf '%s\n' "$list_select_guest" >&2; exit 1; }

list_getattrlist_guest="$(west test --bead dar-e1j --env darling --list)"
printf '%s\n' "$list_getattrlist_guest" | grep -q 'darling/getattrlist_name_objtype_guest' ||
	{ printf '%s\n' "$list_getattrlist_guest" >&2; exit 1; }
getattrlist_json="$(ctest --test-dir testkit/build --show-only=json-v1 \
	-L 'bead:dar-e1j' -L 'env:darling')"
printf '%s\n' "$getattrlist_json" | grep -q 'runtime-profile:homebrew' ||
	{ printf '%s\n' "$getattrlist_json" >&2; exit 1; }

list_bzero_guest="$(west test --bead dar-q95.4 --env darling --list)"
printf '%s\n' "$list_bzero_guest" | grep -q 'darling/bzero_return_register_guest' ||
	{ printf '%s\n' "$list_bzero_guest" >&2; exit 1; }
bzero_json="$(ctest --test-dir testkit/build --show-only=json-v1 \
	-L 'bead:dar-q95.4' -L 'env:darling')"
printf '%s\n' "$bzero_json" | grep -q 'runtime-profile:homebrew-libplatform' ||
	{ printf '%s\n' "$bzero_json" >&2; exit 1; }

for bead in dar-q95.10 dar-q95.11 dar-q95.20 dar-gwn.6.4 dar-gwn.6 dar-gwn.1.6 dar-gyvb dar-6x4.1 dar-gwn.6.5; do
	guest_list="$(west test --bead "$bead" --env darling --list)"
	printf '%s\n' "$guest_list" | grep -q 'darling/' ||
		{ printf '%s\n' "$guest_list" >&2; exit 1; }
done

# The E-UNION host suite has source-base RED proof and must run against a
# materialized selected profile through the same CTest label used by GREEN.
eunion_metadata="$(west test --profile homebrew --patch xnu/eunion-hardening.patch --env host --list)"
printf '%s\n' "$eunion_metadata" | grep -q 'eunion-host' ||
	{ printf '%s\n' "$eunion_metadata" >&2; exit 1; }

printf 'PASS west-test-testkit-contract\n'

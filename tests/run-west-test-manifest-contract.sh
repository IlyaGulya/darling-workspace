#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
from west_commands.test_manifest import ManifestError, normalize_test_profile


profile = {
    "version": 1,
    "test-profiles": {
        "guest-runtime": {
            "kind": "guest",
            "coverage-tier": "runtime",
            "env": "darling",
            "diag": "bare",
            "runner": "guest-c-fixture",
            "repo": "darling-workspace",
            "requires": ["darling-prefix"],
            "compile-flags": ["-std=gnu11", "-Wall", "-Wextra", "-Werror"],
            "red": True,
            "red-proof": {
                "mode": "guest-runtime-deploy",
                "bad-profile": "current-minus-patch",
            },
        },
        "guest-runtime-guarded": {
            "extends": "guest-runtime",
            "diag": "guarded",
        },
    },
    "artifact-profiles": {
        "xnu-kernel": {
            "module": "darling/src/external/xnu",
            "build-targets": ["system_kernel"],
            "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
        },
        "dyld": {
            "module": "darling/src/external/dyld",
            "build-targets": ["src/external/dyld/dyld"],
            "deploy": ["usr/lib/dyld"],
        },
    },
    "patches": [
        {
            "path": "xnu/compact.patch",
            "module": "darling/src/external/xnu",
            "tests": [
                {
                    "use": "guest-runtime-guarded",
                    "name": "compact_guest",
                    "script": "tests/compact_guest.c",
                    "ok-marker": "COMPACT_OK",
                    "artifacts": ["xnu-kernel"],
                    "timeout-seconds": 45,
                    "red-proof": {
                        "current-minus-skip-patches": ["xnu/downstream.patch"],
                    },
                },
                {
                    "name": "verbose_host",
                    "kind": "contract",
                    "coverage-tier": "host",
                    "env": "host",
                    "runner": "script",
                    "script": "tests/run-verbose.sh",
                },
            ],
        }
    ],
}

normalized = normalize_test_profile(profile)
tests = normalized["patches"][0]["tests"]
compact = tests[0]
assert compact["kind"] == "guest", compact
assert compact["coverage-tier"] == "runtime", compact
assert compact["env"] == "darling", compact
assert compact["diag"] == "guarded", compact
assert compact["runner"] == "guest-c-fixture", compact
assert compact["repo"] == "darling-workspace", compact
assert compact["requires"] == ["darling-prefix"], compact
assert compact["compile-flags"] == ["-std=gnu11", "-Wall", "-Wextra", "-Werror"], compact
assert compact["red"] is True, compact
assert compact["script"] == "tests/compact_guest.c", compact
assert compact["ok-marker"] == "COMPACT_OK", compact
assert compact["timeout-seconds"] == 45, compact
assert compact["red-proof"]["mode"] == "guest-runtime-deploy", compact
assert compact["red-proof"]["bad-profile"] == "current-minus-patch", compact
assert compact["red-proof"]["current-minus-skip-patches"] == ["xnu/downstream.patch"], compact
assert compact["red-proof"]["runtime-artifacts"] == [
    {
        "module": "darling/src/external/xnu",
        "build-targets": ["system_kernel"],
        "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
    }
], compact
assert "use" not in compact and "extends" not in compact and "artifacts" not in compact, compact

verbose = tests[1]
assert verbose == profile["patches"][0]["tests"][1], verbose

try:
    normalize_test_profile(
        {
            "test-profiles": {"base": {"runner": "guest-c-fixture"}},
            "patches": [
                {
                    "path": "bad.patch",
                    "module": "darling",
                    "tests": [{"use": "base", "name": "bad", "artifacts": ["missing"]}],
                }
            ],
        }
    )
except ManifestError as error:
    assert "unknown artifact profile 'missing'" in str(error), error
else:
    raise AssertionError("missing artifact profile unexpectedly passed")

print("PASS west-test-manifest-contract")
PY

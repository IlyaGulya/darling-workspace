import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_manifest import ManifestError, normalize_test, normalize_test_profile


profile = {
    "version": 1,
    "test-profiles": {
        "guest-runtime": {
            "kind": "guest",
            "coverage-tier": "runtime",
            "runs": "guest",
            "diag": "bare",
            "runner": "guest-c-fixture",
            "repo": "darling-workspace",
            "compile-flags": ["-std=gnu11", "-Wall", "-Wextra", "-Werror"],
            "red-proof": "runtime",
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
    "resource-profiles": {
        "dcc-smoke-cache": {
            "kind": "dcc-cache",
            "source-module": "darling/src/external/darlingserver",
            "source-ref": "HEAD",
            "tools-dir": "tools/closure-cache",
            "builder": "dcc5-builder.c",
            "closure-list": "smoke2-list.txt",
            "install-root": "guest-visible",
            "env": "DARLING_DYLD_DCC2_PATH",
            "enable-env": "DARLING_DYLD_DCC2",
        },
        "guest-trace": {
            "kind": "host-trace-files",
            "oracle": True,
            "files": [
                {
                    "env": "WEST_TRACE",
                    "prefix-relative-path": "private/var/tmp/trace.log",
                    "contains": ["TRACE_OK"],
                }
            ],
        },
        "rpc-stats": {
            "kind": "host-stat-deltas",
            "fields": [{"path": "rpc.calls", "min-delta": 1}],
        },
    },
    "fixture-profiles": {
        "eunion-overlay": {
            "kind": "eunion-overlay",
            "template-files": [
                {
                    "guest-path": "/private/var/tmp/lower.txt",
                    "contents": "LOWER\n",
                }
            ],
            "upper-files": [
                {
                    "guest-path": "/private/var/tmp/upper.txt",
                    "contents": "UPPER\n",
                }
            ],
            "cleanup-dirs": ["/private/var/tmp/eunion-contract"],
            "verify-template-files-after": True,
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
                    "resources": ["guest-trace", "rpc-stats"],
                    "fixtures": ["eunion-overlay"],
                    "timeout-seconds": 45,
                    "red-proof": {
                        "current-minus-skip-patches": ["xnu/downstream.patch"],
                        "expect-output-contains": ["old runtime failure"],
                    },
                },
                {
                    "ctest": "bead:dar-q95.6",
                    "runs": "host",
                    "red-proof": "source",
                    "build-target": "darling_ec_tls_regress",
                    "runner": "darling-cmake-target-fixture",
                    "name": "ctest_alias",
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
assert compact["requires"] == ["darling-prefix", "darling-eunion-prefix"], compact
assert compact["compile-flags"] == ["-std=gnu11", "-Wall", "-Wextra", "-Werror"], compact
assert compact["red"] is True, compact
assert compact["script"] == "tests/compact_guest.c", compact
assert compact["ok-marker"] == "COMPACT_OK", compact
assert compact["timeout-seconds"] == 45, compact
assert compact["red-proof"]["mode"] == "guest-runtime-deploy", compact
assert compact["red-proof"]["expect-failure-phase"] == "run", compact
assert compact["red-proof"]["bad-profile"] == "current-minus-patch", compact
assert compact["red-proof"]["current-minus-skip-patches"] == ["xnu/downstream.patch"], compact
assert compact["red-proof"]["expect-output-contains"] == ["old runtime failure"], compact
assert compact["red-proof"]["runtime-artifacts"] == [
    {
        "module": "darling/src/external/xnu",
        "build-targets": ["system_kernel"],
        "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
    }
], compact
assert compact["host-trace-oracle"] is True, compact
assert compact["host-trace-files"] == [
    {
        "env": "WEST_TRACE",
        "prefix-relative-path": "private/var/tmp/trace.log",
        "contains": ["TRACE_OK"],
    }
], compact
assert compact["host-stat-deltas"] == [{"path": "rpc.calls", "min-delta": 1}], compact
assert compact["eunion-template-files"] == [
    {"guest-path": "/private/var/tmp/lower.txt", "contents": "LOWER\n"}
], compact
assert compact["eunion-upper-files"] == [
    {"guest-path": "/private/var/tmp/upper.txt", "contents": "UPPER\n"}
], compact
assert compact["eunion-cleanup-dirs"] == ["/private/var/tmp/eunion-contract"], compact
assert compact["eunion-verify-template-files-after"] is True, compact
assert "darling-eunion-prefix" in compact["requires"], compact
assert (
    "use" not in compact
    and "extends" not in compact
    and "artifacts" not in compact
    and "resources" not in compact
    and "fixtures" not in compact
    and "runs" not in compact
)

mixed_red_runner = normalize_test(
    {
        "name": "mixed_red_runner",
        "kind": "guest",
        "runner": "guest-c-fixture",
        "script": "tests/current.c",
        "red": True,
        "red-proof": {
            "mode": "guest-runtime-deploy",
            "red-runner": {"runner": "script", "script": "tests/old.sh"},
            "runtime-artifacts": [
                {
                    "module": "darling/src/external/darlingserver",
                    "build-targets": ["darlingserver"],
                    "deploy": ["bin/darlingserver"],
                }
            ],
        },
    },
    {},
    {},
)
assert mixed_red_runner["red-proof"]["expect-failure-phase"] == "script", mixed_red_runner

ctest_alias = tests[1]
assert ctest_alias["coverage-tier"] == "host", ctest_alias
assert ctest_alias["ctest-label"] == "bead:dar-q95.6", ctest_alias
assert ctest_alias["env"] == "host", ctest_alias
assert ctest_alias["red"] is True, ctest_alias
assert ctest_alias["red-proof"] == {
    "mode": "source-base",
    "expect-failure-phase": "configure",
}, ctest_alias
assert ctest_alias["target"] == "darling_ec_tls_regress", ctest_alias
assert "ctest" not in ctest_alias and "build-target" not in ctest_alias

source_default = normalize_test(
    {
        "kind": "contract",
        "runner": "source-contract-script",
        "script": "tests/source-contract.sh",
    },
    {},
    {},
)
assert source_default["kind"] == "source-contract", source_default
assert source_default["coverage-tier"] == "source", source_default

source_behavior_override = normalize_test(
    {
        "kind": "contract",
        "coverage-tier": "host",
        "runner": "source-contract-script",
        "script": "tests/behavior-contract.sh",
    },
    {},
    {},
)
assert source_behavior_override["kind"] == "contract", source_behavior_override
assert source_behavior_override["coverage-tier"] == "host", source_behavior_override

verbose = tests[2]
assert verbose == profile["patches"][0]["tests"][2], verbose

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

try:
    normalize_test_profile(
        {
            "patches": [
                {
                    "path": "bad.patch",
                    "module": "darling",
                    "tests": [{"name": "bad", "needs": ["xnu-kernel"]}],
                }
            ],
        }
    )
except ManifestError as error:
    assert "needs is not part of the compact test DSL" in str(error), error
else:
    raise AssertionError("needs unexpectedly passed")

print("PASS west-test-manifest-contract")

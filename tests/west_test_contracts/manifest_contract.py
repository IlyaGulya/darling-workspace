"""Flat workload schema and retained structured runner inputs."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_manifest import ManifestError, load_workloads, normalize_workloads


fixture = {
    "version": 1,
    "workloads": [
        {
            "id": "guest-trace", "name": "same-name", "module": "darling/src/external/darlingserver",
            "bead": "dar-a", "kind": "guest", "env": "darling", "diag": "guarded",
            "runner": "guest-c-fixture", "repo": "darling-workspace",
            "script": "tests/guest.c", "args": ["--mode", "full"],
            "ok-marker": "GUEST_OK", "timeout-seconds": 45,
            "compile-flags": ["-std=gnu11", "-Wall", "-Werror"],
            "requires": ["darling-prefix", "darling-eunion-prefix", "homebrew-lz4"],
            "guest-env-vars": {"TRACE": "1"}, "env-vars": {"INPUT": "value"},
            "host-temp-files": [{"env": "FAULT", "prefix-relative-path": "private/var/tmp/fault"}],
            "host-trace-oracle": True,
            "host-trace-files": [{"env": "TRACE", "prefix-relative-path": "private/var/tmp/trace", "contains": ["TRACE_OK"]}],
            "host-stat-deltas": [{"path": "rpc.calls", "min-delta": 1}],
            "eunion-template-files": [{"guest-path": "/private/var/tmp/lower", "contents": "LOWER\n"}],
            "eunion-template-symlinks": [{"guest-path": "/private/var/tmp/link", "target": "lower"}],
            "eunion-upper-files": [{"guest-path": "/private/var/tmp/upper", "contents": "UPPER\n"}],
            "eunion-cleanup-dirs": ["/private/var/tmp/fixture"],
            "eunion-forbid-template-paths": ["/private/var/tmp/created"],
            "eunion-require-upper-paths": ["/private/var/tmp/created"],
            "eunion-verify-template-files-after": True,
        },
        {"id": "host-model", "name": "same-name", "module": "darling", "runner": "c-fixture",
         "coverage-tier": "model", "red": True,
         "red-proof": {"mode": "self", "expect-output-contains": ["OLD_MODEL_REJECTED", "MODEL_OK"]}},
        {"id": "source-script", "module": "darling/src/external/darlingserver",
         "runner": "source-script-fixture", "source-script": "tests/current.sh",
         "source-env": "DSERVER_SRC_ROOT", "cases": [{"name": "default", "args": ["full"], "stdout": "OK\n"}]},
        {"id": "ctest", "module": "darling", "ctest-label": "bead:dar-a", "ctest-name": "host/regression"},
    ],
    "aliases": {"example": ["guest-trace", "host-model"]},
}
normalized = normalize_workloads(fixture)
assert normalized["workloads"][0] == {**fixture["workloads"][0], "coverage-tier": "runtime"}
assert normalized["workloads"][1]["red-proof"] == fixture["workloads"][1]["red-proof"]
assert normalized["workloads"][2]["coverage-tier"] == "source"
assert "env" not in normalized["workloads"][3], "CTest registration owns its environment variants"
assert "diag" not in normalized["workloads"][3], "CTest registration owns its diagnostic variants"
assert "env" not in fixture["workloads"][1], "normalization must not mutate inputs"

with tempfile.TemporaryDirectory(prefix="west-workloads-contract-") as temporary:
    path = Path(temporary) / "workloads.yml"
    path.write_text("version: 1\nworkloads:\n- id: current\n  module: darling\n  ctest-label: smoke\n")
    loaded = load_workloads(path)
    assert loaded["workloads"][0]["id"] == "current"
    assert loaded["aliases"] == {}


def rejects(data):
    try:
        normalize_workloads(data)
    except ManifestError:
        return
    raise AssertionError(f"accepted invalid workload catalog: {data!r}")


for field in ("patches", "source-base", "source-commit", "source-revision", "source-ref", "source-profile",
              "runtime-profile", "runtime-profiles", "requires-profile", "runtime-artifacts", "source-patches",
              "test-profiles", "artifact-profiles", "resource-profiles", "fixture-profiles",
              "current-minus-skip-patches", "ctest-source-override", "runtime-mode", "runtime-root",
              "bootstrap", "launcher-env", "guest-toolchain", "source-worktree"):
    invalid = deepcopy(fixture)
    invalid["workloads"][0][field] = "forbidden"
    rejects(invalid)
    invalid = deepcopy(fixture)
    invalid["workloads"][0]["dcc-cache"] = {field: "nested authority"}
    rejects(invalid)

for proof in ({"mode": "source-base"}, {"mode": "guest-runtime-deploy"}, "runtime"):
    invalid = deepcopy(fixture)
    invalid["workloads"][1]["red-proof"] = proof
    rejects(invalid)

invalid = deepcopy(fixture)
invalid["workloads"][1]["id"] = "guest-trace"
rejects(invalid)
for alias_members in ([], ["missing"], ["host-model", "host-model"]):
    invalid = deepcopy(fixture)
    invalid["aliases"]["example"] = alias_members
    rejects(invalid)
invalid = deepcopy(fixture)
invalid["workloads"][2]["cases"] = []
rejects(invalid)
invalid = deepcopy(fixture)
invalid["workloads"][3]["command"] = "override-registration"
rejects(invalid)

for field, value in (
    ("requires-build-features", ["DARLING_RING_TRANSPORT"]),
    ("requires-build-features", {"bad-feature-name": True}),
    ("requires-build-features", {"DSERVER_TOOLS": []}),
    ("requires-installed-paths", "bin/dserverdbg"),
    ("requires-installed-paths", ["/bin/dserverdbg"]),
    ("requires-installed-paths", ["../foreign"]),
):
    invalid = deepcopy(fixture)
    invalid["workloads"][0][field] = value
    rejects(invalid)
for mode in ("DARLING_ROOTLESS", "DARLING_NOOVERLAYFS", "DARLING_EUNION"):
    invalid = deepcopy(fixture)
    invalid["workloads"][0]["env-vars"][mode] = "1"
    rejects(invalid)

controller = {
    "id": "controller", "module": "darling-workspace",
    "runner": "guest-runtime-script", "script": "owner.sh",
    "env": "darling", "requires": ["darling-prefix"], "prefix-controller": True,
}
normalize_workloads({"version": 1, "workloads": [controller]})
for changed in (
    {"prefix-controller": "yes"}, {"env": "host"}, {"env": "macos"},
    {"requires": []}, {"requires": 1}, {"requires": [{}]},
    {"runner": "guest-c-fixture"}, {"runner": "guest-argv-fixture"},
    {"runner": "ctest"}, {"command": "opaque command"},
):
    rejects({"version": 1, "workloads": [{**controller, **changed}]})
for reserved in ("WEST_TEST_PREFIX_LEASE", "WEST_TEST_PREFIX_LEASE_FD"):
    for field in ("env-vars", "guest-env-vars"):
        rejects({"version": 1, "workloads": [{**controller, field: {reserved: "forged"}}]})

catalog = load_workloads(ROOT / "testkit/workloads.yml")
workloads = catalog["workloads"]
assert len({test["id"] for test in workloads}) == len(workloads)
print("PASS west-test-manifest-contract")

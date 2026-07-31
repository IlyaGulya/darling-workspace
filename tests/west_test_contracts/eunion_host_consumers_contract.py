#!/usr/bin/env python3
"""Wiring contract for the four E-UNION host consumers in ordinary CI.

The consumers themselves are behavioral RED-to-GREEN tests.  This contract
keeps their metadata, current retained-prefix-FD API, and broad host-tier
selection tied together so a stale harness cannot silently leave CI coverage.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    profile = yaml.safe_load((ROOT / "patches/homebrew/patches.yml").read_text())
    tests = {
        test["name"]: test
        for patch in profile["patches"]
        for test in patch.get("tests", [])
    }
    expected = {
        "eunion_core_host_suite": (
            "source-contract-script",
            "experiments/e-union/run.sh",
        ),
        "eunion_nested_lower_root_host_suite": (
            "source-contract-script",
            "experiments/e-union/run-nested-lower-root.sh",
        ),
        "fd_guard_ebadf_contract": (
            "c-fixture",
            "tests/fd_guard_ebadf_contract.c",
        ),
        "eunion_af_unix_path_length_host": (
            "source-contract-script",
            "tests/run-eunion-af-unix-path-length-host.sh",
        ),
    }
    for name, (runner, script) in expected.items():
        test = tests[name]
        assert test["runs"] == "host", name
        assert test["runner"] == runner, name
        assert test["script"] == script, name
        assert test["red"] is True, name
        assert test["red-proof"]["mode"] == "source-base", name
        assert test["red-proof"]["source-env"] == "XNU_SRC_ROOT", name

    runner_source = (ROOT / "experiments/e-union/runner.c").read_text()
    af_source = (ROOT / "tests/eunion_af_unix_path_length_host.c").read_text()
    af_runner = (ROOT / "tests/run-eunion-af-unix-path-length-host.sh").read_text()
    nested_runner = (ROOT / "experiments/e-union/run-nested-lower-root.sh").read_text()
    fd_guard_errno = (
        ROOT / "tests/fixtures/fd-guard/include/darling/emulation/conversion/duct_errno.h"
    ).read_text()

    for source in (runner_source, af_source):
        assert not re.search(r"eunion_init_from_prefix\s*\(\s*\)", source)
        assert "eunion_init_from_prefix(prefix_descriptor)" in source
        assert "eunion_sidecar_runtime_release" in source
        assert "F_GETFD" in source and "EBADF" in source
        assert "O_NOFOLLOW" in source
    assert "eunion_sidecar.c" in af_runner
    assert '"$emulation/tests"' in af_runner
    assert "EUNION_NESTED_LOWER_ROOT=1" in nested_runner
    assert "#include <errno.h>" in fd_guard_errno

    tier = (ROOT / "ci/run-test-tier.sh").read_text()
    host = tier.split("\thost)\n", 1)[1].split("\tguest-smoke)", 1)[0]
    assert "tests/run-eunion-host-consumers-contract.sh" in host
    assert "exec west test --profile homebrew --env host --materialize-profile" in host
    assert "--patch" not in host and "--label" not in host
    print("E-UNION host consumers contract: PASS (4 current-API consumers)")


if __name__ == "__main__":
    main()

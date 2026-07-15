"""Contracts for the guest-toolchain-only ccache integration."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")
west_commands_module.WestCommand = object
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from test_runtime import compose_ctest_runtime_profiles, load_ctest_runtime_profiles
from test_runtime_build import RuntimeBuildService
from test_execution import ProcessResult


class Host:
    topdir = "/tmp"

    def __init__(self, build_dir: Path):
        self._active_profile = None
        self.build_dir = build_dir

    def inf(self, _message):
        pass

    def err(self, _message):
        pass


def assert_profile_contract() -> None:
    profiles = load_ctest_runtime_profiles(ROOT / "testkit/runtime-profiles.yml")
    toolchain = profiles["homebrew-guest-toolchain-provisioning"]
    assert toolchain["compiler-launcher"] == "ccache"
    for name, profile in profiles.items():
        if name != "homebrew-guest-toolchain-provisioning":
            assert "compiler-launcher" not in profile, name
    composed = compose_ctest_runtime_profiles(
        profiles, ["homebrew-guest-toolchain-provisioning"]
    )
    assert composed["compiler-launcher"] == "ccache"

    with tempfile.TemporaryDirectory(prefix="west-ccache-profile-contract-") as raw:
        path = Path(raw) / "profiles.yml"
        data = yaml.safe_load((ROOT / "testkit/runtime-profiles.yml").read_text())
        data["runtime-profiles"]["homebrew-guest-toolchain-provisioning"][
            "compiler-launcher"
        ] = "ccache /tmp/untrusted"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        try:
            load_ctest_runtime_profiles(path)
        except ValueError as error:
            assert "unsupported compiler-launcher" in str(error)
        else:
            raise AssertionError("arbitrary compiler launcher was accepted")

        data["runtime-profiles"]["homebrew"]["compiler-launcher"] = "ccache"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        try:
            load_ctest_runtime_profiles(path)
        except ValueError as error:
            assert "only for guest-toolchain-provisioning" in str(error)
        else:
            raise AssertionError("ccache was accepted outside guest-toolchain")


def assert_build_contract() -> None:
    def compiler_identity() -> dict[str, str]:
        result = {}
        for command, variable in (
            ("clang", "CLANG"),
            ("clang++", "CLANGXX"),
        ):
            resolved = Path(shutil.which(command)).resolve(strict=True)
            result[f"CCACHE_{variable}_PATH"] = str(resolved)
            result[f"CCACHE_{variable}_FINGERPRINT"] = hashlib.sha256(
                resolved.read_bytes()
            ).hexdigest()
        return result

    with tempfile.TemporaryDirectory(prefix="west-ccache-build-contract-") as raw:
        root = Path(raw)
        current_build = root / "current-build"
        current_build.mkdir()
        (current_build / "CMakeCache.txt").write_text(
            "CMAKE_GENERATOR:INTERNAL=Ninja\n"
            "CMAKE_C_COMPILER:FILEPATH=/usr/bin/clang\n"
            "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++\n"
        )
        identity = compiler_identity()
        with patch.dict(os.environ, identity, clear=False):
            os.environ.pop("DARLING_BUILD_DIR", None)
            host = Host(current_build)
            service = RuntimeBuildService(host)
            proof = {"compiler-launcher": "ccache", "cmake-defines": {}}
            scratch = root / "scratch"
            args = service.configure_args(proof, root / "prefix", scratch)
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                return ProcessResult(0)

            service.build_artifacts(
                root / "source",
                {**proof, "runtime-artifacts": [{"build-targets": ["fixture"]}]},
                root / "prefix",
                scratch,
                label="CCACHE",
                allow_failure=False,
                configure_args=service.configure_args,
                dump_command_tail=lambda *_args: None,
                runner=runner,
            )
            assert "-DCMAKE_C_COMPILER_LAUNCHER=ccache" in args
            assert "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache" in args
            assert f"-DCMAKE_C_COMPILER={identity['CCACHE_CLANG_PATH']}" in args
            assert f"-DCMAKE_CXX_COMPILER={identity['CCACHE_CLANGXX_PATH']}" in args
            assert [command[0] for command, _kwargs in calls] == ["cmake", "ninja"]
            assert all(
                kwargs["env"]["CCACHE_BASEDIR"] == str(scratch)
                for _command, kwargs in calls
            )
            c_flags = next(
                value for value in args if value.startswith("-DCMAKE_C_FLAGS=")
            )
            cxx_flags = next(
                value for value in args if value.startswith("-DCMAKE_CXX_FLAGS=")
            )
            for flags in (c_flags, cxx_flags):
                assert f"-fdebug-prefix-map={scratch}=." in flags
                assert f"-ffile-prefix-map={scratch}=." in flags
                assert "-fdebug-compilation-dir=." in flags
            environment = service.build_environment(proof, scratch)
            assert environment["CCACHE_BASEDIR"] == str(scratch)
            assert environment["CCACHE_HASHDIR"] == "true"
            assert environment["CCACHE_COMPILERCHECK"] == "content"
            assert "CCACHE_LOGFILE" not in environment

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DARLING_BUILD_DIR", None)
            for key in identity:
                os.environ.pop(key, None)
            try:
                service.configure_args(proof, root / "prefix", root / "scratch")
            except ValueError as error:
                assert "compiler identity is incomplete" in str(error)
            else:
                raise AssertionError("ccache accepted missing compiler identity")

        non_clang = {}
        for command, variable in (("gcc", "CLANG"), ("g++", "CLANGXX")):
            resolved = Path(shutil.which(command)).resolve(strict=True)
            non_clang[f"CCACHE_{variable}_PATH"] = str(resolved)
            non_clang[f"CCACHE_{variable}_FINGERPRINT"] = hashlib.sha256(
                resolved.read_bytes()
            ).hexdigest()
        with patch.dict(os.environ, non_clang, clear=False):
            os.environ.pop("DARLING_BUILD_DIR", None)
            try:
                service.configure_args(proof, root / "prefix", root / "scratch")
            except ValueError as error:
                assert "not Clang" in str(error)
            else:
                raise AssertionError("non-Clang compiler identity was accepted")

        ordinary_cache = root / "ordinary-build"
        ordinary_cache.mkdir()
        (ordinary_cache / "CMakeCache.txt").write_text(
            "CMAKE_GENERATOR:INTERNAL=Ninja\n"
            "CMAKE_C_COMPILER:FILEPATH=/usr/bin/gcc\n"
            "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/g++\n"
        )
        with patch.dict(os.environ, {"DARLING_BUILD_DIR": str(ordinary_cache)}, clear=False):
            ordinary_args = service.configure_args({}, root / "prefix")
            assert "-DCMAKE_C_COMPILER=/usr/bin/gcc" in ordinary_args
            assert "-DCMAKE_CXX_COMPILER=/usr/bin/g++" in ordinary_args
            assert service.build_environment({}, root / "scratch") is None


def assert_workflow_contract() -> None:
    workflow = (ROOT / ".github/workflows/test-infra.yml").read_text()
    helper = (ROOT / "ci/guest-toolchain-ccache.sh").read_text()
    smoke = workflow.split("  guest-smoke:", 1)[1].split(
        "  guest-toolchain-provisioning:", 1
    )[0]
    toolchain = workflow.split("  guest-toolchain-provisioning:", 1)[1].split(
        "  clt-integrity:", 1
    )[0]
    assert "ccache mode for guest-toolchain only" in workflow
    assert "ccache" not in smoke
    for text in (workflow, helper):
        assert "CCACHE_LOGFILE" not in text
    assert "actions/cache/restore@v4" in toolchain
    assert "actions/cache/save@v4" in toolchain
    assert "ccache --zero-stats" in toolchain
    assert "ccache --print-stats" in helper
    assert "steps.guest_ccache.outputs.restore_prefix" in toolchain
    assert "steps.guest_ccache.outputs.primary_key" in toolchain
    assert "inputs.ccache_mode == 'warm'" in toolchain
    assert "success() && inputs.ccache_mode == 'warm'" in toolchain
    assert toolchain.index("Cleanup test state and runtime evidence") < toolchain.index(
        "Save guest-toolchain ccache"
    )
    assert toolchain.index("Collect guest-toolchain ccache statistics") < toolchain.index(
        "Save guest-toolchain ccache"
    )
    assert "GITHUB_SHA" in helper
    for variable in (
        "CCACHE_CLANG_PATH",
        "CCACHE_CLANGXX_PATH",
        "CCACHE_CLANG_FINGERPRINT",
        "CCACHE_CLANGXX_FINGERPRINT",
    ):
        assert f"printf '{variable}=%s\\n'" in helper
    assert "readlink -f" in helper
    assert "RUNNER_OS" in helper
    assert "RUNNER_ARCH" in helper
    assert "compiler_fingerprint" in helper
    assert "ccache_compatibility" in helper
    assert "runtime_contract_fingerprint" in helper
    assert 'primary_key="${key_prefix}${GITHUB_SHA:-local}"' in helper
    assert 'printf \'restore_prefix=%s\\n\' "$key_prefix"' in helper
    assert "CCACHE_DIR=$HOME" not in helper
    assert "darling-guest-toolchain-ccache" in helper
    assert "${RUNNER_TEMP:" in helper


assert_profile_contract()
assert_build_contract()
assert_workflow_contract()
print("PASS guest-toolchain-ccache-contract")

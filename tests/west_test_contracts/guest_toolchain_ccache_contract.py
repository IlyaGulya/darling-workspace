"""Contracts for the guest-toolchain-only ccache integration."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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
    def compiler_identity(links_root: Path) -> dict[str, str]:
        result = {}
        records = []
        links_root.mkdir()
        shared_target = Path(shutil.which("clang")).resolve(strict=True)
        for command, variable in (
            ("clang", "CLANG"),
            ("clang++", "CLANGXX"),
        ):
            invocation = links_root / command
            invocation.symlink_to(shared_target)
            resolved = invocation.resolve(strict=True)
            result[f"CCACHE_{variable}_PATH"] = str(invocation)
            result[f"CCACHE_{variable}_RESOLVED_PATH"] = str(resolved)
            result[f"CCACHE_{variable}_FINGERPRINT"] = hashlib.sha256(
                resolved.read_bytes()
            ).hexdigest()
            version = subprocess.run(
                [str(invocation), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            records.append(
                {
                    "name": "CLANGXX" if variable == "CLANGXX" else "CLANG",
                    "path": str(invocation),
                    "resolved_path": str(resolved),
                    "fingerprint": result[f"CCACHE_{variable}_FINGERPRINT"],
                    "version_stdout": version.stdout,
                }
            )
        payload = "".join(
            f"{record['name'].lower()}_path={record['path']}\n"
            f"{record['name'].lower()}_resolved_path={record['resolved_path']}\n"
            f"{record['name'].lower()}_fingerprint={record['fingerprint']}\n"
            for record in records
        ) + "".join(record["version_stdout"] for record in records)
        result["CCACHE_COMPILER_FINGERPRINT"] = hashlib.sha256(
            payload.encode()
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
        identity = compiler_identity(root / "compiler-links")
        assert identity["CCACHE_CLANG_RESOLVED_PATH"] == identity[
            "CCACHE_CLANGXX_RESOLVED_PATH"
        ]
        assert identity["CCACHE_CLANG_PATH"] != identity["CCACHE_CLANGXX_PATH"]
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
            assert environment["CCACHE_COMPILERCHECK"] == (
                f"string:{identity['CCACHE_COMPILER_FINGERPRINT']}"
            )
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
            invocation = Path(shutil.which(command)).absolute()
            resolved = invocation.resolve(strict=True)
            non_clang[f"CCACHE_{variable}_PATH"] = str(invocation)
            non_clang[f"CCACHE_{variable}_RESOLVED_PATH"] = str(resolved)
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


def assert_prepare_probe_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="west-ccache-prepare-contract-") as raw:
        root = Path(raw)
        env = {
            **os.environ,
            "GITHUB_ENV": str(root / "github-env"),
            "GITHUB_OUTPUT": str(root / "github-output"),
            "GITHUB_SHA": "contract",
            "RUNNER_ARCH": "X64",
            "RUNNER_OS": "Linux",
            "RUNNER_TEMP": str(root),
        }
        result = subprocess.run(
            [str(ROOT / "ci/guest-toolchain-ccache.sh"), "prepare", "cold"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        output = (root / "github-output").read_text()
        values = dict(line.split("=", 1) for line in output.splitlines())
        for name in (
            "clang_path",
            "clangxx_path",
            "clang_resolved_path",
            "clangxx_resolved_path",
        ):
            assert Path(values[name]).is_absolute(), name
        assert values["clang_resolved_path"] == str(
            Path(values["clang_path"]).resolve(strict=True)
        )
        assert values["clangxx_resolved_path"] == str(
            Path(values["clangxx_path"]).resolve(strict=True)
        )
        assert values["clang_fingerprint"] == hashlib.sha256(
            Path(values["clang_resolved_path"]).read_bytes()
        ).hexdigest()
        assert values["clangxx_fingerprint"] == hashlib.sha256(
            Path(values["clangxx_resolved_path"]).read_bytes()
        ).hexdigest()
        assert len(values["compiler_fingerprint"]) == 64
        assert set(values["compiler_fingerprint"]) <= set("0123456789abcdef")
        env_values = dict(
            line.split("=", 1)
            for line in (root / "github-env").read_text().splitlines()
        )
        assert env_values["CCACHE_COMPILER_FINGERPRINT"] == values[
            "compiler_fingerprint"
        ]
        assert env_values["CCACHE_COMPILERCHECK"] == (
            f"string:{values['compiler_fingerprint']}"
        )

        if values["clang_resolved_path"] == values["clangxx_resolved_path"]:
            assert values["clang_path"] != values["clangxx_path"]


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
        "CCACHE_CLANG_RESOLVED_PATH",
        "CCACHE_CLANGXX_RESOLVED_PATH",
        "CCACHE_CLANG_FINGERPRINT",
        "CCACHE_CLANGXX_FINGERPRINT",
    ):
        assert f"printf '{variable}=%s\\n'" in helper
    assert "CCACHE_COMPILERCHECK=string:%s" in helper
    assert "CCACHE_COMPILER_FINGERPRINT=%s" in helper
    assert "} | sha256sum | cut -d' ' -f1)" in helper
    assert "readlink -f" in helper
    assert 'path="$(command -v "$compiler")"' in helper
    assert '"$clangxx_path" -std=c++11' in helper
    assert "std::string" in helper
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

    tiers = (ROOT / "ci/run-test-tier.sh").read_text()
    smoke = tiers.split("guest-smoke)", 1)[1].split("guest-toolchain)", 1)[0]
    toolchain = tiers.split("guest-toolchain)", 1)[1].split("macos)", 1)[0]
    assert "--runtime-build-timeout-seconds 600" in smoke
    assert "--runtime-build-timeout-seconds 1200" not in smoke
    assert "--runtime-build-timeout-seconds 1800" in toolchain
    assert "--runtime-build-timeout-seconds 1200" not in toolchain
    assert "timeout-minutes: 45" in workflow


assert_profile_contract()
assert_build_contract()
assert_prepare_probe_contract()
assert_workflow_contract()
print("PASS guest-toolchain-ccache-contract")

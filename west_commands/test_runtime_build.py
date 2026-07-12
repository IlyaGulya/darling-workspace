"""Build disposable runtime artifacts for ``west test`` deployments."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from test_execution import run_bounded
from test_results import RuntimeBuildFailure
from test_runtime import runtime_build_targets


class RuntimeBuildService:
    """Own CMake/Ninja runtime builds while the command facade owns policy."""

    def __init__(self, host: Any):
        self._host = host

    @staticmethod
    def cmake_cache_value(build_dir: Path, key: str) -> str | None:
        cache = build_dir / "CMakeCache.txt"
        if not cache.exists():
            return None
        prefix = f"{key}:"
        for line in cache.read_text(errors="replace").splitlines():
            if line.startswith(prefix):
                return line.split("=", 1)[1]
        return None

    def configure_args(self, proof: dict, prefix: Path) -> list[str]:
        current_build = Path(
            os.environ.get("DARLING_BUILD_DIR", str(Path.home() / "work/darling-build"))
        )
        args = ["-G", self.cmake_cache_value(current_build, "CMAKE_GENERATOR") or "Ninja"]
        cmake_defines = {"CMAKE_BUILD_TYPE": "Debug", **(proof.get("cmake-defines") or {})}
        active_profile = getattr(self._host, "_active_profile", None)
        if active_profile and "DARLING_PATCH_PROFILE" not in cmake_defines:
            cmake_defines["DARLING_PATCH_PROFILE"] = active_profile
        for key in ("CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER"):
            value = self.cmake_cache_value(current_build, key)
            if value:
                args.append(f"-D{key}={value}")
        inherited = set(proof.get("inherit-cmake-cache", []))
        if "all" in inherited:
            inherited.update({"DARLING_RING_TRANSPORT", "DSERVER_RING_TRANSPORT"})
        for key in (
            "DARLING_COREDUMP_SANITIZE",
            "DARLING_EUNION",
            "DARLING_GUEST_RECVSPIN",
            "DARLING_RPC_SLEEP_ACCOUNT",
        ):
            value = self.cmake_cache_value(current_build, key)
            if value is not None:
                args.append(f"-D{key}={value}")
        for key in ("DARLING_RING_TRANSPORT", "DSERVER_RING_TRANSPORT"):
            if key in inherited:
                value = self.cmake_cache_value(current_build, key)
                if value is not None:
                    args.append(f"-D{key}={value}")
            else:
                args.append(f"-D{key}=OFF")
        for key, value in sorted(cmake_defines.items()):
            if isinstance(value, bool):
                value = "ON" if value else "OFF"
            elif value is None:
                value = ""
            else:
                value = str(value)
            args.append(f"-D{key}={value}")
        args.append(f"-DCMAKE_INSTALL_PREFIX={prefix}")
        return args

    def dump_command_tail(self, label: str, result) -> None:
        streams = [stream for stream in (result.stdout, result.stderr) if stream]
        output = "\n".join(stream.rstrip("\n") for stream in streams)
        lines = output.splitlines()
        tail = "\n".join(lines[-200:])
        failed = [index for index, line in enumerate(lines) if line.startswith("FAILED:")]
        if failed:
            excerpt = "\n".join(lines[failed[-1] : failed[-1] + 80])
            if excerpt not in tail:
                tail = f"Actionable failure:\n{excerpt}\n\nCommand tail:\n{tail}"
        if tail:
            sys.stderr.write(tail + "\n")
        self._host.err(f"{label} failed with rc {result.returncode}")

    def build_artifacts(
        self,
        source_root: Path,
        proof: dict,
        prefix: Path,
        scratch_root: Path,
        *,
        label: str,
        allow_failure: bool,
        configure_args: Callable[[dict, Path], list[str]],
        dump_command_tail: Callable[[str, Any], None],
        runner: Callable[..., Any] = run_bounded,
    ) -> Path:
        targets = runtime_build_targets(proof)
        build_root = scratch_root / "build"
        timeout = int(proof.get("build-timeout-seconds", 1800))
        configured_at = time.monotonic()
        self._host.inf(f"  runtime phase start: {label} configure")
        self._host.inf(f"  {label} configure: {source_root} -> {build_root}")
        configured = runner(
            ["cmake", "-S", str(source_root), "-B", str(build_root), *configure_args(proof, prefix)],
            cwd=Path(self._host.topdir), env=None, timeout_seconds=timeout, capture_output=True,
        )
        if configured.returncode:
            dump_command_tail(f"{label} configure", configured)
            if allow_failure:
                raise RuntimeBuildFailure("configure", configured)
            self._host.die(f"{label} configure failed with rc {configured.returncode}")
        self._host.inf(f"  runtime phase complete: {label} configure ({time.monotonic() - configured_at:.1f}s)")
        built_at = time.monotonic()
        self._host.inf(f"  runtime phase start: {label} build")
        self._host.inf(f"  {label} build: {', '.join(targets)}")
        built = runner(
            ["ninja", "-C", str(build_root), *targets],
            cwd=Path(self._host.topdir), env=None, timeout_seconds=timeout, capture_output=True,
        )
        if built.returncode:
            dump_command_tail(f"{label} build", built)
            if allow_failure:
                raise RuntimeBuildFailure("build", built)
            self._host.die(f"{label} build failed with rc {built.returncode}")
        self._host.inf(f"  runtime phase complete: {label} build ({time.monotonic() - built_at:.1f}s)")
        return build_root

    def find_build_output(self, build_root: Path, deploy_path: str) -> Path:
        name = Path(deploy_path).name
        candidates = (
            path for path in build_root.rglob(name)
            if path.is_file() and "CMakeFiles" not in path.parts
        )
        best = max(candidates, key=lambda path: path.stat().st_mtime, default=None)
        if best is None:
            self._host.die(f"guest-runtime-deploy built artifact not found for {deploy_path}")
        return best

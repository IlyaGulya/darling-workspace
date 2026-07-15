"""Build disposable runtime artifacts for ``west test`` deployments."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from test_execution import run_bounded
from test_results import RuntimeBuildFailure
from test_runtime import COMPILER_LAUNCHERS, runtime_build_targets


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

    @staticmethod
    def _compiler_launcher(proof: dict) -> str | None:
        launcher = proof.get("compiler-launcher")
        if launcher is None:
            return None
        if not isinstance(launcher, str) or launcher not in COMPILER_LAUNCHERS:
            raise ValueError(f"unsupported runtime compiler launcher: {launcher!r}")
        return launcher

    @staticmethod
    def _compiler_file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _ccache_compiler_identity(cls) -> dict[str, str]:
        identity = {}
        for name, path_name, fingerprint_name in (
            ("clang", "CCACHE_CLANG_PATH", "CCACHE_CLANG_FINGERPRINT"),
            ("clang++", "CCACHE_CLANGXX_PATH", "CCACHE_CLANGXX_FINGERPRINT"),
        ):
            raw_path = os.environ.get(path_name)
            resolved_path_name = (
                "CCACHE_CLANG_RESOLVED_PATH"
                if name == "clang"
                else "CCACHE_CLANGXX_RESOLVED_PATH"
            )
            raw_resolved_path = os.environ.get(resolved_path_name)
            expected_fingerprint = os.environ.get(fingerprint_name)
            if not raw_path or not raw_resolved_path or not expected_fingerprint:
                raise ValueError(
                    "ccache compiler identity is incomplete; missing "
                    f"{path_name}, {resolved_path_name}, or {fingerprint_name}"
                )
            path = Path(raw_path)
            resolved_path = Path(raw_resolved_path)
            if not path.is_absolute():
                raise ValueError(
                    f"ccache compiler invocation path must be absolute: {path}"
                )
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(
                    f"ccache compiler invocation path is not executable: {path}"
                )
            try:
                actual_resolved_path = path.resolve(strict=True)
                declared_resolved_path = resolved_path.resolve(strict=True)
            except OSError as error:
                raise ValueError(
                    "ccache compiler path cannot be resolved: "
                    f"{path} -> {resolved_path}"
                ) from error
            if (
                not resolved_path.is_absolute()
                or declared_resolved_path != resolved_path
                or actual_resolved_path != resolved_path
            ):
                raise ValueError(
                    f"ccache compiler resolved path mismatch: {path} -> "
                    f"{resolved_path} (actual {actual_resolved_path})"
                )
            if not resolved_path.is_file() or not os.access(resolved_path, os.X_OK):
                raise ValueError(
                    f"ccache compiler resolved target is not executable: {resolved_path}"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint):
                raise ValueError(
                    f"ccache compiler fingerprint is not SHA-256: {fingerprint_name}"
                )
            actual_fingerprint = cls._compiler_file_sha256(resolved_path)
            if actual_fingerprint != expected_fingerprint:
                raise ValueError(
                    "ccache compiler fingerprint mismatch for "
                    f"{name}: {resolved_path}"
                )
            try:
                version = subprocess.run(
                    [str(path), "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise ValueError(f"could not verify ccache compiler {path}: {error}") from error
            version_text = f"{version.stdout}\n{version.stderr}".lower()
            if version.returncode or "clang" not in version_text:
                raise ValueError(f"ccache compiler is not Clang: {path}")
            identity[name] = str(path)
        return identity

    def configure_args(
        self, proof: dict, prefix: Path, scratch_root: Path | None = None
    ) -> list[str]:
        launcher = self._compiler_launcher(proof)
        compiler_paths = {}
        current_build = Path(
            os.environ.get("DARLING_BUILD_DIR", str(Path.home() / "work/darling-build"))
        )
        if launcher is not None:
            compiler_paths = self._ccache_compiler_identity()
        args = ["-G", self.cmake_cache_value(current_build, "CMAKE_GENERATOR") or "Ninja"]
        cmake_defines = {"CMAKE_BUILD_TYPE": "Debug", **(proof.get("cmake-defines") or {})}
        active_profile = getattr(self._host, "_active_profile", None)
        if active_profile and "DARLING_PATCH_PROFILE" not in cmake_defines:
            cmake_defines["DARLING_PATCH_PROFILE"] = active_profile
        if launcher is None:
            for key in ("CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER"):
                value = self.cmake_cache_value(current_build, key)
                if value:
                    compiler_paths[key] = value
                    args.append(f"-D{key}={value}")
        else:
            args.extend(
                [
                    f"-DCMAKE_C_COMPILER={compiler_paths['clang']}",
                    f"-DCMAKE_CXX_COMPILER={compiler_paths['clang++']}",
                ]
            )
        if launcher is not None:
            if scratch_root is None:
                raise ValueError(
                    "ccache runtime builds need their per-run scratch root"
                )
            flags = [
                f"-fdebug-prefix-map={scratch_root}=.",
                f"-ffile-prefix-map={scratch_root}=.",
            ]
            flags.append("-fdebug-compilation-dir=.")
            for key in ("CMAKE_C_FLAGS", "CMAKE_CXX_FLAGS"):
                current = str(cmake_defines.get(key, "")).strip()
                cmake_defines[key] = " ".join((current, *flags)).strip()
            args.extend(
                [
                    f"-DCMAKE_C_COMPILER_LAUNCHER={launcher}",
                    f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
                ]
            )
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

    def build_environment(self, proof: dict, scratch_root: Path) -> dict[str, str] | None:
        """Return per-run ccache state without changing the runtime layout."""

        if self._compiler_launcher(proof) is None:
            return None
        self._ccache_compiler_identity()
        environment = os.environ.copy()
        environment.update(
            {
                "CCACHE_BASEDIR": str(scratch_root),
                "CCACHE_HASHDIR": "true",
                "CCACHE_COMPILERCHECK": "content",
            }
        )
        environment.pop("CCACHE_LOGFILE", None)
        return environment

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

    def _forward_runtime_line(self, label: str, phase: str, stream: str, line: str) -> None:
        if phase == "configure":
            self._host.inf(f"  runtime {label} configure {stream}: {line}")
            return
        progress = re.match(r"^\[(\d+)/(\d+)\]", line)
        if progress:
            current, total = (int(value) for value in progress.groups())
            if current not in {1, total} and current % 25:
                return
        elif not any(
            marker in line
            for marker in ("FAILED:", "error:", "Error:", "ninja:")
        ):
            return
        self._host.inf(f"  runtime {label} build {stream}: {line}")

    def build_artifacts(
        self,
        source_root: Path,
        proof: dict,
        prefix: Path,
        scratch_root: Path,
        *,
        label: str,
        allow_failure: bool,
        configure_args: Callable[[dict, Path, Path], list[str]],
        dump_command_tail: Callable[[str, Any], None],
        runner: Callable[..., Any] = run_bounded,
        timeout_seconds: int | None = None,
    ) -> Path:
        targets = runtime_build_targets(proof)
        build_root = scratch_root / "build"
        timeout = int(
            timeout_seconds
            if timeout_seconds is not None
            else proof.get("build-timeout-seconds", 1800)
        )
        if timeout <= 0:
            raise ValueError("runtime build timeout must be greater than zero")
        build_environment = self.build_environment(proof, scratch_root)
        configured_at = time.monotonic()
        self._host.inf(f"  runtime phase start: {label} configure")
        self._host.inf(f"  {label} configure: {source_root} -> {build_root}")
        configured = runner(
            [
                "cmake",
                "-S",
                str(source_root),
                "-B",
                str(build_root),
                *configure_args(proof, prefix, scratch_root),
            ],
            cwd=Path(self._host.topdir),
            env=build_environment,
            timeout_seconds=timeout,
            capture_output=True,
            heartbeat_seconds=30,
            heartbeat=lambda elapsed: self._host.inf(
                f"  runtime heartbeat: {label} configure still running ({elapsed:.0f}s)"
            ),
            output_line=lambda stream, line: self._forward_runtime_line(
                label, "configure", stream, line
            ),
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
            cwd=Path(self._host.topdir),
            env=build_environment,
            timeout_seconds=timeout,
            capture_output=True,
            heartbeat_seconds=30,
            heartbeat=lambda elapsed: self._host.inf(
                f"  runtime heartbeat: {label} build still running ({elapsed:.0f}s)"
            ),
            output_line=lambda stream, line: self._forward_runtime_line(
                label, "build", stream, line
            ),
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

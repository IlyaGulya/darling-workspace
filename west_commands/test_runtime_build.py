"""Build disposable runtime artifacts for ``west test`` deployments."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from test_execution import run_bounded
from test_results import RuntimeBuildFailure
from test_runtime import (
    COMPILER_LAUNCHERS,
    ROOTLESS_BOOTSTRAP_RESOURCE,
    ROOTLESS_TOOLCHAIN_RESOURCE,
    load_runtime_component_manifest,
    runtime_artifact_deploy_paths,
    runtime_build_targets,
)


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
    def derive_ccache_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Bind ccache to the exact Clang invocation selected by PATH."""

        source = os.environ if environment is None else environment
        result: dict[str, str] = {}
        records: list[dict[str, str]] = []
        for name, variable in (("clang", "CLANG"), ("clang++", "CLANGXX")):
            raw_path = shutil.which(name, path=source.get("PATH"))
            if not raw_path:
                raise ValueError(f"ccache compiler is unavailable on PATH: {name}")
            path = Path(raw_path)
            if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(
                    f"ccache compiler invocation path is not executable: {path}"
                )
            try:
                resolved_path = path.resolve(strict=True)
            except OSError as error:
                raise ValueError(
                    f"ccache compiler path cannot be resolved: {path}"
                ) from error
            fingerprint = cls._compiler_file_sha256(resolved_path)
            try:
                version = subprocess.run(
                    [str(path), "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise ValueError(
                    f"could not verify ccache compiler {path}: {error}"
                ) from error
            if version.returncode or "clang" not in (
                f"{version.stdout}\n{version.stderr}".lower()
            ):
                raise ValueError(f"ccache compiler is not Clang: {path}")
            result[f"CCACHE_{variable}_PATH"] = str(path)
            result[f"CCACHE_{variable}_RESOLVED_PATH"] = str(resolved_path)
            result[f"CCACHE_{variable}_FINGERPRINT"] = fingerprint
            records.append(
                {
                    "name": "clangxx" if name == "clang++" else name,
                    "path": str(path),
                    "resolved_path": str(resolved_path),
                    "fingerprint": fingerprint,
                    "version_stdout": version.stdout,
                }
            )
        identity_payload = "".join(
            f"{record['name']}_path={record['path']}\n"
            f"{record['name']}_resolved_path={record['resolved_path']}\n"
            f"{record['name']}_fingerprint={record['fingerprint']}\n"
            for record in records
        ) + "".join(record["version_stdout"] for record in records)
        result["CCACHE_COMPILER_FINGERPRINT"] = hashlib.sha256(
            identity_payload.encode()
        ).hexdigest()
        return result

    @classmethod
    def _ccache_environment(cls) -> dict[str, str]:
        environment = os.environ.copy()
        names = {
            "CCACHE_CLANG_PATH",
            "CCACHE_CLANG_RESOLVED_PATH",
            "CCACHE_CLANG_FINGERPRINT",
            "CCACHE_CLANGXX_PATH",
            "CCACHE_CLANGXX_RESOLVED_PATH",
            "CCACHE_CLANGXX_FINGERPRINT",
            "CCACHE_COMPILER_FINGERPRINT",
        }
        present = {name for name in names if environment.get(name)}
        if not present:
            environment.update(cls.derive_ccache_environment(environment))
        elif present != names:
            missing = ", ".join(sorted(names - present))
            raise ValueError(
                f"ccache compiler identity is incomplete; missing {missing}"
            )
        return environment

    @classmethod
    def _ccache_compiler_identity(
        cls, environment: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        source = os.environ if environment is None else environment
        identity = {}
        records = []
        for name, path_name, fingerprint_name in (
            ("clang", "CCACHE_CLANG_PATH", "CCACHE_CLANG_FINGERPRINT"),
            ("clang++", "CCACHE_CLANGXX_PATH", "CCACHE_CLANGXX_FINGERPRINT"),
        ):
            raw_path = source.get(path_name)
            resolved_path_name = (
                "CCACHE_CLANG_RESOLVED_PATH"
                if name == "clang"
                else "CCACHE_CLANGXX_RESOLVED_PATH"
            )
            raw_resolved_path = source.get(resolved_path_name)
            expected_fingerprint = source.get(fingerprint_name)
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
            records.append(
                {
                    "name": "clangxx" if name == "clang++" else name,
                    "path": str(path),
                    "resolved_path": str(resolved_path),
                    "fingerprint": expected_fingerprint,
                    "version_stdout": version.stdout,
                }
            )

        expected_identity_fingerprint = source.get(
            "CCACHE_COMPILER_FINGERPRINT"
        )
        if not expected_identity_fingerprint:
            raise ValueError(
                "ccache compiler identity is incomplete; missing "
                "CCACHE_COMPILER_FINGERPRINT"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", expected_identity_fingerprint):
            raise ValueError(
                "ccache compiler identity fingerprint is not SHA-256: "
                "CCACHE_COMPILER_FINGERPRINT"
            )
        identity_payload = "".join(
            f"{record['name']}_path={record['path']}\n"
            f"{record['name']}_resolved_path={record['resolved_path']}\n"
            f"{record['name']}_fingerprint={record['fingerprint']}\n"
            for record in records
        ) + "".join(record["version_stdout"] for record in records)
        actual_identity_fingerprint = hashlib.sha256(
            identity_payload.encode()
        ).hexdigest()
        if actual_identity_fingerprint != expected_identity_fingerprint:
            raise ValueError(
                "ccache compiler identity fingerprint mismatch: "
                f"expected {expected_identity_fingerprint}, "
                f"actual {actual_identity_fingerprint}"
            )
        identity["compiler_fingerprint"] = expected_identity_fingerprint
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
            compiler_paths = self._ccache_compiler_identity(
                self._ccache_environment()
            )
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
        environment = self._ccache_environment()
        compiler_identity = self._ccache_compiler_identity(environment)
        environment.update(
            {
                "CCACHE_BASEDIR": str(scratch_root),
                "CCACHE_HASHDIR": "true",
                "CCACHE_COMPILERCHECK": (
                    f"string:{compiler_identity['compiler_fingerprint']}"
                ),
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

    @staticmethod
    def _file_binding(path: Path, root: Path) -> dict[str, Any]:
        metadata = path.stat()
        return {
            "path": path.resolve().relative_to(root.resolve()).as_posix(),
            "sha256": RuntimeBuildService._compiler_file_sha256(path),
            "bytes": metadata.st_size,
            "mode": stat.S_IMODE(metadata.st_mode),
        }

    def _cache_artifacts(self, proof: dict, build_root: Path) -> list[dict[str, Any]]:
        paths: set[Path] = set()
        resources = {
            artifact.get("resource")
            for artifact in proof.get("runtime-artifacts", [])
            if isinstance(artifact, dict)
        }
        for artifact in proof.get("runtime-artifacts", []):
            for deploy_path in runtime_artifact_deploy_paths(artifact):
                paths.add(self.find_build_output(build_root, deploy_path))
        for resource in (
            ROOTLESS_BOOTSTRAP_RESOURCE,
            ROOTLESS_TOOLCHAIN_RESOURCE,
        ):
            if resource not in resources:
                continue
            manifest_name = {
                ROOTLESS_BOOTSTRAP_RESOURCE: "darling-rootless-bootstrap.json",
                ROOTLESS_TOOLCHAIN_RESOURCE: "darling-rootless-toolchain.json",
            }[resource]
            paths.add(build_root / manifest_name)
            paths.update(load_runtime_component_manifest(build_root, resource).values())
        macho_magics = {
            b"\xce\xfa\xed\xfe",
            b"\xcf\xfa\xed\xfe",
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
            b"\xca\xfe\xba\xbe",
            b"\xca\xfe\xba\xbf",
            b"\xbe\xba\xfe\xca",
            b"\xbf\xba\xfe\xca",
        }
        for path in build_root.rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and "CMakeFiles" not in path.parts
            ):
                try:
                    with path.open("rb") as stream:
                        if stream.read(4) in macho_magics:
                            paths.add(path)
                except OSError:
                    continue
        return [
            self._file_binding(path, build_root)
            for path in sorted(paths)
        ]

    def _runtime_cache_identity(
        self,
        proof: dict,
        prefix: Path,
        targets: list[str],
        configured_args: list[str],
    ) -> tuple[Path, dict[str, Any]] | None:
        raw_root = os.environ.get("WEST_RUNTIME_BUILD_CACHE_DIR")
        raw_key = os.environ.get("WEST_RUNTIME_BUILD_CACHE_KEY")
        if not raw_root and not raw_key:
            return None
        if not raw_root or not raw_key or re.fullmatch(r"[0-9a-f]{64}", raw_key) is None:
            raise ValueError("runtime build cache identity is incomplete")
        root = Path(raw_root)
        if not root.is_absolute():
            raise ValueError("runtime build cache root must be absolute")
        identity = {
            "schema_version": 1,
            "key": raw_key,
            "proof": proof,
            "prefix": str(prefix),
            "targets": targets,
            "configure_args": configured_args,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return root / digest, identity

    @classmethod
    def _validate_indexed_artifacts(
        cls,
        indexed: Any,
        build_root: Path,
    ) -> dict[str, tuple[int, int]]:
        if not isinstance(indexed, list) or not indexed:
            raise ValueError("runtime build cache artifact index is invalid")
        observed: list[dict[str, Any]] = []
        signatures: dict[str, tuple[int, int]] = {}
        seen: set[str] = set()
        for row in indexed:
            if (
                not isinstance(row, dict)
                or set(row) != {"path", "sha256", "bytes", "mode"}
                or not isinstance(row.get("path"), str)
                or not row["path"]
            ):
                raise ValueError("runtime build cache artifact index is invalid")
            relative = Path(row["path"])
            if relative.is_absolute() or ".." in relative.parts or row["path"] in seen:
                raise ValueError("runtime build cache artifact path is invalid")
            seen.add(row["path"])
            path = build_root / relative
            try:
                binding = cls._file_binding(path, build_root)
                metadata = path.stat()
            except (OSError, ValueError) as error:
                raise ValueError(
                    "runtime build cache artifacts differ from their index"
                ) from error
            observed.append(binding)
            signatures[row["path"]] = (metadata.st_size, metadata.st_mtime_ns)
        if observed != indexed:
            raise ValueError("runtime build cache artifacts differ from their index")
        return signatures

    @staticmethod
    def _cached_artifact_signatures_unchanged(
        build_root: Path,
        signatures: dict[str, tuple[int, int]],
    ) -> bool:
        for relative, signature in signatures.items():
            try:
                metadata = (build_root / relative).stat()
            except OSError:
                return False
            if (metadata.st_size, metadata.st_mtime_ns) != signature:
                return False
        return True

    def _read_runtime_cache(
        self,
        entry: Path,
        identity: dict[str, Any],
    ) -> tuple[
        Path,
        dict[str, tuple[int, int]],
        list[dict[str, Any]],
    ] | None:
        marker = entry / "cache-index.json"
        if not marker.is_file() or marker.is_symlink():
            return None
        try:
            value = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"runtime build cache index is invalid: {error}") from error
        build_root = entry / "build"
        if (
            value.get("schema_version") != 1
            or value.get("kind") != "west-runtime-build"
            or value.get("identity") != identity
            or not build_root.is_dir()
            or build_root.is_symlink()
        ):
            raise ValueError("runtime build cache identity is invalid")
        indexed = value.get("artifacts")
        signatures = self._validate_indexed_artifacts(indexed, build_root)
        return build_root, signatures, indexed

    def _write_runtime_cache(
        self,
        entry: Path,
        identity: dict[str, Any],
        build_root: Path,
    ) -> None:
        marker = entry / "cache-index.json"
        temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
        data = {
            "schema_version": 1,
            "kind": "west-runtime-build",
            "identity": identity,
            "artifacts": self._cache_artifacts(identity["proof"], build_root),
        }
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(marker)
        descriptor = os.open(entry, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _cached_build_root(
        self,
        scratch_root: Path,
        cache: tuple[Path, dict[str, Any]] | None,
    ):
        if cache is None:
            yield scratch_root / "build", False
            return
        entry, identity = cache
        root = entry.parent
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("runtime build cache root is not a real directory")
        lock_path = root / f".{entry.name}.lock"
        lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cached = self._read_runtime_cache(entry, identity) if entry.exists() else None
            if cached is not None:
                build_root, signatures, indexed = cached
                yield build_root, True
                if not self._cached_artifact_signatures_unchanged(
                    build_root, signatures
                ):
                    self._validate_indexed_artifacts(indexed, build_root)
                return
            if entry.exists():
                shutil.rmtree(entry)
            entry.mkdir(mode=0o700)
            build_root = entry / "build"
            yield build_root, False
            self._write_runtime_cache(entry, identity, build_root)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)

    @staticmethod
    def _ccache_stats(environment: Mapping[str, str] | None) -> dict[str, int]:
        if environment is None:
            return {}
        merged = os.environ.copy()
        merged.update(environment)
        try:
            result = subprocess.run(
                ["ccache", "--print-stats"],
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if result.returncode:
            return {}
        stats: dict[str, int] = {}
        for line in result.stdout.splitlines():
            name, separator, raw_value = line.partition("\t")
            if separator and raw_value.isdecimal():
                stats[name] = int(raw_value)
        return stats

    @staticmethod
    def _ninja_log_entries(build_root: Path) -> int:
        try:
            with (build_root / ".ninja_log").open(encoding="utf-8") as stream:
                return sum(1 for line in stream if line and not line.startswith("#"))
        except OSError:
            return 0

    def _report_build_effectiveness(
        self,
        label: str,
        ninja_before: int,
        ninja_after: int,
        ccache_before: dict[str, int],
        ccache_after: dict[str, int],
    ) -> None:
        direct_hits = max(
            0,
            ccache_after.get("direct_cache_hit", 0)
            - ccache_before.get("direct_cache_hit", 0),
        )
        preprocessed_hits = max(
            0,
            ccache_after.get("preprocessed_cache_hit", 0)
            - ccache_before.get("preprocessed_cache_hit", 0),
        )
        misses = max(
            0,
            ccache_after.get("cache_miss", 0)
            - ccache_before.get("cache_miss", 0),
        )
        hits = direct_hits + preprocessed_hits
        attempts = hits + misses
        hit_rate = round(100 * hits / attempts, 1) if attempts else 100.0
        self._host.inf(
            f"  runtime build effectiveness: {label} "
            f"ninja_edges={max(0, ninja_after - ninja_before)} "
            f"ccache_hits={hits} ccache_misses={misses} "
            f"ccache_hit_rate={hit_rate:.1f}%"
        )


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
        cache_root = os.environ.get("WEST_RUNTIME_BUILD_CACHE_DIR")
        configuration_root = Path(cache_root) if cache_root else scratch_root
        configured_args = configure_args(proof, prefix, configuration_root)
        cache = self._runtime_cache_identity(
            proof, prefix, targets, configured_args
        )
        timeout = int(
            timeout_seconds
            if timeout_seconds is not None
            else proof.get("build-timeout-seconds", 1800)
        )
        if timeout <= 0:
            raise ValueError("runtime build timeout must be greater than zero")
        build_environment = self.build_environment(proof, configuration_root)
        with self._cached_build_root(scratch_root, cache) as (
            build_root,
            cache_reused,
        ):
            if cache_reused:
                self._host.inf(
                    f"  runtime incremental build reuse: {label} -> {build_root}"
                )
            else:
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
                        *configured_args,
                    ],
                    cwd=Path(self._host.topdir),
                    env=build_environment,
                    timeout_seconds=timeout,
                    capture_output=True,
                    heartbeat_seconds=30,
                    heartbeat=lambda elapsed: self._host.inf(
                        f"  runtime heartbeat: {label} configure still running "
                        f"({elapsed:.0f}s)"
                    ),
                    output_line=lambda stream, line: self._forward_runtime_line(
                        label, "configure", stream, line
                    ),
                )
                if configured.returncode:
                    dump_command_tail(f"{label} configure", configured)
                    if allow_failure:
                        raise RuntimeBuildFailure("configure", configured)
                    self._host.die(
                        f"{label} configure failed with rc {configured.returncode}"
                    )
                self._host.inf(
                    f"  runtime phase complete: {label} configure "
                    f"({time.monotonic() - configured_at:.1f}s)"
                )
            ninja_before = self._ninja_log_entries(build_root)
            ccache_before = self._ccache_stats(build_environment)
            built_at = time.monotonic()
            self._host.inf(f"  runtime phase start: {label} build")
            self._host.inf(f"  {label} build: {', '.join(targets)}")
            built = runner(
                ["ninja", "-d", "stats", "-C", str(build_root), *targets],
                cwd=Path(self._host.topdir),
                env=build_environment,
                timeout_seconds=timeout,
                capture_output=True,
                heartbeat_seconds=30,
                heartbeat=lambda elapsed: self._host.inf(
                    f"  runtime heartbeat: {label} build still running "
                    f"({elapsed:.0f}s)"
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
            self._host.inf(
                f"  runtime phase complete: {label} build "
                f"({time.monotonic() - built_at:.1f}s)"
            )
            self._report_build_effectiveness(
                label,
                ninja_before,
                self._ninja_log_entries(build_root),
                ccache_before,
                self._ccache_stats(build_environment),
            )
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

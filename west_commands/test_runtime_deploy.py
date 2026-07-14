"""Runtime closure planning and transactional prefix deployment."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from deploy_transaction import DeploymentTransaction, DeploymentTransactionError
from test_runtime import (
    ROOTLESS_BOOTSTRAP_RESOURCE,
    ROOTLESS_TOOLCHAIN_RESOURCE,
    is_fat_macho_binary,
    is_macho_binary,
    load_runtime_component_manifest,
    parse_macho_dylib_dependencies,
    parse_macho_dylib_id,
    resolve_macho_runtime_closure,
    runtime_artifact_deploy_paths,
    runtime_deploy_targets,
)


@dataclass(frozen=True)
class IsolatedEmptyPrefix:
    """A disposable empty prefix owned by one runtime proof."""

    root: Path
    prefix: Path


class RuntimeDeploymentService:
    """Own closure resolution, atomic deployment, and rollback."""

    def __init__(self, host: Any):
        self._host = host

    def create_empty_prefix(self, requested_prefix: Path) -> IsolatedEmptyPrefix:
        """Create a clean prefix beside, rather than inside, the selected prefix.

        RED proofs must not mutate a retained provider prefix.  The temporary
        root is created by ``mkdtemp`` and is the only path this service later
        removes, so a metadata value cannot turn cleanup into arbitrary
        recursive deletion.
        """

        requested = requested_prefix.expanduser()
        if requested.is_symlink():
            self._host.die(
                "guest-runtime-deploy clean-prefix cannot use a symlink: "
                f"{requested}"
            )
        resolved = requested.resolve(strict=False)
        if resolved == resolved.parent or resolved.parent == Path("/"):
            self._host.die(
                "guest-runtime-deploy clean-prefix needs a non-root prefix parent: "
                f"{requested}"
            )
        if not resolved.parent.is_dir():
            self._host.die(
                "guest-runtime-deploy clean-prefix parent is not a directory: "
                f"{resolved.parent}"
            )
        root = Path(
            tempfile.mkdtemp(
                prefix=f".{resolved.name}.west-red-clean-",
                dir=resolved.parent,
            )
        ).resolve()
        prefix = root / "prefix"
        prefix.mkdir()
        self._host.inf(f"  runtime RED: created empty prefix {prefix}")
        return IsolatedEmptyPrefix(root=root, prefix=prefix)

    def cleanup_empty_prefix(
        self,
        isolated: IsolatedEmptyPrefix,
        *,
        lifecycle_env: dict[str, str] | None = None,
    ) -> bool:
        """Stop and remove a proof-owned empty prefix after the RED run."""

        if not self._host._shutdown_runtime_prefix(
            isolated.prefix, extra_env=lifecycle_env
        ):
            self._host.err(
                "guest-runtime-deploy could not cleanly shutdown isolated RED "
                f"prefix; preserving it for diagnostics: {isolated.prefix}"
            )
            return False
        if (
            isolated.root.name.find(".west-red-clean-") == -1
            or isolated.root.is_symlink()
            or not isolated.root.is_dir()
            or isolated.prefix.parent != isolated.root
            or isolated.prefix.is_symlink()
            or not isolated.prefix.is_dir()
        ):
            self._host.err(
                "guest-runtime-deploy refused to remove an unexpected isolated "
                f"RED prefix layout: {isolated.root}"
            )
            return False
        shutil.rmtree(isolated.root)
        self._host.inf(f"  runtime RED: removed empty prefix {isolated.prefix}")
        return True

    def macho_inspect(self, path: Path, flag: str) -> str:
        try:
            result = subprocess.run(
                ["llvm-objdump", "--macho", flag, str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            self._host.die(
                "guest-runtime-deploy rootless bootstrap closure requires llvm-objdump"
            )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            self._host.die(
                f"guest-runtime-deploy could not inspect Mach-O {path}: "
                f"{detail or f'rc {result.returncode}'}"
            )
        return result.stdout

    def macho_dependencies(self, path: Path) -> list[str]:
        if not is_macho_binary(path):
            return []
        return parse_macho_dylib_dependencies(self.macho_inspect(path, "--dylibs-used"))

    def macho_dylib_providers(self, build_root: Path) -> dict[str, Path]:
        candidates: dict[str, list[Path]] = {}
        for path in build_root.rglob("*"):
            if (
                not path.is_file()
                or "CMakeFiles" in path.parts
                or path.name.endswith(("_firstpass", "_firstpass.dylib"))
                or not is_macho_binary(path)
            ):
                continue
            install_name = parse_macho_dylib_id(self.macho_inspect(path, "--dylib-id"))
            if install_name is not None:
                candidates.setdefault(install_name, []).append(path)
        providers = {}
        for install_name, paths in candidates.items():
            universal = [path for path in paths if is_fat_macho_binary(path)]
            selected = universal or paths
            if len(selected) != 1:
                self._host.die(
                    "guest-runtime-deploy found multiple built providers for "
                    f"{install_name}: {', '.join(str(path) for path in paths)}"
                )
            providers[install_name] = selected[0]
        return providers

    def rootless_bootstrap_closure(
        self, proof: dict, build_root: Path, explicit: dict[str, Path]
    ) -> dict[str, Path]:
        resources = {
            artifact.get("resource")
            for artifact in proof.get("runtime-artifacts", [])
            if isinstance(artifact, dict)
        }
        component_resources = resources & {
            ROOTLESS_BOOTSTRAP_RESOURCE,
            ROOTLESS_TOOLCHAIN_RESOURCE,
        }
        if not component_resources:
            return {}
        roots = {
            "/" + deploy_path: source
            for deploy_path, source in explicit.items()
            if is_macho_binary(source)
        }
        if not roots:
            self._host.die(
                "guest-runtime-deploy rootless bootstrap closure has no Mach-O roots"
            )
        try:
            closure = resolve_macho_runtime_closure(
                roots,
                self.macho_dylib_providers(build_root),
                self.macho_dependencies,
            )
        except ValueError as error:
            self._host.die(f"guest-runtime-deploy {error}")
        return {
            guest_path.removeprefix("/"): source
            for guest_path, source in closure.items()
            if guest_path.removeprefix("/") not in explicit
        }

    def deployment_plan(
        self, proof: dict, build_root: Path, prefix: Path
    ) -> list[tuple[Path, Path]]:
        deployments: dict[str, Path] = {}
        for artifact in proof.get("runtime-artifacts", []):
            for deploy_path in runtime_artifact_deploy_paths(artifact):
                if deploy_path in deployments:
                    self._host.die(
                        "guest-runtime-deploy has duplicate explicit deploy path: "
                        f"{deploy_path}"
                    )
                deployments[deploy_path] = self._host._runtime_red_find_build_output(
                    build_root, deploy_path
                )
        resources = {
            artifact.get("resource")
            for artifact in proof.get("runtime-artifacts", [])
            if isinstance(artifact, dict)
        }
        for resource in (
            ROOTLESS_BOOTSTRAP_RESOURCE,
            ROOTLESS_TOOLCHAIN_RESOURCE,
        ):
            if resource not in resources:
                continue
            try:
                component = load_runtime_component_manifest(build_root, resource)
            except ValueError as error:
                self._host.die(f"guest-runtime-deploy {error}")
            conflicts = set(deployments).intersection(component)
            if conflicts:
                self._host.die(
                    f"guest-runtime-deploy {resource} manifest conflicts "
                    "with explicit deploy path(s): " + ", ".join(sorted(conflicts))
                )
            deployments.update(component)
        closure = self.rootless_bootstrap_closure(proof, build_root, deployments)
        conflicts = set(deployments).intersection(closure)
        if conflicts:
            self._host.die(
                "guest-runtime-deploy rootless bootstrap closure conflicts with "
                "entrypoint path(s): " + ", ".join(sorted(conflicts))
            )
        deployments.update(closure)
        rootless_no_mount = bool(
            resources
            & {ROOTLESS_BOOTSTRAP_RESOURCE, ROOTLESS_TOOLCHAIN_RESOURCE}
        )
        plan = []
        for deploy_path, source in deployments.items():
            try:
                targets = runtime_deploy_targets(
                    prefix, deploy_path, rootless_no_mount=rootless_no_mount
                )
            except ValueError:
                self._host.die(
                    f"guest-runtime-deploy deploy path must be relative: {deploy_path}"
                )
            plan.extend((source, target) for target in targets)
        return plan

    @contextmanager
    def deployed(
        self,
        proof: dict,
        build_root: Path,
        prefix: Path,
        *,
        label: str,
        restore_deployment: bool,
        lifecycle_env: dict[str, str] | None = None,
    ) -> Iterator[None]:
        succeeded = False
        started = time.monotonic()
        self._host.inf(f"  runtime phase start: {label} deploy")
        shutdown_env = lifecycle_env or self._proof_lifecycle_env(proof)
        if not self._host._shutdown_runtime_prefix(prefix, extra_env=shutdown_env):
            self._host.die(
                f"guest-runtime-deploy could not stop Darling prefix before deploy: {prefix}"
            )
        with tempfile.TemporaryDirectory(prefix="west-red-proof-deploy-") as temp:
            transaction = DeploymentTransaction(
                Path(temp) / "manifest.json", prefix, normalize_modes=True
            )
            try:
                for source, destination in self.deployment_plan(proof, build_root, prefix):
                    transaction.replace(source, destination)
                    self._host.inf(f"  {label} deploy: {source} -> {destination}")
                self._host.inf(
                    f"  runtime phase complete: {label} deploy "
                    f"({time.monotonic() - started:.1f}s)"
                )
                yield
                succeeded = True
                if not restore_deployment:
                    transaction.commit()
            except DeploymentTransactionError as error:
                self._host.die(f"guest-runtime-deploy transaction failed: {error}")
            finally:
                if not self._host._shutdown_runtime_prefix(
                    prefix, extra_env=shutdown_env
                ):
                    self._host.err(
                        "guest-runtime-deploy could not stop Darling prefix before restore: "
                        f"{prefix}"
                    )
                if restore_deployment or not succeeded:
                    try:
                        transaction.rollback()
                    except DeploymentTransactionError as error:
                        self._host.die(f"guest-runtime-deploy rollback failed: {error}")
                elif transaction.entries:
                    self._host.inf(f"  {label} deployment retained after successful smoke")
                self._host._shutdown_runtime_prefix(prefix, extra_env=shutdown_env)

    @staticmethod
    def _proof_lifecycle_env(proof: dict) -> dict[str, str]:
        launcher_env = proof.get("launcher-env", {})
        if not isinstance(launcher_env, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in launcher_env.items()
            if isinstance(key, str) and key
        }

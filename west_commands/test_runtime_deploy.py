"""Runtime closure planning and transactional prefix deployment."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from deploy_transaction import DeploymentTransaction, DeploymentTransactionError
from test_prefix import RetainedDirectoryCapability, RetainedRegularFileCapability
from test_runtime import (
    ROOTLESS_BOOTSTRAP_RESOURCE,
    ROOTLESS_TOOLCHAIN_RESOURCE,
    RUNTIME_MODE_MARKER_NAME,
    is_fat_macho_binary,
    is_macho_binary,
    load_runtime_component_manifest,
    parse_macho_dylib_dependencies,
    parse_macho_dylib_id,
    resolve_macho_runtime_closure,
    runtime_artifact_deploy_paths,
    runtime_deploy_targets,
)
PREFIX_STATE_NAME = ".darling-prefix-state-v3"
PREFIX_STATE_HEADER = "DARLING_PREFIX_STATE_V3"
PREFIX_STATE_SCHEMA = 3
PREFIX_STATE_PROVENANCE = "darling-runtime-prefix-sidecar-v1"
PREFIX_SIDECAR_SUFFIX = ".eunion-sidecar-v1"


@dataclass(frozen=True)
class IsolatedEmptyPrefix:
    """A disposable empty prefix owned by one runtime proof."""

    root: Path
    prefix: Path


@dataclass
class RuntimePrefixStateCapability:
    """Retain prefix, typed state, and sidecar through deployment."""

    prefix: RetainedDirectoryCapability
    binding: RetainedRegularFileCapability | None
    sidecar: RetainedDirectoryCapability | None
    create_mode_marker: bool
    mode_marker_content: bytes | None

    def revalidate(self) -> None:
        prefix_status = self.prefix.revalidate(metadata=False)
        initial_prefix = self.prefix.initial_status
        if (
            stat.S_IMODE(prefix_status.st_mode),
            prefix_status.st_uid,
            prefix_status.st_gid,
        ) != (
            stat.S_IMODE(initial_prefix.st_mode),
            initial_prefix.st_uid,
            initial_prefix.st_gid,
        ):
            raise OSError(
                f"retained runtime prefix metadata changed: {self.prefix.path}"
            )
        if self.binding is not None:
            self.binding.revalidate()
        if self.sidecar is not None:
            sidecar_status = self.sidecar.revalidate(metadata=False)
            initial_sidecar = self.sidecar.initial_status
            if (
                stat.S_IMODE(sidecar_status.st_mode),
                sidecar_status.st_uid,
                sidecar_status.st_gid,
            ) != (
                stat.S_IMODE(initial_sidecar.st_mode),
                initial_sidecar.st_uid,
                initial_sidecar.st_gid,
            ):
                raise OSError(
                    f"retained runtime sidecar metadata changed: {self.sidecar.path}"
                )

    def close(self) -> None:
        if self.binding is not None:
            self.binding.close()
            self.binding = None
        if self.sidecar is not None:
            self.sidecar.close()
            self.sidecar = None
        self.prefix.close()

    def __enter__(self) -> "RuntimePrefixStateCapability":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()


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

    def _retain_runtime_mode_state(
        self, proof: dict, prefix: Path
    ) -> RuntimePrefixStateCapability:
        """Validate state through retained FDs and keep its identities alive."""

        try:
            prefix_capability = RetainedDirectoryCapability.open(prefix)
        except OSError as error:
            self._host.die(
                "guest-runtime-deploy runtime prefix is not a stable real "
                f"directory: {prefix}: {error}"
            )
        binding_capability: RetainedRegularFileCapability | None = None
        sidecar_capability: RetainedDirectoryCapability | None = None
        try:
            mode = proof.get("runtime-mode")
            if mode is None:
                return RuntimePrefixStateCapability(
                    prefix_capability, None, None, False, None
                )
            expected = f"DARLING_RUNTIME_MODE_V1={mode}\n".encode()
            entries = prefix_capability.entries()
            if not entries:
                return RuntimePrefixStateCapability(
                    prefix_capability, None, None, True, expected
                )

            state_status = prefix_capability.child_status(PREFIX_STATE_NAME)
            marker_status = prefix_capability.child_status(RUNTIME_MODE_MARKER_NAME)
            if state_status is not None:
                if not stat.S_ISREG(state_status.st_mode):
                    self._host.die(
                        "guest-runtime-deploy typed prefix state is not a "
                        f"regular file: {prefix / PREFIX_STATE_NAME}"
                    )
                prefix_status = prefix_capability.revalidate(metadata=False)
                if (
                    stat.S_IMODE(prefix_status.st_mode) != 0o755
                    or prefix_status.st_nlink < 2
                ):
                    self._host.die(
                        "guest-runtime-deploy typed prefix root metadata "
                        f"mismatch: {prefix}"
                    )
                try:
                    binding_capability = prefix_capability.retain_regular_child(
                        PREFIX_STATE_NAME,
                        mode=0o600,
                        uid=prefix_status.st_uid,
                        gid=prefix_status.st_gid,
                        nlink=1,
                    )
                    raw_state = binding_capability.content
                    content = raw_state.decode("utf-8")
                except (OSError, UnicodeError) as error:
                    self._host.die(
                        "guest-runtime-deploy cannot read stable typed prefix "
                        f"state {prefix / PREFIX_STATE_NAME}: {error}"
                    )
                fields = content.splitlines()
                names = (
                    "generation",
                    "prefix_device",
                    "prefix_inode",
                    "sidecar_device",
                    "sidecar_inode",
                    "owner_uid",
                    "owner_gid",
                )
                if len(fields) != 11 or fields[:3] != [
                    PREFIX_STATE_HEADER,
                    f"schema_version={PREFIX_STATE_SCHEMA}",
                    f"runtime_mode={mode}",
                ] or fields[-1] != f"provenance={PREFIX_STATE_PROVENANCE}":
                    self._host.die(
                        "guest-runtime-deploy typed prefix state mismatch: "
                        f"expected schema {PREFIX_STATE_SCHEMA} mode {mode!r}: "
                        f"{prefix / PREFIX_STATE_NAME}"
                    )
                numbers: dict[str, int] = {}
                for field, name in zip(fields[3:-1], names, strict=True):
                    key, separator, raw_value = field.partition("=")
                    if key != name or separator != "=" or not raw_value.isdecimal():
                        self._host.die(
                            "guest-runtime-deploy typed prefix state has "
                            f"malformed {name}: {prefix / PREFIX_STATE_NAME}"
                        )
                    numbers[name] = int(raw_value)

                sidecar = Path(f"{prefix}{PREFIX_SIDECAR_SUFFIX}")
                try:
                    sidecar_capability = RetainedDirectoryCapability.open(sidecar)
                    sidecar_status = sidecar_capability.revalidate(metadata=True)
                except OSError as error:
                    self._host.die(
                        "guest-runtime-deploy typed prefix sidecar is not a "
                        f"stable real directory: {sidecar}: {error}"
                    )
                if (
                    stat.S_IMODE(sidecar_status.st_mode) != 0o700
                    or sidecar_status.st_uid != prefix_status.st_uid
                    or sidecar_status.st_gid != prefix_status.st_gid
                    or sidecar_status.st_nlink < 2
                ):
                    self._host.die(
                        "guest-runtime-deploy typed prefix sidecar metadata "
                        f"mismatch: {sidecar}"
                    )
                expected_numbers = {
                    "prefix_device": prefix_status.st_dev,
                    "prefix_inode": prefix_status.st_ino,
                    "sidecar_device": sidecar_status.st_dev,
                    "sidecar_inode": sidecar_status.st_ino,
                    "owner_uid": prefix_status.st_uid,
                    "owner_gid": prefix_status.st_gid,
                }
                if numbers["generation"] < 1 or any(
                    numbers[name] != value
                    for name, value in expected_numbers.items()
                ):
                    self._host.die(
                        "guest-runtime-deploy typed prefix state identity "
                        f"mismatch: {prefix / PREFIX_STATE_NAME}"
                    )
                if marker_status is not None:
                    self._host.die(
                        "guest-runtime-deploy current typed prefix retained "
                        "legacy mode metadata: "
                        f"{prefix / RUNTIME_MODE_MARKER_NAME}"
                    )
                prefix_capability.revalidate(metadata=False)
                sidecar_capability.revalidate(metadata=True)
                return RuntimePrefixStateCapability(
                    prefix_capability,
                    binding_capability,
                    sidecar_capability,
                    False,
                    expected,
                )

            if mode == "rootless-eunion" and marker_status is not None:
                self._host.die(
                    "guest-runtime-deploy refuses legacy runtime-mode metadata "
                    f"for rootless prefix: {prefix / RUNTIME_MODE_MARKER_NAME}"
                )
            if marker_status is None or not stat.S_ISREG(marker_status.st_mode):
                self._host.die(
                    "guest-runtime-deploy refuses a populated prefix without "
                    f"a regular typed mode marker: {prefix}"
                )
            try:
                binding_capability = prefix_capability.retain_regular_child(
                    RUNTIME_MODE_MARKER_NAME,
                    nlink=1,
                )
                observed = binding_capability.content
            except OSError as error:
                self._host.die(
                    "guest-runtime-deploy cannot read stable typed mode marker "
                    f"{prefix / RUNTIME_MODE_MARKER_NAME}: {error}"
                )
            if observed != expected:
                self._host.die(
                    "guest-runtime-deploy typed mode marker mismatch: "
                    f"expected {expected.decode().strip()!r}, "
                    f"observed {observed.decode(errors='replace').strip()!r}"
                )
            return RuntimePrefixStateCapability(
                prefix_capability, binding_capability, None, False, expected
            )
        except BaseException:
            if binding_capability is not None:
                binding_capability.close()
            if sidecar_capability is not None:
                sidecar_capability.close()
            prefix_capability.close()
            raise

    def _runtime_mode_marker_required(
        self, proof: dict, prefix: Path
    ) -> tuple[bool, bytes | None]:
        """Validate a mode binding without leaking retained capabilities."""

        with self._retain_runtime_mode_state(proof, prefix) as capability:
            return capability.create_mode_marker, capability.mode_marker_content

    def _revalidate_runtime_prefix_state(
        self,
        capability: RuntimePrefixStateCapability,
        *,
        phase: str,
    ) -> None:
        try:
            capability.revalidate()
        except OSError as error:
            self._host.die(
                "guest-runtime-deploy retained prefix identity changed before "
                f"{phase}: {error}"
            )

    def _initialize_empty_rootless_prefix(
        self,
        proof: dict,
        build_root: Path,
        prefix: Path,
        lifecycle_env: dict[str, str],
        *,
        label: str,
    ) -> bool:
        """Let the built product establish state before artifact deployment.

        The launcher, not West, owns prefix initialization and schema
        publication. Returning true records that the prefix was empty on entry
        so a restoring/failed proof can return it to that exact state.
        """

        create_marker, _ = self._runtime_mode_marker_required(proof, prefix)
        if not create_marker or proof.get("runtime-mode") != "rootless-eunion":
            return False
        launcher = self._host._runtime_red_find_build_output(
            build_root, "bin/darling"
        )
        if launcher.is_symlink() or not launcher.is_file():
            self._host.die(
                "guest-runtime-deploy built launcher is not a regular file: "
                f"{launcher}"
            )
        environment = os.environ.copy()
        environment.update(lifecycle_env)
        environment["DPREFIX"] = str(prefix)
        try:
            initialized = subprocess.run(
                [str(launcher), "--rootless", "shutdown"],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._host.die(
                f"guest-runtime-deploy product prefix initialization failed: {error}"
            )
        detail = "\n".join(
            part.strip()
            for part in (initialized.stdout, initialized.stderr)
            if part.strip()
        )
        already_stopped = (
            initialized.returncode == 1
            and "Darling container is not running" in detail
        )
        if initialized.returncode != 0 and not already_stopped:
            self._host.die(
                "guest-runtime-deploy product prefix initialization failed "
                f"with rc {initialized.returncode}: {detail or 'no output'}"
            )
        create_marker, _ = self._runtime_mode_marker_required(proof, prefix)
        if create_marker:
            self._host.die(
                "guest-runtime-deploy product prefix initialization did not "
                "publish a typed lifecycle state"
            )
        self._host.inf(
            f"  {label} deploy: product initialized typed prefix state -> "
            f"{prefix / PREFIX_STATE_NAME}"
        )
        return True

    def _restore_empty_initialized_prefix(
        self, capability: RuntimePrefixStateCapability
    ) -> None:
        """Restore a proof-owned empty prefix through its retained directory FD."""

        self._revalidate_runtime_prefix_state(
            capability, phase="empty-prefix restore"
        )

        def remove_contents(directory_fd: int) -> None:
            for name in sorted(os.listdir(directory_fd)):
                if not name or "/" in name or name in {".", ".."}:
                    self._host.die(
                        f"guest-runtime-deploy found unsafe restore entry: {name!r}"
                    )
                entry_status = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(entry_status.st_mode):
                    child_fd = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    try:
                        opened_status = os.fstat(child_fd)
                        named_status = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        if (
                            opened_status.st_dev != entry_status.st_dev
                            or opened_status.st_ino != entry_status.st_ino
                            or named_status.st_dev != opened_status.st_dev
                            or named_status.st_ino != opened_status.st_ino
                        ):
                            self._host.die(
                                "guest-runtime-deploy restore entry changed "
                                f"while opening: {name}"
                            )
                        remove_contents(child_fd)
                    finally:
                        os.close(child_fd)
                    os.rmdir(name, dir_fd=directory_fd)
                else:
                    os.unlink(name, dir_fd=directory_fd)

        remove_contents(capability.prefix.fd)
        if os.listdir(capability.prefix.fd):
            self._host.die(
                "guest-runtime-deploy could not restore empty retained prefix: "
                f"{capability.prefix.path}"
            )

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
        # Reject hostile or mismatched existing state before invoking even the
        # shutdown side of a runtime command.
        self._runtime_mode_marker_required(proof, prefix)
        shutdown_env = lifecycle_env or self._proof_lifecycle_env(proof)
        if not self._host._shutdown_runtime_prefix(prefix, extra_env=shutdown_env):
            self._host.die(
                f"guest-runtime-deploy could not stop Darling prefix before deploy: {prefix}"
            )
        initialized_empty = self._initialize_empty_rootless_prefix(
            proof,
            build_root,
            prefix,
            shutdown_env,
            label=label,
        )
        with self._retain_runtime_mode_state(proof, prefix) as state_capability:
            create_mode_marker = state_capability.create_mode_marker
            mode_marker_content = state_capability.mode_marker_content
            plan = self.deployment_plan(proof, build_root, prefix)
            self._revalidate_runtime_prefix_state(
                state_capability, phase="deployment planning"
            )
            with tempfile.TemporaryDirectory(prefix="west-red-proof-deploy-") as temp:
                initial_prefix = state_capability.prefix.initial_status
                try:
                    transaction = DeploymentTransaction(
                        Path(temp) / "manifest.json",
                        prefix,
                        normalize_modes=True,
                        expected_root_identity=(
                            initial_prefix.st_dev,
                            initial_prefix.st_ino,
                        ),
                    )
                except DeploymentTransactionError as error:
                    self._host.die(
                        f"guest-runtime-deploy transaction failed: {error}"
                    )
                self._revalidate_runtime_prefix_state(
                    state_capability, phase="transaction root binding"
                )
                operation_error: BaseException | None = None
                try:
                    if create_mode_marker:
                        marker_source = Path(temp) / "runtime-mode-marker"
                        marker_source.write_bytes(mode_marker_content or b"")
                        marker_source.chmod(0o600)
                        marker_destination = prefix / RUNTIME_MODE_MARKER_NAME
                        self._revalidate_runtime_prefix_state(
                            state_capability, phase="mode-marker publication"
                        )
                        transaction.replace(marker_source, marker_destination)
                        self._revalidate_runtime_prefix_state(
                            state_capability, phase="mode-marker publication"
                        )
                        self._host.inf(
                            f"  {label} deploy: typed mode marker -> "
                            f"{marker_destination}"
                        )
                    for source, destination in plan:
                        self._revalidate_runtime_prefix_state(
                            state_capability, phase=f"deployment of {destination}"
                        )
                        transaction.replace(source, destination)
                        self._revalidate_runtime_prefix_state(
                            state_capability, phase=f"deployment of {destination}"
                        )
                        self._host.inf(
                            f"  {label} deploy: {source} -> {destination}"
                        )
                    self._host.inf(
                        f"  runtime phase complete: {label} deploy "
                        f"({time.monotonic() - started:.1f}s)"
                    )
                    yield
                    self._revalidate_runtime_prefix_state(
                        state_capability, phase="post-deployment yield"
                    )
                    succeeded = True
                    if not restore_deployment:
                        transaction.commit()
                except DeploymentTransactionError as error:
                    operation_error = error
                    self._host.die(
                        f"guest-runtime-deploy transaction failed: {error}"
                    )
                except BaseException as error:
                    operation_error = error
                    raise
                finally:
                    cleanup_error: BaseException | None = None
                    try:
                        self._revalidate_runtime_prefix_state(
                            state_capability, phase="pre-restore shutdown"
                        )
                    except BaseException as error:
                        cleanup_error = error
                    else:
                        if not self._host._shutdown_runtime_prefix(
                            prefix, extra_env=shutdown_env
                        ):
                            self._host.err(
                                "guest-runtime-deploy could not stop Darling prefix "
                                f"before restore: {prefix}"
                            )
                    if restore_deployment or not succeeded:
                        try:
                            self._revalidate_runtime_prefix_state(
                                state_capability, phase="transaction rollback"
                            )
                        except BaseException as error:
                            if cleanup_error is None:
                                cleanup_error = error
                        try:
                            transaction.rollback()
                        except DeploymentTransactionError as error:
                            if cleanup_error is None:
                                cleanup_error = SystemExit(
                                    "guest-runtime-deploy rollback failed: "
                                    f"{error}"
                                )
                        try:
                            self._revalidate_runtime_prefix_state(
                                state_capability, phase="post-transaction rollback"
                            )
                        except BaseException as error:
                            if cleanup_error is None:
                                cleanup_error = error
                        if initialized_empty and cleanup_error is None:
                            self._restore_empty_initialized_prefix(state_capability)
                    elif transaction.entries:
                        self._host.inf(
                            f"  {label} deployment retained after successful smoke"
                        )
                    if cleanup_error is None:
                        self._host._shutdown_runtime_prefix(
                            prefix, extra_env=shutdown_env
                        )
                    if cleanup_error is not None:
                        if operation_error is None:
                            raise cleanup_error
                        self._host.err(
                            "guest-runtime-deploy cleanup also detected an unsafe "
                            f"state transition: {cleanup_error}"
                        )

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

"""Prepare guest state using a normal installed Darling product."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from contextlib import ExitStack

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_receipt import verify_receipt
from guest_toolchain import COMMAND_LINE_TOOLS_RESOURCE, GuestToolchainError, ensure_command_line_tools
from prefix_repair import cleanup_prefix_mounts, darling_init_pid_is_usable
from runtime_install import (
    RuntimeInstallationError, build_lock, build_runtime, configured_runtime_root,
    install_runtime, prefix_path_is_safe, resolve_runtime,
)
from runtime_retention import require_publication_budget, runtime_use
from test_execution import process_output_text
from test_guest_execution import run_guest_shell
from test_prefix import PrefixLifecycleOwner

SMOKE_MARKER = "DARLING_BOOTSTRAP_GUEST_OK"


class DarlingBootstrap(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-bootstrap", "Prepare guest state with the normal installed Darling product",
            "Build/install a new runtime or reuse an installed image, then initialize and smoke an independent guest prefix",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument("--runtime-root", required=True, type=Path)
        parser.add_argument("--prefix", required=True)
        parser.add_argument("--build-dir", type=Path, help="configured product build for a fresh installation")
        parser.add_argument("--no-build", action="store_true", help="install already-built files, or reuse an existing image without rewriting it")
        parser.add_argument("--guest-toolchain", choices=(COMMAND_LINE_TOOLS_RESOURCE,))
        parser.add_argument("--with-test-data", action="store_true", help="include the optional product tests-data package in a new image")
        parser.add_argument("--smoke-timeout-seconds", type=int, default=60)
        parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
        parser.add_argument("--json", action="store_true")
        return parser

    def do_run(self, args, unknown):
        emit = lambda message: print(message, file=sys.stderr, flush=True)
        # Validate spelling before Path can discard "." components.
        raw_prefix = os.path.expanduser(str(args.prefix))
        if not prefix_path_is_safe(raw_prefix):
            self.die("--prefix must be an absolute non-symlink guest-state path without '.' or '..' components")
        prefix = Path(raw_prefix)
        root = args.runtime_root.expanduser().resolve()
        if args.smoke_timeout_seconds < 1:
            self.die("--smoke-timeout-seconds must be positive")
        if prefix == root or root in prefix.parents or prefix in root.parents:
            self.die("--runtime-root and --prefix must be disjoint directories")
        result = {
            "status": "FAIL", "runtime_root": str(root), "prefix": str(prefix),
            "build_dir": str(args.build_dir) if args.build_dir else None,
            "build_performed": False, "installed": False, "smoke": "NOT_RUN",
            "toolchain": "NOT_RUN" if args.guest_toolchain else "NOT_REQUESTED", "cleanup": None,
        }
        lifetime = ExitStack()
        phase = "installation"
        try:
            lifetime.enter_context(runtime_use(args.runtime_root.expanduser()))
            exists = root.is_dir() and any(root.iterdir())
            if exists:
                if args.build_dir and not args.no_build:
                    raise RuntimeInstallationError(
                        f"{root} is already published; use --no-build to run it or configure a fresh root for new code"
                    )
                runtime = resolve_runtime(root)
                if runtime.identity is not None:
                    problems, notes = verify_receipt(runtime.identity)
                    if problems:
                        raise RuntimeInstallationError("; ".join(f"{p.label}: {p.message}" for p in problems))
                    for note in notes:
                        emit(note)
                else:
                    emit("WARNING: ordinary product installation has no workspace receipt; source/build provenance is unverified")
            else:
                if args.build_dir is None:
                    raise RuntimeInstallationError("a fresh installation needs --build-dir configured with this CMAKE_INSTALL_PREFIX")
                require_publication_budget(root)
                build = args.build_dir.expanduser().resolve()
                with build_lock(build):
                    if configured_runtime_root(build) != root:
                        raise RuntimeInstallationError(
                            f"build installs into {configured_runtime_root(build)}, not {root}; configure the final root before building"
                        )
                    if not args.no_build:
                        build_runtime(build, jobs=args.jobs)
                        result["build_performed"] = True
                    runtime = install_runtime(
                        build, root, manifest_repo=self.manifest.repo_abspath,
                        with_test_data=args.with_test_data,
                    )
                    result["installed"] = True
            if result["installed"]:
                lifetime.enter_context(runtime_use(root))
            if args.with_test_data and not (runtime.system_root / "usr/share/darling/tests/eunion").is_dir():
                raise RuntimeInstallationError("selected immutable image lacks tests-data; publish a fresh image with --with-test-data")
            result["installation_identity"] = runtime.summary()
            env = runtime.environment(prefix)
            prefix.parent.mkdir(parents=True, exist_ok=True)
            owner = PrefixLifecycleOwner(
                resolve_launcher=lambda selected: str(runtime.launcher),
                prefix_env=lambda selected: runtime.environment(selected),
                cleanup_mounts=cleanup_prefix_mounts,
                init_pid_is_usable=darling_init_pid_is_usable,
                inf=emit, err=emit, wrn=emit,
            )
            phase = "guest"
            with owner.locked(prefix):
                try:
                    smoke = run_guest_shell(
                        str(runtime.launcher), prefix,
                        f"set -eu; test -r /usr/lib/dyld; printf '%s\\n' {SMOKE_MARKER}",
                        cwd=Path(self.topdir), env=env,
                        timeout_seconds=args.smoke_timeout_seconds,
                        capture_output=True,
                        heartbeat_seconds=30,
                        heartbeat=lambda elapsed: emit(f"guest smoke running ({elapsed:.0f}s)"),
                    )
                    output = process_output_text(smoke)
                    if output:
                        emit(output.rstrip())
                    result["guest_rc"] = smoke.returncode
                    if smoke.timed_out or smoke.returncode != 0 or SMOKE_MARKER not in output:
                        result["smoke"] = "FAIL"
                        raise RuntimeInstallationError(
                            f"guest smoke failed: rc={smoke.returncode}, timeout={smoke.timed_out}, completed_oracle={SMOKE_MARKER in output}"
                        )
                    result["smoke"] = "PASS"
                    if args.guest_toolchain:
                        phase = "toolchain"
                        result["toolchain"] = "FAIL"
                        result["toolchain_packages"] = ensure_command_line_tools(
                            prefix=prefix, launcher=str(runtime.launcher), cwd=Path(self.topdir), env=env, log=emit,
                        )
                        result["toolchain"] = "PASS"
                finally:
                    result["cleanup"] = owner.shutdown(prefix, extra_env=env)
            if not result["cleanup"]:
                phase = "shutdown"
                raise RuntimeInstallationError("product shutdown or independent prefix cleanliness check failed")
            result["status"] = "PASS"
        except (OSError, ValueError, GuestToolchainError, subprocess.CalledProcessError) as error:
            result.update(phase=phase, error=str(error))
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                self.err(f"bootstrap {phase} failed: {error}\ncleanup: {result['cleanup']}")
            raise SystemExit(getattr(error, "returncode", 1) or 1)
        finally:
            lifetime.close()
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            self.inf(
                f"runtime: {root}\nguest state: {prefix}\nsmoke: {result['smoke']}\n"
                f"toolchain: {result['toolchain']}\ncleanup: {result['cleanup']}"
            )

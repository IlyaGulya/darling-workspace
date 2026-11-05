"""Build a configured Darling product and publish its normal CMake installation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_install import (
    RuntimeInstallationError,
    build_lock,
    build_runtime,
    cmake_cache,
    configured_runtime_root,
    install_runtime,
)
from runtime_retention import RuntimeRetentionError, require_publication_budget


class DarlingBuild(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-build", "Build and install the normal Darling product",
            "Build a configured product; optionally publish a fresh, immutable CMake installation",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument("--build-dir", required=True, type=Path)
        parser.add_argument("--targets", nargs="+", help="narrow a build-only operation")
        parser.add_argument("--install", action="store_true", help="publish the complete normal CMake installation")
        parser.add_argument("--runtime-root", type=Path, help="must equal the configured CMAKE_INSTALL_PREFIX")
        parser.add_argument("--with-test-data", action="store_true", help="also install the product's optional tests-data component")
        parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
        parser.add_argument("--json", action="store_true")
        return parser

    def do_run(self, args, unknown):
        if args.install and args.targets:
            self.die("--targets narrows build-only work; --install builds the complete configured product")
        build = args.build_dir.expanduser().resolve()
        try:
            with build_lock(build):
                cache = cmake_cache(build)
                root = (args.runtime_root or configured_runtime_root(build)).expanduser().resolve()
                if root != configured_runtime_root(build):
                    raise RuntimeInstallationError(
                        f"CMAKE_INSTALL_PREFIX is {configured_runtime_root(build)}, not {root}; configure the final root before building"
                    )
                if args.install and root.exists() and any(root.iterdir()):
                    raise RuntimeInstallationError(
                        f"will not overwrite published or partial installation {root}; configure a fresh runtime root"
                    )
                require_publication_budget(root)
                build_runtime(build, targets=args.targets or (), jobs=args.jobs)
                runtime = install_runtime(
                    build, root, manifest_repo=self.manifest.repo_abspath,
                    with_test_data=args.with_test_data,
                ) if args.install else None
                result = {
                    "status": "PASS", "operation": "build-install" if args.install else "build",
                    "source_root": cache.get("CMAKE_HOME_DIRECTORY"),
                    "build_dir": str(build), "runtime_root": str(root),
                    "installed": runtime is not None,
                    "installation_identity": runtime.summary() if runtime else None,
                    "retention": list(runtime.retention) if runtime else [],
                }
        except (OSError, RuntimeInstallationError, RuntimeRetentionError, subprocess.CalledProcessError) as error:
            if args.json:
                print(json.dumps({"status": "FAIL", "build_dir": str(build), "error": str(error)}, sort_keys=True))
            else:
                self.err(str(error))
            raise SystemExit(getattr(error, "returncode", 1) or 1)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            self.inf(f"build: {build}\nsource: {result['source_root']}\ninstallation: {root}\ninstalled: {result['installed']}")
            self.inf("Source integrity and runtime behavior are separate checks; use darling-doctor and the workload's completed oracle.")

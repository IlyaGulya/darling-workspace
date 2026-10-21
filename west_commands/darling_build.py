"""`west darling-build` — build (and optionally deploy) the Darling closure with a doctor gate.

There is no build script in this workspace; the closure is built manually with ninja in the
prefix-matched build dir (~/work/darling-build) and deployed by copying into BOTH closure copies of
the install prefix. That manual flow is exactly what let #89/#90 happen (wrong build dir, drifted
sources). This wrapper enforces the guard rails:

  1. PRE-GATE: run `west darling-doctor`. If it fails (wrong build-dir prefix, undeclared
     manifest<->worktree drift, or a deployed-baseline mismatch), REFUSE to build unless --force.
  2. BUILD: ninja the requested targets in the build dir (default: dyld + the closure dylibs).
     For focused validation, --deploy-closure-names can narrow the default build/deploy set to the
     named closure dylibs.
  3. DEPLOY (opt-in --deploy): copy the selected runtime artifacts into the prefix after backing
     up what is there. Default deploys copy dyld + closure dylibs into BOTH closure copies; focused
     deploys with explicit --targets only copy the requested non-closure runtime binaries unless
     --deploy-closure-names or a dyld target asks for closure/dyld deploy. They can also sync test
     prefixes with --deploy-extra-prefix and opt into deploying darlingserver and selected boot
     binaries when guest/server ABI must move in lock-step.
  4. POST-CHECK (only with --deploy): re-run the doctor so a bad deploy is caught immediately,
     including extra-prefix closure consistency when requested.

This does NOT invent a new build system — it wraps the existing manual ninja/deploy so the checks
that would have saved us run automatically. Read-only unless --deploy is passed.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from west.commands import WestCommand
try:
    from .deploy_receipt import build_receipt, receipt_path, write_receipt
    from .deploy_transaction import DeploymentTransaction, DeploymentTransactionError
    from .runtime_artifacts import COMPONENTS as RUNTIME_COMPONENTS, install_and_verify
    from .prefix_repair import prefix_mount_targets
    from .test_prefix import rootless_prefix_process_snapshot
except ImportError:
    from deploy_receipt import build_receipt, receipt_path, write_receipt
    from deploy_transaction import DeploymentTransaction, DeploymentTransactionError
    from runtime_artifacts import COMPONENTS as RUNTIME_COMPONENTS, install_and_verify
    from prefix_repair import prefix_mount_targets
    from test_prefix import rootless_prefix_process_snapshot

# the 35 closure dylib basenames + libSystem.B, matched by find under the build dir.
# dyld itself is handled separately (its target path is fixed).
_CLOSURE_BASENAMES = [
    "libcache.dylib", "libcommonCrypto.dylib", "libcompiler_rt.dylib", "libcopyfile.dylib",
    "libcorecrypto.dylib", "libdispatch.dylib", "libdyld.dylib", "libkeymgr.dylib", "libkxld.dylib",
    "liblaunch.dylib", "libmacho.dylib", "libquarantine.dylib", "libremovefile.dylib",
    "libsystem_asl.dylib", "libSystem.B.dylib", "libsystem_blocks.dylib", "libsystem_c.dylib",
    "libsystem_configuration.dylib", "libsystem_coreservices.dylib", "libsystem_coretls.dylib",
    "libsystem_darwin.dylib", "libsystem_dnssd.dylib", "libsystem_duct.dylib", "libsystem_info.dylib",
    "libsystem_kernel.dylib", "libsystem_malloc.dylib", "libsystem_m.dylib",
    "libsystem_networkextension.dylib", "libsystem_notify.dylib", "libsystem_platform.dylib",
    "libsystem_pthread.dylib", "libsystem_sandbox.dylib", "libsystem_trace.dylib", "libunwind.dylib",
    "libxpc.dylib",
]
_DYLD_TARGET = "src/external/dyld/dyld"
_LAUNCHER_TARGET = "src/startup/darling"
_DARLINGSERVER_TARGET = "src/external/darlingserver/darlingserver"
_MLDR_TARGETS = [
    "src/startup/mldr/mldr",
    "src/startup/mldr/mldr32",
]
_MLDR_DEPLOYS = [
    ("src/startup/mldr/mldr", "libexec/darling/usr/libexec/darling/mldr"),
    ("src/startup/mldr/mldr32", "libexec/darling/usr/libexec/darling/mldr32"),
]
_BOOTCHAIN_TARGETS = [
    *_MLDR_TARGETS,
    "src/launchd/src/launchd",
    "src/shellspawn/shellspawn",
]
_SHELLSPAWN_DEPLOY = ("src/shellspawn/shellspawn", "libexec/darling/usr/libexec/shellspawn")
_BOOTCHAIN_DEPLOYS = [
    *_MLDR_DEPLOYS,
    ("src/launchd/src/launchd", "libexec/darling/sbin/launchd"),
    _SHELLSPAWN_DEPLOY,
]
# Components whose working-tree content is compiled into the deployed runtime. A deploy
# compiles whatever is in the tree, so an uncommitted experiment becomes deployed bytes
# with no committed provenance -- MEASURED 2026-10-10: a diagnostic probe in
# libsystem_kernel/sigexc shipped this way and killed shellspawn with SIGSEGV at 0x0,
# which the boot smoke could only report as "shellspawn did not become ready".
_RUNTIME_COMPONENTS = [
    ("darling", "."),
    ("darlingserver", "src/external/darlingserver"),
    ("xnu", "src/external/xnu"),
    ("libplatform", "src/external/libplatform"),
    ("libdispatch", "src/external/libdispatch"),
    ("libpthread", "src/external/libpthread"),
]


class DarlingBuild(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-build",
            "Build (and optionally deploy) the Darling closure behind a doctor gate",
            "Doctor-gated ninja build of dyld + closure, with opt-in deploy to both prefix copies",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        p = parser_adder.add_parser(self.name, description=self.description)
        p.add_argument("--build-dir", default=os.environ.get("DARLING_BUILD_DIR", str(Path.home() / "work/darling-build")))
        p.add_argument("--prefix", default=os.environ.get("DARLING_PREFIX", str(Path.home() / "work/darling-prefix")))
        p.add_argument("--targets", nargs="*", default=None,
                       help="ninja targets to build (default: dyld + all closure dylibs)")
        p.add_argument("--deploy", action="store_true",
                       help="after building, deploy selected runtime artifacts into the prefix. "
                            "The deploy is transactional by default: every replaced file and the "
                            "receipt are restored if the post-deploy check fails")
        p.add_argument("--deploy-manifest", metavar="PATH",
                       help="write the deploy transaction manifest at PATH (requires --deploy; "
                            "default: <prefix>/.darling-deploy-txn/<utc>-<pid>.json)")
        p.add_argument("--restore-deploy", metavar="PATH",
                       help="restore one deploy manifest without building")
        p.add_argument("--deploy-component", action="append", default=[], metavar="NAME",
                       choices=sorted(RUNTIME_COMPONENTS),
                       help="with --deploy, also install these built runtime components into "
                            "EVERY copy the loader uses (CoreFoundation, Foundation, "
                            "libsystem_kernel, ...); repeatable")
        p.add_argument("--deploy-only", action="store_true",
                       help="skip doctor, build and provenance; only install --deploy-component "
                            "(requires --deploy-component)")
        p.add_argument("--deploy-extra-prefix", action="append", default=[], metavar="PREFIX",
                       help="with --deploy, also copy closure dylibs into this additional prefix root (repeatable)")
        p.add_argument("--deploy-closure-names", nargs="*", default=None, metavar="BASENAME",
                       help="with --deploy, copy only these closure dylib basenames; also narrows the default build target set")
        p.add_argument("--no-deploy-dyld", action="store_true",
                       help="with --deploy, do not copy dyld (useful for focused closure-dylib validation)")
        p.add_argument("--allow-stale-dyld-for-kernel", action="store_true",
                       help="allow --no-deploy-dyld with libsystem_kernel.dylib; validation will not cover dyld's static emulation_dyld path")
        p.add_argument("--allow-dirty-components", action="store_true",
                       help="deploy a runtime built from components whose working tree has uncommitted "
                            "changes; the deployed bytes then match no committed revision")
        p.add_argument("--deploy-darlingserver", action="store_true",
                       help="with --deploy, also copy the freshly built darlingserver into prefix/bin")
        p.add_argument("--deploy-launcher", action="store_true",
                       help="with --deploy, also copy the freshly built host launcher into prefix/bin")
        p.add_argument("--deploy-mldr", action="store_true",
                       help="with --deploy, also copy freshly built mldr/mldr32 into the base tree")
        p.add_argument("--deploy-shellspawn", action="store_true",
                       help="with --deploy, also copy freshly built shellspawn into the base tree")
        p.add_argument("--deploy-bootchain", action="store_true",
                       help="with --deploy, also copy mldr/mldr32/launchd/shellspawn into the base tree")
        p.add_argument("--shutdown-before-deploy", action="store_true",
                       help="with --deploy, shut down affected prefixes before overwriting live runtime binaries")
        p.add_argument("--skip-post-doctor", action="store_true",
                       help="with --deploy, skip the post-deploy doctor (use only for intentional local dirty deploys)")
        p.add_argument("--skip-boot-smoke", action="store_true",
                       help="with --deploy, skip the automatic boot smoke inside the post-deploy "
                       "doctor. The smoke starts the prefix and requires a guest shell, and a "
                       "failure rolls the deploy back; skip it only when the prefix is deliberately "
                       "not bootable at this point")
        p.add_argument("--force", action="store_true",
                       help="build even if the doctor pre-gate fails (records the override loudly)")
        p.add_argument("--skip-doctor", action="store_true", help="skip the doctor pre-gate entirely (discouraged)")
        return p

    def do_run(self, args, unknown):
        topdir = Path(self.topdir)
        build_dir = Path(args.build_dir)
        prefix = Path(args.prefix)

        if args.deploy_only:
            if not args.deploy_component:
                self.die("--deploy-only requires --deploy-component")
            rc = install_and_verify(prefix, build_dir, args.deploy_component)
            if rc != 0:
                raise SystemExit(rc)
            return
        if args.deploy_component and not args.deploy:
            self.die("--deploy-component requires --deploy (or --deploy-only)")
        if args.deploy and args.restore_deploy:
            self.die("--deploy and --restore-deploy are mutually exclusive")
        if args.deploy_manifest and not args.deploy:
            self.die("--deploy-manifest requires --deploy")

        with self._build_dir_lock(build_dir):
            if args.restore_deploy:
                self._restore_deploy(Path(args.restore_deploy), prefix)
                return
            self._run_locked(args, topdir, build_dir, prefix)

    def _run_locked(self, args, topdir, build_dir, prefix):
        # ---- 1. doctor pre-gate ----
        if not args.skip_doctor:
            self.inf("== doctor pre-gate ==")
            rc = self._doctor(topdir, build_dir=build_dir, prefix=prefix)
            if rc != 0:
                if args.force:
                    self.wrn("doctor FAILED but --force given — proceeding anyway (override).")
                else:
                    self.err("doctor pre-gate FAILED — refusing to build. Fix the issues or pass --force.")
                    raise SystemExit(1)
        else:
            self.wrn("skipping doctor pre-gate (--skip-doctor)")

        # ---- 2. build ----
        cache = build_dir / "CMakeCache.txt"
        if not cache.exists():
            self.err(f"no CMakeCache.txt in {build_dir}; not a configured build dir")
            raise SystemExit(1)
        deploy_closure_names = self._deploy_closure_names(args)
        deploy_dyld = self._deploy_dyld(args, deploy_closure_names)
        provenance = ""
        if args.deploy:
            # Refuse before the build, not after: the build is what turns an uncommitted
            # experiment into runtime bytes, and MEASURED such a probe killed shellspawn.
            provenance = self._deploy_provenance(topdir)
            if provenance and not args.allow_dirty_components:
                self.err(provenance)
                self.err(
                    "refusing to deploy a runtime built from uncommitted component changes: the "
                    "deployed bytes would match no committed revision. Commit or stash them, or "
                    "pass --allow-dirty-components to deploy them on purpose."
                )
                raise SystemExit(1)
        if args.targets:
            targets = list(args.targets)
        elif args.deploy_closure_names is not None:
            targets = self._closure_targets(build_dir, args.deploy_closure_names)
        else:
            targets = [_DYLD_TARGET] + self._closure_targets(build_dir)
        if (
            args.deploy
            and args.no_deploy_dyld
            and deploy_closure_names is not None
            and "libsystem_kernel.dylib" in deploy_closure_names
            and not args.allow_stale_dyld_for_kernel
        ):
            self.err(
                "--no-deploy-dyld with libsystem_kernel.dylib leaves dyld's static "
                "emulation_dyld copy stale. Either deploy dyld too, or pass "
                "--allow-stale-dyld-for-kernel and treat runtime results as "
                "closure-only validation."
            )
            raise SystemExit(1)
        if deploy_dyld and _DYLD_TARGET not in targets:
            targets.insert(0, _DYLD_TARGET)
        if args.deploy and args.deploy_darlingserver and _DARLINGSERVER_TARGET not in targets:
            targets.append(_DARLINGSERVER_TARGET)
        if args.deploy and args.deploy_launcher and _LAUNCHER_TARGET not in targets:
            targets.append(_LAUNCHER_TARGET)
        if args.deploy and args.deploy_mldr:
            for target in _MLDR_TARGETS:
                if target not in targets:
                    targets.append(target)
        if args.deploy and args.deploy_shellspawn and _SHELLSPAWN_DEPLOY[0] not in targets:
            targets.append(_SHELLSPAWN_DEPLOY[0])
        if args.deploy and args.deploy_bootchain:
            for target in _BOOTCHAIN_TARGETS:
                if target not in targets:
                    targets.append(target)
        self.inf(f"== ninja ({len(targets)} targets) in {build_dir} ==")
        rc = subprocess.run(["ninja", *targets], cwd=build_dir).returncode
        if rc != 0:
            self.err("ninja build FAILED")
            raise SystemExit(rc)
        self.inf("build OK")

        # ---- 3. deploy (opt-in) ----
        if args.deploy:
            extra_prefixes = [Path(p) for p in args.deploy_extra_prefix]
            if args.shutdown_before_deploy:
                self._shutdown_prefixes(prefix, extra_prefixes)
            # ALWAYS transactional: the deploy replaces live runtime files, and any
            # failure (including the post-deploy boot smoke) must leave the prefix
            # exactly as it was. Without this, a failed deploy kept the new files
            # while the receipt was restored, so the chain was left half-updated and
            # every later doctor check reported the mismatch.
            try:
                transaction = DeploymentTransaction(
                    self._deploy_manifest_path(args, prefix), prefix, extra_prefixes
                )
            except DeploymentTransactionError as error:
                self.die(str(error))
            self._warn_deploy_scope(args, deploy_closure_names, deploy_dyld)
            if provenance:
                self.wrn(provenance)
            try:
                receipt_written = None
                receipt_backup = None
                deployed_pairs = self._deploy(
                    build_dir,
                    prefix,
                    closure_names=deploy_closure_names,
                    deploy_dyld=deploy_dyld,
                    deploy_darlingserver=args.deploy_darlingserver,
                    deploy_launcher=args.deploy_launcher,
                    deploy_mldr=args.deploy_mldr,
                    deploy_shellspawn=args.deploy_shellspawn,
                    deploy_bootchain=args.deploy_bootchain,
                    extra_prefixes=extra_prefixes,
                    transaction=transaction,
                )
                if args.deploy_component:
                    self.inf("== deploy native components ==")
                    rc = install_and_verify(prefix, build_dir, args.deploy_component)
                    if rc != 0:
                        raise SystemExit(rc)
                # Record what was deployed BEFORE the post-deploy doctor runs:
                # the doctor's default receipt mode verifies the deployed bytes
                # against this deploy, not against the historical baseline.
                receipt_backup = self._snapshot_receipt(prefix)
                receipt_written = self._write_deploy_receipt(
                    prefix, build_dir, deployed_pairs
                )
                # ---- 4. post-deploy doctor ----
                if args.skip_post_doctor:
                    self.wrn("skipping post-deploy doctor (--skip-post-doctor)")
                else:
                    self.inf("== post-deploy doctor ==")
                    rc = self._doctor(
                        topdir,
                        build_dir=build_dir,
                        prefix=prefix,
                        extra_prefixes=extra_prefixes,
                        boot_smoke=not args.skip_boot_smoke,
                    )
                    if rc != 0:
                        self.err(
                            "post-deploy check FAILED — restoring the previous deployment "
                            "(runtime files and receipt) from the deploy transaction."
                        )
                        self.err(self._deploy_scope_hint(args, deploy_closure_names, deploy_dyld))
                        if provenance:
                            self.err(provenance)
                        raise SystemExit(1)
                transaction.commit()
                self.inf(f"transactional deploy manifest: {transaction.manifest_path}")
            except BaseException:
                if receipt_written is not None:
                    self._restore_receipt(prefix, receipt_backup)
                try:
                    transaction.rollback()
                except DeploymentTransactionError as error:
                    self.err(f"transaction rollback failed: {error}")
                raise
        else:
            self.inf("(not deployed; pass --deploy to copy into the prefix)")

    # -- helpers -----------------------------------------------------------
    @contextmanager
    def _build_dir_lock(self, build_dir):
        lock_path = build_dir / ".west-darling-build.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock:
            self.inf(f"== build-dir lock: {lock_path} ==")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _doctor(self, topdir, build_dir=None, prefix=None, extra_prefixes=None, boot_smoke=False):
        cmd = ["west", "darling-doctor"]
        if build_dir is not None:
            cmd.extend(["--build-dir", str(build_dir)])
        if prefix is not None:
            cmd.extend(["--prefix", str(prefix)])
        if boot_smoke:
            # Only the post-deploy gate asks for this: it starts the prefix and requires a guest
            # shell, which is the one property the artifact comparisons cannot see. It costs the
            # failure path's 30s shellspawn deadline plus cleanup when the chain is broken, and a
            # few seconds when it is healthy.
            cmd.append("--boot-smoke")
        for extra in extra_prefixes or []:
            cmd.extend(["--extra-prefix", str(extra)])
        return subprocess.run(cmd, cwd=topdir).returncode

    def _deploy_closure_names(self, args):
        """Return the closure deploy scope for this invocation.

        None means the full closure, a list means that exact subset, and an
        empty list means this deploy is limited to explicitly requested
        non-closure runtime binaries such as mldr or shellspawn.
        """
        if not args.deploy:
            return []
        if args.deploy_closure_names is not None:
            return list(args.deploy_closure_names)
        if args.targets:
            return []
        return None

    def _deploy_dyld(self, args, closure_names) -> bool:
        if not args.deploy or args.no_deploy_dyld:
            return False
        if closure_names is None:
            return True
        if closure_names:
            return True
        return bool(args.targets and _DYLD_TARGET in args.targets)

    def _shutdown_prefixes(self, prefix, extra_prefixes=None):
        launcher = prefix / "bin/darling"
        roots = [prefix] + list(extra_prefixes or [])
        self.inf(f"== shutdown before deploy ({len(roots)} prefixes) ==")
        for root in roots:
            if launcher.exists():
                env = os.environ.copy()
                env["DPREFIX"] = str(root)
                subprocess.run([str(launcher), "shutdown"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            self._kill_dserver_for_prefix(root)

    def _kill_dserver_for_prefix(self, prefix):
        r = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False)
        pids = []
        suffix = f"darlingserver {prefix}"
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_text, _, args = line.partition(" ")
            if not pid_text.isdigit():
                continue
            argv = args.split()
            if len(argv) >= 2 and Path(argv[0]).name == "darlingserver" and argv[1] == str(prefix):
                pids.append(int(pid_text))
            elif args == suffix:
                pids.append(int(pid_text))
        if not pids:
            return
        self.wrn(f"stopping live darlingserver for {prefix}: pids={pids}")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(1)
        live = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                live.append(pid)
            except ProcessLookupError:
                pass
        for pid in live:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if live:
            time.sleep(0.25)

    def _restore_deploy(self, manifest_path, prefix):
        try:
            roots = DeploymentTransaction.manifest_roots(manifest_path, prefix)
            for root in roots:
                self._assert_prefix_idle(root)
            DeploymentTransaction.restore(manifest_path, prefix)
        except (DeploymentTransactionError, OSError, ValueError) as error:
            self.die(f"cannot restore deploy transaction: {error}")
        self.inf(f"restored deploy transaction: {manifest_path}")

    def _assert_prefix_idle(self, prefix):
        mounts = prefix_mount_targets(prefix)
        if mounts:
            self.die(
                "refusing deploy restore while prefix has mounted filesystems: "
                + ", ".join(str(path) for path in mounts)
            )
        processes = rootless_prefix_process_snapshot(prefix)
        listing = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False
        )
        prefix_text = str(prefix.resolve())
        for line in listing.stdout.splitlines():
            pid, separator, args = line.strip().partition(" ")
            if not separator or not pid.isdigit() or prefix_text not in args:
                continue
            name = Path(args.split()[0]).name
            if name in {"darling", "darlingserver", "mldr", "launchd", "shellspawn", "vchroot"}:
                processes.append(f"{pid} {args}")
        if processes:
            self.die(
                "refusing deploy restore while prefix owns Darling process(es): "
                + "; ".join(sorted(set(processes)))
            )

    def _closure_targets(self, build_dir, names=None):
        """Resolve each closure basename to its newest built artifact path (relative to build_dir)."""
        targets = []
        for name in (names if names is not None else _CLOSURE_BASENAMES):
            if name not in _CLOSURE_BASENAMES:
                self.err(f"unknown closure dylib basename: {name}")
                raise SystemExit(1)
            r = subprocess.run(
                ["find", ".", "-name", name, "-type", "f", "-printf", "%T@ %p\n"],
                cwd=build_dir, capture_output=True, text=True, check=False,
            )
            best = None
            best_t = -1.0
            for line in r.stdout.splitlines():
                try:
                    t, path = line.split(" ", 1)
                except ValueError:
                    continue
                if "CMakeFiles" in path:
                    continue
                if float(t) > best_t:
                    best_t, best = float(t), path.lstrip("./")
            if best:
                targets.append(best)
            else:
                self.wrn(f"closure dylib not found in build tree: {name} (skipped)")
        return targets

    def _write_deploy_receipt(self, prefix, build_dir, deployed_pairs):
        """Record the deployed bytes against the current build/workspace."""
        if not deployed_pairs:
            return None
        manifest = getattr(self, "manifest", None)
        repo_abspath = getattr(manifest, "repo_abspath", None)
        topdir = getattr(self, "topdir", None)
        if not repo_abspath or not topdir:
            self.wrn("cannot identify the workspace; no deployment receipt written")
            return None
        receipt = build_receipt(
            manifest_repo=Path(repo_abspath),
            topdir=Path(topdir),
            build_dir=Path(build_dir),
            prefix=Path(prefix),
            deployed=deployed_pairs,
        )
        path = write_receipt(Path(prefix), receipt)
        self.inf(f"deployment receipt: {path}")
        return path

    def _snapshot_receipt(self, prefix):
        path = receipt_path(Path(prefix))
        return path.read_bytes() if path.is_file() else None

    def _restore_receipt(self, prefix, previous):
        path = receipt_path(Path(prefix))
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)

    def _deploy_manifest_path(self, args, prefix) -> Path:
        """Where this deploy records its transaction.

        An explicit --deploy-manifest path wins; otherwise every deploy gets its own
        manifest (and its own .backups tree) under the prefix, so a rollback can never
        restore a backup that a later deploy has already overwritten.
        """
        if args.deploy_manifest:
            return Path(args.deploy_manifest)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return Path(prefix) / ".darling-deploy-txn" / f"{stamp}-{os.getpid()}.json"

    def _deploy_scope_parts(self, args, closure_names, deploy_dyld):
        """Return (replaced, missing): the runtime parts this deploy replaces, and the
        coherence flags it leaves unset. The runtime is one chain -- closure+dyld,
        darlingserver and the bootchain must move to one build together -- so a partial
        deploy can leave a set that boots broken.
        """
        parts = []
        if deploy_dyld:
            parts.append("dyld")
        if closure_names is None:
            parts.append("closure dylibs")
        elif closure_names:
            parts.append(f"{len(closure_names)} closure dylib(s)")
        if args.deploy_darlingserver:
            parts.append("darlingserver")
        if args.deploy_launcher:
            parts.append("launcher")
        if args.deploy_mldr or args.deploy_shellspawn or args.deploy_bootchain:
            parts.append("bootchain")
        missing = []
        if "darlingserver" not in parts:
            missing.append("--deploy-darlingserver")
        if "bootchain" not in parts:
            missing.append("--deploy-bootchain")
        return parts, missing

    def _deploy_scope_hint(self, args, closure_names, deploy_dyld) -> str:
        """Say what this deploy left in place, for the message after a failed boot check."""
        parts, missing = self._deploy_scope_parts(args, closure_names, deploy_dyld)
        hint = "this deploy replaced " + (", ".join(parts) if parts else "nothing") + "."
        if missing:
            hint += (
                " A partial deploy can leave the chain mixed: retry with "
                + " ".join(missing)
                + " so the whole runtime moves to one build."
            )
        return hint

    def _dirty_components(self, topdir) -> list[str]:
        """Name the runtime components whose working tree has uncommitted changes.

        The build compiles the tree as it is, so a dirty component ships uncommitted
        content into the prefix: the deployed bytes then have no committed provenance,
        and MEASURED such a diagnostic probe crashed the boot chain.
        """
        dirty = []
        for name, rel in _RUNTIME_COMPONENTS:
            root = Path(topdir) / "darling"
            if rel != ".":
                root = root / rel
            if not (root / ".git").exists():
                continue
            completed = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            changes = [line for line in (getattr(completed, "stdout", "") or "").splitlines() if line.strip()]
            if changes:
                dirty.append(f"{name} ({len(changes)} path(s))")
        return dirty

    def _deploy_provenance(self, topdir) -> str:
        """Describe the source provenance of the runtime this deploy will install."""
        dirty = self._dirty_components(topdir)
        if not dirty:
            return ""
        return (
            "working-tree provenance: a deploy compiles the tree as it is, and these runtime "
            "components have uncommitted changes: " + ", ".join(dirty)
            + ". The deployed bytes then match no committed revision; commit or stash them "
            "before trusting a deploy."
        )

    def _warn_deploy_scope(self, args, closure_names, deploy_dyld) -> None:
        parts, missing = self._deploy_scope_parts(args, closure_names, deploy_dyld)
        self.inf("deploy scope: " + (", ".join(parts) if parts else "nothing"))
        if missing:
            self.wrn(
                "partial deploy: the rest of the runtime stays as deployed, and the runtime is "
                "one chain, so a mixed set may not boot. Add "
                + " ".join(missing)
                + " to move it together. (The deploy is transactional: a failed post-deploy "
                "check restores the previous deployment.)"
            )

    def _deploy(self, build_dir, prefix, closure_names=None, deploy_dyld=True,
                deploy_darlingserver=False, deploy_launcher=False, deploy_mldr=False,
                deploy_shellspawn=False, deploy_bootchain=False,
                extra_prefixes=None, transaction=None):
        base = prefix / "libexec/darling"
        roots = [prefix] + list(extra_prefixes or [])
        dyld_src = build_dir / _DYLD_TARGET
        launcher_src = build_dir / _LAUNCHER_TARGET
        dserver_src = build_dir / _DARLINGSERVER_TARGET
        if deploy_dyld and not dyld_src.exists():
            self.err(f"built dyld missing at {dyld_src}")
            raise SystemExit(1)
        if deploy_darlingserver and not dserver_src.exists():
            self.err(f"built darlingserver missing at {dserver_src}")
            raise SystemExit(1)
        if deploy_launcher and not launcher_src.exists():
            self.err(f"built launcher missing at {launcher_src}")
            raise SystemExit(1)
        if deploy_mldr:
            for src_rel, _ in _MLDR_DEPLOYS:
                src = build_dir / src_rel
                if not src.exists():
                    self.err(f"built mldr binary missing at {src}")
                    raise SystemExit(1)
        if deploy_shellspawn:
            src = build_dir / _SHELLSPAWN_DEPLOY[0]
            if not src.exists():
                self.err(f"built shellspawn missing at {src}")
                raise SystemExit(1)
        if deploy_bootchain:
            for src_rel, _ in _BOOTCHAIN_DEPLOYS:
                src = build_dir / src_rel
                if not src.exists():
                    self.err(f"built boot-chain binary missing at {src}")
                    raise SystemExit(1)

        # No ad-hoc backup tree here: every replaced file is recorded by the deploy
        # transaction, which restores it (and the receipt) when the post-deploy
        # check fails. The removed .doctor-deploy-bak tree was write-only, was not
        # timestamped despite its comment, and was overwritten by each deploy, so a
        # rollback could restore a state that was already half-updated.
        self.inf(f"== deploy to {1 + len(roots)} closure copies ==")

        resolved = self._closure_targets(build_dir, closure_names)
        name_to_path = {Path(t).name: build_dir / t for t in resolved}
        copy_deployed_file = transaction.replace if transaction is not None else shutil.copy2
        deployed_pairs: list[tuple[Path, Path]] = []

        def deploy_file(source: Path, destination: Path) -> None:
            copy_deployed_file(source, destination)
            deployed_pairs.append((source, destination))

        for tree in [base] + roots:
            if deploy_dyld:
                deploy_file(dyld_src, tree / "usr/lib/dyld")
            for name, src in name_to_path.items():
                if name == "libSystem.B.dylib":
                    deploy_file(src, tree / "usr/lib/libSystem.B.dylib")
                else:
                    deploy_file(src, tree / "usr/lib/system" / name)
        if deploy_darlingserver:
            for root in roots:
                dst = root / "bin/darlingserver"
                dst.parent.mkdir(parents=True, exist_ok=True)
                deploy_file(dserver_src, dst)
        if deploy_launcher:
            for root in roots:
                dst = root / "bin/darling"
                dst.parent.mkdir(parents=True, exist_ok=True)
                deploy_file(launcher_src, dst)
        if deploy_mldr:
            for src_rel, dst_rel in _MLDR_DEPLOYS:
                dst = prefix / dst_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                deploy_file(build_dir / src_rel, dst)
        if deploy_shellspawn:
            src_rel, dst_rel = _SHELLSPAWN_DEPLOY
            dst = prefix / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            deploy_file(build_dir / src_rel, dst)
        if deploy_bootchain:
            for src_rel, dst_rel in _BOOTCHAIN_DEPLOYS:
                dst = prefix / dst_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                deploy_file(build_dir / src_rel, dst)
        dyld_note = "dyld + " if deploy_dyld else ""
        target_count = 1 + len(roots)
        dserver_note = " + darlingserver" if deploy_darlingserver else ""
        launcher_note = " + launcher" if deploy_launcher else ""
        mldr_note = " + mldr" if deploy_mldr else ""
        shellspawn_note = " + shellspawn" if deploy_shellspawn else ""
        bootchain_note = " + bootchain" if deploy_bootchain else ""
        self.inf(f"deployed {dyld_note}{len(name_to_path)} closure dylibs to {target_count} closure copies{dserver_note}{launcher_note}{mldr_note}{shellspawn_note}{bootchain_note}")
        self.wrn("NOTE: this changes the SHARED base tree (affects all prefixes). The current "
                 "deployment receipt records this deploy; update "
                 "darling-workspace/deploy-baseline.md5 only for a new known-good regression set.")
        return deployed_pairs

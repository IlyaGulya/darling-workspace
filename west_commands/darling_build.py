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
  3. DEPLOY (opt-in --deploy): copy the freshly built dyld + closure dylibs into BOTH closure
     copies of the prefix, after backing up what is there. Focused deploys can skip dyld with
     --no-deploy-dyld, sync test prefixes with --deploy-extra-prefix, and opt into deploying
     darlingserver and selected boot binaries when guest/server ABI must move in lock-step.
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
                       help="after building, copy dyld+closure into BOTH prefix closure copies (backs up first)")
        p.add_argument("--deploy-extra-prefix", action="append", default=[], metavar="PREFIX",
                       help="with --deploy, also copy closure dylibs into this additional prefix root (repeatable)")
        p.add_argument("--deploy-closure-names", nargs="*", default=None, metavar="BASENAME",
                       help="with --deploy, copy only these closure dylib basenames; also narrows the default build target set")
        p.add_argument("--no-deploy-dyld", action="store_true",
                       help="with --deploy, do not copy dyld (useful for focused closure-dylib validation)")
        p.add_argument("--allow-stale-dyld-for-kernel", action="store_true",
                       help="allow --no-deploy-dyld with libsystem_kernel.dylib; validation will not cover dyld's static emulation_dyld path")
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
        p.add_argument("--force", action="store_true",
                       help="build even if the doctor pre-gate fails (records the override loudly)")
        p.add_argument("--skip-doctor", action="store_true", help="skip the doctor pre-gate entirely (discouraged)")
        return p

    def do_run(self, args, unknown):
        topdir = Path(self.topdir)
        build_dir = Path(args.build_dir)
        prefix = Path(args.prefix)

        with self._build_dir_lock(build_dir):
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
        if args.targets:
            targets = args.targets
        elif args.deploy_closure_names is not None:
            targets = self._closure_targets(build_dir, args.deploy_closure_names)
        else:
            targets = [_DYLD_TARGET] + self._closure_targets(build_dir)
        if (
            args.deploy
            and args.no_deploy_dyld
            and args.deploy_closure_names is not None
            and "libsystem_kernel.dylib" in args.deploy_closure_names
            and not args.allow_stale_dyld_for_kernel
        ):
            self.err(
                "--no-deploy-dyld with libsystem_kernel.dylib leaves dyld's static "
                "emulation_dyld copy stale. Either deploy dyld too, or pass "
                "--allow-stale-dyld-for-kernel and treat runtime results as "
                "closure-only validation."
            )
            raise SystemExit(1)
        if args.deploy and not args.no_deploy_dyld and _DYLD_TARGET not in targets:
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
            self._deploy(
                build_dir,
                prefix,
                closure_names=args.deploy_closure_names,
                deploy_dyld=not args.no_deploy_dyld,
                deploy_darlingserver=args.deploy_darlingserver,
                deploy_launcher=args.deploy_launcher,
                deploy_mldr=args.deploy_mldr,
                deploy_shellspawn=args.deploy_shellspawn,
                deploy_bootchain=args.deploy_bootchain,
                extra_prefixes=extra_prefixes,
            )
            # ---- 4. post-deploy doctor ----
            if args.skip_post_doctor:
                self.wrn("skipping post-deploy doctor (--skip-post-doctor)")
            else:
                self.inf("== post-deploy doctor ==")
                rc = self._doctor(topdir, build_dir=build_dir, prefix=prefix, extra_prefixes=extra_prefixes)
                if rc != 0:
                    self.err("post-deploy doctor FAILED — the deploy may be inconsistent. Investigate.")
                    raise SystemExit(1)
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

    def _doctor(self, topdir, build_dir=None, prefix=None, extra_prefixes=None):
        cmd = ["west", "darling-doctor"]
        if build_dir is not None:
            cmd.extend(["--build-dir", str(build_dir)])
        if prefix is not None:
            cmd.extend(["--prefix", str(prefix)])
        for extra in extra_prefixes or []:
            cmd.extend(["--extra-prefix", str(extra)])
        return subprocess.run(cmd, cwd=topdir).returncode

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

    def _deploy(self, build_dir, prefix, closure_names=None, deploy_dyld=True,
                deploy_darlingserver=False, deploy_launcher=False, deploy_mldr=False,
                deploy_shellspawn=False, deploy_bootchain=False,
                extra_prefixes=None):
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

        # backup both closure copies once (timestamped) before overwriting
        bak = prefix / f".doctor-deploy-bak"
        self.inf(f"== deploy to {1 + len(roots)} closure copies (backup -> {bak}) ==")
        backup_trees = [(base, "base")] + [(root, f"root-{i}") for i, root in enumerate(roots)]
        for tree, tag in backup_trees:
            b = bak / tag / "usr/lib/system"
            b.mkdir(parents=True, exist_ok=True)
            backup_paths = []
            if deploy_dyld:
                backup_paths.append("usr/lib/dyld")
            names = closure_names if closure_names is not None else _CLOSURE_BASENAMES
            if "libSystem.B.dylib" in names:
                backup_paths.append("usr/lib/libSystem.B.dylib")
            for f in backup_paths:
                src = tree / f
                if src.exists():
                    dst = bak / tag / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            sysdir = tree / "usr/lib/system"
            if sysdir.is_dir():
                if closure_names is None:
                    files = sysdir.glob("*.dylib")
                else:
                    files = (sysdir / name for name in closure_names if name != "libSystem.B.dylib")
                for f in files:
                    if f.exists():
                        shutil.copy2(f, b / f.name)
        if deploy_darlingserver:
            for root in roots:
                src = root / "bin/darlingserver"
                if src.exists():
                    dst = bak / f"{root.name or 'prefix'}-bin" / "bin/darlingserver"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        if deploy_launcher:
            for root in roots:
                src = root / "bin/darling"
                if src.exists():
                    dst = bak / f"{root.name or 'prefix'}-bin" / "bin/darling"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        if deploy_mldr:
            for _, dst_rel in _MLDR_DEPLOYS:
                src = prefix / dst_rel
                if src.exists():
                    dst = bak / "base-mldr" / dst_rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        if deploy_shellspawn:
            _, dst_rel = _SHELLSPAWN_DEPLOY
            src = prefix / dst_rel
            if src.exists():
                dst = bak / "base-shellspawn" / dst_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        if deploy_bootchain:
            for _, dst_rel in _BOOTCHAIN_DEPLOYS:
                src = prefix / dst_rel
                if src.exists():
                    dst = bak / "base-bootchain" / dst_rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

        resolved = self._closure_targets(build_dir, closure_names)
        name_to_path = {Path(t).name: build_dir / t for t in resolved}
        for tree in [base] + roots:
            if deploy_dyld:
                shutil.copy2(dyld_src, tree / "usr/lib/dyld")
            for name, src in name_to_path.items():
                if name == "libSystem.B.dylib":
                    shutil.copy2(src, tree / "usr/lib/libSystem.B.dylib")
                else:
                    shutil.copy2(src, tree / "usr/lib/system" / name)
        if deploy_darlingserver:
            for root in roots:
                dst = root / "bin/darlingserver"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dserver_src, dst)
        if deploy_launcher:
            for root in roots:
                dst = root / "bin/darling"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(launcher_src, dst)
        if deploy_mldr:
            for src_rel, dst_rel in _MLDR_DEPLOYS:
                dst = prefix / dst_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(build_dir / src_rel, dst)
        if deploy_shellspawn:
            src_rel, dst_rel = _SHELLSPAWN_DEPLOY
            dst = prefix / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(build_dir / src_rel, dst)
        if deploy_bootchain:
            for src_rel, dst_rel in _BOOTCHAIN_DEPLOYS:
                dst = prefix / dst_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(build_dir / src_rel, dst)
        dyld_note = "dyld + " if deploy_dyld else ""
        target_count = 1 + len(roots)
        dserver_note = " + darlingserver" if deploy_darlingserver else ""
        launcher_note = " + launcher" if deploy_launcher else ""
        mldr_note = " + mldr" if deploy_mldr else ""
        shellspawn_note = " + shellspawn" if deploy_shellspawn else ""
        bootchain_note = " + bootchain" if deploy_bootchain else ""
        self.inf(f"deployed {dyld_note}{len(name_to_path)} closure dylibs to {target_count} closure copies{dserver_note}{launcher_note}{mldr_note}{shellspawn_note}{bootchain_note}")
        self.wrn("NOTE: this changes the SHARED base tree (affects all prefixes). If this is a new "
                 "known-good set, update darling-workspace/deploy-baseline.md5.")

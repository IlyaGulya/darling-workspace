"""`west darling-build` — build (and optionally deploy) the Darling closure with a doctor gate.

There is no build script in this workspace; the closure is built manually with ninja in the
prefix-matched build dir (~/work/darling-build) and deployed by copying into BOTH closure copies of
the install prefix. That manual flow is exactly what let #89/#90 happen (wrong build dir, drifted
sources). This wrapper enforces the guard rails:

  1. PRE-GATE: run `west darling-doctor`. If it fails (wrong build-dir prefix, undeclared
     manifest<->worktree drift, or a deployed-baseline mismatch), REFUSE to build unless --force.
  2. BUILD: ninja the requested targets in the build dir (default: dyld + the closure dylibs).
  3. DEPLOY (opt-in --deploy): copy the freshly built dyld + closure dylibs into BOTH closure
     copies of the prefix, after backing up what is there.
  4. POST-CHECK (only with --deploy): re-run the doctor so a bad deploy is caught immediately.

This does NOT invent a new build system — it wraps the existing manual ninja/deploy so the checks
that would have saved us run automatically. Read-only unless --deploy is passed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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
        p.add_argument("--force", action="store_true",
                       help="build even if the doctor pre-gate fails (records the override loudly)")
        p.add_argument("--skip-doctor", action="store_true", help="skip the doctor pre-gate entirely (discouraged)")
        return p

    def do_run(self, args, unknown):
        topdir = Path(self.topdir)
        build_dir = Path(args.build_dir)
        prefix = Path(args.prefix)

        # ---- 1. doctor pre-gate ----
        if not args.skip_doctor:
            self.inf("== doctor pre-gate ==")
            rc = self._doctor(topdir)
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
        else:
            targets = [_DYLD_TARGET] + self._closure_targets(build_dir)
        self.inf(f"== ninja ({len(targets)} targets) in {build_dir} ==")
        rc = subprocess.run(["ninja", *targets], cwd=build_dir).returncode
        if rc != 0:
            self.err("ninja build FAILED")
            raise SystemExit(rc)
        self.inf("build OK")

        # ---- 3. deploy (opt-in) ----
        if args.deploy:
            self._deploy(build_dir, prefix)
            # ---- 4. post-deploy doctor ----
            self.inf("== post-deploy doctor ==")
            rc = self._doctor(topdir)
            if rc != 0:
                self.err("post-deploy doctor FAILED — the deploy may be inconsistent. Investigate.")
                raise SystemExit(1)
        else:
            self.inf("(not deployed; pass --deploy to copy into the prefix)")

    # -- helpers -----------------------------------------------------------
    def _doctor(self, topdir):
        return subprocess.run(["west", "darling-doctor"], cwd=topdir).returncode

    def _closure_targets(self, build_dir):
        """Resolve each closure basename to its newest built artifact path (relative to build_dir)."""
        targets = []
        for name in _CLOSURE_BASENAMES:
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

    def _deploy(self, build_dir, prefix):
        base = prefix / "libexec/darling"
        root = prefix
        dyld_src = build_dir / _DYLD_TARGET
        if not dyld_src.exists():
            self.err(f"built dyld missing at {dyld_src}")
            raise SystemExit(1)

        # backup both closure copies once (timestamped) before overwriting
        bak = prefix / f".doctor-deploy-bak"
        self.inf(f"== deploy to BOTH closure copies (backup -> {bak}) ==")
        for tree, tag in ((base, "base"), (root, "root")):
            b = bak / tag / "usr/lib/system"
            b.mkdir(parents=True, exist_ok=True)
            for f in ["usr/lib/dyld", "usr/lib/libSystem.B.dylib"]:
                src = tree / f
                if src.exists():
                    dst = bak / tag / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            sysdir = tree / "usr/lib/system"
            if sysdir.is_dir():
                for f in sysdir.glob("*.dylib"):
                    shutil.copy2(f, b / f.name)

        resolved = self._closure_targets(build_dir)
        name_to_path = {Path(t).name: build_dir / t for t in resolved}
        for tree in (base, root):
            shutil.copy2(dyld_src, tree / "usr/lib/dyld")
            for name, src in name_to_path.items():
                if name == "libSystem.B.dylib":
                    shutil.copy2(src, tree / "usr/lib/libSystem.B.dylib")
                else:
                    shutil.copy2(src, tree / "usr/lib/system" / name)
        self.inf(f"deployed dyld + {len(name_to_path)} closure dylibs to both copies")
        self.wrn("NOTE: this changes the SHARED base tree (affects all prefixes). If this is a new "
                 "known-good set, update darling-workspace/deploy-baseline.md5.")

"""Darling environment doctor — catch build/version drift before it wastes a boot cycle.

This exists because two long debugging detours (Beads perf#24c2c-pre / #89 and #90) turned out to be
pure environment drift, not code bugs:

  #89  Built dyld in the WRONG build directory. There are two build dirs for the same source; the
       deployed, bootable closure comes from the one whose CMAKE_INSTALL_PREFIX equals the prefix
       baked into the setuid launcher at compile time. A dyld built with the wrong prefix can never
       boot that prefix, and the failure looks like a mysterious silent hang.

  #90  A project's working tree was checked out to a different commit than the West manifest pins
       (xnu on an experimental perf branch instead of the manifest revision). A fresh closure build
       then silently compiled unexpected source and wedged launchd.

West is the source of truth for versions here (`west.yml` / `west list`), NOT the git-submodule
pointer inside `darling/.gitmodules` (which can itself differ from both the manifest and the working
tree — xnu had three different commits at once). So this command compares each project's WORKING TREE
HEAD against the WEST MANIFEST revision, plus checks build-prefix alignment and the deployed baseline.

Read-only. Exit 0 = green; exit 1 = at least one problem. Intended for `west darling-doctor` before a
build/deploy/boot, and as a pre-build gate.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from west.commands import WestCommand

_EXTRA_PREFIX_DYLIBS = [
    "libsystem_kernel.dylib",
    "libsystem_pthread.dylib",
]


def _run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _md5(path: Path):
    r = _run(["md5sum", str(path)])
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout else None


class DarlingDoctor(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-doctor",
            "Verify workspace/build/deploy alignment before building or booting",
            "Check West-manifest vs working-tree drift, build-prefix alignment, and deployed baseline",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        p = parser_adder.add_parser(self.name, description=self.description)
        p.add_argument("--prefix", default=os.environ.get("DARLING_PREFIX", str(Path.home() / "work/darling-prefix")),
                       help="install prefix baked into the setuid launcher (default: ~/work/darling-prefix)")
        p.add_argument("--build-dir", default=os.environ.get("DARLING_BUILD_DIR", str(Path.home() / "work/darling-build")),
                       help="the prefix-matched CMake build dir used for deployable binaries")
        # baseline md5s: precedence = explicit flag > env var > deploy-baseline.md5 in the manifest repo
        p.add_argument("--expect-dyld-md5", default=os.environ.get("DARLING_EXPECT_DYLD_MD5"))
        p.add_argument("--expect-mldr-md5", default=os.environ.get("DARLING_EXPECT_MLDR_MD5"))
        p.add_argument("--expect-dserver-md5", default=os.environ.get("DARLING_EXPECT_DSERVER_MD5"))
        p.add_argument("--no-baseline-file", action="store_true",
                       help="ignore darling-workspace/deploy-baseline.md5 (only use flags/env)")
        p.add_argument("--allow-drift", action="append", default=[],
                       help="project name/path whose manifest<->worktree drift is intentional (repeatable). "
                            "Also read from darling-workspace/doctor-allow-drift.txt if present.")
        p.add_argument("--extra-prefix", action="append", default=[], metavar="PREFIX",
                       help="additional runtime/test prefix whose critical closure dylibs must match --prefix "
                            "(repeatable; DARLING_TEST_PREFIX is also checked when set)")
        return p

    def do_run(self, args, unknown):
        self.fail = 0
        topdir = Path(self.topdir)
        env_extra = os.environ.get("DARLING_TEST_PREFIX")
        if env_extra and env_extra not in args.extra_prefix:
            args.extra_prefix.append(env_extra)

        self.inf("== Darling env doctor ==")
        self.inf(f"  workspace = {topdir}")
        self.inf(f"  build     = {args.build_dir}")
        self.inf(f"  prefix    = {args.prefix}")
        if args.extra_prefix:
            self.inf(f"  extra     = {', '.join(args.extra_prefix)}")

        self._check_manifest_drift(topdir, args)
        self._check_build_prefix(topdir, args)
        self._check_baseline(args)
        self._check_extra_prefixes(args)

        self.inf("== Result ==")
        if self.fail:
            self.err("PROBLEMS FOUND — fix the ✗ items before building/deploying.")
            raise SystemExit(1)
        self.inf("ALL GREEN — safe to build/deploy/boot.")

    # -- helpers -----------------------------------------------------------
    def _problem(self, msg):
        self.err(f"  ✗ {msg}")
        self.fail = 1

    def _ok(self, msg):
        self.inf(f"  ✓ {msg}")

    def _warn(self, msg):
        self.wrn(f"  ! {msg}")

    def _baseline_file(self):
        """Read deploy-baseline.md5 from the manifest repo: {'dyld':..,'mldr':..,'dserver':..}."""
        f = Path(self.manifest.repo_abspath) / "deploy-baseline.md5"
        out = {}
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        return out

    def _allowlist(self, topdir, args):
        allow = set(args.allow_drift)
        f = Path(self.manifest.repo_abspath) / "doctor-allow-drift.txt"
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    allow.add(line)
        return allow

    # -- CHECK 1: West manifest revision vs working-tree HEAD --------------
    def _check_manifest_drift(self, topdir, args):
        self.inf("\n== 1. West manifest revision vs working-tree HEAD ==")
        allow = self._allowlist(topdir, args)
        if allow:
            self.inf(f"  (declared-drift allowlist: {sorted(allow)})")
        any_drift = False
        for project in self.manifest.projects:
            if project.name == "manifest":  # the self/manifest project
                continue
            if not self.manifest.is_active(project):
                continue
            abspath = Path(project.abspath)
            if not (abspath / ".git").exists():
                continue
            pinned = project.revision  # what the manifest pins
            head = _run(["git", "rev-parse", "HEAD"], cwd=abspath).stdout.strip()
            if not head:
                continue
            # resolve the pinned revision (may be a branch/tag/sha) to a sha within the project
            pinned_sha = _run(["git", "rev-parse", pinned], cwd=abspath).stdout.strip() or pinned
            if head == pinned_sha:
                continue
            any_drift = True
            label = project.name
            path_rel = str(abspath.relative_to(topdir)) if str(abspath).startswith(str(topdir)) else str(abspath)
            rel = "diverged"
            if _run(["git", "merge-base", "--is-ancestor", pinned_sha, head], cwd=abspath).returncode == 0:
                rel = "ahead of manifest"
            detail = f"{path_rel:42s} manifest={pinned_sha[:8]} head={head[:8]} [{rel}]"
            if label in allow or path_rel in allow:
                self._warn(f"declared drift: {detail}")
            else:
                self._problem(f"UNDECLARED drift: {detail}")
        if not any_drift:
            self._ok("every active project HEAD matches its West manifest revision")
        elif self.fail:
            self.inf("        => a fresh build uses the CHECKED-OUT source, not the pinned one.")
            self.inf("           Reset:   west update <project>   (detaches to manifest-rev)")
            self.inf("           Declare: add the project name/path to doctor-allow-drift.txt")

    # -- CHECK 2: build dir prefix vs launcher baked prefix ---------------
    def _check_build_prefix(self, topdir, args):
        self.inf("\n== 2. build-dir install-prefix vs deployed launcher ==")
        launcher = Path(args.prefix) / "bin" / "darling"
        baked = None
        if launcher.exists():
            r = _run(["strings", str(launcher)])
            for line in r.stdout.splitlines():
                if line.endswith("/bin/darlingserver"):
                    baked = line[: -len("/bin/darlingserver")]
                    break
            if baked:
                self.inf(f"  launcher baked INSTALL_PREFIX = {baked}")
        else:
            self._warn(f"no launcher at {launcher} (skip baked-prefix check)")

        cache = Path(args.build_dir) / "CMakeCache.txt"
        if not cache.exists():
            self._problem(f"no CMakeCache.txt in {args.build_dir} (is --build-dir correct?)")
            return
        bp = None
        for line in cache.read_text().splitlines():
            if line.startswith("CMAKE_INSTALL_PREFIX:"):
                bp = line.split("=", 1)[1]
                break
        self.inf(f"  build dir CMAKE_INSTALL_PREFIX = {bp}")
        if baked and bp != baked:
            self._problem(f"build-dir prefix ({bp}) != launcher baked prefix ({baked}); "
                          f"a dyld/mldr built here will NOT boot {args.prefix}")
        elif baked:
            self._ok("build-dir prefix matches launcher baked prefix")

        wrong = topdir / "darling" / "build"
        wcache = wrong / "CMakeCache.txt"
        if wcache.exists() and str(wrong) != str(Path(args.build_dir)):
            for line in wcache.read_text().splitlines():
                if line.startswith("CMAKE_INSTALL_PREFIX:"):
                    wp = line.split("=", 1)[1]
                    if baked and wp != baked:
                        self._warn(f"a second build dir at {wrong} has prefix={wp} (NOT {baked}); "
                                   f"do not build deployable binaries there")
                    break

    # -- CHECK 3: deployed binaries vs known-good baseline ----------------
    def _check_baseline(self, args):
        self.inf("\n== 3. deployed binaries vs known-good baseline ==")
        prefix = Path(args.prefix)
        bf = {} if args.no_baseline_file else self._baseline_file()
        if bf and not args.no_baseline_file:
            self.inf(f"  (baseline from {Path(self.manifest.repo_abspath) / 'deploy-baseline.md5'})")
        dyld_md5 = args.expect_dyld_md5 or bf.get("dyld")
        mldr_md5 = args.expect_mldr_md5 or bf.get("mldr")
        dserver_md5 = args.expect_dserver_md5 or bf.get("dserver")
        checks = [
            ("deployed dyld (base tree)", prefix / "libexec/darling/usr/lib/dyld", dyld_md5),
            ("deployed dyld (prefix root)", prefix / "usr/lib/dyld", dyld_md5),
            ("deployed mldr", prefix / "libexec/darling/usr/libexec/darling/mldr", mldr_md5),
            ("deployed darlingserver", prefix / "bin/darlingserver", dserver_md5),
        ]
        for label, path, expect in checks:
            if not expect:
                self._warn(f"{label}: no expected md5 (skip)")
                continue
            if not path.exists():
                self._problem(f"{label}: missing at {path}")
                continue
            got = _md5(path)
            if got == expect:
                self._ok(f"{label} md5 matches baseline")
            else:
                self._problem(f"{label} md5 {got} != baseline {expect} ({path})")

        d1 = prefix / "libexec/darling/usr/lib/dyld"
        d2 = prefix / "usr/lib/dyld"
        if d1.exists() and d2.exists():
            if _md5(d1) == _md5(d2):
                self._ok("both deployed dyld copies match each other")
            else:
                self._problem(f"the two deployed dyld copies DIFFER (half-deploy); deploy to BOTH {d1} AND {d2}")

    # -- CHECK 4: additional runtime/test prefixes vs primary prefix ------
    def _check_extra_prefixes(self, args):
        if not args.extra_prefix:
            return
        self.inf("\n== 4. extra runtime prefixes vs primary prefix ==")
        primary = Path(args.prefix)
        for raw in args.extra_prefix:
            extra = Path(raw)
            self.inf(f"  extra prefix = {extra}")
            local_fail = False
            for name in _EXTRA_PREFIX_DYLIBS:
                primary_path = primary / "usr/lib/system" / name
                extra_path = extra / "usr/lib/system" / name
                if not primary_path.exists():
                    self._problem(f"primary prefix missing {name} at {primary_path}")
                    local_fail = True
                    continue
                if not extra_path.exists():
                    self._problem(f"extra prefix missing {name} at {extra_path}")
                    local_fail = True
                    continue
                primary_md5 = _md5(primary_path)
                extra_md5 = _md5(extra_path)
                if primary_md5 != extra_md5:
                    self._problem(f"{extra}: {name} md5 {extra_md5} != primary {primary_md5}")
                    local_fail = True
            if not local_fail:
                self._ok(f"{extra} critical closure dylibs match primary prefix")

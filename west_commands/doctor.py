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

Read-only. Exit 0 = green; exit 1 = diagnosed problem or operational failure; invalid
arguments exit 2. Default output is bounded, while --full and --json expose complete detail.
Intended for `west darling-doctor` before a build/deploy/boot, and as a pre-build gate.
Typed rootless prefixes without a published runtime are checked as PREPARED;
launchd creates their boot directories. Published runtimes must satisfy READY
directory postconditions. Build identity and binary checks apply in both phases.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from west.commands import WestCommand

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prefix_repair import PrefixBootPhase, prefix_boot_phase, prefix_boot_prerequisite_problems

_EXTRA_PREFIX_DYLIBS = [
    "libsystem_kernel.dylib",
    "libsystem_pthread.dylib",
]

SCHEMA_VERSION = 1
DEFAULT_PROBLEM_LIMIT = 8

DEFAULT_ROW_CHARACTER_LIMIT = 500

def _run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _md5(path: Path):
    r = _run(["md5sum", str(path)])
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout else None


def _west_argv() -> list[str]:
    invoked = Path(sys.argv[0])
    if invoked.name == "__main__.py":
        return [sys.executable, "-m", "west"]
    if invoked.suffix == ".py" and invoked.is_file():
        return [sys.executable, str(invoked.resolve())]
    if invoked.is_file():
        return [str(invoked.resolve())]
    executable = shutil.which("west")
    if executable is None:
        raise RuntimeError("cannot locate the current west executable")
    return [executable]


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
        p.add_argument(
            "--prefix",
            default=os.environ.get(
                "DARLING_PREFIX", str(Path.home() / "work/darling-prefix")
            ),
            help="install prefix baked into the setuid launcher (default: ~/work/darling-prefix)",
        )
        p.add_argument(
            "--build-dir",
            default=os.environ.get(
                "DARLING_BUILD_DIR", str(Path.home() / "work/darling-build")
            ),
            help="the prefix-matched CMake build dir used for deployable binaries",
        )
        # baseline md5s: precedence = explicit flag > env var > deploy-baseline.md5 in the manifest repo
        p.add_argument(
            "--expect-dyld-md5", default=os.environ.get("DARLING_EXPECT_DYLD_MD5")
        )
        p.add_argument(
            "--expect-mldr-md5", default=os.environ.get("DARLING_EXPECT_MLDR_MD5")
        )
        p.add_argument(
            "--expect-dserver-md5",
            default=os.environ.get("DARLING_EXPECT_DSERVER_MD5"),
        )
        p.add_argument(
            "--no-baseline-file",
            action="store_true",
            help="ignore darling-workspace/deploy-baseline.md5 (only use flags/env)",
        )
        p.add_argument(
            "--allow-drift",
            action="append",
            default=[],
            help="project name/path whose manifest<->worktree drift is intentional (repeatable). "
            "Also read from darling-workspace/doctor-allow-drift.txt if present.",
        )
        p.add_argument(
            "--extra-prefix",
            action="append",
            default=[],
            metavar="PREFIX",
            help="additional runtime/test prefix whose critical closure dylibs must match --prefix "
            "(repeatable; DARLING_TEST_PREFIX is also checked when set)",
        )
        p.add_argument(
            "--scope",
            choices=("all", "workspace", "runtime"),
            default="all",
            help="check workspace drift, runtime postconditions, or both (default: all)",
        )
        output = p.add_mutually_exclusive_group()
        output.add_argument(
            "--full",
            action="store_true",
            help="show every diagnostic detail (legacy verbose view)",
        )
        output.add_argument(
            "--json",
            action="store_true",
            help="emit the complete schema-v1 result as JSON only",
        )
        return p

    def do_run(self, args, unknown):
        if unknown:
            self.err(f"unknown arguments: {' '.join(unknown)}")
            raise SystemExit(2)

        self._reset_output()
        topdir = Path(self.topdir)
        extra_prefixes = list(args.extra_prefix)
        env_extra = os.environ.get("DARLING_TEST_PREFIX")
        if env_extra and env_extra not in extra_prefixes:
            extra_prefixes.append(env_extra)
        args.extra_prefix = extra_prefixes

        self._detail("== Darling env doctor ==")
        self._detail(f"  workspace = {topdir}")
        self._detail(f"  build     = {args.build_dir}")
        self._detail(f"  prefix    = {args.prefix}")
        if args.extra_prefix:
            self._detail(f"  extra     = {', '.join(args.extra_prefix)}")

        attempts = (
            (
                "workspace",
                "1. West manifest revision vs working-tree HEAD",
                lambda: self._check_manifest_drift(topdir, args),
            ),
            (
                "runtime",
                "2. build-dir install-prefix vs deployed launcher",
                lambda: self._check_build_prefix(topdir, args),
            ),
            (
                "runtime",
                "2b. prefix boot prerequisites",
                lambda: self._check_prefix_boot_prereqs(args),
            ),
            (
                "runtime",
                "3. deployed binaries vs known-good baseline",
                lambda: self._check_baseline(args),
            ),
            (
                "runtime",
                "4. extra runtime prefixes vs primary prefix",
                lambda: self._check_extra_prefixes(args),
            ),
        )
        for scope, section, attempt in attempts:
            if args.scope not in ("all", scope):
                continue
            try:
                attempt()
            except Exception as error:
                self._record_operational_error(section, error)

        if self._operational_errors:
            state = "operational_error"
        else:
            state = "problems" if self.fail else "healthy"
        payload = self._payload(args, state=state)
        self._emit(payload, args)
        if state != "healthy":
            raise SystemExit(1)

    # -- structured output -------------------------------------------------
    def _reset_output(self) -> None:
        self.fail = 0
        self._current_section = "overview"
        self._checks: list[dict[str, str]] = []
        self._legacy_lines: list[tuple[str, str]] = []
        self._operational_errors: list[dict[str, str]] = []
        self._invocation_west_argv = _west_argv()

    def _section(self, title: str) -> None:
        self._current_section = title
        self._detail(f"\n== {title} ==")

    def _detail(self, message: str, level: str = "info") -> None:
        if not hasattr(self, "_legacy_lines"):
            if level == "error":
                self.err(message)
            elif level == "warning":
                self.wrn(message)
            else:
                self.inf(message)
            return
        self._legacy_lines.append((level, message))

    def _record(self, state: str, message: str) -> None:
        if not hasattr(self, "_checks"):
            self._checks = []
        self._checks.append(
            {
                "section": getattr(self, "_current_section", "overview"),
                "state": state,
                "message": message,
            }
        )

    def _problem(self, msg):
        self._record("problem", msg)
        self._detail(f"  ✗ {msg}", "error")
        self.fail = 1

    def _ok(self, msg):
        self._record("ok", msg)
        self._detail(f"  ✓ {msg}")

    def _warn(self, msg):
        self._record("warning", msg)
        self._detail(f"  ! {msg}", "warning")

    def _record_operational_error(self, section: str, error: Exception) -> None:
        self._current_section = section
        entry = {
            "section": section,
            "type": type(error).__name__,
            "message": str(error),
        }
        self._operational_errors.append(entry)
        message = f"{entry['type']}: {entry['message']}"
        self._record("operational_error", message)
        self._detail(f"  ✗ operational error: {message}", "error")

    def _full_command(self, args: argparse.Namespace) -> str:
        command = [
            *self._invocation_west_argv,
            "darling-doctor",
            f"--prefix={args.prefix}",
            f"--build-dir={args.build_dir}",
        ]
        if args.scope != "all":
            command.append(f"--scope={args.scope}")
        for option, attribute in (
            ("--expect-dyld-md5", "expect_dyld_md5"),
            ("--expect-mldr-md5", "expect_mldr_md5"),
            ("--expect-dserver-md5", "expect_dserver_md5"),
        ):
            value = getattr(args, attribute)
            if value:
                command.append(f"{option}={value}")
        if args.no_baseline_file:
            command.append("--no-baseline-file")
        for value in args.allow_drift:
            command.append(f"--allow-drift={value}")
        for value in args.extra_prefix:
            command.append(f"--extra-prefix={value}")
        command.append("--full")
        return shlex.join(command)

    def _payload(
        self,
        args: argparse.Namespace,
        *,
        state: str,
    ) -> dict[str, Any]:
        counts = {
            name: sum(item["state"] == name for item in self._checks)
            for name in ("ok", "warning", "problem", "operational_error")
        }
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation": "doctor",
            "state": state,
            "returncode": 0 if state == "healthy" else 1,
            "inputs": {
                "workspace": str(Path(self.topdir)),
                "scope": args.scope,
                "build_dir": str(args.build_dir),
                "prefix": str(args.prefix),
                "extra_prefixes": [str(value) for value in args.extra_prefix],
                "allow_drift": [str(value) for value in args.allow_drift],
                "baseline_file_enabled": not args.no_baseline_file,
                "west_argv": list(self._invocation_west_argv),
            },
            "summary": {
                **counts,
                "checks": len(self._checks),
            },
            "results": list(self._checks),
            "detail_command": self._full_command(args),
        }
        if self._operational_errors:
            payload["errors"] = list(self._operational_errors)
            first = self._operational_errors[0]
            payload["error"] = {
                "type": first["type"],
                "message": first["message"],
            }
        return payload

    def _emit(self, payload: dict[str, Any], args: argparse.Namespace) -> None:
        if args.json:
            self.inf(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return
        if args.full:
            self._render_full(payload)
            return
        self._render_default(payload)

    def _render_default(self, payload: dict[str, Any]) -> None:
        summary = payload["summary"]
        self.inf(
            "Darling doctor: "
            f"{payload['state']} "
            f"({summary['problem']} problems, {summary['warning']} warnings, "
            f"{summary['ok']} passed)"
        )
        self.inf(f"details: {payload['detail_command']}")
        actionable = [
            item
            for item in payload["results"]
            if item["state"] in {"problem", "warning"}
        ]
        for item in actionable[:DEFAULT_PROBLEM_LIMIT]:
            label = "FAIL" if item["state"] == "problem" else "WARN"
            message = f"{label:4} {item['section']}: {item['message']}"
            if len(message) > DEFAULT_ROW_CHARACTER_LIMIT:
                message = message[: DEFAULT_ROW_CHARACTER_LIMIT - 1] + "…"
            self.inf(message)
        omitted = len(actionable) - min(len(actionable), DEFAULT_PROBLEM_LIMIT)
        if omitted:
            self.inf(f"... {omitted} additional problem/warning rows omitted")
        for error in payload.get("errors", []):
            message = (
                f"FAIL {error['section']}: {error['type']}: {error['message']}"
            )
            if len(message) > DEFAULT_ROW_CHARACTER_LIMIT:
                message = message[: DEFAULT_ROW_CHARACTER_LIMIT - 1] + "…"
            self.inf(message)

    def _render_full(self, payload: dict[str, Any]) -> None:
        for level, message in self._legacy_lines:
            if level == "error":
                self.err(message)
            elif level == "warning":
                self.wrn(message)
            else:
                self.inf(message)
        self.inf("== Result ==")
        if payload["state"] == "healthy":
            self.inf("ALL GREEN — safe to build/deploy/boot.")
        elif payload["state"] == "problems":
            self.err("PROBLEMS FOUND — fix the ✗ items before building/deploying.")
        else:
            errors = payload.get("errors", [])
            self.err(
                "OPERATIONAL ERROR — doctor completed all sections with "
                f"{len(errors)} operational failure(s)."
            )

    # -- helpers -----------------------------------------------------------
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
        self._section("1. West manifest revision vs working-tree HEAD")
        allow = self._allowlist(topdir, args)
        if allow:
            self._detail(f"  (declared-drift allowlist: {sorted(allow)})")
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
            pinned_sha = (
                _run(["git", "rev-parse", pinned], cwd=abspath).stdout.strip()
                or pinned
            )
            if head == pinned_sha:
                continue
            any_drift = True
            label = project.name
            path_rel = (
                str(abspath.relative_to(topdir))
                if str(abspath).startswith(str(topdir))
                else str(abspath)
            )
            rel = "diverged"
            if (
                _run(
                    ["git", "merge-base", "--is-ancestor", pinned_sha, head],
                    cwd=abspath,
                ).returncode
                == 0
            ):
                rel = "ahead of manifest"
            detail = (
                f"{path_rel:42s} manifest={pinned_sha[:8]} "
                f"head={head[:8]} [{rel}]"
            )
            if label in allow or path_rel in allow:
                self._warn(f"declared drift: {detail}")
            else:
                self._problem(f"UNDECLARED drift: {detail}")
        if not any_drift:
            self._ok("every active project HEAD matches its West manifest revision")
        elif self.fail:
            self._detail(
                "        => a fresh build uses the CHECKED-OUT source, not the pinned one."
            )
            self._detail(
                "           Reset:   west update <project>   (detaches to manifest-rev)"
            )
            self._detail(
                "           Declare: add the project name/path to doctor-allow-drift.txt"
            )

    # -- CHECK 2: build dir prefix vs launcher baked prefix ---------------
    def _check_build_prefix(self, topdir, args):
        self._section("2. build-dir install-prefix vs deployed launcher")
        launcher = Path(args.prefix) / "bin" / "darling"
        baked = None
        if launcher.exists():
            r = _run(["strings", str(launcher)])
            for line in r.stdout.splitlines():
                if line.endswith("/bin/darlingserver"):
                    baked = line[: -len("/bin/darlingserver")]
                    break
            if baked:
                self._detail(f"  launcher baked INSTALL_PREFIX = {baked}")
        else:
            self._warn(f"no launcher at {launcher} (skip baked-prefix check)")

        cache = Path(args.build_dir) / "CMakeCache.txt"
        if not cache.exists():
            self._problem(
                f"no CMakeCache.txt in {args.build_dir} (is --build-dir correct?)"
            )
            return
        bp = None
        for line in cache.read_text().splitlines():
            if line.startswith("CMAKE_INSTALL_PREFIX:"):
                bp = line.split("=", 1)[1]
                break
        self._detail(f"  build dir CMAKE_INSTALL_PREFIX = {bp}")
        if baked and bp != baked:
            self._problem(
                f"build-dir prefix ({bp}) != launcher baked prefix ({baked}); "
                f"a dyld/mldr built here will NOT boot {args.prefix}"
            )
        elif baked:
            self._ok("build-dir prefix matches launcher baked prefix")

        wrong = topdir / "darling" / "build"
        wcache = wrong / "CMakeCache.txt"
        if wcache.exists() and str(wrong) != str(Path(args.build_dir)):
            for line in wcache.read_text().splitlines():
                if line.startswith("CMAKE_INSTALL_PREFIX:"):
                    wp = line.split("=", 1)[1]
                    if baked and wp != baked:
                        self._warn(
                            f"a second build dir at {wrong} has prefix={wp} (NOT {baked}); "
                            "do not build deployable binaries there"
                        )
                    break

    # -- CHECK 2b: prefix boot prerequisites ------------------------------
    def _check_prefix_boot_prereqs(self, args):
        self._section("2b. prefix boot prerequisites")

        def check_one(prefix: Path, label: str):
            phase = prefix_boot_phase(prefix)
            problems = prefix_boot_prerequisite_problems(prefix, phase=phase)
            if problems:
                for problem in problems:
                    self._problem(f"{label}: {problem}")
                return
            if phase is PrefixBootPhase.PREPARED:
                self._ok(f"{label}: typed rootless prefix prepared; launchd owns first-boot directories")
                return
            self._ok(f"{label}: boot directory postconditions satisfied")

        check_one(Path(args.prefix), "prefix")
        for extra in args.extra_prefix:
            check_one(Path(extra), f"extra {extra}")

    # -- CHECK 3: deployed binaries vs known-good baseline ----------------
    def _check_baseline(self, args):
        self._section("3. deployed binaries vs known-good baseline")
        prefix = Path(args.prefix)
        bf = {} if args.no_baseline_file else self._baseline_file()
        if bf and not args.no_baseline_file:
            self._detail(
                f"  (baseline from {Path(self.manifest.repo_abspath) / 'deploy-baseline.md5'})"
            )
        dyld_md5 = args.expect_dyld_md5 or bf.get("dyld")
        mldr_md5 = args.expect_mldr_md5 or bf.get("mldr")
        dserver_md5 = args.expect_dserver_md5 or bf.get("dserver")
        checks = [
            (
                "deployed dyld (base tree)",
                prefix / "libexec/darling/usr/lib/dyld",
                dyld_md5,
            ),
            ("deployed dyld (prefix root)", prefix / "usr/lib/dyld", dyld_md5),
            (
                "deployed mldr",
                prefix / "libexec/darling/usr/libexec/darling/mldr",
                mldr_md5,
            ),
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
                self._problem(
                    f"{label} md5 {got} != baseline {expect} ({path})"
                )

        d1 = prefix / "libexec/darling/usr/lib/dyld"
        d2 = prefix / "usr/lib/dyld"
        if d1.exists() and d2.exists():
            if _md5(d1) == _md5(d2):
                self._ok("both deployed dyld copies match each other")
            else:
                self._problem(
                    "the two deployed dyld copies DIFFER (half-deploy); "
                    f"deploy to BOTH {d1} AND {d2}"
                )

    # -- CHECK 4: additional runtime/test prefixes vs primary prefix ------
    def _check_extra_prefixes(self, args):
        if not args.extra_prefix:
            return
        self._section("4. extra runtime prefixes vs primary prefix")
        primary = Path(args.prefix)
        for raw in args.extra_prefix:
            extra = Path(raw)
            self._detail(f"  extra prefix = {extra}")
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
                    self._problem(
                        f"{extra}: {name} md5 {extra_md5} != primary {primary_md5}"
                    )
                    local_fail = True
            if not local_fail:
                self._ok(f"{extra} critical closure dylibs match primary prefix")

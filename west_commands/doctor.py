"""Read-only West source and installed-runtime diagnostics, with bounded/full/JSON output."""

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
from deploy_receipt import receipt_path, verify_receipt
from prefix_repair import prefix_boot_phase, prefix_boot_prerequisite_problems
from runtime_boot_smoke import run_boot_smoke
from runtime_install import cmake_cache, resolve_runtime


SCHEMA_VERSION = 1
DEFAULT_PROBLEM_LIMIT = 8

DEFAULT_ROW_CHARACTER_LIMIT = 500

def _run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)



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
            "Verify West source selection and an ordinary installed Darling runtime",
            "Check active West revisions, selected CMake configuration, installed integrity and optional guest boot",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        p = parser_adder.add_parser(self.name, description=self.description)
        p.add_argument("--runtime-root", type=Path, help="ordinary product installation, separate from guest state")
        p.add_argument(
            "--prefix", type=Path, default=os.environ.get("DPREFIX"),
            help="writable guest state (DPREFIX); required only for --boot-smoke",
        )
        p.add_argument(
            "--build-dir", type=Path, default=os.environ.get("DARLING_BUILD_DIR"),
            help="optional selected CMake build; never the authority for previously installed bytes",
        )
        p.add_argument(
            "--source-root", type=Path,
            help="selected prepared Darling source closure (otherwise selected build CMAKE_HOME_DIRECTORY)",
        )
        p.add_argument(
            "--boot-smoke",
            action="store_true",
            help="runtime scope: start the prefix and require a guest shell. This is the check the "
            "artifact comparisons cannot make -- a chain can match its receipt and still never "
            "reach a shell (MEASURED: a targeted deploy left shellspawn unreachable while every "
            "digest matched). Costs about 30-60s and needs the prefix stopped.",
        )
        p.add_argument(
            "--allow-drift",
            action="append",
            default=[],
            help="project name/path whose manifest<->worktree drift is intentional (repeatable). "
            "Also read from darling-workspace/doctor-allow-drift.txt if present.",
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
        if args.boot_smoke and args.prefix is None:
            self.err("--boot-smoke requires --prefix or DPREFIX")
            raise SystemExit(2)

        self._detail("== Darling env doctor ==")
        self._detail(f"  workspace = {topdir}")
        self._detail(f"  build     = {args.build_dir}")
        self._detail(f"  prefix    = {args.prefix}")
        self._detail(f"  runtime   = {args.runtime_root or os.environ.get('DARLING_RUNTIME_ROOT', 'PATH selection')}")
        self._detail(f"  source    = {args.source_root or ('selected build source' if args.build_dir else 'active West source')}")

        attempts = (
            (
                "workspace",
                "1. West manifest revision vs working-tree HEAD",
                lambda: self._check_manifest_drift(topdir, args),
            ),
            (
                "runtime",
                "2. selected build configuration",
                lambda: self._check_build_prefix(topdir, args),
            ),
            (
                "runtime",
                "2b. prefix boot prerequisites",
                lambda: self._check_prefix_boot_prereqs(args),
            ),
            (
                "runtime",
                "3. installed-runtime integrity",
                lambda: self._check_installation(args),
            ),
            (
                "runtime",
                "5. runtime boot smoke",
                lambda: self._check_boot_smoke(args),
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
        command = [*self._invocation_west_argv, "darling-doctor"]
        for option, attribute in (
            ("--runtime-root", "runtime_root"), ("--prefix", "prefix"),
            ("--build-dir", "build_dir"), ("--source-root", "source_root"),
        ):
            value = getattr(args, attribute)
            if value is not None:
                command.append(f"{option}={value}")
        if args.scope != "all":
            command.append(f"--scope={args.scope}")
        if args.boot_smoke:
            command.append("--boot-smoke")
        for value in args.allow_drift:
            command.append(f"--allow-drift={value}")
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
                "build_dir": str(args.build_dir) if args.build_dir is not None else None,
                "prefix": str(args.prefix) if args.prefix is not None else None,
                "runtime_root": str(args.runtime_root) if args.runtime_root is not None else os.environ.get("DARLING_RUNTIME_ROOT"),
                "source_root": str(args.source_root) if args.source_root is not None else getattr(self, "_resolved_source_root", None),
                "allow_drift": [str(value) for value in args.allow_drift],
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
        product = next((Path(item.abspath) for item in self.manifest.projects if item.name == "darling"), Path(topdir) / "darling")
        selected = self._selected_source_root(args)
        tree = _run(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=selected)
        if tree.returncode:
            raise ValueError(f"cannot inspect selected source closure at {selected}: {tree.stderr.strip()}")
        links = []
        for record in tree.stdout.split("\0"):
            if record.startswith("160000 "):
                links.append(Path(record.split("\t", 1)[1]))
        for project in self.manifest.projects:
            if project.name == "manifest":  # the self/manifest project
                continue
            if not self.manifest.is_active(project):
                continue
            canonical = Path(project.abspath)
            try:
                relative = canonical.relative_to(product)
            except ValueError:
                abspath = canonical
            else:
                # Independently managed West projects (e.g. docs/) are not
                # transplanted into a prepared product/gitlink closure.
                in_closure = relative == Path() or any(relative == link or link in relative.parents for link in links)
                abspath = selected / relative if in_closure else canonical
            if not (abspath / ".git").exists():
                self._problem(f"{project.name}: selected source repository is absent at {abspath}")
                continue
            pinned = project.revision
            head = _run(["git", "rev-parse", "--verify", "HEAD"], cwd=abspath).stdout.strip()
            if not head:
                self._problem(f"{project.name}: cannot read selected source HEAD at {abspath}")
                continue
            tracked = _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=abspath)
            if tracked.returncode:
                self._problem(f"{project.name}: cannot inspect selected source changes at {abspath}")
            elif tracked.stdout.strip():
                message = f"{project.name}: selected source has tracked modifications at {abspath}"
                if project.name in allow or project.path in allow:
                    self._warn(message)
                else:
                    self._problem(message)
            branch = _run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=abspath
            ).stdout.strip()
            if branch.startswith("diag/"):
                # MEASURED 2026-10-10: a diagnostic branch carries probe output (pc-, tc-, dring-,
                # mldr-seed) that interleaves with the guest's own verdict lines on fd 2, so a fixture
                # can look broken and a run log can look like a hang. It is a warning, not a problem:
                # measuring on such a branch is possible, it is just noisy and easy to misread.
                self._warn(
                    f"{project.name} is on diagnostic branch {branch}: its probe output interleaves "
                    "with the guest's verdict lines, so a measurement here is noisy; prefer a clean "
                    "fix/* branch when you need a verdict you can trust"
                )
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
            self._detail("        => build from the intended prepared source closure; preserve foreign checkouts.")
            self._detail("           Declare intentional revision drift with --allow-drift PROJECT.")

    def _selected_source_root(self, args) -> Path:
        if args.source_root is not None:
            selected = Path(args.source_root).expanduser().resolve()
        elif args.build_dir is not None:
            source = cmake_cache(args.build_dir).get("CMAKE_HOME_DIRECTORY")
            if not source:
                raise ValueError(f"selected build has no CMAKE_HOME_DIRECTORY: {args.build_dir}")
            selected = Path(source).resolve()
        else:
            selected = Path(self.topdir) / "darling"
        self._resolved_source_root = str(selected)
        return selected

    def _check_build_prefix(self, topdir, args):
        self._section("2. selected build configuration")
        if args.build_dir is None:
            self._warn("no build selected; installed images do not require their build tree to remain available")
            return
        configuration = cmake_cache(args.build_dir)
        destination = configuration.get("CMAKE_INSTALL_PREFIX")
        source = configuration.get("CMAKE_HOME_DIRECTORY")
        if not destination or not Path(destination).is_absolute():
            self._problem(f"selected build has no absolute CMAKE_INSTALL_PREFIX: {args.build_dir}")
        else:
            self._ok(f"selected build installs into {destination}; this is not writable guest state")
        if not source:
            self._problem(f"selected build has no CMAKE_HOME_DIRECTORY: {args.build_dir}")
        elif Path(source).resolve() != self._selected_source_root(args):
            self._problem(f"selected build source {source} differs from --source-root {args.source_root}")
        else:
            self._ok(f"selected build uses source {source}")

    def _check_prefix_boot_prereqs(self, args):
        self._section("2b. guest prefix prerequisites")
        if args.prefix is None:
            self._warn("no guest prefix selected; installed-runtime integrity remains independently checkable")
            return
        prefix = Path(args.prefix)
        if not prefix.is_absolute() or ".." in prefix.parts:
            self._problem(f"guest prefix must be absolute without '..': {prefix}")
            return
        if prefix.is_symlink():
            self._problem(f"guest prefix is a symlink: {prefix}")
            return
        if prefix.exists() and not prefix.is_dir():
            self._problem(f"guest prefix is not a directory: {prefix}")
            return
        if not prefix.exists():
            self._ok("guest prefix is absent; the normal product launcher owns first-use initialization")
            return
        state = prefix / ".darling-prefix-state-v2"
        if state.exists():
            problems = prefix_boot_prerequisite_problems(prefix, phase=prefix_boot_phase(prefix))
            if problems:
                for problem in problems:
                    self._problem(f"guest prefix: {problem}")
                return
            self._ok("existing guest prefix identity and boot directory prerequisites are valid")
        else:
            self._warn("guest prefix has no retained identity; normal launcher initialization has not been verified")

    def _check_installation(self, args):
        self._section("3. installed-runtime integrity")
        runtime = resolve_runtime(args.runtime_root)
        self._ok(f"ordinary installed launcher: {runtime.launcher}")
        if args.prefix is not None:
            runtime.environment(args.prefix)
        payload = runtime.identity
        if payload is None:
            self._warn(f"no workspace install receipt at {receipt_path(runtime.root)}; package provenance is unknown/unverified, ordinary launch remains supported")
            return
        problems, notes = verify_receipt(payload)
        for problem in problems:
            self._problem(f"{problem.label}: {problem.message}")
        for note in notes:
            self._ok(note)
        self._detail(f"  recorded build: {payload.get('build', {}).get('directory')}")
        self._detail(f"  recorded source: {payload.get('source', {}).get('directory')}")
        self._detail("  mutable source/build changes do not invalidate this immutable installed inventory")

    def _check_boot_smoke(self, args):
        self._section("5. runtime boot smoke")
        if not args.boot_smoke:
            self._warn("guest boot not exercised; --boot-smoke explicitly launches and shuts down the selected prefix")
            return
        ok, detail = run_boot_smoke(
            Path(self.topdir), args.prefix, runtime_root=args.runtime_root,
        )
        if ok:
            self._ok(detail)
        else:
            self._problem(f"ordinary installed runtime did not complete guest boot and clean product shutdown: {detail}")

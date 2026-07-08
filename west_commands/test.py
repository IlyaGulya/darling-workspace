"""Darling workspace test orchestrator (PoC).

`west test` is a thin layer over CTest, in the same spirit as gVisor's Bazel
test targets and Wine's winetest: the runner sits ON TOP of the build system,
it does not reinvent discovery/parallelism/JUnit/WILL_FAIL. CTest owns those.

This command adds the three things CTest does not give for free in this repo:

  --changed   map changed submodules (from the west manifest + git diff) to the
              `submod:<name>` CTest labels, so a quick local cycle runs only the
              tests a PR could affect.
  --bead ID   run the regression(s) attached to an issue (label `bead:<id>`),
              turning the beads graph into a live regression set.
  --executor  the darling-debug-runner binary used by the guarded/forensic
              diagnosis tiers, so a hang becomes a captured, timed-out failure
              instead of a stall (the tier is set per-test in add_compat_test).

It is a PoC: the test tree lives under testkit/ and is configured/built here.
The real version would discover per-submodule test trees from the manifest.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import yaml
from west.commands import WestCommand


class DarlingTest(WestCommand):
    def __init__(self):
        super().__init__(
            "test",
            "Run Darling regression/compat tests (changed-only, by bead, or full)",
            "Discover and run compat tests via ctest with changed/bead targeting",
            accepts_unknown_args=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument(
            "--changed",
            action="store_true",
            help="run only tests labelled for submodules changed vs upstream",
        )
        parser.add_argument(
            "--bead",
            metavar="ID",
            help="run tests attached to a bead (label bead:<ID>)",
        )
        parser.add_argument(
            "--profile",
            metavar="NAME",
            help="run tests declared by a patch profile's patches.yml metadata",
        )
        parser.add_argument(
            "--patch",
            metavar="PATH",
            help="run tests declared for one patch path in patches.yml metadata",
        )
        parser.add_argument(
            "--red-only",
            action="store_true",
            help="with --profile/--patch, select only tests marked red: true",
        )
        parser.add_argument(
            "--red-audit",
            action="store_true",
            help="with --profile, list patches missing tests or test-exception",
        )
        parser.add_argument(
            "--env",
            choices=("host", "darling", "macos"),
            help="restrict to one environment",
        )
        parser.add_argument(
            "--executor",
            metavar="PATH",
            help="darling-debug-runner binary for guarded/forensic tiers",
        )
        parser.add_argument(
            "--diag",
            choices=("bare", "guarded", "forensic"),
            help="restrict to one diagnosis tier; matches the RESOLVED tier, so "
            "guarded/forensic that fell back to bare (no executor) count as bare",
        )
        parser.add_argument(
            "--label",
            metavar="REGEX",
            help="restrict to tests whose CTest label matches (e.g. 'macos:15' "
            "for a CI version row); passed through as ctest -L",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="list selected tests and exit (no run)",
        )
        parser.add_argument(
            "--gc",
            action="store_true",
            help="prune old debug bundles (keep-last + size cap) and exit",
        )
        parser.add_argument(
            "--keep-last",
            type=int,
            default=20,
            metavar="N",
            help="bundles to keep when pruning (default 20)",
        )
        parser.add_argument(
            "--max-bundle-mb",
            type=int,
            default=64,
            metavar="MB",
            help="drop bundles larger than this when pruning (default 64)",
        )
        parser.add_argument(
            "--bundle-root",
            metavar="DIR",
            default="~/work/darling-debug",
            help="debug bundle directory (default ~/work/darling-debug)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="with --gc, show what would be pruned without deleting",
        )
        return parser

    # --- helpers ------------------------------------------------------------

    def _testkit_dir(self) -> Path:
        return Path(self.manifest.repo_abspath) / "testkit"

    def _profile_path(self, profile: str) -> Path:
        return Path(self.manifest.repo_abspath) / "patches" / profile / "patches.yml"

    def _load_profile(self, profile: str) -> dict:
        path = self._profile_path(profile)
        if not path.is_file():
            self.die(f"patch profile not found: {path}")
        return yaml.safe_load(path.read_text()) or {}

    def _metadata_tests(
        self,
        profile: str,
        patch_path: str | None,
        bead: str | None,
        red_only: bool,
    ):
        data = self._load_profile(profile)
        selected = []
        missing = []
        for patch in data.get("patches", []):
            if patch_path and patch["path"] != patch_path:
                continue
            if bead and patch.get("bead") != bead:
                continue
            tests = patch.get("tests") or []
            if red_only:
                tests = [test for test in tests if test.get("red")]
            if tests:
                for test in tests:
                    selected.append((patch, test))
            elif not patch.get("test-exception"):
                missing.append(patch)
        if patch_path and not selected and not missing:
            self.die(f"{profile}: patch not found or has no selected tests: {patch_path}")
        return selected, missing

    def _run_metadata_tests(self, tests, list_only: bool, unknown: list[str]) -> int:
        if unknown:
            self.die("metadata command tests do not accept raw ctest passthrough arguments")
        rc = 0
        for patch, test in tests:
            name = test.get("name", "-")
            env = test.get("env", "-")
            diag = test.get("diag", "-")
            kind = test.get("kind", "-")
            red = "red" if test.get("red") else "non-red"
            command = test.get("command")
            label = test.get("ctest-label")
            target = command or f"ctest-label:{label}"
            self.inf(
                f"{patch['path']}: {name} [{red}, env:{env}, diag:{diag}, kind:{kind}]"
            )
            self.inf(f"  {target}")
            if list_only:
                continue
            if label:
                self.die(
                    f"{patch['path']}: ctest-label metadata is list-only until "
                    "profile test-tree discovery is implemented; use command for now"
                )
            result = subprocess.run(command, cwd=self.topdir, shell=True, check=False)
            if result.returncode:
                rc = result.returncode
        return rc

    def _changed_submodules(self) -> list[str]:
        """Submodules whose checkout differs from their manifest revision.

        PoC heuristic: ask west which projects are not at manifest-rev. The real
        version would diff each project against its upstream merge-base and also
        honour an explicit submod:<name> mapping on each test.
        """
        changed: list[str] = []
        for project in self.manifest.projects:
            if not self.manifest.is_active(project):
                continue
            path = Path(self.topdir) / project.path
            if not (path / ".git").exists():
                continue
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path, capture_output=True, text=True, check=False,
            ).stdout.strip()
            if project.revision and head and not head.startswith(project.revision):
                changed.append(project.name)
        return changed

    def _configure_and_build(self, testkit: Path, executor: str | None) -> Path:
        build = testkit / "build"
        cfg = ["cmake", "-S", str(testkit), "-B", str(build), "-G", "Ninja"]
        if executor:
            cfg.append(f"-DDARLING_TEST_EXECUTOR={executor}")
        self.inf(f"configuring: {testkit}")
        subprocess.run(cfg, check=True)
        subprocess.run(["ninja", "-C", str(build)], check=True)
        return build

    @staticmethod
    def _dir_size(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    def _gc_bundles(
        self, root: Path, keep_last: int, max_mb: int, dry_run: bool = False
    ) -> None:
        """Prune debug bundles so the dir cannot balloon (we saw 7.4G/980).

        Drop any bundle over max_mb (forensic cores/rpctrace), then keep only
        the newest keep_last of the rest. Bundles are timestamp-named dirs.
        Non-directory entries (stray files) are left untouched.
        """
        root = root.expanduser()
        if not root.is_dir():
            self.inf(f"no bundle dir at {root}")
            return
        bundles = sorted(
            (d for d in root.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        cap = max_mb * 1024 * 1024
        freed = 0
        kept = 0
        verb = "would prune" if dry_run else "pruned"
        for bundle in bundles:
            size = self._dir_size(bundle)
            over_cap = size > cap
            over_count = kept >= keep_last
            if over_cap or over_count:
                why = "size" if over_cap else "count"
                freed += size
                self.inf(f"{verb} ({why}, {size // (1024 * 1024)}M): {bundle.name}")
                if not dry_run:
                    shutil.rmtree(bundle, ignore_errors=True)
            else:
                kept += 1
        action = "would free" if dry_run else "freed"
        self.inf(
            f"gc: kept {kept}, {action} {freed // (1024 * 1024)}M from {root}"
        )

    # --- entrypoint ---------------------------------------------------------

    def do_run(self, args, unknown):
        if args.gc:
            self._gc_bundles(
                Path(args.bundle_root), args.keep_last, args.max_bundle_mb,
                dry_run=args.dry_run,
            )
            return

        if args.red_audit:
            profile = args.profile or "homebrew"
            _, missing = self._metadata_tests(
                profile, args.patch, args.bead, red_only=False
            )
            for patch in missing:
                self.inf(f"MISSING {patch['path']} [{patch.get('bead', '-')}]")
            self.inf(f"red-audit: {len(missing)} patch(es) missing tests/exception")
            return

        if args.patch and not args.profile:
            self.die("--patch requires --profile")

        if args.profile:
            selected, missing = self._metadata_tests(
                args.profile, args.patch, args.bead, args.red_only
            )
            if missing:
                for patch in missing:
                    self.inf(f"missing test metadata: {patch['path']} [{patch.get('bead', '-')}]")
            if selected:
                raise SystemExit(self._run_metadata_tests(selected, args.list, unknown))
            if args.list:
                return
            self.die("no tests selected from patch metadata")

        testkit = self._testkit_dir()
        if not testkit.exists():
            self.die(f"no testkit at {testkit}")

        executor = args.executor or shutil.which("darling-debug-runner")
        build = self._configure_and_build(testkit, executor)

        # Translate selectors into a CTest label regex (-L is ANDed per flag).
        label_args: list[str] = []
        if args.bead:
            label_args += ["-L", f"bead:{args.bead}"]
        if args.env:
            label_args += ["-L", f"env:{args.env}"]
        if args.diag:
            label_args += ["-L", f"diag:{args.diag}"]
        if args.label:
            label_args += ["-L", args.label]
        if args.changed:
            changed = self._changed_submodules()
            if not changed:
                self.inf("no changed submodules; nothing selected by --changed")
                return
            alternation = "|".join(f"submod:{name}" for name in changed)
            label_args += ["-L", alternation]
            self.inf(f"changed submodules: {', '.join(changed)}")

        ctest = ["ctest", "--test-dir", str(build), "--output-on-failure"]
        ctest += label_args
        if args.list:
            ctest.append("--show-only")
        ctest += unknown  # pass through e.g. -j, --repeat, --output-junit

        self.inf(f"running: {' '.join(ctest)}")
        raise SystemExit(subprocess.run(ctest, check=False).returncode)

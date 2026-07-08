"""Generic patch profiles for the Darling West workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import OrderedDict
from pathlib import Path

import yaml
from west.commands import WestCommand


def run(
    repo: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        args,
        cwd=repo,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        env=env,
    )
    return result.stdout.strip() if capture else ""


def git(
    repo: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    return run(repo, "git", *args, capture=capture, check=check, env=env)


def format_patch_command(patch, commit: str) -> list[str]:
    revision = (
        f"{patch['source-base']}..{commit}"
        if patch.get("source-base")
        else commit
    )
    command = [
        "git",
        "format-patch",
        "--stdout",
        "--no-signature",
        "--no-numbered",
        "--subject-prefix=PATCH",
        "--full-index",
        "--binary",
        "--no-renames",
    ]
    if not patch.get("source-base"):
        command.append("-1")
    command.append(revision)
    return command


class DarlingPatch(WestCommand):
    def __init__(self):
        super().__init__("patch", "", "Apply tracked Darling patch profiles")
        # Set per-invocation in do_run from the profile's `base-profile` key.
        self._base_profile = None

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        subparsers = parser.add_subparsers(dest="action", required=True)
        for action in ("list", "verify", "export", "apply", "clean", "status", "check"):
            command = subparsers.add_parser(action)
            command.add_argument("--profile", default="homebrew")
            if action == "export":
                command.add_argument(
                    "--check",
                    action="store_true",
                    help="verify exported patch files and metadata without writing",
                )
            if action == "apply":
                command.add_argument("--roll-back", action="store_true")
            if action == "clean":
                command.add_argument("--force", action="store_true")
            if action == "status":
                command.add_argument(
                    "--strict",
                    action="store_true",
                    help="exit non-zero if any patch is MISSING or CONFLICT",
                )
            if action == "check":
                command.add_argument(
                    "--strict",
                    action="store_true",
                    help="exit non-zero if a non-doc patch has no tests/exception",
                )
        return parser

    def do_run(self, args, unknown):
        if unknown:
            self.die(f"unknown arguments: {' '.join(unknown)}")

        manifest_repo = Path(self.manifest.repo_abspath)
        profile_dir = manifest_repo / "patches" / args.profile
        profile_path = profile_dir / "patches.yml"
        if not profile_path.is_file():
            self.die(f"patch profile not found: {profile_path}")

        profile = yaml.safe_load(profile_path.read_text())
        patches = profile.get("patches", [])
        # Optional stacking: a profile may declare `base-profile: <name>` to be
        # applied ON TOP of another profile's integration branch instead of the
        # raw manifest revision (e.g. a `perf` profile that stacks on `homebrew`
        # because a bootable build needs the homebrew fixes underneath). When
        # unset, the base is the manifest revision (the original behaviour).
        self._base_profile = profile.get("base-profile")
        if self._base_profile == args.profile:
            self.die(f"{args.profile}: base-profile cannot be itself")
        if args.action == "list":
            self._list(patches)
        elif args.action == "verify":
            self._verify(profile_dir, patches)
        elif args.action == "export":
            self._export(profile_path, profile_dir, profile, patches, args.check)
        elif args.action == "status":
            self._status(profile_dir, patches, args.strict)
        elif args.action == "check":
            self._check(profile_dir, patches, args.strict)
        elif args.action == "apply":
            self._apply(
                args.profile,
                profile_dir,
                patches,
                profile["integration-date"],
                args.roll_back,
            )
        else:
            self._clean(args.profile, patches, args.force)

    def _projects(self):
        result = {}
        for project in self.manifest.projects:
            result[project.name] = project
            result[project.path] = project
        return result

    def _group(self, patches):
        grouped = OrderedDict()
        for patch in patches:
            grouped.setdefault(patch["module"], []).append(patch)
        return grouped

    def _repo(self, module: str) -> Path:
        project = self._projects().get(module)
        if project is None:
            self.die(f"unknown West project: {module}")
        return Path(project.abspath)

    def _manifest_revision(self, module: str) -> str:
        project = self._projects().get(module)
        if project is None:
            self.die(f"unknown West project: {module}")
        revision = project.revision
        repo = Path(project.abspath)
        if not revision or subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=repo,
            check=False,
        ).returncode != 0:
            self.die(
                f"{module}: manifest revision {revision or '<empty>'} "
                f"is not available; run west update {project.name}"
            )
        return revision

    def _base_revision(self, module: str) -> str:
        """The commit a profile's patches are applied ON TOP of, for `module`.

        Without a base-profile this is the frozen manifest revision (original
        behaviour). With `base-profile: <name>`, it is the tip of that base
        profile's `integration/<name>` branch in the module IF that branch
        exists there (i.e. the base profile actually patches this module);
        otherwise it falls back to the manifest revision, because the base
        profile does not touch this module and there is nothing to stack on.
        """
        if not self._base_profile:
            return self._manifest_revision(module)
        repo = self._repo(module)
        base_branch = f"integration/{self._base_profile}"
        if self._branch_exists(repo, base_branch):
            return git(repo, "rev-parse", base_branch, capture=True)
        # Base profile does not patch this module -> stack directly on manifest.
        return self._manifest_revision(module)

    def _require_base_applied(self, modules):
        """Fail early if a stacked profile is asked to apply but its base
        profile's integration branch is missing from every module that base
        would patch. This catches "apply perf before applying homebrew"."""
        if not self._base_profile:
            return
        base_branch = f"integration/{self._base_profile}"
        base_dir = Path(self.manifest.repo_abspath) / "patches" / self._base_profile
        base_yml = base_dir / "patches.yml"
        if not base_yml.is_file():
            self.die(
                f"base-profile {self._base_profile!r} not found at {base_yml}"
            )
        base_modules = {
            p["module"] for p in yaml.safe_load(base_yml.read_text()).get("patches", [])
        }
        missing = [
            m
            for m in base_modules
            if not self._branch_exists(self._repo(m), base_branch)
        ]
        if missing:
            self.die(
                f"base profile {self._base_profile!r} is not applied "
                f"(missing {base_branch} in: {', '.join(sorted(missing))}). "
                f"Run `west patch apply --profile {self._base_profile}` first."
            )

    def _ensure_clean(self, repo: Path, parent: bool = False):
        command = ["status", "--porcelain"]
        if parent:
            command.extend(["--ignore-submodules=all", "--untracked-files=no"])
        if git(repo, *command, capture=True):
            raise RuntimeError(f"worktree is dirty: {repo}")

    def _list(self, patches):
        for patch in patches:
            source = patch.get("source-branch", "-")
            bead = patch.get("bead", "-")
            self.inf(f"{patch['module']}: {source} [{bead}]")
            self.inf(f"  {patch['path']}")
            for test in patch.get("tests", []) or []:
                red = " red" if test.get("red") else ""
                target = (
                    test.get("ctest-label")
                    or test.get("command")
                    or test.get("target")
                    or test.get("script")
                    or test.get("name", "-")
                )
                self.inf(f"    test:{red} {test.get('name', '-')} -> {target}")
            if patch.get("test-exception"):
                exc = patch["test-exception"]
                reason = exc.get("reason", exc) if isinstance(exc, dict) else exc
                self.inf(f"    test-exception: {reason}")

    def _validate_test_metadata(self, patch) -> list[str]:
        errors: list[str] = []
        tests = patch.get("tests")
        exception = patch.get("test-exception")
        if tests is not None and not isinstance(tests, list):
            errors.append("tests must be a list")
            return errors
        for index, test in enumerate(tests or [], start=1):
            if not isinstance(test, dict):
                errors.append(f"tests[{index}] must be a mapping")
                continue
            if not test.get("name"):
                errors.append(f"tests[{index}] missing name")
            if not (
                test.get("command")
                or test.get("ctest-label")
                or test.get("script")
                or test.get("target")
            ):
                errors.append(
                    f"tests[{index}] needs script, target, ctest-label, or command override"
                )
            runner = test.get("runner")
            if runner and runner not in {"script", "west-build", "ctest"}:
                errors.append(f"tests[{index}] invalid runner {runner!r}")
            if test.get("target") and runner not in {None, "west-build"}:
                errors.append(f"tests[{index}] target requires runner: west-build")
            if test.get("script") and runner not in {None, "script"}:
                errors.append(f"tests[{index}] script requires runner: script")
            if test.get("args") is not None and not isinstance(test.get("args"), list):
                errors.append(f"tests[{index}] args must be a list")
            if test.get("env-vars") is not None and not isinstance(test.get("env-vars"), dict):
                errors.append(f"tests[{index}] env-vars must be a mapping")
            env = test.get("env")
            if env and env not in {"host", "darling", "macos"}:
                errors.append(f"tests[{index}] invalid env {env!r}")
            diag = test.get("diag")
            if diag and diag not in {"bare", "guarded", "forensic"}:
                errors.append(f"tests[{index}] invalid diag {diag!r}")
            kind = test.get("kind")
            if kind and kind not in {
                "unit",
                "contract",
                "guest",
                "package",
                "fuzz",
                "stress",
                "build",
                "gate",
            }:
                errors.append(f"tests[{index}] invalid kind {kind!r}")
        if exception is not None:
            if not isinstance(exception, dict):
                errors.append("test-exception must be a mapping")
            elif not exception.get("reason"):
                errors.append("test-exception needs reason")
        return errors

    def _check(self, profile_dir: Path, patches, strict: bool):
        missing = []
        invalid = []
        covered = 0
        excepted = 0
        for patch in patches:
            errors = self._validate_test_metadata(patch)
            if errors:
                invalid.append((patch, errors))
                continue
            tests = patch.get("tests") or []
            exception = patch.get("test-exception")
            if tests:
                covered += 1
                self.inf(f"TESTED    {patch['path']} ({len(tests)} test(s))")
            elif exception:
                excepted += 1
                reason = exception.get("reason", "-")
                self.inf(f"EXCEPTION {patch['path']} ({reason})")
            else:
                missing.append(patch)
                self.inf(f"MISSING   {patch['path']}  [{patch.get('bead', '-')}]")
        for patch, errors in invalid:
            for error in errors:
                self.err(f"INVALID   {patch['path']}: {error}")
        self.inf(
            f"test metadata: {covered} covered, {excepted} exceptions, "
            f"{len(missing)} missing, {len(invalid)} invalid "
            f"(of {len(patches)})"
        )
        if missing:
            self.inf(
                "hint: add tests: [{name, runner, script|target|ctest-label, env, diag, kind, red}] "
                "or test-exception: {reason, note}"
            )
        if strict and (missing or invalid):
            self.die(
                f"{len(missing)} missing + {len(invalid)} invalid patch test metadata entries"
            )

    def _prepare(
        self, module: str, repo: Path, branch: str, parent: bool = False
    ):
        self._ensure_clean(repo, parent=parent)
        # Start from the base revision: manifest-rev normally, or the base
        # profile's integration tip when this profile stacks (base-profile).
        revision = self._base_revision(module)
        git(repo, "switch", "--detach", revision)
        git(repo, "branch", "-f", branch, revision)
        git(repo, "switch", branch)

    def _ensure_generated_context(self, module: str, profile: str):
        repo = self._repo(module)
        branch = f"integration/{profile}"
        current = git(repo, "branch", "--show-current", capture=True)
        # The acceptable detached base is the base revision (manifest-rev, or
        # the base profile's integration tip when stacking).
        base_revision = self._base_revision(module)
        base_commit = git(repo, "rev-parse", base_revision, capture=True)
        head = git(repo, "rev-parse", "HEAD", capture=True)
        # Accept being: on our own integration branch; detached at the base
        # commit; or (when stacking) still on the base profile's integration
        # branch -- which is exactly where the module legitimately sits right
        # before a stacked profile is applied for the first time.
        base_branch = (
            f"integration/{self._base_profile}" if self._base_profile else None
        )
        allowed = (
            current == branch
            or (current == base_branch and base_branch is not None)
            or (not current and head == base_commit)
        )
        if not allowed:
            expected = f"{branch} or detached {base_revision}"
            if base_branch:
                expected = f"{branch}, {base_branch}, or detached {base_revision}"
            raise RuntimeError(
                f"{module}: expected {expected}, found {current or head}"
            )
        self._ensure_clean(repo, parent=module == "darling")

    def _verify_patch(self, profile_dir: Path, patch):
        path = profile_dir / patch["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = patch["sha256sum"]
        if actual != expected:
            raise RuntimeError(
                f"checksum mismatch for {path}: {actual} != {expected}"
            )
        return path

    def _verify(self, profile_dir: Path, patches):
        manifest_repo = Path(self.manifest.repo_abspath)
        bead_ids = {
            json.loads(line)["id"]
            for line in (
                manifest_repo / ".beads" / "issues.jsonl"
            ).read_text().splitlines()
            if line.strip()
        }
        grouped = self._group(patches)

        for patch in patches:
            source_commit = patch.get("source-commit", "")
            if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
                self.die(f"{patch['path']}: source-commit must be a full SHA")

            publication_status = patch.get("publication-status")
            if publication_status not in {"ready", "provisional", "blocked"}:
                self.die(
                    f"{patch['path']}: invalid publication-status "
                    f"{publication_status!r}"
                )
            if (
                publication_status != "ready"
                and not patch.get("publication-blocker")
            ):
                self.die(
                    f"{patch['path']}: {publication_status} fixes require "
                    "publication-blocker"
                )

            repo = self._repo(patch["module"])
            source_branch = patch["source-branch"]
            if not self._branch_exists(repo, source_branch):
                self.die(f"{patch['module']}: missing source branch {source_branch}")
            branch_head = git(repo, "rev-parse", source_branch, capture=True)
            if branch_head != source_commit:
                self.die(
                    f"{patch['module']}: {source_branch} drifted "
                    f"({branch_head} != {source_commit})"
                )

            path = self._verify_patch(profile_dir, patch)
            exported = subprocess.run(
                format_patch_command(patch, source_commit),
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            if hashlib.sha256(exported).hexdigest() != patch["sha256sum"]:
                self.die(f"{patch['path']}: patch export drifted")

            bead = patch.get("bead")
            if bead and bead not in bead_ids:
                self.die(f"{patch['path']}: unknown Bead {bead}")
            pr_draft = patch.get("pr-draft")
            if pr_draft and not (manifest_repo / pr_draft).is_file():
                self.die(f"{patch['path']}: missing PR draft {pr_draft}")
            self.inf(f"verified {path.relative_to(manifest_repo)}")

        self._verify_applicability(profile_dir, grouped)
        self.inf(f"verified {len(patches)} patches")

    def _export(self, profile_path: Path, profile_dir: Path, profile, patches, check: bool):
        changed = False
        for patch in patches:
            repo = self._repo(patch["module"])
            source_branch = patch["source-branch"]
            if not self._branch_exists(repo, source_branch):
                self.die(f"{patch['module']}: missing source branch {source_branch}")

            commit = git(repo, "rev-parse", source_branch, capture=True)
            exported = subprocess.run(
                format_patch_command(patch, commit),
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            checksum = hashlib.sha256(exported).hexdigest()
            output = profile_dir / patch["path"]

            current_content = output.read_bytes() if output.is_file() else None
            patch_changed = (
                patch.get("source-commit") != commit
                or patch.get("sha256sum") != checksum
                or current_content != exported
            )
            if check:
                if patch_changed:
                    self.die(f"{patch['path']}: exported patch drift")
                self.inf(f"export-check OK {output.relative_to(profile_dir)}")
                continue

            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(exported)
            patch["source-commit"] = commit
            patch["sha256sum"] = checksum
            changed = changed or patch_changed
            self.inf(f"exported {output.relative_to(profile_dir)}")

        if not check:
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False, width=1000))
            self.inf(
                f"updated {profile_path.relative_to(Path(self.manifest.repo_abspath))}"
                if changed
                else f"refreshed {profile_path.relative_to(Path(self.manifest.repo_abspath))}"
            )

    def _verify_applicability(self, profile_dir: Path, grouped):
        with tempfile.TemporaryDirectory(prefix="west-patch-verify-") as temp:
            temp_root = Path(temp)
            for index, (module, module_patches) in enumerate(grouped.items()):
                repo = self._repo(module)
                worktree = temp_root / str(index)
                # Verify each module's patches apply on the SAME base they will
                # be applied on: manifest-rev, or the base profile's integration
                # tip when stacking.
                revision = self._base_revision(module)
                git(
                    repo,
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(worktree),
                    revision,
                )
                try:
                    for patch in module_patches:
                        git(
                            worktree,
                            "am",
                            "--3way",
                            "--committer-date-is-author-date",
                            str(profile_dir / patch["path"]),
                        )
                finally:
                    self._abort_am(worktree)
                    git(
                        repo,
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                        check=False,
                    )

    def _patch_state(self, repo: Path, patch_path: Path) -> str:
        """Classify a patch against the CURRENT working tree of `repo`.

        Unlike `verify` (which checks the patches against a throwaway worktree at
        the frozen manifest revision), this inspects the tree you actually build
        from -- so it catches the case where the build tree drifted away from
        patches.yml and a tracked fix is silently missing.

          APPLIED  - the patch reverse-applies cleanly: its content is present
                     verbatim against the current tree.
          MISSING  - the patch forward-applies cleanly: its content is ABSENT.
                     This is the authoritative "lost patch" signal.
          STACKED? - neither clean reverse nor clean forward. For a standalone
                     patch this means partially applied / context-shifted /
                     conflicting. But it is ALSO the normal result for a member
                     of an interdependent patch SERIES (e.g. eunion-*): once a
                     later patch in the series edits the same lines, an earlier
                     patch no longer reverse-applies verbatim even though it is
                     fully present. So STACKED? is NOT proof of drift -- only
                     MISSING is. Trust a clean `west patch apply`/`verify` as the
                     authoritative applicability check for stacked series.
        """
        reverse_ok = (
            subprocess.run(
                ["git", "apply", "--reverse", "--check", str(patch_path)],
                cwd=repo,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if reverse_ok:
            return "APPLIED"
        forward_ok = (
            subprocess.run(
                ["git", "apply", "--check", str(patch_path)],
                cwd=repo,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if forward_ok:
            return "MISSING"
        return "STACKED?"

    def _status(self, profile_dir: Path, patches, strict: bool):
        grouped = self._group(patches)
        counts = {"APPLIED": 0, "MISSING": 0, "STACKED?": 0, "NOFILE": 0}
        for module, module_patches in grouped.items():
            repo = self._repo(module)
            branch = git(repo, "branch", "--show-current", capture=True) or (
                "(detached " + git(repo, "rev-parse", "--short", "HEAD", capture=True) + ")"
            )
            self.inf(f"{module}  [{branch}]")
            for patch in module_patches:
                patch_path = profile_dir / patch["path"]
                if not patch_path.is_file():
                    state = "NOFILE"
                else:
                    state = self._patch_state(repo, patch_path)
                counts[state] = counts.get(state, 0) + 1
                bead = patch.get("bead", "-")
                self.inf(f"  {state:8} {patch['path']}  [{bead}]")
        total = sum(counts.values())
        self.inf(
            f"status: {counts['APPLIED']} applied, {counts['MISSING']} missing, "
            f"{counts['STACKED?']} stacked?, {counts['NOFILE']} no-file "
            f"(of {total})"
        )
        if counts["STACKED?"]:
            self.inf(
                "note: STACKED? is expected for interdependent series (eunion-*) "
                "and is NOT drift; only MISSING means a tracked patch is absent."
            )
        # MISSING is the authoritative drift signal; STACKED? is not (see
        # _patch_state). A NOFILE is a real error (patches.yml references a
        # patch file that does not exist).
        if strict and (counts["MISSING"] or counts["NOFILE"]):
            self.die(
                f"{counts['MISSING']} missing + {counts['NOFILE']} no-file "
                "patch(es) -- build tree drifted from patches.yml"
            )

    def _abort_am(self, repo: Path):
        am_state = git(
            repo, "rev-parse", "--git-path", "rebase-apply", capture=True
        )
        if (repo / am_state).exists():
            git(repo, "am", "--abort", check=False)

    def _reset_submodule_index(self, repo: Path):
        """Reset submodule gitlink entries in the index to match HEAD.

        Parking the submodules (detaching them onto their base revisions) moves
        each nested module's HEAD, which dirties the superproject's gitlink
        pointers in the index. `git am` runs its own dirty-index guard that --
        unlike our `_ensure_clean` (which passes --ignore-submodules=all) -- is
        NOT submodule-aware, so it refuses with "Dirty index: cannot apply".
        The darling-module patches only touch real files (e.g. mldr.c), never
        the gitlinks, so restoring the gitlink index entries to HEAD makes the
        index clean from `git am`'s perspective without dropping any real change.
        The submodules' own working trees / branches are untouched."""
        dirty = git(
            repo,
            "diff",
            "--cached",
            "--ignore-submodules=none",
            "--name-only",
            "--diff-filter=M",
            capture=True,
        )
        paths = [
            line
            for line in dirty.splitlines()
            if (repo / line / ".git").exists()
        ]
        for path in paths:
            git(repo, "reset", "--quiet", "HEAD", "--", path)

    def _branch_exists(self, repo: Path, branch: str) -> bool:
        return (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=repo,
                check=False,
            ).returncode
            == 0
        )

    def _apply(
        self,
        profile: str,
        profile_dir: Path,
        patches,
        integration_date: str,
        roll_back: bool,
    ):
        lock_source = Path(self.manifest.repo_abspath) / "west.lock.yml"
        if not lock_source.is_file():
            self.die(f"frozen manifest not found: {lock_source}")

        branch = f"integration/{profile}"
        grouped = self._group(patches)
        # A stacked profile requires its base profile to be applied first.
        self._require_base_applied(list(grouped))
        modules = list(grouped)
        if "darling" not in modules:
            modules.append("darling")
        for module in modules:
            try:
                self._ensure_generated_context(module, profile)
            except RuntimeError as error:
                self.die(str(error))

        touched = []
        try:
            for module, module_patches in grouped.items():
                repo = self._repo(module)
                self._prepare(
                    module, repo, branch, parent=module == "darling"
                )
                touched.append(repo)
                if module == "darling":
                    # `git am` on the superproject has a submodule-unaware
                    # dirty-index guard; parked submodule gitlinks trip it even
                    # though the patches only touch real files. See helper.
                    self._reset_submodule_index(repo)
                for patch in module_patches:
                    path = self._verify_patch(profile_dir, patch)
                    git(
                        repo,
                        "am",
                        "--3way",
                        "--committer-date-is-author-date",
                        str(path),
                    )
                self.inf(f"{module}: applied {len(module_patches)} patches")
            lock = self._record_integration(profile, grouped, integration_date)
            self.inf(f"wrote {lock}")
        except Exception as error:
            for repo in touched:
                self._abort_am(repo)
            if roll_back:
                self._reset(profile, grouped, force=True)
            self.die(str(error))

    def _record_integration(
        self, profile: str, grouped, integration_date: str
    ):
        branch = f"integration/{profile}"
        darling = self._repo("darling")
        if "darling" not in grouped:
            self._prepare("darling", darling, branch, parent=True)

        nested = [module for module in grouped if module != "darling"]
        if nested:
            paths = [str(Path(module).relative_to("darling")) for module in nested]
            git(darling, "add", *paths)
            commit_env = os.environ.copy()
            commit_env["GIT_AUTHOR_DATE"] = integration_date
            commit_env["GIT_COMMITTER_DATE"] = integration_date
            git(
                darling,
                "commit",
                "-m",
                f"Integrate {profile} patch profile",
                env=commit_env,
            )

        # Seed the lock from the base profile's lock when stacking (so modules
        # the stacked profile does NOT patch keep the base profile's revisions,
        # not the raw manifest revision); otherwise from the frozen manifest.
        if self._base_profile:
            base_lock = (
                Path(self.manifest.repo_abspath)
                / "patches"
                / self._base_profile
                / "west.lock.yml"
            )
            lock_path = base_lock if base_lock.is_file() else (
                Path(self.manifest.repo_abspath) / "west.lock.yml"
            )
        else:
            lock_path = Path(self.manifest.repo_abspath) / "west.lock.yml"
        lock_data = yaml.safe_load(lock_path.read_text())
        revisions = {"darling": git(darling, "rev-parse", "HEAD", capture=True)}
        for module in grouped:
            revisions[module] = git(
                self._repo(module), "rev-parse", "HEAD", capture=True
            )
        for project in lock_data["manifest"]["projects"]:
            path = project.get("path", project["name"])
            if path in revisions:
                project["revision"] = revisions[path]

        output = (
            Path(self.manifest.repo_abspath)
            / "patches"
            / profile
            / "west.lock.yml"
        )
        output.write_text(yaml.safe_dump(lock_data, sort_keys=False, width=1000))
        return output

    def _reset(self, profile: str, grouped, force: bool):
        branch = f"integration/{profile}"
        modules = list(grouped)
        if "darling" not in modules:
            modules.append("darling")

        for module in modules:
            if not force:
                try:
                    self._ensure_generated_context(module, profile)
                except RuntimeError as error:
                    self.die(f"refusing to clean: {error}")

        for module in reversed(modules):
            repo = self._repo(module)
            self._abort_am(repo)
            # Reset to the base this profile was applied on: manifest-rev, or
            # the base profile's integration tip when stacking. Resetting a
            # stacked profile therefore leaves the base profile intact.
            revision = self._base_revision(module)
            switch_args = ["switch", "--detach", revision]
            if force:
                switch_args.insert(1, "--discard-changes")
            git(repo, *switch_args)
            if self._branch_exists(repo, branch):
                git(repo, "branch", "-D", branch)
            self.inf(f"{module}: reset to {revision}")

    def _clean(self, profile: str, patches, force: bool):
        self._reset(profile, self._group(patches), force=force)

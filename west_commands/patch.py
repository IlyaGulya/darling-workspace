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


class DarlingPatch(WestCommand):
    def __init__(self):
        super().__init__("patch", "", "Apply tracked Darling patch profiles")

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        subparsers = parser.add_subparsers(dest="action", required=True)
        for action in ("list", "verify", "apply", "clean"):
            command = subparsers.add_parser(action)
            command.add_argument("--profile", default="homebrew")
            if action == "apply":
                command.add_argument("--roll-back", action="store_true")
            if action == "clean":
                command.add_argument("--force", action="store_true")
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
        if args.action == "list":
            self._list(patches)
        elif args.action == "verify":
            self._verify(profile_dir, patches)
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

    def _prepare(self, repo: Path, branch: str, parent: bool = False):
        self._ensure_clean(repo, parent=parent)
        git(repo, "switch", "--detach", "refs/heads/manifest-rev")
        git(repo, "branch", "-f", branch, "refs/heads/manifest-rev")
        git(repo, "switch", branch)

    def _ensure_generated_context(self, module: str, profile: str):
        repo = self._repo(module)
        branch = f"integration/{profile}"
        current = git(repo, "branch", "--show-current", capture=True)
        manifest_revision = git(
            repo, "rev-parse", "refs/heads/manifest-rev", capture=True
        )
        head = git(repo, "rev-parse", "HEAD", capture=True)
        allowed = current == branch or (not current and head == manifest_revision)
        if not allowed:
            raise RuntimeError(
                f"{module}: expected {branch} or detached manifest-rev, "
                f"found {current or head}"
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
                [
                    "git",
                    "format-patch",
                    "-1",
                    "--stdout",
                    "--no-signature",
                    "--no-numbered",
                    "--subject-prefix=PATCH",
                    "--full-index",
                    "--binary",
                    "--no-renames",
                    source_commit,
                ],
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

    def _verify_applicability(self, profile_dir: Path, grouped):
        with tempfile.TemporaryDirectory(prefix="west-patch-verify-") as temp:
            temp_root = Path(temp)
            for index, (module, module_patches) in enumerate(grouped.items()):
                repo = self._repo(module)
                worktree = temp_root / str(index)
                git(
                    repo,
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(worktree),
                    "refs/heads/manifest-rev",
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

    def _abort_am(self, repo: Path):
        am_state = git(
            repo, "rev-parse", "--git-path", "rebase-apply", capture=True
        )
        if (repo / am_state).exists():
            git(repo, "am", "--abort", check=False)

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
                self._prepare(repo, branch, parent=module == "darling")
                touched.append(repo)
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
            self._prepare(darling, branch, parent=True)

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

        lock_data = yaml.safe_load(
            (Path(self.manifest.repo_abspath) / "west.lock.yml").read_text()
        )
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
            switch_args = ["switch", "--detach", "refs/heads/manifest-rev"]
            if force:
                switch_args.insert(1, "--discard-changes")
            git(repo, *switch_args)
            if self._branch_exists(repo, branch):
                git(repo, "branch", "-D", branch)
            self.inf(f"{module}: reset to manifest-rev")

    def _clean(self, profile: str, patches, force: bool):
        self._reset(profile, self._group(patches), force=force)

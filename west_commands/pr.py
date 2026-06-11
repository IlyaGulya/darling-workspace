"""GitHub pull request workflow for clean Darling topic branches."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import tempfile
from pathlib import Path

import yaml
from west.commands import WestCommand


PR_FIELDS = (
    "number,url,state,isDraft,title,headRefName,headRefOid,baseRefName,"
    "reviewDecision,mergeStateStatus,statusCheckRollup,mergedAt,mergeCommit"
)


def run(
    cwd: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def git(repo: Path, *args: str, capture: bool = False, check: bool = True) -> str:
    return run(repo, "git", *args, capture=capture, check=check)


class DarlingPr(WestCommand):
    def __init__(self):
        super().__init__("pr", "", "Manage staged and upstream Darling pull requests")

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        subparsers = parser.add_subparsers(dest="action", required=True)

        list_parser = subparsers.add_parser("list")
        list_parser.add_argument("--profile", default="homebrew")
        dashboard = subparsers.add_parser("dashboard")
        dashboard.add_argument("--profile", default="homebrew")
        check = subparsers.add_parser("check")
        check.add_argument("--profile", default="homebrew")
        check.add_argument("bead", nargs="?")
        plan = subparsers.add_parser("publish-plan")
        plan.add_argument("--profile", default="homebrew")
        plan.add_argument("bead")
        plan.add_argument("--target", choices=("fork", "upstream"), default="fork")
        sync = subparsers.add_parser("sync")
        sync.add_argument("--profile", default="homebrew")
        sync.add_argument("bead", nargs="?")
        open_parser = subparsers.add_parser("open")
        open_parser.add_argument("--profile", default="homebrew")
        open_parser.add_argument("bead")
        open_parser.add_argument("--target", choices=("fork", "upstream"), required=True)
        open_parser.add_argument("--print", action="store_true", dest="print_only")

        fork = subparsers.add_parser("fork-draft")
        fork.add_argument("--profile", default="homebrew")
        fork.add_argument("bead")
        fork.add_argument("--dry-run", action="store_true")

        upstream = subparsers.add_parser("upstream-draft")
        upstream.add_argument("--profile", default="homebrew")
        upstream.add_argument("bead")
        upstream.add_argument("--skip-fork-draft", action="store_true")
        upstream.add_argument("--dry-run", action="store_true")

        update = subparsers.add_parser("update-body")
        update.add_argument("--profile", default="homebrew")
        update.add_argument("bead")
        update.add_argument("--target", choices=("fork", "upstream"), required=True)
        update.add_argument("--dry-run", action="store_true")

        ready = subparsers.add_parser("ready")
        ready.add_argument("--profile", default="homebrew")
        ready.add_argument("bead")
        ready.add_argument("--target", choices=("fork", "upstream"), required=True)
        ready.add_argument("--dry-run", action="store_true")
        return parser

    def do_run(self, args, unknown):
        if unknown:
            self.die(f"unknown arguments: {' '.join(unknown)}")

        self.manifest_repo = Path(self.manifest.repo_abspath)
        self.profile_path = (
            self.manifest_repo / "patches" / args.profile / "patches.yml"
        )
        self.profile = yaml.safe_load(self.profile_path.read_text())
        self.patches = self.profile["patches"]
        self.projects = {}
        self.repo_info = {}
        for project in self.manifest.projects:
            self.projects[project.name] = project
            self.projects[project.path] = project

        if args.action == "list":
            self._list()
            return
        if args.action == "dashboard":
            self._dashboard()
            return

        selected = self._select(getattr(args, "bead", None))
        if args.action == "check":
            for patch in selected:
                self._check(patch)
            return
        if args.action == "sync":
            for patch in selected:
                self._sync(patch)
            self._save()
            return
        if args.action == "publish-plan":
            self._publish_plan(selected[0], args.target)
            return
        if args.action == "open":
            self._open(selected[0], args.target, args.print_only)
            return

        patch = selected[0]
        if args.action == "fork-draft":
            self._publish(patch, "fork", args.dry_run)
        elif args.action == "upstream-draft":
            fork_state = patch["github"]["fork"].get("state", "local")
            if fork_state not in {"open", "draft", "ready", "merged"}:
                if not args.skip_fork_draft:
                    self.die(
                        "fork draft is required; create it first or pass "
                        "--skip-fork-draft explicitly"
                    )
            self._publish(patch, "upstream", args.dry_run)
        elif args.action == "update-body":
            self._update_body(patch, args.target, args.dry_run)
        else:
            self._ready(patch, args.target, args.dry_run)

    def _select(self, bead):
        if bead is None:
            return self.patches
        matches = [patch for patch in self.patches if patch.get("bead") == bead]
        if len(matches) != 1:
            self.die(f"expected one patch for {bead}, found {len(matches)}")
        return matches

    def _repo(self, patch) -> Path:
        project = self.projects.get(patch["module"])
        if project is None:
            self.die(f"unknown West project: {patch['module']}")
        return Path(project.abspath)

    def _list(self):
        for patch in self.patches:
            github = patch.get("github", {})
            fork = github.get("fork", {}).get("state", "-")
            upstream = github.get("upstream", {}).get("state", "-")
            self.inf(
                f"{patch.get('bead', '-')}: {patch['source-branch']} "
                f"fork={fork} upstream={upstream}"
            )

    def _checks(self, data) -> str:
        checks = data.get("statusCheckRollup") or []
        if not checks:
            return "-"
        states = []
        for check in checks:
            state = (
                check.get("conclusion")
                or check.get("state")
                or check.get("status")
                or ""
            ).upper()
            if state:
                states.append(state)
        if not states:
            return "-"
        if any(state in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"} for state in states):
            return "failed"
        if any(state in {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED"} for state in states):
            return "pending"
        if all(state in {"SUCCESS", "NEUTRAL", "SKIPPED"} for state in states):
            return "passed"
        return "unknown"

    def _patch_status(self, patch) -> str:
        path = self.profile_path.parent / patch["path"]
        if not path.is_file():
            return "missing"
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        return "ok" if checksum == patch["sha256sum"] else "drift"

    def _pr_summary(self, patch, target) -> tuple[str, str]:
        config = patch.get("github", {}).get(target, {})
        url = config.get("url")
        if not url:
            state = config.get("state", "none")
            return ("none" if state == "local" else state), "-"
        data = self._gh_json(config["repo"], url)
        state = self._state(data)
        if data["headRefOid"] != patch["source-commit"]:
            state = "needs-update"
        number = data.get("number")
        label = f"{state} #{number}" if number else state
        return label, self._checks(data)

    def _dashboard(self):
        rows = []
        for patch in self.patches:
            fork, fork_checks = self._pr_summary(patch, "fork")
            upstream, upstream_checks = self._pr_summary(patch, "upstream")
            checks = upstream_checks if upstream_checks != "-" else fork_checks
            rows.append(
                (
                    patch.get("bead", "-"),
                    patch["source-branch"],
                    self._patch_status(patch),
                    fork,
                    upstream,
                    checks,
                )
            )
        headers = ("Bead", "Branch", "Patch", "Fork PR", "Upstream PR", "Checks")
        widths = [
            max(len(str(row[index])) for row in [headers, *rows])
            for index in range(len(headers))
        ]
        self.inf(
            "  ".join(
                str(value).ljust(widths[index])
                for index, value in enumerate(headers)
            )
        )
        for row in rows:
            self.inf(
                "  ".join(
                    str(value).ljust(widths[index])
                    for index, value in enumerate(row)
                )
            )

    def _check(self, patch):
        branch = patch["source-branch"]
        if not branch.startswith("fix/"):
            self.die(f"{patch['bead']}: only clean fix/* branches are publishable")
        if branch.startswith(("integration/", "backup/")):
            self.die(f"{patch['bead']}: generated or backup branch is forbidden")

        repo = self._repo(patch)
        if git(repo, "status", "--porcelain", capture=True):
            self.die(f"{patch['module']}: worktree is dirty")
        head = git(repo, "rev-parse", branch, capture=True)
        if head != patch["source-commit"]:
            self.die(f"{patch['bead']}: source branch drifted")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "manifest-rev", head],
            cwd=repo,
            check=False,
        ).returncode
        if ancestor != 0:
            self.die(f"{patch['bead']}: source branch is not based on manifest-rev")

        patch_path = self.profile_path.parent / patch["path"]
        checksum = hashlib.sha256(patch_path.read_bytes()).hexdigest()
        if checksum != patch["sha256sum"]:
            self.die(f"{patch['bead']}: patch checksum drifted")
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
                head,
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        if hashlib.sha256(exported).hexdigest() != patch["sha256sum"]:
            self.die(f"{patch['bead']}: exported patch drifted")
        draft = self.manifest_repo / patch.get("pr-draft", "")
        if not patch.get("pr-draft") or not draft.is_file():
            self.die(f"{patch['bead']}: PR draft is missing")
        self._draft_content(draft)

        github = patch.get("github", {})
        for target in ("fork", "upstream"):
            config = github.get(target, {})
            if not config.get("repo") or not config.get("base"):
                self.die(f"{patch['bead']}: GitHub {target} metadata is incomplete")
            self._github_repo(config["repo"])
        fork_parent = self._github_repo(github["fork"]["repo"]).get("parent")
        parent_name = (
            f"{fork_parent['owner']['login']}/{fork_parent['name']}"
            if fork_parent
            else ""
        )
        if parent_name != github["upstream"]["repo"]:
            self.die(f"{patch['bead']}: configured fork does not match upstream")
        upstream_default = self._github_repo(
            github["upstream"]["repo"]
        )["defaultBranchRef"]["name"]
        if upstream_default != github["upstream"]["base"]:
            self.die(
                f"{patch['bead']}: upstream base {github['upstream']['base']} "
                f"is not default branch {upstream_default}"
            )
        self.inf(f"{patch['bead']}: publishable")

    def _github_repo(self, repo: str):
        if repo not in self.repo_info:
            output = run(
                self.manifest_repo,
                "gh",
                "repo",
                "view",
                repo,
                "--json",
                "nameWithOwner,defaultBranchRef,parent,isFork,url",
                capture=True,
            )
            self.repo_info[repo] = json.loads(output)
        return self.repo_info[repo]

    def _draft_content(self, path: Path) -> tuple[str, str]:
        text = path.read_text()
        title_marker = "\n## Title\n"
        body_marker = "\n## Body\n"
        if title_marker not in text or body_marker not in text:
            self.die(f"{path}: expected ## Title and ## Body sections")
        title = text.split(title_marker, 1)[1].split("\n## ", 1)[0].strip()
        body = text.split(body_marker, 1)[1].strip()
        if not title or not body:
            self.die(f"{path}: empty title or body")
        return title, body

    def _gh_json(self, repo: str, url: str):
        output = run(
            self.manifest_repo,
            "gh",
            "pr",
            "view",
            url,
            "--repo",
            repo,
            "--json",
            PR_FIELDS,
            capture=True,
        )
        return json.loads(output)

    def _state(self, data):
        if data.get("mergedAt"):
            return "merged"
        if data["state"] == "CLOSED":
            return "closed"
        if data["isDraft"]:
            return "draft"
        return "ready"

    def _sync(self, patch):
        for target in ("fork", "upstream"):
            config = patch.get("github", {}).get(target, {})
            url = config.get("url")
            if not url:
                continue
            data = self._gh_json(config["repo"], url)
            config.update(
                {
                    "number": data["number"],
                    "url": data["url"],
                    "state": self._state(data),
                    "head-commit": data["headRefOid"],
                    "is-draft": data["isDraft"],
                    "review-decision": data.get("reviewDecision") or "",
                    "merge-state": data.get("mergeStateStatus") or "",
                }
            )
            if data["headRefOid"] != patch["source-commit"]:
                config["remote-state"] = config["state"]
                config["state"] = "needs-update"
            else:
                config.pop("remote-state", None)
            if data.get("mergeCommit"):
                config["merge-commit"] = data["mergeCommit"]["oid"]
            self.inf(f"{patch['bead']} {target}: {config['state']} {data['url']}")

    def _find_existing(self, patch, target):
        config = patch["github"][target]
        head = patch["source-branch"]
        if target == "upstream":
            owner = patch["github"]["fork"]["repo"].split("/", 1)[0]
            head = f"{owner}:{head}"
        output = run(
            self.manifest_repo,
            "gh",
            "pr",
            "list",
            "--repo",
            config["repo"],
            "--base",
            config["base"],
            "--head",
            head,
            "--state",
            "all",
            "--json",
            "number,url,state,isDraft,headRefOid",
            capture=True,
        )
        matches = json.loads(output)
        return matches[0] if matches else None

    def _lease(self, repo: Path, branch: str) -> str:
        output = git(
            repo,
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
            capture=True,
        )
        remote_sha = output.split()[0] if output else ""
        return f"--force-with-lease=refs/heads/{branch}:{remote_sha}"

    def _push_command(self, patch, target, dry_run):
        config = patch["github"][target]
        branch = patch["source-branch"]
        repo = self._repo(patch)
        commands = []
        if target == "fork":
            base = config["base"]
            commands.append(
                [
                    "git",
                    "push",
                    "origin",
                    f"refs/heads/manifest-rev:refs/heads/{base}",
                    (
                        "--force-with-lease"
                        if dry_run
                        else self._lease(repo, base)
                    ),
                ]
            )
        commands.append(
            [
                "git",
                "push",
                "origin",
                f"{patch['source-commit']}:refs/heads/{branch}",
                (
                    "--force-with-lease"
                    if dry_run
                    else self._lease(repo, branch)
                ),
            ]
        )
        return repo, commands

    def _create_command(self, patch, target):
        config = patch["github"][target]
        title, _ = self._draft_content(self.manifest_repo / patch["pr-draft"])
        head = patch["source-branch"]
        if target == "upstream":
            fork_owner = patch["github"]["fork"]["repo"].split("/", 1)[0]
            head = f"{fork_owner}:{head}"
        return [
            "gh",
            "pr",
            "create",
            "--repo",
            config["repo"],
            "--base",
            config["base"],
            "--head",
            head,
            "--draft",
            "--title",
            title,
            "--body-file",
            str(self.manifest_repo / patch["pr-draft"]),
        ]

    def _publish_plan(self, patch, target):
        self._check(patch)
        config = patch["github"][target]
        if config.get("url"):
            state = "already exists"
        elif (
            target == "upstream"
            and patch["github"]["fork"].get("state", "local")
            not in {"open", "draft", "ready", "merged"}
        ):
            state = "blocked: fork draft required"
        else:
            state = "would create draft"
        self.inf("Bead       Repo                           Branch                       Target    State")
        self.inf(
            f"{patch['bead']:<10} {config['repo']:<30} "
            f"{patch['source-branch']:<28} {target:<9} {state}"
        )
        if config.get("url"):
            self.inf(config["url"])
            return
        if state.startswith("blocked:"):
            return
        _, push_commands = self._push_command(patch, target, dry_run=True)
        for command in push_commands:
            self.inf("$ " + shlex.join(command))
        create = self._create_command(patch, target)[:-2]
        draft = self.manifest_repo / patch["pr-draft"]
        body_source = (
            f"<(sed -n '/^## Body$/,$p' {shlex.quote(str(draft))} | tail -n +2)"
        )
        self.inf("$ " + shlex.join(create) + " --body-file " + body_source)

    def _open(self, patch, target, print_only):
        config = patch.get("github", {}).get(target, {})
        url = config.get("url")
        if not url:
            self.die(f"{patch['bead']}: no {target} PR")
        if print_only:
            self.inf(url)
            return
        run(
            self.manifest_repo,
            "gh",
            "pr",
            "view",
            url,
            "--repo",
            config["repo"],
            "--web",
        )

    def _publish(self, patch, target, dry_run):
        self._check(patch)
        config = patch["github"][target]
        if config.get("url"):
            self.die(f"{patch['bead']}: {target} PR already exists: {config['url']}")
        existing = self._find_existing(patch, target)
        if existing:
            config.update(
                {
                    "url": existing["url"],
                    "number": existing["number"],
                    "state": (
                        "draft"
                        if existing["isDraft"]
                        else existing["state"].lower()
                    ),
                    "head-commit": existing["headRefOid"],
                }
            )
            self._save()
            self.die(
                f"{patch['bead']}: discovered existing {target} PR: "
                f"{existing['url']}"
            )

        repo, push_commands = self._push_command(patch, target, dry_run)
        _, body = self._draft_content(
            self.manifest_repo / patch["pr-draft"]
        )
        create = self._create_command(patch, target)[:-2]
        if dry_run:
            for command in push_commands:
                self.inf("DRY RUN: " + " ".join(command))
            self.inf("DRY RUN: " + " ".join(create) + " --body-file <generated>")
            return

        for command in push_commands:
            run(repo, *command)
        with tempfile.NamedTemporaryFile("w", suffix=".md") as body_file:
            body_file.write(body)
            body_file.flush()
            url = run(
                repo,
                *create,
                "--body-file",
                body_file.name,
                capture=True,
            )
        config.update({"url": url, "state": "draft"})
        self._save()
        self.inf(f"created {target} draft: {url}")

    def _update_body(self, patch, target, dry_run):
        config = patch["github"][target]
        url = config.get("url")
        if not url:
            self.die(f"{patch['bead']}: no {target} PR")
        _, body = self._draft_content(self.manifest_repo / patch["pr-draft"])
        if dry_run:
            self.inf(f"DRY RUN: gh pr edit {url} --body-file <generated>")
            return
        with tempfile.NamedTemporaryFile("w", suffix=".md") as body_file:
            body_file.write(body)
            body_file.flush()
            run(
                self.manifest_repo,
                "gh",
                "pr",
                "edit",
                url,
                "--repo",
                config["repo"],
                "--body-file",
                body_file.name,
            )

    def _ready(self, patch, target, dry_run):
        config = patch["github"][target]
        url = config.get("url")
        if not url:
            self.die(f"{patch['bead']}: no {target} PR")
        command = ["gh", "pr", "ready", url, "--repo", config["repo"]]
        if dry_run:
            self.inf("DRY RUN: " + " ".join(command))
            return
        run(self.manifest_repo, *command)
        self._sync(patch)
        self._save()

    def _save(self):
        self.profile_path.write_text(
            yaml.safe_dump(self.profile, sort_keys=False, width=1000)
        )

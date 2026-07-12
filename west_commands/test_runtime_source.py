"""Materialize isolated source forests for runtime RED/GREEN proofs.

This module owns the temporary Git worktrees used to build a runtime from a
profile rather than mutating the developer's checkout.  ``DarlingTest`` is the
CLI facade and supplies the workspace/manifest adapter; source selection,
profile patch application and worktree cleanup stay together here.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from source_worktree import SourceWorktreeError, prepare_source_worktree
from test_results import RuntimeRedProven
from test_runtime_evidence import RuntimeEvidenceSession
from test_worktrees import remove_temporary_worktree


class RuntimeSourceMaterializer:
    """Source-forest domain service backed by one ``west test`` workspace.

    The host is deliberately a narrow legacy adapter rather than a generic
    callback collection.  It supplies manifest identity, profile access and
    reporter methods while this class owns every source-tree mutation.
    """

    def __init__(self, host: Any):
        self._host = host

    def red_source_patch_path(self, path: str) -> Path:
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts:
            self._host.die(f"red-proof source-patches path must be workspace-relative: {path}")
        result = Path(self._host.manifest.repo_abspath) / rel
        if not result.is_file():
            self._host.die(f"red-proof source patch not found: {result}")
        return result

    def project_manifest_path(self, ref: str) -> Path:
        for project in self._host.manifest.projects:
            if ref in {project.name, project.path}:
                return Path(project.path)
        path = Path(ref)
        if path.exists():
            workspace_root = Path(self._host.topdir).parent
            try:
                return path.resolve().relative_to(workspace_root)
            except ValueError:
                pass
        self._host.die(f"unknown West project or path: {ref}")

    def apply_profile_module_patches(
        self,
        profile: str,
        module: str,
        target: Path,
        *,
        skip_patch_paths: set[str] | None = None,
    ) -> None:
        skips = skip_patch_paths or set()
        for stacked in self._host._profile_stack(profile):
            data = self._host._load_profile(stacked)
            profile_dir = self._host._profile_path(stacked).parent
            for patch in data.get("patches", []):
                if patch.get("module") != module:
                    continue
                if patch.get("path") in skips:
                    self._host.inf(f"  skip {stacked}/{patch['path']} for current-minus-patch")
                    continue
                patch_file = profile_dir / patch["path"]
                if self.profile_patch_is_already_applied(target, patch_file, patch):
                    self._host.inf(f"  skip {stacked}/{patch['path']} already in {module}")
                    continue
                self._host.inf(f"  apply {stacked}/{patch['path']} -> {module}")
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "gc.auto=0",
                        "-c",
                        "maintenance.auto=false",
                        "am",
                        "--3way",
                        "--committer-date-is-author-date",
                        str(patch_file),
                    ],
                    cwd=target,
                    check=True,
                )

    @staticmethod
    def commit_is_ancestor(repo: Path, commit: str) -> bool:
        if not commit:
            return False
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if exists.returncode:
            return False
        return subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        ).returncode == 0

    @staticmethod
    def commit_has_equivalent_patch(repo: Path, commit: str) -> bool:
        """Return whether *commit*'s patch is already reachable from ``HEAD``."""

        result = subprocess.run(
            [
                "git",
                "log",
                "--cherry-mark",
                "--right-only",
                "--no-merges",
                "--format=%m%x00%H",
                f"HEAD...{commit}",
                "--not",
                f"{commit}^",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            return False
        return any(
            marker == "=" and candidate == commit
            for marker, separator, candidate in (
                line.partition("\0") for line in result.stdout.splitlines()
            )
            if separator
        )

    def profile_patch_is_already_applied(
        self, repo: Path, patch_file: Path, patch: dict
    ) -> bool:
        source_commit = str(patch.get("source-commit", ""))
        if source_commit:
            return self.commit_is_ancestor(repo, source_commit) or self.commit_has_equivalent_patch(
                repo, source_commit
            )
        return (
            subprocess.run(
                ["git", "apply", "--reverse", "--check", str(patch_file)],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            == 0
        )

    @staticmethod
    def _current_minus_skip_patch_paths(patch: dict, proof: dict) -> set[str]:
        return {
            str(patch["path"]),
            *[str(path) for path in proof.get("current-minus-skip-patches", [])],
        }

    def active_runtime_profile(self, patch: dict) -> str:
        profile = getattr(self._host, "_active_profile", None)
        if not profile:
            self._host.die(f"{patch['path']}: current-minus-patch needs an active profile")
        return profile

    def apply_current_minus_profile(
        self, patch: dict, proof: dict, module: str, target: Path
    ) -> None:
        self.apply_profile_module_patches(
            self.active_runtime_profile(patch),
            module,
            target,
            skip_patch_paths=self._current_minus_skip_patch_paths(patch, proof),
        )

    def apply_full_runtime_profile(self, patch: dict, module: str, target: Path) -> None:
        self.apply_profile_module_patches(self.active_runtime_profile(patch), module, target)

    @contextmanager
    def source_base_green_source_tree(self, patch: dict, module: str) -> Iterator[Path | None]:
        """Materialize the fixed/profile source tree for a source-base proof."""

        cache = getattr(self._host, "_source_base_green_cache", None)
        key = (getattr(self._host, "_active_profile", None), module)
        if cache is not None and key in cache:
            yield cache[key]
            return
        if cache is not None:
            tree = getattr(self._host, "_source_base_green_stack").enter_context(
                self.materialize_source_base_green_tree(patch, module)
            )
            cache[key] = tree
            yield tree
            return
        with self.materialize_source_base_green_tree(patch, module) as tree:
            yield tree

    @contextmanager
    def materialize_source_base_green_tree(
        self, patch: dict, module: str
    ) -> Iterator[Path | None]:
        profile = getattr(self._host, "_active_profile", None)
        if not profile or self._host._profile_is_applied(profile):
            yield None
            return

        module_repo = self._host._project_path(module)
        revision = self._host._manifest_revision(module)
        temp = tempfile.mkdtemp(prefix="west-green-proof-source-")
        target = Path(temp) / "source"
        keep_on_failure = False
        try:
            subprocess.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(target), revision],
                cwd=module_repo,
                check=True,
            )
            self.apply_full_runtime_profile(patch, module, target)
            yield target
        except BaseException:
            keep_on_failure = True
            self._host.err(f"preserving failed GREEN source tree for inspection: {temp}")
            raise
        finally:
            if not keep_on_failure:
                error = remove_temporary_worktree(module_repo, target)
                if error:
                    self._host.die(f"failed to remove GREEN source worktree: {error}")
                shutil.rmtree(temp, ignore_errors=True)

    def _apply_red_source_patches(self, proof: dict, module_label: str, target: Path) -> None:
        for source_patch in proof.get("source-patches", []):
            patch_path = self.red_source_patch_path(str(source_patch))
            rel = patch_path.relative_to(self._host.manifest.repo_abspath)
            self._host.inf(f"  apply RED source patch {rel} -> {module_label}")
            subprocess.run(["git", "apply", "--3way", str(patch_path)], cwd=target, check=True)

    def _guest_runtime_source_modules(self, patch: dict, proof: dict) -> set[Path]:
        modules = {self.project_manifest_path(patch["module"])}
        source_modules = proof.get("source-modules", [])
        if not isinstance(source_modules, list):
            self._host.die(
                f"{patch['path']}: red-proof.source-modules must be a list of West project paths"
            )
        for module in source_modules:
            if not isinstance(module, str) or not module:
                self._host.die(
                    f"{patch['path']}: red-proof.source-modules must be a list of West project paths"
                )
            modules.add(self.project_manifest_path(module))
        return modules

    def _guest_runtime_source_revision(
        self,
        patch: dict,
        project_path: Path,
        patch_module_path: Path,
        omit_patch: bool,
        bad_revision: str | None,
    ) -> tuple[str, bool]:
        module = str(project_path)
        if not omit_patch or project_path != patch_module_path:
            return self._host._manifest_revision(module), False
        if bad_revision is None:
            self._host.die(f"{patch['path']}: missing current-minus revision")
        return bad_revision, False

    @contextmanager
    def guest_runtime_source_forest(
        self,
        patch: dict,
        proof: dict,
        *,
        omit_patch: bool,
        root: Path | None = None,
        evidence_session: RuntimeEvidenceSession | None = None,
    ) -> Iterator[Path]:
        """Create a coherent, disposable Darling source forest for one runtime build."""

        projects_by_path = {
            Path(project.path): Path(project.abspath)
            for project in self._host.manifest.projects
            if project.name != "manifest"
        }
        darling_repo = projects_by_path.get(Path("darling"))
        if darling_repo is None:
            self._host.die("guest-runtime-deploy needs a West project at path 'darling'")
        patch_module_path = self.project_manifest_path(patch["module"])
        materialized_modules = self._guest_runtime_source_modules(patch, proof)
        patch_module_is_darling_root = patch_module_path == Path("darling")
        current_minus_patch = proof.get("bad-profile") == "current-minus-patch"
        if omit_patch and not current_minus_patch:
            self._host.die(f"{patch['path']}: only current-minus-patch runtime proofs are supported")
        bad_revision = self._host._bad_revision(patch) if omit_patch else None
        added: list[tuple[Path, Path]] = []
        owns_root = root is None
        temp = Path(tempfile.mkdtemp(prefix="west-red-proof-source-")) if owns_root else root
        temp.mkdir(parents=True, exist_ok=True)
        yielded = False
        keep_on_failure = False
        source_started = time.monotonic()
        try:
            source_root = temp / "darling"
            darling_ref = bad_revision if omit_patch and patch_module_is_darling_root else self._host._manifest_revision("darling")
            bad_text = "current-minus-patch" if omit_patch else "profile-current"
            self._host.inf(f"  runtime source forest: {patch_module_path}={bad_text} under {source_root}")
            subprocess.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(source_root), darling_ref],
                cwd=darling_repo,
                check=True,
            )
            added.append((darling_repo, source_root))

            def nested_revision(relative_path: Path, tree_revision: str) -> str:
                project_path = Path("darling") / relative_path
                if project_path not in materialized_modules:
                    return tree_revision
                revision, _uses_profile_source_commit = self._guest_runtime_source_revision(
                    patch, project_path, patch_module_path, omit_patch, bad_revision
                )
                return revision

            try:
                nested_entries = prepare_source_worktree(
                    source_root, darling_repo, revision_for=nested_revision
                )
            except SourceWorktreeError as error:
                self._host.die(f"{patch['path']}: cannot hydrate runtime source forest: {error}")
            added.extend(
                (Path(entry.canonical_repo), source_root / entry.relative_path)
                for entry in nested_entries
                if entry.created
            )
            self._host.inf(
                f"  runtime phase complete: source hydration "
                f"({len(nested_entries)} gitlink(s), {time.monotonic() - source_started:.1f}s)"
            )
            profile_started = time.monotonic()
            self._host.inf("  runtime phase start: profile materialization")
            if patch_module_is_darling_root or Path("darling") in materialized_modules:
                if omit_patch:
                    self.apply_current_minus_profile(patch, proof, "darling", source_root)
                else:
                    self.apply_full_runtime_profile(patch, "darling", source_root)
                self._apply_red_source_patches(proof, "darling", source_root)
            for project_path, _repo in sorted(
                projects_by_path.items(), key=lambda item: (len(item[0].parts), str(item[0]))
            ):
                if project_path == Path("darling"):
                    continue
                try:
                    rel = project_path.relative_to("darling")
                except ValueError:
                    continue
                target = source_root / rel
                if project_path not in materialized_modules:
                    continue
                module_text = str(project_path)
                _revision, uses_profile_source_commit = self._guest_runtime_source_revision(
                    patch, project_path, patch_module_path, omit_patch, bad_revision
                )
                if not target.is_dir() or target.is_symlink():
                    self._host.die(
                        f"{patch['path']}: hydrated runtime source is missing nested module {project_path}"
                    )
                if not uses_profile_source_commit:
                    if omit_patch:
                        self.apply_current_minus_profile(patch, proof, module_text, target)
                    else:
                        self.apply_full_runtime_profile(patch, module_text, target)
                if omit_patch:
                    self._apply_red_source_patches(proof, module_text, target)
            self._host.inf(
                f"  runtime phase complete: profile materialization "
                f"({time.monotonic() - profile_started:.1f}s)"
            )
            yielded = True
            yield source_root
        except RuntimeRedProven:
            raise
        except BaseException:
            keep_on_failure = True
            if evidence_session is not None:
                evidence_session.record_worktrees(added)
            elif owns_root:
                suffix = " before yield" if not yielded else ""
                self._host.err(f"preserving failed runtime source forest{suffix} for inspection: {temp}")
            raise
        finally:
            if evidence_session is not None and evidence_session.retention_requested:
                keep_on_failure = True
                evidence_session.record_worktrees(added)
            if not keep_on_failure:
                for repo, target in reversed(added):
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(target)],
                        cwd=repo,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                if owns_root:
                    shutil.rmtree(temp, ignore_errors=True)

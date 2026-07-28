"""Materialize isolated source forests for runtime RED/GREEN proofs.

This module owns the temporary Git worktrees used to build a runtime from a
profile rather than mutating the developer's checkout.  ``DarlingTest`` is the
CLI facade and supplies the workspace/manifest adapter; source selection,
profile patch application and worktree cleanup stay together here.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from source_worktree import SourceWorktreeError, prepare_source_worktree
from patch_git import TEMPORARY_PATCH_GIT_OPTIONS
import patch_stack_lock_first
import patch_stack_materialize
import patch_stack_profile_composition
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

    def _materialize_canonical_profile(self, profile: str, overrides: dict[str, Path]) -> None:
        """Replay one approved typed profile into lifecycle-owned worktrees.

        This intentionally does not use a patch archive or publish integration
        refs/generated locks.  The worktree context owns all resulting commits
        and removes them when its caller exits.
        """
        if profile not in {"homebrew", "perf", "arch"}:
            raise patch_stack_lock_first.LockFirstError(
                f"runtime-source canonical materialization is not enabled for {profile}"
            )
        def typed_plan(name: str) -> patch_stack_lock_first.LockFirstPlan:
            grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
            patches = self._host._load_profile(name).get("patches", [])
            for patch in patches:
                grouped.setdefault(patch["module"], []).append(patch)
            plan = patch_stack_lock_first.plan(name, patches, None, grouped)
            if plan.composition is None:
                raise patch_stack_lock_first.LockFirstError(
                    f"runtime-source {name} requires a typed profile-composition lock"
                )
            return plan

        plan = typed_plan(profile)
        batch = plan.batch
        expected = {
            "homebrew": (
                "darling-homebrew-lock-first-batch-7", 69,
                [
                    "darling/src/external/darlingserver", "darling/src/external/xnu",
                    "darling/src/external/libplatform", "darling/src/external/perl",
                    "darling/src/external/libressl-2.8.3", "darling/src/external/libpthread",
                    "darling", "darling/src/external/installer",
                ],
            ),
            "arch": (
                "darling-arch-lock-first-batch-1", 19,
                [
                    "darling/src/external/libunwind", "darling/src/external/xnu",
                    "darling/src/external/darlingserver", "darling",
                ],
            ),
            "perf": (
                "darling-perf-lock-first-batch-1", 7,
                [
                    "darling", "darling/src/external/xnu",
                    "darling/src/external/dyld", "darling/src/external/darlingserver",
                ],
            ),
        }[profile]
        if (batch["batch_id"], batch["expected_count"], batch["module_order"]) != expected:
            raise patch_stack_lock_first.LockFirstError(
                f"runtime-source {profile} requires exact {expected[0]} "
                f"({expected[1]} series, {len(expected[2])} modules in typed order)"
            )
        plans: list[tuple[str, patch_stack_lock_first.LockFirstPlan]] = []
        visiting: set[str] = set()

        def append_prerequisites(name: str) -> None:
            if name in visiting:
                raise patch_stack_lock_first.LockFirstError("runtime-source profile prerequisites are cyclic")
            candidate = typed_plan(name)
            visiting.add(name)
            for prerequisite in candidate.composition["prerequisites"]:
                if not isinstance(prerequisite, dict) or not isinstance(prerequisite.get("profile"), str):
                    raise patch_stack_lock_first.LockFirstError(
                        "runtime-source profile prerequisite is not typed"
                    )
                append_prerequisites(prerequisite["profile"])
            visiting.remove(name)
            if name not in [known for known, _ in plans]:
                plans.append((name, candidate))

        append_prerequisites(profile)
        mode_marker = "PATCH_STACK_MODE=default-lock-first materializer=runtime-source"
        if profile == "arch":
            mode_marker += " profile=arch"
        self._host.inf(mode_marker)
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        initialized: set[str] = set()
        # Do not reset an overlapping parent before its immutable inputs are
        # fetched. A fresh West clone contains only the manifest revision;
        # the typed base can be absent. ``materialize_batch_into()`` fetches,
        # validates and then performs this reset atomically for the parent
        # module itself, before native replay.
        for phase, phase_plan in plans:
            for module in phase_plan.batch["module_order"]:
                target = overrides.get(module)
                if target is None:
                    raise patch_stack_lock_first.LockFirstError(
                        f"runtime-source canonical target missing for {module}"
                    )
                module_entries = [entry for entry in phase_plan if entry["module"] == module]
                module_results, _stats = patch_stack_lock_first.materialize_batch_into(
                    target, module_entries,
                    git_options=TEMPORARY_PATCH_GIT_OPTIONS,
                    # The first replay establishes the earliest declared
                    # immutable base.  Each typed prerequisite then carries
                    # its validated profile boundary into the next phase.
                    reset_to_first_base=module not in initialized,
                    composition=phase_plan.composition,
                )
                initialized.add(module)
                if phase == profile:
                    results.extend(module_results)
            darling = overrides.get("darling")
            nested = [
                str(Path(module).relative_to("darling"))
                for module in phase_plan.batch["module_order"]
                if module != "darling" and module.startswith("darling/")
            ]
            if darling is not None and nested:
                subprocess.run(["git", "add", "--", *nested], cwd=darling, check=True)
                commit_env = os.environ.copy()
                commit_env.update({
                    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+0000",
                    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+0000",
                })
                subprocess.run(
                    ["git", *TEMPORARY_PATCH_GIT_OPTIONS, "commit", "-m",
                     f"Runtime-source {phase} profile boundary"],
                    cwd=darling, check=True, env=commit_env,
                )
            expected = phase_plan.composition["integration_finals"]
            for module, expected_tree in expected.items():
                target = overrides.get(module)
                if target is None:
                    raise patch_stack_lock_first.LockFirstError(
                        f"runtime-source canonical target missing for {module}"
                    )
                try:
                    patch_stack_profile_composition.verify_integration(
                        module, target, expected_tree, expected, overrides
                    )
                except patch_stack_profile_composition.ProfileCompositionError as error:
                    raise patch_stack_lock_first.LockFirstError(
                        f"runtime-source {phase} {error}"
                    ) from error
        if len(results) != batch["expected_count"]:
            raise patch_stack_lock_first.LockFirstError(
                "runtime-source canonical applied series count differs from typed batch"
            )
        self._host.inf(
            "PATCH_STACK_REPLAY "
            f"batch={batch['batch_id']} expected={batch['expected_count']} "
            f"applied={len(results)} modules={len(batch['module_order'])} "
            f"elapsed_seconds={time.monotonic() - started:.3f} verdict=VALID"
        )

    @contextmanager
    def profile_worktree_checkout(self, profile: str) -> Iterator[None]:
        projects = self._host._projects()
        required_modules: set[str] = set()
        for stacked in self._host._profile_stack(profile):
            required_modules.update(patch["module"] for patch in self._host._load_profile(stacked).get("patches", []))
        modules = sorted(
            required_modules,
            key=lambda module: (len(Path(module).parts), module),
        )
        repos = [(module, projects[module]) for module in modules]
        previous_overrides = getattr(self._host, "_project_overrides", {})
        added: list[tuple[Path, Path]] = []
        with tempfile.TemporaryDirectory(prefix=f"west-profile-{profile}-") as temp:
            root = Path(temp)
            overrides = dict(previous_overrides)
            primary_error: BaseException | None = None
            try:
                for module, repo in repos:
                    target = root / module
                    if target.exists() or target.is_symlink():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    revision = self._host._manifest_revision(module)
                    self._host.inf(f"  materialize {module}: {revision} -> {target}")
                    subprocess.run(
                        ["git", "worktree", "add", "--quiet", "--detach", str(target), revision],
                        cwd=repo,
                        check=True,
                    )
                    added.append((repo, target))
                    for ref, project_path in projects.items():
                        if project_path == repo:
                            overrides[ref] = target
                    overrides[module] = target
                self._host._project_overrides = overrides
                self._materialize_canonical_profile(profile, overrides)
                yield
            except BaseException as error:
                # A cleanup fault must never disguise the canonical replay
                # failure (including SIGINT) that made this lifecycle abort.
                primary_error = error
                raise
            finally:
                self._host._project_overrides = previous_overrides
                previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    errors = []
                    for repo, target in reversed(added):
                        error = remove_temporary_worktree(repo, target)
                        if error:
                            errors.append(error)
                finally:
                    signal.signal(signal.SIGINT, previous_sigint)
                if errors:
                    message = (f"{profile}: failed to remove temporary profile worktree(s): "
                               f"{'; '.join(errors)}")
                    if primary_error is None:
                        self._host.die(message)
                    else:
                        self._host.inf(message)

    @contextmanager
    def bad_source_tree(self, module: str, revision: str) -> Iterator[Path]:
        repo = self._host._project_path(module)
        with tempfile.TemporaryDirectory(prefix="west-red-proof-") as temp:
            worktree = Path(temp) / "source-base"
            subprocess.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(worktree), revision],
                cwd=repo,
                check=True,
            )
            try:
                yield worktree
            finally:
                error = remove_temporary_worktree(repo, worktree)
                if error:
                    self._host.die(f"failed to remove RED source worktree: {error}")

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
        """Materialize an intact profile module from immutable typed locks.

        Current-minus RED proofs deliberately request a non-canonical partial
        series, but still derive every applied change from immutable schema-v2
        lock objects. Historical archives are never executable inputs.
        """
        skips = skip_patch_paths or set()
        if profile not in {"homebrew", "perf", "arch"}:
            raise patch_stack_lock_first.LockFirstError(
                f"runtime-source canonical materialization is not enabled for {profile}"
            )
        observed_skips: set[str] = set()
        initialized = False
        for stacked in self._host._profile_stack(profile):
            profile_patches = self._host._load_profile(stacked).get("patches", [])
            grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
            for patch in profile_patches:
                grouped.setdefault(patch["module"], []).append(patch)
            plan = patch_stack_lock_first.plan(stacked, profile_patches, None, grouped)
            entries = [entry for entry in plan if entry["module"] == module]
            if not entries:
                continue
            phase_skips = {
                entry["patch"] for entry in entries if entry["patch"] in skips
            }
            observed_skips.update(phase_skips)
            for patch in sorted(phase_skips):
                self._host.inf(
                    f"  skip {stacked}/{patch} for canonical current-minus-patch"
                )
            patch_stack_lock_first.materialize_batch_into(
                target,
                entries,
                git_options=TEMPORARY_PATCH_GIT_OPTIONS,
                reset_to_first_base=not initialized,
                composition=plan.composition,
                skip_patches=phase_skips,
            )
            initialized = True
        if observed_skips != skips:
            raise patch_stack_lock_first.LockFirstError(
                "runtime-source current-minus skips differ from typed mappings"
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
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            key = f"{head}:{source_commit}"
            cached = self._patch_identity_cache_get(key)
            if cached is not None:
                self._host.inf(f"  patch identity cache hit: {source_commit[:12]}")
                return cached
            result = self.commit_is_ancestor(
                repo, source_commit
            ) or self.commit_has_equivalent_patch(repo, source_commit)
            self._patch_identity_cache_put(key, result)
            return result
        self._host.die(
            f"{patch.get('path', patch_file)}: immutable source-commit is required"
        )

    def _patch_identity_cache_path(self) -> Path:
        return Path(self._host.manifest.repo_abspath) / ".west-test/cache/patch-identity-v1.json"

    def _patch_identity_cache_get(self, key: str) -> bool | None:
        cache_path = self._patch_identity_cache_path()
        try:
            payload = json.loads(cache_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        value = payload.get("entries", {}).get(key)
        return value if isinstance(value, bool) else None

    def _patch_identity_cache_put(self, key: str, value: bool) -> None:
        cache_path = self._patch_identity_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = cache_path.with_suffix(".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    payload = json.loads(cache_path.read_text())
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    payload = {"schema": 1, "entries": {}}
                entries = payload.setdefault("entries", {})
                entries[key] = value
                temporary = cache_path.with_suffix(f".tmp-{os.getpid()}")
                temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
                os.replace(temporary, cache_path)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

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
        darling_repo = darling_repo.resolve()
        patch_module_path = self.project_manifest_path(patch["module"])
        materialized_modules = self._guest_runtime_source_modules(patch, proof)
        patch_module_is_darling_root = patch_module_path == Path("darling")
        current_minus_patch = proof.get("bad-profile") == "current-minus-patch"
        if omit_patch and not current_minus_patch:
            self._host.die(f"{patch['path']}: only current-minus-patch runtime proofs are supported")
        bad_revision = self._host._bad_revision(patch, proof) if omit_patch else None
        added: list[tuple[Path, Path]] = []
        owns_root = root is None
        temp = (
            Path(tempfile.mkdtemp(prefix="west-red-proof-source-")).resolve()
            if owns_root
            else Path(root).expanduser().resolve()
        )
        temp.mkdir(parents=True, exist_ok=True)
        yielded = False
        keep_on_failure = False
        source_started = time.monotonic()
        try:
            source_root = (temp / "darling").resolve()
            darling_ref = bad_revision if omit_patch and patch_module_is_darling_root else self._host._manifest_revision("darling")
            bad_text = "current-minus-patch" if omit_patch else "profile-current"
            self._host.inf(f"  runtime source forest: {patch_module_path}={bad_text} under {source_root}")
            subprocess.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(source_root), darling_ref],
                cwd=darling_repo,
                check=True,
            )
            registered_root = subprocess.run(
                ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=False,
            )
            if (
                registered_root.returncode
                or Path(registered_root.stdout.strip()).resolve() != source_root
            ):
                cleanup_error = remove_temporary_worktree(darling_repo, source_root)
                detail = registered_root.stderr.strip() or registered_root.stdout.strip()
                if cleanup_error:
                    detail = f"{detail}; cleanup: {cleanup_error}".strip("; ")
                self._host.die(
                    f"{patch['path']}: Git registered runtime source at an unexpected path; "
                    f"expected {source_root}, observed {detail or 'unknown'}"
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

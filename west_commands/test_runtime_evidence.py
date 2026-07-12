"""Durable evidence units for failed runtime materialization and execution."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class RuntimeEvidenceSession:
    """One in-flight runtime source/build unit that may be retained on failure."""

    def __init__(self, root: Path, label: str, context: dict[str, Any]):
        self._root = root
        self._label = label
        self._context = context
        self._directory = Path(tempfile.mkdtemp(prefix=".inflight-", dir=root))
        self._retained = False
        self._worktrees: list[dict[str, str]] = []
        self._diagnostics: list[dict[str, Any]] = []
        self._requested_failure: BaseException | None = None

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def source_root(self) -> Path:
        return self._directory / "source"

    @property
    def build_root(self) -> Path:
        return self._directory / "build"

    def record_worktrees(self, worktrees: list[tuple[Path, Path]]) -> None:
        """Record worktree ownership so explicit evidence GC can remove it safely."""

        records = []
        for repo, target in worktrees:
            try:
                relative_target = target.relative_to(self._directory)
            except ValueError as error:
                raise ValueError(f"evidence worktree escapes its unit: {target}") from error
            records.append({"repo": str(repo), "path": str(relative_target)})
        self._worktrees = records

    @property
    def retention_requested(self) -> bool:
        return self._requested_failure is not None

    @property
    def requested_failure(self) -> BaseException | None:
        return self._requested_failure

    def preserve(self, failure: BaseException) -> None:
        """Request retention when a runner reports failure through a return code."""

        if self._requested_failure is None:
            self._requested_failure = failure

    def record_failure_detail(
        self,
        *,
        phase: str,
        summary: str,
        returncode: int | None = None,
        command: list[str] | None = None,
        output: str | None = None,
        artifacts: list[Path] | None = None,
    ) -> None:
        """Attach a bounded domain failure record before cleanup removes it."""

        if not phase:
            raise ValueError("runtime evidence diagnostic phase must be non-empty")
        if not summary:
            raise ValueError("runtime evidence diagnostic summary must be non-empty")
        index = len(self._diagnostics)
        diagnostics_root = self._directory / "diagnostics"
        entry: dict[str, Any] = {"phase": phase, "summary": summary}
        if returncode is not None:
            entry["returncode"] = returncode
        if command:
            entry["command"] = list(command)
        if output:
            output_path = diagnostics_root / f"{index}-output.log"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output[-64 * 1024 :])
            entry["output"] = str(output_path.relative_to(self._directory))
        copied_artifacts: list[str] = []
        copied_bytes = 0
        artifact_budget = 64 * 1024 * 1024
        for artifact in artifacts or []:
            if not artifact.is_file() or artifact.is_symlink():
                continue
            artifact_size = artifact.stat().st_size
            if artifact_size > artifact_budget - copied_bytes:
                continue
            artifact_path = diagnostics_root / f"{index}-{artifact.name}"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, artifact_path)
            copied_artifacts.append(str(artifact_path.relative_to(self._directory)))
            copied_bytes += artifact_size
        if copied_artifacts:
            entry["artifacts"] = copied_artifacts
        self._diagnostics.append(entry)

    def retain(self, failure: BaseException) -> Path:
        """Finalize this in-flight unit as inspectable failure evidence."""

        if self._retained:
            return self._directory
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = self._root / f"runtime-evidence-{stamp}-{uuid.uuid4().hex[:12]}"
        if self._worktrees:
            target.mkdir()
            self._relocate_worktrees(target)
            for child in self._directory.iterdir():
                destination = target / child.name
                if destination.exists() and child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
                    continue
                child.rename(destination)
            self._directory.rmdir()
        else:
            self._directory.rename(target)
        self._directory = target
        manifest = {
            "schema": 1,
            "status": "failed",
            "label": self._label,
            "created-at": stamp,
            "context": self._context,
            "failure": {"type": type(failure).__name__, "message": str(failure)},
            "paths": {"source": "source/darling", "build": "build"},
            "worktrees": self._worktrees,
        }
        if self._diagnostics:
            manifest["diagnostics"] = self._diagnostics
        manifest_path = target / "manifest.json"
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        temporary.replace(manifest_path)
        self._retained = True
        return target

    def _relocate_worktrees(self, target: Path) -> None:
        """Move registered worktrees before moving the enclosing evidence unit."""

        for worktree in reversed(self._worktrees):
            repo = Path(worktree["repo"])
            relative_path = Path(worktree["path"])
            source = self._directory / relative_path
            destination = target / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "worktree", "move", str(source), str(destination)],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(
                    f"could not retain runtime evidence worktree {source}: {detail}"
                )

    def discard(self) -> None:
        if not self._retained:
            shutil.rmtree(self._directory, ignore_errors=True)


class RuntimeEvidenceStore:
    """Own only declared runtime evidence, separate from disposable scratch."""

    def __init__(self, root: Path):
        self._root = root.expanduser()

    @property
    def root(self) -> Path:
        return self._root

    def start(self, label: str, context: dict[str, Any]) -> RuntimeEvidenceSession:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return RuntimeEvidenceSession(self._root, label, context)

    @staticmethod
    def finish(session: RuntimeEvidenceSession, failure: BaseException | None = None) -> Path | None:
        if failure is not None:
            return session.retain(failure)
        if session.retention_requested:
            assert session.requested_failure is not None
            return session.retain(session.requested_failure)
        session.discard()
        return None

    @contextmanager
    def session(self, label: str, context: dict[str, Any]) -> Iterator[RuntimeEvidenceSession]:
        session = self.start(label, context)
        try:
            yield session
        except BaseException as failure:
            self.finish(session, failure)
            raise
        else:
            self.finish(session)

    def entries(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        entries = []
        for entry in self._root.iterdir():
            manifest = entry / "manifest.json"
            if (
                entry.is_dir()
                and not entry.is_symlink()
                and entry.name.startswith("runtime-evidence-")
                and manifest.is_file()
                and not manifest.is_symlink()
            ):
                entries.append(entry)
        return sorted(entries, key=lambda entry: entry.stat().st_mtime, reverse=True)

    def resolve(self, identifier: str) -> Path:
        """Resolve an exact unit name or an unambiguous trailing identifier."""

        if not identifier or "/" in identifier or identifier in {".", ".."}:
            raise ValueError(f"invalid runtime evidence identifier: {identifier!r}")
        matches = [
            entry
            for entry in self.entries()
            if entry.name == identifier or entry.name.endswith(f"-{identifier}")
        ]
        if not matches:
            raise ValueError(f"runtime evidence not found: {identifier}")
        if len(matches) != 1:
            raise ValueError(f"runtime evidence identifier is ambiguous: {identifier}")
        return matches[0]

    @staticmethod
    def manifest(entry: Path) -> dict[str, Any]:
        manifest = json.loads((entry / "manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise ValueError(f"unsupported runtime evidence manifest: {entry}")
        return manifest

    def replay_report(self, identifier: str) -> dict[str, Any]:
        """Validate retained references and return a portable diagnostic report."""

        entry = self.resolve(identifier)
        manifest = self.manifest(entry)
        referenced = []
        for diagnostic in manifest.get("diagnostics", []):
            if not isinstance(diagnostic, dict):
                raise ValueError(f"invalid runtime evidence diagnostic: {entry}")
            for key in ("output",):
                if key in diagnostic:
                    referenced.append(diagnostic[key])
            referenced.extend(diagnostic.get("artifacts", []))
        checked = []
        for relative_text in referenced:
            relative = Path(str(relative_text))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe runtime evidence attachment: {relative_text}")
            attachment = entry / relative
            if not attachment.is_file() or attachment.is_symlink():
                raise ValueError(f"missing runtime evidence attachment: {relative_text}")
            checked.append({"path": str(relative), "bytes": attachment.stat().st_size})
        return {
            "unit": entry.name,
            "label": manifest.get("label"),
            "context": manifest.get("context", {}),
            "failure": manifest.get("failure", {}),
            "diagnostics": manifest.get("diagnostics", []),
            "attachments": checked,
        }

    def gc(self, *, max_age_hours: float, keep_last: int, dry_run: bool) -> list[Path]:
        if max_age_hours < 0:
            raise ValueError("runtime evidence max age must be >= 0")
        if keep_last < 0:
            raise ValueError("runtime evidence keep-last must be >= 0")
        cutoff = time.time() - max_age_hours * 3600
        selected = []
        for index, entry in enumerate(self.entries()):
            stale = entry.stat().st_mtime <= cutoff
            if index >= keep_last or stale:
                selected.append(entry)
        if not dry_run:
            for entry in selected:
                self._remove_worktrees(entry)
                shutil.rmtree(entry)
        return selected

    @staticmethod
    def _remove_worktrees(entry: Path) -> None:
        manifest = json.loads((entry / "manifest.json").read_text())
        worktrees = manifest.get("worktrees", [])
        if not isinstance(worktrees, list):
            raise ValueError(f"invalid runtime evidence worktree manifest: {entry}")
        for worktree in reversed(worktrees):
            if not isinstance(worktree, dict):
                raise ValueError(f"invalid runtime evidence worktree entry: {entry}")
            repo = Path(str(worktree.get("repo", "")))
            relative_path = Path(str(worktree.get("path", "")))
            if not repo.is_dir() or relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError(f"unsafe runtime evidence worktree entry: {entry}")
            target = entry / relative_path
            listing = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if listing.returncode:
                detail = (listing.stderr or listing.stdout).strip()
                raise RuntimeError(
                    f"could not inspect runtime evidence worktrees for {repo}: {detail}"
                )
            registered = {
                Path(line.removeprefix("worktree ")).resolve()
                for line in listing.stdout.splitlines()
                if line.startswith("worktree ")
            }
            if target.resolve() not in registered:
                continue
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(target)],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(
                    f"could not remove runtime evidence worktree {target}: {detail}"
                )

"""Canonical review/recovery mbox export from immutable schema-v2 locks."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import patch_stack_materialize
from patch_stack_lock_first import LockFirstError, LockFirstPlan


class ExportError(RuntimeError):
    pass


OID = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise ExportError(
            f"{repo}: git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _assert_clean_odb(repo: Path) -> None:
    if _git(repo, "rev-parse", "--is-shallow-repository") != "false":
        raise ExportError(f"{repo}: shallow repository")
    partial = subprocess.run(
        ["git", "config", "--get", "extensions.partialClone"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if partial.returncode not in (0, 1):
        raise ExportError(f"{repo}: partial-clone query failed: {partial.stderr.strip()}")
    if partial.returncode == 0 and partial.stdout.strip():
        raise ExportError(f"{repo}: partial clone")
    alternates = Path(_git(repo, "rev-parse", "--git-path", "objects/info/alternates"))
    if not alternates.is_absolute():
        alternates = repo / alternates
    if alternates.exists():
        raise ExportError(f"{repo}: alternates are forbidden")
    if _git(repo, "for-each-ref", "--format=%(refname)", "refs/replace/"):
        raise ExportError(f"{repo}: replace refs are forbidden")


def _mbox(repo: Path, commits: list[str]) -> tuple[bytes, list[str]]:
    chunks: list[bytes] = []
    patch_ids: list[str] = []
    for commit in commits:
        generated = subprocess.run(
            [
                "git",
                "format-patch",
                "--stdout",
                "--no-stat",
                "--full-index",
                f"{commit}^!",
            ],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if generated.returncode:
            raise ExportError(
                f"git format-patch {commit} failed ({generated.returncode}): "
                f"{generated.stderr.decode(errors='replace').strip()}"
            )
        identity = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=generated.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        fields = identity.stdout.decode().split()
        if (
            identity.returncode
            or len(fields) < 2
            or not OID.fullmatch(fields[0])
            or fields[1] != commit
        ):
            raise ExportError(
                f"git patch-id {commit} failed ({identity.returncode}): "
                f"{identity.stderr.decode(errors='replace').strip()}"
            )
        chunks.append(generated.stdout)
        patch_ids.append(fields[0])
    return b"".join(chunks), patch_ids


def _safe_relative_mbox(module: str, patch: str) -> Path:
    module_path = Path(module)
    patch_path = Path(patch)
    if (
        module_path.is_absolute()
        or patch_path.is_absolute()
        or ".." in module_path.parts
        or ".." in patch_path.parts
        or not module_path.parts
        or not patch_path.parts
    ):
        raise ExportError(f"invalid module+patch identity path: {module}/{patch}")
    return module_path / patch_path.with_suffix(".mbox")


def export_profile(profile: str, plan: LockFirstPlan, output: Path) -> dict[str, Any]:
    """Export one typed profile atomically, leaving no Git object payload."""
    if output.exists() or output.is_symlink():
        raise ExportError("canonical export output already exists")
    if not isinstance(plan, LockFirstPlan):
        raise ExportError("canonical export requires typed lock-first plan")
    expected = [
        {"module": entry["module"], "patch": entry["patch"]} for entry in plan
    ]
    if (
        len(expected) != plan.batch.get("expected_count")
        or expected != plan.batch.get("series_order")
        or list(dict.fromkeys(item["module"] for item in expected))
        != plan.batch.get("module_order")
        or len({(item["module"], item["patch"]) for item in expected})
        != len(expected)
    ):
        raise ExportError("canonical export plan order/count/identity is invalid")
    transaction = uuid.uuid4().hex
    scratch = Path(tempfile.gettempdir()) / f"west-patch-lock-export-{transaction}"
    staged = output.with_name(output.name + f".{transaction}.tmp")
    published = False
    try:
        scratch.mkdir()
        staged.mkdir(parents=True)
        rows: list[dict[str, Any]] = []
        entries_by_module: dict[str, list[dict[str, str]]] = {}
        for entry in plan:
            entries_by_module.setdefault(entry["module"], []).append(entry)
        fetch_count = 0
        for module_index, module in enumerate(plan.batch["module_order"]):
            entries = entries_by_module[module]
            repo = scratch / str(module_index)
            repo.mkdir()
            _git(repo, "init", "-q")
            locks: list[tuple[dict[str, str], dict[str, Any]]] = []
            mirrors: set[str] = set()
            for entry in entries:
                try:
                    lock = patch_stack_materialize.load_lock(Path(entry["lock_path"]))
                except (
                    OSError,
                    ValueError,
                    patch_stack_materialize.MaterializeError,
                ) as error:
                    raise ExportError(f"{entry['patch']}: invalid immutable lock: {error}") from error
                locks.append((entry, lock))
                mirrors.add(lock["mirror"]["url"])
            if len(mirrors) != 1:
                raise ExportError(f"{module}: immutable locks use multiple mirrors")
            _git(repo, "remote", "add", "immutable", next(iter(mirrors)))
            refs: list[tuple[str, str]] = []
            specs: list[str] = []
            for index, (_entry, lock) in enumerate(locks):
                base_ref = f"refs/export/{transaction}/{index}/base"
                source_ref = f"refs/export/{transaction}/{index}/source"
                refs.append((base_ref, source_ref))
                specs.extend(
                    [
                        f"{lock['mirror']['base_ref']}:{base_ref}",
                        f"{lock['mirror']['source_ref']}:{source_ref}",
                    ]
                )
            _git(repo, "fetch", "--no-tags", "immutable", *specs)
            fetch_count += 1
            _assert_clean_odb(repo)
            for (entry, lock), (base_ref, source_ref) in zip(
                locks, refs, strict=True
            ):
                try:
                    proof = patch_stack_materialize.validate_fetched_lock(
                        repo, lock, base_ref, source_ref
                    )
                except patch_stack_materialize.MaterializeError as error:
                    raise ExportError(f"{entry['patch']}: {error}") from error
                content, patch_ids = _mbox(repo, proof["ordered_commits"])
                relative = _safe_relative_mbox(module, entry["patch"])
                destination = staged / "mbox" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                rows.append(
                    {
                        "module": module,
                        "patch": entry["patch"],
                        "lock": Path(entry["lock_path"]).name,
                        "base": proof["base_oid"],
                        "source": proof["source_oid"],
                        "ordered_commits": proof["ordered_commits"],
                        "commit_count": len(proof["ordered_commits"]),
                        "resulting_tree": proof["resulting_tree"],
                        "mbox": str(Path("mbox") / relative),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "stable_patch_ids": patch_ids,
                    }
                )
            _git(repo, "fsck", "--no-dangling")
        observed = [
            {"module": entry["module"], "patch": entry["patch"]} for entry in rows
        ]
        if observed != expected or len(rows) != plan.batch["expected_count"]:
            raise ExportError("canonical export order/count differs from typed batch")
        evidence = {
            "export_schema_version": 1,
            "mode": "immutable-lock-format-patch",
            "profile": profile,
            "batch_id": plan.batch["batch_id"],
            "expected_count": plan.batch["expected_count"],
            "module_order": plan.batch["module_order"],
            "series_order": expected,
            "series": rows,
            "clean_odb": {
                "module_count": len(entries_by_module),
                "immutable_fetch_transactions": fetch_count,
                "alternates": 0,
                "shallow": 0,
                "partial": 0,
            },
            "verdict": "VALID",
        }
        (staged / "evidence.json").write_text(
            json.dumps(evidence, sort_keys=True, indent=2) + "\n"
        )
        # The object-bearing transaction must be gone before the
        # provenance-only export is made visible.
        shutil.rmtree(scratch)
        if output.exists() or output.is_symlink():
            raise ExportError("canonical export output appeared during transaction")
        staged.replace(output)
        published = True
        return evidence
    except (LockFirstError, OSError) as error:
        raise ExportError(str(error)) from error
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
        if staged.exists() and not published:
            shutil.rmtree(staged)

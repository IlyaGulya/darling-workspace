#!/usr/bin/env python3
"""Validate and compare two hosted Phase 3B Mach-O batch artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

WEST_COMMANDS = Path(__file__).resolve().parents[1] / "west_commands"
if str(WEST_COMMANDS) not in sys.path:
    sys.path.insert(0, str(WEST_COMMANDS))

from guest_macho_batch_specs import (  # noqa: E402
    ANCHOR_ARTIFACT_SHA256,
    ANCHOR_FIXTURE,
    BATCH_BOOTSTRAP_PROFILE,
    FIXTURE_SPECS,
    validate_specs,
)
from test_guest_macho import GuestMachoFixtureError, _macho_header  # noqa: E402


RUN_NAMES = ("macho-corpus-batch-a", "macho-corpus-batch-b")
ROOT_FILES = ("batch-manifest.tsv", "batch-state.tsv", "failure-summary.txt", "fixtures")
FIXTURE_FILES = (
    "artifact.sha256",
    "clang-origin.txt",
    "clang-version.txt",
    "clt-provenance.txt",
    "compile-status.tsv",
    "compile.log",
    "dylibs-used.txt",
    "file.txt",
    "macho-manifest.json",
    "macho-summary.tsv",
    "private-header.txt",
    "provenance.tsv",
    "provenance.txt",
    "runtime-evidence.tsv",
    "runtime.log",
)
REQUIRED_FIELDS = (
    "schema",
    "fixture",
    "source-project",
    "source-path",
    "source-revision",
    "source-sha256",
    "patch-path",
    "patch-sha256",
    "compile-flags",
    "link-flags",
    "runtime-profile",
    "bootstrap-profile",
    "compiler-path",
    "compiler-sha256",
    "compiler-version",
    "compiler-origin",
    "clt-product-id",
    "clt-provenance-sha256",
    "provenance-document-sha256",
    "artifact-sha256",
    "compile-status-sha256",
    "compile-log-sha256",
    "runtime-evidence-sha256",
    "runtime-log-sha256",
    "private-header-sha256",
    "dylibs-report-sha256",
    "macho-summary-sha256",
    "expected-returncode",
    "expected-marker",
    "runtime-mode",
    "runtime-status",
    "observed-marker",
)
STABLE_FIELDS = (
    "schema",
    "fixture",
    "source-project",
    "source-path",
    "source-revision",
    "source-sha256",
    "patch-path",
    "patch-sha256",
    "compile-flags",
    "link-flags",
    "runtime-profile",
    "bootstrap-profile",
    "compiler-path",
    "compiler-sha256",
    "compiler-version",
    "compiler-origin",
    "clt-product-id",
    "clt-provenance-sha256",
    "artifact-sha256",
    "expected-returncode",
    "expected-marker",
    "runtime-mode",
    "runtime-status",
    "observed-marker",
)
BATCH_FIELDS = (
    "fixture",
    "source-project",
    "source-path",
    "source-revision",
    "source-sha256",
    "patch-path",
    "patch-sha256",
    "artifact-sha256",
    "runtime-profile",
    "expected-marker",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_tsv(path: Path, header: tuple[str, ...]) -> list[dict[str, str]]:
    rows = path.read_text(encoding="utf-8").splitlines()
    if not rows or tuple(rows[0].split("\t")) != header:
        raise ValueError(f"{path}: invalid TSV header")
    result = []
    for line in rows[1:]:
        fields = line.split("\t")
        if len(fields) != len(header):
            raise ValueError(f"{path}: malformed TSV row")
        result.append(dict(zip(header, fields)))
    return result


def read_key_value_tsv(path: Path) -> dict[str, str]:
    rows = path.read_text(encoding="utf-8").splitlines()
    if not rows or rows[0] != "field\tvalue":
        raise ValueError(f"{path}: invalid key/value TSV header")
    result: dict[str, str] = {}
    for line in rows[1:]:
        fields = line.split("\t", 1)
        if len(fields) != 2 or not fields[0] or fields[0] in result:
            raise ValueError(f"{path}: malformed or duplicate key/value row")
        result[fields[0]] = fields[1]
    return result


def read_summary(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2 or not fields[0] or fields[0] in result:
            raise ValueError(f"{path}: malformed or duplicate Mach-O summary row")
        result[fields[0]] = fields[1]
    return result


def read_colon_document(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(": ")
        if not separator or not key or key in result:
            raise ValueError(f"{path}: malformed or duplicate document row")
        result[key] = value
    return result


def reject_forbidden_payloads(root: Path) -> None:
    forbidden = []
    for path in root.rglob("*"):
        if path.is_file() and (
            path.suffix == ".pkg" or path.name in {"corpus.yml", "corpus.yaml"}
        ):
            forbidden.append(str(path))
        if path.is_dir() and path.name in {"prefix", "cache", "clt-cache", ".work"}:
            forbidden.append(str(path))
    if forbidden:
        raise ValueError(f"batch artifact contains forbidden payload/state: {forbidden}")


def expected_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in FIXTURE_SPECS)


def parsed_manifest(binary: Path) -> dict[str, Any]:
    header = _macho_header(binary)
    return {
        "schema": 1,
        "magic": header.magic,
        "architecture": header.architecture,
        "filetype": header.filetype,
        "dylib-load-commands": list(header.dylib_load_commands),
        "rpath-load-commands": list(header.rpath_load_commands),
    }


def expected_summary(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        "schema": str(manifest["schema"]),
        "magic": manifest["magic"],
        "architecture": manifest["architecture"],
        "filetype": manifest["filetype"],
        "dylib-load-commands": json.dumps(
            manifest["dylib-load-commands"], separators=(",", ":"), sort_keys=True
        ),
        "rpath-load-commands": json.dumps(
            manifest["rpath-load-commands"], separators=(",", ":"), sort_keys=True
        ),
    }


def read_batch_state(path: Path) -> None:
    values = read_key_value_tsv(path)
    if values.get("schema") != "1" or values.get("fixture-count") != "14":
        raise ValueError(f"{path}: invalid batch state")
    if values.get("phase") != "complete" or values.get("status") != "COMPLETE":
        raise ValueError(f"{path}: batch did not complete successfully")


def read_failure_summary(path: Path) -> None:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(": ")
        if not separator or key in values:
            raise ValueError(f"{path}: malformed failure summary")
        values[key] = value
    expected = {
        "status": "success",
        "fixture-count": "14",
        "exit-code": "0",
        "phase": "complete",
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise ValueError(f"{path}: batch failure summary is not successful")


def read_batch_manifest(path: Path) -> dict[str, dict[str, str]]:
    rows = read_tsv(path, BATCH_FIELDS)
    names = [row["fixture"] for row in rows]
    if len(names) != len(set(names)) or set(names) != set(expected_names()):
        raise ValueError(f"{path}: missing or extra fixture")
    return {row["fixture"]: row for row in rows}


def read_provenance(path: Path, spec_name: str) -> dict[str, str]:
    values = read_key_value_tsv(path)
    missing = [field for field in REQUIRED_FIELDS if not values.get(field)]
    if missing:
        raise ValueError(f"{path}: missing provenance fields {missing}")
    spec = next(spec for spec in FIXTURE_SPECS if spec.name == spec_name)
    if values["schema"] != "1" or values["fixture"] != spec.name:
        raise ValueError(f"{path}: wrong fixture identity")
    if values["source-project"] != spec.source_project:
        raise ValueError(f"{path}: source project differs from allowlist")
    if values["source-path"] != spec.source_path:
        raise ValueError(f"{path}: source path differs from allowlist")
    if values["source-sha256"] != spec.source_sha256:
        raise ValueError(f"{path}: source SHA-256 differs from allowlist")
    if values["patch-path"] != spec.patch_path or values["patch-sha256"] != spec.patch_sha256:
        raise ValueError(f"{path}: owning patch provenance differs from allowlist")
    if values["compile-flags"] != "|".join(spec.compile_flags):
        raise ValueError(f"{path}: compile flags differ from allowlist")
    if values["link-flags"] != ("|".join(spec.link_flags) or "-"):
        raise ValueError(f"{path}: link flags differ from allowlist")
    if values["runtime-profile"] != spec.runtime_profile:
        raise ValueError(f"{path}: runtime profile differs from allowlist")
    if values["bootstrap-profile"] != BATCH_BOOTSTRAP_PROFILE:
        raise ValueError(f"{path}: unexpected bootstrap profile")
    if values["expected-marker"] != spec.expected_marker or values["expected-returncode"] != "0":
        raise ValueError(f"{path}: expected behavior differs from allowlist")
    if values["clt-product-id"] != "041-90419":
        raise ValueError(f"{path}: unexpected CLT product")
    if not _SHA256_RE.fullmatch(values["source-sha256"]) or not _SHA256_RE.fullmatch(values["patch-sha256"]):
        raise ValueError(f"{path}: invalid provenance SHA-256")
    if not re.fullmatch(r"[0-9a-f]{40}", values["source-revision"]):
        raise ValueError(f"{path}: invalid source revision")
    return values


def read_runtime_evidence(fixture: Path, spec_name: str, provenance: dict[str, str]) -> dict[str, str]:
    values = read_key_value_tsv(fixture / "runtime-evidence.tsv")
    if values.get("fixture") != spec_name:
        raise ValueError(f"{fixture}: runtime evidence identity mismatch")
    marker = provenance["expected-marker"]
    log = (fixture / "runtime.log").read_text(encoding="utf-8")
    if spec_name == ANCHOR_FIXTURE:
        if values.get("runtime-mode") != "anchor":
            raise ValueError(f"{fixture}: anchor runtime mode missing")
        if values.get("runtime-status") != "OBSERVED" or values.get("runtime-exit-code") != "0":
            raise ValueError(f"{fixture}: anchor runtime was not observed successfully")
        if values.get("observed-marker") != marker or marker not in log.splitlines():
            raise ValueError(f"{fixture}: anchor marker is not observed")
    else:
        if values.get("runtime-mode") != "compile-only":
            raise ValueError(f"{fixture}: non-anchor runtime mode is not compile-only")
        if (
            values.get("runtime-status") != "NOT_RUN"
            or values.get("runtime-exit-code") != "NOT_RUN"
            or values.get("observed-marker") != "NOT_OBSERVED"
        ):
            raise ValueError(f"{fixture}: unverified runtime evidence was accepted")
        if log != "RUNTIME_NOT_RUN_COMPILE_ONLY\n":
            raise ValueError(f"{fixture}: non-anchor runtime log is not the compile-only sentinel")
    return values


def read_fixture(run: Path, spec_name: str) -> tuple[dict[str, str], dict[str, Any], str]:
    fixture = run / "fixtures" / spec_name
    missing = [name for name in FIXTURE_FILES if not (fixture / name).is_file()]
    if missing:
        raise ValueError(f"{fixture}: missing evidence files {missing}")
    binary = fixture / spec_name
    compile_status = read_key_value_tsv(fixture / "compile-status.tsv")
    if (
        compile_status.get("fixture") != spec_name
        or compile_status.get("compile-status") != "PASS"
        or compile_status.get("compile-exit-code") != "0"
    ):
        raise ValueError(f"{fixture}: compile was not successful")
    provenance = read_provenance(fixture / "provenance.tsv", spec_name)
    actual_hash = sha256(binary)
    if provenance["artifact-sha256"] != actual_hash:
        raise ValueError(f"{fixture}: artifact SHA-256 mismatch")
    checksum = (fixture / "artifact.sha256").read_text(encoding="utf-8").split()
    if checksum != [actual_hash, spec_name]:
        raise ValueError(f"{fixture}: artifact.sha256 mismatch")
    if spec_name == ANCHOR_FIXTURE and actual_hash != ANCHOR_ARTIFACT_SHA256:
        raise ValueError(f"{fixture}: select_fdset_guest anchor SHA-256 mismatch")
    for field, evidence_name in (
        ("provenance-document-sha256", "provenance.txt"),
        ("clt-provenance-sha256", "clt-provenance.txt"),
        ("compile-status-sha256", "compile-status.tsv"),
        ("compile-log-sha256", "compile.log"),
        ("runtime-evidence-sha256", "runtime-evidence.tsv"),
        ("runtime-log-sha256", "runtime.log"),
        ("private-header-sha256", "private-header.txt"),
        ("dylibs-report-sha256", "dylibs-used.txt"),
        ("macho-summary-sha256", "macho-summary.tsv"),
    ):
        if provenance[field] != sha256(fixture / evidence_name):
            raise ValueError(f"{fixture}: {field} does not match evidence")

    document = (fixture / "provenance.txt").read_text(encoding="utf-8")
    document_values = read_colon_document(fixture / "provenance.txt")
    for required in (
        "status: REVIEWED_PROVENANCE",
        "clt_review_status: reviewed",
        "batch: guest-macho-phase-3b",
        f"fixture: {spec_name}",
        f"expected_marker: {provenance['expected-marker']}",
    ):
        if required not in document:
            raise ValueError(f"{fixture}: provenance document lacks {required!r}")

    runtime_evidence = read_runtime_evidence(fixture, spec_name, provenance)
    for field in ("runtime-mode", "runtime-status", "observed-marker"):
        if provenance[field] != runtime_evidence[field]:
            raise ValueError(f"{fixture}: provenance.tsv disagrees with runtime evidence for {field}")
    for evidence_field, document_field in (
        ("runtime-mode", "runtime_mode"),
        ("runtime-status", "runtime_status"),
        ("observed-marker", "observed_marker"),
    ):
        if document_values.get(document_field) != runtime_evidence[evidence_field]:
            raise ValueError(f"{fixture}: provenance.txt disagrees with runtime evidence for {evidence_field}")

    clt = (fixture / "clt-provenance.txt").read_text(encoding="utf-8")
    for required in (
        "review_status: reviewed",
        "evidence_run:",
        "product_id: 041-90419",
        "certificate_fingerprints_sha256:",
        "signature_check:",
    ):
        if required not in clt:
            raise ValueError(f"{fixture}: CLT provenance lacks {required!r}")

    version = (fixture / "clang-version.txt").read_text(encoding="utf-8").strip()
    if not version or "clang" not in version.lower():
        raise ValueError(f"{fixture}: guest clang version evidence is missing")
    if provenance["compiler-version"] != " ".join(version.split()):
        raise ValueError(f"{fixture}: compiler version provenance mismatch")
    origin = ";".join(
        line.strip()
        for line in (fixture / "clang-origin.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if (
        "execution-context=guest" not in origin
        or "executable=/Library/Developer/CommandLineTools/usr/bin/clang" not in origin
    ):
        raise ValueError(f"{fixture}: compiler origin is not guest clang")
    if provenance["compiler-origin"] != origin:
        raise ValueError(f"{fixture}: compiler origin provenance mismatch")
    if spec_name != ANCHOR_FIXTURE and provenance["expected-marker"] in (fixture / "compile.log").read_text(encoding="utf-8").splitlines():
        raise ValueError(f"{fixture}: compile-only log contains an observed runtime marker")
    try:
        manifest = json.loads((fixture / "macho-manifest.json").read_text(encoding="utf-8"))
        actual_manifest = parsed_manifest(binary)
    except (OSError, ValueError, GuestMachoFixtureError) as error:
        raise ValueError(f"{fixture}: Mach-O parsing failed: {error}") from error
    if manifest != actual_manifest:
        raise ValueError(f"{fixture}: Mach-O manifest differs from parser output")
    if read_summary(fixture / "macho-summary.tsv") != expected_summary(actual_manifest):
        raise ValueError(f"{fixture}: Mach-O summary differs from parser output")
    return provenance, actual_manifest, actual_hash


def read_run(root: Path, name: str):
    run = root / name
    if not run.is_dir():
        raise ValueError(f"missing matrix artifact directory: {run}")
    if {path.name for path in run.iterdir()} != set(ROOT_FILES):
        raise ValueError(f"{run}: artifact root has missing or extra files")
    if {path.name for path in (run / "fixtures").iterdir()} != set(expected_names()):
        raise ValueError(f"{run}: fixture set has missing or extra fixtures")
    read_batch_state(run / "batch-state.tsv")
    read_failure_summary(run / "failure-summary.txt")
    batch = read_batch_manifest(run / "batch-manifest.tsv")
    records = {name: read_fixture(run, name) for name in expected_names()}
    for name, row in batch.items():
        provenance = records[name][0]
        for field in BATCH_FIELDS[1:]:
            provenance_field = field
            if field == "source-project":
                provenance_field = "source-project"
            if row[field] != provenance[provenance_field]:
                raise ValueError(f"{run}: batch manifest mismatch for {name}: {field}")
    return batch, records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        validate_specs()
        reject_forbidden_payloads(args.root)
        left_batch, left_records = read_run(args.root, RUN_NAMES[0])
        right_batch, right_records = read_run(args.root, RUN_NAMES[1])
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"batch evidence is invalid: {error}", file=sys.stderr)
        return 1

    mismatches = []
    if left_batch != right_batch:
        mismatches.append("batch manifests differ")
    for name in expected_names():
        left_provenance, left_manifest, left_hash = left_records[name]
        right_provenance, right_manifest, right_hash = right_records[name]
        if left_hash != right_hash:
            mismatches.append(f"{name}: binary SHA-256 differs")
        if left_manifest != right_manifest:
            mismatches.append(f"{name}: structured Mach-O manifest differs")
        for field in STABLE_FIELDS:
            if left_provenance[field] != right_provenance[field]:
                mismatches.append(
                    f"{name}: {field}: {left_provenance[field]!r} != {right_provenance[field]!r}"
                )
    if mismatches:
        print("MACHO_CORPUS_BATCH_MISMATCH", file=sys.stderr)
        for mismatch in mismatches:
            print(f"- {mismatch}", file=sys.stderr)
        return 1
    print(
        "MACHO_CORPUS_BATCH_MATCH "
        f"fixtures={len(FIXTURE_SPECS)} anchor_sha256={ANCHOR_ARTIFACT_SHA256}"
    )
    print("A/B compile evidence and anchor runtime evidence match; no corpus registration was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

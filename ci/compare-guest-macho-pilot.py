#!/usr/bin/env python3
"""Validate and compare two hosted Mach-O pilot evidence directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


WEST_COMMANDS = Path(__file__).resolve().parents[1] / "west_commands"
if str(WEST_COMMANDS) not in sys.path:
    sys.path.insert(0, str(WEST_COMMANDS))

from test_guest_macho import GuestMachoFixtureError, _macho_header


RUN_NAMES = ("macho-corpus-pilot-a", "macho-corpus-pilot-b")
REQUIRED_FILES = (
    "select_fdset_guest",
    "artifact.sha256",
    "clang-version.txt",
    "clang-origin.txt",
    "clt-provenance.txt",
    "dylibs-used.txt",
    "file.txt",
    "guest-build.log",
    "guest-marker.txt",
    "macho-manifest.json",
    "macho-summary.tsv",
    "private-header.txt",
    "provenance.tsv",
    "provenance.txt",
)
REQUIRED_FIELDS = (
    "schema",
    "pilot",
    "source-path",
    "source-sha256",
    "compiler-path",
    "compiler-sha256",
    "compiler-version",
    "compiler-origin",
    "flags",
    "clt-product-id",
    "clt-provenance-sha256",
    "provenance-document-sha256",
    "artifact-sha256",
    "private-header-sha256",
    "dylibs-report-sha256",
    "macho-summary-sha256",
    "expected-returncode",
    "expected-marker",
)
STABLE_FIELDS = (
    "schema",
    "pilot",
    "source-path",
    "source-sha256",
    "compiler-path",
    "compiler-sha256",
    "compiler-version",
    "compiler-origin",
    "flags",
    "clt-product-id",
    "clt-provenance-sha256",
    "artifact-sha256",
    "macho-summary-sha256",
    "expected-returncode",
    "expected-marker",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_provenance(path: Path) -> dict[str, str]:
    rows = path.read_text(encoding="utf-8").splitlines()
    if not rows or rows[0] != "field\tvalue":
        raise ValueError(f"{path}: invalid provenance header")
    result: dict[str, str] = {}
    for line in rows[1:]:
        fields = line.split("\t", 1)
        if len(fields) != 2 or not fields[0] or fields[0] in result:
            raise ValueError(f"{path}: malformed or duplicate provenance field")
        result[fields[0]] = fields[1]
    missing = [field for field in REQUIRED_FIELDS if not result.get(field)]
    if missing:
        raise ValueError(f"{path}: missing provenance fields {missing}")
    if result["pilot"] != "select_fdset_guest":
        raise ValueError(f"{path}: unexpected pilot {result['pilot']!r}")
    if result["expected-marker"] != "SELECT_FDSET_GUEST_OK":
        raise ValueError(f"{path}: unexpected expected marker")
    if result["clt-product-id"] != "041-90419":
        raise ValueError(f"{path}: unexpected CLT product")
    return result


def reject_forbidden_payloads(root: Path) -> None:
    forbidden = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix == ".pkg":
            forbidden.append(str(path))
        if path.is_dir() and path.name in {"prefix", "cache", "clt-cache"}:
            forbidden.append(str(path))
    if forbidden:
        raise ValueError(f"artifact set contains forbidden payload/state: {forbidden}")


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


def read_summary(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2 or not fields[0] or fields[0] in result:
            raise ValueError(f"{path}: malformed or duplicate Mach-O summary field")
        result[fields[0]] = fields[1]
    return result


def expected_summary(manifest: dict[str, Any]) -> dict[str, str]:
    return {
        "schema": str(manifest["schema"]),
        "magic": manifest["magic"],
        "architecture": manifest["architecture"],
        "filetype": manifest["filetype"],
        "dylib-load-commands": json.dumps(
            manifest["dylib-load-commands"],
            separators=(",", ":"),
            sort_keys=True,
        ),
        "rpath-load-commands": json.dumps(
            manifest["rpath-load-commands"],
            separators=(",", ":"),
            sort_keys=True,
        ),
    }


def read_run(root: Path, name: str) -> tuple[dict[str, str], dict[str, Any], str]:
    run = root / name
    if not run.is_dir():
        raise ValueError(f"missing matrix artifact directory: {run}")
    missing = [filename for filename in REQUIRED_FILES if not (run / filename).is_file()]
    if missing:
        raise ValueError(f"{run}: missing evidence files {missing}")

    binary = run / "select_fdset_guest"
    provenance = read_provenance(run / "provenance.tsv")
    actual_hash = sha256(binary)
    if provenance["artifact-sha256"] != actual_hash:
        raise ValueError(f"{run}: provenance artifact SHA-256 does not match binary")

    artifact_checksum = (run / "artifact.sha256").read_text(encoding="utf-8").split()
    if artifact_checksum != [actual_hash, "select_fdset_guest"]:
        raise ValueError(f"{run}: artifact.sha256 does not match the downloaded binary")

    provenance_document_hash = sha256(run / "provenance.txt")
    if provenance["provenance-document-sha256"] != provenance_document_hash:
        raise ValueError(f"{run}: provenance document SHA-256 mismatch")
    clt_provenance_hash = sha256(run / "clt-provenance.txt")
    if provenance["clt-provenance-sha256"] != clt_provenance_hash:
        raise ValueError(f"{run}: CLT provenance SHA-256 mismatch")
    report_hashes = {
        "private-header-sha256": sha256(run / "private-header.txt"),
        "dylibs-report-sha256": sha256(run / "dylibs-used.txt"),
        "macho-summary-sha256": sha256(run / "macho-summary.tsv"),
    }
    for field, actual in report_hashes.items():
        if provenance[field] != actual:
            raise ValueError(f"{run}: {field} does not match evidence file")

    document = (run / "provenance.txt").read_text(encoding="utf-8")
    for marker in (
        "status: REVIEWED_PROVENANCE",
        "clt_review_status: reviewed",
        "pilot: select_fdset_guest",
        "expected_marker: SELECT_FDSET_GUEST_OK",
    ):
        if marker not in document:
            raise ValueError(f"{run}: provenance document lacks {marker!r}")

    clt_provenance = (run / "clt-provenance.txt").read_text(encoding="utf-8")
    for marker in (
        "review_status: reviewed",
        "evidence_run:",
        "product_id: 041-90419",
        "certificate_fingerprints_sha256:",
        "signature_check:",
    ):
        if marker not in clt_provenance:
            raise ValueError(f"{run}: CLT provenance lacks reviewed marker {marker!r}")

    clang_version = (run / "clang-version.txt").read_text(encoding="utf-8").strip()
    if not clang_version or "clang" not in clang_version.lower():
        raise ValueError(f"{run}: guest clang version evidence is missing")
    if provenance["compiler-version"] != " ".join(clang_version.split()):
        raise ValueError(f"{run}: compiler-version provenance does not match evidence")

    clang_origin = ";".join(
        line.strip()
        for line in (run / "clang-origin.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if "execution-context=guest" not in clang_origin or "executable=/Library/Developer/CommandLineTools/usr/bin/clang" not in clang_origin:
        raise ValueError(f"{run}: compiler origin is not an explicit guest clang")
    if provenance["compiler-origin"] != clang_origin:
        raise ValueError(f"{run}: compiler-origin provenance does not match evidence")

    marker = (run / "guest-marker.txt").read_text(encoding="utf-8").strip()
    if marker != "SELECT_FDSET_GUEST_OK":
        raise ValueError(f"{run}: guest marker is invalid")
    if "SELECT_FDSET_GUEST_OK" not in (run / "guest-build.log").read_text(encoding="utf-8"):
        raise ValueError(f"{run}: guest build log lacks execution marker")

    try:
        manifest = json.loads((run / "macho-manifest.json").read_text(encoding="utf-8"))
        actual_manifest = parsed_manifest(binary)
    except (OSError, ValueError, GuestMachoFixtureError) as error:
        raise ValueError(f"{run}: Mach-O parsing failed: {error}") from error
    if manifest != actual_manifest:
        raise ValueError(f"{run}: structured Mach-O manifest differs from parser output")
    if read_summary(run / "macho-summary.tsv") != expected_summary(actual_manifest):
        raise ValueError(f"{run}: structured Mach-O summary differs from parser output")

    return provenance, actual_manifest, actual_hash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        reject_forbidden_payloads(args.root)
        records = [read_run(args.root, name) for name in RUN_NAMES]
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"pilot evidence is invalid: {error}", file=sys.stderr)
        return 1

    left, right = records
    mismatches = []
    if left[2] != right[2]:
        mismatches.append(f"artifact SHA-256: {left[2]} != {right[2]}")
    if left[1] != right[1]:
        mismatches.append("structured Mach-O manifests differ")
    for field in STABLE_FIELDS:
        if left[0][field] != right[0][field]:
            mismatches.append(
                f"{field}: {left[0][field]!r} != {right[0][field]!r}"
            )
    if mismatches:
        print("MACHO_CORPUS_PILOT_MISMATCH", file=sys.stderr)
        for mismatch in mismatches:
            print(f"- {mismatch}", file=sys.stderr)
        return 1
    print(f"MACHO_CORPUS_PILOT_MATCH sha256={left[2]}")
    print("Stable provenance and structured Mach-O fields match; no corpus registration was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compare stable fields from two independent CLT provenance artifacts."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


EXPECTED_PACKAGE_IDS = {
    "com.apple.pkg.CLTools_SDK_OSX1012",
    "com.apple.pkg.DevSDK_OSX1012",
    "com.apple.pkg.CLTools_SDK_macOSSDK",
    "com.apple.pkg.CLTools_SDK_macOS1013",
    "com.apple.pkg.CLTools_Executables",
}
STABLE_FIELDS = (
    "catalog_url",
    "download_url",
    "catalog_size",
    "api_sha1",
    "final_url",
    "http_status",
    "content_length",
    "actual_size",
    "actual_sha1",
    "actual_sha256",
    "api_sha1_status",
    "pkgutil_status",
    "certificate_fingerprints",
)


def read_rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    result = {row.get("package_id", ""): row for row in rows}
    if set(result) != EXPECTED_PACKAGE_IDS:
        raise ValueError(f"{path}: package set is {sorted(result)}")
    for package_id, row in result.items():
        if row.get("pkgutil_status") != "SIGNATURE_VALID_NOT_REVIEWED":
            raise ValueError(f"{path}: {package_id} has an unreviewed signature result")
    return result


def read_evidence_status(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if "status: EVIDENCE_COMPLETE" not in lines:
        raise ValueError(f"{path}: evidence is not complete")
    if "trust_status: SIGNATURE_VALID_NOT_REVIEWED" not in lines:
        raise ValueError(f"{path}: trust status is not explicitly unreviewed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory containing two clt-provenance-* runs")
    args = parser.parse_args()
    runs = sorted(path for path in args.root.glob("clt-provenance-*") if path.is_dir())
    if len(runs) != 2:
        print(f"expected exactly two provenance runs, found {len(runs)}", file=sys.stderr)
        return 2
    try:
        records = [read_rows(run / "provenance.tsv") for run in runs]
        for run in runs:
            read_evidence_status(run / "provenance.txt")
    except (OSError, ValueError) as error:
        print(f"cannot read provenance: {error}", file=sys.stderr)
        return 1

    mismatches = []
    for package_id in sorted(EXPECTED_PACKAGE_IDS):
        left, right = records[0][package_id], records[1][package_id]
        for field in STABLE_FIELDS:
            if left.get(field, "") != right.get(field, ""):
                mismatches.append(
                    f"{package_id} {field}: {left.get(field, '')!r} != {right.get(field, '')!r}"
                )
        if left.get("actual_sha256"):
            print(f"{package_id}: SHA-256 {left['actual_sha256']}")
    if mismatches:
        print("provenance runs differ:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"- {mismatch}", file=sys.stderr)
        return 1
    print("EVIDENCE_MATCH")
    print("No allowlist or provider value was consulted or changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

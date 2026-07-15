"""Behavioral contract for CLT provenance normalization and comparison."""

from __future__ import annotations

import csv
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
sys.path.insert(0, str(ROOT / "west_commands"))

import compare_clt_provenance
import guest_toolchain
import verify_clt_provenance
from verify_clt_provenance import (
    MAX_XAR_TOC_COMPRESSED,
    MAX_XAR_TOC_UNCOMPRESSED,
    SIGNATURE_VALID_NOT_REVIEWED,
    VerificationError,
    certificate_report,
    normalize_download_url,
    validate_final_apple_url,
    xar_certificates,
)


def write_run(
    path: Path,
    sha256: str,
    *,
    etag: str = "same",
    last_modified: str = "same",
) -> None:
    path.mkdir(parents=True)
    rows = []
    for package_id in sorted(compare_clt_provenance.EXPECTED_PACKAGE_IDS):
        row = {field: "same" for field in compare_clt_provenance.STABLE_FIELDS}
        row.update(
            {
                "package_id": package_id,
                "pkgutil_status": SIGNATURE_VALID_NOT_REVIEWED,
                "actual_sha256": sha256,
                "etag": etag,
                "last_modified": last_modified,
            }
        )
        rows.append(row)
    with (path / "provenance.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("package_id",)
            + compare_clt_provenance.STABLE_FIELDS
            + ("etag", "last_modified"),
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerows(rows)
    (path / "provenance.txt").write_text(
        "status: EVIDENCE_COMPLETE\n"
        "trust_status: SIGNATURE_VALID_NOT_REVIEWED\n",
        encoding="utf-8",
    )


def run_compare(root: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "ci/compare_clt_provenance.py"),
            str(root),
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def write_xar(path: Path, toc: bytes, declared_size: int | None = None) -> None:
    compressed_toc = zlib.compress(toc)
    path.write_bytes(
        struct.pack(
            ">4sHHQQI",
            b"xar!",
            28,
            1,
            len(compressed_toc),
            len(toc) if declared_size is None else declared_size,
            1,
        )
        + compressed_toc
    )


def read_reviewed_provenance() -> dict[str, dict[str, str]]:
    path = ROOT / "docs/clt-provenance-041-90419.txt"
    packages: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("[package: ") and line.endswith("]"):
            package_id = line[len("[package: ") : -1]
            current = {}
            packages[package_id] = current
        elif current is not None and ": " in line:
            key, value = line.split(": ", 1)
            current[key] = value
    return packages


def main() -> None:
    reviewed = read_reviewed_provenance()
    assert set(reviewed) == set(guest_toolchain.REVIEWED_COMMAND_LINE_TOOLS_SHA256)
    assert {
        package_id: values["actual_sha256"]
        for package_id, values in reviewed.items()
    } == guest_toolchain.REVIEWED_COMMAND_LINE_TOOLS_SHA256

    assert normalize_download_url(
        "http://swcdn.apple.com/path/pkg"
    ) == "https://swcdn.apple.com/path/pkg"
    try:
        normalize_download_url("https://example.invalid/pkg")
    except VerificationError:
        pass
    else:
        raise AssertionError("non-Apple package URL was accepted")
    assert validate_final_apple_url(
        "https://swcdn.apple.com/path/pkg?redirect=1"
    ) == "https://swcdn.apple.com/path/pkg"
    for bad_url in (
        "http://swcdn.apple.com/path/pkg",
        "https://evil.example/path/pkg",
        "https://user:pass@swcdn.apple.com/path/pkg",
    ):
        try:
            validate_final_apple_url(bad_url)
        except VerificationError:
            pass
        else:
            raise AssertionError(f"unsafe final URL was accepted: {bad_url}")

    with tempfile.TemporaryDirectory(prefix="west-clt-provenance-contract-") as raw:
        root = Path(raw)
        equal_root = root / "equal"
        write_run(equal_root / "clt-provenance-macos-14", "a" * 64)
        write_run(
            equal_root / "clt-provenance-macos-15",
            "a" * 64,
            etag="different-etag",
            last_modified="different-last-modified",
        )
        result = run_compare(equal_root)
        assert result.returncode == 0, result.stderr

        mismatch_root = root / "mismatch"
        write_run(mismatch_root / "clt-provenance-macos-14", "a" * 64)
        write_run(mismatch_root / "clt-provenance-macos-15", "b" * 64)
        result = run_compare(mismatch_root)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "actual_sha256" in result.stderr, result.stderr

        with tempfile.TemporaryDirectory(prefix="west-xar-bounds-contract-") as xar_raw:
            xar_root = Path(xar_raw)
            for name, compressed, uncompressed in (
                ("compressed", MAX_XAR_TOC_COMPRESSED + 1, 1),
                ("uncompressed", 1, MAX_XAR_TOC_UNCOMPRESSED + 1),
            ):
                path = xar_root / f"{name}.pkg"
                path.write_bytes(
                    struct.pack(">4sHHQQI", b"xar!", 28, 1, compressed, uncompressed, 1)
                )
                try:
                    xar_certificates(path)
                except VerificationError:
                    pass
                else:
                    raise AssertionError(f"oversized {name} XAR TOC was accepted")

            mismatched_toc = xar_root / "mismatched-uncompressed-size.pkg"
            toc = b"<xar/>"
            write_xar(mismatched_toc, toc, declared_size=len(toc) + 1)
            try:
                xar_certificates(mismatched_toc)
            except VerificationError as error:
                assert "does not match header" in str(error), error
            else:
                raise AssertionError("XAR TOC with mismatched declared size was accepted")

            signatures = xar_root / "signatures"
            signatures.mkdir()
            original_xar_certificates = verify_clt_provenance.xar_certificates
            verify_clt_provenance.xar_certificates = lambda _path: []
            try:
                try:
                    certificate_report(
                        mismatched_toc,
                        xar_root,
                        "com.example.empty-certificates",
                    )
                except VerificationError as error:
                    assert "certificate fingerprints are empty" in str(error), error
                else:
                    raise AssertionError("empty certificate fingerprints were accepted")
            finally:
                verify_clt_provenance.xar_certificates = original_xar_certificates
    print("PASS clt-provenance-contract")


if __name__ == "__main__":
    main()

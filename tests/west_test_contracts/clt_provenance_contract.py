"""Behavioral contract for CLT provenance normalization and comparison."""

from __future__ import annotations

import csv
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
sys.path.insert(0, str(ROOT / "west_commands"))

import compare_clt_provenance
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
    toc_digest: str = "same",
) -> None:
    path.mkdir(parents=True)
    rows = []
    for package_id in sorted(compare_clt_provenance.EXPECTED_PACKAGE_IDS):
        row = {field: "same" for field in compare_clt_provenance.STABLE_FIELDS}
        row.update(
            {
                "package_id": package_id,
                "pkgutil_status": SIGNATURE_VALID_NOT_REVIEWED,
                "api_sha1_status": "MATCH",
                "xar_toc_sha1": toc_digest,
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
        + hashlib.sha1(compressed_toc).digest()
    )


def catalog_digest_contract(root: Path) -> None:
    path = root / "digest-domain.pkg"
    toc = b"<xar><toc/></xar>"
    write_xar(path, toc)
    payload = path.read_bytes()
    toc_digest = hashlib.sha1(zlib.compress(toc)).hexdigest()
    package = {
        "package_id": "com.apple.pkg.CLTools_Executables",
        "catalog_url": "http://swcdn.apple.com/fixture.pkg",
        "download_url": "https://swcdn.apple.com/fixture.pkg",
        "catalog_size": len(payload),
        "api_sha1": toc_digest,
    }
    headers = {
        "http_status": "200",
        "content_length": str(len(payload)),
        "final_url": package["download_url"],
        "etag": "",
        "last_modified": "",
    }
    with (
        patch.object(verify_clt_provenance, "download_package", return_value=(path, headers)),
        patch.object(verify_clt_provenance, "certificate_report", return_value="fingerprint"),
        patch.object(verify_clt_provenance, "pkgutil_report", return_value=SIGNATURE_VALID_NOT_REVIEWED),
    ):
        row = verify_clt_provenance.package_row(package, root)
        assert row["actual_sha1"] == hashlib.sha1(payload).hexdigest()
        assert row["actual_sha256"] == hashlib.sha256(payload).hexdigest()
        assert row["xar_toc_sha1"] == toc_digest
        for wrong_digest in (
            hashlib.sha1(payload).hexdigest(),
            hashlib.sha1(toc).hexdigest(),
        ):
            package["api_sha1"] = wrong_digest
            try:
                verify_clt_provenance.package_row(package, root)
            except VerificationError:
                pass
            else:
                raise AssertionError("catalog digest from the wrong byte domain was accepted")
    for bad in (
        payload[:20],
        payload[:8] + struct.pack(">Q", len(payload)) + payload[16:],
        payload[:24] + struct.pack(">I", 0) + payload[28:],
    ):
        path.write_bytes(bad)
        try:
            verify_clt_provenance.xar_toc_sha1(path)
        except VerificationError:
            pass
        else:
            raise AssertionError("malformed XAR was accepted for catalog verification")


def main() -> None:
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
        catalog_digest_contract(root)
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
        incomplete_root = root / "incomplete"
        for runner in ("macos-14", "macos-15"):
            write_run(
                incomplete_root / f"clt-provenance-{runner}",
                "a" * 64,
                toc_digest="",
            )
        result = run_compare(incomplete_root)
        assert result.returncode == 1, result.stdout + result.stderr

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
            except VerificationError:
                pass
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
                except VerificationError:
                    pass
                else:
                    raise AssertionError("empty certificate fingerprints were accepted")
            finally:
                verify_clt_provenance.xar_certificates = original_xar_certificates
    print("PASS clt-provenance-contract")


if __name__ == "__main__":
    main()

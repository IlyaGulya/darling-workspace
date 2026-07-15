#!/usr/bin/env python3
"""Collect provenance evidence for Apple's five CommandLineTools payloads."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ElementTree
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DARLING_CATALOG_MIRROR_URL = (
    "https://swdistcache.darlinghq.org/api/v1/products/by-tag?tag=DTCommandLineTools"
)
CATALOG_SOURCE_DESCRIPTION = "Darling catalog mirror; not Apple metadata"
EXPECTED_PACKAGE_IDS = (
    "com.apple.pkg.CLTools_SDK_OSX1012",
    "com.apple.pkg.DevSDK_OSX1012",
    "com.apple.pkg.CLTools_SDK_macOSSDK",
    "com.apple.pkg.CLTools_SDK_macOS1013",
    "com.apple.pkg.CLTools_Executables",
)
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": "darling-clt-provenance/1",
}
CERTIFICATE_NAMESPACE = "{http://www.w3.org/2000/09/xmldsig#}X509Certificate"
ALLOWED_APPLE_DOWNLOAD_HOSTS = frozenset({"swcdn.apple.com"})
XAR_HEADER_SIZE = 28
MAX_XAR_HEADER_SIZE = 4096
MAX_XAR_TOC_COMPRESSED = 16 * 1024 * 1024
MAX_XAR_TOC_UNCOMPRESSED = 64 * 1024 * 1024
EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"
EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
SIGNATURE_VALID_NOT_REVIEWED = "SIGNATURE_VALID_NOT_REVIEWED"
TSV_FIELDS = (
    "package_id",
    "catalog_url",
    "download_url",
    "catalog_size",
    "api_sha1",
    "final_url",
    "http_status",
    "content_length",
    "etag",
    "last_modified",
    "actual_size",
    "actual_sha1",
    "actual_sha256",
    "api_sha1_status",
    "pkgutil_status",
    "certificate_fingerprints",
)


class VerificationError(RuntimeError):
    """Raised when provenance evidence cannot be collected."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_apple_download_url(url: object, *, require_https: bool) -> urllib.parse.SplitResult:
    if not isinstance(url, str):
        raise VerificationError("catalog package URL is not a string")
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or hostname not in ALLOWED_APPLE_DOWNLOAD_HOSTS:
        raise VerificationError(f"URL is not an allowed Apple download URL: {url}")
    if require_https and parsed.scheme != "https":
        raise VerificationError(f"redirected Apple download is not HTTPS: {url}")
    try:
        port = parsed.port
    except ValueError as error:
        raise VerificationError(f"Apple download URL has an invalid port: {url}") from error
    expected_port = 443 if parsed.scheme == "https" else 80
    if port not in {None, expected_port}:
        raise VerificationError(f"Apple download URL has an unexpected port: {url}")
    if parsed.username is not None or parsed.password is not None:
        raise VerificationError(f"Apple download URL contains credentials: {url}")
    if not parsed.path.startswith("/") or parsed.path == "/":
        raise VerificationError(f"Apple download URL has no package path: {url}")
    return parsed


def normalize_download_url(url: object) -> str:
    parsed = _parse_apple_download_url(url, require_https=False)
    if parsed.query or parsed.fragment:
        raise VerificationError(f"catalog package URL contains query or fragment: {url}")
    return urllib.parse.urlunsplit(("https", "swcdn.apple.com", parsed.path, "", ""))


def validate_final_apple_url(url: str) -> str:
    parsed = _parse_apple_download_url(url, require_https=True)
    return urllib.parse.urlunsplit(("https", "swcdn.apple.com", parsed.path, "", ""))


def write_response_headers(
    path: Path, response: Any, *, final_url: str | None = None
) -> None:
    with path.open("w", encoding="utf-8") as stream:
        stream.write(f"status: {getattr(response, 'status', '')}\n")
        stream.write(f"final-url: {final_url or response.geturl()}\n")
        for key, value in response.headers.items():
            stream.write(f"{key}: {value}\n")


def response_header(response: Any, name: str) -> str:
    value = response.headers.get(name)
    return "" if value is None else value.strip()


def fetch_catalog(output: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    request = urllib.request.Request(DARLING_CATALOG_MIRROR_URL, headers=NO_CACHE_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            (output / "darling-catalog-response.json").write_bytes(body)
            write_response_headers(output / "darling-catalog-http-headers.txt", response)
    except Exception as error:
        raise VerificationError(f"cannot download product metadata: {error}") from error

    try:
        payload = json.loads(body)
        if not isinstance(payload, list) or len(payload) != 1:
            raise ValueError("expected one product")
        product = payload[0]
        if not isinstance(product, dict) or not isinstance(product.get("packages"), list):
            raise ValueError("product has no package list")
    except (ValueError, json.JSONDecodeError) as error:
        raise VerificationError(f"invalid product metadata: {error}") from error

    packages: dict[str, dict[str, Any]] = {}
    for raw in product["packages"]:
        if not isinstance(raw, dict):
            raise VerificationError("product package is not an object")
        package_id = raw.get("id")
        if not isinstance(package_id, str) or package_id in packages:
            raise VerificationError(f"invalid or duplicate package id: {package_id!r}")
        packages[package_id] = {
            "package_id": package_id,
            "catalog_url": raw.get("url"),
            "download_url": normalize_download_url(raw.get("url")),
            "catalog_size": raw.get("size"),
            "api_sha1": raw.get("digest"),
        }
    missing = [package_id for package_id in EXPECTED_PACKAGE_IDS if package_id not in packages]
    if missing:
        raise VerificationError("product metadata is missing: " + ", ".join(missing))
    return product, packages


def download_package(package: dict[str, Any], output: Path) -> tuple[Path, dict[str, str]]:
    package_id = package["package_id"]
    package_path = output / "packages" / f"{package_id}.pkg"
    partial_path = package_path.with_suffix(".pkg.part")
    request = urllib.request.Request(package["download_url"], headers=NO_CACHE_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            final_url = validate_final_apple_url(response.geturl())
            write_response_headers(
                output / "http-headers" / f"{package_id}.txt",
                response,
                final_url=final_url,
            )
            with (output / "http-headers" / f"{package_id}.final-url.txt").open(
                "w", encoding="utf-8"
            ) as final_url_file:
                final_url_file.write(final_url + "\n")
            with partial_path.open("wb") as stream:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            os.replace(partial_path, package_path)
            return package_path, {
                "http_status": str(getattr(response, "status", "")),
                "final_url": final_url,
                "content_length": response_header(response, "Content-Length"),
                "etag": response_header(response, "ETag"),
                "last_modified": response_header(response, "Last-Modified"),
            }
    except Exception as error:
        partial_path.unlink(missing_ok=True)
        raise VerificationError(f"cannot download {package_id}: {error}") from error


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def xar_certificates(path: Path) -> list[bytes]:
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(XAR_HEADER_SIZE)
        if len(header) != XAR_HEADER_SIZE:
            raise VerificationError(f"{path.name}: truncated XAR header")
        magic, header_size, version, toc_compressed, toc_uncompressed, _ = struct.unpack(
            ">4sHHQQI", header
        )
        if magic != b"xar!" or version != 1 or header_size < XAR_HEADER_SIZE:
            raise VerificationError(f"{path.name}: invalid XAR header")
        if header_size > MAX_XAR_HEADER_SIZE:
            raise VerificationError(f"{path.name}: XAR header exceeds safety bound")
        if toc_compressed > MAX_XAR_TOC_COMPRESSED:
            raise VerificationError(f"{path.name}: compressed XAR TOC exceeds safety bound")
        if toc_uncompressed > MAX_XAR_TOC_UNCOMPRESSED:
            raise VerificationError(f"{path.name}: uncompressed XAR TOC exceeds safety bound")
        if header_size + toc_compressed > file_size:
            raise VerificationError(f"{path.name}: XAR TOC exceeds file size")
        stream.seek(header_size - XAR_HEADER_SIZE, 1)
        compressed_toc = stream.read(toc_compressed)
    try:
        decompressor = zlib.decompressobj()
        toc = decompressor.decompress(compressed_toc, MAX_XAR_TOC_UNCOMPRESSED + 1)
        if len(toc) > MAX_XAR_TOC_UNCOMPRESSED or decompressor.unconsumed_tail:
            raise VerificationError(f"{path.name}: XAR TOC decompression exceeds safety bound")
        toc += decompressor.flush(MAX_XAR_TOC_UNCOMPRESSED + 1 - len(toc))
        if len(toc) > MAX_XAR_TOC_UNCOMPRESSED or not decompressor.eof:
            raise VerificationError(f"{path.name}: incomplete or oversized XAR TOC")
        if len(toc) != toc_uncompressed:
            raise VerificationError(
                f"{path.name}: XAR TOC size {len(toc)} does not match "
                f"header {toc_uncompressed}"
            )
        root = ElementTree.fromstring(toc)
    except (ElementTree.ParseError, zlib.error) as error:
        raise VerificationError(f"{path.name}: invalid XAR table of contents") from error

    signature = root.find("./toc/x-signature")
    if signature is None:
        signature = root.find("./toc/signature")
    if signature is None:
        raise VerificationError(f"{path.name}: no XAR signature metadata")
    values = signature.findall(f".//{CERTIFICATE_NAMESPACE}")
    if not values:
        raise VerificationError(f"{path.name}: XAR signature has no certificates")
    return [base64.b64decode("".join((value.text or "").split())) for value in values]


def certificate_report(path: Path, output: Path, package_id: str) -> str:
    report_path = output / "signatures" / f"{package_id}.certificates.txt"
    reports = []
    for index, der in enumerate(xar_certificates(path), 1):
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-inform",
                "DER",
                "-noout",
                "-subject",
                "-issuer",
                "-serial",
                "-dates",
                "-fingerprint",
                "-sha256",
            ],
            input=der,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise VerificationError(
                f"{package_id}: openssl could not parse certificate {index}: "
                + result.stderr.decode(errors="replace").strip()
            )
        reports.append(f"certificate {index}\n{result.stdout.decode(errors='replace')}")
    report_path.write_text("\n".join(reports), encoding="utf-8")
    fingerprints = []
    for line in report_path.read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("sha256 fingerprint="):
            fingerprints.append(line.split("=", 1)[1].replace(":", "").lower())
    if not fingerprints:
        raise VerificationError(f"{package_id}: certificate fingerprints are empty")
    return ";".join(fingerprints)


def pkgutil_report(path: Path, output: Path, package_id: str) -> str:
    report_path = output / "signatures" / f"{package_id}.pkgutil.txt"
    result = subprocess.run(
        ["pkgutil", "--check-signature", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    report_path.write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        raise VerificationError(
            f"{package_id}: pkgutil --check-signature failed (rc={result.returncode})"
        )
    return SIGNATURE_VALID_NOT_REVIEWED


def package_row(package: dict[str, Any], output: Path) -> dict[str, str]:
    package_id = package["package_id"]
    path, headers = download_package(package, output)
    actual_size = path.stat().st_size
    catalog_size = package["catalog_size"]
    if type(catalog_size) is not int or catalog_size <= 0:
        raise VerificationError(f"{package_id}: invalid catalog size")
    if headers["http_status"] != "200":
        raise VerificationError(
            f"{package_id}: download returned HTTP {headers['http_status']}"
        )
    if headers["content_length"]:
        try:
            content_length = int(headers["content_length"])
        except ValueError as error:
            raise VerificationError(
                f"{package_id}: invalid Content-Length {headers['content_length']!r}"
            ) from error
        if content_length != actual_size:
            raise VerificationError(
                f"{package_id}: Content-Length {headers['content_length']} "
                f"does not match downloaded size {actual_size}"
            )
    if actual_size != catalog_size:
        raise VerificationError(
            f"{package_id}: downloaded size {actual_size}, catalog says {catalog_size}"
        )
    actual_sha1 = sha1(path)
    actual_sha256 = sha256(path)
    cert_fingerprints = certificate_report(path, output, package_id)
    pkgutil_status = pkgutil_report(path, output, package_id)
    api_sha1_status = (
        "MATCH"
        if actual_sha1.lower() == str(package["api_sha1"]).lower()
        else "STALE_OR_REPUBLISHED_CATALOG_METADATA_UNPROVEN"
    )
    return {
        "package_id": package_id,
        "catalog_url": str(package["catalog_url"]),
        "download_url": package["download_url"],
        "final_url": headers["final_url"],
        "catalog_size": str(catalog_size),
        "api_sha1": str(package["api_sha1"]),
        "http_status": headers["http_status"],
        "content_length": headers["content_length"],
        "etag": headers["etag"],
        "last_modified": headers["last_modified"],
        "actual_size": str(actual_size),
        "actual_sha1": actual_sha1,
        "actual_sha256": actual_sha256,
        "api_sha1_status": api_sha1_status,
        "pkgutil_status": pkgutil_status,
        "certificate_fingerprints": cert_fingerprints,
    }


def require_tools() -> None:
    missing = [tool for tool in ("pkgutil", "openssl") if shutil.which(tool) is None]
    if missing:
        raise VerificationError("macOS verification tool is missing: " + ", ".join(missing))


def write_provenance(
    output: Path,
    *,
    status: str,
    retrieved_at: str,
    product: dict[str, Any] | None,
    rows: list[dict[str, str]],
    errors: list[str],
) -> None:
    with (output / "provenance.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TSV_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    with (output / "provenance.txt").open("w", encoding="utf-8") as stream:
        stream.write("Darling CommandLineTools provenance evidence\n")
        stream.write(f"status: {status}\n")
        stream.write(f"retrieved_at_utc: {retrieved_at}\n")
        stream.write(f"catalog_source: {CATALOG_SOURCE_DESCRIPTION}\n")
        stream.write(f"darling_catalog_mirror_url: {DARLING_CATALOG_MIRROR_URL}\n")
        stream.write(f"runner_os: {os.environ.get('RUNNER_OS', platform.system())}\n")
        stream.write(f"runner_arch: {os.environ.get('RUNNER_ARCH', platform.machine())}\n")
        stream.write(f"runner_name: {os.environ.get('RUNNER_NAME', '')}\n")
        stream.write(f"github_run_id: {os.environ.get('GITHUB_RUN_ID', '')}\n")
        if product is not None:
            stream.write(f"product_key: {product.get('key', '')}\n")
            stream.write(f"product_tags: {json.dumps(product.get('tags', []), sort_keys=True)}\n")
        stream.write(f"status_policy: {EVIDENCE_COMPLETE} means evidence collection completed; it is not trust approval\n")
        trust_status = (
            SIGNATURE_VALID_NOT_REVIEWED
            if status == EVIDENCE_COMPLETE
            else EVIDENCE_INCOMPLETE
        )
        stream.write(f"trust_status: {trust_status}\n")
        stream.write("sha256_policy: observation only; no provider allowlist comparison performed\n")
        stream.write("api_sha1_policy: mismatch is recorded as stale_or_republished_catalog_metadata_unproven\n")
        for row in rows:
            stream.write("\n[package]\n")
            for field in TSV_FIELDS:
                stream.write(f"{field}: {row.get(field, '')}\n")
        if errors:
            stream.write("\n[errors]\n")
            for error in errors:
                stream.write(f"- {error}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="empty directory for provenance evidence")
    args = parser.parse_args()
    output = args.output
    if output.exists() and any(output.iterdir()):
        print(f"output directory is not empty: {output}", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    for directory in (output / "packages", output / "http-headers", output / "signatures"):
        directory.mkdir()

    retrieved_at = utc_now()
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    product: dict[str, Any] | None = None
    try:
        require_tools()
        product, packages = fetch_catalog(output)
        for package_id in EXPECTED_PACKAGE_IDS:
            try:
                rows.append(package_row(packages[package_id], output))
            except VerificationError as error:
                errors.append(str(error))
                rows.append(
                    {
                        "package_id": package_id,
                        "catalog_url": str(packages[package_id]["catalog_url"]),
                        "download_url": packages[package_id]["download_url"],
                    }
                )
    except VerificationError as error:
        errors.append(str(error))

    status = EVIDENCE_COMPLETE if not errors and len(rows) == len(EXPECTED_PACKAGE_IDS) else EVIDENCE_INCOMPLETE
    write_provenance(
        output,
        status=status,
        retrieved_at=retrieved_at,
        product=product,
        rows=rows,
        errors=errors,
    )
    if errors:
        print("CLT provenance verification failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(output / "provenance.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

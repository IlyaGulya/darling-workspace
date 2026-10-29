#!/usr/bin/env python3
"""Validate the real iosapp archive and retain a deterministic, unsigned diagnostic IPA.

This is the host artifact phase of xcodebuild-workload-run.sh, not a guest runner
or an Xcode export/signing substitute. No input product is repaired or generated.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import zipfile
from xml.parsers.expat import ExpatError

from closure_analyze import ARCH_CPUS, MH_MAGIC_64, macho_slices


APP_REL = Path("Products/Applications/iosapp.app")
ZIP_ROOT = "Payload/iosapp.app"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root: Path) -> list[dict]:
    """Include directory modes and symlink bytes without following tree links."""
    require(root.is_dir() and not root.is_symlink(), f"not a physical directory: {root}")
    rows = []
    for path in [root, *sorted(root.rglob("*"))]:
        mode = path.lstat().st_mode
        row = {"path": path.relative_to(root).as_posix(), "mode": mode}
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            require(not os.path.isabs(target), f"absolute symlink: {path}")
            require(path.resolve().is_relative_to(root.resolve()) and path.exists(),
                    f"dangling or escaping symlink: {path}")
            data = os.fsencode(target)
            row.update(kind="symlink", target=target, size=len(data),
                       sha256=hashlib.sha256(data).hexdigest())
        elif stat.S_ISREG(mode):
            row.update(kind="file", size=path.stat().st_size, sha256=sha256(path))
        else:
            require(stat.S_ISDIR(mode), f"unsupported archive entry: {path}")
            row.update(kind="directory", size=0)
        rows.append(row)
    return rows


def plist(path: Path) -> dict:
    require(path.is_file() and not path.is_symlink(), f"missing physical plist: {path}")
    with path.open("rb") as stream:
        value = plistlib.load(stream)
    require(isinstance(value, dict), f"plist is not a dictionary: {path}")
    return value


def version(value: str) -> int:
    require(isinstance(value, str), "deployment version is not a string")
    parts = value.split(".")
    require(1 <= len(parts) <= 3 and all(p.isdigit() for p in parts),
            f"invalid deployment version: {value!r}")
    numbers = [int(p) for p in parts] + [0] * (3 - len(parts))
    require(0 < numbers[0] <= 65535 and all(n <= 255 for n in numbers[1:]),
            f"invalid deployment version: {value!r}")
    return numbers[0] << 16 | numbers[1] << 8 | numbers[2]


def version_text(value: int) -> str:
    return f"{value >> 16}.{(value >> 8) & 255}.{value & 255}"


def validate_macho(path: Path, minimum: str) -> dict:
    # Reuse the repository's thin/universal slicing implementation; the guest
    # corpus header validator intentionally accepts only x86_64 guest programs.
    payload = path.read_bytes()
    slices = macho_slices(payload)
    require(len(slices) == 1, "iosapp must contain exactly one arm64 slice")
    image = slices[0]
    blob = image["blob"]
    require(len(blob) >= 32, "truncated Mach-O64 header")
    magic, cpu, subtype, filetype, ncmds, sizeofcmds, _, _ = struct.unpack_from("<IiiIIIII", blob)
    require(magic == MH_MAGIC_64 and cpu == ARCH_CPUS["arm64"] and filetype == 2,
            "product is not an arm64 MH_EXECUTE Mach-O64")
    require(not image["cputype"] or image["cputype"] == cpu, "universal CPU/header mismatch")
    end = 32 + sizeofcmds
    require(end <= len(blob), "load commands exceed Mach-O size")
    offset = 32
    builds, dylibs, text_ranges, entries = [], [], [], []
    for _ in range(ncmds):
        require(offset + 8 <= end, "truncated Mach-O load command")
        cmd, size = struct.unpack_from("<II", blob, offset)
        require(size >= 8 and size % 8 == 0 and offset + size <= end,
                "invalid Mach-O load command size")
        if cmd == 0x32:  # LC_BUILD_VERSION, platform 2 is device iOS, not simulator/Catalyst.
            require(size >= 24, "truncated LC_BUILD_VERSION")
            platform, minos, sdk, tools = struct.unpack_from("<4I", blob, offset + 8)
            require(size == 24 + tools * 8, "invalid LC_BUILD_VERSION tool table")
            require(platform == 2, f"wrong Mach-O platform: {platform}")
            require(minos == version(minimum) and sdk >= minos, "Mach-O deployment/SDK mismatch")
            builds.append({"platform": "iOS", "platform_id": platform,
                           "minimum_os": version_text(minos), "sdk": version_text(sdk)})
        elif cmd in (0x24, 0x25, 0x2F, 0x30):
            raise ValueError("unexpected legacy/conflicting deployment command")
        elif cmd in (0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023):
            require(size >= 24, "truncated dylib command")
            start = struct.unpack_from("<I", blob, offset + 8)[0]
            require(24 <= start < size, "dylib name escapes command")
            stop = blob.find(b"\0", offset + start, offset + size)
            require(stop >= 0, "unterminated dylib name")
            dylibs.append({"command": cmd, "name": blob[offset + start:stop].decode("utf-8")})
        elif cmd == 0x19:  # LC_SEGMENT_64
            require(size >= 72, "truncated segment")
            name, _, _, fileoff, filesize, _, _, sections, _ = struct.unpack_from("<16s4Q4I", blob, offset + 8)
            require(size == 72 + 80 * sections and fileoff + filesize <= len(blob),
                    "invalid segment/section file layout")
            if name.rstrip(b"\0") == b"__TEXT":
                require(filesize > 0, "empty __TEXT segment")
                text_ranges.append((fileoff, fileoff + filesize))
        elif cmd == 0x80000028:  # LC_MAIN
            require(size == 24, "invalid LC_MAIN")
            entries.append(struct.unpack_from("<Q", blob, offset + 8)[0])
        elif cmd == 0x1D:  # LC_CODE_SIGNATURE
            raise ValueError("unexpected code signature in unsigned diagnostic executable")
        offset += size
    require(offset == end, "load command count disagrees with sizeofcmds")
    require(len(builds) == 1, "expected one iOS LC_BUILD_VERSION")
    require(len(text_ranges) == 1 and len(entries) == 1
            and text_ranges[0][0] <= entries[0] < text_ranges[0][1], "invalid executable entry point")
    for framework in ("UIKit", "Foundation"):
        install_name = f"/System/Library/Frameworks/{framework}.framework/{framework}"
        require(any(d["name"] == install_name and d["command"] == 0xC for d in dylibs),
                f"missing strong {framework} linkage")
    return {"architecture": "arm64", "cpu_subtype": subtype, "filetype": "MH_EXECUTE",
            "deployment": builds[0], "dylibs": dylibs, "code_signature": "absent"}


def validate_archive(archive: Path) -> dict:
    info = plist(archive / "Info.plist")
    require(info.get("ArchiveVersion") == 2, "unsupported/missing ArchiveVersion")
    require(isinstance(info.get("CreationDate"), datetime.datetime), "missing archive CreationDate")
    require(info.get("Name") == "iosapp" and info.get("SchemeName") == "iosapp", "archive identity mismatch")
    props = info.get("ApplicationProperties")
    require(isinstance(props, dict), "missing archive ApplicationProperties")
    require(props.get("ApplicationPath") == "Applications/iosapp.app", "wrong archive ApplicationPath")
    applications = archive / "Products/Applications"
    require(applications.is_dir() and sorted(p.name for p in applications.iterdir()) == ["iosapp.app"],
            "archive must contain exactly Products/Applications/iosapp.app")
    app = archive / APP_REL
    app_rows = inventory(app)
    app_info = plist(app / "Info.plist")
    require(app_info.get("CFBundlePackageType") == "APPL", "product is not an application")
    require(app_info.get("LSRequiresIPhoneOS") is True
            and app_info.get("CFBundleSupportedPlatforms") == ["iPhoneOS"]
            and app_info.get("DTPlatformName") == "iphoneos", "wrong application platform metadata")
    require(app_info.get("CFBundleExecutable") == "iosapp", "wrong declared executable")
    for field in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion"):
        value = app_info.get(field)
        require(isinstance(value, str) and value and "$" not in value and props.get(field) == value,
                f"missing/unexpanded/mismatched {field}")
    require(version(app_info.get("MinimumOSVersion")) == version("17.0"), "wrong fixture deployment target")
    require(props.get("Architectures") == ["arm64"], "archive architectures are not arm64")
    require(not props.get("SigningIdentity"), "unexpected archive signing identity")
    require(not any(Path(row["path"]).name in ("_CodeSignature", "embedded.mobileprovision")
                    for row in app_rows), "unexpected signing/provisioning material")
    executable = app / app_info["CFBundleExecutable"]
    require(executable.is_file() and not executable.is_symlink()
            and executable.stat().st_mode & 0o111 != 0, "declared executable missing or not executable")
    return {"archive_version": info["ArchiveVersion"], "application_path": str(APP_REL),
            "bundle_identifier": app_info["CFBundleIdentifier"],
            "executable": app_info["CFBundleExecutable"],
            "macho": validate_macho(executable, app_info["MinimumOSVersion"]), "app_entries": app_rows}


def zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = mode << 16
    if stat.S_ISDIR(mode):
        info.external_attr |= 0x10
    return info


def package(app: Path, rows: list[dict], ipa: Path) -> list[dict]:
    expected = [{"path": "Payload/", "mode": stat.S_IFDIR | 0o755, "kind": "directory", "size": 0}]
    for row in rows:
        suffix = "" if row["path"] == "." else "/" + row["path"]
        name = ZIP_ROOT + suffix + ("/" if row["kind"] == "directory" else "")
        expected.append({**row, "path": name})
    with zipfile.ZipFile(ipa, "x", compression=zipfile.ZIP_STORED) as output:
        for row in expected:
            info = zip_info(row["path"], row["mode"])
            if row["kind"] == "file":
                relative = row["path"][len(ZIP_ROOT) + 1:]
                info.file_size = row["size"]
                with (app / relative).open("rb") as source, output.open(info, "w") as destination:
                    shutil.copyfileobj(source, destination)
            else:
                output.writestr(info, os.fsencode(row["target"]) if row["kind"] == "symlink" else b"")
    with zipfile.ZipFile(ipa) as output:
        infos = output.infolist()
        require([i.filename for i in infos] == [r["path"] for r in expected], "IPA entry list mismatch")
        require(output.testzip() is None, "IPA CRC validation failed")
        for info, row in zip(infos, expected):
            require(info.date_time == ZIP_DATE and info.create_system == 3
                    and info.external_attr >> 16 == row["mode"] and info.file_size == row["size"],
                    f"IPA metadata/mode mismatch: {info.filename}")
            with output.open(info) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            require(digest == row.get("sha256", hashlib.sha256(b"").hexdigest()),
                    f"IPA resource bytes mismatch: {info.filename}")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--build-log", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--prior-failures", default="")
    args = parser.parse_args()
    output = args.output_dir.absolute()
    report = {"schema": 1, "status": "FAIL", "boundary": "unsigned-diagnostic-only",
              "device_installable": False, "development_export": False,
              "source_archive": str(args.archive.absolute()), "prior_failures": args.prior_failures.strip()}
    # Refuse stale retention outputs rather than overwriting evidence or an IPA.
    require(output.is_dir() and not output.is_symlink() and not any(output.iterdir()),
            f"output directory is not fresh and empty: {output}")
    try:
        retained_log = output / "xcodebuild.out"
        shutil.copy2(args.build_log, retained_log)
        log = retained_log.read_text(errors="replace")
        report["build_log"] = {"path": str(retained_log), "sha256": sha256(retained_log),
                               "source": str(args.build_log.absolute())}
        retained_probe = output / "probe.sh"
        shutil.copy2(args.probe, retained_probe)
        report["probe"] = {"path": str(retained_probe), "sha256": sha256(retained_probe)}
        report["build_markers"] = {marker: any(line.startswith(marker) for line in log.splitlines())
                                   for marker in ("CompileC ", "Ld ", "** ARCHIVE SUCCEEDED **")}
        source_rows = inventory(args.archive)
        archive = output / "iosapp.xcarchive"
        shutil.copytree(args.archive, archive, symlinks=True)
        rows = inventory(archive)
        require(rows == source_rows, "retained archive differs from actual xcodebuild product")
        report["archive"] = {"path": str(archive), "entries": rows}
        validation = validate_archive(archive)
        report["validation"] = validation
        require(all(report["build_markers"].values()), "missing real compile/link/archive success output")
        require(not args.prior_failures.strip(), "workload/harness/license/cleanup did not pass")
        ipa = output / "iosapp.unsigned.ipa"
        report["ipa"] = {"path": str(ipa), "entries": package(archive / APP_REL, validation["app_entries"], ipa),
                         "sha256": sha256(ipa), "bytes": ipa.stat().st_size}
        manifest = output / "SHA256SUMS"
        checksums = [(row["sha256"], "iosapp.xcarchive/" + row["path"])
                     for row in rows if row["kind"] == "file"]
        checksums.append((report["ipa"]["sha256"], ipa.name))
        checksums.extend((sha256(path), path.name) for path in (retained_log, retained_probe))
        with manifest.open("x") as stream:
            for digest, path in checksums:
                stream.write(f"{digest}  {path}\n")
        report["checksums"] = {"path": str(manifest), "sha256": sha256(manifest)}
        report["status"] = "PASS"
    except (OSError, ValueError, TypeError, KeyError, struct.error, zipfile.BadZipFile, RuntimeError, ExpatError) as error:
        report["error"] = str(error)
    with (output / "validation.json").open("x") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"status": report["status"], "evidence": str(output / "validation.json")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

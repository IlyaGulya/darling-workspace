#!/usr/bin/env python3
"""Checksum operator Xcode inputs and build a public unsigned device diagnostic.

No publisher authentication, license acceptance, provisioning, archive/export,
private-project build, or device execution is performed by this command.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from closure_analyze import macho_slices
from west_commands.test_prefix import validate_borrowed_prefix_lease

_spec = importlib.util.spec_from_file_location("iosapp_package", ROOT / "scripts/iosapp-archive-package.py")
_pack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pack)
validate_app, inventory, package, sha256 = (_pack.validate_app, _pack.inventory, _pack.package, _pack.sha256)
require = _pack.require
TC = Path("Contents/Developer/Toolchains/XcodeDefault.xctoolchain")
SDK = Path("Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS.sdk")
TOOLS = {name: (TC / "usr/bin" / name).as_posix() for name in ("clang", "swiftc", "swift-frontend", "swift-stdlib-tool", "ld", "lipo")}
PROFILE = {"target": "arm64-apple-ios17.0", "minimum_os": "17.0", "platform": "iphoneos", "architecture": "arm64"}


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def tree_inventory(root: Path) -> list[dict]:
    """Hash physical entries, including link text, without following tree links."""
    require(root.is_dir() and not root.is_symlink(), "Xcode root must be a physical directory")
    rows = [{"path": ".", "kind": "directory", "mode": stat.S_IMODE(root.stat().st_mode)}]
    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            row = {"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(mode)}
            if stat.S_ISLNK(mode):
                row.update(kind="symlink", target=os.readlink(path))
            elif stat.S_ISREG(mode):
                row.update(kind="file", size=path.stat().st_size, sha256=sha256(path))
            else:
                require(stat.S_ISDIR(mode), f"unsupported input entry: {path}")
                row.update(kind="directory")
            rows.append(row)
            if stat.S_ISDIR(mode):
                visit(path)
    visit(root)
    return sorted(rows, key=lambda row: row["path"])


def inside(root: Path, relative: str | Path) -> Path:
    path = (root / relative).resolve(strict=True)
    require(path.is_relative_to(root), f"selected input escapes Xcode tree: {relative}")
    return path


def tool_identity(root: Path, relative: str) -> dict:
    path = inside(root, relative)
    require(path.is_file() and path.stat().st_mode & 0o111, f"tool is not executable: {relative}")
    payload = path.read_bytes()
    slices = macho_slices(payload)
    cpus = []
    for image in slices:
        blob = image["blob"]
        require(len(blob) >= 32, f"truncated tool: {relative}")
        magic, cpu, _, filetype = struct.unpack_from("<IiiI", blob)
        require(magic == 0xFEEDFACF and filetype == 2, f"not a Mach-O executable: {relative}")
        require(not image["cputype"] or image["cputype"] == cpu, "tool slice CPU mismatch")
        cpus.append(cpu)
    require(0x01000007 in cpus, f"tool lacks Darling x86_64 slice: {relative}")
    return {"path": relative, "resolved_path": path.relative_to(root).as_posix(),
            "cpus": cpus, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def validate_metadata(xcode: dict, sdk: dict) -> None:
    require(isinstance(xcode, dict) and isinstance(sdk, dict), "Xcode and SDK metadata must be dictionaries")
    for field in ("CFBundleShortVersionString", "ProductBuildVersion"):
        require(isinstance(xcode.get(field), str) and bool(xcode[field]), f"missing Xcode {field}")
    _pack.version(xcode["CFBundleShortVersionString"])
    require(_pack.version(sdk.get("Version")) >= _pack.version("17.0"), "SDK predates deployment target")
    require(sdk.get("CanonicalName") == "iphoneos" + sdk["Version"], "SDK canonical identity mismatch")
    properties, targets = sdk.get("DefaultProperties"), sdk.get("SupportedTargets")
    require(isinstance(properties, dict) and isinstance(targets, dict), "invalid SDK property/target dictionaries")
    require(properties.get("PLATFORM_NAME") == "iphoneos", "not a device SDK")
    target = targets.get("iphoneos")
    require(isinstance(target, dict), "missing device SDK target")
    require(target.get("LLVMTargetTripleSys") == "ios" and target.get("LLVMTargetTripleVendor") == "apple"
            and target.get("LLVMTargetTripleEnvironment") == "" and str(target.get("BuildVersionPlatformID")) == "2",
            "SDK target is not device iOS")
    architectures, deployments = target.get("Archs"), target.get("ValidDeploymentTargets")
    require(isinstance(architectures, list) and all(isinstance(value, str) for value in architectures)
            and isinstance(deployments, list) and all(isinstance(value, str) for value in deployments),
            "invalid SDK architecture/deployment lists")
    require("arm64" in architectures
            and _pack.version(PROFILE["minimum_os"]) in {_pack.version(value) for value in deployments},
            "SDK does not support arm64 iOS17.0")


def inspect_inputs(root: Path) -> dict:
    root = root.absolute()
    require(not root.is_symlink(), "Xcode root must not be a symlink")
    root = root.resolve(strict=True)
    with inside(root, "Contents/version.plist").open("rb") as stream:
        xcode = plistlib.load(stream)
    sdk = json.loads(inside(root, SDK / "SDKSettings.json").read_text())
    validate_metadata(xcode, sdk)
    tools = {name: tool_identity(root, rel) for name, rel in TOOLS.items()}
    rows = tree_inventory(root)
    return {"schema": "iosapp-direct-input-lock", "schema_version": 1,
            "source_kind": "operator-provided-xcode-tree", "publisher_authentication": "not-verified",
            "profile": PROFILE, "xcode": xcode, "sdk": sdk, "sdk_path": SDK.as_posix(),
            "tools": tools, "tree": {"sha256": hashlib.sha256(canonical(rows)).hexdigest(), "entries": rows}}


def validate_lock(lock: dict, actual: dict) -> None:
    require(type(lock) is dict and type(lock.get("schema_version")) is int and lock["schema_version"] == 1,
            "unsupported input lock version")
    require(lock.get("schema") == "iosapp-direct-input-lock" and lock.get("profile") == PROFILE,
            "unsupported input lock/profile")
    require(canonical(lock) == canonical(actual), "Xcode input identity changed or input lock was modified")


def fresh_output(output: Path, protected: list[Path]) -> None:
    require(output.is_absolute(), "output directory must be absolute")
    require(not output.exists() and not output.is_symlink(), "output directory already exists")
    resolved = output.resolve()
    for source in protected:
        source = source.resolve()
        require(not resolved.is_relative_to(source) and not source.is_relative_to(resolved),
                f"output overlaps protected input: {source}")


def guest_path(path: Path) -> str:
    return "/Volumes/SystemRoot" + str(path)


def command(argv: list[str]) -> str:
    return shlex.join(argv)


def runtime_metadata(app: Path) -> list[dict]:
    """Record actual copied Darwin runtime ABIs; never manufacture dylibs."""
    result = []
    frameworks = app / "Frameworks"
    for path in sorted(frameworks.rglob("*.dylib")):
        require(path.parent == frameworks and not path.is_symlink(),
                "embedded runtime must be a physical file directly in Frameworks")
        slices = macho_slices(path.read_bytes())
        identities = []
        for image in slices:
            blob = image["blob"]
            magic, cpu, subtype, kind, count, size = struct.unpack_from("<IiiIII", blob)
            require(magic == 0xFEEDFACF and cpu == 0x0100000C and kind == 6,
                    f"embedded runtime is not arm64 MH_DYLIB: {path.name}")
            offset, platforms, signatures = 32, [], []
            require(offset + size <= len(blob), "truncated runtime load commands")
            for _ in range(count):
                require(offset + 8 <= 32 + size, "truncated runtime command")
                cmd, length = struct.unpack_from("<II", blob, offset)
                require(length >= 8 and offset + length <= 32 + size, "invalid runtime command")
                if cmd == 0x32:
                    require(length >= 24, "truncated runtime platform")
                    platform, minimum, sdk = struct.unpack_from("<III", blob, offset + 8)
                    require(platform == 2 and minimum <= _pack.version("17.0"), "incompatible embedded runtime")
                    platforms.append({"platform": platform, "minimum": _pack.version_text(minimum), "sdk": _pack.version_text(sdk)})
                elif cmd == 0x25:
                    require(length == 16, "invalid runtime minimum")
                    minimum, sdk = struct.unpack_from("<II", blob, offset + 8)
                    require(minimum <= _pack.version("17.0"), "incompatible runtime minimum")
                    platforms.append({"platform": 2, "minimum": _pack.version_text(minimum), "sdk": _pack.version_text(sdk)})
                elif cmd == 0x1D:
                    require(length == 16, "invalid embedded runtime signature command")
                    signature_offset, signature_size = struct.unpack_from("<II", blob, offset + 8)
                    require(signature_offset + signature_size <= len(blob), "runtime signature exceeds file")
                    signatures.append({"offset": signature_offset, "size": signature_size})
                offset += length
            require(offset == 32 + size and len(platforms) == 1, "missing/ambiguous runtime platform")
            identities.append({"cpu": cpu, "subtype": subtype, "deployment": platforms[0],
                               "original_vendor_signatures": signatures, "signing_performed": False})
        require(len(identities) == 1, "embedded runtime must be thin arm64")
        result.append({"path": path.relative_to(app).as_posix(), "sha256": sha256(path), "slices": identities})
    return result


def build(args: argparse.Namespace) -> dict:
    # Keep raw DPREFIX bytes for the existing owner's no-follow/.. guard.
    prefix_raw, runtime_raw = os.environ.get("DPREFIX"), os.environ.get("DARLING_RUNTIME_ROOT")
    require(bool(prefix_raw) and bool(runtime_raw), "build requires explicit managed DPREFIX and DARLING_RUNTIME_ROOT")
    descriptor = validate_borrowed_prefix_lease(prefix_raw)
    if descriptor is None or descriptor != 8:
        # The shared controller validates an existing borrowed descriptor or acquires
        # the real exclusive lease and re-enters with its established FD8 ABI.
        argv = [sys.executable, "-B", str(ROOT / "west_commands/test_prefix.py"), "--prefix", prefix_raw,
                "--runtime-root", runtime_raw, "--", sys.executable, "-B", str(Path(__file__).resolve()), *sys.argv[1:]]
        if descriptor is not None:
            os.set_inheritable(descriptor, True)
        os.execv(sys.executable, argv)
    prefix, runtime = Path(prefix_raw), Path(runtime_raw)
    root = args.xcode_app.absolute()
    output = args.output_dir
    fresh_output(output, [root, prefix, runtime, args.input_lock, ROOT / "tests/xcodebuild_fixture"])
    lock = json.loads(args.input_lock.read_text())
    actual = inspect_inputs(root)
    validate_lock(lock, actual)
    output.mkdir(parents=True)
    write_json(output / "input-lock.json", actual)
    stage_name = "iosapp-direct-" + uuid.uuid4().hex
    stage = prefix / "private/var/tmp" / stage_name
    require(not stage.exists(), "guest stage already exists")
    stage.mkdir()
    guest = "/private/var/tmp/" + stage_name
    source_name = "main.m" if args.kind == "objc-uikit" else "App.swift"
    source = ROOT / "tests/xcodebuild_fixture" / ("iosapp/main.m" if args.kind == "objc-uikit" else "swiftui/App.swift")
    shutil.copyfile(source, stage / source_name)
    shutil.copyfile(source, output / source_name)
    app = stage / "iosapp.app"
    app.mkdir(mode=0o755)
    template = (ROOT / "tests/xcodebuild_fixture/iosapp/Info.plist").read_bytes()
    (output / "Info.source.plist").write_bytes(template)
    info = plistlib.loads(template)
    for field, variable, value in (
        ("CFBundleExecutable", "$(EXECUTABLE_NAME)", "iosapp"),
        ("CFBundleIdentifier", "$(PRODUCT_BUNDLE_IDENTIFIER)", "com.example.iosapp"),
        ("CFBundleName", "$(PRODUCT_NAME)", "iosapp"),
    ):
        if info.get(field) == variable:
            info[field] = value
    info.update(CFBundleSupportedPlatforms=["iPhoneOS"], DTPlatformName="iphoneos",
                MinimumOSVersion=PROFILE["minimum_os"], UIDeviceFamily=[1])
    with (app / "Info.plist").open("xb") as stream:
        plistlib.dump(info, stream, sort_keys=True)
    (app / "Info.plist").chmod(0o644)
    gx, gsdk = guest_path(root), guest_path(root / SDK)
    tools = {name: gx + "/" + rel for name, rel in TOOLS.items()}
    cache = guest + "/module-cache"
    common = ["-target", PROFILE["target"], "-isysroot", gsdk]
    if args.kind == "objc-uikit":
        compile_argv = [tools["clang"], *common, "-fobjc-arc", "-fmodules", "-fmodules-cache-path=" + cache,
                        "-ffile-prefix-map=" + guest + "=/iosapp-direct", "-c", source_name, "-o", "iosapp.o"]
        link_argv = [tools["clang"], *common, "iosapp.o", "-framework", "UIKit", "-framework", "Foundation",
                     "-Wl,-no_adhoc_codesign", "-o", "iosapp.app/iosapp"]
    else:
        swift_common = ["-target", PROFILE["target"], "-sdk", gsdk, "-module-cache-path", cache,
                        "-module-name", "iosapp", "-parse-as-library", "-debug-prefix-map", guest + "=/iosapp-direct"]
        compile_argv = [tools["swiftc"], *swift_common, "-emit-object", source_name, "-o", "iosapp.o"]
        # Swift 6.2.4 (swift-driver aedacc6c, DarwinToolchain+LinkerSupport.swift)
        # forwards -sdk as --sysroot. Clang's Darwin SDK metadata uses -isysroot,
        # as newer swift-driver does; forward the actual selected SDK explicitly.
        link_argv = [tools["swiftc"], *swift_common, "iosapp.o", "-framework", "SwiftUI", "-framework", "UIKit",
                     "-framework", "Foundation", "-Xlinker", "-no_adhoc_codesign",
                     "-Xclang-linker", "-isysroot", "-Xclang-linker", gsdk,
                     "-Xlinker", "-rpath", "-Xlinker", "@executable_path/Frameworks", "-o", "iosapp.app/iosapp"]
    steps = ["set -eu", "umask 022", "export DEVELOPER_DIR=" + shlex.quote(gx + "/Contents/Developer"),
             "export PATH=" + shlex.quote(gx + "/" + str(TC / "usr/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"),
             "export SOURCE_DATE_EPOCH=0",
             "unset SDKROOT SWIFT_EXEC SWIFT_DRIVER_SWIFT_FRONTEND_EXEC MACOSX_DEPLOYMENT_TARGET IPHONEOS_DEPLOYMENT_TARGET CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH OBJC_INCLUDE_PATH LIBRARY_PATH",
             "cd " + shlex.quote(guest),
             command([tools["clang" if args.kind == "objc-uikit" else "swiftc"], "--version"]),
             command(compile_argv), "echo DIRECT_COMPILE_DONE=1", command(link_argv), "echo DIRECT_LINK_DONE=1"]
    if args.kind == "swiftui":
        steps += ["mkdir iosapp.app/Frameworks", command([tools["swift-stdlib-tool"], "--copy", "--verbose",
                   "--scan-executable", "iosapp.app/iosapp", "--platform", "iphoneos",
                   "--source-libraries", gx + "/" + str(TC / "usr/lib/swift/iphoneos"),
                   "--destination", "iosapp.app/Frameworks"]),
                  "for library in iosapp.app/Frameworks/*.dylib; do",
                  '  [ -f "$library" ] || continue',
                  "  case $(" + command([tools["lipo"], "-archs"]) + ' "$library") in arm64) continue ;; esac',
                  "  " + command([tools["lipo"], "-thin", "arm64"]) + ' "$library" -output "$library.arm64"',
                  '  mv "$library.arm64" "$library"',
                  "done"]
    steps += ["echo DIRECT_BUILD_DONE=1"]
    # Always publish the workload's real status, even on compiler/linker failure.
    probe = "#!/bin/bash\n(\n" + "\n".join(steps) + "\n) >" + shlex.quote(guest + "/guest.out") + " 2>&1\nrc=$?\ncat " + shlex.quote(guest + "/guest.out") + "\necho DIRECT_RC=$rc\necho ORACLE_RC=$rc\necho DIRECT_PROBE_DONE=1\nexit $rc\n"
    (stage / "probe.sh").write_text(probe)
    (output / "probe.sh").write_text(probe)
    bundle = output / "bundle"
    harness = [str(ROOT / "scripts/darling-boot-run.sh"), "--wait", str(args.wait), "--marker", "DIRECT_COMPILE_DONE=1",
               "--marker", "DIRECT_LINK_DONE=1", "--marker", "DIRECT_BUILD_DONE=1", "--marker", "DIRECT_PROBE_DONE=1",
               "--status-marker", "DIRECT_RC=", "--status-marker", "ORACLE_RC=", "--log", str(output / "guest.log"), "--bundle", str(bundle),
               "--cmd", command(["/bin/bash", guest + "/probe.sh"]), "--assert-prefix-maps"]
    with (output / "harness.txt").open("x") as log:
        completed = subprocess.run(harness, stdout=log, stderr=subprocess.STDOUT, pass_fds=(descriptor,))
    if (stage / "guest.out").is_file():
        shutil.copyfile(stage / "guest.out", output / "guest.out")
    # Identity is checked even when the workload failed; retain all failure evidence.
    validate_lock(lock, inspect_inputs(root))
    require(completed.returncode == 0, f"guest harness failed: {completed.returncode}; evidence: {output}")
    cleanup = json.loads((bundle / "cleanup.json").read_text())
    require(cleanup.get("processes") == 0 and cleanup.get("mounts") == 0 and cleanup.get("shutdown_rc") == 0
            and cleanup.get("external_rescue") is False and cleanup.get("rescue_processes") == 0,
            "ordinary prefix cleanup was not demonstrated")
    require(re.search(r"(?m)^ORACLE_RC=0\s*$", (output / "guest.log").read_text()) is not None,
            "missing actual ORACLE_RC=0")
    guest_text = (output / "guest.out").read_text()
    for marker in ("DIRECT_COMPILE_DONE=1", "DIRECT_LINK_DONE=1", "DIRECT_BUILD_DONE=1"):
        require(marker in guest_text.splitlines(), f"missing workload completion: {marker}")
    require((stage / "iosapp.o").is_file(), "compiler produced no object")
    shutil.copyfile(stage / "iosapp.o", output / "iosapp.o")
    shutil.copytree(app, output / "iosapp.app", symlinks=True)
    retained = output / "iosapp.app"
    validation = validate_app(retained, kind=args.kind)
    require(_pack.version(validation["macho"]["deployment"]["sdk"]) == _pack.version(actual["sdk"]["Version"]),
            "executable SDK metadata does not match the locked SDK")
    embedded = runtime_metadata(retained)
    rows = inventory(retained)
    package(retained, rows, output / "iosapp.unsigned.ipa")
    report = {"schema_version": 1, "status": "PASS", "boundary": "unsigned-diagnostic-only",
              "method": "raw-appleclang" if args.kind == "objc-uikit" else "raw-swift",
              "kind": args.kind, "profile": PROFILE, "publisher_authentication": "not-verified",
              "input_tree_sha256": actual["tree"]["sha256"], "validation": validation,
              "embedded_runtime": embedded, "cleanup": cleanup, "device_execution": "not-performed",
              "object_sha256": sha256(output / "iosapp.o"), "executable_sha256": sha256(retained / "iosapp"),
              "source_sha256": sha256(output / source_name), "info_template_sha256": sha256(output / "Info.source.plist"),
              "ipa_sha256": sha256(output / "iosapp.unsigned.ipa"), "commands": [compile_argv, link_argv]}
    write_json(output / "validation.json", report)
    shutil.rmtree(stage)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    inspect = commands.add_parser("inspect", help="read-only checksum/provenance inspection; not publisher authentication")
    inspect.add_argument("--xcode-app", type=Path, required=True)
    inspect.add_argument("--output-lock", type=Path, required=True)
    compile_parser = commands.add_parser("build", help="raw compiler diagnostic in an explicitly selected managed prefix")
    compile_parser.add_argument("--xcode-app", type=Path, required=True)
    compile_parser.add_argument("--input-lock", type=Path, required=True)
    compile_parser.add_argument("--output-dir", type=Path, required=True)
    compile_parser.add_argument("--kind", choices=("objc-uikit", "swiftui"), default="objc-uikit")
    compile_parser.add_argument("--wait", type=int, default=300)
    args = parser.parse_args()
    try:
        if args.action == "inspect":
            fresh_output(args.output_lock, [args.xcode_app])
            write_json(args.output_lock, inspect_inputs(args.xcode_app))
            print(args.output_lock)
        else:
            require(args.wait > 0, "wait must be positive")
            print(json.dumps(build(args), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        print(f"iosapp-direct-build: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

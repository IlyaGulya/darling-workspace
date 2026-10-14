"""Provision Apple's Xcode system-resource frameworks the guest load ladder demands.

Xcode 26.3 ships `/Library/Developer/PrivateFrameworks/{CoreSimulator,DVTDownloads,CoreDevice,
CoreDeviceUtilities,ROCKit}.framework` OUTSIDE its bundle, in
`Contents/Resources/Packages/XcodeSystemResources.pkg`, which Xcode's installer unpacks into
`/Library/Developer/...` on a real Mac. This module is the declared, repeatable route that puts the
demanded subset of that payload at the same guest paths. It exists because the route cannot be a
hand copy:

  * the DEMANDED SUBSET is measured, not assumed. Enumerating the whole unsatisfied set of Xcode's
    `Contents/Frameworks/IDESimulatorFoundation.framework/Versions/A/IDESimulatorFoundation` against
    the deployed prefix (`scripts/closure_analyze.py --cover --order 8 --entry <plug-in>`) reports
    exactly ONE strong unmet IMAGE whose provider this tree does not implement -- `DVTDownloads` --
    and the package's own bundle inventory is the only source of it. `CoreDevice` is reached only by
    a WEAK load command (`XCTHarness`), which dyld binds to NULL rather than stopping on, and
    `CoreDeviceUtilities`/`ROCKit` are not reached at all. Installing the whole package would also
    put Apple's `CoreSimulator` where this tree's own build belongs, which is forbidden: the tree
    builds and deploys that one itself.
  * the guest-installer shape the CommandLineTools provider uses does not fit for the same two
    reasons: `installer(8)` has no per-member selection, and the package installs all five
    frameworks, one of which this tree implements. So this route reads the payload itself and places
    only the measured subset -- at the package's own paths, with the package's own modes and
    symlinks.
  * the INTEGRITY DISCIPLINE is the same one `west_commands/guest_toolchain.py` applies to a
    provisioned Apple package, and it runs BEFORE anything is written: the file's size, its XAR
    envelope boundaries, the SHA-1 of the COMPRESSED XAR TOC the envelope names, and a reviewed
    full-file SHA-256. Every digest is compared against a pinned value; a mismatch raises and no
    bytes are placed. Publisher authentication is a separate obligation from local hashing (the same
    distinction the CommandLineTools provider records).

The pinned digests below were computed from the local Xcode 26.3 copy named by `docs/environment.md`
(`/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app`); this is not publisher authentication.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
import zlib
from pathlib import Path
from typing import NamedTuple

from west.commands import WestCommand

DEFAULT_XCODE_APP = "/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app"

# The guest paths the loader is asked for come from the package's own bundle inventory
# (`grep -ao 'path="\./Library/Developer/PrivateFrameworks/[A-Za-z0-9_+.-]*\.framework' <pkg>`), which
# is also what a real Mac's install produces.
PACKAGE_RELATIVE_PATH = "Contents/Resources/Packages/XcodeSystemResources.pkg"

REVIEWED_PACKAGE_SIZE = 101283347
# The catalog digest of the COMPRESSED XAR TOC (SHA-1 over the bytes immediately after the XAR
# header). For a CommandLineTools package this value comes from Darling's distribution manifest; an
# Xcode .xip carries no per-package digests (`Metadata` names only the format and the unpacked
# size), so it is computed here and pinned, and the full-file SHA-256 below is the independent
# whole-file pin.
REVIEWED_PACKAGE_TOC_SHA1 = "638c2860ead3bf9ef4bab4f7a16b3496a3ddece9"
REVIEWED_PACKAGE_SHA256 = "cc84e1509d301d667ebea4fb1c7bfff039cb11a1a80b68437721dd68a81148a5"

RECEIPT_NAME = ".west-xcode-system-resources.json"


class XcodeSystemResourcesError(RuntimeError):
    """Raised when the payload cannot be authenticated or placed."""

    def __init__(self, message: str, *, kind: str = "integrity"):
        super().__init__(message)
        self.kind = kind


class DemandedFramework(NamedTuple):
    """One framework the enumeration demands, with the measurement that puts it in the subset.

    A NamedTuple rather than a dataclass because West loads an extension module by path without
    registering it in sys.modules, and dataclasses' string-annotation handling reads that entry.
    """

    name: str
    # Directory inside the package payload, relative to the payload root (no leading "./").
    payload_dir: str
    # sha256 of the framework's Versions/A image, as it stands in the package.
    image_sha256: str
    demand: str

    @property
    def guest_dir(self) -> str:
        return f"/Library/Developer/PrivateFrameworks/{self.name}.framework"

    @property
    def guest_image(self) -> str:
        return f"{self.guest_dir}/Versions/A/{self.name}"

    @property
    def payload_image(self) -> str:
        return f"{self.payload_dir}/Versions/A/{self.name}"


DEMANDED_FRAMEWORKS = (
    DemandedFramework(
        name="DVTDownloads",
        payload_dir="Library/Developer/PrivateFrameworks/DVTDownloads.framework",
        image_sha256="a896690f97949ffff502613d628ec9b3e15e310534bb0dec059d5bb09e1bf45b",
        demand=(
            "STRONG LC_LOAD_DYLIB of Xcode 26.3's IDESimulatorFoundation (compat 1.0.0 / current "
            "24250.0.0), 99 strong eager Swift binds from it; the only strong unmet image of the "
            "plug-in's 239-image load closure that this tree does not implement"
        ),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package(path: Path) -> dict[str, object]:
    """Authenticate the package before anything is read out of it or written anywhere.

    Same discipline as the CommandLineTools provider: size, XAR envelope boundaries, the compressed
    TOC SHA-1, then the reviewed full-file SHA-256.
    """

    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(28)
            if size != REVIEWED_PACKAGE_SIZE:
                raise XcodeSystemResourcesError(
                    f"{path} has size {size}, expected {REVIEWED_PACKAGE_SIZE}", kind="integrity"
                )
            if len(header) != 28 or header[:4] != b"xar!":
                raise XcodeSystemResourcesError(f"{path} is not a XAR package", kind="integrity")
            _, header_size, version, toc_compressed, toc_uncompressed, checksum = struct.unpack(
                ">4sHHQQI", header
            )
            if (
                version != 1
                or header_size < 28
                or toc_compressed == 0
                or toc_uncompressed == 0
                or checksum != 1
                or header_size + toc_compressed + 20 > size
            ):
                raise XcodeSystemResourcesError(
                    f"{path} has an invalid XAR header", kind="integrity"
                )
            # XAR hashes the compressed bytes immediately following its header. Stream them
            # without allocating/decompressing an untrusted TOC.
            digest = hashlib.sha1()
            stream.seek(header_size)
            remaining = toc_compressed
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise XcodeSystemResourcesError(
                        f"{path} has a truncated XAR TOC", kind="integrity"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
            toc_sha1 = digest.hexdigest()
    except OSError as error:
        raise XcodeSystemResourcesError(f"cannot inspect {path}: {error}", kind="integrity") from error

    if toc_sha1 != REVIEWED_PACKAGE_TOC_SHA1:
        raise XcodeSystemResourcesError(
            f"{path} has compressed XAR TOC SHA-1 {toc_sha1}, expected {REVIEWED_PACKAGE_TOC_SHA1}",
            kind="integrity",
        )
    actual_sha256 = _sha256(path)
    if actual_sha256 != REVIEWED_PACKAGE_SHA256:
        raise XcodeSystemResourcesError(
            f"{path} has unreviewed SHA-256 {actual_sha256}, expected {REVIEWED_PACKAGE_SHA256}",
            kind="integrity",
        )
    return {
        "path": str(path),
        "size": size,
        "toc_sha1": toc_sha1,
        "sha256": actual_sha256,
        "header_size": header_size,
        "toc_compressed": toc_compressed,
        "toc_uncompressed": toc_uncompressed,
    }


def _payload_bytes(path: Path, info: dict[str, object]) -> bytes:
    """Return the package's `Payload` file contents (the gzip-compressed cpio archive).

    The heap starts immediately after the header and the compressed TOC -- `xar_get_heap_offset()`
    is exactly `toc_length_compressed + header.size` (`xar/lib/util.c` in mackyle/xar) -- with no
    alignment padding. Member `<data><offset>` values are relative to that heap start and already
    include the heap's own first members, the TOC `<checksum>` and the `<signature>`; for
    `Payload` here that is 20 + 256 = 276. Adding the checksum length to the heap start on top of
    that offset reads every member 20 bytes past its own first byte, which is what made an earlier
    revision of this reader hand `gzip` the bytes following the payload instead of the payload.
    """

    header_size = int(info["header_size"])
    toc_compressed = int(info["toc_compressed"])
    with path.open("rb") as stream:
        stream.seek(header_size)
        toc = stream.read(toc_compressed)
        heap_start = header_size + toc_compressed
        root = ElementTree.fromstring(zlib.decompress(toc))
        # Prove the heap boundary before trusting any offset measured from it. A XAR's checksum
        # member holds the SHA-1 of the compressed TOC and sits at the heap's offset 0; a wrong
        # boundary would otherwise silently read neighbouring bytes.
        checksum = root.find("checksum")
        if checksum is not None and checksum.get("style") == "sha1":
            checksum_at = heap_start + int(checksum.findtext("offset", "0"))
            checksum_length = int(checksum.findtext("size", "0"))
            stream.seek(checksum_at)
            if stream.read(checksum_length) != hashlib.sha1(toc).digest():
                raise XcodeSystemResourcesError(
                    f"{path} does not hold its compressed-TOC checksum at the computed heap start "
                    f"({heap_start}); refusing to read member data at offsets measured from it",
                    kind="integrity",
                )
        for entry in root.iter("file"):
            if entry.findtext("name") != "Payload":
                continue
            data = entry.find("data")
            if data is None:
                raise XcodeSystemResourcesError(
                    f"{path} has no data for its Payload member", kind="format"
                )
            offset = int(data.findtext("offset"))
            length = int(data.findtext("length"))
            stream.seek(heap_start + offset)
            blob = stream.read(length)
            if len(blob) != length:
                raise XcodeSystemResourcesError(
                    f"{path} has a truncated Payload member", kind="format"
                )
            encoding = ""
            style = data.find("encoding")
            if style is not None:
                encoding = style.get("style", "")
            if "gzip" in encoding:
                blob = gzip.decompress(blob)
            return blob
    raise XcodeSystemResourcesError(f"{path} carries no Payload member", kind="format")


def _iter_cpio(archive: bytes):
    """Yield (name, mode, data_or_none, link_target) for a cpio archive.

    Two shapes occur in flat-package payloads. The "new ASCII" format (`070701`/`070702`, 110-byte
    header of 8-hex-digit fields, name and data padded to a 4-byte boundary) and the "old ASCII"
    (odc) format (`070707`, 76-byte header of 6-digit octal fields plus an 11-digit mtime and
    filesize, name and data unpadded). Xcode 26.3's XcodeSystemResources.pkg payload is odc; a
    reader that only knew newc rejected it as "not a cpio newc archive".
    """

    if archive[:6] == b"070707":
        yield from _iter_cpio_odc(archive)
        return
    position = 0
    while position + 110 <= len(archive):
        header = archive[position : position + 110]
        if header[:6] not in (b"070701", b"070702"):
            raise XcodeSystemResourcesError("payload is not a cpio newc archive", kind="format")
        fields = [int(header[6 + index * 8 : 14 + index * 8], 16) for index in range(13)]
        mode = fields[1]
        filesize = fields[6]
        namesize = fields[11]
        name_start = position + 110
        name = archive[name_start : name_start + namesize - 1].decode("utf-8", "surrogateescape")
        data_start = name_start + namesize
        data_start += (-data_start) % 4
        data = archive[data_start : data_start + filesize]
        position = data_start + filesize
        position += (-position) % 4
        if name == "TRAILER!!!":
            return
        link_target = None
        if stat.S_ISLNK(mode):
            link_target = data.decode("utf-8", "surrogateescape")
        yield name, mode, data, link_target


# The odc header's ten fields, in order, as octal-digit widths (mackyle/cpio's `odc` layout: dev,
# ino, mode, uid, gid, nlink, rdev, mtime, namesize, filesize).
_ODC_FIELD_WIDTHS = (6, 6, 6, 6, 6, 6, 6, 11, 6, 11)
_ODC_HEADER_SIZE = 6 + sum(_ODC_FIELD_WIDTHS)


def _iter_cpio_odc(archive: bytes):
    """Yield (name, mode, data_or_none, link_target) for a cpio odc (old ASCII) archive."""

    position = 0
    while position + _ODC_HEADER_SIZE <= len(archive):
        header = archive[position : position + _ODC_HEADER_SIZE]
        if header[:6] != b"070707":
            raise XcodeSystemResourcesError("payload is not a cpio odc archive", kind="format")
        values = []
        cursor = 6
        for width in _ODC_FIELD_WIDTHS:
            field = header[cursor : cursor + width]
            try:
                values.append(int(field, 8))
            except ValueError as error:
                raise XcodeSystemResourcesError(
                    f"cpio odc header carries a non-octal field {field!r}", kind="format"
                ) from error
            cursor += width
        mode = values[2]
        namesize = values[8]
        filesize = values[9]
        if namesize < 1:
            raise XcodeSystemResourcesError("cpio odc member has an empty name", kind="format")
        name_start = position + _ODC_HEADER_SIZE
        name = archive[name_start : name_start + namesize - 1].decode("utf-8", "surrogateescape")
        data_start = name_start + namesize
        data = archive[data_start : data_start + filesize]
        if len(data) != filesize:
            raise XcodeSystemResourcesError(
                f"cpio odc member {name!r} is truncated by {filesize - len(data)} bytes",
                kind="format",
            )
        # odc has no padding: the next header follows the data immediately.
        position = data_start + filesize
        if name == "TRAILER!!!":
            return
        link_target = None
        if stat.S_ISLNK(mode):
            link_target = data.decode("utf-8", "surrogateescape")
        yield name, mode, data, link_target
    while position + 110 <= len(archive):
        header = archive[position : position + 110]
        if header[:6] not in (b"070701", b"070702"):
            raise XcodeSystemResourcesError("payload is not a cpio newc archive", kind="format")
        fields = [int(header[6 + index * 8 : 14 + index * 8], 16) for index in range(13)]
        mode = fields[1]
        filesize = fields[6]
        namesize = fields[11]
        name_start = position + 110
        name = archive[name_start : name_start + namesize - 1].decode("utf-8", "surrogateescape")
        data_start = name_start + namesize
        data_start += (-data_start) % 4
        data = archive[data_start : data_start + filesize]
        position = data_start + filesize
        position += (-position) % 4
        if name == "TRAILER!!!":
            return
        link_target = None
        if stat.S_ISLNK(mode):
            link_target = data.decode("utf-8", "surrogateescape")
        yield name, mode, data, link_target


def _extract_framework(package: Path, info: dict[str, object], framework: DemandedFramework, staging: Path) -> list[dict[str, object]]:
    """Write the demanded framework's members from the payload into *staging*.

    Returns the per-file sha256 rows; symlink members are written as symlinks and recorded by target.
    """

    archive = gzip.decompress(_payload_bytes(package, info))
    prefix = f"./{framework.payload_dir}"
    staged: list[dict[str, object]] = []
    for name, mode, data, link_target in _iter_cpio(archive):
        if name != prefix and not name.startswith(prefix + "/"):
            continue
        relative = name[len(prefix) + 1 :] if name != prefix else ""
        destination = staging if not relative else staging / relative
        if stat.S_ISDIR(mode):
            destination.mkdir(parents=True, exist_ok=True)
            os.chmod(destination, stat.S_IMODE(mode))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISLNK(mode):
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            os.symlink(link_target, destination)
            staged.append({"path": relative, "type": "symlink", "target": link_target})
            continue
        with destination.open("wb") as output:
            output.write(data)
        os.chmod(destination, stat.S_IMODE(mode))
        staged.append(
            {
                "path": relative,
                "type": "file",
                "mode": oct(stat.S_IMODE(mode)),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    if not staged:
        raise XcodeSystemResourcesError(
            f"the package payload carries no member of {framework.payload_dir}", kind="format"
        )
    return staged


def _place_tree(source: Path, destination: Path) -> list[dict[str, object]]:
    """Copy a staged framework into *destination*, replacing whatever was there."""

    if destination.exists() or destination.is_symlink():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    rows: list[dict[str, object]] = []
    for path in sorted(destination.rglob("*")):
        relative = str(path.relative_to(destination))
        if path.is_symlink():
            rows.append({"path": relative, "type": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            rows.append(
                {"path": relative, "type": "file", "size": path.stat().st_size,
                 "sha256": _sha256(path)}
            )
    return rows


def prefix_roots(prefix: Path) -> tuple[Path, ...]:
    """The two runtime copies a Darling prefix keeps (the guest-visible root and libexec/darling).

    The guest-visible root shadows the other where both carry a name, and an absolute guest path
    resolves there; the libexec copy is the runtime closure the deploy tooling keeps. Both are
    written so the two copies cannot drift (the same rule ios-toolchain-runtime-additions.sh
    applies).
    """

    return (prefix, prefix / "libexec" / "darling")


def provision(
    *,
    prefix: Path,
    xcode_app: Path = Path(DEFAULT_XCODE_APP),
    package: Path | None = None,
    log=print,
) -> dict[str, object]:
    """Authenticate the package, place the demanded frameworks in BOTH prefix roots, write a receipt.

    Idempotent: a re-run with the same package and payload reproduces the same bytes.
    """

    resolved = package or (xcode_app / PACKAGE_RELATIVE_PATH)
    if not resolved.is_file():
        raise XcodeSystemResourcesError(
            f"no Xcode system-resources package at {resolved}", kind="setup"
        )
    info = validate_package(resolved)
    log(
        "xcode-system-resources: authenticated {path} (size {size}, compressed-TOC SHA-1 {toc_sha1}, "
        "SHA-256 {sha256})".format(**info)
    )

    receipt: dict[str, object] = {
        "package": info,
        "frameworks": [],
    }
    roots = prefix_roots(prefix)
    for framework in DEMANDED_FRAMEWORKS:
        with tempfile.TemporaryDirectory(prefix="west-xcode-system-resources-") as temporary:
            staging = Path(temporary) / f"{framework.name}.framework"
            staged = _extract_framework(resolved, info, framework, staging)
            image = staging / "Versions" / "A" / framework.name
            if not image.is_file():
                raise XcodeSystemResourcesError(
                    f"the payload's {framework.name} has no Versions/A/{framework.name}",
                    kind="format",
                )
            actual = _sha256(image)
            if actual != framework.image_sha256:
                raise XcodeSystemResourcesError(
                    f"{framework.name} payload image has sha256 {actual}, expected "
                    f"{framework.image_sha256}",
                    kind="integrity",
                )
            copies: list[dict[str, object]] = []
            for root in roots:
                destination = root / framework.guest_dir.lstrip("/")
                rows = _place_tree(staging, destination)
                placed = destination / "Versions" / "A" / framework.name
                placed_sha = _sha256(placed)
                if placed_sha != actual:
                    raise XcodeSystemResourcesError(
                        f"{destination} carries {placed_sha} for {framework.name}, not the payload's "
                        f"{actual}",
                        kind="integrity",
                    )
                log(
                    f"xcode-system-resources: {framework.name} -> {destination} "
                    f"({len(rows)} members, image sha256 {placed_sha})"
                )
                copies.append({"root": str(root), "path": str(destination), "members": rows,
                               "image_sha256": placed_sha})
            receipt["frameworks"].append(
                {
                    "name": framework.name,
                    "guest_image": framework.guest_image,
                    "demand": framework.demand,
                    "payload_image_sha256": actual,
                    "payload_members": staged,
                    "copies": copies,
                }
            )

    pending = prefix / f"{RECEIPT_NAME}.pending"
    with pending.open("w") as output:
        json.dump(receipt, output, indent=2, sort_keys=True)
        output.write("\n")
    pending.replace(prefix / RECEIPT_NAME)
    log(f"xcode-system-resources: receipt {prefix / RECEIPT_NAME}")
    return receipt


GUEST_CHECK_SOURCE = "tests/xcode_system_resources_load_guest.c"
GUEST_CHECK_TRANSPORT = "testkit/scripts/run-darling-c-test.sh"
GUEST_CHECK_MARKER = "XCODE_SYSTEM_RESOURCES_VERDICT=GREEN"


def guest_loader_check(
    *,
    workspace: Path,
    prefix: Path,
    launcher: Path,
    source: Path | None = None,
    guest_cc: str | None = None,
    guest_cflags: str | None = None,
    log=print,
) -> str:
    """Run the guest-side check that the loader finds each provisioned framework.

    The check is a guest C fixture run through the workspace's own guest C transport
    (testkit/scripts/run-darling-c-test.sh), which owns upload, in-guest compilation, the timeout,
    and the ok-marker; a compiler inside the guest is therefore required, exactly as it is for the
    ladder's other guest oracles. Returns the transport's combined output.
    """

    fixture = source or (workspace / GUEST_CHECK_SOURCE)
    transport = workspace / GUEST_CHECK_TRANSPORT
    if not fixture.is_file():
        raise XcodeSystemResourcesError(f"guest check fixture missing: {fixture}", kind="setup")
    if not transport.is_file():
        raise XcodeSystemResourcesError(f"guest check transport missing: {transport}", kind="setup")
    environment = dict(os.environ)
    environment["DPREFIX"] = str(prefix)
    environment["DARLING_PREFIX"] = str(prefix)
    environment["DARLING_LAUNCHER"] = str(launcher)
    if guest_cc:
        environment["DARLING_GUEST_CC"] = guest_cc
    if guest_cflags:
        environment["DARLING_GUEST_CFLAGS"] = guest_cflags
    command = [
        "bash",
        str(transport),
        "--name",
        "xcode_system_resources_load_guest",
        "--source",
        str(fixture),
        "--launcher",
        str(launcher),
        "--ok-marker",
        GUEST_CHECK_MARKER,
    ]
    log(f"xcode-system-resources: guest check {transport.name} {fixture.name}")
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    output = result.stdout + result.stderr
    if result.returncode != 0 or GUEST_CHECK_MARKER not in output:
        raise XcodeSystemResourcesError(
            "the guest-side loader check did not pass "
            f"(rc={result.returncode}):\n{output[-4000:]}",
            kind="guest-check",
        )
    log("xcode-system-resources: guest-side loader check PASSED")
    return output


class DarlingXcodeSystemResources(WestCommand):
    def __init__(self):
        super().__init__(
            "darling-xcode-system-resources",
            "Provision Apple's Xcode system-resource frameworks into a Darling prefix",
            "Authenticate Xcode's XcodeSystemResources.pkg and place the demanded frameworks at "
            "their guest paths in BOTH prefix roots, then check them from inside the guest",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, description=self.description)
        parser.add_argument(
            "--prefix", required=True, help="Darling prefix to provision"
        )
        parser.add_argument(
            "--xcode-app",
            default=DEFAULT_XCODE_APP,
            help="Xcode application whose XcodeSystemResources.pkg is the payload source",
        )
        parser.add_argument(
            "--package",
            default="",
            help="explicit package path (default: <xcode-app>/Contents/Resources/Packages/XcodeSystemResources.pkg)",
        )
        parser.add_argument(
            "--workspace",
            default=str(Path(__file__).resolve().parent.parent),
            help="workspace root holding tests/ and testkit/ (for the guest check)",
        )
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="authenticate the package and report; write nothing",
        )
        parser.add_argument(
            "--no-guest-check",
            action="store_true",
            help="skip the guest-side loader check (prefix need not be bootable)",
        )
        parser.add_argument(
            "--guest-cc",
            default="",
            help="guest C compiler for the check (default: the transport's, "
            "i.e. the guest CommandLineTools clang)",
        )
        parser.add_argument(
            "--guest-cflags",
            default="",
            help="guest C flags for the check (default: the transport's)",
        )
        return parser

    def do_run(self, args, unknown):
        prefix = Path(args.prefix).expanduser().resolve()
        package = Path(args.package).expanduser() if args.package else None
        try:
            if args.check_only:
                resolved = package or (
                    Path(args.xcode_app).expanduser() / PACKAGE_RELATIVE_PATH
                )
                info = validate_package(resolved)
                self.inf(
                    "authenticated {path}: size {size}, compressed-TOC SHA-1 {toc_sha1}, "
                    "SHA-256 {sha256}".format(**info)
                )
                return
            provision(
                prefix=prefix,
                xcode_app=Path(args.xcode_app).expanduser(),
                package=package,
                log=self.inf,
            )
            if not args.no_guest_check:
                guest_loader_check(
                    workspace=Path(args.workspace).expanduser(),
                    prefix=prefix,
                    launcher=prefix / "bin" / "darling",
                    guest_cc=args.guest_cc or None,
                    guest_cflags=args.guest_cflags or None,
                    log=self.inf,
                )
        except XcodeSystemResourcesError as error:
            self.die(f"{error.kind}: {error}")

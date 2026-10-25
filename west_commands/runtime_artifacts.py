"""One table of where every Darling runtime artifact lives, and one install/verify path.

WHY THIS MODULE EXISTS. Two tools used to own this knowledge: `west darling-build --deploy`
knew the closure/boot-chain artifacts, and `scripts/darling-deploy-verify.sh` knew the native
components and the fact that several of them exist in TWO runtime copies. A component that only
the second tool knew (CoreFoundation, Foundation, libsystem_kernel, ...) could therefore not be
deployed through the supported command, and the script installed without the transaction/receipt
the command has. Both now use this table.

The rule the table encodes, measured the hard way: a component is loaded from a path that is not
the one that was deployed unless EVERY copy is replaced, because the loader takes dyld from
`<prefix>/libexec/usr/lib/dyld` while guest processes are launched through `mldr`, and several
components exist under both `usr/...` and `libexec/darling/usr/...`. A destination whose parent
directory does not exist is SKIPPED and reported; it is never created from a guess.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Component:
    name: str
    source: str                      # path relative to the build directory
    destinations: tuple[str, ...]    # paths relative to the prefix


# Component name -> (build-relative source, prefix-relative destinations).
# The sources and destinations are exactly the ones scripts/darling-deploy-verify.sh used;
# that script is now a thin wrapper over this module.
COMPONENTS: dict[str, Component] = {
    "mldr": Component(
        "mldr",
        "src/startup/mldr/mldr",
        ("usr/libexec/darling/mldr", "libexec/darling/usr/libexec/darling/mldr"),
    ),
    "dyld": Component(
        "dyld",
        "src/external/dyld/dyld",
        ("usr/lib/dyld", "libexec/darling/usr/lib/dyld"),
    ),
    # Like libobjc, this dylib had no component, so a libmalloc change could not be installed by tooling.
    "libsystem_malloc": Component(
        "libsystem_malloc",
        "src/external/libmalloc/libsystem_malloc.dylib",
        ("usr/lib/system/libsystem_malloc.dylib",
         "libexec/darling/usr/lib/system/libsystem_malloc.dylib"),
    ),
    "libsystem_kernel": Component(
        "libsystem_kernel",
        "src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib",
        ("usr/lib/system/libsystem_kernel.dylib",
         "libexec/darling/usr/lib/system/libsystem_kernel.dylib"),
    ),
    "libunwind": Component(
        "libunwind",
        "src/external/libunwind/libunwind.dylib",
        ("usr/lib/system/libunwind.dylib", "libexec/darling/usr/lib/system/libunwind.dylib"),
    ),
    "libxpc": Component(
        "libxpc",
        "src/external/libxpc/libxpc.dylib",
        ("usr/lib/system/libxpc.dylib", "libexec/darling/usr/lib/system/libxpc.dylib"),
    ),
    "libnetwork": Component(
        "libnetwork",
        "src/external/libnetwork/libnetwork.dylib",
        ("usr/lib/libnetwork.dylib", "libexec/darling/usr/lib/libnetwork.dylib"),
    ),
    "libmdns": Component(
        "libmdns",
        "src/external/mDNSResponder/mDNSMacOSX/mdns_objects/libmdns.dylib",
        ("usr/lib/libmdns.dylib", "libexec/darling/usr/lib/libmdns.dylib"),
    ),
    "corefoundation": Component(
        "corefoundation",
        "src/external/corefoundation/CoreFoundation",
        ("System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
         "libexec/darling/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"),
    ),
    "foundation": Component(
        "foundation",
        "src/external/foundation/Foundation",
        ("System/Library/Frameworks/Foundation.framework/Versions/C/Foundation",
         "libexec/darling/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"),
    ),
    "cfnetwork": Component(
        "cfnetwork",
        "src/external/cfnetwork/src/CFNetwork",
        ("System/Library/Frameworks/CFNetwork.framework/Versions/A/CFNetwork",
         "libexec/darling/System/Library/Frameworks/CFNetwork.framework/Versions/A/CFNetwork"),
    ),
    # The Foundation XPC daemon. The source bootstrap manifest now carries its entrypoint; this
    # component installs the already-built target into an existing prefix and verifies both copies.
    "filecoordinationd": Component(
        "filecoordinationd",
        "src/external/foundation/services/filecoordinationd/filecoordinationd",
        ("usr/sbin/filecoordinationd", "libexec/darling/usr/sbin/filecoordinationd"),
    ),
    # xcodebuild's ProcessProductPackagingDER invokes /usr/bin/derq. Under the
    # EUNION/no-overlay canonical boot both the guest-visible usr/bin copy and
    # the libexec copy must be present; installing only the CMake destination
    # left the guest path absent. Keep both copies in the typed table.
    "derq": Component(
        "derq",
        "src/external/security/SecurityTool/macOS/derq",
        ("usr/bin/derq", "libexec/darling/usr/bin/derq"),
    ),
    # libobjc.A.dylib was absent from this table, so an objc4 change could not be deployed by any
    # tooling path (`west darling-build --deploy` does not carry it either): the prefix kept whatever
    # copy an earlier bootstrap had installed, and a measured objc4 fix could read as "no effect".
    "libobjc": Component(
        "libobjc",
        "src/external/objc4/runtime/libobjc.A.dylib",
        ("usr/lib/libobjc.A.dylib", "libexec/darling/usr/lib/libobjc.A.dylib"),
    ),
    "objc-trampolines": Component(
        "objc-trampolines",
        "src/external/objc4/runtime/libobjc-trampolines.dylib",
        ("usr/lib/libobjc-trampolines.dylib", "libexec/darling/usr/lib/libobjc-trampolines.dylib"),
    ),
    # The CONCRETE version directory in both roots: the framework's top-level name is a symlink in
    # the libexec root and absent in the guest-visible one, so installing into it would destroy the
    # symlink or create a second, loadable-looking copy.
    "mobiledevice": Component(
        "mobiledevice",
        "src/private-frameworks/MobileDevice/MobileDevice",
        ("System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice",
         "libexec/darling/System/Library/PrivateFrameworks/MobileDevice.framework/Versions/A/MobileDevice"),
    ),
    "coresimulator": Component(
        "coresimulator",
        "src/private-frameworks/CoreSimulator/CoreSimulator",
        ("Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/CoreSimulator",
         "libexec/darling/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/CoreSimulator"),
    ),
    "oah": Component(
        "oah",
        "src/liboah/liboah.dylib",
        ("usr/lib/liboah.dylib", "libexec/darling/usr/lib/liboah.dylib"),
    ),
    "darlingserver": Component(
        "darlingserver",
        "src/external/darlingserver/darlingserver",
        ("bin/darlingserver",),
    ),
    "shellspawn": Component(
        "shellspawn",
        "src/shellspawn/shellspawn",
        ("usr/libexec/shellspawn", "libexec/darling/usr/libexec/shellspawn"),
    ),
    "sudo": Component(
        "sudo",
        "src/tools/sudo",
        ("usr/bin/sudo", "libexec/darling/usr/bin/sudo"),
    ),
    "vchroot": Component(
        "vchroot",
        "src/vchroot/vchroot",
        ("usr/libexec/darling/vchroot", "libexec/darling/usr/libexec/darling/vchroot"),
    ),
    "launchd": Component(
        "launchd",
        "src/launchd/src/launchd",
        ("sbin/launchd",),
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_and_verify(prefix: Path, build_dir: Path, names: list[str], out=sys.stdout) -> int:
    """Install each component into every existing destination and verify the bytes.

    Returns 0 when every destination that exists carries the built file, 1 on a mismatch or
    install failure, 2 for an unknown component or a source that was never built.
    """
    rc = 0
    for name in names:
        component = COMPONENTS.get(name)
        if component is None:
            print(f"{name}: unknown component", file=sys.stderr)
            rc = max(rc, 2)
            continue

        source = build_dir / component.source
        if not source.is_file():
            print(f"{name}: NOT BUILT: {source}", file=sys.stderr)
            rc = max(rc, 2)
            continue

        want = sha256_file(source)
        for relative in component.destinations:
            destination = prefix / relative
            if not destination.parent.is_dir():
                print(f"{name}: skip (no directory): {relative}", file=out)
                continue
            try:
                shutil.copyfile(source, destination)
                os.chmod(destination, 0o755)
            except OSError as exc:
                print(f"{name}: INSTALL FAILED {relative}: {exc}", file=sys.stderr)
                rc = max(rc, 1)
                continue
            got = sha256_file(destination)
            if got == want:
                print(f"{name}: ok   {want[:16]}  {relative}", file=out)
            else:
                print(
                    f"{name}: MISMATCH built={want[:16]} deployed={got[:16]}  {relative}",
                    file=sys.stderr,
                )
                rc = max(rc, 1)
    return rc


def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Install built Darling runtime components into every runtime copy and verify them"
    )
    parser.add_argument("--prefix", required=True, help="Darling prefix to install into")
    parser.add_argument("--build", required=True, help="Darling build directory")
    parser.add_argument(
        "--component",
        action="append",
        default=[],
        help="component to install (repeatable); default: every known component",
    )
    args = parser.parse_args(argv)

    names = args.component or list(COMPONENTS)
    return install_and_verify(Path(args.prefix), Path(args.build), names)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

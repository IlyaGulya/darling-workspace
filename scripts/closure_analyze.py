#!/usr/bin/env python3
"""Per-dependency closure analysis of the Xcode load-time ladder, with implementation status.

Derived from the scratch analyzer written during the 25-step load-time walk (/tmp/closure.py,
/tmp/analyze*.py).  Its classification lessons are kept here verbatim because they are the
reason this tool exists at all:

WHEN A HAND COUNT AND THIS SCRIPT DISAGREE, THIS SCRIPT WINS UNTIL THE HAND COUNT IS RE-DERIVED
NAME BY NAME.  That rule is here because it was learned twice in one walk: a hand count attributed
libswiftAppleArchive's symbols to libAppleArchive, and a second one attributed kperfdataDT's 29
symbols to kperf, making a complete framework look like an implementation task.  Both were substring
errors; this script classifies per DEPENDENCY FIELD of the bind table, which is what got both cases
right.  Do not "fix" the heuristic by widening its substring matching -- widen it by making it more
bind-table-driven, per dependency.

WHAT THIS REPORTS, per component:

  * the exact install name, taken from the deployment declaration (guest_path), never assembled;
  * weak/strong, taken from each referrer's LC_LOAD_DYLIB attributes;
  * the required symbols, grouped by the EXACT dependency field of the referrer's bind table (a
    symbol is required by this component only when the bind row's dylib column equals this
    component's own column name -- never when the symbol text merely contains a similar name);
  * implementation status against the tree, decided mechanically from the built artifact.

IMPLEMENTATION STATUS.  Loading successfully is NOT implementing an API, and no component here may
be called fully supported.  A component that loads satisfies a load command; what the client gets
when it uses the image is a separate fact, and this script measures that fact:

  REAL            the walk wrote a real implementation (source added by the ladder's commits) whose
                  demanded symbols have behaviour (they call into something).
  EXISTING        the ladder's commits did not write implementation source for this component; the
                  tree already had a real implementation and the walk only deployed/declared it.
  DELIBERATE      the walk wrote implementation source and every demanded symbol it answers is a
                  truthful headless answer: a data symbol, a function whose body makes no call and
                  returns absence (zero) or nothing at all, or an Objective-C class whose method
                  list is EMPTY.  Truthful is not a stub: a placeholder stands in for work that does
                  not exist, whereas "no GPU, so no device" is the truth about this guest.
  STUB            the deployment is a stub or is incomplete, i.e. any of: a demanded symbol is not
                  exported by the built artifact; a demanded symbol is named in a printed
                  "STUB: <name> called" string; a demanded Objective-C class is a forwarding stub
                  (its method list is exactly -methodSignatureForSelector:/-forwardInvocation:); or
                  the artifact exports no symbol at all (a load-name-only placeholder image).

Every signal behind a class is printed, so the class is reproducible from this script's own output
rather than from memory.  One limit is stated rather than hidden: the tree also contains SILENT
stubs -- a body that neither prints "STUB:" nor calls anything but hands back a made-up constant
(AppleSystemInfo's ASI_NumberOfCPUs returns 1 with a "// stub" comment).  Those are reported as
"value: no call but returns a value, not absence" and are not counted as deliberate headless
behaviour, but the script cannot tell a made-up constant from a genuine one; it can only refuse to
call it absence.

WHERE THE DEPLOYMENT DECLARATIONS COME FROM (no hand-written list anywhere in this file):
  * the generated manifest (<build>/darling-rootless-toolchain.json) is the DECLARATION SOURCE
    (dar-a0ty) as well as the artifact source: it is the resolved deployment declaration a prefix
    is actually filled from, and a component whose declaration only exists as a generated resource
    -- the globbed Swift payload (`swift_libswiftX_dylib`) or a globbed payload framework
    (`swift_framework_X`) -- is named by it and nowhere in the CMake text.  A payload-declared
    target therefore needs no hand entry to be analyzed;
  * the CMake text's own JSON snippets (appended to DARLING_ROOTLESS_TOOLCHAIN_TARGETS' component in
    <darling>/CMakeLists.txt) are read as a SECOND source, and the two are reconciled rather than
    either being trusted silently: a target the text declares but the manifest does not carry is a
    deployment gap (the prefix will not receive it), and a target only the manifest carries is a
    generated/payload declaration.  `--declarations` prints both directions;
  * LOCK_TARGETS is RUNG ORDER ONLY -- the order the ladder measured -- never a declaration list;
  * provenance: git, over the ladder's own commit range (default: everything after the parent of
    the oldest reachable commit whose subject starts with "rootless toolchain").

WHAT `--sweep` IS FOR.  Walking the ladder one rung at a time costs one measured run per rung.  The
analyzer already knows the whole load-command closure and every install name it fails to resolve, so
`--sweep` groups that whole missing set by the route that would supply each one:

  xcode-provided   not a prefix gap: the guest reaches it through Xcode's own tree via the mount
  no-referrer      not a blocker: nothing in the closure loads it
  weak-only        not a blocker: every referrer links it weakly
  declaration      the tree already builds it; declare and deploy (no new source)
  payload          the payload carries it, or an overlay recipe in the overlay checkout builds it
  source           an open-source tree exists here, holds an implementation of a demanded name,
                   and nothing builds or declares it
  substitute       no source anywhere -- a truthful substitute is the only honest answer

Supplying a wave and re-running the ladder once replaces several rung-by-rung turns.  Every row prints
the install name, the referrers with weak/strong, the demanded symbol count, and the evidence the
classification rests on; routes marked [heuristic] rest on a name match rather than on the manifest's
own declaration, and are to be confirmed by hand before acting.

Read-only: this script never writes into the source or build trees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys

DEFAULTS = {
    "darling": "/home/ilyagulya/work/wr-fresh/darling",
    "build": "/home/ilyagulya/work/wr-fresh/build/verify14",
    "prefix": "/home/ilyagulya/work/wr-fresh/prefix-verify14",
    "xcode": "/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents",
    "swift_bin": "/home/ilyagulya/work/downloads/swift-linux/swift-6.3.1-RELEASE-ubuntu24.04/usr/bin",
}

# Xcode subtrees that hold the guest's load-time clients.  Scanning only these keeps referrer
# discovery cheap; every client the ladder measured lives in one of them.
XCODE_SCAN_ROOTS = [
    "Frameworks",
    "SharedFrameworks",
    "PrivateFrameworks",
    "PlugIns",
    "Applications",
    "Developer/Library/PrivateFrameworks",
]

# RUNG ORDER ONLY (dar-a0ty).  This is the sequence the ladder actually measured, kept so
# `closure_analyze.py` with no --components analyzes the same components in the same order.  It is
# NOT a declaration list: every component below is resolved to its declaration through the manifest
# and the CMake text (see `declaration_sources`), so a component only the manifest declares -- a
# globbed Swift-payload dylib, for instance -- needs no entry of its own here to be analyzable, and
# an entry here declares nothing.
LOCK_TARGETS = [
    "UniformTypeIdentifiers", "Rosetta", "MobileAsset", "CoreData", "ImageIO", "CoreAnalytics",
    "AssertionServices", "CoreGraphics", "DiskArbitration", "DiskManagement", "AppleSystemInfo",
    "AssetCacheServices", "DiskImages", "CrashReporterSupport", "compression", "AppleArchive",
    "PlugInKit", "libdtrace", "NetworkStatistics", "kperf", "StreamingZip", "Metal",
    # The rung above was measured as the MetalNoGPU declaration (that is the rung comment kept in
    # the tree); it is named by the target that DECLARES the guest Metal path now, because the same
    # rung's measurement showed the placeholder was shadowing the real Metal this configuration
    # builds and that QuartzCore links. See the MetalNoGPU/Metal selection in the tree's CMakeLists.
    "LoggingSupport", "QuartzCore", "CoreLocation",
    # The rung after CoreLocation: DVTInstrumentsFoundation's load command for
    # /usr/lib/swift/libswiftAppleArchive.dylib (image not found, XCB_RC=132).  Its declaration is a
    # Swift-payload RESOURCE rather than a $<TARGET_FILE:...> target, because the whole libswift*.dylib
    # payload is globbed and staged by configure_file in the tree's own CMakeLists; the manifest
    # therefore carries this component's target/guest_path/host_path and the analysis takes the
    # declaration from there (see main: manifest declarations).
    "swift_libswiftAppleArchive_dylib",
    # The rung after libswiftAppleArchive: DVTInstrumentsFoundation's load command for
    # /usr/lib/swift/libswiftSystem.dylib (image not found, XCB_RC=132).  Same payload-resource shape
    # as the entry above, and for the same reason: the libswift*.dylib payload is globbed and staged
    # by configure_file, so the CMake text declares no $<TARGET_FILE:...> for it and the analysis
    # takes this component's declaration from the generated manifest (see main).
    "swift_libswiftSystem_dylib",
    # The rung after libswiftSystem: XCTHarness's load command for
    # /usr/lib/swift/libswiftDemangle.dylib (image not found, XCB_RC=132).  XCTHarness is the only
    # referrer inside libxcodebuildLoader's load closure and it links the image NON-weakly, binding
    # three C entry points eagerly (__DATA_CONST,__got).  Same payload-resource shape as the two
    # entries above, and for the same reason (see main: manifest declarations).
    "swift_libswiftDemangle_dylib",
    # The rung after libswiftDemangle: XCTHarness's load command for
    # /usr/lib/swift/libswiftUniformTypeIdentifiers.dylib (image not found, XCB_RC=132).  XCTHarness is
    # the only referrer inside libxcodebuildLoader's load closure that is still missing a STRONG
    # dependency there -- CoreImage, IOKit, Metal, OSLog, QuartzCore and simd are LC_LOAD_WEAK_DYLIB --
    # and it binds TEN names from the dependency field, nine strong.  Same payload-resource shape as the
    # three entries above, and for the same reason (see main: manifest declarations).
    "swift_libswiftUniformTypeIdentifiers_dylib",
]

GUEST_XCODE_MOUNT_MARK = "Xcode.app/Contents"

CLASS_SHORT = {
    "REAL": "REAL        ",
    "EXISTING": "EXISTING    ",
    "DELIBERATE": "DELIBERATE  ",
    "STUB": "STUB/INCOMPL",
}
CLASS_LEGEND = [
    ("REAL", "REAL implementation"),
    ("EXISTING", "EXISTING implementation newly deployed"),
    ("DELIBERATE", "DELIBERATE truthful headless behaviour"),
    ("STUB", "STUB-or-incomplete"),
]
CLASS_CRITERIA = [
    "STUB         any demanded symbol absent from the deployed artifact / named in a printed",
    "             \"STUB: <name> called\" string / a demanded ObjC class that is a forwarding stub",
    "             (method list = methodSignatureForSelector:+forwardInvocation:) / no exports at all",
    "DELIBERATE   the walk wrote the source and every demanded symbol it answers is absence-shaped",
    "             (returns zero, or a void body, or a data symbol, or a method-free class)",
    "REAL         the walk wrote the source and a demanded symbol it answers returns a value or calls",
    "EXISTING     the walk wrote no source answering a demanded symbol; declaration only",
]


def run(args, timeout=300):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except Exception:
        return ""


# --------------------------------------------------------------------------- declarations

DECL_RE = re.compile(
    r'"target":\s*"([^"]+)"\s*,\s*"guest_path":\s*"([^"]+)"\s*,\s*"host_path":\s*"([^"]+)"'
)
GENERATOR_FILE_RE = re.compile(r"^\$<TARGET_FILE:([^>]+)>$")
# The same declaration in the pair spelling a foreach expands: "target|/guest/path".
PAIR_DECL_RE = re.compile(r'"([^"|${}]+)\|(/[^"|${}]+)"')
TARGETS_RE = re.compile(r"set\(DARLING_ROOTLESS_TOOLCHAIN_TARGETS(.*?)\n\)", re.S)


def read_declarations(darling):
    """(declared targets, {declared target: declaration}, files read) from the tree's CMake text.

    Two spellings the tree uses for a deployment declaration are read, because reading only one of
    them made the reconciliation report lie: the literal JSON snippets (`"target": ..., "guest_path":
    ..., "host_path": "$<TARGET_FILE:...>"`), and the `<target>|<guest_path>` pair lists a foreach
    expands into exactly those snippets (`cmake/RootlessHomebrew.cmake`'s userland and resource
    lists).  Templates that carry `${...}` in the target position (the locale resources) declare
    nothing literal and are deliberately not guessed at.
    """
    path = os.path.join(darling, "CMakeLists.txt")
    paths = [path]
    cmake_dir = os.path.join(darling, "cmake")
    if os.path.isdir(cmake_dir):
        paths += [os.path.join(cmake_dir, f) for f in sorted(os.listdir(cmake_dir))
                  if f.endswith(".cmake")]
    targets = []
    decls = {}
    for p in paths:
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        # The declarations live inside CMake string literals, so their quotes are escaped.
        text = text.replace('\\"', '"')
        m = TARGETS_RE.search(text)
        if m:
            for line in m.group(1).splitlines():
                line = line.split("#")[0].strip()
                if line:
                    targets.append(line)
        for tgt, guest, host in DECL_RE.findall(text):
            # A template that carries ${...} declares nothing literal; skip it rather than report
            # `${_homebrew_target}` as a declaration the manifest is missing.
            if "${" in tgt or "${" in guest or "${" in host:
                continue
            gen = GENERATOR_FILE_RE.match(host)
            decls.setdefault(tgt, {"target": tgt, "install_name": guest,
                                   "cmake_target": gen.group(1) if gen else tgt,
                                   "where": os.path.relpath(p, darling)})
        for tgt, guest in PAIR_DECL_RE.findall(text):
            decls.setdefault(tgt, {"target": tgt, "install_name": guest, "cmake_target": tgt,
                                   "where": os.path.relpath(p, darling)})
    return targets, decls, path


def read_manifest(build):
    path = os.path.join(build, "darling-rootless-toolchain.json")
    if not os.path.isfile(path):
        return None, None
    data = json.load(open(path))
    by_target = {}
    by_install = {}
    for entry in data.get("entrypoints", []) + data.get("resources", []):
        by_target.setdefault(entry["target"], entry)
        by_install.setdefault(entry["guest_path"], entry)
    return (by_target, by_install), path


def deployed_path(prefix, install_name):
    """The prefix's own copy of an install name, or None: both roots the deploy fills are searched."""
    if not prefix or not install_name:
        return None
    for base in (prefix, os.path.join(prefix, "libexec/darling")):
        p = os.path.join(base, install_name.lstrip("/"))
        if os.path.isfile(p):
            return p
    return None


def deployed_state(prefix, install_name, artifact):
    """Whether the prefix carries this install name, and whether that copy is the built artifact."""
    p = deployed_path(prefix, install_name)
    if not p:
        return "-", None
    if not artifact:
        return "present", p
    try:
        a = hashlib.sha256(open(p, "rb").read()).hexdigest()
        b = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
    except OSError:
        return "present", p
    return ("same" if a == b else "differs"), p


def resolve_decl(name, decls):
    """Find the declaration a requested component name refers to.

    The tree keys some declarations by a descriptor rather than the CMake target
    (`framework_UniformTypeIdentifiers` for target `UniformTypeIdentifiers`), so resolve by declared
    name, then CMake target, then install-name stem.
    """
    if name in decls:
        return name, decls[name]
    for key, decl in decls.items():
        if decl["cmake_target"] == name:
            return key, decl
    for key, decl in decls.items():
        if os.path.basename(decl["install_name"]).split(".")[0] == name:
            return key, decl
        if key.endswith("_" + name):
            return key, decl
    return None, None


def resolve_artifact(manifest, decl):
    if not manifest:
        return None
    by_target, by_install = manifest
    entry = by_target.get(decl["cmake_target"]) or by_target.get(decl["target"]) \
        or by_install.get(decl["install_name"])
    if entry and os.path.isfile(entry["host_path"]):
        return entry["host_path"]
    return None


# --------------------------------------------------------------------------- declaration sources

CMAKE_BUILD_TARGET_RE = re.compile(
    r"^\s*(add_library|add_executable|add_framework|add_separated_framework|add_darling_library|"
    r"add_darling_executable|add_darling_static_library|add_darling_object_library|add_bundle|"
    r"add_zsh_module|add_ruby_bundle\w*)"
    r"\s*\(\s*([A-Za-z0-9_.+-]+)")
SWIFT_OVERLAY_PREFIX = "libswift"


def read_cmake_build_targets(darling, max_depth=6):
    """name -> "relative/CMakeLists.txt:line" for every build target in the tree's CMake text.

    This is the tree's own answer to "does anything here build it", which is what makes a component
    a DECLARATION rung (deploy what already builds) rather than a source-authoring rung.  It is read
    from the CMake text rather than from a configured build tree so the classification works on a
    machine with no build directory.
    """
    paths = []
    root = os.path.join(darling, "CMakeLists.txt")
    if os.path.isfile(root):
        paths.append(root)
    src = os.path.join(darling, "src")
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if os.path.relpath(dirpath, darling).count(os.sep) >= max_depth:
            dirnames[:] = []
        if "CMakeLists.txt" in filenames:
            paths.append(os.path.join(dirpath, "CMakeLists.txt"))
    targets = {}
    for path in sorted(paths):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            m = CMAKE_BUILD_TARGET_RE.match(line)
            if m:
                targets.setdefault(m.group(2), f"{os.path.relpath(path, darling)}:{lineno} "
                                               f"{m.group(1)}({m.group(2)})")
    return targets


def other_component_declarations(build):
    """target -> guest_path for the OTHER rootless components' manifests in this build.

    A CMake declaration of another component (the bootstrap manifest: darling, mldr, launchd, ...)
    is not this component's gap, so it is subtracted from the "text only" direction instead of being
    reported as one.
    """
    out = {}
    path = os.path.join(build, "darling-rootless-bootstrap.json")
    if os.path.isfile(path):
        try:
            data = json.load(open(path))
        except (OSError, ValueError):
            return out
        for entry in data.get("entrypoints", []) + data.get("resources", []):
            out[entry["target"]] = entry["guest_path"]
    return out


def declaration_sources(darling, build):
    """The reconciled declaration set, and both directions of the disagreement.

    The MANIFEST is the declaration source (dar-a0ty): it is the resolved declaration a prefix is
    filled from, and the only place a generated/payload declaration exists (the globbed Swift
    payload `swift_libswiftX_dylib`, a globbed payload framework `swift_framework_X`).  The CMake
    text is read as well, and the two are compared instead of either being trusted silently:
    a text declaration the manifest does not carry is a DEPLOYMENT GAP, and a manifest declaration
    the text does not carry is a GENERATED/PAYLOAD declaration.
    """
    targets, text_decls, cmake_path = read_declarations(darling)
    manifest, manifest_path = read_manifest(build)
    declared = {}
    if manifest:
        by_target, _by_install = manifest
        for tgt, entry in by_target.items():
            declared[tgt] = {"target": tgt, "install_name": entry["guest_path"], "cmake_target": tgt,
                             "source": "manifest", "host_path": entry["host_path"]}
    manifest_only = sorted(t for t in declared if t not in text_decls)
    # A manifest-only declaration is not automatically suspicious: the manifest is composed AFTER
    # the CMake text from two generators the text cannot express, so those two are counted and not
    # listed, and anything else is listed as unexplained (that is the set worth reading).
    manifest_only_kind = {"payload": [], "staged": [], "unexplained": []}
    for tgt in manifest_only:
        if tgt.startswith("swift_"):
            manifest_only_kind["payload"].append(tgt)
        elif tgt.startswith("homebrew-perl:"):
            manifest_only_kind["staged"].append(tgt)
        else:
            manifest_only_kind["unexplained"].append(tgt)
    text_only_all = sorted((t, d) for t, d in text_decls.items() if t not in declared)
    other = other_component_declarations(build)
    text_elsewhere = [(t, d) for t, d in text_only_all if t in other]
    text_only = [(t, d) for t, d in text_only_all if t not in other]
    for tgt, decl in text_decls.items():
        # The CMake text names the real CMake target in its $<TARGET_FILE:...>, which the manifest
        # may not carry under that name (its entry is keyed by the declaration's descriptor).
        merged = dict(decl, source="cmake-text")
        declared.setdefault(tgt, merged)
    return {"decls": declared, "declaration_targets": targets, "cmake_path": cmake_path,
            "declared_by_text": sorted(text_decls), "manifest": manifest,
            "manifest_path": manifest_path, "manifest_only": manifest_only,
            "manifest_only_kind": manifest_only_kind, "text_only": text_only,
            "text_elsewhere": text_elsewhere}


def report_declarations(sources, darling):
    """The declaration reconciliation, in both directions (dar-a0ty)."""
    cmake_targets = read_cmake_build_targets(darling)
    kinds = sources["manifest_only_kind"]
    print("closure_analyze.py --declarations -- where a component's deployment declaration comes from")
    print(f"  manifest (declaration source) : {sources['manifest_path']} "
          f"({len(sources['decls'])} declarations)")
    print(f"  CMake text ({len(sources['declared_by_text'])} declarations) : "
          f"{sources['cmake_path']} and cmake/*.cmake "
          f"({len(sources['declaration_targets'])} targets in DARLING_ROOTLESS_TOOLCHAIN_TARGETS)")
    print(f"  CMake build targets           : {len(cmake_targets)} add_library/add_executable names "
          f"under {darling}")
    print()
    by_both = len(sources["decls"]) - len(sources["manifest_only"])
    print(f"declared by BOTH the manifest and the CMake text : {by_both}")
    print(f"declared ONLY by the manifest ({len(sources['manifest_only'])}) -- generated/payload "
          "declarations; no hand entry is needed for these:")
    print(f"  {len(kinds['payload']):>5} glob-generated payload declaration(s) "
          "(CONFIGURE_DEPENDS over src/external/swift), the dar-a0ty case:")
    for tgt in kinds["payload"]:
        print(f"        {tgt:<52} {sources['decls'][tgt]['install_name']}")
    print(f"  {len(kinds['staged']):>5} staged by cmake/stage-homebrew-perl.py (composed into the "
          "manifest after the CMake text)")
    print(f"  {len(kinds['unexplained']):>5} unexplained -- in the manifest, in no CMake text, and "
          "not one of the two known generators:")
    for tgt in kinds["unexplained"]:
        print(f"        {tgt:<52} {sources['decls'][tgt]['install_name']}")
    print()
    print(f"declared ONLY by the CMake text ({len(sources['text_only'])}) -- the manifest does not "
          "carry them, so a prefix filled from it would NOT receive them (a deployment gap, or a "
          "manifest older than the text):")
    for tgt, decl in sources["text_only"]:
        print(f"  {tgt:<52} {decl['install_name']}  ({(decl.get('where') or '')})")
    print(f"declared by the CMake text for ANOTHER rootless component "
          f"({len(sources['text_elsewhere'])}): "
          + ", ".join(t for t, _ in sources["text_elsewhere"]) + " -- not this component's gap")
    print()
    print("LOCK_TARGETS is rung order only and declares nothing here; each name in it resolves "
          "through the declarations above.")
    missing = [t for t in LOCK_TARGETS if resolve_decl(t, sources["decls"])[1] is None]
    if missing:
        print(f"NEXT: these LOCK_TARGETS entries resolve to NO declaration: {', '.join(missing)}")
    return 0


def name_candidates(install_name):
    """Names an install name may be known by in this tree, most specific first.

    One thing has three names here: the guest install name
    (/usr/lib/swift/libswiftUniformTypeIdentifiers.dylib), the payload file of the same name, and a
    CMake target or overlay directory with a shorter name (UniformTypeIdentifiers, exslt, xslt).
    The candidates exist so a lookup can try each of them EXPLICITLY rather than matching by
    substring; `target_name_candidates` drops the shortened ones for /usr/lib/swift, where the short
    name belongs to the framework and not to the Swift overlay, and both the build-target and the
    manifest-declaration lookups go through it.
    """
    leaf = os.path.basename(install_name)
    stem = leaf.split(".")[0]
    out = [leaf, stem]
    for part in install_name.split("/"):
        if part.endswith(".framework"):
            out.append(part[: -len(".framework")])
    if stem.startswith(SWIFT_OVERLAY_PREFIX) and len(stem) > len(SWIFT_OVERLAY_PREFIX):
        out.append(stem[len(SWIFT_OVERLAY_PREFIX):])
    if stem.startswith("lib") and len(stem) > 3:
        out.append(stem[3:])
    return [c for i, c in enumerate(out) if c and c not in out[:i]]


def target_name_candidates(install_name):
    """The candidates a BUILD TARGET or a deployment DECLARATION may match this install name by.

    For /usr/lib/swift/libswiftX.dylib only the full `libswiftX` name counts: a target or a
    declaration named X builds/deploys the X framework, NOT the Swift overlay over X, and accepting
    the shortened candidate would be the same collision this module's docstring records twice --
    once for a build target and once for the manifest's install-name stem (dar-xovc).
    """
    stem = os.path.basename(install_name).split(".")[0]
    if "/usr/lib/swift/" in install_name and stem.startswith(SWIFT_OVERLAY_PREFIX):
        return [stem]
    return name_candidates(install_name)


def build_target_for(install_name, cmake_targets):
    """(target, "CMakeLists.txt:line") the tree builds for an install name, or (None, None).

    For /usr/lib/swift/libswiftX.dylib only the full `libswiftX` name is accepted: a target named
    `X` builds the X framework, not the Swift overlay over X, and accepting it would be the same
    kind of substring error this module's docstring records twice.
    """
    for c in target_name_candidates(install_name):
        if c in cmake_targets:
            return c, cmake_targets[c]
    return None, None


def payload_index(payload, overlay=None):
    """What the payload supplies: (artifacts by stem, overlay recipe dirs by name).

    `payload` is the directory whose committed files the deploy stages (libswift*.dylib,
    *.framework); `overlay` is the overlay checkout that holds the recipe directories
    (overlays/<Name>/), and defaults to <payload>/overlays.
    """
    artifacts, overlays = {}, {}
    if payload and os.path.isdir(payload):
        for name in sorted(os.listdir(payload)):
            path = os.path.join(payload, name)
            if name.endswith(".dylib") and os.path.isfile(path):
                artifacts.setdefault(name.split(".")[0], path)
            elif name.endswith(".framework") and os.path.isdir(path):
                stem = name[: -len(".framework")]
                binary = os.path.join(path, "Versions/A", stem)
                if os.path.isfile(binary):
                    artifacts.setdefault(stem, binary)
    if overlay is None:
        overlay = os.path.join(payload, "overlays") if payload else None
    if overlay and os.path.isdir(overlay):
        for name in sorted(os.listdir(overlay)):
            if os.path.isdir(os.path.join(overlay, name)):
                overlays.setdefault(name, os.path.join(overlay, name))
    return artifacts, overlays


def source_index(darling, max_depth=3):
    """Directory name -> path for every directory under src/, shallowest wins."""
    index = {}
    src = os.path.join(darling, "src")
    for dirpath, dirnames, _files in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        if os.path.relpath(dirpath, darling).count(os.sep) >= max_depth:
            dirnames[:] = []
        for name in dirnames:
            index.setdefault(name, os.path.relpath(os.path.join(dirpath, name), darling))
    return index


def symbol_source_name(sym):
    """The text a definition of `sym` carries in source: the linker name minus its prefixes."""
    name = sym[1:] if sym.startswith("_") else sym
    if name.startswith("OBJC_CLASS_$_"):
        name = name[len("OBJC_CLASS_$_"):]
    return name


def source_implementation_for(path, demanded):
    """(file, symbol) -- an implementation source file under `path` that names a demanded symbol.

    A matched DIRECTORY is not an implementation (dar-xovc).  `src/softlinking/include/SoftLinking`
    holds two macro headers, and `src/frameworks/Accelerate` is the C umbrella under a Swift overlay
    name, so the search that finds a real definition must reject both.  A header is excluded on
    purpose -- a declaration is exactly the promise this route must not make -- and the scan stops
    at the first implementation file (a compiler input) that carries a demanded name.
    """
    if not os.path.isdir(path):
        return None, None
    named = sorted((sym, symbol_source_name(sym)) for sym in demanded)
    named = [(sym, name) for sym, name in named if name]
    if not named:
        return None, None
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for fn in sorted(filenames):
            if not fn.endswith(SOURCE_IMPL_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for sym, name in named:
                if name in text:
                    return full, sym
    return None, None


# --------------------------------------------------------------------------- wave sweep routes

ROUTE_XCODE = "xcode-provided"
ROUTE_ORPHAN = "no-referrer"
ROUTE_WEAK = "weak-only"
ROUTE_DECL = "declaration"
ROUTE_PAYLOAD = "payload"
ROUTE_SOURCE = "source"
ROUTE_SUBSTITUTE = "substitute"
ROUTE_ORDER = (ROUTE_XCODE, ROUTE_ORPHAN, ROUTE_WEAK, ROUTE_DECL, ROUTE_PAYLOAD, ROUTE_SOURCE,
               ROUTE_SUBSTITUTE)
ROUTE_HEADLINE = {
    ROUTE_XCODE: "NOT A PREFIX GAP -- Xcode's own tree supplies it through the host-root mount",
    ROUTE_ORPHAN: "NOT A BLOCKER -- no image in the closure demands it (only its own install name)",
    ROUTE_WEAK: "NOT A BLOCKER -- every referrer links it weakly; excluded from the wave",
    ROUTE_DECL: "DECLARATION -- the tree already builds it; declare/deploy (no new source)",
    ROUTE_PAYLOAD: "PAYLOAD DEPLOY -- the payload carries it or an overlay recipe builds it",
    ROUTE_SOURCE: "BUILD FROM SOURCE -- an open-source tree here implements it; nothing builds it yet",
    ROUTE_SUBSTITUTE: "TRUTHFUL SUBSTITUTE ONLY -- no source for it anywhere in this workspace",
}


def xcode_provides(install_name, xcode, cache=None):
    """(provided, evidence) -- whether the guest's dyld finds this image inside Xcode's tree.

    Strict on purpose.  Only two shapes count:
      * a path that already names the Xcode mount -- the guest reaches Xcode's files through the
        host-root mount, so /Volumes/SystemRoot/.../Xcode.app/Contents/... is real;
      * an @rpath/ reference, which dyld resolves against the referrer's rpaths, and every referrer
        in this closure lives inside Xcode.app, so those rpaths are inside Xcode's tree.  A bare
        @rpath dylib (Xcode's XCTest support libraries, the loader itself) needs a bounded basename
        search, cached for the whole sweep.
    An ABSOLUTE guest path (/System/..., /usr/...) is NOT satisfiable by Xcode's tree: the guest's
    /System and /usr come from the prefix (libexec/darling), which is why the ladder exists.  Xcode
    does ship images with colliding basenames -- its own libswiftIOKit.dylib lives under
    Toolchains/XcodeDefault.xctoolchain/usr/lib/swift-5.0/macosx -- and reporting the guest's
    /usr/lib/swift/libswiftIOKit.dylib as supplied by that copy would be exactly the false negative
    this tool exists to avoid.
    """
    if GUEST_XCODE_MOUNT_MARK in install_name:
        tail = install_name.split(GUEST_XCODE_MOUNT_MARK, 1)[1]
        path = os.path.join(xcode, tail.lstrip("/"))
        if os.path.isfile(path):
            return True, f"Xcode mount path: {os.path.relpath(path, xcode)}"
    if not install_name.startswith("@rpath/"):
        return False, ""
    path = xcode_file(install_name, xcode)
    if path:
        return True, f"Xcode's own tree: {os.path.relpath(path, xcode)}"
    rel = install_name[len("@rpath/"):]
    direct = os.path.join(xcode, rel)
    if os.path.isfile(direct):
        return True, f"Xcode's own tree: {rel}"
    if cache is None:
        cache = {}
    leaf = os.path.basename(install_name)
    fw = rel.split("/", 1)[0]
    if fw.endswith(".framework"):
        # A framework nested in another bundle (ContentDeliveryServices.framework/Versions/A/
        # Frameworks/AppStoreService.framework) or in Xcode's Developer/Library: the referrer's own
        # @loader_path rpaths reach it, so a bounded search for the bundle is the honest check.
        name = fw[: -len(".framework")]
        key = f"fw:{name}"
        if key not in cache:
            out = run(["find", xcode, "-name", fw, "-type", "d", "-maxdepth", "10"], timeout=120)
            cache[key] = sorted(line for line in out.splitlines() if line.strip())
        for found in cache[key]:
            for shape in ("Versions/A/" + name, name):
                if os.path.isfile(os.path.join(found, shape)):
                    return True, (f"Xcode's own tree: {os.path.relpath(os.path.join(found, shape), xcode)}")
        return False, ""
    if not leaf.endswith(".dylib"):
        return False, ""
    if leaf not in cache:
        out = run(["find", xcode, "-name", leaf, "-type", "f"], timeout=120)
        cache[leaf] = [line for line in out.splitlines() if line.strip()]
    found = cache[leaf]
    if found:
        return True, (f"Xcode's own tree ({len(found)} copy/copies), e.g. "
                      f"{os.path.relpath(found[0], xcode)}")
    return False, ""


def classify_context(darling, manifest, xcode, payload=None, overlay=None):
    """The facts classify_route rests on, gathered once for the sweep or for a single rung."""
    payload = payload or os.path.join(darling, "src/external/swift")
    provider_cache = {}
    return {
        "manifest": manifest,
        "darling": darling,
        "cmake_targets": read_cmake_build_targets(darling),
        "payload": payload_index(payload, overlay),
        "sources": source_index(darling),
        "payload_dir": payload,
        "overlay_dir": overlay or os.path.join(payload, "overlays"),
        "xcode_provides": lambda name: xcode_provides(name, xcode, provider_cache),
    }


def classify_route(install_name, refs, ctx, demanded=None):
    """(route, evidence) for one install name nothing in the prefix carries.

    Order: weak-only first (a missing name behind only weak links cannot stop the loader), then the
    manifest's own declaration, then the tree's build targets, then the payload's artifacts and
    overlay recipes, then a source tree that nothing builds but that DOES hold an implementation of
    a demanded name, and finally the honest answer when none of those exist.

    The source step requires an implementation artefact (dar-xovc): a directory whose name matches a
    candidate is not a build.  A header-only tree (SoftLinking) and a same-named umbrella for
    another image (the C Accelerate tree under the Swift overlay's name) have nothing to build, so
    they are SUBSTITUTE with that fact recorded rather than SOURCE.
    """
    if not refs:
        return ROUTE_ORPHAN, ("no image in the closure demands it: the only load command naming it "
                              "is its own LC_ID_DYLIB")
    if all(r["weak"] for r in refs):
        return ROUTE_WEAK, ("every referrer names it LC_LOAD_WEAK_DYLIB: "
                            + ", ".join(os.path.basename(r["image"]) for r in refs))
    candidates = name_candidates(install_name)
    man = ctx.get("manifest")
    by_install = man[1] if man else {}
    if install_name in by_install:
        entry = by_install[install_name]
        return ROUTE_DECL, (f"[manifest] target={entry['target']} host_path={entry['host_path']}")
    for stem, entry in sorted(by_install.items()):
        if os.path.basename(stem).split(".")[0] in target_name_candidates(install_name):
            return ROUTE_DECL, (f"[heuristic: install-name stem] manifest guest_path={stem} "
                                f"target={entry['target']}")
    target, where = build_target_for(install_name, ctx.get("cmake_targets", {}))
    if target:
        return ROUTE_DECL, (f"[heuristic: name match] the tree builds it: {where} (matched target "
                            f"'{target}')")
    artifacts, overlays = ctx.get("payload", ({}, {}))
    for c in candidates:
        if c in artifacts:
            return ROUTE_PAYLOAD, f"payload artifact {artifacts[c]} (matched candidate '{c}')"
    for c in candidates:
        if c in overlays:
            return ROUTE_PAYLOAD, (f"overlay recipe {overlays[c]} (matched candidate '{c}') "
                                   "-- in the overlay checkout, not deployed")
    darling = ctx.get("darling", "")
    sources = ctx.get("sources", {})
    if demanded is None:
        demanded = demand_tables(install_name, refs)[0]
    same_named = []
    for c in candidates:
        rel = sources.get(c)
        if not rel:
            continue
        found, sym = source_implementation_for(os.path.join(darling, rel), demanded)
        if found:
            return ROUTE_SOURCE, (
                f"source tree {rel} exists (matched candidate '{c}') and "
                f"{os.path.relpath(found, darling)} defines demanded symbol {sym}; no declaration "
                "or build target names it")
        same_named.append((c, rel))
    if same_named:
        c, rel = same_named[0]
        return ROUTE_SUBSTITUTE, (
            "no manifest declaration, no CMake build target, no payload artifact or overlay "
            f"recipe; the same-named directory {rel} (matched candidate '{c}') holds no "
            "implementation source defining a demanded name (headers only, or another image's "
            "umbrella)")
    return ROUTE_SUBSTITUTE, ("no manifest declaration, no CMake build target, no payload artifact "
                              "or overlay recipe, no source directory for "
                              + "/".join(candidates[:3]))


def sweep_missing(edges, prefix, ctx):
    """Group every missing install name the analyzer already knows, by the route that supplies it."""
    names = sorted({dep for deps in edges.values() for dep, _weak, _up in deps})
    rows = []
    for install_name in names:
        state, _path = deployed_state(prefix, install_name, None)
        if state != "-":
            continue
        refs = [{"image": img, "weak": weak, "upward": upward}
                for img, weak, upward in referrers_for(install_name, edges)]
        demanded_kinds, per_referrer = demand_tables(install_name, refs)
        # the sweep's JSON keeps `demanded` a symbol list (a caller reads it): the bind kind is a
        # detail of the driver's rung view, not of the wave row.
        demanded = set(demanded_kinds)
        provided, evidence = ctx["xcode_provides"](install_name)
        if provided:
            rows.append({"install_name": install_name, "route": ROUTE_XCODE, "evidence": evidence,
                         "referrers": refs, "demanded": demanded,
                         "demanded_by_referrer": per_referrer})
            continue
        route, evidence = classify_route(install_name, refs, ctx, demanded)
        rows.append({"install_name": install_name, "route": route, "evidence": evidence,
                     "referrers": refs, "demanded": demanded,
                     "demanded_by_referrer": per_referrer})
    return rows


def print_sweep(rows, prefix, out=sys.stdout):
    counts = {route: 0 for route in ROUTE_ORDER}
    for row in rows:
        counts[row["route"]] = counts.get(row["route"], 0) + 1
    print("WAVE SWEEP -- every install name the prefix does not carry, grouped by the route that", file=out)
    print("would supply it.  Supplying a wave and re-running the ladder once replaces several", file=out)
    print("rung-by-rung turns; the routes are the ladder's own four plus the two that are not gaps.", file=out)
    print(f"  prefix: {prefix}", file=out)
    print(file=out)
    for route in ROUTE_ORDER:
        group = [row for row in rows if row["route"] == route]
        if not group:
            continue
        print(f"== {route.upper()} ({len(group)}) -- {ROUTE_HEADLINE[route]}", file=out)
        hdr = (f"   {'INSTALL NAME':<72} {'LINK':<6} {'REF':>3} {'DEM':>4} EVIDENCE")
        print(hdr, file=out)
        for row in group:
            refs = row["referrers"]
            link = fmt_links(refs)
            print(f"   {row['install_name']:<72} {link:<6} {len(refs):>3} "
                  f"{len(row['demanded']):>4} {row['evidence']}", file=out)
            for ref in refs:
                count = len(row["demanded_by_referrer"].get(ref["image"], {}))
                print(f"        referrer [{'weak' if ref['weak'] else 'strong'}"
                      f"{', upward' if ref['upward'] else ''}] "
                      f"{os.path.basename(ref['image'])}  ({count} demanded symbols from this field)",
                      file=out)
        print(file=out)
    blockers = [r for r in rows if r["route"] in (ROUTE_DECL, ROUTE_PAYLOAD, ROUTE_SOURCE)]
    non_gaps = ", ".join(f"{counts[route]} {route}" for route in (ROUTE_XCODE, ROUTE_ORPHAN,
                                                                  ROUTE_WEAK) if counts[route])
    print("wave: " + ", ".join(f"{counts[r]} {r}" for r in ROUTE_ORDER if counts[r]) + ".", file=out)
    print(f"      {len(blockers)} name(s) a strong referrer demands can be supplied by building or "
          f"declaring something ({counts[ROUTE_DECL]} declaration, {counts[ROUTE_PAYLOAD]} payload, "
          f"{counts[ROUTE_SOURCE]} source); {counts[ROUTE_SUBSTITUTE]} more have no source here and "
          f"only a truthful substitute can answer them.", file=out)
    print(f"      {non_gaps or 'no'} name(s) are not prefix gaps and need no supply.", file=out)
    print("      A substitute-only name is the one route with no artifact to build, and the "
          "analyzer's class column exists to keep a truthful headless answer distinct from an "
          "implementation.", file=out)
    return counts


# --------------------------------------------------------------------------- mach-o helpers

def column_key(install_name):
    """dyld's own name for an install name: leaf, truncated at the first '.'.

    Measured: /usr/lib/libcompression.dylib -> libcompression, /usr/lib/libSystem.B.dylib ->
    libSystem, .../Versions/A/Metal -> Metal.  Comparing with this key (equality, never substring)
    is what keeps kperf from absorbing kperfdataDT's symbols.
    """
    return os.path.basename(install_name).split(".")[0]


USED_RE = re.compile(r"^\s*(\S+)\s+\(compatibility version ([^,)]+), current version ([^,)]+)([^)]*)\)\s*$")


def dylibs_used(image):
    out = run([swift("llvm-objdump"), "--macho", "--dylibs-used", image])
    deps = []
    for line in out.splitlines():
        m = USED_RE.match(line)
        if not m:
            continue
        attrs = m.group(4)
        deps.append((m.group(1), "weak" in attrs, "upward" in attrs))
    return deps


BIND_TYPES = ("bind", "weak_bind", "lazy_bind")
_BINDS_CACHE = {}
_BINDS_DISAGREEMENTS = {}
_DYLIB_ID_CACHE = {}
_LOAD_COMMANDS_CACHE = {}


def dylib_id(image):
    """The image's own install name (LC_ID_DYLIB), or None when it has none."""
    if image not in _DYLIB_ID_CACHE:
        out = run([swift("llvm-objdump"), "--macho", "--dylib-id", image])
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        _DYLIB_ID_CACHE[image] = lines[1] if len(lines) > 1 else None
    return _DYLIB_ID_CACHE[image]


# --------------------------------------------------------------------------- mach-o import readers

# The load commands and bind opcodes the import readers decode, so an image's undefined symbols can
# be read from WHICHEVER form it carries.  Measured on this checkout, image by image:
#
#   image                              load command            --dyld-info    --bind
#   CoreGraphics, libsystem_c (built)  LC_DYLD_INFO_ONLY       0 rows         its table
#   DevToolsCore, libswiftCore         LC_DYLD_CHAINED_FIXUPS  its table      0 rows
#
# so neither llvm-objdump command covers both forms, and reading an image's demand from one of them
# alone is how a tree-built referrer came to read as demanding nothing (dar-m3s0 follow-up).
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
LC_REQ_DYLD = 0x80000000
LC_LOAD_DYLIB = 0xC
LC_LOAD_WEAK_DYLIB = 0x18 | LC_REQ_DYLD
LC_REEXPORT_DYLIB = 0x1F | LC_REQ_DYLD
LC_LAZY_LOAD_DYLIB = 0x20 | LC_REQ_DYLD
LC_LOAD_UPWARD_DYLIB = 0x23 | LC_REQ_DYLD
# Every one of these occupies an ordinal slot: leaving the lazy/upward forms out of the list shifts
# every later ordinal and misattributes dependency fields.
DYLIB_COMMANDS = (LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB, LC_LAZY_LOAD_DYLIB,
                  LC_LOAD_UPWARD_DYLIB)
LC_RPATH = 0x1C | LC_REQ_DYLD
LC_DYLD_INFO_ONLY = 0x22 | LC_REQ_DYLD
LC_DYLD_EXPORTS_TRIE = 0x33 | LC_REQ_DYLD
LC_DYLD_CHAINED_FIXUPS = 0x34 | LC_REQ_DYLD
LC_ID_DYLIB = 0xD

BIND_OPCODE_MASK = 0xF0
BIND_IMMEDIATE_MASK = 0x0F
BIND_OPCODE_DONE = 0x00
BIND_OPCODE_SET_DYLIB_ORDINAL_IMM = 0x10
BIND_OPCODE_SET_DYLIB_ORDINAL_ULEB = 0x20
BIND_OPCODE_SET_DYLIB_SPECIAL_IMM = 0x30
BIND_OPCODE_SET_SYMBOL_TRAILING_FLAGS_IMM = 0x40
BIND_OPCODE_SET_TYPE_IMM = 0x50
BIND_OPCODE_SET_ADDEND_SLEB = 0x60
BIND_OPCODE_SET_SEGMENT_AND_OFFSET_ULEB = 0x70
BIND_OPCODE_ADD_ADDR_ULEB = 0x80
BIND_OPCODE_DO_BIND = 0x90
BIND_OPCODE_DO_BIND_ADD_ADDR_ULEB = 0xA0
BIND_OPCODE_DO_BIND_ADD_ADDR_IMM_SCALED = 0xB0
BIND_OPCODE_DO_BIND_ULEB_TIMES_SKIPPING_ULEB = 0xC0
BIND_SYMBOL_FLAGS_WEAK_IMPORT = 0x1
BIND_TYPE_POINTER = 1

# dyld's special ordinals (a bind's dependency field when it is not an image).  The values are the
# guest's own dyld header's (darling/src/external/dyld/dyld3/MachOFile.h and dyldinfo.cpp):
# BIND_SPECIAL_DYLIB_SELF = 0, MAIN_EXECUTABLE = -1, FLAT_LOOKUP = -2, WEAK_LOOKUP = -3.  The
# earlier map here was shifted (it called -2 "main-executable" and only knew a weak lookup at -4),
# which both mislabelled every special-ordinal bind and made a -3 weak-coalesce lookup read as a
# flat one -- i.e. a weak lookup that is allowed to be absent could be reported as a hard stop.
SPECIAL_ORDINALS = {0: "self", -1: "main-executable", -2: "flat-lookup", -3: "weak-lookup"}


def macho_slices(data):
    """[(cputype, blob)] for the slices of a Mach-O (thin or universal)."""
    if len(data) < 8:
        return []
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        out = []
        count = struct.unpack_from(">I", data, 4)[0]
        off = 8
        for _ in range(count):
            cpu, _sub, o, size, _align = struct.unpack_from(">iiIII", data, off)
            off += 20
            out.append((cpu, data[o:o + size]))
        return out
    return [(0, data)]


def macho_load_commands(path):
    """What the import readers need from an image's load commands (cached per path).

    Returns {"slices": [{"cputype", "dylib_id", "dylibs": [(install_name, weak, reexport)],
    "rpaths", "chained_fixups": bytes|None, "dyld_info": bytes|None, "exports_trie": bytes|None,
    "defined": bool}]}: `dylibs` is ordinal order (ordinal 1 is the first entry), which is how a
    bind's dependency field is named, and each blob is the linkedit data itself.
    """
    if path in _LOAD_COMMANDS_CACHE:
        return _LOAD_COMMANDS_CACHE[path]
    result = _read_load_commands(path)
    _LOAD_COMMANDS_CACHE[path] = result
    return result


def _read_load_commands(path):
    try:
        data = open(path, "rb").read()
    except OSError:
        return {"slices": []}
    out = []
    for cputype, blob in macho_slices(data):
        entry = {"cputype": cputype, "dylibs": [], "rpaths": [], "chained_fixups": None,
                 "dyld_info": None, "exports_trie": None, "defined": False, "dylib_id": None,
                 "twolevel": True}
        if len(blob) < 32 or struct.unpack_from("<I", blob, 0)[0] != MH_MAGIC_64:
            out.append(entry)
            continue
        entry["defined"] = True
        flags, = struct.unpack_from("<I", blob, 24)
        # MH_TWOLEVEL: without it the image binds by NAME, not by dependency field, so every import
        # of a flat-namespace image is a flat lookup (measured: libunwind, whose --bind rows print
        # "flat-namespace" while its opcodes carry the main-executable special ordinal).
        entry["twolevel"] = bool(flags & 0x80)
        ncmds, = struct.unpack_from("<I", blob, 16)
        off = 32
        for _ in range(ncmds):
            if off + 8 > len(blob):
                break
            cmd, cmdsize = struct.unpack_from("<II", blob, off)
            if cmdsize < 8:
                break
            if cmd in DYLIB_COMMANDS:
                nameoff, = struct.unpack_from("<I", blob, off + 8)
                start = off + nameoff
                end = blob.find(b"\x00", start)
                entry["dylibs"].append((blob[start:end].decode("utf-8", "replace"),
                                        cmd == LC_LOAD_WEAK_DYLIB, cmd == LC_REEXPORT_DYLIB))
            elif cmd == LC_RPATH:
                nameoff, = struct.unpack_from("<I", blob, off + 8)
                start = off + nameoff
                end = blob.find(b"\x00", start)
                entry["rpaths"].append(blob[start:end].decode("utf-8", "replace"))
            elif cmd == LC_ID_DYLIB:
                nameoff, = struct.unpack_from("<I", blob, off + 8)
                start = off + nameoff
                end = blob.find(b"\x00", start)
                entry["dylib_id"] = blob[start:end].decode("utf-8", "replace")
            elif cmd == LC_DYLD_CHAINED_FIXUPS:
                doff, dsize = struct.unpack_from("<II", blob, off + 8)
                entry["chained_fixups"] = blob[doff:doff + dsize]
            elif cmd == LC_DYLD_INFO_ONLY:
                _ro, _rs, bind_off, bind_size, weak_off, weak_size, lazy_off, lazy_size = \
                    struct.unpack_from("<8I", blob, off + 8)
                entry["dyld_info"] = {
                    "bind": blob[bind_off:bind_off + bind_size],
                    "weak_bind": blob[weak_off:weak_off + weak_size],
                    "lazy_bind": blob[lazy_off:lazy_off + lazy_size],
                }
            elif cmd == LC_DYLD_EXPORTS_TRIE:
                doff, dsize = struct.unpack_from("<II", blob, off + 8)
                entry["exports_trie"] = blob[doff:doff + dsize]
            off += cmdsize
        out.append(entry)
    return {"slices": out}


def chained_fixup_imports(blob):
    """[{name, lib_ordinal, weak, addend}] from an LC_DYLD_CHAINED_FIXUPS payload.

    The imports table is the image's undefined-symbol list in that format: one entry per imported
    name, the library ordinal that names its dependency field, and the import's own WEAK flag.  Dyld
    applies every chained fixup when it loads the image (that is the format's whole point), so an
    import here is EAGER: there is no lazy form to report, and a missing strong one stops the load.
    """
    if not blob or len(blob) < 28:
        return []
    _version, _starts, imports_off, symbols_off, count, fmt, symfmt = struct.unpack_from("<7I", blob, 0)
    imports = []
    for i in range(count):
        if fmt == 1:          # DYLD_CHAINED_IMPORT
            value, = struct.unpack_from("<I", blob, imports_off + 4 * i)
            addend = 0
        elif fmt == 2:        # DYLD_CHAINED_IMPORT_ADDEND
            value, addend = struct.unpack_from("<Ii", blob, imports_off + 8 * i)
        elif fmt == 3:        # DYLD_CHAINED_IMPORT_ADDEND64
            value, addend = struct.unpack_from("<QI", blob, imports_off + 12 * i)
        else:
            return imports       # an imports_format this reader does not know: report what it has
        if fmt == 3:
            lib, weak, nameoff = value & 0xFFFF, (value >> 16) & 1, value >> 32
        else:
            lib, weak, nameoff = value & 0xFF, (value >> 8) & 1, value >> 9
        if lib > 127:
            lib -= 256
        start = symbols_off + nameoff
        end = blob.find(b"\x00", start)
        if end < 0:
            continue
        imports.append({"name": blob[start:end].decode("utf-8", "replace"), "lib_ordinal": lib,
                        "weak": bool(weak), "addend": addend, "eager": True})
    return imports


def dyld_info_bind_imports(stream, kind):
    """[{name, lib_ordinal, weak, eager}] from one dyld-info bind opcode stream.

    `kind` is "bind" (eager), "weak_bind" (a weak-definition lookup) or "lazy_bind" (deferred past
    load): the opcode stream does not carry that distinction, the load command does.  The special
    ordinals are kept as their values (0 = flat lookup by name, -1 self, -2 main executable, -3
    flat), so a caller can label a dependency field honestly instead of inventing one.
    """
    imports = []
    if not stream:
        return imports
    ordinal = 0
    symbol = None
    symbol_weak = kind == "weak_bind"
    pos = 0
    while pos < len(stream):
        byte = stream[pos]
        pos += 1
        opcode, imm = byte & BIND_OPCODE_MASK, byte & BIND_IMMEDIATE_MASK
        if opcode == BIND_OPCODE_DONE:
            if kind == "lazy_bind":
                continue        # a lazy stream is a series of DONE-terminated segments
            break
        if opcode == BIND_OPCODE_SET_DYLIB_ORDINAL_IMM:
            ordinal = imm
        elif opcode == BIND_OPCODE_SET_DYLIB_ORDINAL_ULEB:
            ordinal, pos = _read_uleb(stream, pos)
        elif opcode == BIND_OPCODE_SET_DYLIB_SPECIAL_IMM:
            ordinal = (imm | BIND_OPCODE_MASK) - 0x100 if imm else 0
        elif opcode == BIND_OPCODE_SET_SYMBOL_TRAILING_FLAGS_IMM:
            end = stream.find(b"\x00", pos)
            if end < 0:
                break
            symbol = stream[pos:end].decode("utf-8", "replace")
            symbol_weak = kind == "weak_bind" or bool(imm & BIND_SYMBOL_FLAGS_WEAK_IMPORT)
            pos = end + 1
        elif opcode == BIND_OPCODE_SET_TYPE_IMM:
            pass
        elif opcode == BIND_OPCODE_SET_ADDEND_SLEB:
            _value, pos = _read_sleb(stream, pos)
        elif opcode == BIND_OPCODE_SET_SEGMENT_AND_OFFSET_ULEB:
            _value, pos = _read_uleb(stream, pos)
        elif opcode == BIND_OPCODE_ADD_ADDR_ULEB:
            _value, pos = _read_uleb(stream, pos)
        elif opcode == BIND_OPCODE_DO_BIND:
            _bind_import(imports, symbol, ordinal, symbol_weak, kind)
        elif opcode == BIND_OPCODE_DO_BIND_ADD_ADDR_ULEB:
            _bind_import(imports, symbol, ordinal, symbol_weak, kind)
            _value, pos = _read_uleb(stream, pos)
        elif opcode == BIND_OPCODE_DO_BIND_ADD_ADDR_IMM_SCALED:
            _bind_import(imports, symbol, ordinal, symbol_weak, kind)
        elif opcode == BIND_OPCODE_DO_BIND_ULEB_TIMES_SKIPPING_ULEB:
            count, pos = _read_uleb(stream, pos)
            _skip, pos = _read_uleb(stream, pos)
            for _ in range(count):
                _bind_import(imports, symbol, ordinal, symbol_weak, kind)
        else:
            break               # an opcode this reader does not know: report what it has
    return imports


def _bind_import(imports, symbol, ordinal, weak, kind):
    if symbol is None or not symbol.startswith("_"):
        return
    imports.append({"name": symbol, "lib_ordinal": ordinal, "weak": weak,
                    "eager": kind == "bind"})


def _read_uleb(blob, pos):
    value = 0
    shift = 0
    while pos < len(blob):
        byte = blob[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return value, pos


def _read_sleb(blob, pos):
    value = 0
    shift = 0
    while pos < len(blob):
        byte = blob[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if byte & 0x40:
                value -= 1 << shift
            break
    return value, pos


def guest_slice(path):
    """The slice of an image the guest's architecture uses (or None).

    A universal binary carries several; only a 64-bit slice has the load commands, chained fixups and
    dyld-info opcodes this reader decodes, and a 32-bit i386 slice next to an x86_64 one would
    otherwise supply an EMPTY dylib list (Security.framework is such a binary: 0 dylibs in slice 0,
    21 in slice 1).  Imports and the ordinal -> image mapping must come from the SAME slice, or every
    dependency field is misattributed.
    """
    slices = macho_load_commands(path)["slices"]
    for candidate in slices:
        if candidate["defined"]:
            return candidate
    return slices[0] if slices else None


def image_imports(path):
    """The image's undefined symbols, from whichever import form it carries.

    Returns {"defined", "form", "dylibs", "dylib_id", "rpaths", "imports"}: `dylibs` is ordinal order
    of the SAME slice the imports were read from, `form` is "chained-fixups" or "dyld-info" (or "" for
    an image this reader cannot read), and each import is {"name", "lib_ordinal", "weak", "eager",
    "field"} where `field` is the dependency field's own name (the ordinal's install-name stem, dyld's
    bind-table column) or None for the special ordinals (flat lookup, self, main executable).

    This is the reader `llvm-objdump --dyld-info` is NOT: that command prints nothing at all for a
    chained-fixups image, which is how a tree-built referrer used to read as demanding nothing.
    """
    slice_ = guest_slice(path)
    if slice_ is None or not slice_["defined"]:
        return {"defined": False, "form": "", "dylibs": [], "dylib_id": None, "rpaths": [],
                "imports": []}
    dylibs = slice_["dylibs"]
    if slice_["chained_fixups"]:
        form = "chained-fixups"
        imports = chained_fixup_imports(slice_["chained_fixups"])
    else:
        form = "dyld-info"
        streams = slice_["dyld_info"] or {}
        imports = []
        for kind in ("bind", "weak_bind", "lazy_bind"):
            imports += dyld_info_bind_imports(streams.get(kind), kind)
    for imp in imports:
        ordinal = imp["lib_ordinal"]
        if not slice_["twolevel"]:
            # A flat-namespace image binds by NAME: every import is a flat lookup, whatever ordinal
            # its opcode carried (measured on libunwind).
            imp["field"] = None
            imp["special"] = "flat-lookup"
            continue
        if 1 <= ordinal <= len(dylibs):
            imp["field"] = os.path.basename(dylibs[ordinal - 1][0]).split(".")[0]
            imp["special"] = ""
        else:
            imp["field"] = None
            imp["special"] = SPECIAL_ORDINALS.get(ordinal, f"ordinal {ordinal}")
    return {"defined": True, "form": form, "dylibs": dylibs, "dylib_id": slice_["dylib_id"],
            "rpaths": slice_["rpaths"], "imports": imports}


def objdump_bind_rows(image):
    """[(field, symbol, kind)] from llvm-objdump --macho --dyld-info -- the CROSS-CHECK source only.

    No tool here takes a name, a field or a kind from this text: it is read to compare against the
    table decoded from the image, because the command MISLABELS neighbouring GOT slots on at least
    one image of this workload (measured 2026-10-12 on DVTInstrumentsFoundation, x86_64, chained
    fixups: a `__LSCopyMatchingApplicationsWithItems` slot attributed to a different `__LSC...`
    import entry -- the rung caught it by decoding LC_DYLD_CHAINED_FIXUPS itself).
    """
    out = run([swift("llvm-objdump"), "--macho", "--dyld-info", image])
    rows = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 8 and f[4] in BIND_TYPES and f[7].startswith("_"):
            rows.append((f[6], f[7], f[4]))
    return rows


def compare_bind_tables(decoded, text_rows):
    """[(field, symbol, decoded_kind, text_kind)] where two readers of one image disagree.

    Pure, so the disagreement shape is contractable: `text_kind` is None when the text does not carry
    the name at all, and a row only the text carries comes back with `decoded_kind` None.
    """
    text = {}
    for field, sym, kind in text_rows:
        text.setdefault((field, sym), kind)
    out = []
    for field, syms in decoded.items():
        for sym, kind in syms.items():
            text_kind = text.get((field, sym))
            if text_kind is not None and text_kind != kind:
                out.append((field, sym, kind, text_kind))
    for (field, sym), text_kind in text.items():
        if sym not in decoded.get(field, {}):
            out.append((field, sym, None, text_kind))
    return sorted(out)


def binds_by_dependency(image):
    """dylib column name -> {symbol: bind kind}, DECODED from the image itself.

    Cached per image for the life of the process: the sweep asks for the same referrer's table once
    per install name it appears in.  The decode is `image_imports` -- chained-fixups imports table
    (with its own weak flag) or the dyld-info bind/weak_bind/lazy_bind opcodes -- from the ONE slice
    `guest_slice` chooses, so an ordinal and the field it names always come from the same place.

    `llvm-objdump --macho --dyld-info` is NOT a source here (it mislabels neighbouring GOT slots:
    see objdump_bind_rows); its text is read only to cross-check, and a disagreement is recorded by
    binds_disagreement(image) so a caller can say the two readers differ instead of trusting either
    blindly.
    """
    if image in _BINDS_CACHE:
        return _BINDS_CACHE[image]
    table = {}
    for imp in image_imports(image)["imports"]:
        if imp["field"] is None or not imp["name"].startswith("_"):
            continue
        table.setdefault(imp["field"], {}).setdefault(
            imp["name"], "weak_bind" if imp["weak"] else "bind")
    rows = objdump_bind_rows(image)
    if rows:
        differences = compare_bind_tables(table, rows)
        if differences:
            _BINDS_DISAGREEMENTS[image] = differences
    _BINDS_CACHE[image] = table
    return table


def binds_disagreement(image):
    """The recorded decoded-vs-objdump disagreement for an image, or None (fills the cache first)."""
    binds_by_dependency(image)
    return _BINDS_DISAGREEMENTS.get(image)


EXPORTS_TRIE_HEADER = "Exports trie:"
# The line forms llvm-objdump --exports-trie emits for a name a client can bind, captured from real
# output on this checkout (163 deployed + 47 Xcode images, every line accounted for):
#
#   0x00058F70  _memset_s                      a definition in this image
#   0x000AB770  __ZTISt16nested_exception [weak_def]
#                                              a WEAK definition (the annotation is its own token)
#   [re-export] _memset_pattern16 (__platform_memset_pattern16 from libsystem_platform)
#                                              an ALIAS: the name is satisfied by another name in
#                                              another image -- exactly how libsystem_c supplies
#                                              _memcpy, _memmove, _memset, _strlen, _strcmp, _strlcpy
#   [re-export] __spawn_via_launchd (from liblaunch)
#                                              a plain re-export: satisfied from another image
#
# `0xADDR $ld$hide$osX.Y$_NAME` / `$ld$previous$...` / `R8289209$_close` / `dyld_stub_binder` are the
# other tokens the tool prints; they carry no whitespace and were already read as names.
EXPORT_DEFINED_RE = re.compile(r"^0x[0-9a-fA-F]+\s+(\S+)(?:\s+\[(\w+)\])?$")
EXPORT_ALIAS_RE = re.compile(r"^\[re-export\]\s+(\S+)\s+\((\S+)\s+from\s+([^)]*)\)$")
EXPORT_REEXPORT_RE = re.compile(r"^\[re-export\]\s+(\S+)\s+\(from\s+([^)]*)\)$")
# The annotation tags measured in that same output; both name a symbol a client can bind (a weak
# definition, and an absolute symbol like _swift_runtimeSupportsNoncopyableTypes).
EXPORT_ANNOTATIONS = ("weak_def", "absolute")


def parse_exports_trie(text):
    """Every line form of an export trie, from captured output rather than one spelling.

    Returns {"read", "names", "aliases", "providers", "annotations", "unknown"}: `names` is every name
    a client's binding can be satisfied by (definitions, annotated definitions and re-exports alike,
    which is what the line forms above mean); `aliases` records {name: (alias, image)}, `providers`
    {name: image} and `annotations` {name: tag} so a caller can say WHERE a name came from and HOW.

    `read` is False when the output carries no `Exports trie:` header at all -- the tool did not read
    a trie (a non-Mach-O or an unreadable file), which is a DIFFERENT fact from a trie that IS read
    and is empty: a load-name-only image has no names, and saying so is the honest answer rather than
    one a fallback reader might invent.  A line matching none of the captured forms is kept in
    `unknown` rather than dropped, so a caller can report that the trie was only partly read.
    """
    detail = {"read": EXPORTS_TRIE_HEADER in text, "names": set(), "aliases": {}, "providers": {},
              "annotations": {}, "unknown": []}
    if not detail["read"]:
        return detail
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == EXPORTS_TRIE_HEADER or stripped.endswith(":"):
            continue
        m = EXPORT_DEFINED_RE.match(stripped)
        if m:
            detail["names"].add(m.group(1))
            if m.group(2):
                detail["annotations"][m.group(1)] = m.group(2)
            continue
        m = EXPORT_ALIAS_RE.match(stripped)
        if m:
            detail["names"].add(m.group(1))
            detail["aliases"][m.group(1)] = (m.group(2), m.group(3).strip())
            continue
        m = EXPORT_REEXPORT_RE.match(stripped)
        if m:
            detail["names"].add(m.group(1))
            detail["providers"][m.group(1)] = m.group(2).strip()
            continue
        detail["unknown"].append(stripped)
    return detail


def exports_detail(artifact):
    """parse_exports_trie over the tool's own output, with the symbol table as a marked fallback.

    The fallback runs only when NO trie was read (no header): then the names come from `llvm-nm` and
    `source` says so, so an unread trie can never be mistaken for an empty one.
    """
    detail = parse_exports_trie(run([swift("llvm-objdump"), "--macho", "--exports-trie", artifact]))
    if detail["read"]:
        detail["source"] = "exports-trie"
        return detail
    names = set()
    for line in run([swift("llvm-nm"), "-g", "--defined-only", artifact]).splitlines():
        f = line.split()
        if len(f) == 3:
            names.add(f[2])
    detail["names"] = names
    detail["source"] = "llvm-nm"
    return detail


def exports(artifact):
    """The names an image satisfies for a client: definitions, weak definitions and re-exports.

    A re-export is how libsystem_c supplies _memcpy (as __platform_memmove from
    libsystem_platform), so dropping those lines turns a satisfied demand into a miss (dar-m3s0
    follow-up).
    """
    return set(exports_detail(artifact)["names"])


def reexports_of(image):
    """Install names this image re-exports (LC_REEXPORT_DYLIB), in load-command order.

    dyld resolves a client's binding through the image's re-export list, so these install names are
    part of what the image supplies (dar-m3s0).
    """
    out = run([swift("llvm-objdump"), "--macho", "--dylibs-used", image])
    names = []
    for line in out.splitlines():
        m = USED_RE.match(line)
        if m and "reexport" in m.group(4):
            names.append(m.group(1))
    return names


def reexport_closure(root, reexports_of, resolve, exports_of, max_depth=8):
    """({names}, {install name: exports detail}) over the images re-export-reachable from `root`.

    Kept separate from the Mach-O readers so the SHAPE is contractable without llvm tools:
    `reexports_of(path)` names the install names an image re-exports, `resolve(install_name)` the
    path the deploy resolves one to (or None), `exports_of(path)` the detail dict `exports_detail`
    returns (the walk unions its `names`, and keeps the dict so the caller can say WHERE a name came
    from -- including an ALIAS, `_memcpy` -> `__platform_memmove` from libsystem_platform).  The walk
    is iterative, depth-bounded and cycle-safe, because the shape that matters is the NESTED one
    (Accelerate re-exports vecLib, vecLib re-exports libBNNS/libvDSP, and the intermediate hub's own
    trie is empty) -- a one-level walk from the umbrella would see zero names there (dar-m3s0).
    """
    names, providers = set(), {}
    seen = {os.path.realpath(root)}
    queue = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for install_name in reexports_of(current):
            path = resolve(install_name)
            if not path:
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            got = exports_of(path)
            providers[install_name] = got
            names |= set(got["names"])
            queue.append((path, depth + 1))
    return names, providers


def reexported_exports(image, prefix, max_depth=8):
    """(names, providers) what a deployed image satisfies through the dylibs it re-exports.

    Dyld resolves a client's binding through the image's whole re-export graph, so these names are
    part of what the deployed image supplies.  Each re-exported install name is resolved inside the
    prefix the way the deploy resolves it (both prefix roots), and `providers` maps each re-exported
    install name to its export detail so the caller can say WHERE a name came from.
    """
    return reexport_closure(image, reexports_of, lambda n: deployed_path(prefix, n), exports_detail,
                            max_depth)


def symbol_types(artifact):
    """symbol -> nm type letter, so a function (T/t) is never disassembled as if it were data."""
    types = {}
    for line in run([swift("llvm-nm"), "-g", "--defined-only", artifact]).splitlines():
        f = line.split()
        if len(f) >= 3 and len(f[1]) == 1:
            types[f[-1]] = f[1]
    return types


def stub_evidence(artifact):
    """The family's stub convention, as byte counts and as the names it prints.

    Marker 1: C stubs print `STUB: <function name> called`, so the string names the exact symbol
    that is a stand-in.  Marker 2: Objective-C stubs forward every selector and log
    `Stub called: ...`; the count is a presence count, because the linker merges identical
    literals, so it is reported as a count of surviving distinct occurrences, not of stub sites.
    """
    try:
        blob = open(artifact, "rb").read()
    except OSError:
        blob = b""
    stub_count = blob.count(b"STUB:")
    forward_count = blob.count(b"Stub called")
    names = set()
    for m in re.finditer(rb"STUB:\s*([A-Za-z_][A-Za-z0-9_$.:]*)", blob):
        names.add(m.group(1).decode("ascii", "replace"))
    return {"stub_string_count": stub_count, "forward_string_count": forward_count,
            "stub_names": names}


OBJC_CLASS_RE = re.compile(r"^[0-9a-f]+\s+0x[0-9a-f]+\s+(_OBJC_CLASS_\$_(\S+))")
BASEMETHODS_RE = re.compile(r"baseMethods\s+(0x[0-9a-f]+)\s+\(struct method_list_t \*\)")
METHOD_NAME_RE = re.compile(r"^\s+name\s+0x[0-9a-f]+\s+(\S+:\S*)\s*$")


def objc_classes(artifact):
    """class name -> set of its own (base) method selectors, or None when it has no methods."""
    out = run([swift("llvm-objdump"), "--macho", "--objc-meta-data", artifact])
    classes = {}
    current = None
    in_methods = False
    for line in out.splitlines():
        m = OBJC_CLASS_RE.match(line.strip())
        if m:
            current = m.group(2)
            classes.setdefault(current, None)
            in_methods = False
            continue
        if current is None:
            continue
        bm = BASEMETHODS_RE.search(line)
        if bm and not in_methods:
            if bm.group(1) != "0x0":
                classes[current] = set()
                in_methods = True
            else:
                in_methods = False
            continue
        if in_methods:
            nm = METHOD_NAME_RE.match(line)
            if nm:
                classes[current].add(nm.group(1))
    return classes


def disassemble_symbol(artifact, symbol):
    """The instructions of one demanded symbol: [(mnemonic, operand)] as printed."""
    out = run([swift("llvm-objdump"), "--macho", "--disassemble", "--dis-symname", symbol, artifact])
    if "Can't find -dis-symname" in out:
        return None
    body = []
    for line in out.splitlines():
        m = re.match(r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{2}\s+)+\s*(\S+)\s*(.*)$", line)
        if m:
            body.append((m.group(1), m.group(2).strip()))
    return body


def has_behaviour(body):
    """A call, or a jump that leaves this function (a tail call into another symbol).

    An unconditional jump to a plain address is an internal branch -- a truthful function that
    returns a value from a table has one -- and is not behaviour.
    """
    for mnemonic, operand in body:
        if mnemonic.startswith("call"):
            return True
        if mnemonic.startswith("jmp") and not re.match(r"^0x[0-9a-f]+$", operand):
            return True
    return False


def return_shape(body):
    """What a body with no call answers with: True = absence (zero), False = a value, None = void.

    This separates "there is no device, so here is nothing" (a truth about a headless guest) from a
    body that hands back a made-up constant.  The tree contains both, and only the first is
    deliberate headless behaviour: AppleSystemInfo's ASI_NumberOfCPUs returns 1 with no call and no
    printed notice, and ASI_CopyComputerModelName returns an @"UNIMPLEMENTED" literal -- silent stubs
    that a "no call" test alone would call truthful.
    """
    last = None
    for mnemonic, operand in body:
        parts = [p.strip() for p in operand.split(",")] if operand else []
        if not parts or parts[-1] not in ("%eax", "%rax", "%al", "%ax"):
            continue
        last = (mnemonic, parts)
    if last is None:
        return None
    mnemonic, parts = last
    if mnemonic.startswith("xor") and len(parts) >= 2 and parts[0] == parts[1]:
        return True
    if mnemonic.startswith("sub") and len(parts) >= 2 and parts[0] == parts[1]:
        return True
    if mnemonic.startswith("mov") and parts[0] in ("$0x0", "$0x00"):
        return True
    return False


def swift(tool):
    return os.path.join(SWIFT_BIN, tool)


SWIFT_BIN = DEFAULTS["swift_bin"]


# --------------------------------------------------------------------------- provenance

def ladder_base(darling):
    """Parent of the oldest reachable commit whose subject starts with 'rootless toolchain'."""
    out = run(["git", "-C", darling, "log", "--format=%H%x09%s", "HEAD"])
    oldest = None
    for line in out.splitlines():
        sha, _, subject = line.partition("\t")
        if subject.startswith("rootless toolchain"):
            oldest = sha
    if not oldest:
        return None
    return run(["git", "-C", darling, "rev-parse", oldest + "^"]).strip() or None


def repo_and_relpath(path):
    """Walk up from `path` to the nearest repo root; return (repo, relpath)."""
    p = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p, os.path.relpath(os.path.abspath(path), p)
        parent = os.path.dirname(p)
        if parent == p:
            return None, None
        p = parent


SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".m", ".mm", ".h", ".hpp", ".swift")
# A SOURCE route promises a build, so only a compiler input counts as the implementation: a header
# DECLARES, it does not define (dar-xovc).
SOURCE_IMPL_SUFFIXES = (".c", ".cc", ".cpp", ".m", ".mm", ".swift")


def walk_source_changes(darling, base, source_dir):
    """Which implementation files (if any) the ladder's commits changed in this component.

    CMakeLists.txt does not count: several rungs touched a component's CMakeLists only to raise and
    then revert a version claim, and that is not implementation work.
    """
    repo, rel = repo_and_relpath(source_dir)
    if repo is None:
        return None, []
    if os.path.realpath(repo) != os.path.realpath(darling):
        # Nested repository (submodule): use the base commit the parent pinned for it.
        sub_rel = os.path.relpath(repo, darling)
        base = run(["git", "-C", darling, "rev-parse", f"{base}:{sub_rel}"]).strip() or base
    head = run(["git", "-C", repo, "rev-parse", "HEAD"]).strip()
    out = run(["git", "-C", repo, "diff", "--name-only", f"{base}..{head}", "--", rel])
    files = [os.path.join(repo, f) for f in out.splitlines() if f.endswith(SOURCE_SUFFIXES)]
    return base, files


# --------------------------------------------------------------------------- referrers

def xcode_file(path, xcode):
    """Resolve a load-command path to a file under Xcode's own installation, if it lives there.

    Kept from the scratch analyzer: a guest path inside the Xcode mount, an @rpath framework, and
    the framework-relative forms are all the same tree once the guest mount prefix is stripped.
    """
    cands = []
    if GUEST_XCODE_MOUNT_MARK in path:
        tail = path.split(GUEST_XCODE_MOUNT_MARK, 1)[1]
        cands.append(os.path.join(xcode, tail.lstrip("/")))
    stem = os.path.basename(path).split(".")[0]
    for sub in ("SharedFrameworks", "Frameworks", "PrivateFrameworks", "PlugIns"):
        cands.append(os.path.join(xcode, sub, f"{stem}.framework/Versions/A/{stem}"))
        cands.append(os.path.join(xcode, sub, f"{stem}.framework/Versions/C/{stem}"))
    if path.startswith("/"):
        cands.append(os.path.join(xcode, path.lstrip("/")))
        cands.append(os.path.join(
            xcode,
            path.lstrip("/")
            .replace("System/Library/Frameworks/", "Frameworks/")
            .replace("System/Library/PrivateFrameworks/", "PrivateFrameworks/")))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def load_closure(entry, xcode):
    """Breadth-first closure of the guest's load-command graph, as the scratch analyzer did it.

    The referrer set that matters is the images the guest actually loads, not every image in an
    Xcode.app that mentions the same install name: Xcode ships Instruments/GPUTools plugins that
    link Metal and would otherwise be counted as clients of a framework the build never loads.
    """
    edges = {}
    seen = {entry}
    queue = [entry]
    while queue:
        img = queue.pop(0)
        deps = dylibs_used(img)
        edges[img] = deps
        for dep, _weak, _up in deps:
            if dep in seen:
                continue
            seen.add(dep)
            hp = xcode_file(dep, xcode)
            if hp and len(seen) < 6000:
                queue.append(hp)
    return edges


def referrers_for(install_name, edges):
    """Images in the closure whose own load commands name this exact install name.

    An image that names ITSELF is not a client, and `--dylibs-used` prints the image's own
    LC_ID_DYLIB as its first dependency line (measured on AppThinning), so that entry is skipped:
    counting it made a component look like its own strong referrer and could turn a weak-only
    absence into a strong one.
    """
    found = []
    for img, deps in edges.items():
        if dylib_id(img) == install_name:
            continue
        for dep, weak, upward in deps:
            if dep == install_name:
                found.append((img, weak, upward))
                break
    return sorted(found)


def tree_referrers_for(install_name, xcode):
    """Every load-command reference anywhere in the Xcode subtrees (not just the closure)."""
    roots = [os.path.join(xcode, r) for r in XCODE_SCAN_ROOTS]
    roots = [r for r in roots if os.path.isdir(r)]
    out = run(["grep", "-rlF", "--binary-files=binary", install_name] + roots, timeout=600)
    found = []
    for cand in out.splitlines():
        if cand.endswith(".tbd") or not os.path.isfile(cand):
            continue
        if dylib_id(cand) == install_name:
            continue
        for dep, weak, upward in dylibs_used(cand):
            if dep == install_name:
                found.append((cand, weak, upward))
                break
    return sorted(found)


# --------------------------------------------------------------------------- demand classification

def referrer_dicts(refs):
    """[{image, weak, upward}] from either shape: the analyzer's tuples or the driver's dicts."""
    if refs and isinstance(refs[0], dict):
        return [{"image": r["image"], "weak": r["weak"], "upward": r["upward"]} for r in refs]
    return [{"image": r[0], "weak": r[1], "upward": r[2]} for r in refs]


def demand_tables(install_name, refs):
    """({symbol: bind kind}, {referrer image: {symbol: bind kind}}) for one dependency field.

    The field is dyld's own: a referrer demands a symbol from this image only when the bind row's
    dylib column equals this image's column name (see column_key), never when the symbol text merely
    resembles it.  `demanded` collapses the referrers' tables with the first bind kind seen.
    """
    column = column_key(install_name)
    demanded, per_referrer = {}, {}
    for ref in referrer_dicts(refs):
        syms = binds_by_dependency(ref["image"]).get(column, {})
        per_referrer[ref["image"]] = syms
        for sym, kind in syms.items():
            demanded.setdefault(sym, kind)
    return demanded, per_referrer


def demand_split(demanded, referrers, demanded_by_referrer, own=frozenset(),
                 reexported=frozenset()):
    """One demand classification (dar-m3s0), shared by the analyzer and the rung driver.

    `own` is the image's own export trie and `reexported` the export tries of the images it
    re-exports; dyld resolves a client's binding through both, so a name in either is SATISFIED and
    not an absence.  An absent name is FATAL only when a referrer that links this image neither
    weakly nor upwardly binds it non-lazily; a lazily bound absence is LAZY (dyld defers it past
    load), and a name demanded only by weak or upward referrers is TOLERATED -- their references are
    resolved weakly by dyld, so the absence cannot stop the load.

    `referrers` is [{image, weak, upward}] (or the analyzer's tuples); `demanded_by_referrer` maps a
    referrer image to its own {symbol: bind kind} table.  Returns the four buckets plus `missing`
    (absent names) and the satisfied names split by where they came from, so a caller can report the
    reason rather than re-derive it.
    """
    own = set(own)
    reexported = set(reexported)
    referrers = referrer_dicts(referrers)
    satisfied_own, satisfied_reexport, missing, fatal, lazy, tolerated = [], [], [], [], [], []
    for sym in sorted(demanded):
        if sym in own:
            satisfied_own.append(sym)
            continue
        if sym in reexported:
            satisfied_reexport.append(sym)
            continue
        missing.append(sym)
        kinds = []
        for ref in referrers:
            kind = demanded_by_referrer.get(ref["image"], {}).get(sym)
            if kind is not None:
                kinds.append((ref["weak"] or ref["upward"], kind))
        if any((not link_tolerant) and kind == "bind" for link_tolerant, kind in kinds):
            fatal.append(sym)
        elif any((not link_tolerant) and kind != "weak_bind"
                 for link_tolerant, kind in kinds):
            lazy.append(sym)
        else:
            tolerated.append(sym)
    return {"demanded": sorted(demanded), "satisfied_own": satisfied_own,
            "satisfied_reexport": satisfied_reexport, "missing": missing,
            "fatal": fatal, "lazy": lazy, "tolerated": tolerated}


# --------------------------------------------------------------------------- analysis

def analyze_component(target, decl, manifest, darling, base, referrers):
    install_name = decl["install_name"]
    artifact = resolve_artifact(manifest, decl)
    result = {
        "target": target,
        "install_name": install_name,
        "cmake_target": decl["cmake_target"],
        "artifact": artifact,
        "exists": artifact is not None,
        "referrers": [],
        "demanded": {},          # symbol -> kind
        "demanded_by_referrer": {},
        "exported": set(),
        "missing": [],
        "stub_attributed": [],
        "truthful": [],
        "behavioural": [],
        "unknown": [],
        "walk_truthful": [],
        "walk_behavioural": [],
        "class_notes": [],
        "walk_source_changes": [],
        "column": column_key(install_name),
    }
    norm_refs = referrer_dicts(referrers)
    demanded, demanded_by_referrer = demand_tables(install_name, norm_refs)
    result["demanded"] = demanded
    result["demanded_by_referrer"] = demanded_by_referrer
    for ref in norm_refs:
        result["referrers"].append({**ref, "dependent_symbol_count":
                                    len(demanded_by_referrer[ref["image"]])})

    if not artifact:
        result["class_notes"].append("no built artifact resolved from the manifest")
        result["klass"] = "STUB"
        return result

    exported = exports(artifact)
    result["exported"] = exported
    ev = stub_evidence(artifact)
    result.update(ev)
    classes = objc_classes(artifact)
    types = symbol_types(artifact)
    result["objc_class_count"] = len(classes)

    # One classification for both this analyzer and the rung driver (dar-m3s0): an absent name is
    # fatal only when a referrer that links the component neither weakly nor upwardly binds it
    # non-lazily.
    split = demand_split(demanded, result["referrers"], demanded_by_referrer, exported)
    result["missing"] = split["missing"]
    result["missing_fatal"] = split["fatal"]
    result["missing_lazy"] = split["lazy"]
    result["missing_tolerated"] = split["tolerated"]

    for sym in sorted(demanded):
        if sym in result["missing"]:
            continue
        if sym.startswith("_OBJC_CLASS_$_"):
            cls = sym[len("_OBJC_CLASS_$_"):]
            if cls not in classes:
                status = "unknown: class symbol exported, no metadata"
            elif classes[cls] is None:
                status = "absence: method-free class"
            elif classes[cls] == {"methodSignatureForSelector:", "forwardInvocation:"}:
                status = "stub: ObjC forwarding"
            else:
                status = "real: class has methods"
        else:
            cname = sym[1:] if sym.startswith("_") else sym
            if cname in ev["stub_names"]:
                status = "stub: printed STUB: name"
            else:
                t = types.get(sym)
                if t is None:
                    status = "unknown: not in the symbol table"
                elif t in "Tt":
                    body = disassemble_symbol(artifact, sym)
                    if body is None:
                        status = "unknown: not disassemblable"
                    elif has_behaviour(body):
                        status = "real: function body calls"
                    else:
                        shape = return_shape(body)
                        if shape is None:
                            status = "absence: void body, no call"
                        elif shape:
                            status = "absence: returns zero, no call"
                        else:
                            status = "value: no call but returns a value, not absence"
                else:
                    status = "absence: data symbol"
        if status.startswith("stub"):
            result["stub_attributed"].append((sym, status))
        elif status.startswith("absence"):
            result["truthful"].append((sym, status))
        elif status.startswith("real") or status.startswith("value"):
            result["behavioural"].append((sym, status))
        else:
            result["unknown"].append((sym, status))

    _, changes = walk_source_changes(darling, base, source_dir_for(artifact, darling))
    result["walk_source_changes"] = changes

    # Which demanded symbols does the walk's own code answer?  A symbol is walk-attributed when its
    # name appears in a source file the ladder's commits changed; that is what separates "the walk
    # wrote this" from "the tree already had it and the walk declared it".
    walk_text = {}
    for f in changes:
        try:
            walk_text[f] = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            walk_text[f] = ""

    def walk_attributed(sym):
        name = symbol_source_name(sym)
        return any(name in text for text in walk_text.values())

    truthful_syms = {s for s, _ in result["truthful"]}
    walk_truthful = [s for s in truthful_syms if walk_attributed(s)]
    walk_value = [s for s, _ in result["behavioural"] if walk_attributed(s)]
    walk_behavioural = walk_value
    result["walk_truthful"] = walk_truthful
    result["walk_behavioural"] = walk_behavioural

    empty_surface = len(exported) == 0
    if result["missing_fatal"] or result["missing_lazy"]:
        result["klass"] = "STUB"
        detail = result["missing_fatal"] + result["missing_lazy"]
        result["class_notes"].append(
            "incomplete: demanded symbol(s) not exported: " + ", ".join(detail[:4])
            + (f" (+{len(detail) - 4} more)" if len(detail) > 4 else ""))
    elif result["stub_attributed"]:
        result["klass"] = "STUB"
        result["class_notes"].append(
            f"{len(result['stub_attributed'])} of {len(demanded)} demanded symbol(s) are stub-backed "
            + "(demanded surface answers with a stand-in, not an implementation)")
    elif empty_surface:
        result["klass"] = "STUB"
        result["class_notes"].append("load-name-only: artifact exports no symbols")
    elif walk_behavioural:
        result["klass"] = "REAL"
        result["class_notes"].append(
            "walk wrote source; its demanded symbol(s) answer with behaviour, not absence: "
            + ", ".join(walk_behavioural[:4]))
    elif walk_truthful:
        result["klass"] = "DELIBERATE"
        result["class_notes"].append(
            "walk wrote source; its demanded symbol(s) answer with absence or are method-free "
            "classes, with no stub marker: " + ", ".join(walk_truthful[:4]))
    else:
        result["klass"] = "EXISTING"
        result["class_notes"].append("walk wrote no demanded implementation source; declaration only")
    if result["truthful"] and result["behavioural"] and result["klass"] in ("DELIBERATE", "REAL"):
        result["class_notes"].append(
            f"component also exports {len(result['behavioural'])} demanded symbol(s) of pre-existing "
            "real behaviour")
    return result


def source_dir_for(artifact, darling):
    """The tree directory a built artifact came from: the build tree mirrors the source layout."""
    rel = os.path.relpath(os.path.dirname(artifact), BUILD_DIR)
    return os.path.join(darling, rel)


# --------------------------------------------------------------------------- output

def fmt_links(refs):
    if not refs:
        return "none"
    if any(not r["weak"] for r in refs):
        return "strong"
    return "weak"


# --------------------------------------------------------------------------- symbol coverage

# What an import's resolution can be, in the order the loader tries them.
COVER_OWN = "own-export-trie"
COVER_REEXPORT = "reexport"
COVER_FLAT = "flat-lookup"
COVER_ABSENT_WEAK = "absent-weak"
COVER_MISSING = "missing"
COVER_IMAGE_ABSENT = "field-image-absent"
COVER_LOAD_ORDER = "satisfiable-by-load-order"
COVER_ABSENT_LAZY = "absent-lazy"
COVER_ORDER = (COVER_OWN, COVER_REEXPORT, COVER_FLAT, COVER_LOAD_ORDER, COVER_ABSENT_WEAK,
               COVER_ABSENT_LAZY, COVER_MISSING, COVER_IMAGE_ABSENT)
# Which labels are STATIC FACTS about the deployed files and which are MODEL DECISIONS about what
# dyld would do; the output states this rather than dressing a model up as an observation.
COVER_MODEL = (COVER_FLAT, COVER_LOAD_ORDER, COVER_ABSENT_WEAK, COVER_ABSENT_LAZY, COVER_MISSING,
               COVER_IMAGE_ABSENT)
COVER_HEADLINE = {
    COVER_OWN: "the dependency field image's own export trie carries it",
    COVER_REEXPORT: "reached through the field image's re-export graph (nested, aliases included)",
    COVER_FLAT: "dyld's flat lookup: no field, so any image in the closure may carry it",
    COVER_LOAD_ORDER: "no field image carries it, but an image the process LOADS does: the flat/weak "
                      "path could satisfy it (model)",
    COVER_ABSENT_WEAK: "absent, but the import is weak: dyld binds it to NULL, not a stop",
    COVER_ABSENT_LAZY: "absent, but the import is LAZY: dyld defers it past load, not a stop (model)",
    COVER_MISSING: "absent from the field image, its re-exports and every image the process loads: "
                   "dyld ABORTS (model)",
    COVER_IMAGE_ABSENT: "the dependency field's own image is not in the prefix nor in Xcode's tree",
}


def parse_run_log(text):
    """What a guest run log observes, in order: the dlopen targets and the loader stops.

    Both are the run's OWN statements, which is what the entry and the reachable set are taken from
    (rather than theory): `dlopen(@rpath/libxcodebuildLoader.dylib, 1)` names what the workload
    opened, and `Symbol not found: X` / `Referenced from: Y` names a stop and the image that was
    being loaded when it happened.
    """
    dlopens = []
    for m in re.finditer(r"dlopen\(([^,)]+)", text):
        target = m.group(1).strip()
        if target not in dlopens:
            dlopens.append(target)
    stops = []
    for m in re.finditer(r"(?:Symbol not found|Library not loaded):\s*(\S+)\s*\n\s*Referenced from:\s*(\S+)",
                         text):
        stops.append({"symbol_or_image": m.group(1), "referrer": m.group(2)})
    if not stops:
        for m in re.finditer(r"Symbol not found:\s*(\S+)", text):
            stops.append({"symbol_or_image": m.group(1), "referrer": ""})
    return {"dlopens": dlopens, "stops": stops}


def workload_entry(log_path, xcode):
    """(entries, observed) for a run log: the workload the log's own runner script names, then the
    images the log says it dlopened.

    The log's sibling `.sh` is the recorded argv of the run, so the executable it names is the
    OBSERVED entry -- the tool that dlopens the loader and whose rpaths resolve @rpath.  `observed`
    keeps the log's own dlopen order AND a diagnosis of exactly what was found, because an entry that
    yields no images must say which of the things it looked for was missing rather than reading like
    "nothing to do".
    """
    observed = {"log": log_path, "kind": "", "executable": None, "script": None, "dlopens": [],
                "stops": [], "diagnosis": []}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        observed["diagnosis"].append(f"could not read {log_path}: {exc}")
        return [], observed
    observed["diagnosis"].append(f"read {log_path} ({len(text)} bytes)")
    guest_marks = [m for m in ("dlopen(", "Symbol not found", "Library not loaded", "XCB_RC=")
                   if m in text]
    observed["kind"] = "guest output" if guest_marks else "NOT a guest output"
    observed["diagnosis"].append(
        "entry kind: " + observed["kind"]
        + (f" ({', '.join(guest_marks)} present)" if guest_marks else
           " (no dlopen(, no 'Symbol not found', no 'Library not loaded', no XCB_RC= -- this looks "
           "like a job/harness log; pass the guest's .out file)"))
    parsed = parse_run_log(text)
    observed["dlopens"] = parsed["dlopens"]
    observed["stops"] = parsed["stops"]
    if not parsed["dlopens"]:
        observed["diagnosis"].append("no 'dlopen(' line: the log does not say what the workload opened")
    script = os.path.splitext(log_path)[0] + ".sh"
    observed["script"] = script
    entries = []
    if not os.path.isfile(script):
        observed["diagnosis"].append(f"runner script {script}: NOT FOUND -- the workload executable "
                                     "cannot be named from this log")
    else:
        try:
            script_text = open(script, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            script_text = ""
            observed["diagnosis"].append(f"runner script {script}: unreadable ({exc})")
        found = False
        for m in re.finditer(r'^"([^"]+)"\s+-', script_text, re.M):
            candidate = _host_entry_path(m.group(1), xcode)
            if os.path.isfile(candidate):
                observed["executable"] = candidate
                entries.append(candidate)
                observed["diagnosis"].append(f"runner script {script}: workload {candidate}")
                found = True
                break
            observed["diagnosis"].append(
                f"runner script {script}: command line names {m.group(1)}, which is not a file on "
                "the host")
        if not found and not observed["executable"]:
            observed["diagnosis"].append(f"runner script {script}: no quoted command line found")
    return entries, observed


def supply_route(field_install_name, field_path, prefix, xcode, ctx):
    """What would supply a missing SYMBOL of this dependency field, as one phrase (a model decision).

    The field image's own supply is what a symbol needs: a declared component in this tree means
    "implement it there", a prefix gap takes the same route `--sweep` gives the IMAGE, and an image
    only Xcode carries is Apple's, not a prefix gap.
    """
    if field_install_name is None:
        return "Apple-provided (dyld's flat lookup; else implement it in the demanding image)"
    if field_path and xcode and os.path.realpath(field_path).startswith(os.path.realpath(xcode)):
        return "Apple-provided (Xcode's own tree, not a prefix gap)"
    if field_path and prefix and os.path.realpath(field_path).startswith(os.path.realpath(prefix)):
        man = ctx.get("manifest") if ctx else None
        by_install = man[1] if man else {}
        if field_install_name in by_install:
            return f"implement in the declared component {by_install[field_install_name]['target']}"
        target, where = build_target_for(field_install_name, ctx.get("cmake_targets", {}) if ctx else {})
        if target:
            return f"implement in the tree's target {target} ({where})"
        return "implement in the image this prefix deploys for that field"
    route, _evidence = classify_route(field_install_name, [{"image": "/cover", "weak": False,
                                                           "upward": False}], ctx or {})
    if route == ROUTE_DECL:
        return "declare it (the tree already builds this image)"
    if route == ROUTE_PAYLOAD:
        return "deploy it from the payload or an overlay recipe"
    if route == ROUTE_SOURCE:
        return "build it from source"
    return "truthful substitute (no source for it in this workspace)"


def guest_resolver(prefix, xcode=None):
    """resolve(install_name, referrer=None, rpath_from=()) -> path|None, the way the guest's dyld
    resolves a name.

    Order: the deployed prefix (BOTH roots of the union -- forgetting libexec/darling reports phantom
    misses), then the @rpath entries of the referrer AND OF ITS LOADER CHAIN expanded against
    @loader_path, then Xcode's tree for the names the guest reaches through the host-root mount.
    None means "nowhere", which is what makes a missing dependency IMAGE a predicted stop rather
    than a silently skipped edge.

    THE RPATH CHAIN IS THE GUEST'S OWN dyld BEHAVIOUR, NOT A CONVENIENCE.  `ImageLoader::
    recursiveLoadLibraries` (darling/src/external/dyld/src/ImageLoader.cpp) builds
    `RPathChain thisRPaths(&loaderRPaths, &rpathsFromThisImage)` -- the image's own LC_RPATHs plus
    every rpath already in effect up the load chain -- and expands `@rpath/...` against THAT, so an
    image with NO LC_RPATH of its own (measured: .../SKAgent.xpc/Contents/Frameworks/SKIPC.dylib)
    still resolves `@rpath/SKSupport.dylib` through the rpath its loader carries (measured:
    SourceKit.framework's `@loader_path/XPCServices/com.apple.dt.SKAgent.xpc/Contents/Frameworks`).
    Resolving against the referrer's own rpaths alone is what used to report that existing image as
    absent.  `rpath_from` is that chain, nearest loader first, the loader's own rpaths always tried
    first -- the order `RPathChain` walks.
    """
    load_cache = {}

    def rpaths_of(holder):
        if holder not in load_cache:
            loaded = macho_load_commands(holder)
            slices = loaded["slices"]
            load_cache[holder] = slices[0]["rpaths"] if slices else []
        return load_cache[holder]

    def resolve(install_name, referrer=None, rpath_from=()):
        if not install_name:
            return None
        path = deployed_path(prefix, install_name)
        if path:
            return path
        if install_name.startswith("@rpath/") and referrer:
            for holder in (referrer,) + tuple(rpath_from):
                base = os.path.dirname(holder)
                for rpath in rpaths_of(holder):
                    expanded = rpath.replace("@loader_path", base)
                    expanded = expanded.replace("@executable_path", base)
                    if expanded.startswith("@"):
                        continue
                    tail = os.path.join(expanded, install_name[len("@rpath/"):])
                    for candidate in (tail, deployed_path(prefix, tail)):
                        if candidate and os.path.isfile(candidate):
                            return candidate
        if xcode:
            found = xcode_file(install_name, xcode)
            if found:
                return found
        return None
    return resolve


def cover_closure(entries, prefix, xcode=None, without_images=(), without_symbols=(),
                  max_images=600):
    """The guest's load closure at SYMBOL granularity, image by image.

    Breadth-first from `entries` over LC_LOAD_DYLIB/LC_LOAD_WEAK_DYLIB/LC_REEXPORT_DYLIB, each
    install name resolved with `guest_resolver` (prefix both roots, then Xcode), and each image's
    imports read with `image_imports` (chained fixups or dyld-info; one code path).  Every import is
    then resolved against the deployed closure and labelled (COVER_*): its field image's own trie,
    the field's nested re-export graph, dyld's flat lookup for the special ordinals, an absent weak
    import, or MISSING -- only an absent EAGER one can stop the loader.

    `without_images` treats an install name as NOT deployed and `without_symbols` treats a name as
    exported by nobody, so a past state can be reconstructed from the current prefix (dar-m3s0's
    stop history).  Returns the graph: entries, images, unresolved edges and the ordered events.
    """
    without_images = set(without_images)
    without_symbols = set(without_symbols)
    resolve = guest_resolver(prefix, xcode)
    detail_cache = {}
    chain_cache = {}

    def detail_of(path):
        if path not in detail_cache:
            detail_cache[path] = exports_detail(path)
        return detail_cache[path]

    def chain_of(path):
        if path not in chain_cache:
            chain_cache[path] = reexport_closure(path, reexports_of,
                                                 lambda n, p=path: resolve(n, p), detail_of)
        return chain_cache[path]

    # Phase 1: the graph itself.  Load commands and imports are decoded in-process (no subprocess per
    # edge), so the BFS is cheap even at a few hundred images.
    closure_of_path = {}
    graph = {"entries": [], "images": {}, "unresolved": [], "missing": [], "observed": None}
    queue = []
    for entry in entries:
        if os.path.isfile(entry) and _looks_like_run_log(entry):
            # The ENTRY taken from an observation: the workload the run's own script names, and the
            # images the log says it dlopened, rather than the loader bundle alone.
            log_entries, observed = workload_entry(entry, xcode)
            graph["observed"] = observed
            for log_entry in log_entries:
                path = log_entry if os.path.isfile(log_entry) else resolve(log_entry)
                graph["entries"].append({"entry": log_entry, "path": path, "from": "run log"})
                if path:
                    queue.append((path, None))
            for target in observed["dlopens"]:
                # @rpath resolves against the workload's OWN rpaths, so the executable is the
                # referrer -- without it the loader bundle (and its whole chain) stays unresolved.
                path = resolve(target, observed.get("executable"))
                graph["entries"].append({"entry": target, "path": path, "from": "dlopen in run log"})
                observed["diagnosis"].append(
                    f"dlopen target {target}: " + (f"resolved to {path}" if path else
                     "UNRESOLVED (no rpath of the workload matched and the name is not in the prefix "
                     "or Xcode's tree)"))
                if path:
                    queue.append((path, None))
            if not queue:
                graph["empty_reason"] = ("the log yielded no image to walk: "
                                         + "; ".join(observed["diagnosis"]))
            continue
        path = entry if os.path.isfile(entry) else resolve(entry)
        graph["entries"].append({"entry": entry, "path": path, "from": "argument"})
        if path and not (guest_slice(path) or {}).get("defined"):
            # A file that is neither a Mach-O image nor a guest run log: the mistake Main made with
            # a job log.  Say it, rather than reporting an image with zero imports.
            graph.setdefault("entry_problems", []).append(
                f"{entry} is neither a Mach-O image this reader can read nor a guest run log (no "
                "dlopen(, no 'Symbol not found', no XCB_RC=): pass the guest's .out file or an image")
            continue
        if path:
            queue.append((path, None))
    # The load chain, for `@rpath` expansion: dyld hands every image the rpath chain of the image
    # that required it (ImageLoader::recursiveLoadLibraries), so a name is resolved against the
    # referrer's own LC_RPATHs and then each ancestor's, nearest first (see guest_resolver).
    parent_of = {}

    def rpath_chain(name):
        """The ancestor image PATHS of `name`, nearest loader first (empty for an entry)."""
        chain = []
        seen = {name}
        parent = parent_of.get(name)
        while parent and parent not in seen:
            seen.add(parent)
            record = graph["images"].get(parent)
            if not record:
                break
            chain.append(record["path"])
            parent = parent_of.get(parent)
        return chain

    while queue and len(graph["images"]) < max_images:
        path, field_of = queue.pop(0)
        if path in closure_of_path:
            continue
        imports = image_imports(path)
        name = imports["dylib_id"] or path
        closure_of_path[path] = name
        dylibs = imports["dylibs"]
        graph["images"][name] = {"install_name": name, "path": path, "field_of": field_of,
                                 "form": imports["form"], "dylibs": dylibs,
                                 "imports": imports["imports"], "labels": {}}
        if field_of:
            parent_of[name] = field_of
        for install_name, weak, _reexport in dylibs:
            if install_name in without_images:
                graph["unresolved"].append({"referrer": name, "install_name": install_name,
                                            "weak": weak})
                continue
            dep_path = resolve(install_name, path, rpath_chain(name))
            if not dep_path:
                graph["unresolved"].append({"referrer": name, "install_name": install_name,
                                            "weak": weak})
                continue
            queue.append((dep_path, name))

    # Phase 2: what every image of the closure satisfies (one export read per image, and the withheld
    # symbols removed), plus the LOAD ORDER, because an image's imports can be satisfied by anything
    # the process has already loaded at that point.
    if not graph["images"]:
        graph["empty_reason"] = graph.get("empty_reason") or "; ".join(
            graph.get("entry_problems", ()) or
            ["no image could be read from the entries given: "
             + "; ".join(f"{e['entry']} -> {e['path']}" for e in graph["entries"])])
        graph.setdefault("order", [])
        graph.setdefault("reached", [])
        graph.setdefault("rest", [])
        return graph
    for record in graph["images"].values():
        detail = detail_of(record["path"])
        if without_symbols:
            detail = dict(detail, names=detail["names"] - set(without_symbols))
            detail_cache[record["path"]] = detail
    order = _cover_traversal(graph)
    graph["order"] = order
    graph["reached"] = order
    graph["rest"] = [name for name in graph["images"] if name not in order]

    # Phase 3: label every import.  The field image's nested re-export graph is walked only when the
    # name is not in the field's own trie, and cached per image; `loaded_paths` is the union of what
    # the images loaded up to and including this one satisfy (the field/load-order MODEL), and
    # `closure_paths` is every mapped image, which is what a field-less SPECIAL-ORDINAL bind (flat
    # or weak lookup) resolves against -- dyld binds only after the whole closure is mapped.
    closure_paths = [graph["images"][n]["path"] for n in order + graph["rest"]
                     if n in graph["images"]]
    loaded_paths = []
    for name in order + graph["rest"]:
        record = graph["images"].get(name)
        if not record:
            continue
        # The image being bound is itself part of "already loaded" for its own flat-lookup binds
        # (libobjc importing _objc_release from itself is the measured case).
        loaded_paths.append(record["path"])
        for imp in record["imports"]:
            field_path = None
            if imp["field"] is not None and imp["lib_ordinal"] - 1 < len(record["dylibs"]):
                field_install = record["dylibs"][imp["lib_ordinal"] - 1][0]
                if field_install not in without_images:
                    field_path = resolve(field_install, record["path"], rpath_chain(name))
            label, where = _cover_label(imp, field_path, detail_of, chain_of, loaded_paths,
                                        closure_paths)
            imp["label"], imp["where"] = label, where
            record["labels"][label] = record["labels"].get(label, 0) + 1
            if label in (COVER_MISSING, COVER_IMAGE_ABSENT):
                graph["missing"].append({"image": name, "path": record["path"],
                                         "symbol": imp["name"],
                                         "field": imp.get("field") or imp.get("special"),
                                         "label": label, "weak": imp["weak"]})
    return graph


def _looks_like_run_log(path):
    """Whether a file is a guest run log: the loader's own dlopen line or the harness's rc marker."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(200000)
    except OSError:
        return False
    return "dlopen(" in head or "XCB_RC=" in head or "Library not loaded" in head


def _guest_path(path, prefix, xcode):
    """The path the GUEST would print for a host file, so a report line reads like the run's own."""
    for base in (os.path.join(prefix, "libexec/darling"), prefix):
        if path.startswith(base + os.sep):
            return path[len(base):]
    if xcode and path.startswith(xcode + os.sep):
        return "/Volumes/SystemRoot" + path
    return path


def _absence_label(imp):
    """The label for a name nothing the process loads carries: only an eager, non-weak one stops."""
    if imp["weak"] or imp.get("special") == "weak-lookup":
        return COVER_ABSENT_WEAK
    if not imp.get("eager", True):
        return COVER_ABSENT_LAZY
    return COVER_MISSING


def supplied_by_loaded(name, loaded_paths, detail_of, chain_of):
    """The image the process has loaded that supplies `name` for a client, or None.

    A loaded image supplies what a CLIENT of it can resolve: its own trie first (one read per image),
    then the image's re-export graph -- which is why libSystem.B counts for a libsystem_c name.  The
    graph walk runs only for a name no loaded image's own trie carries, so the common case is cheap.
    """
    for path in loaded_paths:
        if name in detail_of(path)["names"]:
            return path
    for path in loaded_paths:
        names, _providers = chain_of(path)
        if name in names:
            return path
    return None


def _cover_label(imp, field_path, detail_of, chain_of, loaded_paths, closure_paths=()):
    """(label, where) for one import: the export trie, a re-export, a loaded image, or absent.

    `loaded_paths` is the images the process has loaded (bound) at or before this one, in order:
    STATIC for the field image, a MODEL for the flat/weak path -- the label says which.
    `closure_paths` is every image of the closure, in bind order, and it is what a FLAT lookup
    (a special-ordinal bind) resolves against: the guest's own dyld loads the WHOLE closure before
    it binds anything (`ImageLoader::recursiveLoadLibraries` then `recursiveRebase` then
    `recursiveBind`, darling/src/external/dyld/src/ImageLoader.cpp), and a flat lookup is
    `flatFindExportedSymbol` -> `findExportedSymbol`, which walks `sAllImages`.  Resolving a flat
    lookup against "the images traversed so far" instead under-approximates whenever the traversal
    order and dyld's load order disagree -- measured on /usr/lib/system/libunwind.dylib, whose
    `___stack_chk_guard`/`___stderrp` are exported by the prefix's libsystem_c.dylib and were
    reported missing because the model's post-order put libunwind first.
    """
    if imp["field"] is None:
        # dyld's special ordinals: a flat lookup searches every mapped image by name (and the
        # main-executable ordinal falls back to it), while the weak lookup additionally tolerates
        # absence -- that is where _memcpy/_memmove/_memset/_strlen live: names no dylib exports.
        where = supplied_by_loaded(imp["name"], closure_paths or loaded_paths, detail_of, chain_of)
        if where:
            return COVER_FLAT, f"{where} (mapped in the closure, flat lookup)"
        return _absence_label(imp), ""
    if field_path is None:
        return COVER_IMAGE_ABSENT, ""
    own = detail_of(field_path)
    if imp["name"] in own["names"]:
        how = "alias" if imp["name"] in own["aliases"] else "definition"
        return COVER_OWN, f"{field_path} ({how})"
    names, providers = chain_of(field_path)
    if imp["name"] in names:
        for install_name in sorted(providers):
            if imp["name"] in providers[install_name]["names"]:
                return COVER_REEXPORT, install_name
        return COVER_REEXPORT, "re-exported"
    where = supplied_by_loaded(imp["name"], loaded_paths, detail_of, chain_of)
    if where:
        # No field image carries it, but an image the process loads does: dyld's flat/weak path could
        # satisfy it, so calling it absent would over-report (a MODEL decision, labelled as one).
        return COVER_LOAD_ORDER, f"{where} (loaded)"
    return _absence_label(imp), ""


def cover_events(graph):
    """The stop sequence dyld would report, in traversal order.

    The loader walks the closure breadth-first and aborts at the first thing it cannot satisfy: an
    image whose load command resolves nowhere, or an eager import the field image cannot supply.  A
    run ends there, the operator supplies that one thing, and the next run stops at the next -- so the
    sequence is produced by re-walking with each stop treated as fixed, which is exactly what the
    ladder did one rung at a time.  Only eager demands are stops (a weak import binds to NULL).
    """
    events = []
    reached = set(graph.get("reached", graph["images"]))
    for edge in graph["unresolved"]:
        if not edge["weak"] and edge["referrer"] in reached:
            events.append({"kind": "image", "referrer": edge["referrer"],
                           "install_name": edge["install_name"]})
    for miss in graph["missing"]:
        if miss["label"] == COVER_MISSING and not miss["weak"] and miss["image"] in reached:
            events.append({"kind": "symbol", "referrer": miss["image"], "symbol": miss["symbol"],
                           "field": miss["field"]})
    order = {name: i for i, name in enumerate(_cover_traversal(graph))}
    events.sort(key=lambda e: (order.get(e.get("referrer"), len(order)), e.get("symbol") or ""))
    return events


def _cover_traversal(graph):
    """Install names in the order dyld BINDS them: post-order -- dependencies before their user.

    dyld's `loadDependents` loads each dependent (and its own dependents) before applying the
    image's own binds, so the image that fails first is the deepest one reached first, and an image's
    imports can be satisfied by everything loaded before it INCLUDING its dependencies.  The order is
    the whole point of predicting a stop, so it is modelled rather than approximated.
    """
    by_path = {record["path"]: name for name, record in graph["images"].items()}
    done = []
    in_progress = set()

    def visit(name, depth=0):
        if name in in_progress or name in done or depth > 200:
            return
        record = graph["images"].get(name)
        if not record:
            return
        in_progress.add(name)
        for install_name, _weak, _reexport in record["dylibs"]:
            leaf = column_key(install_name)
            for candidate in graph["images"]:
                if column_key(candidate) == leaf:
                    visit(candidate, depth + 1)
        in_progress.discard(name)
        done.append(name)

    for entry in graph["entries"]:
        name = by_path.get(entry.get("path"))
        if name:
            visit(name)
    return done


def _host_entry_path(entry, xcode):
    """A guest path for --entry mapped to the host: the load-command paths the guest prints.

    /Volumes/SystemRoot/... is the host reached through the mount, and a path containing the Xcode
    mount mark lives under the Xcode tree; anything else is already a host path.
    """
    if os.path.isfile(entry):
        return entry
    if entry.startswith("/Volumes/SystemRoot/"):
        candidate = entry[len("/Volumes/SystemRoot"):]
        if os.path.isfile(candidate):
            return candidate
    if GUEST_XCODE_MOUNT_MARK in entry:
        tail = entry.split(GUEST_XCODE_MOUNT_MARK, 1)[1]
        candidate = os.path.join(xcode, tail.lstrip("/"))
        if os.path.isfile(candidate):
            return candidate
    return entry


def print_cover(graph, prefix, ctx=None, xcode=None, out=sys.stdout):
    """The readable table: the entry and its source, the reached/rest split, and one line per
    remaining unmet eager demand of a REACHED image with what would supply it."""
    counts = {label: 0 for label in COVER_ORDER}
    for image in graph["images"].values():
        for imp in image["imports"]:
            counts[imp["label"]] = counts.get(imp["label"], 0) + 1
    reached = set(graph.get("reached", ()))
    print("SYMBOL COVERAGE -- every import of the load closure resolved against the deployed prefix.", file=out)
    print(f"  prefix : {prefix}", file=out)
    for entry in graph["entries"]:
        print(f"  entry  : {entry['entry']}  [{entry.get('from', 'argument')}]"
              f" -> {entry['path']}", file=out)
    if graph.get("observed"):
        observed = graph["observed"]
        print(f"  observed: run log {observed['log']}"
              + (f", workload {observed['executable']}" if observed["executable"] else "")
              + (f", dlopens {', '.join(observed['dlopens'])}" if observed["dlopens"] else ""),
              file=out)
        for note in observed.get("diagnosis", ()):
            print(f"  entry fact: {note}", file=out)
    print(f"  images : {len(graph['images'])}  (reached by this entry: {len(reached)}; "
          f"rest of the closure: {len(graph['images']) - len(reached)})", file=out)
    if graph.get("empty_reason"):
        print(file=out)
        print("== FAIL: nothing to cover -- " + graph["empty_reason"], file=out)
        print("   the zero above is 'I could not find an entry here', not 'nothing to do'", file=out)
        print(file=out)
        return counts
    if len(graph["images"]) and len(reached) < 5:
        print(f"  note : only {len(reached)} image(s) were reached, which usually means rpath "
              "expansion or the dlopen target failed -- see the entry facts above", file=out)
    print(f"  imports: {sum(counts.values())} -- " + ", ".join(
        f"{counts[label]} {label}" for label in COVER_ORDER if counts[label]), file=out)
    print("  labels : " + "; ".join(f"{label} = {COVER_HEADLINE[label]}" for label in COVER_ORDER
                                    if counts.get(label)), file=out)
    print("           (STATIC: " + ", ".join(l for l in COVER_ORDER if l not in COVER_MODEL)
          + "; MODEL: " + ", ".join(COVER_MODEL) + ")", file=out)
    print(file=out)

    stops = {}
    for miss in graph["missing"]:
        stops.setdefault(miss["image"], []).append(miss)
    print("== remaining unmet eager demands of REACHED images (a rung reads off one line)", file=out)
    printed = 0
    for name in graph.get("reached", ()):
        record = graph["images"].get(name)
        if not record:
            continue
        unmet = [m for m in stops.get(name, []) if m["label"] == COVER_MISSING and not m["weak"]]
        for miss in unmet:
            install = None
            for imp in record["imports"]:
                if imp["name"] == miss["symbol"]:
                    ordinal = imp["lib_ordinal"]
                    if 1 <= ordinal <= len(record["dylibs"]):
                        install = record["dylibs"][ordinal - 1][0]
                    break
            field_path = resolve_field(install, record["path"], prefix, xcode) if install else None
            route = supply_route(install, field_path, prefix, xcode, ctx or {})
            print(f"  {_guest_path(record['path'], prefix, xcode)}  {miss['symbol']}  "
                  f"{install or miss['field']}  {route}", file=out)
            printed += 1
    if not printed:
        print("  (none)", file=out)
    print(file=out)

    if graph["unresolved"]:
        print("== load commands that resolve nowhere (a missing IMAGE stops the load too)", file=out)
        for edge in graph["unresolved"]:
            if edge["referrer"] not in reached:
                continue
            print(f"  {edge['referrer']}  ->  {edge['install_name']}"
                  f"{'  [weak]' if edge['weak'] else ''}", file=out)
        print(file=out)
    rest_unmet = sum(1 for name, items in stops.items() if name not in reached
                     for m in items if m["label"] == COVER_MISSING and not m["weak"])
    print(f"  not reached by this entry: {len(graph['images']) - len(reached)} image(s) carry "
          f"{rest_unmet} further unmet eager demand(s) -- reported, not a worklist until an entry "
          "that reaches them is given", file=out)
    print(file=out)
    print("== predicted stop sequence (a MODEL: each run ends at the first of these)", file=out)
    for i, event in enumerate(cover_events(graph)[:20], 1):
        if event["kind"] == "image":
            print(f"  {i:>2}. IMAGE  {event['install_name']}  (needed by {event['referrer']})", file=out)
        else:
            print(f"  {i:>2}. SYMBOL {event['symbol']}  (needed by {event['referrer']}, "
                  f"field {event['field']})", file=out)
    print(file=out)
    return counts


def resolve_field(install_name, referrer, prefix, xcode):
    """Where a dependency field's image lives (the same resolution the walk used)."""
    return guest_resolver(prefix, xcode)(install_name, referrer)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--darling", default=DEFAULTS["darling"])
    ap.add_argument("--build", default=DEFAULTS["build"])
    ap.add_argument("--prefix", default=DEFAULTS["prefix"])
    ap.add_argument("--xcode", default=DEFAULTS["xcode"])
    ap.add_argument("--swift-bin", default=DEFAULTS["swift_bin"])
    ap.add_argument("--base", default=None, help="ladder base commit (default: derived from git)")
    ap.add_argument("--components", default=None,
                    help="comma-separated declared target names (default: the lock ladder list)")
    ap.add_argument("--referrers", choices=("closure", "tree"), default="closure",
                    help="closure (default): only images the guest's load graph reaches; "
                         "tree: every reference in the Xcode subtrees")
    ap.add_argument("--declarations", action="store_true",
                    help="report the declaration source and reconcile it against the CMake text "
                         "(no artifacts, no Mach-O: works with only --darling and --build)")
    ap.add_argument("--sweep", action="store_true",
                    help="group every missing install name of the load closure by the route that "
                         "supplies it (the wave, instead of one rung at a time); exits 1 while the "
                         "wave is non-empty, 0 when nothing needs supplying")
    ap.add_argument("--payload", default=None,
                    help="payload tree whose committed artifacts the deploy stages "
                         "(default: <darling>/src/external/swift)")
    ap.add_argument("--overlay", default=None,
                    help="overlay checkout holding the recipe directories "
                         "(default: <payload>/overlays)")
    ap.add_argument("--json", action="store_true", help="emit the analysis as JSON")
    ap.add_argument("--detail", action="store_true", help="print the per-component symbol detail")
    ap.add_argument("--cover", action="store_true",
                    help="symbol coverage: resolve EVERY import of the load closure against the "
                         "deployed prefix (chained fixups or dyld-info, both prefix roots, nested "
                         "re-exports and aliases) and predict which unmet eager demand stops the "
                         "loader first")
    ap.add_argument("--entry", action="append", default=None, metavar="PATH",
                    help="closure entry, repeatable (default: the Xcode loader image).  A guest path "
                         "(/Volumes/SystemRoot/... or a path inside the Xcode mount) is mapped to the "
                         "host")
    ap.add_argument("--order", type=int, default=0, metavar="N",
                    help="print the next N predicted stops (implies --cover)")
    ap.add_argument("--without", action="append", default=[], metavar="INSTALL_NAME",
                    help="treat this install name as NOT deployed, to reconstruct a past state")
    ap.add_argument("--without-symbol", action="append", default=[], metavar="SYMBOL",
                    help="treat this symbol as exported by nobody, to reconstruct a past state")
    args = ap.parse_args()

    global SWIFT_BIN, BUILD_DIR
    SWIFT_BIN = args.swift_bin
    BUILD_DIR = args.build

    sources = declaration_sources(args.darling, args.build)
    decls = sources["decls"]
    manifest = sources["manifest"]
    cmake_path = sources["cmake_path"]
    manifest_path = sources["manifest_path"]
    targets = sources["declaration_targets"]

    if args.declarations:
        return report_declarations(sources, args.darling)

    for tool in ("llvm-objdump", "llvm-nm"):
        if not os.path.isfile(swift(tool)):
            print(f"closure_analyze.py: missing {swift(tool)}; pass --swift-bin", file=sys.stderr)
            return 2
    if not os.path.isfile(os.path.join(args.xcode, "Frameworks/libxcodebuildLoader.dylib")):
        print(f"closure_analyze.py: no load-time entry image under {args.xcode}", file=sys.stderr)
        return 2

    base = args.base or ladder_base(args.darling)

    # The closure entries: --entry when given, else the Xcode loader image.  BOTH the cover and the
    # sweep read them (dar-nowc: --sweep used to hardcode the loader and silently ignore --entry, so
    # a wall inside a later-loaded bundle -- SWBBuildSystem's strong OSLog demand, for one -- could
    # not be swept at all and the wrong closure was reported as "the" wave).
    entries = args.entry or [os.path.join(args.xcode, "Frameworks/libxcodebuildLoader.dylib")]
    entries = [_host_entry_path(e, args.xcode) for e in entries]
    for e in entries:
        if not os.path.isfile(e):
            print(f"closure_analyze.py: entry image does not exist: {e}", file=sys.stderr)
            return 2

    if args.cover or args.order:
        graph = cover_closure(entries, args.prefix, args.xcode, without_images=args.without,
                              without_symbols=args.without_symbol)
        ctx = classify_context(args.darling, manifest, args.xcode,
                               payload=args.payload or os.path.join(args.darling, "src/external/swift"),
                               overlay=args.overlay)
        print("closure_analyze.py --cover")
        print(f"  darling      : {args.darling}")
        print(f"  prefix       : {args.prefix} (both roots: the root and libexec/darling)")
        print(f"  xcode        : {args.xcode}")
        if args.without:
            print(f"  withheld     : {', '.join(sorted(args.without))} (treated as NOT deployed)")
        if args.without_symbol:
            print(f"  withheld sym : {', '.join(sorted(args.without_symbol))}")
        print()
        counts = print_cover(graph, args.prefix, ctx, args.xcode)
        if graph.get("empty_reason"):
            if args.json:
                print(json.dumps({"entries": graph["entries"], "observed": graph.get("observed"),
                                  "empty_reason": graph["empty_reason"], "counts": counts},
                                 indent=2, sort_keys=True))
            return 2      # 2 = the ENTRY gave nothing to cover (distinct from 1 = stops found)
        events = cover_events(graph)
        if args.order:
            print(f"== next {args.order} predicted stop(s) ==")
            for i, event in enumerate(events[:args.order], 1):
                if event["kind"] == "image":
                    print(f"  {i}. IMAGE  {event['install_name']}   needed by {event['referrer']}")
                else:
                    print(f"  {i}. SYMBOL {event['symbol']}   needed by {event['referrer']} "
                          f"(field {event['field']})")
            print()
        if args.json:
            enc = {name: {k: (v if not isinstance(v, set) else sorted(v))
                          for k, v in record.items()} for name, record in graph["images"].items()}
            payload = {"darling": args.darling, "prefix": args.prefix, "xcode": args.xcode,
                       "entries": graph["entries"], "observed": graph.get("observed"),
                       "image_count": len(graph["images"]), "reached": graph.get("reached"),
                       "rest": graph.get("rest"), "order": graph.get("order"),
                       "counts": counts, "labels_static": [l for l in COVER_ORDER
                                                           if l not in COVER_MODEL],
                       "labels_model": list(COVER_MODEL),
                       "images": enc, "unresolved": graph["unresolved"],
                       "missing": graph["missing"], "events": cover_events(graph),
                       "note": ("loading is not implementing: this is the load-time coverage of the "
                                "deployed closure, and only an absent EAGER demand can stop the "
                                "loader; the reached/rest split and the predicted order are the "
                                "entry's, and the labels marked MODEL are decisions, not "
                                "observations")}
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if events else 0

    if args.sweep:
        payload = args.payload or os.path.join(args.darling, "src/external/swift")
        ctx = classify_context(args.darling, manifest, args.xcode, payload=payload,
                               overlay=args.overlay)
        # The referrer set must be the closure the ENTRY reaches, resolved the way dyld resolves it:
        # against the prefix's both roots, then the referrer's own rpath chain.  load_closure above
        # resolves a dependency by NAME alone (xcode_file), which reaches 2 of the 108 images the
        # SWBBuildService.bundle entry loads and therefore reported a wave of 9 rows for a closure
        # of 40 -- so the sweep takes its edges from the same walk --cover uses (dar-nowc, measured
        # 2026-10-15).  `dylibs` is already [(install_name, weak, reexport)] per image.
        graph = cover_closure(entries, args.prefix, args.xcode)
        edges = {rec["path"]: [tuple(d) for d in rec["dylibs"]]
                 for rec in graph["images"].values()}
        print("closure_analyze.py --sweep")
        print(f"  darling      : {args.darling}")
        print(f"  manifest     : {manifest_path}")
        print(f"  payload      : {payload}")
        print(f"  overlay      : {ctx['overlay_dir']}")
        print(f"  prefix       : {args.prefix}")
        print(f"  xcode        : {args.xcode}")
        print(f"  closure      : {len(graph['images'])} images reached from "
              + " + ".join(entries))
        print(f"  entry facts  : {', '.join(str(e.get('path')) for e in graph['entries'])}")
        if graph.get("empty_reason"):
            print(f"  EMPTY        : {graph['empty_reason']}")
        print()
        rows = sweep_missing(edges, args.prefix, ctx)
        counts = print_sweep(rows, args.prefix)
        if args.json:
            print(json.dumps([{k: (sorted(v) if isinstance(v, set) else v)
                               for k, v in row.items()} for row in rows], indent=2, sort_keys=True))
        return 1 if counts[ROUTE_DECL] + counts[ROUTE_PAYLOAD] + counts[ROUTE_SOURCE] + counts[ROUTE_SUBSTITUTE] else 0

    wanted = LOCK_TARGETS if not args.components else \
        [c.strip() for c in args.components.split(",") if c.strip()]

    edges = {}
    for e in entries:
        edges.update(load_closure(e, args.xcode))

    results = []
    for t in wanted:
        _, decl = resolve_decl(t, decls)
        if not decl:
            results.append({"target": t, "install_name": None, "klass": "STUB",
                            "class_notes": ["not declared in the tree's CMakeLists"],
                            "referrers": [], "demanded": {}, "missing": [], "exported": set(),
                            "stub_attributed": [], "truthful": [], "behavioural": [],
                            "unknown": [], "walk_truthful": [], "walk_behavioural": [],
                            "walk_source_changes": []})
            continue
        if args.referrers == "closure":
            refs = referrers_for(decl["install_name"], edges)
        else:
            refs = tree_referrers_for(decl["install_name"], args.xcode)
        results.append(analyze_component(t, decl, manifest, args.darling, base, refs))
    for r in results:
        dep, path = deployed_state(args.prefix, r.get("install_name"), r.get("artifact"))
        r["deployed"] = dep
        r["deployed_path"] = path

    if args.json:
        def enc(o):
            if isinstance(o, set):
                return sorted(o)
            if isinstance(o, dict) and all(isinstance(k, str) for k in o):
                return {k: enc(v) for k, v in o.items()}
            return o
        payload = {
            "darling": args.darling, "build": args.build, "prefix": args.prefix,
            "xcode": args.xcode, "declarations": cmake_path, "manifest": manifest_path,
            "ladder_base": base,
            "referrer_mode": args.referrers, "closure_entry": entry,
            "closure_image_count": len(edges),
            "note": ("loading is NOT implementing; the class describes what a client gets when it "
                     "uses the image, and no component here may be called fully supported"),
            "components": [{k: enc(v) for k, v in r.items()} for r in results],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("closure_analyze.py -- per-dependency implementation status after the load-time ladder")
    print(f"  declarations : {manifest_path} is the DECLARATION SOURCE; reconciled against "
          f"{cmake_path}")
    print(f"                 ({len(targets)} targets in DARLING_ROOTLESS_TOOLCHAIN_TARGETS, "
          f"{len(sources['decls'])} declarations: {len(sources['manifest_only'])} manifest-only, "
          f"{len(sources['text_only'])} CMake-text-only -- see --declarations)")
    print(f"  manifest     : {manifest_path}")
    print(f"  darling      : {args.darling}")
    print(f"  build        : {args.build}")
    print(f"  prefix       : {args.prefix} (deployment check only; artifacts come from the build)")
    print(f"  xcode        : {args.xcode}")
    print(f"  referrers    : {args.referrers} "
          f"({len(edges)} images in the load-command closure of {entry})")
    print(f"  ladder base  : {base}")
    print()
    print("LOADING IS NOT IMPLEMENTING.  A component below loaded; that says nothing about what it")
    print("implements.  No component in this table may be called fully supported.")
    print("A demanded symbol that a strong referrer binds non-lazily but the artifact does not")
    print("export is reported as incomplete; the runtime consequence of that is NOT measured here.")
    print()
    print("classes: " + " | ".join(f"{k} = {v}" for k, v in CLASS_LEGEND))
    for line in CLASS_CRITERIA:
        print("  " + line)
    print()
    hdr = (f"{'COMPONENT':<24} {'INSTALL NAME (guest_path)':<68} {'LINK':<6} "
           f"{'REF':>3} {'DEM':>4} {'EXP':>4} {'MISS':>4} {'TOL':>4} {'STUB':>4} {'FWD':>4} "
           f"{'DEPLOY':<7} CLASS")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        refs = r.get("referrers", [])
        miss = len(r.get("missing_fatal", ())) + len(r.get("missing_lazy", ()))
        dep = r.get("deployed", "-")
        print(f"{r['target']:<24} {(r['install_name'] or '(undeclared)'):<68} "
              f"{fmt_links(refs):<6} {len(refs):>3} {len(r.get('demanded', {})):>4} "
              f"{len(r.get('exported', ())):>4} {miss:>4} "
              f"{len(r.get('missing_tolerated', ())):>4} "
              f"{r.get('stub_string_count', 0):>4} {r.get('forward_string_count', 0):>4} "
              f"{dep:<7} {CLASS_SHORT.get(r['klass'], r['klass'])}")
    print()
    print("MISS = demanded symbols absent and required by a strong link with a non-weak bind;")
    print("TOL  = demanded symbols absent but only ever behind a weak link or weak bind;")
    print("DEPLOY = whether the prefix carries the install name and whether that copy is the built")
    print("         artifact (same/differs), so a stale prefix cannot be mistaken for the tree.")
    print()
    print("per-component class and the signal it came from:")
    for r in results:
        print(f"  {r['target']:<24} {CLASS_SHORT.get(r['klass'], r['klass'])}  "
              f"{'; '.join(r.get('class_notes', []))}")
        if r.get("walk_source_changes"):
            print(f"      ladder source changes: {', '.join(r['walk_source_changes'])}")

    if args.detail:
        print()
        print("detail: exact dependency field, referrers, demanded symbols and their status")
        for r in results:
            print(f"\n=== {r['target']}  install name {r['install_name']}")
            print(f"    dependency field (bind-table column): {r.get('column')}")
            print(f"    artifact: {r.get('artifact')}")
            for ref in r.get("referrers", []):
                link = "weak" if ref["weak"] else "strong"
                print(f"    referrer [{link}{', upward' if ref['upward'] else ''}] "
                      f"{ref['image']}  ({ref['dependent_symbol_count']} symbols from this field)")
                syms = r.get("demanded_by_referrer", {}).get(ref["image"], {})
                for sym in sorted(syms):
                    print(f"        {sym}  ({syms[sym]})")
            for sym, why in r.get("stub_attributed", []):
                print(f"    STUB      {sym}  [{why}]")
            for sym in r.get("missing_fatal", []):
                print(f"    MISSING   {sym}  [absent; a strong referrer binds it non-lazily]")
            for sym in r.get("missing_lazy", []):
                print(f"    MISSING   {sym}  [absent; a strong referrer binds it lazily]")
            for sym in r.get("missing_tolerated", []):
                print(f"    ABSENT-OK {sym}  [absent; only weak-linked referrers demand it]")
            for sym, why in r.get("truthful", []):
                print(f"    ABSENCE   {sym}  [{why}]")
            for sym, why in r.get("behavioural", []):
                print(f"    BEHAVIOUR {sym}  [{why}]")
            for sym, why in r.get("unknown", []):
                print(f"    UNKNOWN   {sym}  [{why}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())

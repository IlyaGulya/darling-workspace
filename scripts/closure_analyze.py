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
_DYLIB_ID_CACHE = {}


def dylib_id(image):
    """The image's own install name (LC_ID_DYLIB), or None when it has none."""
    if image not in _DYLIB_ID_CACHE:
        out = run([swift("llvm-objdump"), "--macho", "--dylib-id", image])
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        _DYLIB_ID_CACHE[image] = lines[1] if len(lines) > 1 else None
    return _DYLIB_ID_CACHE[image]


def binds_by_dependency(image):
    """dylib column name -> {symbol: bind kind}, from the referrer's own bind table.

    Cached per image for the life of the process: the sweep asks for the same referrer's table once
    per install name it appears in, and re-reading an image's bind table is one llvm-objdump run.
    """
    if image in _BINDS_CACHE:
        return _BINDS_CACHE[image]
    out = run([swift("llvm-objdump"), "--macho", "--dyld-info", image])
    table = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) < 8 or f[4] not in BIND_TYPES:
            continue
        dep, sym = f[6], f[7]
        if not sym.startswith("_"):
            continue
        table.setdefault(dep, {}).setdefault(sym, f[4])
    _BINDS_CACHE[image] = table
    return table


def exports(artifact):
    out = run([swift("llvm-objdump"), "--macho", "--exports-trie", artifact])
    names = set()
    for line in out.splitlines():
        m = re.match(r"^0x[0-9a-fA-F]+\s+(\S+)$", line.strip())
        if m:
            names.add(m.group(1))
    if names:
        return names
    for line in run([swift("llvm-nm"), "-g", "--defined-only", artifact]).splitlines():
        f = line.split()
        if len(f) == 3:
            names.add(f[2])
    return names


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
    """({names}, {install name: exports}) over the images re-export-reachable from `root`.

    Kept separate from the Mach-O readers so the SHAPE is contractable without llvm tools:
    `reexports_of(path)` names the install names an image re-exports, `resolve(install_name)` the
    path the deploy resolves one to (or None), `exports_of(path)` its export trie.  The walk is
    iterative, depth-bounded and cycle-safe, because the shape that matters is the NESTED one
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
            names |= got
            queue.append((path, depth + 1))
    return names, providers


def reexported_exports(image, prefix, max_depth=8):
    """(names, providers) what a deployed image satisfies through the dylibs it re-exports.

    Dyld resolves a client's binding through the image's whole re-export graph, so these names are
    part of what the deployed image supplies.  Each re-exported install name is resolved inside the
    prefix the way the deploy resolves it (both prefix roots), and `providers` maps each re-exported
    install name to its own export trie so the caller can say WHERE a name came from.
    """
    return reexport_closure(image, reexports_of, lambda n: deployed_path(prefix, n), exports,
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

    entry = os.path.join(args.xcode, "Frameworks/libxcodebuildLoader.dylib")
    edges = load_closure(entry, args.xcode)

    if args.sweep:
        payload = args.payload or os.path.join(args.darling, "src/external/swift")
        ctx = classify_context(args.darling, manifest, args.xcode, payload=payload,
                               overlay=args.overlay)
        print("closure_analyze.py --sweep")
        print(f"  darling      : {args.darling}")
        print(f"  manifest     : {manifest_path}")
        print(f"  payload      : {payload}")
        print(f"  overlay      : {ctx['overlay_dir']}")
        print(f"  prefix       : {args.prefix}")
        print(f"  xcode        : {args.xcode}")
        print(f"  closure      : {len(edges)} images in the load-command closure of {entry}")
        print()
        rows = sweep_missing(edges, args.prefix, ctx)
        counts = print_sweep(rows, args.prefix)
        if args.json:
            print(json.dumps([{k: (sorted(v) if isinstance(v, set) else v)
                               for k, v in row.items()} for row in rows], indent=2, sort_keys=True))
        return 1 if counts[ROUTE_DECL] + counts[ROUTE_PAYLOAD] + counts[ROUTE_SOURCE] + counts[ROUTE_SUBSTITUTE] else 0

    wanted = LOCK_TARGETS if not args.components else \
        [c.strip() for c in args.components.split(",") if c.strip()]

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

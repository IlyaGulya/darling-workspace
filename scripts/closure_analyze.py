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
  * target and install name: the JSON snippets appended to DARLING_ROOTLESS_TOOLCHAIN_TARGETS'
    component in <darling>/CMakeLists.txt (`{"target": ..., "guest_path": ..., "host_path":
    "$<TARGET_FILE:...>"}`), which is the same source the rootless manifest is generated from;
  * the built artifact: that generated manifest
    (<build>/darling-rootless-toolchain.json), matched by target name and then by guest_path;
  * provenance: git, over the ladder's own commit range (default: everything after the parent of
    the oldest reachable commit whose subject starts with "rootless toolchain").

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

LOCK_TARGETS = [
    "UniformTypeIdentifiers", "Rosetta", "MobileAsset", "CoreData", "ImageIO", "CoreAnalytics",
    "AssertionServices", "CoreGraphics", "DiskArbitration", "DiskManagement", "AppleSystemInfo",
    "AssetCacheServices", "DiskImages", "CrashReporterSupport", "compression", "AppleArchive",
    "PlugInKit", "libdtrace", "NetworkStatistics", "kperf", "StreamingZip", "MetalNoGPU",
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
    r'"target":\s*"([^"]+)"\s*,\s*"guest_path":\s*"([^"]+)"\s*,\s*"host_path":\s*"\$<TARGET_FILE:([^>]+)>"'
)
TARGETS_RE = re.compile(r"set\(DARLING_ROOTLESS_TOOLCHAIN_TARGETS(.*?)\n\)", re.S)


def read_declarations(darling):
    """(declared targets, [(declared target, install name)] from the tree's own CMakeLists."""
    path = os.path.join(darling, "CMakeLists.txt")
    text = open(path, encoding="utf-8", errors="replace").read()
    # The declarations live inside CMake string literals, so their quotes are escaped.
    text = text.replace('\\"', '"')
    m = TARGETS_RE.search(text)
    targets = []
    if m:
        for line in m.group(1).splitlines():
            line = line.split("#")[0].strip()
            if line:
                targets.append(line)
    decls = {}
    for tgt, guest, cmake_target in DECL_RE.findall(text):
        # The security entry names a different CMake target than its declaration name.
        decls[tgt] = {"target": tgt, "install_name": guest, "cmake_target": cmake_target}
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


def deployed_state(prefix, install_name, artifact):
    """Whether the prefix carries this install name, and whether that copy is the built artifact."""
    if not prefix or not install_name:
        return "-", None
    for base in (prefix, os.path.join(prefix, "libexec/darling")):
        p = os.path.join(base, install_name.lstrip("/"))
        if os.path.isfile(p):
            if not artifact:
                return "present", p
            try:
                a = hashlib.sha256(open(p, "rb").read()).hexdigest()
                b = hashlib.sha256(open(artifact, "rb").read()).hexdigest()
            except OSError:
                return "present", p
            return ("same" if a == b else "differs"), p
    return "-", None


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


def binds_by_dependency(image):
    """dylib column name -> {symbol: bind kind}, from the referrer's own bind table."""
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
    """Images in the closure whose own load commands name this exact install name."""
    found = []
    for img, deps in edges.items():
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
        for dep, weak, upward in dylibs_used(cand):
            if dep == install_name:
                found.append((cand, weak, upward))
                break
    return sorted(found)


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
    for img, weak, upward in referrers:
        table = binds_by_dependency(img)
        syms = table.get(result["column"], {})
        result["referrers"].append({"image": img, "weak": weak, "upward": upward,
                                    "dependent_symbol_count": len(syms)})
        result["demanded_by_referrer"][img] = syms
        for sym, kind in syms.items():
            result["demanded"].setdefault(sym, kind)

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

    demanded = result["demanded"]
    result["missing"] = sorted(s for s in demanded if s not in exported)

    # A demanded symbol that is absent is only an incompleteness when a referrer that links the
    # component STRONGLY demands it with a non-weak bind: a weak-linked image (LC_LOAD_WEAK_DYLIB)
    # has its references resolved weakly by dyld, and a lazy bind is not resolved at load at all.
    fatal, lazy, tolerated = [], [], []
    for sym in result["missing"]:
        kinds = []
        for ref in result["referrers"]:
            kind = result["demanded_by_referrer"].get(ref["image"], {}).get(sym)
            if kind is not None:
                kinds.append((ref["weak"], kind))
        if any((not weak) and kind == "bind" for weak, kind in kinds):
            fatal.append(sym)
        elif any((not weak) and kind != "weak_bind" for weak, kind in kinds):
            lazy.append(sym)
        else:
            tolerated.append(sym)
    result["missing_fatal"] = fatal
    result["missing_lazy"] = lazy
    result["missing_tolerated"] = tolerated

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
        name = sym[1:] if sym.startswith("_") else sym
        if name.startswith("OBJC_CLASS_$_"):
            name = name[len("OBJC_CLASS_$_"):]
        return any(name in text for text in walk_text.values())

    truthful_syms = {s for s, _ in result["truthful"]}
    walk_truthful = [s for s in truthful_syms if walk_attributed(s)]
    walk_value = [s for s, _ in result["behavioural"] if walk_attributed(s)]
    walk_behavioural = walk_value
    result["walk_truthful"] = walk_truthful
    result["walk_behavioural"] = walk_behavioural

    empty_surface = len(exported) == 0
    if fatal or lazy:
        result["klass"] = "STUB"
        detail = fatal + lazy
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
    ap.add_argument("--json", action="store_true", help="emit the analysis as JSON")
    ap.add_argument("--detail", action="store_true", help="print the per-component symbol detail")
    args = ap.parse_args()

    global SWIFT_BIN, BUILD_DIR
    SWIFT_BIN = args.swift_bin
    BUILD_DIR = args.build

    for tool in ("llvm-objdump", "llvm-nm"):
        if not os.path.isfile(swift(tool)):
            print(f"closure_analyze.py: missing {swift(tool)}; pass --swift-bin", file=sys.stderr)
            return 2
    if not os.path.isfile(os.path.join(args.xcode, "Frameworks/libxcodebuildLoader.dylib")):
        print(f"closure_analyze.py: no load-time entry image under {args.xcode}", file=sys.stderr)
        return 2

    targets, decls, cmake_path = read_declarations(args.darling)
    manifest, manifest_path = read_manifest(args.build)
    # Declarations for components the CMake TEXT cannot express.  A Swift-payload dylib is declared by
    # a generated resource entry (`{"target": "swift_libswiftX_dylib", ...}`) built from a
    # `${_swift_name}` template inside a foreach, so the declaration regex over CMakeLists.txt yields
    # nothing for it.  The manifest is the RESOLVED deployment declaration for exactly those entries,
    # so fill any target the text did not declare from it.  setdefault, so an explicit CMake
    # declaration (with its $<TARGET_FILE:...> host path) always wins.
    if manifest:
        _by_target, _by_install = manifest
        for _tgt, _entry in _by_target.items():
            decls.setdefault(_tgt, {"target": _tgt, "install_name": _entry["guest_path"],
                                    "cmake_target": _tgt})
    base = args.base or ladder_base(args.darling)

    entry = os.path.join(args.xcode, "Frameworks/libxcodebuildLoader.dylib")
    edges = load_closure(entry, args.xcode)

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
    print(f"  declarations : {cmake_path} "
          f"({len(targets)} targets in DARLING_ROOTLESS_TOOLCHAIN_TARGETS)")
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

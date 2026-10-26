#!/usr/bin/env python3
"""Contracts for the rung-loop tooling: declarations, sweep routes, the driver, and pin sync.

The rung loop was five hand steps per rung, repeated for four rungs.  These are the tools that
replaced it, and this covers the decisions they make, because each one is a decision that was made
wrong by hand at least once:

  * the declaration source is the generated MANIFEST (dar-a0ty), reconciled against the CMake text in
    both directions, so a payload-only declaration is not invisible and a CMake declaration the
    manifest does not carry is reported instead of trusted;
  * the wave sweep's route for a missing install name (weak-only, declaration, payload, source,
    substitute) and the two non-gap routes (Xcode-provided, no-referrer), each with its evidence --
    where a source rung must hold an implementation of a demanded name and not merely a matching
    directory (dar-xovc), and where a demand satisfied through a re-export (including the NESTED
    umbrella -> hub -> providers chain) or made only by a weak/upward referrer is not a failure
    while a strong absence still is (dar-m3s0);
  * the loader stop parse, including a log with only dyld's first line;
  * pin sync's rewrite: only a revision value changes, a forward pin moves, an ahead pin is refused
    unless told otherwise, and a manifest with nothing to change is reported as no change.

Every case is synthetic: no Xcode tree, prefix or build directory is needed.
The cover CLI cases use installed LLVM tools when available; the other cases
require no LLVM.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ca = load("rung_closure_analyze", "closure_analyze.py")
driver = load("rung_driver", "rung-driver.py")
pin = load("rung_pin_sync", "west-pin-sync.py")


def check(condition, message):
    if not condition:
        raise SystemExit(f"rung-tooling-contract: {message}")


# --------------------------------------------------------------------------- declaration source

def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def declaration_fixture(tmp):
    darling = tmp / "darling"
    build = tmp / "build"
    write(darling / "CMakeLists.txt", """\
set(DARLING_ROOTLESS_TOOLCHAIN_TARGETS
\tWidget
\tGadget
)
if(DARLING_ROOTLESS_TOOLCHAIN)
\tstring(APPEND _extra
\t\t",\\n    {\\"target\\": \\"framework_Widget\\", \\"guest_path\\": \\"/System/Library/Frameworks/Widget.framework/Versions/A/Widget\\", \\"host_path\\": \\"$<TARGET_FILE:Widget>\\"}")
\tstring(APPEND _extra
\t\t",\\n    {\\"target\\": \\"installer\\", \\"guest_path\\": \\"/usr/bin/installer\\", \\"host_path\\": \\"$<TARGET_FILE:installer>\\"}")
endif()
""")
    write(darling / "cmake" / "Spelling.cmake", """\
foreach(_pair IN ITEMS "Gadget|/usr/lib/libGadget.dylib")
\tstring(APPEND _extra ",\\n    {\\"target\\": \\"${_pair}\\"}")
endforeach()
""")
    manifest = {
        "schema": 1,
        "entrypoints": [
            {"target": "framework_Widget", "guest_path": "/System/Library/Frameworks/Widget.framework/Versions/A/Widget",
             "host_path": "/build/Widget"},
            {"target": "Gadget", "guest_path": "/usr/lib/libGadget.dylib", "host_path": "/build/libGadget.dylib"},
            {"target": "swift_libswiftWidget_dylib", "guest_path": "/usr/lib/swift/libswiftWidget.dylib",
             "host_path": "/build/rootless-toolchain-resources/usr/lib/swift/libswiftWidget.dylib"},
            {"target": "installer", "guest_path": "/usr/bin/installer", "host_path": "/build/installer"},
        ],
        "resources": [
            {"target": "homebrew-perl:AppendToPath", "guest_path": "/Library/Perl/5.18/AppendToPath",
             "host_path": "/build/perl/AppendToPath"},
            {"target": "locale_src", "guest_path": "/usr/share/locale/en_US.UTF-8/LC_TIME",
             "host_path": "/build/locale/LC_TIME"},
        ],
    }
    write(build / "darling-rootless-toolchain.json", json.dumps(manifest, indent=1))
    write(build / "darling-rootless-bootstrap.json", json.dumps({
        "schema": 2,
        "entrypoints": [{"target": "mldr", "guest_path": "/usr/libexec/darling/mldr", "host_path": "/b/mldr"}],
        "resources": [],
    }))
    return darling, build


def test_declarations(tmp):
    darling, build = declaration_fixture(tmp)
    sources = ca.declaration_sources(str(darling), str(build))
    kinds = sources["manifest_only_kind"]
    check(sources["text_only"] == [],
          f"every CMake-text declaration is in the manifest, got {sources['text_only']}")
    check(sorted(sources["declared_by_text"]) == ["Gadget", "framework_Widget", "installer"],
          f"text declarations read: {sources['declared_by_text']}")
    check(kinds["payload"] == ["swift_libswiftWidget_dylib"],
          f"payload-only declarations: {kinds['payload']}")
    check(kinds["staged"] == ["homebrew-perl:AppendToPath"],
          f"staged declarations: {kinds['staged']}")
    check(kinds["unexplained"] == ["locale_src"],
          f"unexplained declarations: {kinds['unexplained']}")
    check([t for t, _ in sources["text_elsewhere"]] == [],
          "a bootstrap manifest absent from --build names nothing; nothing should be elsewhere")
    # The manifest is the declaration source: a payload-only target resolves, and a LOCK_TARGETS
    # entry naming it needs no hand-written declaration anywhere.
    name, decl = ca.resolve_decl("swift_libswiftWidget_dylib", sources["decls"])
    check(decl is not None and decl["install_name"] == "/usr/lib/swift/libswiftWidget.dylib",
          "the manifest declares the payload dylib and resolve_decl finds it")

    # The other direction: a CMake declaration the manifest does not carry is a deployment gap.
    manifest_path = build / "darling-rootless-toolchain.json"
    data = json.loads(manifest_path.read_text())
    data["entrypoints"] = [e for e in data["entrypoints"] if e["target"] != "Gadget"]
    manifest_path.write_text(json.dumps(data, indent=1))
    sources = ca.declaration_sources(str(darling), str(build))
    check([t for t, _ in sources["text_only"]] == ["Gadget"],
          f"the manifest gap is reported, got {sources['text_only']}")

    # And a declaration of ANOTHER component is not this component's gap.
    data["entrypoints"].append({"target": "mldr", "guest_path": "/usr/libexec/darling/mldr",
                                "host_path": "/b/mldr"})
    manifest_path.write_text(json.dumps(data, indent=1))
    write(darling / "CMakeLists.txt", (darling / "CMakeLists.txt").read_text() + """\
string(APPEND _boot ",\\n    {\\"target\\": \\"mldr\\", \\"guest_path\\": \\"/usr/libexec/darling/mldr\\", \\"host_path\\": \\"$<TARGET_FILE:mldr>\\"}")
""")
    sources = ca.declaration_sources(str(darling), str(build))
    check([t for t, _ in sources["text_elsewhere"]] == [] and
          [t for t, _ in sources["text_only"]] == ["Gadget"],
          "the bootstrap manifest only subtracts when --build carries it")
    print("PASS declarations: manifest is the source, both directions reported")


# --------------------------------------------------------------------------- sweep routes

def classify_fixture(tmp):
    """A tree, a manifest and a payload that give every route one unambiguous case."""
    darling, build = declaration_fixture(tmp)
    sources = ca.declaration_sources(str(darling), str(build))
    payload = tmp / "payload"
    write(payload / "libswiftOrphan.dylib", "payload bytes")
    write(payload / "overlays" / "OverlayOnly" / "build.sh", "#!/bin/sh\n")
    # A CMake build target the manifest does not declare, and a source tree with no target at all.
    write(darling / "src" / "frameworks" / "OnlyBuilt" / "CMakeLists.txt", "add_framework(OnlyBuilt FAT)\n")
    # dar-xovc: a SOURCE row is a promise to build, so only the first of these three may take it --
    # a tree whose implementation file defines the demanded name, a header-only tree that merely
    # declares it, and a same-named C umbrella for a different (here Swift overlay) image.
    write(darling / "src" / "frameworks" / "SourceOnly" / "src" / "SourceOnly.m",
          "void SourceOnlyWidget(void) { }\n")
    write(darling / "src" / "frameworks" / "HeaderOnly" / "include" / "HeaderOnly" / "HeaderOnly.h",
          "void SourceOnlyWidget(void);\nvoid HeaderOnlyThing(void);\n")
    write(darling / "src" / "frameworks" / "UmbrellaSwift" / "src" / "UmbrellaSwift.c",
          "void UmbrellaSwift_C_entry(void) { }\n")
    ctx = ca.classify_context(str(darling), sources["manifest"], str(tmp / "no-xcode"),
                              payload=str(payload))
    ctx["xcode_provides"] = lambda name: (name.startswith("@rpath/"), "stub Xcode")
    return ctx


def test_routes(tmp):
    ctx = classify_fixture(tmp)

    def refs(*weak):
        return [{"image": f"/x/{i}.dylib", "weak": w, "upward": False} for i, w in enumerate(weak)]

    # The manifest's own declaration is authoritative and says so, not "[heuristic]".
    route, why = ca.classify_route("/usr/lib/libGadget.dylib", refs(False), ctx)
    check(route == ca.ROUTE_DECL and why.startswith("[manifest]"),
          f"an exact manifest guest_path match is authoritative: {route} ({why})")
    route, why = ca.classify_route("/System/Library/Frameworks/Widget.framework/Versions/A/Widget",
                                   refs(False), ctx)
    check(route == ca.ROUTE_DECL and why.startswith("[manifest]"),
          f"the declared framework is a declaration rung: {route} ({why})")

    # A CMake build target with no manifest entry is a declaration rung; the evidence names it.
    route, why = ca.classify_route("/System/Library/Frameworks/OnlyBuilt.framework/Versions/A/OnlyBuilt",
                                   refs(False), ctx)
    check(route == ca.ROUTE_DECL and "add_framework(OnlyBuilt" in why,
          f"a CMake build target is a declaration rung: {route} ({why})")

    # The payload's own artifact, and an overlay recipe in the overlay checkout.
    route, why = ca.classify_route("/usr/lib/swift/libswiftOrphan.dylib", refs(False), ctx)
    check(route == ca.ROUTE_PAYLOAD and "libswiftOrphan.dylib" in why,
          f"a payload artifact is a payload rung: {route} ({why})")
    route, why = ca.classify_route("/System/Library/PrivateFrameworks/OverlayOnly.framework/Versions/A/OverlayOnly",
                                   refs(False), ctx)
    check(route == ca.ROUTE_PAYLOAD and "overlays/OverlayOnly" in why,
          f"an overlay recipe is a payload rung: {route} ({why})")

    # A source tree is a source rung only when it holds an implementation of a demanded name
    # (dar-xovc).  The same demanded name is declared in the header-only tree and absent from the
    # umbrella tree, and neither of those is a build; with no demanded name to find at all (the
    # referrers' tables unreadable), a matching directory cannot be promoted to a build here either.
    route, why = ca.classify_route("/System/Library/Frameworks/HeaderOnly.framework/Versions/A/HeaderOnly",
                                   refs(False), ctx)
    check(route == ca.ROUTE_SUBSTITUTE and "src/frameworks/HeaderOnly" in why,
          f"a header-only tree is not a source rung: {route} ({why})")
    route, why = ca.classify_route("/usr/lib/swift/libswiftUmbrellaSwift.dylib", refs(False), ctx)
    check(route == ca.ROUTE_SUBSTITUTE and "src/frameworks/UmbrellaSwift" in why,
          f"a same-named C umbrella is not a source rung: {route} ({why})")

    demanded = {"_SourceOnlyWidget": "bind"}
    route, why = ca.classify_route("/System/Library/Frameworks/SourceOnly.framework/Versions/A/SourceOnly",
                                   refs(False), ctx, demanded)
    check(route == ca.ROUTE_SOURCE and "src/frameworks/SourceOnly" in why
          and "SourceOnlyWidget" in why and "SourceOnly.m" in why,
          f"a source tree implementing a demanded name is a source rung: {route} ({why})")
    route, why = ca.classify_route("/System/Library/Frameworks/HeaderOnly.framework/Versions/A/HeaderOnly",
                                   refs(False), ctx, demanded)
    check(route == ca.ROUTE_SUBSTITUTE and "src/frameworks/HeaderOnly" in why,
          f"a header-only tree that declares the name is NOT a source rung: {route} ({why})")
    route, why = ca.classify_route("/usr/lib/swift/libswiftUmbrellaSwift.dylib", refs(False), ctx,
                                   {"_$s10UmbrellaSwift4BNNSO5ShapeOMa": "bind"})
    check(route == ca.ROUTE_SUBSTITUTE and "src/frameworks/UmbrellaSwift" in why,
          f"a same-named C umbrella under a Swift overlay name is NOT a source rung: {route} ({why})")
    # The same tree asked about a name it does implement still takes the source route, so the guard
    # is the implemented name and not the directory.
    route, why = ca.classify_route("/System/Library/Frameworks/SourceOnly.framework/Versions/A/SourceOnly",
                                   refs(False), ctx, {"_UmbrellaSwift_C_entry": "bind"})
    check(route == ca.ROUTE_SUBSTITUTE,
          f"a demanded name the tree does not define is not a source rung either: {route} ({why})")

    # A name with nothing at all.
    route, why = ca.classify_route("/System/Library/PrivateFrameworks/NothingAnywhere.framework/Versions/A/NothingAnywhere",
                                   refs(False), ctx)
    check(route == ca.ROUTE_SUBSTITUTE,
          f"nothing matches -> substitute, and the evidence lists what was searched: {route} ({why})")

    # weak-only is a non-blocker, and one strong referrer among weak ones is not weak-only.
    route, why = ca.classify_route("/usr/lib/libWeak.dylib", refs(True, True), ctx)
    check(route == ca.ROUTE_WEAK and "LC_LOAD_WEAK_DYLIB" in why,
          f"weak-only is excluded from the wave: {route} ({why})")
    route, _why = ca.classify_route("/usr/lib/libWeak.dylib", refs(True, False), ctx)
    check(route != ca.ROUTE_WEAK, "one strong referrer is not a weak-only absence")

    # No referrer at all is not a weak-only absence (the vacuous case) and not a wave item either.
    route, why = ca.classify_route("/usr/lib/libNobody.dylib", [], ctx)
    check(route == ca.ROUTE_ORPHAN and "LC_ID_DYLIB" in why,
          f"no referrer is its own route: {route} ({why})")

    # Xcode's own tree is decided by the sweep before any route is classified.
    check(ctx["xcode_provides"]("@rpath/Widget.framework/Versions/A/Widget") == (True, "stub Xcode"),
          "the injected Xcode resolver answers for @rpath names")
    check(ca.xcode_provides("/usr/lib/swift/libswiftWidget.dylib", str(tmp / "no-xcode")) == (False, ""),
          "an absolute guest path is never 'provided by Xcode' (its basename collides with Xcode's "
          "own copy, and the guest's /usr is the prefix's)")

    # The Swift-overlay guard: a target named after the framework must not be taken for the overlay.
    check(ca.build_target_for("/usr/lib/swift/libswiftWidget.dylib", {"Widget": "x:1"}) == (None, None),
          "the Swift overlay lookup must not accept the framework's target name")
    check(ca.build_target_for("/System/Library/Frameworks/Widget.framework/Versions/A/Widget",
                              {"Widget": "x:1"}) == ("Widget", "x:1"),
          "the framework lookup does accept it")
    # ... and the same guard holds for a DECLARATION: the framework's manifest entry must not answer
    # the Swift overlay over it (dar-xovc, through the install-name stem instead of a directory name).
    from_framework_decl = {"/System/Library/Frameworks/Widget.framework/Versions/A/Widget":
                           {"target": "framework_Widget", "host_path": "/b/Widget",
                            "guest_path": "/System/Library/Frameworks/Widget.framework/Versions/A/Widget"}}
    overlay_ctx = dict(ctx)
    overlay_ctx["manifest"] = ({}, from_framework_decl)
    route, why = ca.classify_route("/usr/lib/swift/libswiftWidget.dylib", refs(False), overlay_ctx)
    check(route != ca.ROUTE_DECL,
          f"the framework's declaration must not answer the Swift overlay: {route} ({why})")
    route, why = ca.classify_route("/System/Library/Frameworks/Widget.framework/Versions/A/Widget",
                                   refs(False), overlay_ctx)
    check(route == ca.ROUTE_DECL and "framework_Widget" in why,
          f"the framework's own declaration still answers the framework: {route} ({why})")
    check(ca.target_name_candidates("/usr/lib/swift/libswiftWidget.dylib") == ["libswiftWidget"],
          "the overlay name has exactly one build/declaration candidate")
    check("Widget" in ca.target_name_candidates(
              "/System/Library/Frameworks/Widget.framework/Versions/A/Widget"),
          "a framework install name still offers its short name")
    print("PASS routes: every route reached, with evidence, and the overlay/framework split kept")


def test_sweep(tmp):
    """The sweep groups the whole closure's missing set: the same classifier, one pass, no prefix."""
    ctx = classify_fixture(tmp)
    prefix = tmp / "empty-prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    edges = {
        "/x/one.dylib": [
            ("@rpath/Widget.framework/Versions/A/Widget", False, False),
            ("/usr/lib/libGadget.dylib", False, False),
            ("/usr/lib/libWeakOnly.dylib", True, False),
            ("/System/Library/PrivateFrameworks/NothingAnywhere.framework/Versions/A/NothingAnywhere",
             False, False),
        ],
    }
    rows = {row["install_name"]: row for row in ca.sweep_missing(edges, str(prefix), ctx)}
    check(len(rows) == 4, f"every missing name is a row, got {sorted(rows)}")
    check(rows["@rpath/Widget.framework/Versions/A/Widget"]["route"] == ca.ROUTE_XCODE,
          "Xcode-provided names are their own group")
    check(rows["/usr/lib/libGadget.dylib"]["route"] == ca.ROUTE_DECL,
          "the declared name is a declaration rung")
    check(rows["/usr/lib/libWeakOnly.dylib"]["route"] == ca.ROUTE_WEAK,
          "the weakly linked name is excluded from the wave")
    check(rows["/System/Library/PrivateFrameworks/NothingAnywhere.framework/Versions/A/NothingAnywhere"]
          ["route"] == ca.ROUTE_SUBSTITUTE, "the unsupplied name is a substitute rung")
    check(rows["/usr/lib/libWeakOnly.dylib"]["referrers"][0]["weak"] is True,
          "the referrer's weak link is carried into the row")
    # An install name the prefix carries is not in the sweep at all.
    deployed = prefix / "usr/lib/libGadget.dylib"
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(b"present")
    rows = {row["install_name"] for row in ca.sweep_missing(edges, str(prefix), ctx)}
    check("/usr/lib/libGadget.dylib" not in rows,
          "a name the prefix carries is not part of the wave")
    print("PASS sweep: the whole missing set groups by route in one pass")


# --------------------------------------------------------------------------- verifier demand

def test_demand_split(tmp):
    """The driver's step-1 classification (dar-m3s0): a re-export and a weak/upward referrer are not
    failures, while a strong referrer's absent, non-lazily bound demand still is.

    This is the classification the analyzer and the driver now share, so a tolerant absence can no
    longer be re-derived as fatal from a raw count of demanded names against one export trie.
    """
    strong = [{"image": "/x/StrongClient.dylib", "weak": False, "upward": False}]
    weak = [{"image": "/x/WeakClient.dylib", "weak": True, "upward": False}]
    upward = [{"image": "/x/UpwardClient.dylib", "weak": False, "upward": True}]
    demanded = {"_own": "bind", "_reexported": "bind", "_weak_only": "bind", "_fully_absent": "bind"}
    per_referrer = {
        "/x/StrongClient.dylib": {"_own": "bind", "_reexported": "bind", "_fully_absent": "bind"},
        "/x/WeakClient.dylib": {"_weak_only": "bind"},
    }

    # (a) a demand satisfied only through a re-export: the image's own trie does not carry it.
    split = ca.demand_split({"_own": "bind", "_reexported": "bind"}, strong,
                            {"/x/StrongClient.dylib": {"_reexported": "bind"}},
                            own={"_own"}, reexported={"_reexported"})
    check(split["satisfied_own"] == ["_own"] and split["satisfied_reexport"] == ["_reexported"],
          f"own and re-exported names are satisfied, not absent: {split}")
    check(split["fatal"] == [] and split["lazy"] == [] and split["missing"] == [],
          f"a re-export satisfies a strong demand: {split}")

    # (b) a demand only a weak referrer makes, absent everywhere: tolerated, not a failure, while
    # the strong referrer's own absent, non-lazily bound demand is still fatal.
    split = ca.demand_split(demanded, strong + weak, per_referrer, own={"_own"},
                            reexported={"_reexported"})
    check(split["tolerated"] == ["_weak_only"],
          f"a weak-only demand is tolerated: {split}")
    check(split["fatal"] == ["_fully_absent"],
          f"the strong, non-lazy absence is still fatal: {split}")
    # The rung fails only because of the strong absence: drop it and nothing fails.
    split = ca.demand_split({"_own": "bind", "_reexported": "bind", "_weak_only": "bind"},
                            strong + weak, per_referrer, own={"_own"}, reexported={"_reexported"})
    check(split["fatal"] == [] and split["lazy"] == [] and split["tolerated"] == ["_weak_only"],
          f"a re-export plus a weak-only demand is not a failure: {split}")

    # An upward referrer's demand is tolerated like a weak one (dar-m3s0).
    split = ca.demand_split({"_up": "bind"}, upward,
                            {"/x/UpwardClient.dylib": {"_up": "bind"}})
    check(split["fatal"] == [] and split["tolerated"] == ["_up"],
          f"an upward-only demand is tolerated: {split}")

    # A strong referrer's LAZY bind is deferred past load: reported, but not the load-time failure.
    split = ca.demand_split({"_lazy": "lazy_bind"}, strong,
                            {"/x/StrongClient.dylib": {"_lazy": "lazy_bind"}})
    check(split["fatal"] == [] and split["lazy"] == ["_lazy"],
          f"a strong lazy bind is not a fatal absence: {split}")

    # A strong demand absent from the image and its re-exports is the case the rung exists for.
    split = ca.demand_split({"_gone": "bind"}, strong, {"/x/StrongClient.dylib": {"_gone": "bind"}})
    check(split["fatal"] == ["_gone"] and split["missing"] == ["_gone"],
          f"a strong, non-lazy absence must stay fatal: {split}")

    # The driver carries the shared tables out of one bind-table read.
    demanded_table, per_referrer_table = ca.demand_tables(
        "/System/Library/Frameworks/Widget.framework/Versions/A/Widget", strong)
    check(demanded_table == {} and per_referrer_table == {"/x/StrongClient.dylib": {}},
          f"an unreadable referrer demands nothing rather than a wrong name: {per_referrer_table}")

    # The deployed copy is resolved the way the deploy fills the prefix: both roots.
    prefix = tmp / "prefix"
    write(prefix / "usr/lib/libDeployed.dylib", "d")
    write(prefix / "libexec/darling/System/Library/Frameworks/Widget.framework/Versions/A/Widget", "w")
    check(ca.deployed_path(str(prefix), "/usr/lib/libDeployed.dylib") is not None,
          "a prefix copy under the root is found")
    check(ca.deployed_path(str(prefix),
                           "/System/Library/Frameworks/Widget.framework/Versions/A/Widget") is not None,
          "a prefix copy under libexec/darling is found")
    check(ca.deployed_path(str(prefix), "/usr/lib/libAbsent.dylib") is None,
          "an install name the prefix does not carry has no deployed copy")
    print("PASS verifier demand: a re-export and a weak/upward referrer are tolerated, a strong "
          "absence still fails")


def test_reexport_closure(tmp):
    """dar-m3s0 follow-up: the NESTED re-export chain is walked to the leaves.

    The shape is an umbrella that re-exports a hub that re-exports the providers (Accelerate ->
    vecLib -> libBNNS/libvDSP), where the umbrella's own trie has 0 names and the hub's own trie has
    0 names too: a one-level walk from the umbrella sees zero names, which is how a correct rung
    gets reported as 12 strong demands absent.  The walk is iterative, cycle-safe and depth-bounded.
    """
    graph = {
        "/x/Umbrella": ["/x/Hub", "/x/LibA"],
        "/x/Hub": ["/x/Leaf1", "/x/Leaf2", "/x/Umbrella"],   # the back edge: a cycle
        "/x/LibA": [],
        "/x/Leaf1": [],
        "/x/Leaf2": [],
    }
    owned = {"/x/Umbrella": set(), "/x/Hub": set(), "/x/LibA": {"_A"},
             "/x/Leaf1": {"_L1", "_L2"}, "/x/Leaf2": {"_L3"}}

    def detail_of(names):
        """The detail dict the walk takes, built through the real parser so both are exercised."""
        body = "".join(f"0x0000{i:04X}  {n}\n" for i, n in enumerate(sorted(names)))
        return ca.parse_exports_trie("Exports trie:\n" + body)

    tries = {path: detail_of(names) for path, names in owned.items()}
    resolve = lambda name: name if name in graph else None            # noqa: E731
    names, providers = ca.reexport_closure("/x/Umbrella", lambda p: graph[p], resolve,
                                           lambda p: tries[p])
    check(names == {"_A", "_L1", "_L2", "_L3"}, f"the walk reaches the leaves: {sorted(names)}")
    check(set(providers) == {"/x/Hub", "/x/LibA", "/x/Leaf1", "/x/Leaf2"},
          f"every re-exported image is a provider and the cycle re-visits none: {sorted(providers)}")
    check(owned["/x/Umbrella"] == set() and owned["/x/Hub"] == set(),
          "the umbrella and the hub export nothing themselves -- the shape a one-level walk gets "
          "wrong")
    check(providers["/x/Leaf1"]["names"] == {"_L1", "_L2"},
          f"a provider keeps its own export detail: {providers['/x/Leaf1']['names']}")

    # A demanded name living ONLY in a leaf is satisfied for the client, so MISS is 0 through the
    # chain (a strong, non-lazy referrer no longer makes it fatal).
    demanded = {"_L1": "bind", "_L3": "bind"}
    split = ca.demand_split(demanded,
                            [{"image": "/x/DVTSmartSearch", "weak": False, "upward": False}],
                            {"/x/DVTSmartSearch": dict(demanded)},
                            own=owned["/x/Umbrella"], reexported=names)
    check(split["fatal"] == [] and split["missing"] == [] and len(split["satisfied_reexport"]) == 2,
          f"leaf-only demanded names are satisfied through the chain: {split}")

    # The bound is real: depth 1 stops at the umbrella's own re-exports, so the hub contributes
    # nothing and the leaf-only names are not reached.
    shallow, shallow_providers = ca.reexport_closure("/x/Umbrella", lambda p: graph[p], resolve,
                                                     lambda p: tries[p], max_depth=1)
    check(shallow == {"_A"} and set(shallow_providers) == {"/x/Hub", "/x/LibA"},
          f"max_depth bounds the walk: {sorted(shallow)} / {sorted(shallow_providers)}")
    print("PASS re-export closure: the nested chain is walked to the leaves, with a cycle and a bound")


def test_exports_trie(tmp):
    """Every line form llvm-objdump --exports-trie emits counts as a name a client can bind.

    The forms are the captured ones (this checkout: 163 deployed + 47 Xcode images, every line
    accounted for): a definition, an annotated definition ([weak_def], [absolute]), a re-exported
    ALIAS (`_memcpy` as `__platform_memmove from libsystem_platform`, which is how libsystem_c
    supplies _memcpy/_memmove/_memset/_strlen/_strcmp/_strlcpy) and a plain re-export.  Reading only
    one spelling dropped the last two forms and turned satisfied demands into misses (dar-m3s0
    follow-up: a correct rung failed).
    """
    text = """\
/pfx/usr/lib/system/libsystem_c.dylib:

Exports trie:
0x00058F70  _memset_s
0x000AB770  __ZTISt16nested_exception [weak_def]
0x00000000  _swift_runtimeSupportsNoncopyableTypes [absolute]
0x0004B310  $ld$hide$os10.5$_OBJC_CLASS_$_NSObject
[re-export] _memset_pattern16 (__platform_memset_pattern16 from libsystem_platform)
[re-export] _memcpy (__platform_memmove from libsystem_platform)
[re-export] __spawn_via_launchd (from liblaunch)
"""
    detail = ca.parse_exports_trie(text)
    check(detail["read"] is True, "the header means the trie WAS read")
    for name in ("_memset_s", "__ZTISt16nested_exception", "_swift_runtimeSupportsNoncopyableTypes",
                 "$ld$hide$os10.5$_OBJC_CLASS_$_NSObject", "_memset_pattern16", "_memcpy",
                 "__spawn_via_launchd"):
        check(name in detail["names"], f"{name} is a name this image satisfies: {sorted(detail['names'])}")
    check(detail["aliases"] == {"_memset_pattern16": ("__platform_memset_pattern16", "libsystem_platform"),
                                "_memcpy": ("__platform_memmove", "libsystem_platform")},
          f"an alias records its name and providing image: {detail['aliases']}")
    check(detail["providers"] == {"__spawn_via_launchd": "liblaunch"},
          f"a plain re-export records its providing image: {detail['providers']}")
    check(detail["annotations"] == {"__ZTISt16nested_exception": "weak_def",
                                    "_swift_runtimeSupportsNoncopyableTypes": "absolute"},
          f"the annotation tags measured in real output are recorded: {detail['annotations']}")
    check(detail["unknown"] == [],
          f"every captured form is accounted for, nothing is silently dropped: {detail['unknown']}")

    # A demanded name satisfied by the ALIAS is satisfied (with the provider nameable); a genuinely
    # absent name is still a miss, from the same non-weak referrer.
    client = [{"image": "/x/DTXConnectionServices", "weak": False, "upward": False}]
    demanded = {"_memcpy": "bind", "_not_anywhere": "bind"}
    split = ca.demand_split(demanded, client, {"/x/DTXConnectionServices": dict(demanded)},
                            own=detail["names"])
    check(split["satisfied_own"] == ["_memcpy"] and split["missing"] == ["_not_anywhere"],
          f"the alias satisfies _memcpy and only _not_anywhere is absent: {split}")
    check(split["fatal"] == ["_not_anywhere"],
          f"a genuinely absent strong demand still fails: {split}")

    # The chain carries those details through, so a caller can say WHERE a name came from.
    graph = {"/x/Umbrella": ["/x/Hub"], "/x/Hub": ["/x/libsystem_c.dylib"], "/x/libsystem_c.dylib": []}
    empty = ca.parse_exports_trie("Exports trie:\n")
    details = {"/x/Umbrella": empty, "/x/Hub": empty, "/x/libsystem_c.dylib": detail}
    names, providers = ca.reexport_closure("/x/Umbrella", lambda p: graph[p],
                                           lambda n: n if n in graph else None,
                                           lambda p: details[p])
    check("_memcpy" in names and providers["/x/libsystem_c.dylib"]["aliases"]["_memcpy"][1]
          == "libsystem_platform",
          f"a re-exported alias reaches the chain's names and keeps its provider: {sorted(names)[:4]}")

    # A trie that IS read and is empty is reported as empty (a load-name-only image); a response with
    # no trie at all is a different fact, and a line no form matches is reported, never dropped.
    check(empty["read"] is True and empty["names"] == set() and empty["unknown"] == [],
          f"a header-only trie is empty, not unread: {empty}")
    unread = ca.parse_exports_trie("/x/not-a-macho:\n")
    check(unread["read"] is False and unread["names"] == set(),
          f"no header means no trie was read: {unread}")
    odd = ca.parse_exports_trie("Exports trie:\n0x00000001  _ok\nsomething else entirely\n")
    check(odd["names"] == {"_ok"} and odd["unknown"] == ["something else entirely"],
          f"an unrecognized line is reported, not dropped: {odd}")
    print("PASS exports trie: definitions, annotations, re-exported aliases and re-exports all count")


def test_cover(tmp):
    """Symbol coverage (dar-m3s0 successor): what an import resolves to, and what stops the loader.

    The labels a demand can get are exactly: the field image's own trie (a definition OR an alias),
    its NESTED re-export graph, dyld's flat lookup for the special ordinals, an absent weak import,
    and missing.  Only an absent EAGER demand is a stop: a weak import binds to NULL, an alias is a
    satisfaction, and a flat name no dylib exports (_memcpy's documented case) is not a failure.
    """
    leaf = ca.parse_exports_trie("Exports trie:\n0x00000001  _own\n")
    hub = ca.parse_exports_trie("Exports trie:\n")
    hub2 = ca.parse_exports_trie(
        "Exports trie:\n[re-export] _aliased (__realname from libsystem_platform)\n")
    details = {"/p/leaf": leaf, "/p/hub": hub, "/p/hub2": hub2}
    detail_of = lambda p: details[p]                                     # noqa: E731
    chains = {"/p/hub": ({"_deep", "_aliased"}, {"/p/hub2": hub2})}
    chain_of = lambda p: chains.get(p, (set(), {}))                      # noqa: E731
    loaded = ["/p/leaf"]          # the images bound at or before the demanding one, in order

    def imp(field, name, weak=False, special="", eager=True):
        return {"name": name, "field": field, "special": special, "weak": weak, "eager": eager,
                "lib_ordinal": 1}

    # own trie, including an alias in it
    check(ca._cover_label(imp("Leaf", "_own"), "/p/leaf", detail_of, chain_of, loaded)
          == (ca.COVER_OWN, "/p/leaf (definition)"), "a definition is OWN")
    check(ca._cover_label(imp("Hub2", "_aliased"), "/p/hub2", detail_of, chain_of, loaded)
          == (ca.COVER_OWN, "/p/hub2 (alias)"), "an alias in the field image is OWN too")
    # the nested re-export graph, with the provider named
    label, where = ca._cover_label(imp("Hub", "_deep"), "/p/hub", detail_of, chain_of, loaded)
    check(label == ca.COVER_REEXPORT, f"a name only the re-export graph carries is REEXPORT: {label}")
    check(where in ("/p/hub2", "re-exported"), f"and it can name the provider: {where}")
    # Flat lookup: certify the resolution label and supplying image, not its prose annotation.
    label, where = ca._cover_label(imp(None, "_own", special="flat-lookup"), None,
                                  detail_of, chain_of, loaded)
    check(label == ca.COVER_FLAT and where.startswith("/p/leaf"),
          f"a flat-lookup name is supplied by the loaded leaf: {label}, {where}")
    check(ca.supplied_by_loaded("_own", loaded, detail_of, chain_of) == "/p/leaf",
          "the flat resolver identifies the exact supplying image")
    # dyld maps the whole closure before binding: a provider need not be bound yet.
    label, where = ca._cover_label(imp(None, "_own", special="flat-lookup"), None,
                                  detail_of, chain_of, ["/p/hub"], ["/p/hub", "/p/leaf"])
    check(label == ca.COVER_FLAT and where.startswith("/p/leaf"),
          f"a mapped but not-yet-bound image supplies flat demand: {label}, {where}")
    check(ca._cover_label(imp(None, "_gone", special="flat-lookup"), None,
                          detail_of, chain_of, loaded, ["/p/hub", "/p/leaf"])[0]
          == ca.COVER_MISSING, "a genuinely absent strong flat demand remains missing")
    # no field image carries it, but a LOADED one does: the flat/weak path could satisfy it
    check(ca._cover_label(imp("Hub", "_deep"), "/p/hub", detail_of, chain_of, ["/p/leaf"])[0]
          in (ca.COVER_REEXPORT, ca.COVER_LOAD_ORDER),
          "a loaded image's chain counts as satisfiable")
    # weak / lazy / eager absence
    check(ca._cover_label(imp("Leaf", "_gone", weak=True), "/p/leaf", detail_of, chain_of, loaded)[0]
          == ca.COVER_ABSENT_WEAK, "an absent WEAK import is tolerated")
    check(ca._cover_label(imp("Leaf", "_gone", eager=False), "/p/leaf", detail_of, chain_of, loaded)[0]
          == ca.COVER_ABSENT_LAZY, "an absent LAZY import is tolerated (not a stop)")
    check(ca._cover_label(imp("Leaf", "_gone"), "/p/leaf", detail_of, chain_of, loaded)[0]
          == ca.COVER_MISSING, "an absent EAGER import is MISSING")
    check(ca._cover_label(imp("Leaf", "_gone"), None, detail_of, chain_of, loaded)[0]
          == ca.COVER_IMAGE_ABSENT, "a field image that resolves nowhere is its own label")

    # The predicted sequence: an absent image and an absent eager symbol are stops, in traversal
    # order; the weak and the satisfied ones are not.
    graph = {"entries": [{"entry": "/p/entry", "path": "/p/entry"}],
             "images": {
                 "/p/entry": {"install_name": "/p/entry", "path": "/p/entry",
                              "dylibs": [("/p/early", False, False), ("/p/late", False, False)],
                              "imports": [], "labels": {}},
                 "/p/early": {"install_name": "/p/early", "path": "/p/early", "dylibs": [],
                              "imports": [], "labels": {}},
                 "/p/late": {"install_name": "/p/late", "path": "/p/late", "dylibs": [],
                             "imports": [], "labels": {}}},
             "unresolved": [{"referrer": "/p/entry", "install_name": "/p/absent",
                             "weak": False},
                            {"referrer": "/p/late", "install_name": "/p/absent-weak", "weak": True}],
             "missing": [{"image": "/p/early", "symbol": "_gone", "field": "Foundation",
                          "label": ca.COVER_MISSING, "weak": False},
                         {"image": "/p/early", "symbol": "_weakly", "field": "Foundation",
                          "label": ca.COVER_ABSENT_WEAK, "weak": True}]}
    check(ca._cover_traversal(graph) == ["/p/early", "/p/late", "/p/entry"],
          f"the traversal is dyld's binding order: dependencies first: {ca._cover_traversal(graph)}")
    events = ca.cover_events(graph)
    check([e["kind"] for e in events] == ["symbol", "image"],
          f"the weak IMAGE and the weak SYMBOL are not stops, and the deeper image binds first: {events}")
    check(events[0]["symbol"] == "_gone" and events[1]["install_name"] == "/p/absent",
          f"the eager symbol of the dependency precedes the entry's eager image: {events}")

    # The resolver reads BOTH prefix roots and says None for a name that is nowhere.
    prefix = tmp / "pfx"
    write(prefix / "usr/lib/libA.dylib", "a")
    write(prefix / "libexec/darling/usr/lib/libB.dylib", "b")
    resolve = ca.guest_resolver(str(prefix))
    check(resolve("/usr/lib/libA.dylib") is not None, "the prefix root is searched")
    check(resolve("/usr/lib/libB.dylib") is not None, "libexec/darling is searched too")
    check(resolve("/usr/lib/libNowhere.dylib") is None, "a name in neither root resolves to None")
    # A guest entry path is mapped to the host (the Xcode mount, and the host-root mount) when it
    # names a file that exists there.
    xcode = tmp / "xc"
    write(xcode / "Frameworks" / "Q.framework" / "Versions" / "A" / "Q", "q")
    mount_entry = f"/xc/{ca.GUEST_XCODE_MOUNT_MARK}/Frameworks/Q.framework/Versions/A/Q"
    check(ca._host_entry_path(mount_entry, str(xcode))
          == str(xcode / "Frameworks" / "Q.framework" / "Versions" / "A" / "Q"),
          "a path inside the Xcode mount maps under --xcode")
    host_file = write(tmp / "host" / "libX.dylib", "x")
    check(ca._host_entry_path("/Volumes/SystemRoot" + str(host_file), str(xcode)) == str(host_file),
          "a /Volumes/SystemRoot path maps to the host path it names")
    check(ca._host_entry_path("/xc/Xcode.app/Contents/Nope/Nope", str(xcode))
          == "/xc/Xcode.app/Contents/Nope/Nope",
          "a guest path that exists nowhere on the host is returned unchanged, not guessed")
    print("PASS cover: own/reexport/flat/weak labels, and only an eager absence is a stop")


def test_bind_crosscheck(tmp):
    """No tool here takes a name, a field or a kind from `llvm-objdump --macho --dyld-info`.

    Measured 2026-10-12: that command MISLABELS neighbouring GOT slots on DVTInstrumentsFoundation
    (a __LSCopyMatchingApplicationsWithItems slot attributed to a different __LSC... import entry) and
    cannot express a weak import on a chained-fixups image at all.  The decoded table is the source
    (`binds_by_dependency`), the text is read only to cross-check, and a disagreement is recorded so a
    caller can say the two readers differ instead of trusting either blindly.
    """
    decoded = {"GPURawCounter": {"_GRCCopyAllCounterSourceGroup": "weak_bind",
                                 "_GRCReleaseAllCounterSourceGroup": "weak_bind"}}
    text_rows = [("GPURawCounter", "_GRCCopyAllCounterSourceGroup", "bind"),
                 ("GPURawCounter", "_GRCCopyAllCounterSourceGroup", "bind"),
                 ("GPURawCounter", "_GRCReleaseAllCounterSourceGroup", "bind"),
                 ("GPURawCounter", "_GRCNeighbouringSlot", "bind")]
    differences = ca.compare_bind_tables(decoded, text_rows)
    check(("GPURawCounter", "_GRCCopyAllCounterSourceGroup", "weak_bind", "bind") in differences,
          f"a kind disagreement is reported (decoded weak_bind, text bind): {differences}")
    check(("GPURawCounter", "_GRCNeighbouringSlot", None, "bind") in differences,
          f"a name only the TEXT carries is reported as such, never adopted: {differences}")
    check(ca.compare_bind_tables(decoded,
                                 [("GPURawCounter", "_GRCCopyAllCounterSourceGroup", "weak_bind"),
                                  ("GPURawCounter", "_GRCReleaseAllCounterSourceGroup", "weak_bind")])
          == [], "agreement produces no rows")
    check(ca.compare_bind_tables(decoded, []) == [], "no text at all produces no rows")
    print("PASS bind cross-check: the decoded table is the source, --dyld-info is only compared")


# --------------------------------------------------------------------------- loader stop

def test_loader_stop(tmp):
    tmp.mkdir(parents=True, exist_ok=True)
    log = tmp / "run.log"
    text = (
        "Error loading required libraries. ... (dlopen(@rpath/libxcodebuildLoader.dylib, 1): "
        "Library not loaded: /System/Library/Frameworks/One.framework/Versions/A/One\n"
        "  Referenced from: /Volumes/SystemRoot/x/Xcode.app/Contents/SharedFrameworks/A.framework/Versions/A/A\n"
        "  Reason: image not found)\n"
        "more output\n"
        "Library not loaded: /System/Library/PrivateFrameworks/Two.framework/Versions/A/Two\n"
        "  Referenced from: /Volumes/SystemRoot/x/Xcode.app/Contents/SharedFrameworks/XCResultKit.framework/Versions/A/XCResultKit\n"
        "  Reason: image not found\n"
    )
    log.write_text(text)
    stops = driver.parse_loader_stop(text)
    check(len(stops) == 2, f"two stops parsed, got {len(stops)}")
    check(stops[-1][0] == "/System/Library/PrivateFrameworks/Two.framework/Versions/A/Two",
          f"the LAST stop is the current rung: {stops[-1]}")
    check(stops[-1][1].endswith("XCResultKit"), f"the referrer is parsed: {stops[-1]}")
    loose = driver.parse_loader_stop("Library not loaded: /usr/lib/Only.dylib")
    check(loose == [("/usr/lib/Only.dylib", "", "")], f"a loose line still names the install name: {loose}")
    check(driver.parse_loader_stop("VERDICT: PASS\nno dyld here\n") == [],
          "a passing run has no loader stop")
    print("PASS loader stop: strict shape, loose shape, and a run that did not stop in dyld")


def test_unresolved_stop_referrer(tmp):
    darling, build = declaration_fixture(tmp)
    xcode = tmp / "xcode"
    entry = xcode / "Frameworks/libxcodebuildLoader.dylib"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(struct.pack("<IiiIIIII", 0xFEEDFACF, 0x01000007, 3, 6, 0, 0, 0, 0))
    log = write(tmp / "run.log",
                "Library not loaded: /System/Library/PrivateFrameworks/StorageKit.framework/Versions/A/StorageKit\n"
                "  Referenced from: /Library/Developer/PrivateFrameworks/CoreDevice.framework/Versions/A/CoreDevice\n"
                "  Reason: image not found\n")
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "rung-driver.py"), "stop", "--log", str(log),
         "--darling", str(darling), "--build", str(build), "--xcode", str(xcode),
         "--prefix", str(tmp / "wrong-prefix")],
        capture_output=True, text=True)
    check(result.returncode == 2,
          f"an unresolved observed referrer must not be a successful orphan classification: {result}")
    print("PASS loader scope: unresolved observed referrer is incomplete input, not absent demand")
    incomplete_log = write(tmp / "incomplete.log", "Library not loaded: /usr/lib/Missing.dylib\n")
    incomplete_args = result.args.copy()
    incomplete_args[incomplete_args.index(str(log))] = str(incomplete_log)
    incomplete = subprocess.run(incomplete_args, capture_output=True, text=True)
    check(incomplete.returncode == 2,
          f"a truncated loader stop with unknown referrers cannot be classified as a non-blocker: {incomplete}")

    prefix = tmp / "prefix"
    referrer = prefix / "Library/Developer/PrivateFrameworks/CoreDevice.framework/Versions/A/CoreDevice"
    referrer.parent.mkdir(parents=True)
    referrer.write_bytes(entry.read_bytes())
    args = [sys.executable, "-B", str(SCRIPTS / "rung-driver.py"), "stop", "--log", str(log),
            "--darling", str(darling), "--build", str(build), "--xcode", str(xcode),
            "--prefix", str(prefix)]
    resolved = subprocess.run(args, capture_output=True, text=True)
    check(resolved.returncode == 0, f"a resolved observed referrer can be classified: {resolved}")
    zstd = shutil.which("zstd")
    if zstd is None:
        print("SKIP compressed rung-log contract: zstd is not installed")
        return
    stored = log.with_name(log.name + ".zst")
    with stored.open("wb") as output:
        subprocess.run([zstd, "-q", "-c", str(log)], stdout=output, check=True)
    log.unlink()
    compressed = subprocess.run(args, capture_output=True, text=True)
    check(compressed.returncode == 0,
          f"the same logical log must remain usable after zstd archival: {compressed}")
    stored.write_bytes(b"not a zstd frame")
    corrupt = subprocess.run(args, capture_output=True, text=True)
    check(corrupt.returncode == 2, f"unreadable archived evidence is not a classification: {corrupt}")
    print("PASS loader archival: compressed evidence works; corruption fails closed")

# --------------------------------------------------------------------------- pin sync

def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def git_out(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    for key, value in (("user.email", "contract@example.invalid"), ("user.name", "contract")):
        git("config", key, value, cwd=path)
    return path


def commit(path, message):
    git("commit", "-q", "--allow-empty", "-m", message, cwd=path)
    return git_out("rev-parse", "HEAD", cwd=path)


MANIFEST_TEMPLATE = """\
manifest:
  version: '0.13'
  remotes:
  - name: decoy-remote
    url-base: https://example.invalid
  projects:
  - name: darling
    repo-path: darling
    path: darling
    remote: decoy-remote
    # a comment inside a project block must survive
    revision: {darling}
    userdata:
      kind: darling-source
  - name: child-matching
    repo-path: child-matching
    path: darling/src/external/matching
    remote: decoy-remote
    revision: {matching}  # hand pinned
  - name: child-ahead
    repo-path: child-ahead
    path: darling/src/external/ahead
    remote: decoy-remote
    revision: {ahead}
  - name: nested
    repo-path: nested
    path: darling/src/external/nested/deeper
    remote: decoy-remote
    revision: {nested}
  west-commands:
  - file: west-commands.yml
"""


def pin_fixture(tmp):
    super_repo = init_repo(tmp / "superproject")
    old = commit(super_repo, "old")
    new = commit(super_repo, "new")
    ahead_repo = init_repo(super_repo / "src" / "external" / "ahead")
    low = commit(ahead_repo, "low")
    high = commit(ahead_repo, "high")
    # The superproject records: matching -> new, ahead -> low (the manifest pins high).
    for rel, sha in (("src/external/matching", new), ("src/external/ahead", low)):
        git("update-index", "--add", "--cacheinfo", f"160000,{sha},{rel}", cwd=super_repo)
    git("commit", "-q", "-m", "gitlinks", cwd=super_repo)
    manifest = tmp / "west.yml"
    manifest.write_text(MANIFEST_TEMPLATE.format(darling=old, matching=old, ahead=high,
                                                 nested=old))
    return super_repo, manifest, old, new, high


def run_pin(*args):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / "west-pin-sync.py"), *args],
                          capture_output=True, text=True)


def test_pin_sync(tmp):
    super_repo, manifest, old, new, high = pin_fixture(tmp)

    out = run_pin("check", "--manifest", str(manifest), "--darling", str(super_repo))
    check(out.returncode == 1, f"an out-of-sync manifest must fail the check (rc={out.returncode})")
    check("darling/src/external/matching" in out.stdout or "child-matching" in out.stdout,
          f"the drifting project is named: {out.stdout}")
    check("child-ahead" in out.stdout and "west-ahead" in out.stdout,
          "a pin ahead of the gitlink is reported as west-ahead")
    check("nested" in out.stdout and "no-gitlink" in out.stdout,
          "a project with no gitlink is reported and left alone")
    check("decoy-remote" not in out.stdout,
          "the remotes: section is not read as projects")

    before = manifest.read_text()
    out = run_pin("sync", "--manifest", str(manifest), "--darling", str(super_repo))
    check(out.returncode == 0, f"sync must succeed: {out.stdout}{out.stderr}")
    after = manifest.read_text()
    check(before != after, "sync must have written the forward pin")
    expected = before.replace(f"    revision: {old}  # hand pinned",
                              f"    revision: {new}  # hand pinned")
    check(after == expected,
          "sync must change exactly one revision value and nothing else\n"
          f"--- got ---\n{after}")
    check(high in after, "the ahead pin must NOT be lowered without --allow-downgrade")
    check("# a comment inside a project block must survive" in after,
          "comments survive the rewrite")

    out = run_pin("check", "--manifest", str(manifest), "--darling", str(super_repo))
    check(out.returncode == 0, f"after sync the forward pin is in sync (rc={out.returncode})")
    check("no change to write" in out.stdout, f"and it says so: {out.stdout}")
    check("AHEAD" in out.stdout, "the refused pin is still reported")

    # A manifest already in sync: sync writes nothing and says so.
    before = manifest.read_text()
    out = run_pin("sync", "--manifest", str(manifest), "--darling", str(super_repo))
    check(out.returncode == 0 and "no change" in out.stdout,
          f"a second sync is a no-op: {out.stdout}")
    check(manifest.read_text() == before, "a no-op sync must not rewrite the file")

    # With --allow-downgrade the ahead pin moves too, and only then.
    out = run_pin("sync", "--manifest", str(manifest), "--darling", str(super_repo),
                  "--allow-downgrade", "--project", "child-ahead")
    check(out.returncode == 0, f"--allow-downgrade must act: {out.stdout}{out.stderr}")
    check(high not in manifest.read_text(), "the lowered pin is gone")
    print("PASS pin sync: forward moves, refusals, no-ops, and a one-line rewrite")


def test_pin_selection(tmp):
    """An unmatched --project must fail loudly, and a matched one must say what it examined."""
    super_repo, manifest, old, new, high = pin_fixture(tmp / "pin-selection")
    # A selection that matches NOTHING must never report about the tree (measured 2026-10-12: a
    # mistyped --project printed an empty summary and "no change ... in sync", exit 0, while west.yml
    # kept the pre-rung revision and the doctor then failed the next bootstrap with UNDECLARED drift).
    bad = run_pin("check", "--manifest", str(manifest), "--darling", str(super_repo),
                  "--project", "child-Matching")
    check(bad.returncode == 2, f"an unmatched --project must fail (rc={bad.returncode})")
    check("no project named 'child-Matching'" in bad.stderr, f"and name it: {bad.stderr}")
    check("did you mean 'child-matching'" in bad.stderr,
          f"and suggest the near-match a case-sensitive compare would miss: {bad.stderr}")
    check("nothing was examined" in bad.stderr, f"and say nothing was looked at: {bad.stderr}")
    check("summary:" not in bad.stdout and "in sync" not in bad.stdout,
          f"it must not report about a set it never examined: {bad.stdout}")

    # ... and a real selection says WHICH projects it examined, in the table and in the RESULT -- so
    # "no change" can never be printed by a selection that matched nothing.
    run_pin("sync", "--manifest", str(manifest), "--darling", str(super_repo))
    good = run_pin("check", "--manifest", str(manifest), "--darling", str(super_repo),
                   "--project", "child-matching")
    check("examined      : 1 of" in good.stdout and "child-matching" in good.stdout,
          f"the selection is named in the table: {good.stdout}")
    check("RESULT: in sync" in good.stdout and "examined 1 of" in good.stdout,
          f"and in the RESULT, so 'no change' cannot come from an empty set: {good.stdout}")
    check("1 of 4 project(s)" in good.stdout, f"and the total it was chosen from: {good.stdout}")
    # The parser behind sync's stale-allowlist note (a fixed project still listed there hides the
    # NEXT real drift): names/paths, comments and blanks skipped, a missing file is not an error.
    root = tmp / "allow"
    root.mkdir(parents=True, exist_ok=True)
    (root / "doctor-allow-drift.txt").write_text(
        "# west darling-doctor allowlist\n\nchild-ahead   # dev branch, why\nchild-nested\n")
    _path, entries = pin.allowlist_entries(str(root))
    check(entries == ["child-ahead", "child-nested"], f"allowlist entries: {entries}")
    _path, entries = pin.allowlist_entries(str(tmp / "no-allowlist-here"))
    check(entries == [], "a missing allowlist is not an error")
    print("PASS pin selection: an unmatched --project fails loudly, a matched one says what it examined")


# --------------------------------------------------------------------------- cli

def test_cover_json_cli(tmp):
    objdump = shutil.which("llvm-objdump")
    if objdump is None or not (Path(objdump).parent / "llvm-nm").is_file():
        print("SKIP cover JSON CLI: llvm-objdump and llvm-nm are not installed together")
        return
    darling, build = declaration_fixture(tmp)
    bundle = tmp / "Xcode.app"
    contents = bundle / "Contents"
    entry = contents / "Frameworks/libxcodebuildLoader.dylib"
    entry.parent.mkdir(parents=True)
    missing = "/usr/lib/libMissing.dylib"
    name = missing.encode() + b"\0"
    command_size = (24 + len(name) + 7) & ~7
    dylib = struct.pack("<6I", 0xC, command_size, 24, 0, 0, 0)
    dylib += name + b"\0" * (command_size - 24 - len(name))
    bind = b"\x11\x40_needed\0\x90\0"
    commands = [
        struct.pack("<II16s4Q4I", 0x19, 72, b"__TEXT", 0x100000000, 0x1000,
                    0, 0x1000, 7, 5, 0, 0),
        dylib,
        struct.pack("<12I", 0x80000022, 48, 0, 0, 0x400, len(bind), 0, 0, 0, 0, 0, 0),
    ]
    data = bytearray(0x1000)
    command_bytes = b"".join(commands)
    struct.pack_into("<IiiIIIII", data, 0, 0xFEEDFACF, 0x01000007, 3, 6,
                     len(commands), len(command_bytes), 0x80, 0)
    data[32:32 + len(command_bytes)] = command_bytes
    data[0x400:0x400 + len(bind)] = bind
    entry.write_bytes(data)
    common = [
        sys.executable, "-B", str(SCRIPTS / "closure_analyze.py"), "--json",
        "--darling", str(darling), "--build", str(build), "--prefix", str(tmp / "prefix"),
        "--swift-bin", str(Path(objdump).parent),
    ]
    for selected, mode in ((bundle, ["--cover", "--order", "3"]),
                           (contents, ["--order", "3"])):
        result = subprocess.run([*common, "--xcode", str(selected), *mode],
                                capture_output=True, text=True, timeout=30)
        check(result.returncode == 1, f"the strong missing image must remain a stop: {result}")
        report = json.loads(result.stdout)
        check(report["xcode"] == str(contents), f"selected bundle must resolve to Contents: {report}")
        check(report["entries"] == [{"entry": str(entry), "path": str(entry), "from": "argument"}],
              f"the selected loader, not another Xcode, must be analyzed: {report}")
        check(report["counts"][ca.COVER_IMAGE_ABSENT] == 1,
              f"the real eager import must be counted against its missing field: {report}")
        check([(event["kind"], event.get("install_name")) for event in report["events"]]
              == [("image", missing)], f"JSON must preserve the actual predicted loader stop: {report}")
    invalid = write(tmp / "invalid-entry", "not a Mach-O image or guest run log\n")
    result = subprocess.run(
        [*common, "--xcode", str(bundle), "--cover", "--entry", str(invalid)],
        capture_output=True, text=True, timeout=30)
    check(result.returncode == 2, f"an unreadable closure entry must not be success: {result}")
    report = json.loads(result.stdout)
    check(bool(report["empty_reason"]) and all(count == 0 for count in report["counts"].values()),
          f"empty-input JSON must explain why no imports could be analyzed: {report}")
    print("PASS cover CLI: selected bundle/Contents, machine-readable stops, and incomplete input")


def test_cli(tmp):
    tmp.mkdir(parents=True, exist_ok=True)
    for script in ("closure_analyze.py", "rung-driver.py", "west-pin-sync.py"):
        out = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), "--help"],
                             capture_output=True, text=True)
        check(out.returncode == 0, f"{script} --help must work: {out.stderr}")
        check("usage:" in out.stdout, f"{script} --help must print usage")
    out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "closure_analyze.py"), "--declarations",
                          "--darling", str(tmp / "nope"), "--build", str(tmp / "nope")],
                         capture_output=True, text=True)
    check(out.returncode == 0, f"--declarations on an empty tree must still report: {out.stderr}")
    out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "rung-driver.py"), "stop",
                          "--log", str(tmp / "missing.log")], capture_output=True, text=True)
    check(out.returncode == 2, f"a missing log is refused: {out.stderr}")
    log = tmp / "pass.log"
    log.write_text("VERDICT: PASS\nXCB_RC=0\n")
    out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "rung-driver.py"), "stop",
                          "--log", str(log), "--xcode", str(tmp)], capture_output=True, text=True)
    check(out.returncode == 1, f"a run without a loader stop reports that state: {out.stdout}{out.stderr}")
    print("PASS cli: --help for each tool, and the driver's two non-errors")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="rung-tooling-"))
    try:
        test_declarations(tmp / "declarations")
        test_routes(tmp / "routes")
        test_sweep(tmp / "sweep")
        test_demand_split(tmp / "verifiedemand")
        test_reexport_closure(tmp / "reexport")
        test_exports_trie(tmp / "exports")
        test_cover(tmp / "cover")
        test_bind_crosscheck(tmp / "bindcheck")
        test_loader_stop(tmp / "stop")
        test_unresolved_stop_referrer(tmp / "unresolved-stop")
        test_pin_sync(tmp / "pin")
        test_pin_selection(tmp / "pin-selection")
        test_cover_json_cli(tmp / "cover-cli")
        test_cli(tmp / "cli")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS rung-tooling-contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())

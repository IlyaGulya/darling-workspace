#!/usr/bin/env python3
"""Contracts for the rung-loop tooling: declarations, sweep routes, the driver, and pin sync.

The rung loop was five hand steps per rung, repeated for four rungs.  These are the tools that
replaced it, and this covers the decisions they make, because each one is a decision that was made
wrong by hand at least once:

  * the declaration source is the generated MANIFEST (dar-a0ty), reconciled against the CMake text in
    both directions, so a payload-only declaration is not invisible and a CMake declaration the
    manifest does not carry is reported instead of trusted;
  * the wave sweep's route for a missing install name (weak-only, declaration, payload, source,
    substitute) and the two non-gap routes (Xcode-provided, no-referrer), each with its evidence;
  * the loader stop parse, including a log with only dyld's first line;
  * pin sync's rewrite: only a revision value changes, a forward pin moves, an ahead pin is refused
    unless told otherwise, and a manifest with nothing to change is reported as no change.

Every case is synthetic: no Xcode tree, no prefix, no build directory and no llvm tools are needed,
because a contract that needs the ladder's inputs can only run where the ladder already runs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
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
    write(darling / "src" / "frameworks" / "SourceOnly" / "src" / "SourceOnly.m", "// source only\n")
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

    # A source tree nothing builds or declares, and a name with nothing at all.
    route, why = ca.classify_route("/System/Library/Frameworks/SourceOnly.framework/Versions/A/SourceOnly",
                                   refs(False), ctx)
    check(route == ca.ROUTE_SOURCE and "src/frameworks/SourceOnly" in why,
          f"a source tree alone is a source rung: {route} ({why})")
    route, why = ca.classify_route("/System/Library/PrivateFrameworks/NothingAnywhere.framework/Versions/A/NothingAnywhere",
                                   refs(False), ctx)
    check(route == ca.ROUTE_SUBSTITUTE and "no manifest declaration" in why,
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


# --------------------------------------------------------------------------- cli

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
    check(out.returncode == 2 and "no run log" in out.stderr, f"a missing log is refused: {out.stderr}")
    log = tmp / "pass.log"
    log.write_text("VERDICT: PASS\nXCB_RC=0\n")
    out = subprocess.run([sys.executable, "-B", str(SCRIPTS / "rung-driver.py"), "stop",
                          "--log", str(log), "--xcode", str(tmp)], capture_output=True, text=True)
    check(out.returncode == 1 and "did not stop in dyld" in out.stdout,
          f"a passing run reports no stop rather than failing: {out.stdout}{out.stderr}")
    print("PASS cli: --help for each tool, and the driver's two non-errors")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="rung-tooling-"))
    try:
        test_declarations(tmp / "declarations")
        test_routes(tmp / "routes")
        test_sweep(tmp / "sweep")
        test_loader_stop(tmp / "stop")
        test_pin_sync(tmp / "pin")
        test_cli(tmp / "cli")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS rung-tooling-contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())

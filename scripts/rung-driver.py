#!/usr/bin/env python3
"""Rung driver -- the current load-time rung, and a supplied rung's verification, in one command.

The loop this replaces was five hand steps per rung, repeated for four rungs: read the loader's stop
line out of a run log, resolve which install name stopped it and who demanded it, measure the demand
per dependency field by hand, decide the route, and then verify a supplied artifact four ways.
`stop` does the first four; `verify` does the last one.

  stop    Parse the loader stop from a run log, resolve the install name and its referrer, print the
          demand per dependency field with demangled shapes and weak/strong, and print the route the
          missing name takes with the evidence behind it (`closure_analyze.py --sweep` classifies the
          whole closure the same way; this is the same classifier for the current stop).
  verify  For a supplied rung: the deployed export trie carries every demanded name, the analyzer
          reports MISS 0, and the sha chain payload == staged resource == deployed prefix copy.

Nothing here writes: it reads the log, the tree, the manifest, the payload and the prefix.
`closure_analyze.py` owns the declarations, the Mach-O readers and the route classifier; this script
is the command surface over them, so there is one implementation of each fact, not two.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import closure_analyze as ca  # noqa: E402


# --------------------------------------------------------------------------- loader stop

LOADER_STOP_RE = re.compile(
    r"Library not loaded:\s*(\S+)\s*\n\s*Referenced from:\s*(\S+)\s*\n\s*Reason:\s*([^\n)]*)")
LOADER_STOP_LOOSE_RE = re.compile(r"Library not loaded:\s*(\S+)")


def parse_loader_stop(text):
    """[(install_name, referrer, reason)] for every loader stop in a log, in log order.

    The strict shape is dyld's own: the three lines "Library not loaded:", "Referenced from:" and
    "Reason:".  A log that carries only the first line still names the install name, and that is
    reported with an empty referrer rather than dropped, because the install name is the fact the
    next rung is built from.
    """
    stops = []
    for m in LOADER_STOP_RE.finditer(text):
        stops.append((m.group(1), m.group(2), m.group(3).strip()))
    if not stops:
        for m in LOADER_STOP_LOOSE_RE.finditer(text):
            stops.append((m.group(1), "", ""))
    return stops


def guest_path_to_host(raw, xcode, prefix):
    """Where a log path lives on the host: inside the Xcode tree, or inside the prefix.

    A path the guest printed is one of three things: a host path reached through the host-root mount
    (/Volumes/SystemRoot/...), Xcode's own files under the mount, or a guest path the prefix owns
    (/System/..., /usr/...).  A path that is not a load-command path at all (the harness prints its
    own argv) comes back unchanged, and the caller says so rather than pretending.
    """
    if ca.GUEST_XCODE_MOUNT_MARK in raw:
        tail = raw.split(ca.GUEST_XCODE_MOUNT_MARK, 1)[1]
        cand = os.path.join(xcode, tail.lstrip("/"))
        return cand if os.path.exists(cand) else None
    if raw.startswith("/Volumes/SystemRoot/"):
        cand = raw[len("/Volumes/SystemRoot"):]
        return cand if os.path.exists(cand) else None
    for base in (prefix, os.path.join(prefix, "libexec/darling")):
        cand = os.path.join(base, raw.lstrip("/"))
        if os.path.exists(cand):
            return cand
    return None


def demangle(symbols):
    """{symbol: demangled shape}, or {} when the demangler is unavailable."""
    symbols = list(symbols)
    if not symbols:
        return {}
    tool = ca.swift("swift-demangle")
    if not os.path.isfile(tool):
        return {}
    out = ca.run([tool, "-compact"] + symbols)
    lines = out.splitlines()
    if len(lines) != len(symbols):
        return {}
    return dict(zip(symbols, lines))


# --------------------------------------------------------------------------- shared facts

def rung_facts(args):
    """Resolve the tree's facts once: declarations, closure, and the route context."""
    sources = ca.declaration_sources(args.darling, args.build)
    manifest = sources["manifest"]
    edges = ca.load_closure(os.path.join(args.xcode, "Frameworks/libxcodebuildLoader.dylib"),
                            args.xcode)
    ctx = ca.classify_context(args.darling, manifest, args.xcode, payload=args.payload,
                              overlay=args.overlay)
    return sources, manifest, edges, ctx


def refs_for(install_name, edges):
    return [{"image": img, "weak": weak, "upward": upward}
            for img, weak, upward in ca.referrers_for(install_name, edges)]


def print_demand(install_name, refs, indent="  "):
    """The demand per dependency field: bind kind, demangled shape, weak/strong per referrer."""
    column = ca.column_key(install_name)
    demanded = {}
    per_referrer = {}
    for ref in refs:
        syms = ca.binds_by_dependency(ref["image"]).get(column, {})
        per_referrer[ref["image"]] = syms
        for sym, kind in syms.items():
            demanded.setdefault(sym, kind)
    shapes = demangle(demanded)
    swift_shaped = {s: t for s, t in shapes.items() if t != s}
    print(f"{indent}dependency field (bind-table column): {column}")
    print(f"{indent}demanded symbols: {len(demanded)} distinct; {len(swift_shaped)} demangle to a "
          f"Swift shape ({'swift-demangle' if shapes else 'demangler UNAVAILABLE -- shapes not printed'})")
    for ref in refs:
        link = "weak" if ref["weak"] else "strong"
        print(f"{indent}referrer [{link}{', upward' if ref['upward'] else ''}] "
              f"{os.path.basename(ref['image'])}  ({len(per_referrer[ref['image']])} demanded)")
        for sym in sorted(per_referrer[ref["image"]]):
            print(f"{indent}    {sym}  ({per_referrer[ref['image']][sym]})")
            if sym in swift_shaped:
                print(f"{indent}        -> {swift_shaped[sym]}")
    return demanded


def print_declaration(sources, install_name):
    """Which declaration (if any) supplies this install name, from the manifest first."""
    manifest = sources["manifest"]
    if not manifest:
        print("  declaration  : no generated manifest under --build")
        return None, None
    by_install = manifest[1]
    entry = by_install.get(install_name)
    if entry:
        print(f"  declaration  : [manifest] target={entry['target']}")
        print(f"                 host_path={entry['host_path']}")
        for tgt, decl in sorted(sources["decls"].items()):
            if decl["install_name"] == install_name:
                print(f"                 declared as '{tgt}' (cmake_target "
                      f"{decl.get('cmake_target')}, source {decl.get('source')})")
                return entry, decl
        return entry, None
    print("  declaration  : NOT in the generated manifest (no component deploys this install name)")
    return None, None


# --------------------------------------------------------------------------- stop

def cmd_stop(args):
    if not os.path.isfile(args.log):
        print(f"rung-driver: no run log at {args.log}", file=sys.stderr)
        return 2
    text = open(args.log, encoding="utf-8", errors="replace").read()
    stops = parse_loader_stop(text)
    if not stops:
        print(f"rung-driver: no 'Library not loaded' line in {args.log} -- the run did not stop in dyld")
        print("  (that is a fact about the run, not a driver failure: a PASS run prints none)")
        return 1
    print(f"rung-driver stop -- the current load-time rung")
    print(f"  log          : {args.log}")
    print(f"  loader stops : {len(stops)} "
          f"({'using the LAST one: the run ended there' if len(stops) > 1 else 'the run ended there'})")
    install_name, referrer_raw, reason = stops[-1]
    for name, ref, _reason in stops[:-1]:
        print(f"                 earlier stop: {name} (from {os.path.basename(ref) or '?'})")
    print()
    print("== the stop ==")
    print(f"  install name : {install_name}")
    print(f"  reason       : {reason}")
    print(f"  referenced by: {referrer_raw or '(the log does not name a referrer)'}")
    referrer_host = guest_path_to_host(referrer_raw, args.xcode, args.prefix) if referrer_raw else None
    if referrer_raw and referrer_host is None:
        print("                 -- the log's referrer path resolves to nothing on this host: it is "
              "printed as-is and no demand is measured from it")
    elif referrer_host:
        print(f"  referrer host: {referrer_host}")

    sources, manifest, edges, ctx = rung_facts(args)

    refs = refs_for(install_name, edges)
    if referrer_host and os.path.isfile(referrer_host):
        observed = [r for r in refs if os.path.realpath(r["image"]) == os.path.realpath(referrer_host)]
        if not observed:
            observed = [{"image": referrer_host, "weak": False, "upward": False}]
            print("  NOTE: the log's referrer is NOT one of the closure's referrers of this install "
                  "name; measuring the demand from the log's referrer alone.")
            refs = observed
    print()
    print("== the demand ==")
    print(f"  prefix carries the install name: "
          f"{ca.deployed_state(args.prefix, install_name, None)[0]}")
    print_declaration(sources, install_name)
    print()
    demanded = print_demand(install_name, refs)

    print()
    print("== the route ==")
    route, evidence = ca.classify_route(install_name, refs, ctx)
    print(f"  {route.upper()}: {ca.ROUTE_HEADLINE[route]}")
    print(f"  evidence: {evidence}")
    print(f"  demanded symbols a strong referrer would need: "
          f"{sum(1 for r in refs for _ in [0] if not r['weak'])} referrer(s), {len(demanded)} "
          f"symbol(s) from this field")
    return 0


# --------------------------------------------------------------------------- verify

def sha256(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return None


def cmd_verify(args):
    sources = ca.declaration_sources(args.darling, args.build)
    decl = None
    if args.component:
        name, decl = ca.resolve_decl(args.component, sources["decls"])
        if not decl:
            print(f"rung-driver: no declaration resolves '{args.component}' "
                  f"(see closure_analyze.py --declarations)", file=sys.stderr)
            return 2
    install_name = args.install_name or decl["install_name"]

    print("rung-driver verify -- a supplied rung, four ways")
    print(f"  component    : {args.component or '(by install name)'}")
    print(f"  install name : {install_name}")
    print(f"  manifest     : {sources['manifest_path']}")
    print(f"  prefix       : {args.prefix}")
    print(f"  payload      : {args.payload or os.path.join(args.darling, 'src/external/swift')}")
    print()

    _sources, manifest, edges, ctx = rung_facts(args)
    refs = refs_for(install_name, edges)
    if not refs:
        print("rung-driver: no referrer in the load closure names this install name -- nowhere to "
              "measure a demand from", file=sys.stderr)
        return 2

    failures = []
    print("== demand (from the referrers' own bind tables) ==")
    demanded = print_demand(install_name, refs)
    print()
    route, evidence = ca.classify_route(install_name, refs, ctx)
    print("== route this rung took ==")
    print(f"  {route.upper()}: {ca.ROUTE_HEADLINE[route]}")
    print(f"  evidence: {evidence}")
    print()

    # 1. the deployed export trie carries every demanded name.
    print("== 1. deployed export trie carries every demanded name ==")
    deployed_path = None
    for base in (args.prefix, os.path.join(args.prefix, "libexec/darling")):
        cand = os.path.join(base, install_name.lstrip("/"))
        if os.path.isfile(cand):
            deployed_path = cand
            break
    if not deployed_path:
        print(f"  FAIL: the prefix carries no {install_name}")
        failures.append("deployed copy absent")
        exported = set()
    else:
        print(f"  deployed copy: {deployed_path}")
        exported = ca.exports(deployed_path)
        missing = sorted(s for s in demanded if s not in exported)
        print(f"  exported names: {len(exported)}; demanded: {len(demanded)}; "
              f"demanded-not-exported: {len(missing)}")
        if missing:
            failures.append(f"{len(missing)} demanded name(s) not exported")
            for sym in missing[:20]:
                print(f"    MISSING {sym}")
        else:
            print("  OK: every demanded name is in the deployed export trie")
    print()

    # 2. the analyzer reports MISS 0 for this component.
    print("== 2. analyzer MISS 0 ==")
    artifact = ca.resolve_artifact(manifest, decl) if decl else None
    result = None
    if decl:
        base = args.base or ca.ladder_base(args.darling)
        result = ca.analyze_component(args.component or install_name, decl, manifest, args.darling,
                                      base, refs)
        miss = len(result.get("missing_fatal", [])) + len(result.get("missing_lazy", []))
        print(f"  MISS (fatal+lazy) = {miss}   "
              f"[fatal {len(result.get('missing_fatal', []))}, "
              f"lazy {len(result.get('missing_lazy', []))}, "
              f"tolerated {len(result.get('missing_tolerated', []))}]")
        for sym in result.get("missing_fatal", [])[:10]:
            print(f"    MISSING (strong, non-weak bind) {sym}")
        for sym in result.get("missing_lazy", [])[:10]:
            print(f"    MISSING (strong, lazy bind)     {sym}")
        if miss:
            failures.append(f"analyzer MISS {miss}")
        else:
            print(f"  OK: MISS 0.  class {result['klass']}: {'; '.join(result['class_notes'])}")
    else:
        print("  skipped: no declaration, so the analyzer cannot classify this install name")
    print()

    # 3. the sha chain payload == staged resource == deployed prefix copy.
    print("== 3. sha chain payload == staged resource == deployed copy ==")
    staged = os.path.join(args.build, "rootless-toolchain-resources", install_name.lstrip("/"))
    payload_dir = args.payload or os.path.join(args.darling, "src/external/swift")
    payload_file = os.path.join(payload_dir, os.path.basename(install_name))
    if not os.path.isfile(payload_file):
        # A framework carried by the payload, or a component this tree builds: the payload leg is
        # the build artifact instead, and the chain says which leg is missing rather than failing.
        fw = None
        for part in install_name.split("/"):
            if part.endswith(".framework"):
                fw = part[: -len(".framework")]
                break
        if fw:
            candidate = os.path.join(payload_dir, f"{fw}.framework/Versions/A/{fw}")
            if os.path.isfile(candidate):
                payload_file = candidate
            else:
                payload_file = None
        else:
            payload_file = None
    built = artifact
    legs = [("payload", payload_file), ("staged resource", staged),
            ("deployed", deployed_path), ("analyzer artifact", built)]
    digests = {}
    for label, path in legs:
        digest = sha256(path) if path else None
        digests[label] = digest
        print(f"  {label:<17}: {digest or '(absent)'}  {path or '-'}")
    present = {label: d for label, d in digests.items() if d}
    if not present:
        print("  FAIL: none of the four legs exists")
        failures.append("no leg of the sha chain exists")
    else:
        unique = set(present.values())
        if len(unique) == 1:
            print(f"  OK: the {len(present)} present leg(s) are byte-identical")
        else:
            print("  FAIL: the present legs disagree")
            failures.append("sha chain legs differ")
        missing_legs = [label for label, d in digests.items() if not d]
        if missing_legs:
            print(f"  note: leg(s) with nothing to hash: {', '.join(missing_legs)}")
            if "deployed" in missing_legs:
                failures.append("deployed leg missing")
    print()
    print("== verdict ==")
    if failures:
        for reason in failures:
            print(f"  FAIL: {reason}")
        print("  RESULT: FAIL")
        return 1
    print("  RESULT: PASS -- every demanded name is deployed and exported, the analyzer reports "
          "MISS 0, and the sha chain is intact")
    return 0


# --------------------------------------------------------------------------- cli

def add_common(ap):
    ap.add_argument("--darling", default=ca.DEFAULTS["darling"])
    ap.add_argument("--build", default=ca.DEFAULTS["build"])
    ap.add_argument("--prefix", default=ca.DEFAULTS["prefix"])
    ap.add_argument("--xcode", default=ca.DEFAULTS["xcode"])
    ap.add_argument("--swift-bin", default=ca.DEFAULTS["swift_bin"])
    ap.add_argument("--base", default=None, help="ladder base commit (default: derived from git)")
    ap.add_argument("--payload", default=None,
                    help="payload tree (default: <darling>/src/external/swift)")
    ap.add_argument("--overlay", default=None,
                    help="overlay checkout (default: <payload>/overlays)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    stop = sub.add_parser("stop", help="the current rung from a run log",
                          description="Parse the loader stop from a run log (the LAST stop is the "
                                      "current rung), resolve the install name and the referrer, "
                                      "print the demand per dependency field with demangled shapes "
                                      "and weak/strong, and print the route with its evidence.")
    stop.add_argument("--log", required=True, help="the run log carrying the loader stop")
    add_common(stop)
    stop.set_defaults(func=cmd_stop)
    verify = sub.add_parser("verify", help="verify a supplied rung",
                            description="Verify a supplied rung three ways: the deployed export trie "
                                        "carries every demanded name, the analyzer reports MISS 0, "
                                        "and the sha chain payload == staged resource == deployed "
                                        "prefix copy.")
    verify.add_argument("--component", default=None,
                        help="declared target or install-name stem (e.g. "
                             "swift_libswiftUniformTypeIdentifiers_dylib)")
    verify.add_argument("--install-name", default=None,
                        help="verify by install name when no declaration resolves")
    add_common(verify)
    verify.set_defaults(func=cmd_verify)
    args = ap.parse_args()

    ca.SWIFT_BIN = args.swift_bin
    ca.BUILD_DIR = args.build
    for tool in ("llvm-objdump", "llvm-nm"):
        if not os.path.isfile(ca.swift(tool)):
            print(f"rung-driver: missing {ca.swift(tool)}; pass --swift-bin", file=sys.stderr)
            return 2
    if not os.path.isdir(args.xcode):
        print(f"rung-driver: no Xcode tree at {args.xcode}", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

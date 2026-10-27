#!/usr/bin/env python3
"""The MISSING census of a `closure_analyze.py --cover` run: name, field, ordinal, referrer.

`closure_analyze.py --cover` labels EVERY import of the closure; this reads that graph and prints
the one set a rung has to close, with the identity needed to fix it:

  * the MISSING set -- label `missing`, which the classifier only produces for an import that is
    neither weak nor lazy (a weak one binds to NULL, a lazy one is deferred), so a row here is a
    bind that stops dyld at load;
  * each row's dependency FIELD, its lib_ordinal, and the referrer's sha256 and whether the
    referrer is a PREFIX image or an XCODE one (the two routes a fix can take);
  * the count of the other non-exported classes, so a census can be compared before and after a
    change without a second tool.

The census adds no resolution logic: every label comes from the pure JSON cover
artifact. Raw and indexed zstd evidence use the same logical-payload reader.

Usage:
    closure_missing.py COVER_OUT [--json OUT] [--order N]
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "west_commands"))
from evidence_storage import EvidenceError, open_payload  # noqa: E402

XCODE_MARK = "/Xcode.app/"
# The classes that are NOT a resolution; printed as context, never as the census itself.
CONTEXT = ("field-image-absent", "absent-weak", "absent-lazy", "flat-lookup")


def load(path):
    try:
        with open_payload(Path(path)) as payload:
            return json.load(payload)
    except (EvidenceError, OSError, ValueError) as error:
        print(f"closure-missing: cannot read cover graph {path}: {error}", file=sys.stderr)
        raise SystemExit(2)


def sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return "<unreadable>"


def where_class(path):
    return "XCODE" if XCODE_MARK in path else ("PREFIX" if path.startswith("/") else "?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cover")
    ap.add_argument("--json", default=None, metavar="OUT")
    ap.add_argument("--order", type=int, default=0,
                    help="also print the first N predicted stops from the graph's own events")
    args = ap.parse_args()

    cover = load(args.cover)
    images = cover["images"]
    missing = []
    for inst, rec in images.items():
        for imp in rec["imports"]:
            if imp.get("label") != "missing":
                continue
            # `missing` is produced only for a strong, eager bind (see _absence_label), but the
            # row carries the flags so a reader can check that claim rather than trust it.
            missing.append({"image": inst, "path": rec["path"], "symbol": imp["name"],
                            "field": imp.get("field") or imp.get("special"),
                            "lib_ordinal": imp["lib_ordinal"], "weak": imp["weak"],
                            "eager": imp.get("eager", True)})
    for row in missing:
        row["referrer_class"] = where_class(row["path"])
        row["referrer_sha256"] = sha256(row["path"])
    missing.sort(key=lambda r: (r["symbol"], r["image"]))

    counts = cover.get("counts", {})
    print(f"cover: {args.cover}")
    print(f"entries: {', '.join(e['entry'] for e in cover['entries'])}")
    print(f"images: {len(images)}   imports: {sum(len(r['imports']) for r in images.values())}")
    print("label counts: " + json.dumps(counts, sort_keys=True))
    swift = [m for m in missing if m["symbol"].startswith(("_$s", "_$S"))]
    print()
    print("== MISSING strong eager imports (label=missing; weak/lazy binds cannot stop the loader) ==")
    print(f"   total {len(missing)}   swift-mangled {len(swift)}   non-swift {len(missing) - len(swift)}")
    for m in missing:
        print(f"   [{m['referrer_class']}] {m['image']}")
        print(f"        symbol {m['symbol']}   field={m['field']} ordinal={m['lib_ordinal']} "
              f"weak={m['weak']} eager={m['eager']}")
        print(f"        referrer sha256 {m['referrer_sha256']}")
        print(f"        referrer path   {m['path']}")
    print()
    print("== the non-exported classes that are NOT stops (context) ==")
    for name in CONTEXT:
        rows = [(inst, i["name"]) for inst, rec in images.items() for i in rec["imports"]
                if i.get("label") == name]
        print(f"   {name}: {len(rows)}")
        if name == "field-image-absent":
            # A field-image-absent bind is only inert when the referrer links that field WEAKLY or
            # the import itself is weak -- dyld then tolerates the missing image.  A bind with
            # neither is a stop of the IMAGE kind, and the unresolved-edge list owns it; this count
            # is what makes "these are not stops" checkable instead of asserted.
            strong = []
            for inst, rec in images.items():
                for imp in rec["imports"]:
                    if imp.get("label") != "field-image-absent" or imp["lib_ordinal"] < 1:
                        continue
                    dep_weak = (rec["dylibs"][imp["lib_ordinal"] - 1][1]
                                if imp["lib_ordinal"] <= len(rec["dylibs"]) else None)
                    if not imp["weak"] and not dep_weak:
                        strong.append((inst, imp["name"]))
            print(f"      of them STRONG on both sides (a stop of the image kind): {len(strong)}")
            for inst, sym in strong[:20]:
                print(f"        {inst}   {sym}")
            for inst, sym in rows[:20]:
                print(f"      {inst}   {sym}")
            if len(rows) > 20:
                print(f"      ... {len(rows) - 20} more")
    edges = [e for e in cover.get("unresolved", []) if not e["weak"]]
    print(f"   unresolved NON-weak IMAGE edges (the image kind of stop): {len(edges)}")
    for e in edges[:20]:
        print(f"      {e['referrer']} -> {e['install_name']}")

    if args.order:
        events = cover.get("events", [])
        print()
        print(f"== first {args.order} predicted stops (the graph's own model) ==")
        for e in events[:args.order]:
            print("   " + json.dumps(e, sort_keys=True))

    if args.json:
        json.dump({"cover": args.cover, "counts": counts, "missing": missing},
                  open(args.json, "w"), indent=1, sort_keys=True)
        print(f"\nwrote {args.json}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

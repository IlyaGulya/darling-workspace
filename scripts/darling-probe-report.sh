#!/usr/bin/env python3
"""Report a Darling run log as a PROBE BISECTION instead of a wall of lines.

WHY THIS EXISTS. Reading these logs by hand went wrong in three different ways in one session:
`grep -o` with a pattern that did not match the tag's real shape reported "no probes" when there were many;
a histogram of tags across several guest processes said nothing about WHICH process died, which is the only
question a bisection asks; and the last line of the log is not the last event of the run, because several threads
write to the same descriptor and the death of one is silent while the others continue.

So this groups probe lines by the identity the probes print (see docs/tooling.md rule 10: the identity must be
free, portable, and the same kind in every component), and then answers the three questions directly:

  * which stacks are UNBALANCED -- a probe that was entered and whose following probe never appeared is the
    point of death, and it is invisible in a plain count;
  * which events are LAST in the log, in order;
  * which markers and counters the run itself reported.

Usage:
  darling-probe-report.sh LOG [--balance PREV NEXT]... [--tail N]
  darling-probe-report.sh --latest
"""

import argparse
import collections
import glob
import os
import re
import sys

# tag -> the probe that must follow it if control returned; the pair is what makes a death visible
# Pairs are only meaningful WITHIN one frame: the identity is the address of a probe's local buffer, so a
# nested call has a different one. Cross-function pairs (sys_open -> sys_openat_nocancel) are therefore NOT
# checked here; they are correlated by running the same identity form in both components instead.
# pc-entry -> pc-threads-ok is deliberately absent: the !uses_threads() arm legally returns after pc-nothreads.
DEFAULT_PAIRS = [
    ("open-entry", "open-postcancel"),
    ("open-postcancel", "open-postwd"),
    ("pc-threads-ok", "pc-postplane-"),
    ("console-rpc-begin", "console-rpc-"),
]
# Two shapes exist and both must parse: "[tag sp=hex]" (dylib probes) and "[tag] sp=hex]" (a probe whose
# macro already closed the bracket). Reading only the first made every launchd tag look unattributed.
TAG_RE = re.compile(r"\[([A-Za-z0-9_-]+)\](?:\s+sp=([0-9a-f]+))?|\[([A-Za-z0-9_-]+)\s+sp=([0-9a-f]+)\]")


def parse(path):
    lines = open(path, errors="replace").read().splitlines()
    per = collections.defaultdict(collections.Counter)
    events = []
    untagged = []
    for i, line in enumerate(lines):
        found = False
        for m in TAG_RE.finditer(line):
            tag = m.group(1) or m.group(3)
            sp = (m.group(2) or m.group(4)) or "<no-identity>"
            per[sp][tag] += 1
            events.append((i, sp, tag))
            found = True
        if not found:
            untagged.append((i, line.strip()))
    return lines, per, events, untagged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--tail", type=int, default=10)
    ap.add_argument("--balance", action="append", default=None,
                    help="PREV:NEXT pair to check for balance (repeatable)")
    args = ap.parse_args()

    path = args.log
    if args.latest or not path:
        logs = sorted(glob.glob("/tmp/darling-boot-*.log") + glob.glob("/tmp/darling-trace-*.log"),
                      key=os.path.getmtime)
        if not logs:
            print("no run logs found", file=sys.stderr)
            return 2
        path = logs[-1]
    print("log: %s" % path)

    lines, per, events, untagged = parse(path)
    print("lines: %d   probes: %d   distinct identities: %d" % (len(lines), len(events), len(per)))

    print("\n=== per identity (the unit a bisection needs) ===")
    for sp, c in sorted(per.items()):
        print("  %-14s %s" % (sp, dict(c)))

    pairs = DEFAULT_PAIRS
    if args.balance:
        pairs = [tuple(p.split(":", 1)) for p in args.balance]
    print("\n=== balance (entered but the following probe never appeared = the point of death) ===")
    any_unbalanced = False
    for sp, c in sorted(per.items()):
        for prev, nxt in pairs:
            # PREFIX match: a tag carries its own digit suffix (`pc-postplane-7`), so an exact-string lookup
            # reports every one of them as missing and invents unbalanced stacks that are not there.
            got_prev = sum(v for k, v in c.items() if k == prev or k.startswith(prev))
            got_next = sum(v for k, v in c.items() if k == nxt or k.startswith(nxt))
            if got_prev and got_prev > got_next:
                print("  UNBALANCED %s: %s=%d -> %s*=%d" % (sp, prev, got_prev, nxt, got_next))
                any_unbalanced = True
    if not any_unbalanced:
        print("  (no unbalanced stack for the checked pairs)")

    print("\n=== last %d events, in log order ===" % args.tail)
    for i, sp, tag in events[-args.tail:]:
        print("  line %-6d %-14s %s" % (i, sp, tag))

    if untagged:
        print("\n=== the last %d non-probe lines (the run's own reporting) ===" % args.tail)
        for i, line in untagged[-args.tail:]:
            print("  line %-6d %s" % (i, line[:120]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

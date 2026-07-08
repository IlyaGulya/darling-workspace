#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path


CONFIGS = [
    (
        "5.18/perl/config.h",
        "5.18/perl/lib/Config_heavy.pl",
    ),
    (
        "5.28/perl/config.h",
        "5.28/perl/lib/Config_heavy.pl",
    ),
    (
        "DSTROOT/System/Library/Perl/5.18/darwin-thread-multi-2level/CORE/config.h",
        "DSTROOT/System/Library/Perl/5.18/darwin-thread-multi-2level/Config_heavy.pl",
    ),
    (
        "DSTROOT/System/Library/Perl/5.28/darwin-thread-multi-2level/CORE/config.h",
        "DSTROOT/System/Library/Perl/5.28/darwin-thread-multi-2level/Config_heavy.pl",
    ),
]


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


root = Path(os.environ["PERL_SRC_ROOT"])
bad = []

for config_rel, heavy_rel in CONFIGS:
    config = root / config_rel
    heavy = root / heavy_rel
    if not config.is_file():
        fail(f"missing config.h: {config}")
    if not heavy.is_file():
        fail(f"missing Config_heavy.pl: {heavy}")

    config_text = config.read_text(errors="replace")
    heavy_text = heavy.read_text(errors="replace")

    if re.search(r"^\s*#\s*define\s+USE_NSGETEXECUTABLEPATH\b", config_text, re.M):
        bad.append(f"{config_rel}: USE_NSGETEXECUTABLEPATH is still defined")

    match = re.search(r"^usensgetexecutablepath='([^']+)'$", heavy_text, re.M)
    if not match:
        bad.append(f"{heavy_rel}: missing usensgetexecutablepath entry")
    elif match.group(1) != "undef":
        bad.append(
            f"{heavy_rel}: usensgetexecutablepath is {match.group(1)!r}, expected 'undef'"
        )

if bad:
    for item in bad:
        print(f"FAIL: {item}", file=sys.stderr)
    raise SystemExit(1)

print("GREEN: Perl configs consistently avoid unavailable _NSGetExecutablePath")

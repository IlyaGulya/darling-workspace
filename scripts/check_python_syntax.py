#!/usr/bin/env python3
"""Parse Python files without producing ``__pycache__`` artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", metavar="PATH", nargs="+", type=Path)
    args = parser.parse_args()

    for path in args.paths:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        print(f"syntax OK {path}")


if __name__ == "__main__":
    main()

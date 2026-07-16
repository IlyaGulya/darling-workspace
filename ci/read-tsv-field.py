#!/usr/bin/env python3
"""Read one field from the strict field/value TSV evidence format."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn


def fail(message: str) -> NoReturn:
    print(f"read-tsv-field: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    if len(sys.argv) != 3:
        fail("usage: read-tsv-field.py TSV_PATH FIELD")
    path = Path(sys.argv[1])
    field = sys.argv[2]
    if not field:
        fail("field must not be empty")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        fail(f"cannot read {path}: {error}")
    if not lines or lines[0] != "field\tvalue":
        fail(f"{path} must start with the field/value header")

    matches: list[str] = []
    for line_number, line in enumerate(lines[1:], start=2):
        columns = line.split("\t")
        if len(columns) != 2 or not columns[0] or not columns[1]:
            fail(f"{path}:{line_number} is not a non-empty field/value row")
        if columns[0] == field:
            matches.append(columns[1])
    if len(matches) != 1:
        fail(f"{path} must contain exactly one {field!r} field")
    print(matches[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

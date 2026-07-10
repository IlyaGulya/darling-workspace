#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

printf 'value = 1\n' >"$tmp/valid.py"
python3 -B "$repo/scripts/check_python_syntax.py" "$tmp/valid.py" \
	>"$tmp/valid.out"
grep -F -x -q "syntax OK $tmp/valid.py" "$tmp/valid.out"

printf 'def broken(:\n' >"$tmp/invalid.py"
if python3 -B "$repo/scripts/check_python_syntax.py" "$tmp/invalid.py" \
	>"$tmp/invalid.out" 2>&1; then
	cat "$tmp/invalid.out" >&2
	exit 1
fi
grep -F -q 'SyntaxError' "$tmp/invalid.out"

if find "$tmp" -type d -name __pycache__ -print | grep -q .; then
	find "$tmp" -type d -name __pycache__ -print >&2
	exit 1
fi

printf 'PASS check-python-syntax-contract\n'

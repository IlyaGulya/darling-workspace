#!/usr/bin/env bash
set -euo pipefail

src="${DARLING_SRC_ROOT:?set DARLING_SRC_ROOT}"
test -f "$src/CMakeLists.txt"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/build"

# The production gate intentionally runs only in a Darling workspace layout:
# <workspace>/darling next to <workspace>/darling-workspace/patches/homebrew.
# `west test --materialize-profile` creates just the source worktree, so provide
# the minimal sibling marker needed to exercise the real gate path.
workspace_marker="$src/../darling-workspace/patches/homebrew/patches.yml"
if [ ! -e "$workspace_marker" ]; then
	mkdir -p "$(dirname "$workspace_marker")"
	printf 'patches: []\n' > "$workspace_marker"
fi

cat > "$tmp/bin/west" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$tmp/west.log"
printf '%s\n' "fake drift from test west" >&2
exit 42
EOF
chmod +x "$tmp/bin/west"

set +e
PATH="$tmp/bin:$PATH" cmake -S "$src" -B "$tmp/build" -DDARLING_SKIP_DRIFT_GATE=OFF \
	>"$tmp/cmake.out" 2>&1
rc=$?
set -e

if [ "$rc" -eq 0 ]; then
	echo "FAIL: CMake configure succeeded despite fake drift" >&2
	exit 1
fi
if ! grep -Fxq 'patch status --strict' "$tmp/west.log" 2>/dev/null; then
	echo "FAIL: drift gate did not invoke west patch status --strict" >&2
	tail -80 "$tmp/cmake.out" >&2
	exit 1
fi
if ! grep -q 'patchset DRIFT' "$tmp/cmake.out"; then
	echo "FAIL: configure failed without the drift-gate diagnostic" >&2
	tail -80 "$tmp/cmake.out" >&2
	exit 1
fi
if ! grep -q 'fake drift from test west' "$tmp/cmake.out"; then
	echo "FAIL: drift-gate diagnostic did not include west failure output" >&2
	tail -80 "$tmp/cmake.out" >&2
	exit 1
fi

echo "GREEN: CMake configure fails through the patchset drift gate"

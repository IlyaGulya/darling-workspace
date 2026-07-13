#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"

cat >"$tmp/bin/west" <<'WEST'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
	list)
		printf 'manifest\tworkspace\n'
		printf 'one\tone\n'
		printf 'two\ttwo\n'
		printf 'three\ttwo/three\n'
		;;
	update)
		printf 'update %s\n' "${2:-all}" >>"$WEST_LOG"
		sleep 0.01
		if [[ "${2:-}" == bad ]]; then
			printf 'fatal: bad project\n'
			exit 1
		fi
		;;
	*)
		echo "unexpected west command: $*" >&2
		exit 2
		;;
esac
WEST
chmod +x "$tmp/bin/west"

export PATH="$tmp/bin:$PATH"
export WEST_LOG="$tmp/west.log"
DARLING_WEST_UPDATE_JOBS=2 "$repo/ci/west-update-parallel.sh"
for project in one two three; do
	grep -F -x -q "update $project" "$WEST_LOG"
done
parent_line="$(grep -n -F 'update two' "$WEST_LOG" | cut -d: -f1)"
child_line="$(grep -n -F 'update three' "$WEST_LOG" | cut -d: -f1)"
((child_line > parent_line))

cat >"$tmp/bin/west" <<'WEST'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
	list)
		printf 'good\tgood\n'
		printf 'bad\tbad\n'
		;;
	update)
		printf 'update %s\n' "${2:-all}" >>"$WEST_LOG"
		if [[ "${2:-}" == bad ]]; then
			printf 'fatal: bad project\n'
			exit 1
		fi
		;;
	*)
		exit 2
		;;
esac
WEST
chmod +x "$tmp/bin/west"
: >"$WEST_LOG"
if DARLING_WEST_UPDATE_JOBS=2 "$repo/ci/west-update-parallel.sh" \
	2>"$tmp/failure.log"; then
	echo 'parallel west update unexpectedly succeeded' >&2
	exit 1
fi
grep -F -q 'west update failed for:' "$tmp/failure.log"
grep -F -q 'fatal: bad project' "$tmp/failure.log"
grep -F -x -q 'update good' "$WEST_LOG"
grep -F -x -q 'update bad' "$WEST_LOG"

printf 'PASS west-update-parallel-contract\n'

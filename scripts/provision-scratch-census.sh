#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
tools="$repo/.darling-tools"
destination="$tools/darling-scratch-census"
lock="$tools/.provision.lock"
stage="$tools/.scratch-census-stage"
staged="$tools/.darling-scratch-census.new"

for command in cargo python3 flock install stat sync mv; do
	command -v "$command" >/dev/null 2>&1 || {
		echo "setup-scratch-census requires optional prerequisite: $command" >&2
		exit 127
	}
done

cleanup() {
	local rc=$?
	if [[ -d "$stage" && ! -L "$stage" ]]; then rm -rf -- "$stage"; fi
	if [[ -f "$staged" && ! -L "$staged" ]]; then rm -f -- "$staged"; fi
	exit "$rc"
}
trap cleanup EXIT HUP INT TERM

validate_directory() {
	local path=$1 owner mode
	[[ -d "$path" && ! -L "$path" ]] || return 1
	owner="$(stat -c '%u' -- "$path")"
	mode="$(stat -c '%a' -- "$path")"
	[[ "$owner" == "$(id -u)" && $((8#$mode & 0022)) -eq 0 ]]
}

validate_executable() {
	local path=$1 owner mode links
	[[ -f "$path" && ! -L "$path" && -x "$path" ]] || return 1
	owner="$(stat -c '%u' -- "$path")"
	mode="$(stat -c '%a' -- "$path")"
	links="$(stat -c '%h' -- "$path")"
	[[ "$owner" == "$(id -u)" && "$links" == 1 && $((8#$mode & 0022)) -eq 0 ]]
}

if [[ ! -e "$tools" ]]; then mkdir -m 0700 -- "$tools"; fi
validate_directory "$tools" || { echo "unsafe scratch census tool directory" >&2; exit 1; }
if [[ ! -e "$lock" ]]; then (umask 077; : >"$lock"); fi
[[ -f "$lock" && ! -L "$lock" ]] || { echo "unsafe scratch census provisioning lock" >&2; exit 1; }
exec 9<>"$lock"
[[ "$(stat -c '%u:%a:%h' -- "$lock")" == "$(id -u):600:1" ]] || {
	echo "hostile scratch census provisioning lock metadata" >&2; exit 1;
}
flock -w 30 9 || { echo "scratch census provisioning is busy" >&2; exit 1; }

# Only these fixed private names are recoverable after a hard parent death.
for stale in "$stage" "$staged"; do
	if [[ -e "$stale" || -L "$stale" ]]; then
		if [[ "$stale" == "$stage" && -d "$stale" && ! -L "$stale" && "$(stat -c '%u' -- "$stale")" == "$(id -u)" ]]; then
			rm -rf -- "$stale"
		elif [[ "$stale" == "$staged" && -f "$stale" && ! -L "$stale" && "$(stat -c '%u:%h' -- "$stale")" == "$(id -u):1" ]]; then
			rm -f -- "$stale"
		else
			echo "hostile scratch census stale stage: $stale" >&2; exit 1
		fi
	fi
done
if [[ -e "$destination" || -L "$destination" ]]; then
	validate_executable "$destination" || {
		echo "refusing to replace hostile scratch census helper: $destination" >&2; exit 1;
	}
fi

mkdir -m 0700 -- "$stage"
mkdir -m 0700 -- "$stage/tmp"
TMPDIR="$stage/tmp" python3 -B "$repo/scripts/run-cargo-with-parent-death.py" \
	cargo build --quiet --locked --release \
	--manifest-path "$repo/lifecycle/operation-boundary/Cargo.toml" \
	--target-dir "$stage/target" --bin darling-scratch-census
built="$stage/target/release/darling-scratch-census"
[[ -f "$built" && ! -L "$built" && -x "$built" && "$(stat -c '%u' -- "$built")" == "$(id -u)" ]] || {
	echo "Cargo did not produce a safe scratch census helper" >&2; exit 1;
}
install -m 0755 -- "$built" "$staged"
validate_executable "$staged" || { echo "staged scratch census helper failed validation" >&2; exit 1; }
sync -f "$staged"
mv -fT -- "$staged" "$destination"
sync -f "$tools"
validate_executable "$destination" || { echo "published scratch census helper failed validation" >&2; exit 1; }
printf 'provisioned %s\n' "$destination"

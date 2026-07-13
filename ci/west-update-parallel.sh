#!/usr/bin/env bash
set -euo pipefail

jobs="${DARLING_WEST_UPDATE_JOBS:-1}"
case "$jobs" in
	''|*[!0-9]*)
		echo "DARLING_WEST_UPDATE_JOBS must be a positive integer" >&2
		exit 2
		;;
esac
if ((jobs < 1)); then
	echo "DARLING_WEST_UPDATE_JOBS must be a positive integer" >&2
	exit 2
fi

if [[ "${1:-}" == '--worker' ]]; then
	project="${2:?worker requires a project name}"
	safe_project="$(printf '%s' "$project" | tr -c 'A-Za-z0-9_.-' '_')"
	log_file="$DARLING_WEST_UPDATE_LOG_DIR/$safe_project.log"
	if west update "$project" >"$log_file" 2>&1; then
		echo "west update $project: ok"
		exit 0
	fi
	printf '%s\n' "$project" >"$DARLING_WEST_UPDATE_STATUS_DIR/$safe_project.failed"
	echo "west update $project: failed (see bootstrap summary)" >&2
	exit 1
fi

if ((jobs == 1)); then
	exec west update
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
worker="${script_dir}/$(basename "${BASH_SOURCE[0]}")"
state_dir="$(mktemp -d "${TMPDIR:-/tmp}/darling-west-update.XXXXXX")"
trap 'rm -rf "$state_dir"' EXIT
mkdir -p "$state_dir/logs" "$state_dir/status"

project_list="$state_dir/projects.tsv"
west list -f '{name}|{path}' |
	awk -F '|' '$1 != "manifest"' >"$project_list"
if [[ ! -s "$project_list" ]]; then
	echo 'west manifest has no active projects' >&2
	exit 2
fi

export DARLING_WEST_UPDATE_LOG_DIR="$state_dir/logs"
export DARLING_WEST_UPDATE_STATUS_DIR="$state_dir/status"

update_status=0
run_batch() {
	local depth="$1"
	mapfile -t projects < <(
		awk -F '|' -v wanted_depth="$depth" '
			{
				project_depth = split($2, components, "/")
				if (project_depth == wanted_depth) print $1
			}' "$project_list"
	)
	if ((${#projects[@]} == 0)); then
		return 0
	fi

	echo "west update: depth $depth (${#projects[@]} projects)"
	set +e
	printf '%s\0' "${projects[@]}" |
		xargs -0 -n 1 -P "$jobs" "$worker" --worker
	local update_status=$?
	set -e
	return "$update_status"
}

depths="$(awk -F '|' '{print split($2, components, "/")}' \
	"$project_list" | sort -nu)"
for depth in $depths; do
	if ! run_batch "$depth"; then
		update_status=1
		break
	fi
done

if ((update_status == 0)); then
	exit 0
fi

echo 'west update failed for:' >&2
for status_file in "$state_dir"/status/*.failed; do
	[[ -e "$status_file" ]] || continue
	project="$(<"$status_file")"
	safe_project="${status_file##*/}"
	safe_project="${safe_project%.failed}"
	echo "--- $project" >&2
	cat "$state_dir/logs/$safe_project.log" >&2
done
exit 1

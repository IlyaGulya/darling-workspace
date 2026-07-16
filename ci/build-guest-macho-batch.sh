#!/usr/bin/env bash
set -euo pipefail

usage() {
	echo "usage: $0 OUTPUT_DIR VARIANT" >&2
	echo "VARIANT must be a or b; OUTPUT_DIR must be outside the repository" >&2
	exit 2
}

(( $# == 2 )) || usage

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output="$(realpath -m -- "$1")"
variant="$2"
case "$variant" in
	a|b) ;;
	*) usage ;;
esac
case "$output" in
	"$root"|"$root"/*)
		echo "batch output must be outside the repository: $output" >&2
		exit 2
		;;
esac
if [[ -e "$output" ]]; then
	[[ -d "$output" ]] || {
		echo "batch output is not a directory: $output" >&2
		exit 2
	}
	if [[ -n "$(find "$output" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
		echo "batch output must be empty: $output" >&2
		exit 2
	fi
else
	mkdir -p -- "$output"
fi

cd "$root"
work="$output/.work"
export ROOTLESS_TIER_REPO="$root"
export TMPDIR="$work"
export DARLING_CLT_CACHE="$work/clt-cache"
: "${RUNNER_TEMP:?batch requires the official RUNNER_TEMP directory}"
. "$root/ci/rootless-prefix.sh"
. "$root/testkit/scripts/darling-guest-shell.sh"

batch_phase=startup
active_prefix=""
declare -a fixture_names=()
declare -a fixture_projects=()
declare -a fixture_paths=()
declare -a fixture_source_hashes=()
declare -a fixture_compile_json=()
declare -a fixture_link_json=()
declare -a fixture_markers=()
declare -a fixture_profiles=()
declare -a fixture_patch_paths=()
declare -a fixture_patch_hashes=()
declare -a fixture_source_files=()
declare -a fixture_source_revisions=()

write_batch_state() {
	local phase="$1"
	local status="$2"
	local temporary
	temporary="$(mktemp "$output/.batch-state.XXXXXX")"
	{
		printf 'field\tvalue\n'
		printf 'schema\t1\n'
		printf 'variant\t%s\n' "$variant"
		printf 'fixture-count\t14\n'
		printf 'phase\t%s\n' "$phase"
		printf 'status\t%s\n' "$status"
	} >"$temporary"
	chmod 0644 "$temporary"
	mv -f -- "$temporary" "$output/batch-state.tsv"
}

set_batch_state() {
	batch_phase="$1"
	write_batch_state "$1" "$2"
}

startup_failure() {
	local rc="$?"
	set +e
	write_batch_state "${batch_phase:-source-validation}" FAILED
	{
		printf 'status: failure\n'
		printf 'variant: %s\n' "$variant"
		printf 'fixture-count: %s\n' "${#fixture_names[@]}"
		printf 'exit-code: %s\n' "$rc"
		printf 'phase: %s\n' "${batch_phase:-source-validation}"
	} >"$output/failure-summary.txt"
	exit "$rc"
}

write_batch_state startup RUNNING
set_batch_state source-validation RUNNING
trap startup_failure EXIT INT TERM

source_rows="$(PYTHONDONTWRITEBYTECODE=1 python3 -B \
	"$root/ci/guest_macho_batch_specs.py" --emit-tsv)"

west_project_root() {
	local project="$1"
	local project_root
	if [[ "$project" == "darling-workspace" ]]; then
		project_root="$(realpath -e -- "$(west topdir)/$(west config manifest.path)")"
		[[ "$project_root" == "$root" ]] || {
			echo "manifest project root differs from checked-out workspace: $project_root" >&2
			return 1
		}
	else
		project_root="$(west list "$project" -f '{abspath}')"
		project_root="$(realpath -e -- "$project_root")"
	fi
	printf '%s\n' "$project_root"
}

west_project_revision() {
	local project="$1"
	local project_root
	project_root="$(west_project_root "$project")"
	git -C "$project_root" rev-parse HEAD
}

while IFS=$'\t' read -r name source_project source_path source_sha256 compile_json link_json marker profile patch_path patch_sha256; do
	[[ "$name" == "name" ]] && continue
	[[ -n "$name" && -n "$source_project" && -n "$source_path" && -n "$source_sha256" && -n "$compile_json" && -n "$link_json" && -n "$marker" && -n "$profile" && -n "$patch_path" && -n "$patch_sha256" ]] || {
		echo "malformed guest Mach-O batch spec" >&2
		exit 1
	}
	project_root="$(west_project_root "$source_project")" || exit 1
	case "$source_path" in
		/*|*..*) echo "unsafe source path for $name: $source_path" >&2; exit 1 ;;
	esac
	source_file="$(realpath -e -- "$project_root/$source_path")" || {
		echo "fixture source is missing: $source_project/$source_path" >&2
		exit 1
	}
	case "$source_file" in
		"$project_root"/*) ;;
		*) echo "source symlink escapes West project root: $name" >&2; exit 1 ;;
	esac
	actual_source_sha256="$(sha256sum -- "$source_file" | cut -d' ' -f1)"
	[[ "$actual_source_sha256" == "$source_sha256" ]] || {
		echo "source SHA-256 mismatch for $name: expected $source_sha256, got $actual_source_sha256" >&2
		exit 1
	}
	case "$patch_path" in
		/*|*..*) echo "unsafe owning patch path for $name: $patch_path" >&2; exit 1 ;;
	esac
	patch_file="$(realpath -e -- "$root/$patch_path")" || {
		echo "owning patch is missing for $name: $patch_path" >&2
		exit 1
	}
	case "$patch_file" in
		"$root"/*) ;;
		*) echo "owning patch symlink escapes workspace: $name" >&2; exit 1 ;;
	esac
	actual_patch_sha256="$(sha256sum -- "$patch_file" | cut -d' ' -f1)"
	[[ "$actual_patch_sha256" == "$patch_sha256" ]] || {
		echo "patch SHA-256 mismatch for $name: expected $patch_sha256, got $actual_patch_sha256" >&2
		exit 1
	}
	fixture_names+=("$name")
	fixture_projects+=("$source_project")
	fixture_paths+=("$source_path")
	fixture_source_hashes+=("$source_sha256")
	fixture_compile_json+=("$compile_json")
	fixture_link_json+=("$link_json")
	fixture_markers+=("$marker")
	fixture_profiles+=("$profile")
	fixture_patch_paths+=("$patch_path")
	fixture_patch_hashes+=("$patch_sha256")
	fixture_source_files+=("$source_file")
	fixture_source_revisions+=("$(west_project_revision "$source_project")")
done <<<"$source_rows"
(( ${#fixture_names[@]} == 14 )) || {
	echo "expected exactly 14 guest Mach-O specs" >&2
	exit 1
}

cleanup_prefix() {
	local original_rc="$1"
	local cleanup_rc=0
	local gc_rc=0
	local jobs_rc=0
	local remove_rc=0
	set +e
	if [[ -n "$active_prefix" && -d "$active_prefix" ]]; then
		DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
			west test --prefix "$active_prefix" --cleanup-prefix
		cleanup_rc=$?
	fi
	west test --gc --gc-runtime-evidence
	gc_rc=$?
	"$root/scripts/west-job.sh" assert-no-live-west-test --state-root "$work"
	jobs_rc=$?
	if [[ -n "$active_prefix" ]] && (( cleanup_rc == 0 && gc_rc == 0 && jobs_rc == 0 )); then
		rootless_prefix_remove corpus "$active_prefix"
		remove_rc=$?
	fi
	if (( cleanup_rc != 0 || gc_rc != 0 || jobs_rc != 0 || remove_rc != 0 )); then
		echo "batch cleanup failed; preserving work diagnostics: $work" >&2
	else
		active_prefix=""
		rm -rf -- "$work"
	fi
	if (( original_rc != 0 )); then
		return "$original_rc"
	fi
	return $((cleanup_rc || gc_rc || jobs_rc || remove_rc))
}

write_failure_summary() {
	local rc="$1"
	local status=success
	(( rc == 0 )) || status=failure
	{
		printf 'status: %s\n' "$status"
		printf 'variant: %s\n' "$variant"
		printf 'fixture-count: %s\n' "${#fixture_names[@]}"
		printf 'exit-code: %s\n' "$rc"
		printf 'phase: %s\n' "${batch_phase:-startup}"
		if [[ -n "${active_prefix:-}" ]]; then
			printf 'prefix: %s\n' "$active_prefix"
		fi
	} >"$output/failure-summary.txt"
}

on_exit() {
	local rc="$?"
	local cleanup_rc
	set +e
	cleanup_prefix "$rc"
	cleanup_rc=$?
	if (( rc == 0 && cleanup_rc != 0 )); then
		rc="$cleanup_rc"
	fi
	if (( rc == 0 )); then
		write_batch_state complete COMPLETE
	else
		write_batch_state "${batch_phase:-startup}" FAILED
	fi
	write_failure_summary "$rc"
	exit "$rc"
}
trap on_exit EXIT INT TERM

ccache_env="$work/ccache.env"
ccache_output="$work/ccache.output"
ccache_runner_temp="$work/ccache-runner-temp"
mkdir -p -- "$work" "$ccache_runner_temp"
: >"$ccache_env"
: >"$ccache_output"
GITHUB_ENV="$ccache_env" \
GITHUB_OUTPUT="$ccache_output" \
GITHUB_SHA="local-macho-corpus-$variant" \
RUNNER_ARCH=X64 \
RUNNER_OS=Linux \
RUNNER_TEMP="$ccache_runner_temp" \
	"$root/ci/guest-toolchain-ccache.sh" prepare cold
set -a
. "$ccache_env"
set +a

set_batch_state prefix-create RUNNING
prefix="$(rootless_prefix_create corpus CORPUS_PREFIX)"
active_prefix="$prefix"
printf '%s\n' "$prefix" >"$output/.prefix-path"
for socket_path in \
	"$prefix/var/run/shellspawn.sock" \
	"$prefix/.darlingserver.sock"; do
	socket_path_bytes="$(printf '%s' "$socket_path" | LC_ALL=C wc -c)"
	if (( socket_path_bytes > 107 )); then
		echo "batch socket path exceeds AF_UNIX budget: $socket_path ($socket_path_bytes bytes)" >&2
		exit 1
	fi
done

stage="$prefix/private/var/tmp"
batch_stage="$stage/guest-macho-batch"
mkdir -p -- "$batch_stage"
for index in "${!fixture_names[@]}"; do
	name="${fixture_names[$index]}"
	if [[ "$name" == "select_fdset_guest" ]]; then
		guest_source="/private/var/tmp/select_fdset_guest.c"
		guest_binary="/private/var/tmp/select_fdset_guest"
	else
		guest_source="/private/var/tmp/guest-macho-batch/$name/$name.c"
		guest_binary="/private/var/tmp/guest-macho-batch/$name/$name"
		mkdir -p -- "$batch_stage/$name"
	fi
	cp -- "${fixture_source_files[$index]}" "$prefix$guest_source"
done

set_batch_state runtime-bootstrap RUNNING
west test --prefix "$prefix" \
	--bootstrap-runtime-profile homebrew-guest-toolchain-provisioning \
	--runtime-build-timeout-seconds 1800
rootless_prefix_assert_guest_toolchain corpus "$prefix"

set_batch_state guest-compile RUNNING
compiler_path="/Library/Developer/CommandLineTools/usr/bin/clang"
sdk_path="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
batch_guest_script="$work/batch-guest.sh"
{
	printf '%s\n' 'set -u'
	printf '%s\n' "cc=$(printf '%q' "$compiler_path")"
	printf '%s\n' "sdk=$(printf '%q' "$sdk_path")"
	printf '%s\n' 'compile_failed=0'
	printf '%s\n' 'anchor_compile_rc=1'
	for index in "${!fixture_names[@]}"; do
		name="${fixture_names[$index]}"
		compile_flags=()
		while IFS= read -r flag; do
			compile_flags+=("$flag")
		done < <(python3 -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])))' "${fixture_compile_json[$index]}")
		link_flags=()
		while IFS= read -r flag; do
			link_flags+=("$flag")
		done < <(python3 -c 'import json,sys; print("\n".join(json.loads(sys.argv[1])))' "${fixture_link_json[$index]}")
		if [[ "$name" == "select_fdset_guest" ]]; then
			guest_source="/private/var/tmp/select_fdset_guest.c"
			guest_binary="/private/var/tmp/select_fdset_guest"
		else
			guest_source="/private/var/tmp/guest-macho-batch/$name/$name.c"
			guest_binary="/private/var/tmp/guest-macho-batch/$name/$name"
		fi
		version="${guest_binary}.clang-version"
		origin="${guest_binary}.clang-origin"
		compile_log="${guest_binary}.compile.log"
		compile_status="${guest_binary}.compile-status.tsv"
		printf '"$cc" --version > %q\n' "$version"
		printf 'printf "%%s\\n" %q %q > %q\n' 'execution-context=guest' "executable=$compiler_path" "$origin"
		printf '"$cc"'
		printf ' %q' "${compile_flags[@]}"
		printf ' %q' "$guest_source"
		printf ' %q' "${link_flags[@]}"
		printf ' -o %q > %q 2>&1\n' "$guest_binary" "$compile_log"
		printf 'compile_rc=$?\n'
		printf 'if (( compile_rc == 0 )); then\n'
		printf '  printf "field\\tvalue\\nfixture\\t%%s\\ncompile-status\\tPASS\\ncompile-exit-code\\t0\\n" %q > %q\n' "$name" "$compile_status"
		printf 'else\n'
		printf '  compile_failed=1\n'
		printf '  printf "field\\tvalue\\nfixture\\t%%s\\ncompile-status\\tFAILED\\ncompile-exit-code\\t%%s\\n" %q "$compile_rc" > %q\n' "$name" "$compile_status"
		printf 'fi\n'
		if [[ "$name" == "select_fdset_guest" ]]; then
			printf 'anchor_compile_rc=$compile_rc\n'
		fi
	done
	anchor_binary="/private/var/tmp/select_fdset_guest"
	anchor_log="/private/var/tmp/select_fdset_guest.runtime.log"
	anchor_runtime_status="/private/var/tmp/select_fdset_guest.runtime-status.tsv"
	printf 'runtime_rc=125\n'
	printf 'if (( anchor_compile_rc == 0 )); then\n'
	printf '  runtime_rc=0\n'
	printf '  "$anchor_binary" > %q 2>&1 || runtime_rc=$?\n' "$anchor_log"
	printf '  printf "field\\tvalue\\nfixture\\tselect_fdset_guest\\nruntime-status\\tEXECUTED\\nruntime-exit-code\\t%%s\\n" "$runtime_rc" > %q\n' "$anchor_runtime_status"
	printf 'else\n'
	printf '  printf "field\\tvalue\\nfixture\\tselect_fdset_guest\\nruntime-status\\tNOT_RUN\\nruntime-exit-code\\tNOT_RUN\\n" > %q\n' "$anchor_runtime_status"
	printf 'fi\n'
	printf 'if (( compile_failed != 0 )); then exit 1; fi\n'
	printf 'exit "$runtime_rc"\n'
} >"$batch_guest_script"
guest_script="$(<"$batch_guest_script")"
guest_rc=0
DARLING_ROOTLESS=1 \
	DARLING_NOOVERLAYFS=1 \
	DARLING_EUNION=1 \
	darling_guest_shell "$prefix/bin/darling" "$prefix" 600 "$guest_script" \
	>"$work/batch-guest.log" 2>&1 || guest_rc=$?

shutdown_rc=0
DARLING_ROOTLESS=1 \
	DARLING_NOOVERLAYFS=1 \
	DARLING_EUNION=1 \
	DPREFIX="$prefix" \
	DARLING_PREFIX="$prefix" \
	west test --prefix "$prefix" --cleanup-prefix || shutdown_rc=$?
if (( shutdown_rc != 0 )); then
	echo "host-side Darling shutdown failed with rc $shutdown_rc" >&2
	guest_rc="$shutdown_rc"
fi

set_batch_state evidence RUNNING
compiler_path="/Library/Developer/CommandLineTools/usr/bin/clang"
compiler_target="$(readlink -f -- "$prefix$compiler_path")"
[[ -x "$compiler_target" ]] || {
	echo "guest compiler target is not executable: $compiler_target" >&2
	exit 1
}
compiler_sha256="$(sha256sum -- "$compiler_target" | cut -d' ' -f1)"
clt_provenance="$root/docs/clt-provenance-041-90419.txt"
objdump="$(command -v llvm-objdump || true)"
[[ -n "$objdump" ]] || {
	echo "llvm-objdump is required for batch evidence" >&2
	exit 1
}

manifest="$output/batch-manifest.tsv"
printf 'fixture\tsource-project\tsource-path\tsource-revision\tsource-sha256\tpatch-path\tpatch-sha256\tartifact-sha256\truntime-profile\texpected-marker\n' >"$manifest"
for index in "${!fixture_names[@]}"; do
	name="${fixture_names[$index]}"
	marker="${fixture_markers[$index]}"
	fixture_dir="$output/fixtures/$name"
	mkdir -p -- "$fixture_dir"
	if [[ "$name" == "select_fdset_guest" ]]; then
		guest_binary="/private/var/tmp/select_fdset_guest"
		runtime_log="/private/var/tmp/select_fdset_guest.runtime.log"
		runtime_status="/private/var/tmp/select_fdset_guest.runtime-status.tsv"
	else
		guest_binary="/private/var/tmp/guest-macho-batch/$name/$name"
		runtime_log=""
		runtime_status=""
	fi
	compile_log="${guest_binary}.compile.log"
	compile_status="${guest_binary}.compile-status.tsv"
	cp -- "$prefix$compile_log" "$fixture_dir/compile.log"
	cp -- "$prefix$compile_status" "$fixture_dir/compile-status.tsv"
	cp -- "$prefix${guest_binary}.clang-version" "$fixture_dir/clang-version.txt"
	cp -- "$prefix${guest_binary}.clang-origin" "$fixture_dir/clang-origin.txt"
	cp -- "$clt_provenance" "$fixture_dir/clt-provenance.txt"
	if [[ "$name" == "select_fdset_guest" ]]; then
		cp -- "$prefix$runtime_status" "$fixture_dir/.anchor-runtime-status.tsv"
		cp -- "$prefix$runtime_log" "$fixture_dir/runtime.log"
	else
		printf 'RUNTIME_NOT_RUN_COMPILE_ONLY\n' >"$fixture_dir/runtime.log"
		printf 'field\tvalue\nfixture\t%s\nruntime-mode\tcompile-only\nruntime-status\tNOT_RUN\nruntime-exit-code\tNOT_RUN\nobserved-marker\tNOT_OBSERVED\n' "$name" >"$fixture_dir/runtime-evidence.tsv"
	fi
	compile_status_value="$(PYTHONPATH="$root/ci" python3 -B -c 'from pathlib import Path; import sys; print(dict(line.split("\t",1) for line in Path(sys.argv[1]).read_text().splitlines()[1:])[\"compile-status\"])' "$fixture_dir/compile-status.tsv")"
	if [[ "$compile_status_value" == "PASS" ]]; then
		[[ -x "$prefix$guest_binary" ]] || {
			echo "compile status says PASS but binary is missing: $name" >&2
			guest_rc=1
			continue
		}
		cp -- "$prefix$guest_binary" "$fixture_dir/$name"
		chmod 0755 "$fixture_dir/$name"
		file --brief "$fixture_dir/$name" >"$fixture_dir/file.txt"
		"$objdump" --macho --dylibs-used "$fixture_dir/$name" >"$fixture_dir/dylibs-used.txt"
		"$objdump" --macho --private-header "$fixture_dir/$name" >"$fixture_dir/private-header.txt"
		PYTHONPATH="$root/west_commands" python3 -B \
			"$root/ci/inspect-guest-macho-batch.py" \
			"$fixture_dir/$name" "$fixture_dir/macho-manifest.json" "$fixture_dir/macho-summary.tsv"
	else
		echo "compile failed for $name; preserving compile evidence only" >&2
		guest_rc=1
		continue
	fi
	artifact_sha256="$(sha256sum -- "$fixture_dir/$name" | cut -d' ' -f1)"
	if [[ "$name" == "select_fdset_guest" && "$artifact_sha256" != \
		de9e7097a60f7f0aaf31bc6be0bac760bccf9f6d2a412d5b16aa14ec5685eab6 ]]; then
		echo "select_fdset_guest anchor SHA-256 mismatch: $artifact_sha256" >&2
		guest_rc=1
	fi
	if [[ "$name" == "select_fdset_guest" ]]; then
		runtime_exit="$(PYTHONPATH="$root/ci" python3 -B -c 'from pathlib import Path; import sys; print(dict(line.split("\t",1) for line in Path(sys.argv[1]).read_text().splitlines()[1:])[\"runtime-exit-code\"])' "$fixture_dir/.anchor-runtime-status.tsv")"
		if [[ "$runtime_exit" == "0" ]] && grep -Fxq "$marker" "$fixture_dir/runtime.log"; then
			runtime_mode="anchor"
			runtime_status_value="OBSERVED"
			observed_marker="$marker"
			printf 'field\tvalue\nfixture\t%s\nruntime-mode\tanchor\nruntime-status\tOBSERVED\nruntime-exit-code\t0\nobserved-marker\t%s\n' "$name" "$marker" >"$fixture_dir/runtime-evidence.tsv"
		else
			runtime_mode="anchor"
			runtime_status_value="FAILED"
			observed_marker="NOT_OBSERVED"
			printf 'field\tvalue\nfixture\t%s\nruntime-mode\tanchor\nruntime-status\tFAILED\nruntime-exit-code\t%s\nobserved-marker\tNOT_OBSERVED\n' "$name" "$runtime_exit" >"$fixture_dir/runtime-evidence.tsv"
			guest_rc=1
		fi
		rm -f -- "$fixture_dir/.anchor-runtime-status.tsv"
	else
		runtime_mode="compile-only"
		runtime_status_value="NOT_RUN"
		observed_marker="NOT_OBSERVED"
	fi
	compile_flags_value="$(python3 -c 'import json,sys; print("|".join(json.loads(sys.argv[1])))' "${fixture_compile_json[$index]}")"
	link_flags_value="$(python3 -c 'import json,sys; print("|".join(json.loads(sys.argv[1])))' "${fixture_link_json[$index]}")"
	[[ -n "$link_flags_value" ]] || link_flags_value="-"
	clang_version="$(tr '\n' ' ' <"$fixture_dir/clang-version.txt" | sed 's/[[:space:]]*$//')"
	clang_origin="$(tr '\n' ';' <"$fixture_dir/clang-origin.txt" | sed 's/;$//')"
	private_header_sha256="$(sha256sum -- "$fixture_dir/private-header.txt" | cut -d' ' -f1)"
	dylibs_sha256="$(sha256sum -- "$fixture_dir/dylibs-used.txt" | cut -d' ' -f1)"
	summary_sha256="$(sha256sum -- "$fixture_dir/macho-summary.tsv" | cut -d' ' -f1)"
	clt_hash="$(sha256sum -- "$fixture_dir/clt-provenance.txt" | cut -d' ' -f1)"
	{
		printf 'status: REVIEWED_PROVENANCE\n'
		printf 'clt_review_status: reviewed\n'
		printf 'batch: guest-macho-phase-3b\n'
		printf 'fixture: %s\n' "$name"
		printf 'expected_marker: %s\n' "$marker"
		printf 'runtime_mode: %s\n' "$runtime_mode"
		printf 'runtime_status: %s\n' "$runtime_status_value"
		printf 'observed_marker: %s\n' "$observed_marker"
	} >"$fixture_dir/provenance.txt"
	provenance_document_sha256="$(sha256sum -- "$fixture_dir/provenance.txt" | cut -d' ' -f1)"
	{
		printf 'field\tvalue\n'
		printf 'schema\t1\n'
		printf 'fixture\t%s\n' "$name"
		printf 'source-project\t%s\n' "${fixture_projects[$index]}"
		printf 'source-path\t%s\n' "${fixture_paths[$index]}"
		printf 'source-revision\t%s\n' "${fixture_source_revisions[$index]}"
		printf 'source-sha256\t%s\n' "${fixture_source_hashes[$index]}"
		printf 'patch-path\t%s\n' "${fixture_patch_paths[$index]}"
		printf 'patch-sha256\t%s\n' "${fixture_patch_hashes[$index]}"
		printf 'compile-flags\t%s\n' "$compile_flags_value"
		printf 'link-flags\t%s\n' "$link_flags_value"
		printf 'runtime-profile\t%s\n' "${fixture_profiles[$index]}"
		printf 'bootstrap-profile\thomebrew-guest-toolchain-provisioning\n'
		printf 'compiler-path\t%s\n' "$compiler_path"
		printf 'compiler-sha256\t%s\n' "$compiler_sha256"
		printf 'compiler-version\t%s\n' "$clang_version"
		printf 'compiler-origin\t%s\n' "$clang_origin"
		printf 'clt-product-id\t041-90419\n'
		printf 'clt-provenance-sha256\t%s\n' "$clt_hash"
		printf 'provenance-document-sha256\t%s\n' "$provenance_document_sha256"
		printf 'artifact-sha256\t%s\n' "$artifact_sha256"
		printf 'private-header-sha256\t%s\n' "$private_header_sha256"
		printf 'dylibs-report-sha256\t%s\n' "$dylibs_sha256"
		printf 'macho-summary-sha256\t%s\n' "$summary_sha256"
		printf 'expected-returncode\t0\n'
		printf 'expected-marker\t%s\n' "$marker"
		printf 'runtime-mode\t%s\n' "$runtime_mode"
		printf 'runtime-status\t%s\n' "$runtime_status_value"
		printf 'observed-marker\t%s\n' "$observed_marker"
	} >"$fixture_dir/provenance.tsv"
	printf '%s  %s\n' "$artifact_sha256" "$name" >"$fixture_dir/artifact.sha256"
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
		"$name" "${fixture_projects[$index]}" "${fixture_paths[$index]}" \
		"${fixture_source_revisions[$index]}" "${fixture_source_hashes[$index]}" \
		"${fixture_patch_paths[$index]}" "${fixture_patch_hashes[$index]}" \
		"$artifact_sha256" "${fixture_profiles[$index]}" "$marker" >>"$manifest"
done

if (( guest_rc != 0 )); then
	echo "batch completed with compile/runtime failures; evidence preserved" >&2
	exit "$guest_rc"
fi
echo "MACHO_CORPUS_BATCH_EVIDENCE_COMPLETE=1"

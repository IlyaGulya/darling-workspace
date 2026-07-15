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
		echo "pilot output must be outside the repository: $output" >&2
		exit 2
		;;
esac

source_file="$root/tests/select_fdset_guest.c"
[[ -f "$source_file" ]] || {
	echo "pilot source is missing: $source_file" >&2
	exit 1
}
mkdir -p -- "$output"

work="$output/.work"
runner_temp="$work/runner-temp"
mkdir -p -- "$runner_temp"
export ROOTLESS_TIER_REPO="$root"
export RUNNER_TEMP="$runner_temp"
export TMPDIR="$work"
export DARLING_CLT_CACHE="$work/clt-cache"
. "$root/ci/rootless-prefix.sh"

source_sha256="$(sha256sum -- "$source_file" | cut -d' ' -f1)"
compiler_path="/Library/Developer/CommandLineTools/usr/bin/clang"
sdk_path="/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
build_flags=(
	-isysroot "$sdk_path"
	-std=gnu11
	-Wall
	-Wextra
	-Werror
)
active_prefix=""

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
		echo "pilot cleanup failed; preserving work diagnostics: $work" >&2
	else
		active_prefix=""
		rm -rf -- "$work"
	fi
	if (( original_rc != 0 )); then
		return "$original_rc"
	fi
	return $((cleanup_rc || gc_rc || jobs_rc || remove_rc))
}

on_exit() {
	local rc="$?"
	cleanup_prefix "$rc" || rc=$?
	exit "$rc"
}
trap on_exit EXIT INT TERM

ccache_env="$work/ccache.env"
ccache_output="$work/ccache.output"
mkdir -p -- "$work"
: > "$ccache_env"
: > "$ccache_output"
GITHUB_ENV="$ccache_env" \
GITHUB_OUTPUT="$ccache_output" \
GITHUB_SHA="local-macho-corpus-$variant" \
RUNNER_ARCH=X64 \
RUNNER_OS=Linux \
	"$root/ci/guest-toolchain-ccache.sh" prepare cold
set -a
. "$ccache_env"
set +a

prefix="$(rootless_prefix_create corpus CORPUS_PREFIX)"
active_prefix="$prefix"
printf '%s\n' "$prefix" > "$output/.prefix-path"
stage="$prefix/private/var/tmp"
mkdir -p -- "$stage"
cp -- "$source_file" "$stage/select_fdset_guest.c"

echo "bootstrap with reviewed guest CommandLineTools (pilot $variant)"
west test --prefix "$prefix" \
	--bootstrap-runtime-profile homebrew-guest-toolchain-provisioning \
	--runtime-build-timeout-seconds 1800
rootless_prefix_assert_guest_toolchain corpus "$prefix"

guest_script='set -eu
cc=/Library/Developer/CommandLineTools/usr/bin/clang
sdk=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk
source=/private/var/tmp/select_fdset_guest.c
binary=/private/var/tmp/select_fdset_guest
version=/private/var/tmp/select_fdset_guest.clang-version
origin=/private/var/tmp/select_fdset_guest.clang-origin
marker=/private/var/tmp/select_fdset_guest.marker
"$cc" --version > "$version"
printf "%s\n" "execution-context=guest" "executable=$cc" > "$origin"
"$cc" -isysroot "$sdk" -std=gnu11 -Wall -Wextra -Werror "$source" -o "$binary"
"$binary"
printf "%s\n" SELECT_FDSET_GUEST_OK > "$marker"'

echo "compile and execute select_fdset_guest inside Darling"
guest_output="$({
	env \
		DARLING_ROOTLESS=1 \
		DARLING_NOOVERLAYFS=1 \
		DARLING_EUNION=1 \
		DPREFIX="$prefix" \
		DARLING_PREFIX="$prefix" \
		"$prefix/bin/darling" shell /bin/bash --login -c "$guest_script"
} | tee "$output/guest-build.log")"
case "$guest_output" in
	*SELECT_FDSET_GUEST_OK*) ;;
	*)
		echo "guest pilot did not emit SELECT_FDSET_GUEST_OK" >&2
		exit 1
		;;
esac

artifact="$stage/select_fdset_guest"
[[ -x "$artifact" ]] || {
	echo "guest pilot did not produce an executable Mach-O: $artifact" >&2
	exit 1
}
cp -- "$artifact" "$output/select_fdset_guest"
cp -- "$stage/select_fdset_guest.clang-version" "$output/clang-version.txt"
cp -- "$stage/select_fdset_guest.clang-origin" "$output/clang-origin.txt"
cp -- "$stage/select_fdset_guest.marker" "$output/guest-marker.txt"
chmod 0755 "$output/select_fdset_guest"

compiler_target="$(readlink -f -- "$prefix$compiler_path")"
[[ -x "$compiler_target" ]] || {
	echo "guest compiler target is not executable: $compiler_target" >&2
	exit 1
}
compiler_sha256="$(sha256sum -- "$compiler_target" | cut -d' ' -f1)"
clt_provenance="$root/docs/clt-provenance-041-90419.txt"
clt_provenance_sha256="$(sha256sum -- "$clt_provenance" | cut -d' ' -f1)"
cp -- "$clt_provenance" "$output/clt-provenance.txt"

file --brief "$output/select_fdset_guest" > "$output/file.txt"
objdump="$(command -v llvm-objdump || true)"
[[ -n "$objdump" ]] || {
	echo "llvm-objdump is required for pilot evidence" >&2
	exit 1
}
"$objdump" --macho --dylibs-used "$output/select_fdset_guest" > "$output/dylibs-used.txt"
"$objdump" --macho --private-header "$output/select_fdset_guest" > "$output/private-header.txt"

PYTHONPATH="$root/west_commands" python3 -B \
	"$root/ci/inspect-guest-macho.py" \
	"$output/select_fdset_guest" "$output/macho-manifest.json" "$output/macho-summary.tsv"

artifact_sha256="$(sha256sum -- "$output/select_fdset_guest" | cut -d' ' -f1)"
private_header_sha256="$(sha256sum -- "$output/private-header.txt" | cut -d' ' -f1)"
dylibs_sha256="$(sha256sum -- "$output/dylibs-used.txt" | cut -d' ' -f1)"
summary_sha256="$(sha256sum -- "$output/macho-summary.tsv" | cut -d' ' -f1)"
clang_version="$(tr '\n' ' ' < "$output/clang-version.txt" | sed 's/[[:space:]]*$//')"
clang_origin="$(tr '\n' ';' < "$output/clang-origin.txt" | sed 's/;$//')"
flags_value="$(printf '%s|' "${build_flags[@]}" | sed 's/|$//')"

{
	printf 'status: REVIEWED_PROVENANCE\n'
	printf 'clt_review_status: reviewed\n'
	printf 'pilot: select_fdset_guest\n'
	printf 'matrix_variant: %s\n' "$variant"
	printf 'source_sha256: %s\n' "$source_sha256"
	printf 'compiler_path: %s\n' "$compiler_path"
	printf 'compiler_sha256: %s\n' "$compiler_sha256"
	printf 'clt_product_id: 041-90419\n'
	printf 'clt_provenance: clt-provenance.txt\n'
	printf 'artifact_sha256: %s\n' "$artifact_sha256"
	printf 'expected_returncode: 0\n'
	printf 'expected_marker: SELECT_FDSET_GUEST_OK\n'
} > "$output/provenance.txt"
provenance_document_sha256="$(sha256sum -- "$output/provenance.txt" | cut -d' ' -f1)"

{
	printf 'field\tvalue\n'
	printf 'schema\t1\n'
	printf 'pilot\tselect_fdset_guest\n'
	printf 'source-path\ttests/select_fdset_guest.c\n'
	printf 'source-sha256\t%s\n' "$source_sha256"
	printf 'compiler-path\t%s\n' "$compiler_path"
	printf 'compiler-sha256\t%s\n' "$compiler_sha256"
	printf 'compiler-version\t%s\n' "$clang_version"
	printf 'compiler-origin\t%s\n' "$clang_origin"
	printf 'flags\t%s\n' "$flags_value"
	printf 'clt-product-id\t041-90419\n'
	printf 'clt-provenance-sha256\t%s\n' "$clt_provenance_sha256"
	printf 'provenance-document-sha256\t%s\n' "$provenance_document_sha256"
	printf 'artifact-sha256\t%s\n' "$artifact_sha256"
	printf 'private-header-sha256\t%s\n' "$private_header_sha256"
	printf 'dylibs-report-sha256\t%s\n' "$dylibs_sha256"
	printf 'macho-summary-sha256\t%s\n' "$summary_sha256"
	printf 'expected-returncode\t0\n'
	printf 'expected-marker\tSELECT_FDSET_GUEST_OK\n'
} > "$output/provenance.tsv"
printf '%s  %s\n' "$artifact_sha256" select_fdset_guest > "$output/artifact.sha256"

echo "MACHO_CORPUS_PILOT_EVIDENCE_COMPLETE=1"

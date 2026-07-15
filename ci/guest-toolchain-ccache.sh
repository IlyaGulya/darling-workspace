#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command_name="${1:-}"

case "$command_name" in
	prepare)
		mode="${2:?prepare requires warm or cold mode}"
		case "$mode" in
			warm)
				cache_dir="${HOME}/.cache/darling-guest-toolchain-ccache"
				;;
			cold)
				cache_dir="${RUNNER_TEMP:?RUNNER_TEMP is required for cold ccache mode}/darling-guest-toolchain-ccache"
				;;
			*)
				echo "unsupported ccache mode: $mode" >&2
				exit 2
				;;
		esac
		command -v ccache >/dev/null
		command -v clang >/dev/null
		command -v clang++ >/dev/null
		mkdir -p -- "$cache_dir"

		clang_path="$(readlink -f -- "$(command -v clang)")"
		clangxx_path="$(readlink -f -- "$(command -v clang++)")"
		clang_fingerprint="$(sha256sum -- "$clang_path" | cut -d' ' -f1)"
		clangxx_fingerprint="$(sha256sum -- "$clangxx_path" | cut -d' ' -f1)"
		compiler_fingerprint="$({
			printf 'clang_path=%s\n' "$clang_path"
			printf 'clang_fingerprint=%s\n' "$clang_fingerprint"
			printf 'clangxx_path=%s\n' "$clangxx_path"
			printf 'clangxx_fingerprint=%s\n' "$clangxx_fingerprint"
			"$clang_path" --version
			"$clangxx_path" --version
		} | sha256sum | cut -c1-16)"
		ccache_version="$(ccache --version | sed -n '1s/.*version //p')"
		[[ -n "$ccache_version" ]] || {
			echo "could not determine ccache compatibility version" >&2
			exit 1
		}
		ccache_compatibility="ccache-${ccache_version//[^A-Za-z0-9._-]/-}"
		contract_files=(
			.github/workflows/test-infra.yml
			ci/guest-toolchain-ccache.sh
			ci/run-test-tier.sh
			west_commands/test.py
			west_commands/test_runtime.py
			west_commands/test_runtime_build.py
			testkit/runtime-profiles.yml
		)
		for file in "${contract_files[@]}"; do
			[[ -f "$root/$file" ]] || {
				echo "runtime contract input is missing: $file" >&2
				exit 1
			}
		done
		runtime_contract_fingerprint="$(
			cd "$root"
			sha256sum -- "${contract_files[@]}" | sha256sum | cut -c1-16
		)"
		key_prefix="darling-guest-toolchain-ccache-v1-${RUNNER_OS:-unknown}-${RUNNER_ARCH:-unknown}-${compiler_fingerprint}-${ccache_compatibility}-${runtime_contract_fingerprint}-"
		primary_key="${key_prefix}${GITHUB_SHA:-local}"

		{
			printf 'CCACHE_DIR=%s\n' "$cache_dir"
			printf 'CCACHE_HASHDIR=true\n'
			printf 'CCACHE_COMPILERCHECK=content\n'
			printf 'CCACHE_MAXSIZE=2G\n'
			printf 'CCACHE_CLANG_PATH=%s\n' "$clang_path"
			printf 'CCACHE_CLANGXX_PATH=%s\n' "$clangxx_path"
			printf 'CCACHE_CLANG_FINGERPRINT=%s\n' "$clang_fingerprint"
			printf 'CCACHE_CLANGXX_FINGERPRINT=%s\n' "$clangxx_fingerprint"
		} >>"${GITHUB_ENV:?GITHUB_ENV is required}"
		{
			printf 'mode=%s\n' "$mode"
			printf 'cache_dir=%s\n' "$cache_dir"
			printf 'primary_key=%s\n' "$primary_key"
			printf 'restore_prefix=%s\n' "$key_prefix"
			printf 'compiler_fingerprint=%s\n' "$compiler_fingerprint"
			printf 'clang_path=%s\n' "$clang_path"
			printf 'clangxx_path=%s\n' "$clangxx_path"
			printf 'clang_fingerprint=%s\n' "$clang_fingerprint"
			printf 'clangxx_fingerprint=%s\n' "$clangxx_fingerprint"
			printf 'ccache_compatibility=%s\n' "$ccache_compatibility"
			printf 'runtime_contract_fingerprint=%s\n' "$runtime_contract_fingerprint"
		} >>"${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
		;;
	stats)
		output_dir="${2:?stats requires an output directory}"
		mkdir -p -- "$output_dir"
		ccache --print-stats >"$output_dir/ccache-stats.txt"
		ccache --show-stats >"$output_dir/ccache-stats-human.txt"
		printf 'CCACHE_DIR=%s\n' "${CCACHE_DIR:?CCACHE_DIR is required}" >"$output_dir/ccache-environment.txt"
		printf 'CCACHE_HASHDIR=%s\n' "${CCACHE_HASHDIR:-}" >>"$output_dir/ccache-environment.txt"
		printf 'CCACHE_COMPILERCHECK=%s\n' "${CCACHE_COMPILERCHECK:-}" >>"$output_dir/ccache-environment.txt"
		;;
	*)
		echo "usage: $0 prepare warm|cold | stats OUTPUT_DIR" >&2
		exit 2
		;;
esac

#!/usr/bin/env bash

export DW_STATE_ROOT="${DW_STATE_ROOT:-${ROOTLESS_TIER_REPO:?}/../.west-state/ci}"

rootless_runtime_select() {
	[[ "$DW_STATE_ROOT" == /* ]] || {
		rootless_prefix_die "DW_STATE_ROOT must be absolute"
		return 2
	}
	mkdir -p -- "$DW_STATE_ROOT/tmp" || return
	: "${DARLING_RUNTIME_ROOT:?set DARLING_RUNTIME_ROOT to the normal product installation root}"
	[[ "$DARLING_RUNTIME_ROOT" == /* ]] || {
		rootless_prefix_die "DARLING_RUNTIME_ROOT must be absolute"
		return 2
	}
	export DARLING_RUNTIME_ROOT
	export DARLING_BUILD_DIR="${DARLING_BUILD_DIR:-$DW_STATE_ROOT/runtime-build}"
	[[ "$DARLING_BUILD_DIR" == /* ]] || {
		rootless_prefix_die "DARLING_BUILD_DIR must be absolute"
		return 2
	}
	export DARLING_LAUNCHER="$DARLING_RUNTIME_ROOT/bin/darling"
}

rootless_runtime_bootstrap() {
	local prefix="$1"
	local toolchain="${2:-}"
	local args=(--build-dir "$DARLING_BUILD_DIR" --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$prefix")
	[[ "${DARLING_CI_TEST_DATA:-0}" != 1 ]] || args+=(--with-test-data)
	if [[ -x "$DARLING_RUNTIME_ROOT/bin/darling" &&
		-x "$DARLING_RUNTIME_ROOT/bin/darlingserver" &&
		-d "$DARLING_RUNTIME_ROOT/libexec/darling" ]]; then
		args+=(--no-build)
	else
		local source
		source="$(west list darling -f '{abspath}')" || return
		cmake -S "$source" -B "$DARLING_BUILD_DIR" -G Ninja \
			-DCMAKE_BUILD_TYPE=Release -DCOMPONENTS=stock \
			-DCMAKE_INSTALL_PREFIX="$DARLING_RUNTIME_ROOT" || return
	fi
	[[ -z "$toolchain" ]] || args+=(--guest-toolchain "$toolchain")
	west darling-bootstrap "${args[@]}"
}

rootless_prefix_die() {
	echo "rootless tier prefix: $*" >&2
	return 2
}

rootless_prefix_trusted_root() {
	local root="${DW_STATE_ROOT}/p"
	local repo="${ROOTLESS_TIER_REPO:?run-test-tier must set ROOTLESS_TIER_REPO}"
	[[ "$DW_STATE_ROOT" == /* ]] || {
		rootless_prefix_die "DW_STATE_ROOT must be absolute"
		return 2
	}
	root="$(realpath -m -- "$root")" || return 2
	repo="$(realpath -m -- "$repo")" || return 2
	local state_root
	state_root="$(realpath -m -- "$DW_STATE_ROOT")" || return 2
	[[ "$state_root" != / && "$state_root" != "$HOME" && "$state_root" != "$repo" ]] || {
		rootless_prefix_die "unsafe DW_STATE_ROOT: $state_root"
		return 2
	}
	[[ "$root" != "/" ]] || {
		rootless_prefix_die "trusted root must not be filesystem root"
		return 2
	}
	case "$root" in
		"$HOME"|"$repo"|"$repo"/*)
			rootless_prefix_die "trusted root is unsafe: $root" || return
			return 2
			;;
	esac
	[[ -d "$root" ]] || mkdir -p -- "$root" || return 2
	printf '%s\n' "$root"
}

rootless_prefix_validate_path() {
	local kind="$1"
	local prefix="$2"
	local trusted_root
	local repo
	local base
	trusted_root="$(rootless_prefix_trusted_root)" || return 2
	repo="$(realpath -m -- "${ROOTLESS_TIER_REPO:?}")" || return 2
	prefix="$(realpath -m -- "$prefix")" || return 2
	base="$(basename -- "$prefix")"
	[[ "$(dirname -- "$prefix")" == "$trusted_root" ]] || {
		rootless_prefix_die "prefix must be directly under $trusted_root: $prefix"
		return 2
	}
	[[ "$base" =~ ^dw-${kind:0:1}([.-][A-Za-z0-9_]+)*$ ]] || {
		rootless_prefix_die "prefix has an unexpected basename: $base"
		return 2
	}
	case "$prefix" in
		"/"|"$HOME"|"$repo"|"$repo"/*)
			rootless_prefix_die "refusing a dangerous prefix path: $prefix"
			return 2
			;;
	esac
	[[ -d "$prefix" && ! -L "$prefix" ]] || {
		rootless_prefix_die "prefix is not a non-symlink directory: $prefix"
		return 2
	}
}

rootless_prefix_create() {
	local kind="$1"
	local variable="$2"
	local trusted_root
	local requested="${!variable:-}"
	local prefix
	local owner
	trusted_root="$(rootless_prefix_trusted_root)" || return 2
	if [[ -n "$requested" ]]; then
		prefix="$(realpath -m -- "$requested")" || return 2
		rootless_prefix_validate_path "$kind" "$prefix" 2>/dev/null && {
			rootless_prefix_die "refusing to reuse an existing prefix: $prefix"
			return 2
		}
		[[ "$(dirname -- "$prefix")" == "$trusted_root" ]] || return 2
		[[ ! -e "$prefix" && ! -L "$prefix" ]] || {
			rootless_prefix_die "requested prefix already exists: $prefix"
			return 2
		}
		mkdir -- "$prefix" || return 2
	else
		prefix="$(mktemp -d -- "$trusted_root/dw-${kind:0:1}.XXXXXX")" || return 2
	fi
	owner="${prefix}.west-tier-owner"
	if ! {
		printf 'schema=1\nkind=%s\npid=%s\n\n' "$kind" "$$" >"$owner"
	}; then
		rmdir -- "$prefix" 2>/dev/null || true
		return 2
	fi
	printf '%s\n' "$prefix"
}

rootless_prefix_export_output() {
	local name="$1"
	local prefix="$2"
	[[ -n "${GITHUB_OUTPUT:-}" ]] || return 0
	printf '%s=%s\n' "$name" "$prefix" >>"$GITHUB_OUTPUT"
}

rootless_prefix_has_guest_toolchain() {
	local prefix="$1"
	local root
	for root in \
		"$prefix/Library/Developer/CommandLineTools" \
		"$prefix/libexec/darling/Library/Developer/CommandLineTools"; do
		if [[ -x "$root/usr/bin/clang" && -d "$root/SDKs/MacOSX.sdk" ]]; then
			return 0
		fi
	done
	return 1
}

rootless_prefix_contains_guest_toolchain() {
	local prefix="$1"
	local root
	for root in \
		"$prefix/Library/Developer/CommandLineTools" \
		"$prefix/libexec/darling/Library/Developer/CommandLineTools"; do
		if [[ -e "$root" || -L "$root" ]]; then
			return 0
		fi
	done
	return 1
}

rootless_prefix_assert_no_guest_toolchain() {
	local kind="$1"
	local prefix="$2"
	rootless_prefix_assert_owned "$kind" "$prefix" || return 2
	if rootless_prefix_contains_guest_toolchain "$prefix"; then
		rootless_prefix_die "no-CLT tier left CommandLineTools in prefix: $prefix"
		return 1
	fi
}

rootless_prefix_assert_guest_toolchain() {
	local kind="$1"
	local prefix="$2"
	rootless_prefix_assert_owned "$kind" "$prefix" || return 2
	rootless_prefix_has_guest_toolchain "$prefix" || {
		rootless_prefix_die "CLT tier did not leave a usable CommandLineTools in prefix: $prefix"
		return 1
	}
}

rootless_prefix_assert_owned() {
	local kind="$1"
	local prefix="$2"
	local owner="${prefix}.west-tier-owner"
	rootless_prefix_validate_path "$kind" "$prefix" || return 2
	[[ -f "$owner" && ! -L "$owner" ]] || {
		rootless_prefix_die "prefix is not owned by this tier: $prefix"
		return 2
	}
	grep -F -x -q "kind=$kind" "$owner" || {
		rootless_prefix_die "prefix owner kind mismatch: $prefix"
		return 2
	}
}

rootless_prefix_remove() {
	local kind="$1"
	local prefix="$2"
	local owner="${prefix}.west-tier-owner"
	rootless_prefix_assert_owned "$kind" "$prefix" || return 2
	rm -rf -- "$prefix" || return 2
	rm -f -- "$owner" || return 2
}

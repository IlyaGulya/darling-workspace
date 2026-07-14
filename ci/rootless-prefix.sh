#!/usr/bin/env bash

rootless_prefix_die() {
	echo "rootless tier prefix: $*" >&2
	return 2
}

rootless_prefix_trusted_root() {
	local root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
	local repo="${ROOTLESS_TIER_REPO:?run-test-tier must set ROOTLESS_TIER_REPO}"
	root="$(realpath -m -- "$root")" || return 2
	repo="$(realpath -m -- "$repo")" || return 2
	[[ -d "$root" ]] || mkdir -p -- "$root" || return 2
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
	if [[ -z "${RUNNER_TEMP:-}" ]]; then
		case "$root" in
			"$HOME"/*)
				rootless_prefix_die "trusted root is unsafe without RUNNER_TEMP: $root" || return
				return 2
				;;
		esac
	fi
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
	[[ "$base" =~ ^darling-rootless-${kind}([.-][A-Za-z0-9_]+)*$ ]] || {
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
		prefix="$(mktemp -d -- "$trusted_root/darling-rootless-${kind}.XXXXXX")" || return 2
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

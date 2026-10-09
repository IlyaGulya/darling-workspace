#!/bin/bash
# prefix-overlay-instance.sh -- a disposable PREFIX INSTANCE built from a verified base prefix.
#
# WHY THIS EXISTS. Every experiment currently needs its own bootstrapped prefix: one base means one
# experiment at a time, and a second experiment means a second full build and boot. An overlayfs
# instance gives each experiment its own writable prefix on top of ONE read-only base, so instances
# are created and thrown away in milliseconds and can exist in parallel - which is what the T4 ladder
# needs (several capture shapes against the same verified runtime).
#
# PRIVILEGE BOUNDARY, stated because it is the whole point of the design: the PRODUCT stays rootless
# (its privileged runtime mode is retired and E-UNION is its rootless path). This script is TEST
# INFRASTRUCTURE and uses a mount capability for exactly two operations, mount and umount, against an
# instance directory this script created. It never runs the product, never touches the network, and
# never escalates beyond those two mounts. Unprivileged user namespaces are blocked on the supported
# hosts (unshare -Ur fails to write /proc/self/uid_map), so an overlay mount cannot be unprivileged
# here; `sudo -n` is required and its absence is reported rather than worked around.
#
# Layout per instance:
#   <state>/<name>/merged   <- the prefix a run points at (lowerdir is the base, upperdir is writable)
#   <state>/<name>/upper    <- everything a run writes lands here; delete it to reset
#   <state>/<name>/work     <- overlayfs work dir (must be on the same filesystem as upper)
#
# Usage:
#   prefix-overlay-instance.sh create  --base DIR --name NAME [--state-dir DIR]
#   prefix-overlay-instance.sh destroy --name NAME [--state-dir DIR]
#   prefix-overlay-instance.sh list    [--state-dir DIR]
#   prefix-overlay-instance.sh status  --name NAME [--state-dir DIR]
#
# The base is never written: it is the lowerdir. A run that needs to change something about the
# runtime changes it in the instance, and re-bootstrapping the base is a separate, deliberate act.
set -uo pipefail

state_root=${WEST_PREFIX_INSTANCE_DIR:-/home/ilyagulya/work/wr-fresh/.west-prefix-instances}

usage() { sed -n '2,40p' "$0"; exit 2; }

require_sudo() {
	sudo -n true 2>/dev/null || {
		echo "prefix-overlay-instance: sudo -n is required for the overlay mount and is not available" >&2
		exit 3
	}
}

cmd=${1:-}
[ -n "$cmd" ] || usage
shift

base=""; name=""
while [ $# -gt 0 ]; do
	case "$1" in
		--base) base=${2:?}; shift 2 ;;
		--name) name=${2:?}; shift 2 ;;
		--state-dir) state_root=${2:?}; shift 2 ;;
		*) usage ;;
	esac
done

case "$cmd" in
create)
	[ -n "$base" ] || usage
	[ -n "$name" ] || usage
	[ -d "$base" ] || { echo "prefix-overlay-instance: base '$base' is not a directory" >&2; exit 4; }
	[ -e "$base/bin/darling" ] || { echo "prefix-overlay-instance: base '$base' has no bin/darling (not a bootstrapped prefix)" >&2; exit 4; }
	# The runtime profile is what `west test --prefix` uses to select a prefix; it is not required for a
	# raw instance, so its absence is reported and not fatal (prefix-ios in this workspace has none).
	[ -e "$base/.west-runtime-profile.json" ] || echo "prefix-overlay-instance: note: base '$base' has no .west-runtime-profile.json" >&2
	require_sudo
	dir="$state_root/$name"
	[ -e "$dir/merged" ] && { echo "prefix-overlay-instance: instance '$name' already exists at $dir" >&2; exit 5; }
	mkdir -p "$dir/upper" "$dir/work" "$dir/merged"
	if ! sudo -n mount -t overlay overlay \
		-o "lowerdir=$base,upperdir=$dir/upper,workdir=$dir/work" "$dir/merged"; then
		echo "prefix-overlay-instance: overlay mount failed; leaving $dir for inspection" >&2
		exit 6
	fi
	echo "$dir/merged"
	;;
destroy)
	[ -n "$name" ] || usage
	dir="$state_root/$name"
	[ -d "$dir/merged" ] || { echo "prefix-overlay-instance: no instance '$name' at $dir" >&2; exit 5; }
	# Unmount first, always: a removed mount point leaves a mount tail, which the repo has tooling to
	# repair precisely because it is easy to get wrong.
	if mountpoint -q "$dir/merged"; then
		require_sudo
		sudo -n umount "$dir/merged" || { echo "prefix-overlay-instance: umount failed for $dir/merged" >&2; exit 6; }
	fi
	mountpoint -q "$dir/merged" && { echo "prefix-overlay-instance: $dir/merged is still mounted; refusing to delete" >&2; exit 6; }
	rm -rf "$dir"
	echo "destroyed $name"
	;;
list)
	for d in "$state_root"/*/merged; do
		[ -d "$d" ] || continue
		if mountpoint -q "$d"; then echo "mounted   $(basename "$(dirname "$d")") $d"; else echo "unmounted $(basename "$(dirname "$d")") $d"; fi
	done
	;;
status)
	[ -n "$name" ] || usage
	dir="$state_root/$name"
	if mountpoint -q "$dir/merged"; then echo "mounted $dir/merged"; else echo "not mounted: $dir/merged"; fi
	mount | grep -F "$dir/merged" | head -2
	;;
*)
	usage
	;;
esac

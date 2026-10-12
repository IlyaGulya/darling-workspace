#!/bin/sh
# xcodebuild-workload-run.sh -- the two xcodebuild workloads of the Xcode compatibility ladder,
# run out of THIS repository instead of host scratch.
#
# WHY THIS EXISTS (dar-44d8). The project the ladder built was /tmp/minproj, created ad hoc in host
# scratch on 2026-10-11 and reachable by the guest only through its /Volumes/SystemRoot mount of the
# host root. A clean checkout does not have /tmp/minproj, so the workload was not reproducible from
# the repository -- and the guest reaches /tmp through the same host-root mount that makes the
# workload invisible to CI. Both projects now live under tests/xcodebuild_fixture/ and this script
# INSTALLS the one it runs into the prefix's persistent guest directory, so the project itself no
# longer comes from host scratch or from the mount.
#
# THE ASSETS (tests/xcodebuild_fixture/)
#   minproj/  the macOS command-line tool "hello": main.m (int main(void) { return 0; }) and
#             hello.xcodeproj (target/product hello, com.apple.product-type.tool, no SDKROOT).
#             Copied here from /tmp/minproj, byte-identical to what the ladder built.
#   iosapp/   a real iOS APPLICATION "iosapp": main.m (a UIApplicationDelegate that makes a key
#             window), Info.plist (CFBundlePackageType APPL, LSRequiresIPhoneOS), iosapp.xcodeproj
#             (target/product iosapp, com.apple.product-type.application, SDKROOT iphoneos,
#             CODE_SIGNING_ALLOWED=NO, IPHONEOS_DEPLOYMENT_TARGET 17.0, -framework UIKit) and the
#             shared scheme iosapp.xcscheme. Copied here from the hand-written project at
#             m1max:/tmp/aaprobe/iosapp, which is where it was measured to build.
#
# THE EXACT COMMANDS (recorded; the probe scripts below are generated from these verbatim)
#   minproj: <XB> -project hello.xcodeproj -target hello -configuration Debug build
#   iosapp : <XB> -project iosapp.xcodeproj -target iosapp -sdk iphoneos -configuration Debug \
#                 -destination generic/platform=iOS clean build
#   The archive arm of the app, run on the Mac (Xcode 27.0) and NOT yet run in the guest:
#   iosapp : <XB> -project iosapp.xcodeproj -scheme iosapp -sdk iphoneos -configuration Release \
#                 archive -archivePath <path>.xcarchive CODE_SIGNING_ALLOWED=NO
#   <XB> is xcodebuild inside the guest:
#     /Volumes/SystemRoot/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents/Developer/usr/bin/xcodebuild
#
# THE XCODE IDENTITY THESE COMMANDS ASSUME. Xcode 26.3 (build 17C528), universal x86_64+arm64,
# decoded by the operator from the .xip they supplied; its iPhoneOS SDK is 26.2. The dependency
# edges the ladder walks are Xcode-version-specific, so a run under another Xcode is a different
# measurement. The tree is NOT in this repository (it is ~12 GB of Apple toolchain) and a clean
# checkout does not carry it: name it with --xcode-app.
#
# HOW THE GUEST REACHES THE ASSETS. The project tree is copied into
# $PREFIX/private/var/tmp/xcodebuild-<name> -- the prefix's persistent guest directory, the same
# place testkit/scripts/run-darling-c-test.sh keeps guest sources -- and the generated probe cds
# there and runs it as /private/var/tmp/xcodebuild-<name>. The build products are written back under
# that directory, which is host-visible, so the artifact check below needs no guest round trip.
#
# WHAT STILL DEPENDS ON THE /Volumes/SystemRoot HOST-ROOT MOUNT (stated, not hidden):
#   * the Xcode tree, and therefore the real clang/ld/xcodebuild and the iPhoneOS SDK;
#   * the guest's own /usr, which is the host's /usr through that same mount.
# A truly isolated CI checkout would have to bake the Xcode tree into a guest-visible volume of the
# CI image (not a host-root mount) and provision the prefix's runtime additions
# (scripts/ios-toolchain-runtime-additions.sh). The PROJECTS are already repo-local; nothing about
# them is read through the mount any more. The only /Volumes/SystemRoot path this script still
# names is the Xcode toolchain, and it prints that path in the summary so a reader can see it.
#
# Usage:
#   xcodebuild-workload-run.sh --prefix PREFIX [--workload minproj|iosapp|both]
#                              [--xcode-app PATH] [--wait SECONDS] [--log-dir DIR]
# For each workload this prints, labelled: the guest's own XCB_RC, the harness VERDICT, whether an
# artifact was produced (host path and size, or "none") and the loader stop line when the workload
# stopped inside dyld. Exit 0 only when every workload produced its artifact -- a harness verdict is
# reported as a fact, never substituted for the artifact. Run long invocations through the supported
# job owner: scripts/west-job.sh start --state-dir DIR -- bash scripts/xcodebuild-workload-run.sh ...

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
BOOT_RUN="$SCRIPT_DIR/darling-boot-run.sh"

PREFIX=""
XCODE_APP="/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app"
WORKLOADS="both"
WAIT="300"
LOG_DIR=""
XB_REL="Contents/Developer/usr/bin/xcodebuild"

usage() {
	sed -n '2,/^$/p' "$0" >&2
	exit 2
}

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--xcode-app) XCODE_APP="$2"; shift 2 ;;
		--workload) WORKLOADS="$2"; shift 2 ;;
		--wait) WAIT="$2"; shift 2 ;;
		--log-dir) LOG_DIR="$2"; shift 2 ;;
		-h|--help) usage ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[ -n "$PREFIX" ] || usage
[ -x "$PREFIX/bin/darling" ] || { echo "xcodebuild-workloads: --prefix must name a booted Darling prefix (no $PREFIX/bin/darling)" >&2; exit 2; }
[ -x "$XCODE_APP/$XB_REL" ] || { echo "xcodebuild-workloads: no xcodebuild under $XCODE_APP ($XB_REL)" >&2; exit 2; }
case "$WORKLOADS" in minproj|iosapp|both) ;; *) echo "xcodebuild-workloads: --workload takes minproj, iosapp or both" >&2; exit 2 ;; esac

# The prefix maps the host root at /Volumes/SystemRoot (SYSTEM_ROOT in Darling's config), so a host
# path is named to the guest by prefixing that. Paths already inside the guest namespace pass through.
to_guest() {
	case "$1" in
		/Volumes/SystemRoot/*) printf '%s' "$1" ;;
		*) printf '/Volumes/SystemRoot%s' "$1" ;;
	esac
}
guest_xcode=$(to_guest "$XCODE_APP")

[ -n "$LOG_DIR" ] || LOG_DIR="${DW_STATE_ROOT:-${TMPDIR:-/tmp}}/xcodebuild-workloads-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOG_DIR"

echo "prefix:      $PREFIX"
echo "xcode:       $XCODE_APP  (guest: $guest_xcode)"
echo "assets:      $REPO_ROOT/tests/xcodebuild_fixture"
echo "log dir:     $LOG_DIR"
echo

summary=""

case "$WORKLOADS" in
	both) list="minproj iosapp" ;;
	*) list="$WORKLOADS" ;;
esac

for name in $list; do
	case "$name" in
		minproj)
			argv="-project hello.xcodeproj -target hello -configuration Debug build"
			artifact_rel="build/Debug/hello"
			;;
		iosapp)
			argv="-project iosapp.xcodeproj -target iosapp -sdk iphoneos -configuration Debug -destination generic/platform=iOS clean build"
			artifact_rel="build/Debug-iphoneos/iosapp.app"
			;;
	esac

	src="$REPO_ROOT/tests/xcodebuild_fixture/$name"
	guest_dir="/private/var/tmp/xcodebuild-$name"
	host_dir="$PREFIX/private/var/tmp/xcodebuild-$name"
	guest_out="/private/var/tmp/xcodebuild-$name.out"
	host_out="$PREFIX/private/var/tmp/xcodebuild-$name.out"
	probe_host="$PREFIX/private/var/tmp/xcodebuild-$name.sh"
	log="$LOG_DIR/$name.log"

	[ -d "$src" ] || { echo "xcodebuild-workloads: missing asset tree $src" >&2; exit 2; }

	# Install the project into the prefix's persistent guest directory. Wipe first: a stale build
	# directory would let an earlier run's product be read as this run's artifact.
	rm -rf "$host_dir"
	mkdir -p "$host_dir"
	cp -a "$src/." "$host_dir/"
	rm -f "$host_out"

	# The probe carries the exact argv above; it prints the workload's own status as XCB_RC=<n>.
	cat >"$probe_host" <<PROBE
#!/bin/bash
# Generated by scripts/xcodebuild-workload-run.sh; the argv below is the recorded one.
export DEVELOPER_DIR="$guest_xcode/Contents/Developer"
cd "$guest_dir" || { echo "XCB_RC=9"; exit 0; }
: > "$guest_out"
"$guest_xcode/$XB_REL" $argv >>"$guest_out" 2>&1
rc=\$?
echo "XCB_RC=\$rc"
echo "XCB_PROBE_DONE=1"
echo "XCB_RC=\$rc" >>"$guest_out"
exit 0
PROBE
	chmod +x "$probe_host"

	echo "=== workload $name ==="
	echo "command (guest cwd $guest_dir):"
	echo "  \"$guest_xcode/$XB_REL\" $argv"
	echo "probe: /private/var/tmp/$(basename "$probe_host")  output: $guest_out"
	echo "running through $BOOT_RUN ..."

	# --marker replaces the harness's legacy default marker pair (HELLO=1/FINAL=1, meant for the
	# original fixture); without it a passing build would still be reported FAIL.
	harness_out=$(bash "$BOOT_RUN" --prefix "$PREFIX" --wait "$WAIT" \
		--poll 'XCB_RC=' --status-marker 'XCB_RC=' --marker 'XCB_PROBE_DONE=1' --log "$log" \
		--cmd "/bin/bash /private/var/tmp/$(basename "$probe_host")" 2>&1)
	printf '%s\n' "$harness_out" | sed 's/^/  /'

	verdict=$(printf '%s\n' "$harness_out" | grep -a -m1 '^VERDICT:' || true)
	guest_rc=$(grep -ao 'XCB_RC=[0-9]*' "$log" 2>/dev/null | tail -1 || true)
	[ -n "$guest_rc" ] || guest_rc="XCB_RC=absent"

	# An artifact that exists is THIS run's product only because the build directory was wiped above.
	artifact="$host_dir/$artifact_rel"
	artifact_line="none"
	if [ -f "$artifact" ]; then
		artifact_line="file $artifact bytes=$(stat -c %s "$artifact")"
	elif [ -d "$artifact" ]; then
		inner="$artifact/$(basename "$artifact" .app)"
		artifact_line="dir $artifact bytes=$(du -sb "$artifact" | cut -f1)"
		[ -f "$inner" ] && artifact_line="$artifact_line executable=$inner bytes=$(stat -c %s "$inner")"
	fi

	# The loader stop, when the workload stopped in dyld rather than in the build system. The
	# workload's own stdout/stderr is in its .out file (host-visible); the harness log carries the
	# launcher trace and the probe's status lines.  TWO forms reach the log, and matching only one
	# reported "loader stop: absent" for a run that died in dyld (measured 2026-10-12): a missing
	# IMAGE prints "Library not loaded: <path>", while a missing SYMBOL inside a dlopened image prints
	# "Error loading required libraries ... (dlopen(@rpath/libxcodebuildLoader.dylib, 1): Symbol not
	# found: <symbol> ... Expected in: <image>)".
	stop=""
	stop_src=""
	for candidate in "$host_out" "$log"; do
		stop=$(grep -a -m1 -A3 -e 'Library not loaded' -e 'Symbol not found' "$candidate" 2>/dev/null || true)
		if [ -n "$stop" ]; then
			stop_src="$candidate"
			break
		fi
	done

	echo "  guest exit code:   $guest_rc"
	echo "  harness verdict:   ${verdict:-VERDICT: absent}"
	echo "  artifact:          $artifact_line"
	if [ -n "$stop" ]; then
		echo "  loader stop (from $stop_src):"
		printf '%s\n' "$stop"
	fi
	summary="$summary$name guest_rc=${guest_rc#XCB_RC=} harness=${verdict:-absent} artifact=$artifact_line
"
	echo
done

echo "== summary =="
printf '%s' "$summary"
fail_artifacts=0
case "$summary" in
	*"artifact=none"*) fail_artifacts=1 ;;
esac
if [ "$fail_artifacts" = 1 ]; then
	echo "RESULT: at least one workload produced no artifact (the loader stop above is the fact; this is not a harness bug)"
	exit 1
fi
echo "RESULT: every workload produced its artifact"
exit 0

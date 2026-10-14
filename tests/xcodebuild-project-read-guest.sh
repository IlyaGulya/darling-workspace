#!/bin/bash
# Guest driver for tests/xcodebuild_project_read_guest.c -- the RED/GREEN arms of the xcodebuild
# project-read oracle.
#
# It compiles the fixture INSIDE the guest with the workspace's canonical Xcode toolchain (the prefix
# carries no guest CommandLineTools), reading the fixture straight out of the repository through the
# /Volumes/SystemRoot host-root mount -- no copy, so the compiled-from bytes are the committed ones.
# It then runs it with the DVTFoundation the ladder actually loads as argv[1] and the project
# directory the ladder actually builds as argv[2], so the layers are measured on the deployed runtime
# under the ladder's own paths.
#
# STREAMS. The deployed runtime carries in-path instruments that write to fd 2 (the loader's
# `[open-path ...]` and friends), and they are far more voluminous than the fixture's own lines. The
# fixture's stdout therefore goes to its OWN file and its stderr to another, so a reader of the
# oracle's verdict cannot be fed the loader's noise and vice versa; the driver then re-prints the
# fixture's stdout on its own stdout, so the harness log carries the verdict too.
#
# Run through the sanctioned one-run harness, which owns the clean start and the cleanup:
#   bash scripts/darling-boot-run.sh --prefix <PREFIX> --wait 300 \
#     --poll 'XPR_DRIVER_RC=' --status-marker 'XPR_DRIVER_RC=' --marker 'XPR_DRIVER_DONE=1' \
#     --log <LOG> --cmd '/bin/bash /Volumes/SystemRoot/<repo>/tests/xcodebuild-project-read-guest.sh'
#
# The driver does not install the project: the workload probe installs it (the same rm -rf + cp -a
# scripts/xcodebuild-workload-run.sh performs), so the bytes the oracle reads are the ones the
# workload in the same run builds. A missing project is reported as such, never skipped silently.
set -u
REPO=/Volumes/SystemRoot/home/ilyagulya/work/wr-fresh/darling-workspace
DEV=/Volumes/SystemRoot/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents/Developer
DVT=/Volumes/SystemRoot/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents/SharedFrameworks/DVTFoundation.framework/Versions/A/DVTFoundation
XC="$DEV/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"
SDK="$DEV/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
SRC="$REPO/tests/xcodebuild_project_read_guest.c"
BIN=/private/var/tmp/xcodebuild_project_read_guest
OUT=/private/var/tmp/xcodebuild-project-read-fixture.out
ERR=/private/var/tmp/xcodebuild-project-read-fixture.stderr
PROJ=/private/var/tmp/xcodebuild-iosapp/iosapp.xcodeproj
export DEVELOPER_DIR="$DEV"

echo "XPR-DRIVER src=$SRC proj=$PROJ"
cd /private/var/tmp || { echo "XPR_DRIVER_CD_FAIL"; exit 0; }

# The guest's own view of the project tree first, with the guest's own tools: a file that the shell
# cannot see cannot be read by anything above it, and this separates "not there" from "not readable".
echo "XPR-DRIVER ls -la $PROJ"
ls -la "$PROJ" | sed 's/^/XPR-DRIVER ls /'
echo "XPR-DRIVER wc -c $PROJ/project.pbxproj"
wc -c "$PROJ/project.pbxproj" | sed 's/^/XPR-DRIVER wc /'
echo "XPR-DRIVER head -c 40 $PROJ/project.pbxproj"
head -c 40 "$PROJ/project.pbxproj" | sed 's/^/XPR-DRIVER head /'
echo "XPR-DRIVER pwd=$(pwd)"

rm -f "$BIN"
"$XC" -isysroot "$SDK" -std=gnu11 -Wall -Wextra -Werror -O0 -o "$BIN" "$SRC" \
	> /private/var/tmp/xcodebuild_project_read_guest.compile.log 2>&1
echo "XPR_CC_RC=$?"
if [ ! -x "$BIN" ]; then
	echo "XPR_BUILD_FAILED=1"
	cat /private/var/tmp/xcodebuild_project_read_guest.compile.log
	echo "XPR_DRIVER_DONE=1"
	exit 0
fi

"$BIN" "$DVT" "$PROJ" >"$OUT" 2>"$ERR"
echo "XPR_RUN_RC=$?"
cat "$OUT"
echo "XPR_DRIVER_RC=0"
echo "XPR_DRIVER_DONE=1"

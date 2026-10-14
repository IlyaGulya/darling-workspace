#!/bin/bash
# Guest driver for tests/dvtplatform_scan_guest.c -- the RED/GREEN arms of the DVTPlatform platform
# scan oracle.
#
# It compiles the fixture INSIDE the guest with the workspace's canonical Xcode toolchain
# (prefix-verify14 carries no guest CommandLineTools), reading the fixture straight out of the
# repository through the /Volumes/SystemRoot host-root mount -- no copy, so the compiled-from bytes
# are the committed ones. It then runs it with the DVTFoundation the ladder actually loads as
# argv[1], with DEVELOPER_DIR set exactly the way scripts/xcodebuild-workload-run.sh sets it, so the
# scan root the tool computes is measured under the ladder's own environment.
#
# Run through the sanctioned one-run harness, which owns the clean start and the cleanup:
#   bash scripts/darling-boot-run.sh --prefix /home/ilyagulya/work/wr-fresh/prefix-verify14 --wait 300 \
#     --poll 'DVTP_DRIVER_DONE=' --status-marker 'DVTP_DRIVER_RC=' --marker 'DVTP_DRIVER_DONE=1' \
#     --log <LOG> --cmd '/bin/bash /Volumes/SystemRoot/home/ilyagulya/work/wr-fresh/darling-workspace/tests/dvtplatform-scan-guest.sh'
#
# The declared harness RED arm cannot select on this prefix today (the whole-profile applicability
# plan is evaluated before --test selects a row and requires the guest CLT this prefix lacks), so the
# raw arms are this driver on the deployed runtime -- say so when citing them.
set -u
REPO=/Volumes/SystemRoot/home/ilyagulya/work/wr-fresh/darling-workspace
DEV=/Volumes/SystemRoot/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents/Developer
DVT=/Volumes/SystemRoot/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents/SharedFrameworks/DVTFoundation.framework/Versions/A/DVTFoundation
XC="$DEV/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"
SDK="$DEV/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
SRC="$REPO/tests/dvtplatform_scan_guest.c"
BIN=/private/var/tmp/dvtplatform_scan_guest
export DEVELOPER_DIR="$DEV"
cd /private/var/tmp || { echo "DVTP_DRIVER_CD_FAIL"; exit 0; }
rm -f "$BIN"
"$XC" -isysroot "$SDK" -std=gnu11 -Wall -Wextra -Werror -O0 -o "$BIN" "$SRC" > /private/var/tmp/dvtplatform_scan_guest.compile.log 2>&1
echo "DVTP_CC_RC=$?"
if [ ! -x "$BIN" ]; then
	echo "DVTP_BUILD_FAILED=1"
	cat /private/var/tmp/dvtplatform_scan_guest.compile.log
	echo "DVTP_DRIVER_DONE=1"
	exit 0
fi
"$BIN" "$DVT"
echo "DVTP_RUN_RC=$?"
echo "DVTP_DRIVER_RC=0"
echo "DVTP_DRIVER_DONE=1"

#!/usr/bin/env bash
# Reproduce a committed transport candidate through the normal product installation.
# The original R1 baseline remains an explicit comparison input, not a deployment recipe.
# Dirty overlays, profile replay, partial artifact copies and historical source trees are not used.
set -euo pipefail

WS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TOP="$(cd -- "$WS/.." && pwd -P)"
BASELINE=5f2d7401d878455cf3c3c0865ee5a4290dfa03f0
SOURCE=
REVISION=
EXECUTOR=
RUN=
JOBS=8
WORKLOADS=()

usage() {
	cat <<'USAGE'
usage: r1-clean-base-reproduction.sh --source REPOSITORY --revision COMMIT
       --executor REAL_DEBUG_RUNNER --workload NAME [--workload NAME ...]
       [--baseline COMMIT] [--state-dir NEW_DIRECTORY] [--jobs N]

Reproduce only committed source, with exact nested gitlinks prepared by the supported
Darling source-worktree owner. The original baseline is 5f2d7401d878455cf3c3c0865ee5a4290dfa03f0;
--baseline selects a different explicit comparison commit. Neither baseline diffs nor
dirty working-tree bytes are applied to the candidate.

The transport experiment explicitly enables the paired ring switches and malloc-region
diagnostic tool; all targets and runtime files otherwise use normal Release/stock CMake
build/install rules. --workload selects the real structured runtime oracle, not a source
recipe. For example, select comparison_on_ring_mode and comparison_on_ring_fd_lifetime.
Both need the real diagnostic executor; guest C uses genuinely provisioned CLT.

State defaults to <West root>/.west-state/r1-reproduction, or the absolute DW_STATE_ROOT.
Each invocation creates a fresh source worktree, build, immutable runtime installation
and independent short guest prefix. Existing installations and prefixes are never reused
or overwritten. Source/build/install inputs, full per-phase stdout/stderr and verdicts
are retained; the final lifecycle cleanup must succeed before rc=0 is reported.

Run long invocations through scripts/west-job.sh start/follow. Child phase logs register
with that supervisor automatically. No source worktree, installation or guest-state tree
is deleted by this command; retain its printed paths for supported cleanup afterward.
USAGE
}

die() {
	printf 'r1 reproduction: %s\n' "$*" >&2
	exit 2
}

while (($#)); do
	case "$1" in
		--source|--revision|--baseline|--executor|--state-dir|--workload|--jobs)
			(($# >= 2)) || die "missing value for $1"
			case "$1" in
				--source) SOURCE="$2" ;;
				--revision) REVISION="$2" ;;
				--baseline) BASELINE="$2" ;;
				--executor) EXECUTOR="$2" ;;
				--state-dir) RUN="$2" ;;
				--workload) WORKLOADS+=("$2") ;;
				--jobs) JOBS="$2" ;;
			esac
			shift 2
			;;
		--help|-h) usage; exit 0 ;;
		*) die "unknown argument: $1" ;;
	esac
done
[[ -n "$SOURCE" && -n "$REVISION" && -n "$EXECUTOR" ]] || {
	usage >&2
	die '--source, --revision and --executor are required'
}
((${#WORKLOADS[@]})) || die 'select the actual completed oracle with --workload'
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die '--jobs must be a positive integer'
SOURCE="$(realpath -e -- "$SOURCE")"
EXECUTOR="$(realpath -e -- "$EXECUTOR")"
[[ -x "$EXECUTOR" ]] || die "diagnostic executor is not executable: $EXECUTOR"
[[ -z "$(git -C "$SOURCE" status --porcelain --untracked-files=all)" ]] ||
	die 'source repository must be clean; preserve/commit the candidate before reproducing it'
REVISION="$(git -C "$SOURCE" rev-parse --verify --end-of-options "$REVISION^{commit}")"
BASELINE="$(git -C "$SOURCE" rev-parse --verify --end-of-options "$BASELINE^{commit}")"

export DW_STATE_ROOT="${DW_STATE_ROOT:-$TOP/.west-state/r1-reproduction}"
[[ "$DW_STATE_ROOT" == /* ]] || die 'DW_STATE_ROOT must be absolute'
DW_STATE_ROOT="$(realpath -m -- "$DW_STATE_ROOT")"
case "$DW_STATE_ROOT" in
	/|"$HOME"|"$WS"|"$WS"/*|"$SOURCE"|"$SOURCE"/*|/tmp|/tmp/*|/var/tmp|/var/tmp/*)
		die "unsafe DW_STATE_ROOT: $DW_STATE_ROOT" ;;
esac
mkdir -p -- "$DW_STATE_ROOT"
if [[ -n "$RUN" ]]; then
	[[ "$RUN" == /* ]] || die '--state-dir must be absolute'
	RUN="$(realpath -m -- "$RUN")"
	case "$RUN" in "$DW_STATE_ROOT"/*) ;; *) die '--state-dir must be inside DW_STATE_ROOT' ;; esac
	[[ ! -e "$RUN" && ! -L "$RUN" ]] || die "state directory already exists: $RUN"
	mkdir -- "$RUN"
else
	RUN="$(mktemp -d -- "$DW_STATE_ROOT/r1.XXXXXX")"
fi
EVID="$RUN/evidence"
CLEAN="$RUN/source"
BUILD="$RUN/build"
RUNTIME="$RUN/runtime"
PREFIX=
PREFIX_ENTERED=0
WEST=(mise -C "$WS" run west)
mkdir -- "$EVID" "$RUN/tmp"
export TMPDIR="$RUN/tmp"
export WEST_TEST_VERDICT_CACHE=off
export ROOTLESS_TIER_REPO="$WS"
. "$WS/ci/rootless-prefix.sh"

run_step() {
	local name="$1" rc
	shift
	printf '%q ' "$@" >"$EVID/$name.command"
	printf '\n' >>"$EVID/$name.command"
	printf 'r1 phase start: %s (stdout=%s stderr=%s)\n' \
		"$name" "$EVID/$name.stdout" "$EVID/$name.stderr"
	if [ -n "${WEST_JOB_STATE_DIR:-}" ]; then
		"$WS/scripts/west-job.sh" register --state-dir "$WEST_JOB_STATE_DIR" -- \
			file "$EVID/$name.stdout" file "$EVID/$name.stderr"
	fi
	if "$@" >"$EVID/$name.stdout" 2>"$EVID/$name.stderr"; then
		rc=0
	else
		rc=$?
	fi
	printf '%s\n' "$rc" >"$EVID/$name.rc"
	printf 'r1 phase complete: %s rc=%s\n' "$name" "$rc"
	return "$rc"
}

finish() {
	local rc=$? cleanup_rc=0 seal_rc=0
	trap - EXIT INT TERM
	set +e
	if ((PREFIX_ENTERED)); then
		rootless_prefix_assert_owned r1-reproduction "$PREFIX"
		cleanup_rc=$?
		if ((cleanup_rc == 0)); then
			run_step cleanup "${WEST[@]}" test --runtime-root "$RUNTIME" \
				--prefix "$PREFIX" --cleanup-prefix
			cleanup_rc=$?
		fi
		((cleanup_rc == 0)) || { ((rc != 0)) || rc="$cleanup_rc"; }
	else
		printf 'NOT_LAUNCHED\n' >"$EVID/cleanup.status"
	fi
	printf '%s\n' "$cleanup_rc" >"$EVID/cleanup.rc"
	printf '%s\n' "$rc" >"$EVID/execution.rc"
	python3 -B "$WS/scripts/darling-evidence.py" verify "$EVID" --recursive --write-manifest
	seal_rc=$?
	printf '%s\n' "$seal_rc" >"$RUN/seal.rc"
	((seal_rc == 0)) || { ((rc != 0)) || rc="$seal_rc"; }
	printf '%s\n' "$rc" >"$RUN/final.rc"
	printf 'CLEAN-BASE-DONE rc=%s source=%s build=%s runtime-root=%s prefix=%s evidence=%s\n' \
		"$rc" "$CLEAN" "$BUILD" "$RUNTIME" "${PREFIX:-(not allocated)}" "$EVID"
	exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Use the CI owner's compact, independently owned prefix allocation, not a runtime-root alias.
R1_PREFIX_REQUEST=
PREFIX="$(rootless_prefix_create r1-reproduction R1_PREFIX_REQUEST)"
python3 -B - "$EVID" "$SOURCE" "$REVISION" "$BASELINE" "$CLEAN" "$BUILD" \
	"$RUNTIME" "$PREFIX" "$EXECUTOR" "${WORKLOADS[@]}" <<'PY'
import json
from pathlib import Path
import sys

evidence = Path(sys.argv[1])
record = dict(zip(("source_repository", "revision", "comparison_baseline", "source_root",
                   "build_dir", "runtime_root", "guest_prefix", "executor"), sys.argv[2:10]))
record["workloads"] = sys.argv[10:]
with (evidence / "selection.json").open("x", encoding="utf-8") as output:
    json.dump(record, output, indent=2, sort_keys=True)
    output.write("\n")
PY

# The baseline diff is review evidence only. All build inputs come from the selected commit.
git -C "$SOURCE" diff --binary --submodule=log "$BASELINE" "$REVISION" >"$EVID/baseline-to-candidate.diff"
run_step source-create git -C "$SOURCE" worktree add --detach "$CLEAN" "$REVISION"
run_step source-prepare "${WEST[@]}" darling-source-worktree prepare \
	--source "$CLEAN" --canonical "$SOURCE" --record "$EVID/source-record.json"
run_step configure cmake -S "$CLEAN" -B "$BUILD" -G Ninja \
	-DCMAKE_BUILD_TYPE=Release -DCOMPONENTS=stock -DCMAKE_INSTALL_PREFIX="$RUNTIME" \
	-DCMAKE_C_COMPILER="${CC:-clang}" -DCMAKE_CXX_COMPILER="${CXX:-clang++}" \
	-DDARLING_NO_CCACHE=ON -DDARLING_RING_TRANSPORT=ON -DDSERVER_RING_TRANSPORT=ON \
	-DDARLING_MALLOC_REGION_TEST=ON
cp -- "$BUILD/CMakeCache.txt" "$EVID/CMakeCache.txt"
run_step source-check "${WEST[@]}" darling-source-worktree check \
	--record "$EVID/source-record.json" --build-dir "$BUILD"
run_step build-install "${WEST[@]}" darling-build --build-dir "$BUILD" \
	--runtime-root "$RUNTIME" --install --jobs "$JOBS" --json
run_step installation-record python3 -B - "$WS/west_commands" "$RUNTIME" \
	"$EVID/install-receipt.json" <<'PY'
from pathlib import Path
import shutil
import sys
sys.path.insert(0, sys.argv[1])
from deploy_receipt import receipt_path
from runtime_install import resolve_runtime
runtime = resolve_runtime(sys.argv[2])
if runtime.identity is None:
    raise SystemExit("normal build/install did not publish an installation identity")
shutil.copyfile(receipt_path(runtime.root), Path(sys.argv[3]))
PY

PREFIX_ENTERED=1
run_step bootstrap "${WEST[@]}" darling-bootstrap --build-dir "$BUILD" \
	--runtime-root "$RUNTIME" --prefix "$PREFIX" --no-build \
	--guest-toolchain darling-command-line-tools --json
for index in "${!WORKLOADS[@]}"; do
	run_step "workload-$index" "${WEST[@]}" test --workload "${WORKLOADS[$index]}" \
		--env darling --source-root "$CLEAN" --runtime-root "$RUNTIME" --prefix "$PREFIX" \
		--executor "$EXECUTOR" --bundle-root "$EVID/bundles" \
		--proof-scratch-root "$RUN/scratch" --runtime-evidence-root "$EVID/runtime"
done

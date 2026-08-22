#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:?usage: run-lifecycle-fuzz-campaign.sh asan|tsan|fuzz}"
case "$mode" in
	asan|tsan|fuzz) ;;
	*) printf 'unknown lifecycle fuzz campaign: %s\n' "$mode" >&2; exit 2 ;;
esac

task_root="${LIFECYCLE_LAB_TASK_ROOT:?LIFECYCLE_LAB_TASK_ROOT is required}"
artifact_staging="${LIFECYCLE_LAB_ARTIFACT_STAGING:?LIFECYCLE_LAB_ARTIFACT_STAGING is required}"
corpus="$task_root/corpus-$mode"
target="$task_root/target-$mode"
fuzz_artifacts="$task_root/fuzz-artifacts-$mode"
source_artifacts="$repo/lifecycle/operation-boundary/fuzz/artifacts"
campaign_tmp="$task_root/tmp"
if [[ -e "$source_artifacts" ]]; then
	printf 'LIFECYCLE_%s_CAMPAIGN=FAIL source-artifact-root-preexists\n' "${mode^^}"
	exit 2
fi
cleanup_source_artifacts() {
	if [[ -d "$source_artifacts" ]]; then
		rm -rf -- "$source_artifacts"
	fi
}
trap cleanup_source_artifacts EXIT
mkdir -p -- "$artifact_staging" "$corpus" "$fuzz_artifacts" "$campaign_tmp"
export TMPDIR="$campaign_tmp"

if ! command -v cargo-fuzz >/dev/null 2>&1; then
	printf 'LIFECYCLE_%s_CAMPAIGN=UNSUPPORTED cargo-fuzz-unavailable\n' "${mode^^}"
	exit 2
fi
if ! rustup toolchain list | grep -q '^nightly'; then
	printf 'LIFECYCLE_%s_CAMPAIGN=UNSUPPORTED nightly-unavailable\n' "${mode^^}"
	exit 2
fi

env CARGO_NET_OFFLINE=true cargo build --manifest-path "$repo/lifecycle/operation-boundary/Cargo.toml" \
	--bin lifecycle-fuzz --target-dir "$task_root/producer-target" >/dev/null
"$task_root/producer-target/debug/lifecycle-fuzz" --materialize-corpus "$corpus"
corpus_count="$(find "$corpus" -maxdepth 1 -type f -printf x | wc -c)"
if (( corpus_count > ${LIFECYCLE_LAB_MAX_CORPUS:?} )); then
	printf 'LIFECYCLE_%s_CAMPAIGN=FAIL corpus-budget-exceeded\n' "${mode^^}"
	exit 2
fi
find "$corpus" -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort >"$artifact_staging/corpus-seeds.txt"
tar -cf "$artifact_staging/corpus.tar" -C "$corpus" .
printf '{"schema_version":1,"inputs":[]}\n' >"$artifact_staging/minimized-inputs.json"

common=(run lifecycle_fuzz --target-dir "$target" "$corpus" -- \
	-runs=512 -max_total_time=60 -max_len=4096 -rss_limit_mb=2048 -timeout=5 \
	-artifact_prefix="$fuzz_artifacts/")
cd "$repo/lifecycle/operation-boundary"
case "$mode" in
	asan)
		RUSTUP_TOOLCHAIN=nightly ASAN_OPTIONS='symbolize=0:detect_odr_violation=0' \
			cargo fuzz "${common[@]:0:2}" --sanitizer address "${common[@]:2}" || campaign_rc=$?
		;;
	tsan)
		RUSTUP_TOOLCHAIN=nightly TSAN_OPTIONS='report_signal_unsafe=0:symbolize=0' \
			cargo fuzz "${common[@]:0:2}" --sanitizer thread --build-std "${common[@]:2}" || campaign_rc=$?
		;;
	fuzz)
		RUSTUP_TOOLCHAIN=nightly cargo fuzz "${common[@]:0:2}" --sanitizer none "${common[@]:2}" || campaign_rc=$?
		;;
esac

campaign_rc="${campaign_rc:-0}"
find "$fuzz_artifacts" -maxdepth 1 -type f -print0 | while IFS= read -r -d '' artifact; do
	base="$(basename "$artifact")"
	cp -- "$artifact" "$artifact_staging/minimized-$base"
done
if [[ -d "$source_artifacts" ]]; then
	find "$source_artifacts" -type f -print0 | while IFS= read -r -d '' artifact; do
		base="$(basename "$artifact")"
		cp -- "$artifact" "$artifact_staging/minimized-$base"
	done
fi
find "$fuzz_artifacts" -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort >"$artifact_staging/minimized-inputs.txt"
if (( campaign_rc != 0 )); then
	if [[ -s "$artifact_staging/minimized-inputs.txt" ]]; then
		printf 'LIFECYCLE_%s_CAMPAIGN=FORENSIC rc=%s\n' "${mode^^}" "$campaign_rc"
	else
		printf 'LIFECYCLE_%s_CAMPAIGN=FAIL rc=%s\n' "${mode^^}" "$campaign_rc"
	fi
	exit "$campaign_rc"
fi
printf 'LIFECYCLE_%s_CAMPAIGN=PASS\n' "${mode^^}"

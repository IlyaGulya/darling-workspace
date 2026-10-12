#!/usr/bin/env bash
# Host contract for the vecLib/BNNS random generator.
#
# Runs tests/bnns_random_generator_kat.c against the guest library's own source file, on the host, and
# asserts the three things a plausible wrong implementation gets wrong: the cipher core (FIPS-197 C.1
# known answer), the interval the fill promises (no draw outside [a,b), with a bad-model arm proving the
# probe can still see an unclamped rounding at b), and the descriptor's failure modes.
#
#   DARLING_SRC_ROOT   Darling source root to read (default: <workspace>/../darling)
#   CC                 C compiler to use (default: clang, then cc, then gcc)
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
darling_root="${DARLING_SRC_ROOT:-$workspace_root/../darling}"
bnns_dir="$darling_root/src/frameworks/Accelerate/vecLib/BNNS"
kat_c="$workspace_root/tests/bnns_random_generator_kat.c"

fail() {
	echo "bnns-random-generator-contract: $*" >&2
	exit 1
}

[ -f "$bnns_dir/src/BNNS.c" ] || fail "no BNNS source at $bnns_dir/src/BNNS.c"
[ -f "$kat_c" ] || fail "no oracle at $kat_c"

cc_bin="${CC:-}"
if [ -z "$cc_bin" ]; then
	for candidate in clang cc gcc; do
		if command -v "$candidate" >/dev/null 2>&1; then
			cc_bin="$candidate"
			break
		fi
	done
fi
[ -n "$cc_bin" ] || fail "no C compiler (set CC)"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/bnns-kat.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

# The oracle includes the library's source file itself, so statics such as the round function and the
# uniform draw are reachable and the bad-model arm can compute the unclamped map from the same keystream.
"$cc_bin" -O1 -Wall -Wno-unused-function \
	-I"$bnns_dir/include" -I"$bnns_dir/src" \
	"$kat_c" -o "$tmp/kat" -lm

out="$("$tmp/kat")" || fail "the oracle exited non-zero:
$out"
echo "$out"

value_of() {
	printf '%s\n' "$out" | sed -n "s/^$1=//p" | head -1
}

kat="$(value_of KAT)"
[ "$kat" = "69c4e0d86a7b0430d8cdb78070b4c55a" ] || fail "FIPS-197 C.1 known answer is $kat, expected 69c4e0d86a7b0430d8cdb78070b4c55a"

unclamped="$(value_of UNCLAMPED_OUTSIDE)"
clamped="$(value_of CLAMPED_OUTSIDE)"
distinct="$(value_of DISTINCT_WIDE)"
mean="$(value_of MEAN)"
a_eq_b="$(value_of A_EQ_B_EXACT)"
bad_desc="$(value_of BAD_DESCRIPTOR_FAILS)"

# The bad model must still be able to see the defect, or the good-model assertion below proves nothing.
[ "${unclamped:-0}" -ge 1 ] || fail "the unclamped affine map produced no draw equal to b, so this probe can no longer detect that rounding (got $unclamped)"
[ "${clamped:-1}" -eq 0 ] || fail "the implemented fill produced $clamped draw(s) outside [a,b)"
[ "${distinct:-0}" -ge 4000 ] || fail "only $distinct of 4096 draws on a wide interval were distinct, so the fill is not a keystream-driven draw (a ramp, a constant or a repeated block would do that)"
[ "$a_eq_b" = "yes" ] || fail "a == b did not return exactly that value"
[ "$bad_desc" = "yes" ] || fail "an unsupported data type or a rank-0 layout returned success"

# Loose sanity on the mean: the interval is [1000,1001), so 1000.5 is expected.
awk -v m="$mean" 'BEGIN { if (m < 1000.45 || m > 1000.55) exit 1 }' || fail "mean $mean is not near 1000.5"

echo "PASS bnns-random-generator-contract"

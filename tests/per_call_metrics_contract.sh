#!/usr/bin/env bash
set -euo pipefail

src="${DSERVER_SRC_ROOT:?set DSERVER_SRC_ROOT}"
test -f "$src/tests/per_call_metrics_test.cpp"
test -f "$src/src/metrics.cpp"
test -f "$src/internal-include/darlingserver/metrics.hpp"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/include/darlingserver"

cat > "$tmp/include/darlingserver/rpc.h" <<'H_EOF'
#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DSERVER_CALL_UNMANAGED_FLAG 0x80000000u

typedef enum dserver_callnum {
	dserver_callnum_invalid = 0,
	dserver_callnum_checkin = 1,
	dserver_callnum_mach_msg_overwrite = 2,
	dserver_callnum_vchroot = 3,
} dserver_callnum_t;

static inline const char *dserver_callnum_to_string(dserver_callnum_t callnum) {
	switch ((uint32_t)callnum & ~DSERVER_CALL_UNMANAGED_FLAG) {
	case dserver_callnum_checkin:
		return "checkin";
	case dserver_callnum_mach_msg_overwrite:
		return "mach_msg_overwrite";
	case dserver_callnum_vchroot:
		return "vchroot";
	default:
		return 0;
	}
}

#ifdef __cplusplus
}
#endif
H_EOF

cat > "$tmp/include/darlingserver/rpc-supplement.h" <<'H_EOF'
#pragma once
#include <stdint.h>
#define DSERVER_RING_CLASS_SIMPLE_C2S 0x1u
#define DSERVER_RING_CLASS_NOFIBER_FAST 0x2u
#define DSERVER_RING_CLASS_DESTROY 0x4u
#define DSERVER_RING_CLASS_CALLER_S2C 0x8u
static inline uint32_t dserver_ring_op_class(uint32_t callnum) {
	(void)callnum;
	return 0;
}
H_EOF

cxx="${CXX:-c++}"
"$cxx" -std=c++17 -w \
	-I "$tmp/include" \
	-I "$src/include" \
	-I "$src/internal-include" \
	-o "$tmp/per-call-metrics" \
	"$src/tests/per_call_metrics_test.cpp" \
	"$src/src/metrics.cpp"

"$tmp/per-call-metrics"

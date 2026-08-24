#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DARLINGSERVER_ROOT=${DARLING_LIFECYCLE_DARLINGSERVER_ROOT:-"$ROOT/../darlingserver"}
TASK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dar-awrp-cohort-host.XXXXXX")
trap 'rm -rf -- "$TASK_ROOT"' EXIT

clang -std=c11 -D_GNU_SOURCE -Wall -Wextra -Werror \
	-I"$ROOT/../darling/src/startup" \
	-c "$ROOT/../darling/src/startup/rootless_shutdown.c" \
	-o "$TASK_ROOT/rootless_shutdown.o"

clang++ -std=c++17 -Wall -Wextra -Werror -pthread \
	-I"$ROOT/lifecycle/operation-boundary/include" \
	-I"$DARLINGSERVER_ROOT/internal-include" \
	"$ROOT/tests/lifecycle_product_cohort_host.cpp" \
	"$DARLINGSERVER_ROOT/src/lifecycle-bootstrap.cpp" \
	"$DARLINGSERVER_ROOT/src/rootless-session-drain.cpp" \
	"$TASK_ROOT/rootless_shutdown.o" \
	-o "$TASK_ROOT/lifecycle-product-cohort-host"

"$TASK_ROOT/lifecycle-product-cohort-host"

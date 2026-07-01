#!/usr/bin/env bash
# Drive the E-UNION route-2 verification inside a DEFAULT unprivileged docker
# container (dar-test-infra-sp5.8.4.4). No --privileged, no --cap-add, no
# --device /dev/fuse, no userns remap -- the whole point is that E-UNION works
# with none of them.
#
# Usage: run-in-docker.sh <darling-install-tree>
#   <darling-install-tree> = a host dir containing bin/darling + libexec/darling
#                            built with -DDARLING_EUNION=ON and installed with
#                            CMAKE_INSTALL_PREFIX=/usr/local (so paths match the
#                            baked-in EUNION_LIBEXEC_PATH=/usr/local/libexec/darling).
set -euo pipefail

INSTALL="${1:?usage: run-in-docker.sh <install-tree>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="ubuntu:24.04"

[ -x "$INSTALL/bin/darling" ] || { echo "no $INSTALL/bin/darling"; exit 1; }
[ -d "$INSTALL/libexec/darling" ] || { echo "no $INSTALL/libexec/darling"; exit 1; }

echo "== launching default unprivileged container ($IMAGE) =="
echo "   install tree: $INSTALL  (mounted read-only at /usr/local)"

# Deliberately minimal flags: only the bind mounts. No caps, no devices, no
# privileged, no security-opt relaxation, default seccomp + default userns.
exec docker run --rm \
  -v "$INSTALL:/usr/local:ro" \
  -v "$HERE/verify-route2.sh:/verify-route2.sh:ro" \
  -e DARLING_INSTALL=/usr/local \
  "$IMAGE" \
  bash /verify-route2.sh

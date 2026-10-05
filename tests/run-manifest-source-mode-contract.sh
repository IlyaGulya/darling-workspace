#!/usr/bin/env bash
# Manifest source mode: proves a manifest-native provider needs no patch machinery, fails before the
# build when the workspace is not the manifest's product source, shares the build/deploy half with the
# legacy providers, and records an identity the Ring oracle can refuse on.
set -euo pipefail

cd "$(dirname "$0")/.."
exec env PYTHONDONTWRITEBYTECODE=1 python3 tests/west_test_contracts/manifest_source_mode_contract.py

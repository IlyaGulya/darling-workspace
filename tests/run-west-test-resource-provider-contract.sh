#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import sys

sys.path.insert(0, "west_commands")
from test_resources import active_resource_provider_names

assert active_resource_provider_names({}) == []
assert active_resource_provider_names({"requires_resources": ["darling-prefix"]}) == []
assert active_resource_provider_names({"dcc_cache": {"source-ref": "HEAD"}}) == ["dcc-cache"]
assert active_resource_provider_names({
    "requires_resources": ["darling-eunion-prefix"],
}) == ["darling-eunion-prefix"]
assert active_resource_provider_names({
    "requires_resources": ["unknown-resource", "darling-eunion-prefix"],
    "dcc_cache": {"source-ref": "HEAD"},
}) == ["dcc-cache", "darling-eunion-prefix"]
PY

printf 'PASS west-test-resource-provider-contract\n'

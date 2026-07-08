#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import tempfile
import sys
import types
from pathlib import Path

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.test import DarlingTest


def make_test():
    test = DarlingTest.__new__(DarlingTest)
    test.inf = lambda *args, **kwargs: None
    test.wrn = lambda *args, **kwargs: None
    test.err_messages = []
    test.err = lambda message: test.err_messages.append(message)
    return test


test = make_test()
prefix = Path("/tmp/west-test-prefix-contract")
test._prefix = str(prefix)
test._keep_prefix_running = False
test._resolve_darling_launcher = lambda _prefix: None
test._kill_dserver_for_prefix = lambda _prefix: None
test._ps_entries = lambda: [
    (100, 1, f"darlingserver {prefix} 1000 1000 4 0"),
    (101, 100, "/sbin/launchd"),
    (102, 101, "/usr/libexec/shellspawn"),
    (200, 1, "darlingserver /tmp/other-prefix 1000 1000 4 0"),
]

snapshot = test._prefix_process_snapshot(prefix)
assert any(line.startswith("100 darlingserver ") for line in snapshot), snapshot
assert any(line == "101 /sbin/launchd" for line in snapshot), snapshot
assert any(line == "102 /usr/libexec/shellspawn" for line in snapshot), snapshot
assert all("other-prefix" not in line for line in snapshot), snapshot
assert not test._shutdown_test_prefix()
assert any("leftover Darling prefix" in line for line in test.err_messages), test.err_messages

test = make_test()
test._prefix = str(prefix)
test._keep_prefix_running = False
test._resolve_darling_launcher = lambda _prefix: None
test._kill_dserver_for_prefix = lambda _prefix: None
test._ps_entries = lambda: []
assert test._shutdown_test_prefix()

test = make_test()
with tempfile.TemporaryDirectory() as temp:
    test._prefix = temp
    test._shutdown_test_prefix = lambda: False
    with test._prefix_resource_context(True):
        pass
    assert test._prefix_cleanup_failed

print("PASS west-test-prefix-cleanup-contract")
PY

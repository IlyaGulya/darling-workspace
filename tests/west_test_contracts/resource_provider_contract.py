from contextlib import contextmanager
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")
class WestCommand:
    def die(self, message):
        raise SystemExit(message)
west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

sys.path.insert(0, "west_commands")
from test import DarlingTest
from test_resources import active_resource_provider_names, resource_context

assert active_resource_provider_names({}) == []
assert active_resource_provider_names({"requires_resources": ["darling-prefix"]}) == []
assert active_resource_provider_names({"host_trace_files": [{"env": "TRACE"}]}) == ["host-trace-files"]
assert active_resource_provider_names({"host_stat_deltas": [{"path": "rpc.count"}]}) == ["host-stat-deltas"]
assert active_resource_provider_names({"dcc_cache": {"source-ref": "HEAD"}}) == ["dcc-cache"]
assert active_resource_provider_names({
    "requires_resources": ["darling-eunion-prefix"],
}) == ["darling-eunion-prefix"]
assert active_resource_provider_names({
    "requires_resources": ["unknown-resource", "darling-eunion-prefix"],
    "host_trace_files": [{"env": "TRACE"}],
    "host_stat_deltas": [{"path": "rpc.count"}],
    "dcc_cache": {"source-ref": "HEAD"},
}) == ["host-trace-files", "host-stat-deltas", "dcc-cache", "darling-eunion-prefix"]

class Command:
    def __init__(self):
        self.order = []

    @contextmanager
    def _host_trace_context(self, invocation, env):
        self.order.append("host-trace-files")
        merged = dict(env or {})
        merged["TRACE_FROM_PROVIDER"] = "1"
        yield merged

    @contextmanager
    def _dcc_cache_context(self, invocation, env):
        self.order.append("dcc-cache")
        assert env["TRACE_FROM_PROVIDER"] == "1"
        yield

    @contextmanager
    def _host_stat_context(self, invocation, env):
        self.order.append("host-stat-deltas")
        assert env["TRACE_FROM_PROVIDER"] == "1"
        invocation["_host_stat_tool"] = "/tmp/fake-darling-stat"
        yield env

    @contextmanager
    def _eunion_prefix_context(self, invocation, env):
        self.order.append("darling-eunion-prefix")
        assert env["TRACE_FROM_PROVIDER"] == "1"
        yield

command = Command()
invocation = {
    "host_trace_files": [{"env": "TRACE"}],
    "host_stat_deltas": [{"path": "rpc.count"}],
    "dcc_cache": {"source-ref": "HEAD"},
    "requires_resources": ["darling-eunion-prefix"],
}
with resource_context(command, invocation, {}) as env:
    assert env["TRACE_FROM_PROVIDER"] == "1"
assert invocation["_host_stat_tool"] == "/tmp/fake-darling-stat"
assert command.order == ["host-trace-files", "host-stat-deltas", "dcc-cache", "darling-eunion-prefix"]

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    stat_tool = tempdir / "darling-stat"
    stat_tool.write_text("#!/usr/bin/env bash\nprintf '{}\\n'\n")
    stat_tool.chmod(0o755)
    real_command = object.__new__(DarlingTest)
    real_invocation = {
        "name": "host_stat_provider_contract",
        "host_stat_deltas": [{"path": "rpc.count"}],
        "host_stat_tool": str(stat_tool),
    }
    with real_command._host_stat_context(real_invocation, {"DPREFIX": str(tempdir)}) as env:
        assert env["DPREFIX"] == str(tempdir)
    assert real_invocation["_host_stat_tool"] == str(stat_tool)

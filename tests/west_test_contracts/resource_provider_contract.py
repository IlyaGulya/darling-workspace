from contextlib import contextmanager
import shutil
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
assert active_resource_provider_names({"host_temp_files": [{"env": "FAULT"}]}) == ["host-temp-files"]
assert active_resource_provider_names({"host_trace_files": [{"env": "TRACE"}]}) == ["host-trace-files"]
assert active_resource_provider_names({"host_stat_deltas": [{"path": "rpc.count"}]}) == ["host-stat-deltas"]
assert active_resource_provider_names({
    "descriptor_trace": {"windows": [{"id": "w", "expect-descriptor-messages": "none"}]},
}) == ["descriptor-trace"]
assert active_resource_provider_names({"dcc_cache": {"source-ref": "HEAD"}}) == ["dcc-cache"]
assert active_resource_provider_names({
    "requires_resources": ["darling-eunion-prefix"],
}) == ["darling-eunion-prefix"]
assert active_resource_provider_names({
    "requires_resources": ["unknown-resource", "darling-eunion-prefix"],
    "host_temp_files": [{"env": "FAULT"}],
    "host_trace_files": [{"env": "TRACE"}],
    "host_stat_deltas": [{"path": "rpc.count"}],
    "descriptor_trace": {"windows": [{"id": "w", "expect-descriptor-messages": "none"}]},
    "dcc_cache": {"source-ref": "HEAD"},
}) == ["host-temp-files", "host-trace-files", "host-stat-deltas", "descriptor-trace", "dcc-cache", "darling-eunion-prefix"]

class Command:
    def __init__(self):
        self.order = []

    @contextmanager
    def _host_trace_context(self, invocation, env):
        self.order.append("host-trace-files")
        assert env["FAULT_FROM_PROVIDER"] == "1"
        merged = dict(env or {})
        merged["TRACE_FROM_PROVIDER"] = "1"
        yield merged

    @contextmanager
    def _host_temp_context(self, invocation, env):
        self.order.append("host-temp-files")
        merged = dict(env or {})
        merged["FAULT_FROM_PROVIDER"] = "1"
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
    def _descriptor_trace_context(self, invocation, env):
        self.order.append("descriptor-trace")
        assert env["TRACE_FROM_PROVIDER"] == "1"
        merged = dict(env or {})
        merged["DESCRIPTOR_TRACE_FROM_PROVIDER"] = "1"
        yield merged

    @contextmanager
    def _eunion_prefix_context(self, invocation, env):
        self.order.append("darling-eunion-prefix")
        assert env["TRACE_FROM_PROVIDER"] == "1"
        yield

command = Command()
invocation = {
    "host_temp_files": [{"env": "FAULT"}],
    "host_trace_files": [{"env": "TRACE"}],
    "host_stat_deltas": [{"path": "rpc.count"}],
    "descriptor_trace": {"windows": [{"id": "w", "expect-descriptor-messages": "none"}]},
    "dcc_cache": {"source-ref": "HEAD"},
    "requires_resources": ["darling-eunion-prefix"],
}
with resource_context(command, invocation, {}) as env:
    assert env["TRACE_FROM_PROVIDER"] == "1"
    assert env["DESCRIPTOR_TRACE_FROM_PROVIDER"] == "1"
assert invocation["_host_stat_tool"] == "/tmp/fake-darling-stat"
assert command.order == ["host-temp-files", "host-trace-files", "host-stat-deltas", "descriptor-trace", "dcc-cache", "darling-eunion-prefix"]

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    real_command = object.__new__(DarlingTest)
    real_command._prefix = str(tempdir)
    real_invocation = {
        "name": "host_temp_provider_contract",
        "host_temp_files": [
            {
                "env": "DSERVER_TEST_FAULT_FILE",
                "prefix-relative-path": "private/var/tmp/west-fault",
                "contents": "fault.name\n",
            }
        ],
    }
    with real_command._host_temp_context(real_invocation, {"DPREFIX": str(tempdir)}) as env:
        fault_path = Path(env["DSERVER_TEST_FAULT_FILE"])
        assert fault_path.read_text() == "fault.name\n"
    assert not fault_path.exists()

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

with tempfile.TemporaryDirectory() as tmp:
    tempdir = Path(tmp)
    workspace = tempdir / "workspace"
    workspace.mkdir()
    real_command = object.__new__(DarlingTest)
    real_invocation = {
        "name": "vchroot_fdless_path_rpc_guest",
        "cwd": workspace,
        "descriptor_trace": {
            "windows": [
                {"id": "console-open-control", "expect-descriptor-messages": "at-least-one"},
                {"id": "vchroot-fdless-valid", "expect-descriptor-messages": "none"},
            ]
        },
    }
    # The capture needs strace on the host; the contract must not depend on it.
    real_which = shutil.which
    shutil.which = lambda name: "/usr/bin/strace" if name == "strace" else real_which(name)
    try:
        stale = workspace / ".west-test/descriptor-trace/vchroot_fdless_path_rpc_guest"
        stale.mkdir(parents=True)
        (stale / "trace.1").write_text("stale\n")
        with real_command._descriptor_trace_context(real_invocation, {"DPREFIX": str(tempdir)}) as env:
            trace_dir = Path(env["WEST_DESCRIPTOR_TRACE_DIR"])
            assert trace_dir == stale, (trace_dir, stale)
            assert trace_dir.is_dir() and list(trace_dir.iterdir()) == [], trace_dir
            assert str(trace_dir) == env["WEST_DESCRIPTOR_TRACE_DIR"]
        assert real_invocation["_descriptor_trace_dir"] == trace_dir
        assert [spec.window_id for spec in real_invocation["_descriptor_trace_specs"]] == [
            "console-open-control",
            "vchroot-fdless-valid",
        ]

        broken = dict(real_invocation)
        broken["descriptor_trace"] = {"windows": [{"id": "w"}]}
        try:
            with real_command._descriptor_trace_context(broken, {"DPREFIX": str(tempdir)}):
                raise AssertionError("declaration without expectations accepted")
        except SystemExit as error:
            assert "expect-descriptor-messages" in str(error), str(error)

        shutil.which = lambda name: None
        try:
            with real_command._descriptor_trace_context(dict(real_invocation), {}):
                raise AssertionError("capture without strace accepted")
        except SystemExit as error:
            assert "requires strace on the host" in str(error), str(error)
    finally:
        shutil.which = real_which

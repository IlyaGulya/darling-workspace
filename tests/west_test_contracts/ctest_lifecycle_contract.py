"""Ensure CTest guest selections own the Darling prefix lifecycle."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import west_commands.test as test_module
from west_commands.test import DarlingTest
from west_commands.test_execution import ProcessResult

os.environ.setdefault("WEST_RUNTIME_MIN_FREE_BYTES", "0")

debug_test = DarlingTest.__new__(DarlingTest)
debug_test._executor = "/tmp/darling-debug-runner"
debug_args = debug_test._debug_runner_args(
    {
        "name": "cwd_contract",
        "diag": "guarded",
        "cwd": Path("/tmp/workspace-owned-script"),
        "args": ["tests/runtime.sh"],
        "shell": False,
        "timeout_seconds": 7,
    }
)
assert debug_args[-4:] == ["--cwd", "/tmp/workspace-owned-script", "--", "tests/runtime.sh"], debug_args


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    empty_selection_test = DarlingTest.__new__(DarlingTest)
    empty_selection_test.topdir = str(root)
    empty_selection_test._ctest_build = root / "build"
    empty_selection_test._ctest_build.mkdir()
    empty_selection_test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    empty_selection_test._dump_command_tail = lambda *_args: None
    original = test_module.run_bounded
    test_module.run_bounded = lambda *_args, **_kwargs: ProcessResult(
        0, stdout=json.dumps({"tests": []})
    )
    try:
        try:
            empty_selection_test._ctest_label_args({"ctest_label": "missing"})
            raise AssertionError("empty CTest label selection unexpectedly passed")
        except SystemExit as exc:
            assert "refusing a false GREEN" in str(exc), exc
    finally:
        test_module.run_bounded = original


test = DarlingTest.__new__(DarlingTest)
test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
original_disk_usage = test_module.shutil.disk_usage
original_minimum = os.environ["WEST_RUNTIME_MIN_FREE_BYTES"]
test_module.shutil.disk_usage = lambda _path: SimpleNamespace(free=7)
os.environ["WEST_RUNTIME_MIN_FREE_BYTES"] = "8"
try:
    try:
        test._require_runtime_scratch_space("contract")
        raise AssertionError("runtime scratch preflight unexpectedly passed")
    except SystemExit as exc:
        assert "Runtime deployment contract needs at least 8 free bytes" in str(exc), exc
finally:
    test_module.shutil.disk_usage = original_disk_usage
    os.environ["WEST_RUNTIME_MIN_FREE_BYTES"] = original_minimum

os.environ["WEST_RUNTIME_MIN_FREE_BYTES"] = "not-a-byte-count"
try:
    try:
        test._require_runtime_scratch_space("contract")
        raise AssertionError("invalid runtime scratch threshold unexpectedly passed")
    except SystemExit as exc:
        assert str(exc) == (
            "WEST_RUNTIME_MIN_FREE_BYTES must be an integer number of bytes "
            "greater than or equal to 0"
        ), exc
finally:
    os.environ["WEST_RUNTIME_MIN_FREE_BYTES"] = original_minimum


bootstrap_test = DarlingTest.__new__(DarlingTest)
bootstrap_test._prefix = None
bootstrap_test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
try:
    bootstrap_test._bootstrap_runtime_profile("homebrew-prefix-baseline")
    raise AssertionError("bootstrap accepted a missing prefix")
except SystemExit as exc:
    assert str(exc) == (
        "--bootstrap-runtime-profile requires --prefix, --prefix-profile, or DPREFIX "
        "(for example: --prefix-profile homebrew)"
    ), exc


with tempfile.TemporaryDirectory() as temp:
    trace_dir = Path(temp)
    (trace_dir / "bootstrap.101").write_text(
        'execve("/prefix/usr/libexec/shellspawn", ["shellspawn"], []) = 0\n'
        'recvmsg(7, {msg_namelen=0}, MSG_DONTWAIT) = -1 EAGAIN (Resource temporarily unavailable)\n'
    )
    (trace_dir / "bootstrap.102").write_text(
        'execve("/prefix/bin/darling", ["darling"], []) = 0\n'
        'poll([{fd=3, events=POLLIN}], 1, -1\n'
    )
    (trace_dir / "bootstrap.103").write_text(
        'execve("/prefix/usr/libexec/opendirectoryd", ["opendirectoryd"], []) = 0\n'
        'recvmsg(7, {msg_namelen=0}, MSG_DONTWAIT) = -1 EAGAIN (Resource temporarily unavailable)\n'
        '+++ exited with 0 +++\n'
    )
    (trace_dir / "bootstrap.104").write_text(
        'execve("/prefix/sbin/launchd", ["launchd"], []) = 0\n'
        'sendmsg(7, {msg_name={sa_family=AF_UNIX}}, 0) = 20\n'
        'recvmsg(7, {msg_namelen=0}, MSG_DONTWAIT) = 8\n'
        'recvmsg(7, {msg_namelen=0}, MSG_DONTWAIT) = -1 EAGAIN (Resource temporarily unavailable)\n'
    )
    (trace_dir / "bootstrap.105").write_text(
        'execve("/prefix/sbin/iokitd", ["iokitd"], []) = 0\n'
        'sendmsg(7, {msg_name={sa_family=AF_UNIX}}, 0) = 20\n'
        'recvmsg(7, {msg_namelen=0}, MSG_DONTWAIT) = -1 EAGAIN (Resource temporarily unavailable)\n'
    )
    summary = test_module.bootstrap_syscall_stall_summary(trace_dir)
    assert summary == (
        "shellspawn[101]: polling empty RPC receive without a request | "
        "darling[102]: waiting for a socket event | "
        "launchd[104]: polling after a delivered RPC reply | "
        "iokitd[105]: awaiting reply to most recent RPC"
    ), summary


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "prefix"
    prefix.mkdir()
    context_test = DarlingTest.__new__(DarlingTest)
    context_test._prefix = str(prefix)
    context_test._keep_prefix_running = False
    context_test._prefix_cleanup_failed = False
    context_test.inf = lambda _message: None
    shutdowns = []
    context_test._shutdown_test_prefix = lambda: (shutdowns.append("shutdown") or True)
    with context_test._prefix_resource_context(True):
        assert shutdowns == ["shutdown"], shutdowns
    assert shutdowns == ["shutdown", "shutdown"], shutdowns


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    build = root / "build"
    build.mkdir()
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.wrn = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._resolve_prefix = lambda _args: str(root / "prefix")
    test._resolve_executor = lambda _executor: None
    test._resolve_darling_launcher = lambda _prefix: "/fake/darling"
    test._testkit_dir = lambda: root
    test._ctest_runtime_profile_definitions = lambda: {
        "extra": {
            "source-profile": "homebrew",
            "source-module": "darling/src/external/xnu",
            "source-modules": ["darling"],
            "runtime-artifacts": [{
                "build-targets": ["system_kernel"],
                "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
            }],
        }
    }
    test._configure_and_build = lambda *_args, **_kwargs: build
    test._prefix_cleanup_failed = False
    stale_failure_record = build / "Testing" / "Temporary" / "LastTestsFailed.log"
    stale_failure_record.parent.mkdir(parents=True)
    stale_failure_record.write_text("darling/stale_failure\n")
    lifecycle = []

    @contextmanager
    def prefix_context(enabled):
        lifecycle.append(enabled)
        yield

    test._prefix_resource_context = prefix_context
    runtime_contexts = []

    @contextmanager
    def runtime_context(profiles):
        runtime_contexts.append(profiles)
        yield

    test._ctest_runtime_profile_context = runtime_context
    recorded = []
    original = test_module.run_bounded
    def bounded(args, **kwargs):
        recorded.append((args, kwargs))
        if "--show-only=json-v1" in args:
            return ProcessResult(0, stdout=json.dumps({"tests": [{
                "name": "darling/extra",
                "properties": [{"name": "LABELS", "value": [
                    "env:darling", "runtime-profile:extra",
                ]}],
            }]}))
        assert not stale_failure_record.exists(), stale_failure_record
        return ProcessResult(0)

    test_module.run_bounded = bounded
    try:
        args = SimpleNamespace(
            bundle_root=str(root / "bundles"),
            materialize_profile=False,
            keep_prefix_running=False,
            ctest_timeout_seconds=17,
            gc=False,
            red_audit=False,
            profile=None,
            patch=None,
            submodule=[],
            fuzz=False,
            stress=False,
            list=False,
            env="darling",
            changed=False,
            bead=None,
            diag=None,
            label=None,
            executor=None,
            red_only=False,
            prove_red=False,
            with_runtime_profile=["extra"],
        )
        try:
            test.do_run(args, [])
        except SystemExit as exc:
            assert exc.code == 0, exc.code
        else:
            raise AssertionError("do_run did not exit")
    finally:
        test_module.run_bounded = original

    assert lifecycle == [True], lifecycle
    assert runtime_contexts == [["extra"]], runtime_contexts
    assert len(recorded) == 2, recorded
    assert "--show-only=json-v1" in recorded[0][0], recorded
    assert recorded[1][0][0] == "ctest", recorded
    assert recorded[1][1]["timeout_seconds"] == 17, recorded
    assert not stale_failure_record.exists(), stale_failure_record


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    (root / "prefix" / "bin").mkdir(parents=True)
    (root / "prefix" / "bin" / "darling").write_text("launcher\n")
    server_trace = root / "prefix" / "private/var/log/dserver-rpc-trace.log"
    server_trace.parent.mkdir(parents=True)
    server_trace.write_text("stale server trace\n")
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test._prefix = str(root / "prefix")
    test._runtime_evidence_root = root / "evidence"
    test._active_profile = None
    test._bootstrap_syscall_trace = None
    test._bootstrap_stack_sample = root / "bootstrap-stack-sample"
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew": {
            "source-profile": "homebrew",
            "source-module": "darling/src/external/xnu",
            "bootstrap": "rootless-no-mount",
            "source-modules": [
                "darling",
                "darling/src/external/darlingserver",
                "darling/src/external/xnu",
            ],
            "runtime-artifacts": [{"build-targets": ["system_kernel"]}],
        }
    }
    events = []

    source_roots = []

    @contextmanager
    def source_forest(anchor, proof, *, omit_patch, root, evidence_session):
        assert test._active_profile == "homebrew"
        assert anchor["module"] == "darling/src/external/xnu"
        assert proof["runtime-artifacts"][0]["build-targets"] == ["system_kernel"]
        assert not omit_patch
        assert root.parent == evidence_session.directory
        events.append("source")
        source_roots.append(root / "darling")
        yield source_roots[-1]

    @contextmanager
    def deployed(proof, build_root, prefix, *, label, restore_deployment=True):
        assert proof["source-modules"] == [
            "darling",
            "darling/src/external/darlingserver",
            "darling/src/external/xnu",
        ]
        assert build_root == source_roots[-1].parent.parent / "build"
        assert prefix == root / "prefix"
        assert label == "CTest homebrew"
        assert restore_deployment is True
        events.append("deploy")
        yield
        events.append("restore")

    test._guest_runtime_source_forest = source_forest
    test._runtime_red_build_artifacts = lambda _source, _proof, _prefix, scratch, **_kwargs: (
        events.append("build") or scratch / "build"
    )
    test._runtime_red_deployed_artifacts = deployed
    test._preflight_runtime_profile_stack = lambda profile, label: (
        events.append("preflight"),
        profile == "homebrew",
        label == "CTest homebrew",
    )
    with test._ctest_runtime_profile_context(["homebrew"]) as runtime_env:
        assert events == ["preflight", "source", "build", "deploy"], events
        assert runtime_env["DARLING"] == str(root / "prefix" / "bin" / "darling")
        assert runtime_env["DARLING_LAUNCHER"] == str(root / "prefix" / "bin" / "darling")
        assert runtime_env["DPREFIX"] == str(root / "prefix")
        assert runtime_env["DSERVER_TEST_TRACE_FILE"] == str(server_trace)
        assert not server_trace.exists()
    assert events == ["preflight", "source", "build", "deploy", "restore"], events
    assert test._active_profile is None


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test._prefix = str(root / "prefix")
    test._runtime_evidence_root = root / "evidence"
    test._active_profile = None
    errors = []
    test.inf = lambda _message: None
    test.err = errors.append
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew": {
            "source-profile": "homebrew",
            "source-module": "darling/src/external/xnu",
            "source-modules": [
                "darling",
                "darling/src/external/darlingserver",
                "darling/src/external/xnu",
            ],
            "runtime-artifacts": [{"build-targets": ["system_kernel"]}],
        }
    }

    @contextmanager
    def source_forest(_anchor, _proof, *, omit_patch, root, evidence_session):
        assert not omit_patch
        assert root.parent == evidence_session.directory
        yield root / "darling"

    test._guest_runtime_source_forest = source_forest
    test._runtime_red_build_artifacts = lambda *_args, **_kwargs: test.die("build failed")
    test._preflight_runtime_profile_stack = lambda *_args: None
    try:
        with test._ctest_runtime_profile_context(["homebrew"]):
            raise AssertionError("runtime build failure unexpectedly yielded")
    except SystemExit as exc:
        assert str(exc) == "build failed", exc
    entries = list(test._runtime_evidence_root.glob("runtime-evidence-*"))
    assert len(entries) == 1, entries
    assert (entries[0] / "manifest.json").is_file()
    assert errors == [f"preserved failed CTest runtime evidence: {entries[0]}"], errors


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    prefix.mkdir()
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test._prefix = str(prefix)
    test._prefix_cleanup_failed = False
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew-prefix-baseline": {
            "purpose": "prefix-baseline",
            "bootstrap-smoke-timeout-seconds": 60,
        }
    }
    events = []

    @contextmanager
    def prefix_context(enabled):
        assert enabled is True
        events.append("lock")
        yield
        events.append("cleanup")

    @contextmanager
    def deployment_context(profiles, *, label_prefix, retain_deployment):
        assert profiles == ["homebrew-prefix-baseline"], profiles
        assert label_prefix == "Prefix bootstrap", label_prefix
        assert retain_deployment is True
        events.append("deploy")
        yield types.SimpleNamespace(
            prefix=prefix,
            build_root=root / "build",
            env={"DARLING_LAUNCHER": "/fake/darling"},
        )
        events.append("retain")

    test._prefix_resource_context = prefix_context
    test._runtime_profile_deployment_context = deployment_context
    original_repair = test_module.repair_prefix_boot_prerequisites
    test_module.repair_prefix_boot_prerequisites = lambda _prefix: (
        events.append("provision") or types.SimpleNamespace(success=True, changed=[], problems=[])
    )
    original_run_guest_shell = test_module.run_guest_shell
    original_run_bounded = test_module.run_bounded
    test_module.run_guest_shell = lambda *_args, **kwargs: (
        events.append("smoke") or ProcessResult(
            0, stdout="WEST_PREFIX_BOOTSTRAP_OK\n", stderr=""
        )
    )
    test_module.run_bounded = lambda *_args, **_kwargs: ProcessResult(0)
    try:
        test._bootstrap_runtime_profile("homebrew-prefix-baseline")
    finally:
        test_module.repair_prefix_boot_prerequisites = original_repair
        test_module.run_guest_shell = original_run_guest_shell
        test_module.run_bounded = original_run_bounded
    assert events == ["lock", "deploy", "provision", "smoke", "retain", "cleanup"], events


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    trace_dir = root / "trace"
    prefix.mkdir()
    trace_dir.mkdir()
    server_trace = prefix / "private/var/log/dserver-rpc-trace.log"
    server_trace.parent.mkdir(parents=True)
    server_trace.write_text("stale trace\n")
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test._prefix = str(prefix)
    test._prefix_cleanup_failed = False
    test._bootstrap_syscall_trace = None
    test._bootstrap_stack_sample = trace_dir
    recorded_failures = []
    test._active_runtime_evidence = types.SimpleNamespace(
        record_failure_detail=lambda **detail: recorded_failures.append(detail)
    )
    runtime_state = test._bootstrap_runtime_state(prefix)
    assert "RLIMIT_NOFILE soft=" in runtime_state, runtime_state
    assert ".darlingserver.stat.sock: absent" in runtime_state, runtime_state
    messages = []
    test.inf = messages.append
    test.err = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew-prefix-baseline": {
            "purpose": "prefix-baseline",
            "bootstrap-smoke-timeout-seconds": 60,
        }
    }

    @contextmanager
    def prefix_context(_enabled):
        yield

    deployment_env = {"DARLING_LAUNCHER": "/fake/darling"}

    @contextmanager
    def deployment_context(_profiles, *, label_prefix, retain_deployment):
        assert label_prefix == "Prefix bootstrap"
        assert retain_deployment is True
        yield types.SimpleNamespace(
            prefix=prefix,
            build_root=root / "build",
            env=deployment_env,
        )

    observed_prefixes = []
    test._prefix_resource_context = prefix_context
    test._runtime_profile_deployment_context = deployment_context
    original_run_guest_shell = test_module.run_guest_shell
    original_run_bounded = test_module.run_bounded
    def timed_out_guest(*_args, **kwargs):
        observed_prefixes.append(kwargs["command_prefix"])
        (trace_dir / "bootstrap.perf.data").write_text("perf data\n")
        server_trace.parent.mkdir(parents=True, exist_ok=True)
        server_trace.write_text("rpc.recv number=1 name=mldr_path\n")
        (prefix / ".west-rootless-boot.log").write_text(
            "launcher pid=1 waiting-for-shellspawn\n"
        )
        guest_fd_trace = prefix / ".west-rootless-guest-fd.log"
        guest_fd_trace.write_text("launchd pid=2 shellspawn dispatch-start\n")
        return ProcessResult(124, timed_out=True, stdout="", stderr="")

    test_module.run_guest_shell = timed_out_guest
    def completed_command(command, *_args, **_kwargs):
        if tuple(command[:2]) == ("perf", "script"):
            return ProcessResult(0, stdout="sampled stack\n", stderr="")
        return ProcessResult(0)

    test_module.run_bounded = completed_command
    try:
        try:
            test._bootstrap_runtime_profile("homebrew-prefix-baseline")
            raise AssertionError("bootstrap unexpectedly accepted a timed-out stack-sample run")
        except SystemExit as exc:
            assert str(exc) == (
                f"prefix bootstrap guest smoke timed out after 60s; stack sample: {trace_dir}"
            ), exc
    finally:
        test_module.run_guest_shell = original_run_guest_shell
        test_module.run_bounded = original_run_bounded
    assert trace_dir.is_dir(), trace_dir
    assert observed_prefixes == [
        ("perf", "record", "--all-user", "--call-graph", "fp", "--output", str(trace_dir / "bootstrap.perf.data"), "--")
    ], observed_prefixes
    assert (trace_dir / "bootstrap.perf.txt").read_text() == "sampled stack\n"
    assert (trace_dir / "darlingserver-rpc.log").read_text() == "rpc.recv number=1 name=mldr_path\n"
    assert len(recorded_failures) == 1, recorded_failures
    failure = recorded_failures[0]
    assert failure["phase"] == "bootstrap", failure
    assert failure["summary"] == "prefix bootstrap guest smoke timed out after 60s", failure
    assert failure["returncode"] == 124, failure
    assert failure["command"] == [
        "/fake/darling",
        "shell",
        "set -eu\nprintf '%s\\n' WEST_PREFIX_BOOTSTRAP_OK",
    ], failure
    assert failure["output"] == "", failure
    assert failure["artifacts"] == [
        trace_dir / "bootstrap.perf.data",
        trace_dir / "bootstrap.perf.txt",
        trace_dir / "darlingserver-rpc.log",
        prefix / ".west-rootless-boot.log",
        prefix / ".west-rootless-guest-fd.log",
    ], failure
    assert messages == [
        f"prefix bootstrap stack sample: {trace_dir}",
        "prefix bootstrap provision: created private/var/tmp with mode 1777",
        "prefix bootstrap provision: created libexec/darling/private/var/tmp with mode 1777",
        f"prefix bootstrap server trace: {trace_dir / 'darlingserver-rpc.log'}",
    ], messages


with tempfile.TemporaryDirectory() as temp:
    trace_dir = Path(temp)
    (trace_dir / "bootstrap.123").write_text(
        "12:00:00.000001 --- SIGSEGV {si_signo=SIGSEGV, si_code=SEGV_MAPERR, si_addr=0x18} ---\n"
    )
    assert test_module.bootstrap_trace_fatal_signal(trace_dir) == "SIGSEGV at 0x18"


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    prefix.mkdir()
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test._prefix = str(prefix)
    test._prefix_cleanup_failed = False
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew-prefix-baseline": {
            "purpose": "prefix-baseline",
            "bootstrap-smoke-timeout-seconds": 60,
        }
    }
    restored = []

    @contextmanager
    def prefix_context(_enabled):
        yield

    @contextmanager
    def deployment_context(_profiles, *, label_prefix, retain_deployment):
        assert label_prefix == "Prefix bootstrap"
        assert retain_deployment is True
        try:
            yield types.SimpleNamespace(
                prefix=prefix,
                build_root=root / "build",
                env={"DARLING_LAUNCHER": "/fake/darling"},
            )
        finally:
            restored.append("restored")

    test._prefix_resource_context = prefix_context
    test._runtime_profile_deployment_context = deployment_context
    original_run_guest_shell = test_module.run_guest_shell
    original_run_bounded = test_module.run_bounded
    test_module.run_guest_shell = lambda *_args, **kwargs: ProcessResult(
        0, stdout="wrong marker\n", stderr=""
    )
    test_module.run_bounded = lambda *_args, **_kwargs: ProcessResult(0)
    try:
        try:
            test._bootstrap_runtime_profile("homebrew-prefix-baseline")
            raise AssertionError("bootstrap accepted a guest run without its verdict marker")
        except SystemExit as exc:
            assert str(exc) == "prefix bootstrap guest smoke returned without its verdict marker", exc
    finally:
        test_module.run_guest_shell = original_run_guest_shell
        test_module.run_bounded = original_run_bounded
    assert restored == ["restored"], restored


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    prefix.mkdir()
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(root)
    test._prefix = str(prefix)
    test._prefix_cleanup_failed = False
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._ctest_runtime_profile_definitions = lambda: {
        "homebrew-prefix-baseline": {
            "purpose": "prefix-baseline",
            "bootstrap-smoke-timeout-seconds": 60,
        }
    }

    @contextmanager
    def prefix_context(_enabled):
        yield

    @contextmanager
    def deployment_context(_profiles, *, label_prefix, retain_deployment):
        assert label_prefix == "Prefix bootstrap"
        assert retain_deployment is True
        yield types.SimpleNamespace(
            prefix=prefix,
            build_root=root / "build",
            env={"DARLING_LAUNCHER": "/fake/darling"},
        )

    test._prefix_resource_context = prefix_context
    test._runtime_profile_deployment_context = deployment_context
    calls = []
    original_run_guest_argv = test_module.run_guest_argv
    original_run_bounded = test_module.run_bounded

    def successful_executable(*args, **kwargs):
        calls.append((args, kwargs))
        return ProcessResult(0, stdout="", stderr="")

    test_module.run_guest_argv = successful_executable
    test_module.run_bounded = lambda *_args, **_kwargs: ProcessResult(0)
    try:
        test._bootstrap_runtime_profile(
            "homebrew-prefix-baseline", executable="/usr/bin/true"
        )
    finally:
        test_module.run_guest_argv = original_run_guest_argv
        test_module.run_bounded = original_run_bounded

    assert calls and calls[0][0][2] == ("/usr/bin/true",), calls


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    old_output = root / "west-ctest-guest-c.old"
    fresh_output = root / "west-ctest-guest-c.fresh"
    output_dir = root / "west-ctest-guest-c.directory"
    unrelated = root / "unrelated"
    old_output.write_text("old\n")
    fresh_output.write_text("fresh\n")
    output_dir.mkdir()
    unrelated.write_text("keep\n")
    old_time = time.time() - 7200
    os.utime(old_output, (old_time, old_time))

    test = DarlingTest.__new__(DarlingTest)
    messages = []
    test.inf = messages.append
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._gc_guest_runner_output(root, max_age_hours=1)

    assert not old_output.exists(), old_output
    assert fresh_output.exists(), fresh_output
    assert output_dir.is_dir(), output_dir
    assert unrelated.exists(), unrelated
    assert any("guest-runner gc: pruned 1 file(s)" in message for message in messages), messages

print("PASS west-test-ctest-lifecycle-contract")

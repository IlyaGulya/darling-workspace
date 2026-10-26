import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.test import DarlingTest
from test_prefix import (
    cleanup_rootless_prefix_processes,
    cleanup_rootless_runtime_sockets,
    darlingserver_pids_for_prefix,
    remove_stale_init_pid,
    remove_stale_server_socket,
    PrefixLifecycleOwner,
    rootless_prefix_process_snapshot,
)
import test_prefix as prefix_module
from west_commands.prefix_repair import repair_prefix_boot_prerequisites


def make_test():
    test = DarlingTest.__new__(DarlingTest)
    test.inf = lambda *args, **kwargs: None
    test.wrn = lambda *args, **kwargs: None
    test.err_messages = []
    test.err = lambda message: test.err_messages.append(message)
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._darling_prefix_env = lambda prefix: {"DPREFIX": str(prefix), "DARLING_PREFIX": str(prefix)}
    return test


def owned_loader(prefix: Path):
    executable = prefix / "mldr"
    shutil.copy2("/bin/sleep", executable)
    descriptor = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return subprocess.Popen([str(executable), "60"], env={**os.environ, "DPREFIX": str(prefix)}, pass_fds=(descriptor,))
    finally:
        os.close(descriptor)



with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    tagged = owned_loader(prefix)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, "DARLING_PREFIX": str(prefix)},
    )
    try:
        for _ in range(20):
            if any(entry.startswith(f"{tagged.pid} ") for entry in rootless_prefix_process_snapshot(prefix)):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("tagged rootless process was not discovered")
        result = cleanup_rootless_prefix_processes(prefix)
        assert result.success and result.changed, result
        tagged.wait(timeout=3)
        assert unrelated.poll() is None, "cleanup matched an untagged prefix process"
    finally:
        for process in (tagged, unrelated):
            if process.poll() is None:
                process.kill()
                process.wait()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    orphaned_shellspawn = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=prefix, env=dict(os.environ),
    )
    try:
        assert not any(entry.startswith(f"{orphaned_shellspawn.pid} ")
                       for entry in rootless_prefix_process_snapshot(prefix))
        result = cleanup_rootless_prefix_processes(prefix)
        assert result.success and not result.changed, result
        assert orphaned_shellspawn.poll() is None, "cwd alone is not runtime ownership"
    finally:
        if orphaned_shellspawn.poll() is None:
            orphaned_shellspawn.kill()
            orphaned_shellspawn.wait()

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    other_prefix = root / "other-prefix"
    prefix.mkdir()
    other_prefix.mkdir()
    loader = root / "mldr"
    shutil.copy2("/bin/sleep", loader)
    server = root / "darlingserver"
    shutil.copy2("/bin/sleep", server)
    owned_names = {"owned", "owned_scrubbed", "owned_server"}
    markerless_env = dict(os.environ)
    markerless_env.pop("DARLING_PREFIX", None)
    markerless_env.pop("DARLING_ROOTLESS", None)
    endpoint_owner = subprocess.Popen(["/bin/sleep", "0"])
    endpoint_owner.wait(timeout=3)
    directory_fds = []
    processes = {}
    try:
        for directory in (prefix, other_prefix):
            directory_fds.append(os.open(directory, os.O_RDONLY | os.O_DIRECTORY))
        prefix_fd, other_fd = directory_fds
        for name, executable, socket_fd, inherited_fds in (
            ("owned", loader, prefix_fd, (prefix_fd,)),
            ("owned_scrubbed", loader, prefix_fd, (prefix_fd,)),
            ("owned_server", server, prefix_fd, (prefix_fd,)),
            ("other_prefix", loader, other_fd, (other_fd,)),
            ("ordinary_host", "/bin/sleep", prefix_fd, (prefix_fd,)),
            ("missing_fd", loader, prefix_fd, ()),
        ):
            processes[name] = subprocess.Popen(
                ["darlingserver" if name == "owned_server" else
                 "mldr" if name == "ordinary_host" else "/usr/bin/wget", "60"],
                executable=executable,
                cwd=root,
                pass_fds=inherited_fds,
                env=markerless_env if name in {"owned_scrubbed", "owned_server"} else {
                    **markerless_env,
                    "__mldr_sockpath": (
                        f"/proc/{endpoint_owner.pid}/fd/{socket_fd}/.darlingserver.sock"
                    ),
                },
            )
        owned = processes["owned"]
        for _ in range(20):
            discovered = rootless_prefix_process_snapshot(prefix)
            if all(any(entry.startswith(f"{processes[name].pid} ") for entry in discovered)
                   for name in owned_names):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "markerless loader retaining the prefix directory fd was not discovered"
            )
        for name, process in processes.items():
            if name not in owned_names:
                assert not any(
                    entry.startswith(f"{process.pid} ") for entry in discovered
                ), (name, discovered)
        server_pid = processes["owned_server"].pid
        server_entries = [(server_pid, 1, "darlingserver 60")]
        assert darlingserver_pids_for_prefix(prefix, server_entries) == [server_pid]
        assert darlingserver_pids_for_prefix(other_prefix, server_entries) == []
        result = cleanup_rootless_prefix_processes(prefix)
        assert result.success and result.changed, result
        for name in owned_names:
            processes[name].wait(timeout=3)
            assert processes[name].returncode < 0, (name, processes[name].returncode)
        for name, process in processes.items():
            if name not in owned_names:
                assert process.poll() is None, f"cleanup terminated {name}"
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.kill()
            process.wait()
        for directory_fd in directory_fds:
            os.close(directory_fd)

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    tagged = owned_loader(prefix)
    test = make_test()
    test._resolve_darling_launcher = lambda _prefix: None
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._cleanup_prefix_mounts = lambda _prefix: True
    test._remove_stale_init_pid = lambda _prefix: None
    test._remove_stale_server_socket = lambda _prefix: False
    try:
        assert not test._shutdown_runtime_prefix(prefix), "external rescue must not create PASS"
        tagged.wait(timeout=3)
    finally:
        if tagged.poll() is None:
            tagged.kill()
            tagged.wait()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    tagged = owned_loader(prefix)
    test = make_test()
    test._prefix = str(prefix)
    test._keep_prefix_running = False
    test._resolve_darling_launcher = lambda _prefix: None
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._cleanup_prefix_mounts = lambda _prefix: True
    test._remove_stale_init_pid = lambda _prefix: None
    test._remove_stale_server_socket = lambda _prefix: False
    try:
        assert not test._shutdown_test_prefix(), "external rescue must not create PASS"
        tagged.wait(timeout=3)
    finally:
        if tagged.poll() is None:
            tagged.kill()
            tagged.wait()

# Real owned host processes exercise shutdown verdicts, not mocked argv/result
# forwarding. These are lifecycle models, not guest-execution evidence.
for outcome, expected, attempts in (
    ("zero", True, 1), ("nonzero", False, 1),
    ("retry", False, 2), ("timeout", False, 1),
):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        prefix = root / "guest"
        prefix.mkdir()
        worker = owned_loader(prefix)
        launcher = root / "shutdown-host-fixture"
        calls = root / "attempts"
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import os,signal,time\n"
            "from pathlib import Path\n"
            "calls=Path(os.environ['SHUTDOWN_ATTEMPTS_FILE'])\n"
            "attempt=int(calls.read_text())+1 if calls.exists() else 1\n"
            "calls.write_text(str(attempt))\n"
            "outcome=os.environ['SHUTDOWN_OUTCOME']\n"
            "if outcome=='timeout': time.sleep(30)\n"
            "if outcome=='retry' and attempt==1: raise SystemExit(7)\n"
            "pid=int(os.environ['SHUTDOWN_WORKER_PID'])\n"
            "os.kill(pid,signal.SIGTERM)\n"
            "deadline=time.monotonic()+5\n"
            "while Path(f'/proc/{pid}/exe').exists():\n"
            "    assert time.monotonic()<deadline\n"
            "    time.sleep(0.01)\n"
            "if outcome=='nonzero':\n"
            "    print('Darling container is not running')\n"
            "    raise SystemExit(7)\n"
        )
        launcher.chmod(0o755)
        test = make_test()
        test._prefix = str(prefix)
        test._keep_prefix_running = False
        test._resolve_darling_launcher = lambda _prefix: str(launcher)
        selected = {
            "WEST_TEST_SHUTDOWN_TIMEOUT_SECONDS": "1" if outcome == "timeout" else "10",
            "WEST_TEST_SHUTDOWN_ATTEMPTS": str(attempts),
            "SHUTDOWN_ATTEMPTS_FILE": str(calls),
            "SHUTDOWN_OUTCOME": outcome,
            "SHUTDOWN_WORKER_PID": str(worker.pid),
        }
        saved = {key: os.environ.get(key) for key in selected}
        os.environ.update(selected)
        try:
            assert worker.poll() is None
            assert test._shutdown_test_prefix() is expected, (outcome, test.err_messages)
            assert calls.read_text() == str(attempts)
            worker.wait(timeout=5)
            assert not rootless_prefix_process_snapshot(prefix)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if worker.poll() is None:
                worker.kill()
            worker.wait()

with tempfile.TemporaryDirectory() as temp:
    stale_prefix = Path(temp)
    (stale_prefix / ".init.pid").write_text("999999999\n")
    assert remove_stale_init_pid(stale_prefix, pid_is_usable=lambda _pid: False)
    (stale_prefix / ".init.pid").write_text("999999999\n")
    test._remove_stale_init_pid(stale_prefix)
    assert not (stale_prefix / ".init.pid").exists()

with tempfile.TemporaryDirectory() as temp:
    stale_prefix = Path(temp)
    stale_socket = stale_prefix / ".darlingserver.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(stale_socket))
    server.close()
    assert remove_stale_server_socket(stale_prefix)
    assert not stale_socket.exists()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    owned_paths = [
        prefix / ".darlingserver.stat.sock",
        prefix / "var/run/shellspawn.sock",
        prefix / "var/tmp/launchd/sock",
    ]
    listeners = []
    for socket_path in owned_paths:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listeners.append(listener)
    fixture_socket = prefix / "private/tmp/guest-fixture.sock"
    fixture_socket.parent.mkdir(parents=True)
    fixture_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    fixture_listener.bind(str(fixture_socket))
    try:
        result = cleanup_rootless_runtime_sockets(prefix)
        assert result.success, result
        assert len(result.changed) == len(owned_paths), result
        assert all(not socket_path.exists() for socket_path in owned_paths)
        assert fixture_socket.exists(), "cleanup removed a guest fixture socket"
    finally:
        for listener in [*listeners, fixture_listener]:
            listener.close()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    socket_path = prefix / "var/run/shellspawn.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.write_text("not a socket")
    result = cleanup_rootless_runtime_sockets(prefix)
    assert not result.success, result
    assert socket_path.exists(), "cleanup removed a non-socket artifact"
    assert any("not a socket" in problem for problem in result.problems), result

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    stale_socket = prefix / ".darlingserver.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(stale_socket))
    server.close()
    test = make_test()
    test._resolve_darling_launcher = lambda _prefix: None
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._cleanup_prefix_mounts = lambda _prefix: True
    test._remove_stale_init_pid = lambda _prefix: None
    assert test._shutdown_runtime_prefix(prefix)
    assert not stale_socket.exists()

with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    owner = PrefixLifecycleOwner(
        resolve_launcher=lambda _prefix: None,
        prefix_env=lambda _prefix: {},
        cleanup_mounts=lambda _prefix: types.SimpleNamespace(
            changed=[], problems=["busy"], success=False
        ),
        init_pid_is_usable=lambda _pid: False,
        inf=lambda _message: None,
        err=lambda _message: None,
        wrn=lambda _message: None,
        process_entries=lambda: [],
    )
    assert not owner.finalize(prefix)

with tempfile.TemporaryDirectory() as temp:
    parent = Path(temp)
    prefix = parent / "fresh-prefix"
    owner = PrefixLifecycleOwner(
        resolve_launcher=lambda _prefix: None,
        prefix_env=lambda _prefix: {},
        cleanup_mounts=lambda _prefix: types.SimpleNamespace(
            changed=[], problems=[], success=True
        ),
        init_pid_is_usable=lambda _pid: False,
        inf=lambda _message: None,
        err=lambda _message: None,
        wrn=lambda _message: None,
        process_entries=lambda: [],
    )
    with owner.locked(prefix):
        assert not prefix.exists()

test = make_test()
with tempfile.TemporaryDirectory() as temp:
    test._prefix = temp
    test._shutdown_test_prefix = lambda: False
    try:
        with test._prefix_resource_context(True):
            raise AssertionError("failed prefix reset unexpectedly yielded")
    except SystemExit:
        pass
    else:
        raise AssertionError("failed prefix reset unexpectedly passed")
    assert test._prefix_cleanup_failed




# A runtime retaining the prefix remains discoverable without readable environ.
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "guest"
    prefix.mkdir()
    proc = root / "proc"
    process = proc / "4242"
    (process / "fd").mkdir(parents=True)
    runtime_executable = root / "mldr"
    runtime_executable.write_bytes(b"host ownership model")
    (process / "exe").symlink_to(runtime_executable)
    (process / "fd/5").symlink_to(prefix)
    assert not (process / "environ").exists()
    assert [entry.split(" ", 1)[0] for entry in rootless_prefix_process_snapshot(
        prefix, proc_root=proc, current_pid=1,
    )] == ["4242"]
    (process / "fd/5").unlink()
    (process / "environ").write_bytes(f"DPREFIX={prefix}\0DARLING_PREFIX={prefix}\0".encode())
    assert rootless_prefix_process_snapshot(prefix, proc_root=proc, current_pid=1) == []

    alias = root / "alias"
    alias.symlink_to(prefix, target_is_directory=True)
    assert not prefix_module.prefix_path_is_safe(str(alias))
    assert not prefix_module.prefix_path_is_safe(str(root) + "/guest/../guest")
    attempted = []
    owner = PrefixLifecycleOwner(
        resolve_launcher=lambda _prefix: attempted.append("launcher"),
        prefix_env=lambda _prefix: {}, cleanup_mounts=lambda _prefix: attempted.append("mounts"),
        init_pid_is_usable=lambda _pid: False, inf=lambda _message: None,
        err=lambda _message: None, wrn=lambda _message: None, process_entries=lambda: [],
    )
    owner._kill_server = lambda _prefix: attempted.append("kill")
    assert not owner.shutdown(str(alias))
    assert not owner.finalize(str(root) + "/guest/../guest")
    assert attempted == [], "unsafe spelling reached product shutdown or host rescue"

# An orphan may retain the validated launcher's prefix environment after
# closing directory capabilities. That is not kill authority, but ignoring it
# must not turn an unverified residual runtime into a clean shutdown verdict.
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "guest"
    prefix.mkdir()
    image = root / "installed"
    image.mkdir()
    executable = image / "mldr"
    shutil.copy2("/bin/sleep", executable)
    orphan = subprocess.Popen(
        [str(executable), "60"],
        env={**os.environ, "DARLING_PREFIX": str(prefix)},
    )
    try:
        test = make_test()
        test._prefix = str(prefix)
        assert not test._shutdown_test_prefix(), "unverified runtime residue produced PASS"
        assert orphan.poll() is None, "a prefix marker alone authorized a kill"
        assert any(str(orphan.pid) in message for message in test.err_messages)
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait(timeout=3)

print("PASS west-test-prefix-cleanup-contract")

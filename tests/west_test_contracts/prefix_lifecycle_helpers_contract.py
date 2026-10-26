"""Installed-runtime prefix resolution, diagnostic capture and fixture assertions."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import test_prefix as prefix_module
from test_prefix import PrefixLifecycleMixin
from runtime_install import InstalledRuntime
from test_execution import run_bounded


def new_test(**attributes) -> PrefixLifecycleMixin:
    test = PrefixLifecycleMixin.__new__(PrefixLifecycleMixin)
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.wrn = lambda _message: None
    test._prefix_env = {}

    def die(message: str):
        raise SystemExit(message)

    test.die = die
    for name, value in attributes.items():
        setattr(test, name, value)
    return test


def expect_die(call, needle: str) -> None:
    try:
        call()
    except SystemExit as exc:
        assert needle in str(exc), (needle, str(exc))
        return
    raise AssertionError(f"expected a refusal containing {needle!r}")


# Prefix resolution does not inspect retired parser flags or provision state.
saved_prefix = os.environ.pop("DPREFIX", None)
try:
    test = new_test()
    assert test._resolve_prefix(SimpleNamespace(prefix=None)) is None
    expect_die(lambda: test._resolve_prefix(SimpleNamespace(prefix="/state/guest/../guest")), "unsafe Darling prefix path")
    expect_die(lambda: test._resolve_prefix(SimpleNamespace(prefix="/state/./guest")), "unsafe Darling prefix path")
    assert test._prefix_env == {}
    os.environ["DPREFIX"] = "/state/from-environment"
    assert test._resolve_prefix(SimpleNamespace(prefix=None)) == "/state/from-environment"
    assert test._resolve_prefix(SimpleNamespace(prefix="/state/explicit")) == "/state/explicit"
finally:
    if saved_prefix is None:
        os.environ.pop("DPREFIX", None)
    else:
        os.environ["DPREFIX"] = saved_prefix

runtime = InstalledRuntime(Path("/opt/darling"), Path("/opt/darling/bin/darling"),
                           Path("/opt/darling/libexec/darling"), None)
test = new_test(_runtime=runtime, _installed_runtime=lambda: runtime)
environment = test._darling_prefix_env("/state/guest")
assert environment["DPREFIX"] == "/state/guest"
assert environment["DARLING_PREFIX"] == "/state/guest"
assert environment["DARLING_LAUNCHER"] == "/opt/darling/bin/darling"
assert environment["DARLING_RUNTIME_ROOT"] == "/opt/darling"
assert test._resolve_darling_launcher("/state/guest") == str(runtime.launcher)
assert test._resolve_darling_launcher("/state/another-guest") == str(runtime.launcher)


# A lease is external to a virgin guest prefix and independent of its siblings.
with tempfile.TemporaryDirectory(prefix="west-prefix-lease-contract-") as temporary:
    parent = Path(temporary)
    first = parent / "first"
    sibling = parent / "second"
    owner = prefix_module.PrefixLifecycleOwner(
        resolve_launcher=lambda _prefix: None,
        prefix_env=lambda _prefix: {},
        cleanup_mounts=lambda _prefix: SimpleNamespace(changed=[], problems=[], success=True),
        init_pid_is_usable=lambda _pid: False,
        inf=lambda _message: None, err=lambda _message: None, wrn=lambda _message: None,
        process_entries=lambda: [],
    )
    first_lease = prefix_module.prefix_lease_path(first)
    sibling_lease = prefix_module.prefix_lease_path(sibling)
    assert first_lease.parent == parent and first_lease != sibling_lease
    with owner.locked(first):
        assert not first.exists(), "leasing must not initialize or decorate guest state"
        inode = first_lease.stat().st_ino
        descriptor = os.open(first_lease, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("the same prefix acquired a second lease")
        finally:
            os.close(descriptor)
        descriptor = os.open(
            sibling_lease, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    assert first_lease.stat().st_ino == inode
    with owner.locked(first):
        assert first_lease.stat().st_ino == inode
    first_lease.unlink()
    victim = parent / "unrelated"
    victim.write_text("preserve\n")
    first_lease.symlink_to(victim)
    try:
        with owner.locked(first):
            raise AssertionError("symlink lease was accepted")
    except OSError:
        pass
    assert victim.read_text() == "preserve\n"
    first_lease.unlink()
    os.mkfifo(first_lease, 0o600)
    try:
        with owner.locked(first):
            raise AssertionError("nonregular lease was accepted")
    except ValueError:
        pass
    first_lease.unlink()
    with patch.object(prefix_module.os, "fstat", return_value=SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600, st_uid=os.geteuid() + 1,
    )):
        try:
            with owner.locked(first):
                raise AssertionError("another user's lease was accepted")
        except ValueError:
            pass

# Real CTest descendants borrow the same open-file description without releasing
# its ancestor's lock. Ordinary subprocesses receive neither token nor lease FD.
with tempfile.TemporaryDirectory(prefix="west-borrowed-prefix-contract-") as temporary:
    parent = Path(temporary)
    prefix = parent / "guest"
    lease = prefix_module.prefix_lease_path(prefix)
    child = parent / "borrow.py"
    sentinel = parent / "borrowed"
    child.write_text(
        "import os,sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'west_commands')!r})\n"
        "from test_prefix import prefix_lease,validate_borrowed_prefix_lease\n"
        "from test_execution import run_bounded\n"
        "prefix=sys.argv[1]\n"
        "descriptor=validate_borrowed_prefix_lease(prefix)\n"
        "assert descriptor is not None\n"
        "with prefix_lease(prefix) as borrowed:\n"
        "    assert borrowed == descriptor\n"
        "metadata=os.fstat(descriptor)\n"
        "probe='import os,sys; from pathlib import Path; "
        "assert not any(k in os.environ for k in (\"WEST_TEST_PREFIX_LEASE\",\"WEST_TEST_PREFIX_LEASE_FD\")); "
        "expected=(int(sys.argv[1]),int(sys.argv[2])); "
        "entries=list(Path(\"/proc/self/fd\").iterdir())\\n"
        "for entry in entries:\\n"
        " try: actual=entry.stat()\\n"
        " except OSError: continue\\n"
        " assert (actual.st_dev,actual.st_ino)!=expected\\n'\n"
        "result=run_bounded([sys.executable,'-c',probe,str(metadata.st_dev),str(metadata.st_ino)],"
        "cwd=Path.cwd(),env=dict(os.environ),timeout_seconds=10,capture_output=True)\n"
        "assert result.returncode == 0, result.stdout+result.stderr\n"
        "Path(sys.argv[2]).write_text('borrowed without releasing ancestor\\n')\n"
    )
    suite = parent / "suite"
    suite.mkdir()
    (suite / "CTestTestfile.cmake").write_text(
        "add_test(borrowed_prefix " + " ".join(
            f"[=[{argument}]=]" for argument in (sys.executable, str(child), str(prefix), str(sentinel))
        ) + ")\n"
    )
    def assert_ancestor_still_locked():
        contender = os.open(lease, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            try:
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            raise AssertionError("borrowed child released the ancestor's lease")
        finally:
            os.close(contender)
    with prefix_module.prefix_lease(prefix) as descriptor:
        environment = dict(os.environ, WEST_TEST_PREFIX_LEASE=str(prefix),
                           WEST_TEST_PREFIX_LEASE_FD=str(descriptor))
        result = run_bounded(
            ["ctest", "--test-dir", str(suite), "--output-on-failure"],
            cwd=parent, env=environment, timeout_seconds=30,
            capture_output=True, pass_fds=(descriptor,),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert sentinel.read_text() == "borrowed without releasing ancestor\n"
        assert_ancestor_still_locked()
        sentinel.unlink()
        unlocked = os.open(lease, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        other = os.open(parent / "other-lease", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(other, fcntl.LOCK_EX)
            missing_descriptor = dict(environment)
            missing_descriptor.pop("WEST_TEST_PREFIX_LEASE_FD")
            cases = [
                (str(prefix), missing_descriptor, ()),
                (str(prefix), {**environment, "WEST_TEST_PREFIX_LEASE_FD": str(other)}, (other,)),
                (str(prefix), {**environment, "WEST_TEST_PREFIX_LEASE_FD": str(unlocked)}, (unlocked,)),
                (str(prefix) + "/../different", environment, (descriptor,)),
            ]
            for requested, selected, inherited in cases:
                refused = subprocess.run(
                    [sys.executable, str(child), requested, str(sentinel)],
                    cwd=parent, env=selected, pass_fds=inherited,
                    capture_output=True, text=True, timeout=10, check=False,
                )
                assert refused.returncode != 0, refused.stdout + refused.stderr
                assert not sentinel.exists() and not prefix.exists()
                assert_ancestor_still_locked()
        finally:
            os.close(other)
            os.close(unlocked)

# --- _parse_file_mode ----------------------------------------------------
test = new_test()
assert test._parse_file_mode({"name": "fixture"}, "eunion-template-files", 0, 0o644) == 0o644
assert test._parse_file_mode({"name": "fixture"}, "eunion-template-files", 0, "755") == 0o755
expect_die(
    lambda: test._parse_file_mode({"name": "fixture"}, "eunion-template-files", 2, "nope"),
    "eunion-template-files[2] has invalid mode",
)


# --- _verify_prefix_idle -------------------------------------------------
errors: list[str] = []
test = new_test(err=errors.append)
assert test._verify_prefix_idle() is False
assert errors == ["clean-shutdown verification needs a selected Darling prefix"], errors

test = new_test(_prefix="/tmp/west-prefix")
test._prefix_lifecycle_owner = lambda: SimpleNamespace(finalize=lambda prefix: prefix == Path("/tmp/west-prefix"))
assert test._verify_prefix_idle() is True

# --- _bootstrap_runtime_state --------------------------------------------
original_snapshot = prefix_module.rootless_prefix_process_snapshot
prefix_module.rootless_prefix_process_snapshot = lambda _prefix: ["42 darlingserver --prefix"]
try:
    with tempfile.TemporaryDirectory(prefix="west-bootstrap-state-") as raw:
        prefix = Path(raw)
        (prefix / ".darlingserver.stat.sock").write_text("")
        state = PrefixLifecycleMixin._bootstrap_runtime_state(prefix)
finally:
    prefix_module.rootless_prefix_process_snapshot = original_snapshot
lines = state.splitlines()
assert lines[0] == "--- bootstrap runtime state ---"
assert lines[2] == "42 darlingserver --prefix"
assert any(line.startswith("RLIMIT_NOFILE soft=") for line in lines), lines
assert ".darlingserver.stat.sock: file mode=" in state, state
assert "var/run/shellspawn.sock: absent" in state, state

# --- _capture_bootstrap_server_trace -------------------------------------
with tempfile.TemporaryDirectory(prefix="west-bootstrap-trace-") as raw:
    root = Path(raw)
    prefix = root / "prefix"
    diagnostic_dir = root / "diagnostics"
    diagnostic_dir.mkdir()
    test = new_test()
    test._capture_bootstrap_server_trace(prefix, diagnostic_dir, label="bootstrap")
    assert not (diagnostic_dir / "darlingserver-rpc.log").exists()

    trace = prefix / "private/var/log/dserver-rpc-trace.log"
    trace.parent.mkdir(parents=True)
    trace.write_text("rpc trace\n")
    test._capture_bootstrap_server_trace(prefix, diagnostic_dir, label="bootstrap")
    assert (diagnostic_dir / "darlingserver-rpc.log").read_text() == "rpc trace\n"

# --- _resolve_bootstrap_diagnostic_dir -----------------------------------
with tempfile.TemporaryDirectory(prefix="west-diagnostic-dir-") as raw:
    root = Path(raw)
    test = new_test(topdir=root)
    assert test._resolve_bootstrap_diagnostic_dir("logs") == (root / "logs").resolve()
    absolute = root / "absolute"
    assert test._resolve_bootstrap_diagnostic_dir(str(absolute)) == absolute.resolve()

# --- _eunion_prefix_context ----------------------------------------------
# A fixture without the E-UNION resource must not touch the prefix at all.
test = new_test(_prefix="/tmp/west-prefix")
with test._eunion_prefix_context({"name": "plain", "requires_resources": []}, {}) as value:
    assert value is None

expected = {"name": "eunion", "requires_resources": ["darling-eunion-prefix"]}
test = new_test(_prefix=None)
expect_die(
    lambda: test._eunion_prefix_context(expected, {}).__enter__(),
    "darling-eunion-prefix needs DPREFIX",
)

# --- E-UNION fixture assertions ------------------------------------------
with tempfile.TemporaryDirectory(prefix="west-eunion-assertions-") as raw:
    prefix = Path(raw)
    test = new_test()

    lower = prefix / "usr/share/darling/tests/eunion/contract/template.txt"
    lower.parent.mkdir(parents=True)
    lower.write_text("contents\n")
    lower.chmod(0o644)
    test._verify_eunion_template_files_after(
        {"name": "eunion"},
        [
            {
                "path": lower,
                "contents": "contents\n",
                "mode": "644",
                "xattrs": {},
                "absent_xattrs": ["user.west-absent"],
            }
        ],
    )
    try:
        test._verify_eunion_template_files_after(
            {"name": "eunion"},
            [{"path": lower, "contents": "other\n", "mode": None, "xattrs": {}}],
        )
    except SystemExit as exc:
        assert "E-UNION template fixture was modified" in str(exc), exc
    else:
        raise AssertionError("a modified E-UNION template fixture must be refused")

    try:
        test._verify_eunion_template_files_after(
            {"name": "eunion"}, [{"path": lower, "contents": "contents\n", "mode": "600"}]
        )
    except SystemExit as exc:
        assert "E-UNION template fixture mode changed" in str(exc), exc
    else:
        raise AssertionError("a changed E-UNION template fixture mode must be refused")

    upper = prefix / "private/var/tmp/west-contract/upper.txt"
    upper.parent.mkdir(parents=True, exist_ok=True)
    upper.write_text("upper\n")
    test._verify_eunion_upper_paths_after(
        {"name": "eunion"}, prefix, ["/private/var/tmp/west-contract/upper.txt"]
    )
    try:
        test._verify_eunion_upper_paths_after(
            {"name": "eunion"}, prefix, ["/private/var/tmp/west-contract/missing.txt"]
        )
    except SystemExit as exc:
        assert "required E-UNION upper path is missing" in str(exc), exc
    else:
        raise AssertionError("a missing required E-UNION upper path must be refused")

    # A forbidden path is one that only the disposable template owns.
    forbidden = prefix / "usr/share/darling/tests/eunion/contract/forbidden.txt"
    test._verify_eunion_forbidden_template_paths_after(
        {"name": "eunion"}, prefix, ["/usr/share/darling/tests/eunion/contract/forbidden.txt"]
    )
    forbidden.write_text("created\n")
    try:
        test._verify_eunion_forbidden_template_paths_after(
            {"name": "eunion"}, prefix, ["/usr/share/darling/tests/eunion/contract/forbidden.txt"]
        )
    except SystemExit as exc:
        assert "forbidden E-UNION template path was created" in str(exc), exc
    else:
        raise AssertionError("a created forbidden E-UNION template path must be refused")

# --- _ps_entries / _prefix_process_snapshot ------------------------------
if os.path.isfile("/bin/ps") or os.path.isfile("/usr/bin/ps"):
    test = new_test()
    entries = test._ps_entries()
    assert all(
        isinstance(pid, int) and isinstance(ppid, int) and isinstance(args, str)
        for pid, ppid, args in entries
    ), entries
    assert any(pid == os.getpid() for pid, _ppid, _args in entries), "the runner itself must appear"

test = new_test(_prefix="/tmp/west-prefix")
test._prefix_lifecycle_owner = lambda: SimpleNamespace(
    process_snapshot=lambda prefix: [f"snapshot of {prefix}"]
)
assert test._prefix_process_snapshot(Path("/tmp/west-prefix")) == [
    "snapshot of /tmp/west-prefix"
]

print("PASS prefix-lifecycle-helpers-contract")

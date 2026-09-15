"""Contract for the prefix lifecycle helpers that moved out of ``test.py``.

``PrefixLifecycleMixin`` owns prefix resolution, the retained-provider marker
check, the bootstrap diagnostics that must survive prefix cleanup, and the
E-UNION fixture assertions.  These assertions cover that observable behaviour,
including the exact refusals that keep a stale or wrong prefix from being
silently accepted.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import types
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

import west_commands.test as test_module  # noqa: E402

# Importing the facade puts ``west_commands`` on sys.path, so the top-level
# names below are the very modules the facade resolves at call time.
import test_prefix as prefix_module  # noqa: E402
from west_commands.test import (  # noqa: E402
    DarlingTest,
    # Taken from the facade: its inheritance resolves these from the top-level
    # module names, so a package-relative import would be a second class object.
    RETAINED_RUNTIME_PROFILE_MARKER,
    RuntimeProfileDeployment,
)

PROFILE_DEFINITION = {
    "source-profile": "homebrew",
    "source-module": "darling/src/external/xnu",
    "source-modules": ["darling"],
    "runtime-artifacts": [
        {
            "build-targets": ["system_kernel"],
            "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
        }
    ],
    "launcher-env": {"DARLING_NOOVERLAYFS": "1"},
}


def new_test(**attributes) -> DarlingTest:
    test = DarlingTest.__new__(DarlingTest)
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


# --- _resolve_prefix ------------------------------------------------------
test = new_test()
assert test._resolve_prefix(SimpleNamespace(
    no_overlayfs=False, prefix=None, prefix_profile=None
)) is None, "no prefix source must resolve to no prefix"

test = new_test()
assert test._resolve_prefix(SimpleNamespace(
    no_overlayfs=False, prefix="existing:/tmp/west-prefix", prefix_profile=None
)) == "/tmp/west-prefix", "the existing: marker must be stripped"
assert test._prefix_env == {}

test = new_test()
test._resolve_prefix(SimpleNamespace(
    no_overlayfs=True, prefix="/tmp/west-prefix", prefix_profile=None
))
assert test._prefix_env == {"DARLING_NOOVERLAYFS": "1"}

test = new_test(topdir="/workspace")
test._load_retained_prefix_env = lambda prefix: None
assert test._resolve_prefix(SimpleNamespace(
    no_overlayfs=False, prefix=None, prefix_profile="smoke"
)).endswith("darling-prefix-smoke")
assert test._prefix_env == {}

test = new_test(topdir="/workspace")
test._load_retained_prefix_env = lambda prefix: None
assert test._resolve_prefix(SimpleNamespace(
    no_overlayfs=False, prefix=None, prefix_profile="homebrew"
)).endswith("darling-prefix-homebrew-test")
assert test._prefix_env == {"DARLING_NOOVERLAYFS": "1"}

test = new_test()
expect_die(
    lambda: test._resolve_prefix(SimpleNamespace(
        no_overlayfs=False, prefix="/tmp/p", prefix_profile="homebrew"
    )),
    "--prefix and --prefix-profile are mutually exclusive",
)

previous = os.environ.get("DPREFIX")
os.environ["DPREFIX"] = "/tmp/west-dprefix"
try:
    test = new_test()
    assert test._resolve_prefix(SimpleNamespace(
        no_overlayfs=False, prefix=None, prefix_profile=None
    )) == "/tmp/west-dprefix"
finally:
    if previous is None:
        del os.environ["DPREFIX"]
    else:
        os.environ["DPREFIX"] = previous

# --- _darling_prefix_env --------------------------------------------------
test = new_test(_prefix_env={"DARLING_NOOVERLAYFS": "1"})
assert test._darling_prefix_env(Path("/tmp/west-prefix")) == {
    "DPREFIX": "/tmp/west-prefix",
    "DARLING_PREFIX": "/tmp/west-prefix",
    "DARLING_NOOVERLAYFS": "1",
}

# --- _load_retained_prefix_env -------------------------------------------
with tempfile.TemporaryDirectory(prefix="west-retained-") as raw:
    prefix = Path(raw)
    test = new_test(_ctest_runtime_profile_definitions=lambda: {"extra": PROFILE_DEFINITION})
    (prefix / RETAINED_RUNTIME_PROFILE_MARKER).write_text(
        json.dumps({"schema": 2, "profile": "extra", "source-profile": "homebrew"})
    )
    test._load_retained_prefix_env(prefix)
    assert test._prefix_env == {"DARLING_NOOVERLAYFS": "1"}, test._prefix_env

    test = new_test(_ctest_runtime_profile_definitions=lambda: {"extra": PROFILE_DEFINITION})
    (prefix / RETAINED_RUNTIME_PROFILE_MARKER).write_text(
        json.dumps({"schema": 2, "profile": "extra", "source-profile": "other"})
    )
    test._load_retained_prefix_env(prefix)
    assert test._prefix_env == {}, "a foreign retained profile must not contribute flags"

    test = new_test(_ctest_runtime_profile_definitions=lambda: {"extra": PROFILE_DEFINITION})
    (prefix / RETAINED_RUNTIME_PROFILE_MARKER).write_text("{not json")
    test._load_retained_prefix_env(prefix)
    assert test._prefix_env == {}

# --- _parse_file_mode ----------------------------------------------------
test = new_test()
assert test._parse_file_mode({"name": "fixture"}, "eunion-template-files", 0, 0o644) == 0o644
assert test._parse_file_mode({"name": "fixture"}, "eunion-template-files", 0, "755") == 0o755
expect_die(
    lambda: test._parse_file_mode({"name": "fixture"}, "eunion-template-files", 2, "nope"),
    "eunion-template-files[2] has invalid mode",
)

# --- _retained_runtime_profile -------------------------------------------
with tempfile.TemporaryDirectory(prefix="west-retained-profile-") as raw:
    root = Path(raw)
    prefix = root / "prefix"
    (prefix / "bin").mkdir(parents=True)
    launcher = prefix / "bin" / "darling"
    launcher.write_text("#!/bin/sh\n")

    test = new_test(
        topdir=str(root),
        manifest=SimpleNamespace(repo_abspath=str(root)),
        _ctest_runtime_profile_definitions=lambda: {"extra": PROFILE_DEFINITION},
    )
    expect_die(
        lambda: test._retained_runtime_profile("extra"),
        "--reuse-prefix-runtime requires --prefix or DPREFIX",
    )

    test._prefix = str(prefix)
    expect_die(
        lambda: test._retained_runtime_profile("extra"),
        "needs a retained provider marker",
    )
    marker = prefix / RETAINED_RUNTIME_PROFILE_MARKER
    marker.write_text(json.dumps({"schema": 2, "profile": "extra", "source-profile": "homebrew"}))

    original_identity = test_module.runtime_identity
    test_module.runtime_identity = lambda **_kwargs: "fingerprint"
    try:
        expect_die(
            lambda: test._retained_runtime_profile("extra"),
            "retained provider fingerprint mismatch",
        )

        marker.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "profile": "extra",
                    "source-profile": "homebrew",
                    "fingerprint": "fingerprint",
                }
            )
        )
        deployment = test._retained_runtime_profile("extra")
        assert isinstance(deployment, RuntimeProfileDeployment)
        assert deployment.name == "extra"
        assert deployment.prefix == prefix
        assert deployment.build_root == prefix
        assert deployment.env["DPREFIX"] == str(prefix)
        assert deployment.env["DARLING"] == str(launcher)
        assert deployment.env["DARLING_LAUNCHER"] == str(launcher)
        assert deployment.env["DARLING_NOOVERLAYFS"] == "1"

        marker.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "profile": "extra",
                    "source-profile": "homebrew",
                    "fingerprint": "different",
                }
            )
        )
        expect_die(
            lambda: test._retained_runtime_profile("extra"),
            "retained provider fingerprint mismatch",
        )
    finally:
        test_module.runtime_identity = original_identity

    test._ctest_runtime_profile_definitions = lambda: {}
    expect_die(
        lambda: test._retained_runtime_profile("extra"),
        "unknown retained runtime profile: extra",
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
        state = DarlingTest._bootstrap_runtime_state(prefix)
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

    lower = prefix / "libexec/darling/private/var/tmp/west-contract/template.txt"
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
    forbidden = prefix / "libexec/darling/private/var/tmp/west-contract/forbidden.txt"
    test._verify_eunion_forbidden_template_paths_after(
        {"name": "eunion"}, prefix, ["/private/var/tmp/west-contract/forbidden.txt"]
    )
    forbidden.write_text("created\n")
    try:
        test._verify_eunion_forbidden_template_paths_after(
            {"name": "eunion"}, prefix, ["/private/var/tmp/west-contract/forbidden.txt"]
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

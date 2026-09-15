"""Contract for the runtime plan/display mapping that moved out of ``test.py``.

``RuntimePlanMixin`` maps a runtime proof document onto the commands a user
sees and onto the prefix paths a deployment will touch, and it decides the
identity a reused runtime build is keyed on.  These assertions cover those
observable results, plus the facade seam the build still resolves its runner
through.
"""

from __future__ import annotations

import json
import os
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

from west_commands.test import DarlingTest  # noqa: E402

# Importing the facade puts ``west_commands`` on sys.path, so these top-level
# names are the very modules the facade resolves at call time.
import test_runtime as runtime_module  # noqa: E402
import test_runtime_cache as cache_module  # noqa: E402
import test_runtime_build as build_module  # noqa: E402
import test_runtime_deploy as deploy_module  # noqa: E402

PROOF = {
    "source-modules": ["darling"],
    "runtime-artifacts": [
        {
            "module": "darling/src/external/xnu",
            "build-targets": ["libsystem_kernel"],
            "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
        }
    ],
}


def new_test(**attributes) -> DarlingTest:
    test = DarlingTest.__new__(DarlingTest)
    test.inf = lambda _message: None
    test.err = lambda _message: None
    test.wrn = lambda _message: None

    def die(message: str):
        raise SystemExit(message)

    test.die = die
    for name, value in attributes.items():
        setattr(test, name, value)
    return test


# --- _runtime_red_deploy_targets ------------------------------------------
test = new_test()
assert test._runtime_red_deploy_targets(Path("/prefix"), "usr/lib/x.dylib") == [
    Path("/prefix/libexec/darling/usr/lib/x.dylib"),
    Path("/prefix/usr/lib/x.dylib"),
], "a usr runtime path must deploy to both prefix views"
assert test._runtime_red_deploy_targets(Path("/prefix"), "private/var/x") == [
    Path("/prefix/private/var/x")
]
for escaped in ("/abs/x.dylib", "../x.dylib"):
    try:
        test._runtime_red_deploy_targets(Path("/prefix"), escaped)
    except SystemExit as exc:
        assert "deploy path must be relative" in str(exc), exc
    else:
        raise AssertionError(f"deploy path {escaped!r} must be refused")

# --- _display_guest_runtime_deploy_plan -----------------------------------
assert test._display_guest_runtime_deploy_plan(PROOF) == (
    "guest-runtime-deploy sources:darling: "
    "darling/src/external/xnu[build:libsystem_kernel; "
    "deploy:usr/lib/system/libsystem_kernel.dylib]"
)
assert test._display_guest_runtime_deploy_plan(
    {"runtime-artifacts": [{"deploy": ["a"]}]}
) == "guest-runtime-deploy: <missing-module>[build:<missing-build-targets>; deploy:a]"

# --- _display_invocation --------------------------------------------------
test = new_test()
for fixture in (
    "darling_cmake_target_fixture",
    "guest_argv_fixture",
    "guest_macho_fixture",
    "guest_command_fixture",
):
    assert test._display_invocation(
        {"name": "case", "display": "displayed", fixture: True, "diag": "guarded"}
    ) == "displayed", fixture
assert test._display_invocation({"name": "case", "display": "bare display"}) == "bare display"
assert test._display_invocation(
    {"name": "case", "display": "bare display", "diag": "bare"}
) == "bare display"

test = new_test(_executor="/usr/bin/darling-debug-runner", _bundle_root="/bundles")
display = test._display_invocation(
    {
        "name": "guest case",
        "display": "<guest-c-fixture> run",
        "guest_c_fixture": True,
        "diag": "guarded",
        "timeout_seconds": 42,
    }
)
assert display == (
    "/usr/bin/darling-debug-runner run --name 'west-test-guest case' "
    "--bundle-root /bundles --timeout-seconds 42 -- '<guest-c-fixture>' "
    "'<guest-c-fixture> run'"
), display

test = new_test()
test._debug_runner_args = lambda invocation, display_only=False: ["runner", "run", "--x"]
assert test._display_invocation({"name": "case", "display": "d", "diag": "guarded"}) == (
    "runner run --x"
)

# --- _runtime_diagnostic_output -------------------------------------------
with tempfile.TemporaryDirectory(prefix="west-runtime-diagnostics-") as raw:
    root = Path(raw)
    host_trace = root / "host.log"
    host_trace.write_text("host\n")
    provider_trace = root / "provider.log"
    provider_trace.write_text("provider\n")
    test = new_test()
    assert test._runtime_diagnostic_output(
        {
            "_host_trace_paths": [str(host_trace)],
            "_runtime_diagnostic_trace_paths": [
                str(provider_trace),
                str(host_trace),
                str(root / "absent.log"),
            ],
        }
    ) == "host\nprovider\n", "each trace must be read once, in declaration order"

# --- _bound_runtime_reuse_store ------------------------------------------
calls: list[tuple] = []
original_prune = cache_module.prune
original_max_bytes = cache_module.max_bytes
staging = tempfile.TemporaryDirectory(prefix="west-runtime-reuse-")
pruned_store = Path(staging.name) / "store"
cache_module.prune = lambda store, max_bytes, protect: (
    calls.append((store, max_bytes, tuple(protect)))
    or {"evicted": ["entry"], "evicted_bytes": 2048}
)
cache_module.max_bytes = lambda environ: 4096
try:
    messages: list[str] = []
    test = new_test(inf=messages.append)
    plan = SimpleNamespace(
        store=pruned_store, source_entry=Path(staging.name) / "entry", build_key="k", source_key="s"
    )
    test._bound_runtime_reuse_store(plan)
    test._bound_runtime_reuse_store(plan)
finally:
    cache_module.prune = original_prune
    cache_module.max_bytes = original_max_bytes
    staging.cleanup()
assert calls == [(pruned_store, 4096, (Path(staging.name) / "entry",))], calls
assert messages == ["  runtime reuse evicted 1 entry(s), 2048 bytes"], messages

# --- _runtime_reuse_plan --------------------------------------------------
# The reuse key must change when the patch/omission inputs change, and must be
# stable while they do not: a verdict recorded for one runtime must not be
# reused for another.
original_cache_root = cache_module.cache_root
original_source_identity = cache_module.source_identity
original_source_key = cache_module.source_key
original_build_service = build_module.RuntimeBuildService
original_runtime_identity = runtime_module.runtime_identity
with tempfile.TemporaryDirectory(prefix="west-runtime-plan-") as raw:
    root = Path(raw)
    store = root / "store"

    class FakeBuildService:
        def __init__(self, owner):
            self.owner = owner

        def runtime_build_identity(self, proof, prefix, store, source_key, *, configure_args):
            self.proof = proof
            self.configure_args = configure_args
            return f"build:{source_key}"

    cache_module.cache_root = lambda manifest_repo, environ: store
    cache_module.source_identity = lambda identity, **kwargs: json.dumps(kwargs, sort_keys=True)
    cache_module.source_key = lambda identity: f"source:{identity}"
    build_module.RuntimeBuildService = FakeBuildService
    runtime_module.runtime_identity = lambda **kwargs: "identity"
    try:
        test = new_test(
            topdir=str(root),
            manifest=SimpleNamespace(repo_abspath=str(root)),
            _bad_revision=lambda patch, proof: "bad-revision",
        )
        base = dict(
            profile_name="extra",
            definition={"source-profile": "homebrew"},
            proof=PROOF,
            patch={"path": "x/y.patch"},
            prefix_text=str(root / "prefix"),
        )
        reused = test._runtime_reuse_plan(**base, omit_patch=False)
        again = test._runtime_reuse_plan(**base, omit_patch=False)
        omitted = test._runtime_reuse_plan(**base, omit_patch=True)
        assert reused == again, "an unchanged profile must reuse its runtime build"
        assert reused.store == store
        assert reused.source_key.startswith("source:")
        assert reused.build_key == f"build:{reused.source_key}"
        assert omitted.source_key != reused.source_key, (
            "a RED runtime that omits the patch must not reuse the GREEN build"
        )
        assert '"bad_revision": "bad-revision"' in omitted.source_key

        cache_module.cache_root = lambda manifest_repo, environ: None
        assert test._runtime_reuse_plan(**base, omit_patch=False) is None
        cache_module.cache_root = lambda manifest_repo, environ: store
    finally:
        cache_module.cache_root = original_cache_root
        cache_module.source_identity = original_source_identity
        cache_module.source_key = original_source_key
        build_module.RuntimeBuildService = original_build_service
        runtime_module.runtime_identity = original_runtime_identity

# --- _runtime_red_build_artifacts / _cmake_cache_value / macho helpers -----
# The build must be reachable through the facade seam so a focused contract can
# intercept execution, and it must carry the runner configuration the facade
# resolved.
recorded: dict = {}


class RecordingBuildService:
    def __init__(self, owner):
        recorded["owner"] = owner

    def build_artifacts(self, source_root, proof, prefix, scratch_root, **kwargs):
        recorded["args"] = (source_root, proof, prefix, scratch_root)
        recorded["kwargs"] = kwargs
        return Path("/built/artifact")

    @staticmethod
    def cmake_cache_value(build_dir, key):
        return f"{build_dir}:{key}"

    def configure_args(self, proof, prefix, scratch_root=None):
        return ["cmake", "-S", str(prefix)]

    def find_build_output(self, build_root, deploy_path):
        return Path(build_root) / deploy_path


class RecordingDeployService:
    def __init__(self, owner):
        recorded["deploy_owner"] = owner

    def macho_inspect(self, path, flag):
        return f"{flag}:{path}"

    def macho_dependencies(self, path):
        return [str(path)]

    def macho_dylib_providers(self, build_root, explicit):
        return {**explicit, "resolved": Path(build_root) / "resolved"}


original_build_service = build_module.RuntimeBuildService
original_deploy_service = deploy_module.RuntimeDeploymentService
build_module.RuntimeBuildService = RecordingBuildService
deploy_module.RuntimeDeploymentService = RecordingDeployService
try:
    test = new_test(_runtime_build_timeout_seconds=99)
    test._dump_command_tail = lambda label, result: None
    assert test._cmake_cache_value(Path("/build"), "KEY") == "/build:KEY"
    assert test._runtime_red_configure_args(PROOF, Path("/prefix")) == [
        "cmake",
        "-S",
        "/prefix",
    ]
    assert test._runtime_red_find_build_output(Path("/build"), "usr/lib/x") == Path(
        "/build/usr/lib/x"
    )
    built = test._runtime_red_build_artifacts(
        Path("/source"), PROOF, Path("/prefix"), Path("/scratch"), label="RED"
    )
    assert built == Path("/built/artifact")
    assert recorded["owner"] is test
    assert recorded["args"] == (Path("/source"), PROOF, Path("/prefix"), Path("/scratch"))
    assert recorded["kwargs"]["label"] == "RED"
    assert recorded["kwargs"]["timeout_seconds"] == 99, recorded["kwargs"]
    assert recorded["kwargs"]["runner"] == test._run_bounded, (
        "the build must run through the facade seam, not a module import"
    )
    assert recorded["kwargs"]["configure_args"] == test._runtime_red_configure_args

    assert test._runtime_macho_inspect(Path("/lib/x"), "--macho") == "--macho:/lib/x"
    assert test._runtime_macho_dependencies(Path("/lib/x")) == ["/lib/x"]
    assert test._runtime_macho_dylib_providers(
        Path("/build"), {"explicit": Path("/lib/e")}
    ) == {"explicit": Path("/lib/e"), "resolved": Path("/build/resolved")}
finally:
    build_module.RuntimeBuildService = original_build_service
    deploy_module.RuntimeDeploymentService = original_deploy_service

print("PASS runtime-plan-helpers-contract")

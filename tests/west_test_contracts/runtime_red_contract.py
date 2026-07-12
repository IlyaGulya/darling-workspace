import sys
import os
import io
import json
import signal
import shutil
import subprocess
import tempfile
import time
import types
from contextlib import contextmanager
from contextlib import redirect_stderr
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

import west_commands.test as west_test_module
import west_commands.test_guest_c as guest_c_module
from west_commands.test import DarlingTest, RuntimeBuildFailure
from west_commands.test_execution import ProcessResult, process_output_text
from west_commands.test_runtime import (
    compose_ctest_runtime_profiles,
    describe_runtime_deploy_plan,
    is_macho_binary,
    is_fat_macho_binary,
    load_rootless_bootstrap_manifest,
    load_ctest_runtime_profiles,
    merge_runtime_cmake_define_overrides,
    parse_macho_dylib_dependencies,
    parse_macho_dylib_id,
    parse_runtime_cmake_define_overrides,
    partition_ctest_runtime_profiles,
    runtime_artifact_deploy_paths,
    runtime_artifact_has_resource,
    runtime_build_targets,
    runtime_deploy_targets,
    ROOTLESS_BOOTSTRAP_CLOSURE_SOURCE_MODULES,
    ROOTLESS_BOOTSTRAP_RESOURCE,
    ROOTLESS_BOOTSTRAP_TARGET,
    resolve_macho_runtime_closure,
)

os.environ.setdefault("WEST_RUNTIME_MIN_FREE_BYTES", "0")


def make_test():
    test = DarlingTest.__new__(DarlingTest)
    test.topdir = str(Path.cwd())
    test.inf_messages = []
    test.err_messages = []
    test.inf = lambda message: test.inf_messages.append(message)
    test.wrn = lambda message: None
    test.err = lambda message: test.err_messages.append(message)
    test.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    test._active_profile = "homebrew"
    test._resolve_darling_launcher = lambda _prefix: None
    test._kill_dserver_for_prefix = lambda _prefix: None
    test._prefix_process_snapshot = lambda _prefix: []
    test._missing_requirements = lambda _invocation: []
    test._execution_env = lambda _invocation: {"DPREFIX": test._prefix}
    test._preflight_runtime_profile_stack = lambda *_args: None
    return test


assert parse_runtime_cmake_define_overrides(
    ["DARLING_GUEST_RECVSPIN=0", "DSERVER_TOOLS=ON"]
) == {"DARLING_GUEST_RECVSPIN": "0", "DSERVER_TOOLS": "ON"}
assert merge_runtime_cmake_define_overrides(
    {"DARLING_EUNION": True, "DARLING_GUEST_RECVSPIN": 512},
    {"DARLING_GUEST_RECVSPIN": "0"},
) == {"DARLING_EUNION": True, "DARLING_GUEST_RECVSPIN": "0"}
for invalid in ("DARLING_GUEST_RECVSPIN", "BAD-NAME=0", "DARLING_PATCH_PROFILE=perf"):
    try:
        parse_runtime_cmake_define_overrides([invalid])
    except ValueError:
        pass
    else:
        raise AssertionError(f"invalid runtime CMake override accepted: {invalid}")


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    workspace = tempdir / "workspace"
    source_module = tempdir / "source-module"
    workspace.mkdir()
    (source_module / "include").mkdir(parents=True)
    (source_module / "include" / "module_contract.h").write_text(
        "#pragma once\n#define WEST_SOURCE_ROOT_MODULE_VALUE 42\n"
    )
    script = workspace / "fixture.c"
    script.write_text(
        "#include <module_contract.h>\n"
        "int main(void) { return WEST_SOURCE_ROOT_MODULE_VALUE == 42 ? 0 : 1; }\n"
    )

    source_root_test = make_test()
    source_root_test._project_path = lambda ref: {
        "workspace": workspace,
        "source-module": source_module,
    }[ref]
    assert source_root_test._run_c_fixture(
        {
            "name": "source_root_module_contract",
            "diag": "bare",
            "cwd": workspace,
            "script_path": script,
            "cc": os.environ.get("CC", "cc"),
            "compile_flags": ["-std=gnu11", "-Wall", "-Wextra", "-Werror"],
            "fixture_include_dirs": [],
            "include_dirs": ["include"],
            "stub_headers": [],
            "generated_headers": {},
            "source_files": [],
            "source_root_module": "source-module",
        }
    ) == 0


proof_plan = {
    "bad-profile": "current-minus-patch",
    "source-modules": ["darling/src/external/darlingserver"],
    "runtime-artifacts": [
        {
            "module": "darling/src/external/xnu",
            "build-targets": ["system_kernel", "system_kernel"],
            "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
        },
        {
            "module": "darling/src/external/darlingserver",
            "build-targets": ["darlingserver"],
            "deploy": ["bin/darlingserver"],
        },
    ],
}
assert runtime_build_targets(proof_plan) == ["system_kernel", "darlingserver"]
assert describe_runtime_deploy_plan(proof_plan) == (
    "guest-runtime-deploy [current-minus-patch] "
    "sources:darling/src/external/darlingserver: "
    "darling/src/external/xnu[build:system_kernel,system_kernel; "
    "deploy:usr/lib/system/libsystem_kernel.dylib]; "
    "darling/src/external/darlingserver[build:darlingserver; deploy:bin/darlingserver]"
)
prefix_for_targets = Path("/tmp/prefix")
assert runtime_deploy_targets(prefix_for_targets, "usr/lib/system/libsystem_kernel.dylib") == [
    prefix_for_targets / "libexec/darling/usr/lib/system/libsystem_kernel.dylib",
    prefix_for_targets / "usr/lib/system/libsystem_kernel.dylib",
]
assert runtime_deploy_targets(prefix_for_targets, "bin/darlingserver") == [
    prefix_for_targets / "bin/darlingserver",
]
assert runtime_deploy_targets(prefix_for_targets, "sbin/launchd") == [
    prefix_for_targets / "sbin/launchd",
]
with tempfile.TemporaryDirectory() as temp:
    profiles_path = Path(temp) / "runtime-profiles.yml"
    profiles_path.write_text(
        "runtime-profiles:\n"
        "  homebrew:\n"
        "    source-profile: homebrew\n"
        "    source-module: darling/src/external/xnu\n"
        "    source-modules:\n"
        "    - darling\n"
        "    - darling/src/external/darlingserver\n"
        "    - darling/src/external/xnu\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [system_kernel]\n"
        "      deploy: [usr/lib/system/libsystem_kernel.dylib]\n"
    )
    assert load_ctest_runtime_profiles(profiles_path)["homebrew"]["source-modules"] == [
        "darling",
        "darling/src/external/darlingserver",
        "darling/src/external/xnu",
    ]
    profiles_path.write_text(
        profiles_path.read_text().replace("    - darling\n", "")
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "must materialize darling" in str(exc), exc
    else:
        raise AssertionError("system_kernel runtime profile accepted mixed live darlingserver source")


runtime_profiles = {
    "kernel": {
        "source-profile": "homebrew",
        "source-module": "darling/src/external/xnu",
        "source-modules": ["darling", "darling/src/external/xnu"],
        "runtime-artifacts": [{"build-targets": ["system_kernel"], "deploy": ["usr/lib/system/libsystem_kernel.dylib"]}],
    },
    "server": {
        "source-profile": "homebrew",
        "source-module": "darling/src/external/darlingserver",
        "source-modules": ["darling", "darling/src/external/darlingserver"],
        "runtime-artifacts": [{"build-targets": ["darlingserver"], "deploy": ["bin/darlingserver"]}],
    },
    "other": {
        "source-profile": "arch",
        "source-module": "darling",
        "source-modules": ["darling"],
        "runtime-artifacts": [{"build-targets": ["other"], "deploy": ["bin/other"]}],
    },
    "rootless": {
        "source-profile": "homebrew",
        "source-module": "darling",
        "source-modules": ["darling", "darling/src/external/darlingserver", "darling/src/external/xnu"],
        "runtime-artifacts": [{"build-targets": ["darling"], "deploy": ["bin/darling"]}],
        "launcher-env": {"DARLING_ROOTLESS": "1", "DARLING_NOOVERLAYFS": "1"},
    },
}
combined_runtime = compose_ctest_runtime_profiles(runtime_profiles, ["kernel", "server", "kernel"])
assert combined_runtime == {
    "name": "kernel+server",
    "source-profile": "homebrew",
    "source-module": "darling/src/external/xnu",
    "source-modules": [
        "darling",
        "darling/src/external/xnu",
        "darling/src/external/darlingserver",
    ],
    "runtime-artifacts": [
        {"build-targets": ["system_kernel"], "deploy": ["usr/lib/system/libsystem_kernel.dylib"]},
        {"build-targets": ["darlingserver"], "deploy": ["bin/darlingserver"]},
    ],
}
try:
    compose_ctest_runtime_profiles(runtime_profiles, ["kernel", "other"])
except ValueError as exc:
    assert "incompatible runtime source profiles" in str(exc), exc
else:
    raise AssertionError("incompatible runtime profiles were combined")
configured_profiles = {
    **runtime_profiles,
    "configured": {
        "source-profile": "homebrew",
        "source-module": "darling/src/external/darlingserver",
        "source-modules": ["darling", "darling/src/external/darlingserver"],
        "runtime-artifacts": [{"build-targets": ["configured"], "deploy": ["bin/configured"]}],
        "cmake-defines": {"DSERVER_RING_TRANSPORT": True},
    },
    "configured-conflict": {
        "source-profile": "homebrew",
        "source-module": "darling/src/external/darlingserver",
        "source-modules": ["darling", "darling/src/external/darlingserver"],
        "runtime-artifacts": [{"build-targets": ["other"], "deploy": ["bin/other"]}],
        "cmake-defines": {"DSERVER_RING_TRANSPORT": False},
    },
}
configured_runtime = compose_ctest_runtime_profiles(
    configured_profiles, ["kernel", "configured"]
)
assert configured_runtime["cmake-defines"] == {"DSERVER_RING_TRANSPORT": True}
try:
    compose_ctest_runtime_profiles(
        configured_profiles, ["configured", "configured-conflict"]
    )
except ValueError as exc:
    assert "conflicts on CMake definition" in str(exc), exc
else:
    raise AssertionError("conflicting runtime CMake definitions were accepted")
rootless_runtime = compose_ctest_runtime_profiles(runtime_profiles, ["rootless"])
assert rootless_runtime["launcher-env"] == {
    "DARLING_ROOTLESS": "1",
    "DARLING_NOOVERLAYFS": "1",
}

actual_runtime_profiles = load_ctest_runtime_profiles(ROOT / "testkit/runtime-profiles.yml")
rootless_provider = actual_runtime_profiles["homebrew-rootless-no-mount"]
assert rootless_provider["bootstrap"] == "rootless-no-mount"
assert rootless_provider["cmake-defines"] == {"DARLING_EUNION": True}
assert rootless_provider["launcher-env"] == {
    "DARLING_ROOTLESS": "1",
    "DARLING_NOOVERLAYFS": "1",
    "DARLING_EUNION": "1",
}
assert any(
    runtime_artifact_has_resource(artifact, ROOTLESS_BOOTSTRAP_RESOURCE)
    for artifact in rootless_provider["runtime-artifacts"]
)
assert rootless_provider["runtime-artifacts"] == [
    {
        "module": "darling",
        "build-targets": [ROOTLESS_BOOTSTRAP_TARGET],
        "resource": ROOTLESS_BOOTSTRAP_RESOURCE,
    }
]
assert "darling/src/external/dyld" in rootless_provider["source-modules"]
assert ROOTLESS_BOOTSTRAP_CLOSURE_SOURCE_MODULES.issubset(
    rootless_provider["source-modules"]
)
baseline_provider = actual_runtime_profiles["homebrew-prefix-baseline"]
assert baseline_provider["purpose"] == "prefix-baseline"
assert baseline_provider["bootstrap"] == "rootless-no-mount"
assert baseline_provider["bootstrap-smoke-timeout-seconds"] == 20
assert baseline_provider["launcher-env"] == rootless_provider["launcher-env"]
assert "darling/src/external/dyld" in baseline_provider["source-modules"]
assert ROOTLESS_BOOTSTRAP_CLOSURE_SOURCE_MODULES.issubset(
    baseline_provider["source-modules"]
)
assert any(
    runtime_artifact_has_resource(artifact, ROOTLESS_BOOTSTRAP_RESOURCE)
    for artifact in baseline_provider["runtime-artifacts"]
)

with tempfile.TemporaryDirectory() as temp:
    profiles_path = Path(temp) / "runtime-profiles.yml"
    profiles_path.write_text(
        "runtime-profiles:\n"
        "  incomplete-rootless:\n"
        "    source-profile: homebrew\n"
        "    source-module: darling\n"
        "    source-modules: [darling, darling/src/external/darlingserver, darling/src/external/xnu, darling/src/external/dyld, darling/src/external/corefoundation, darling/src/external/libsystem]\n"
        "    bootstrap: rootless-no-mount\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [darling]\n"
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "missing runtime resource(s): rootless-bootstrap" in str(exc), exc
    else:
        raise AssertionError("incomplete rootless runtime provider was accepted")

with tempfile.TemporaryDirectory() as temp:
    profiles_path = Path(temp) / "runtime-profiles.yml"
    profiles_path.write_text(
        "runtime-profiles:\n"
        "  mixed-rootless:\n"
        "    source-profile: homebrew\n"
        "    source-module: darling\n"
        "    source-modules: [darling, darling/src/external/darlingserver, darling/src/external/xnu, darling/src/external/corefoundation, darling/src/external/libsystem]\n"
        "    bootstrap: rootless-no-mount\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [rootless_bootstrap]\n"
        "      resource: rootless-bootstrap\n"
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "must materialize bootstrap source module(s)" in str(exc), exc
        assert "darling/src/external/dyld" in str(exc), exc
    else:
        raise AssertionError("rootless runtime provider accepted a live dyld source")

with tempfile.TemporaryDirectory() as temp:
    profiles_path = Path(temp) / "runtime-profiles.yml"
    profiles_path.write_text(
        "runtime-profiles:\n"
        "  incomplete-resource-rootless:\n"
        "    source-profile: homebrew\n"
        "    source-module: darling\n"
        "    source-modules: [darling, darling/src/external/darlingserver, darling/src/external/xnu, darling/src/external/dyld, darling/src/external/corefoundation, darling/src/external/libsystem]\n"
        "    bootstrap: rootless-no-mount\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [darling]\n"
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "missing runtime resource(s): rootless-bootstrap" in str(exc), exc
    else:
        raise AssertionError("rootless runtime provider accepted without its system closure")

assert runtime_artifact_deploy_paths(
    {"resource": ROOTLESS_BOOTSTRAP_RESOURCE}
) == []
try:
    runtime_artifact_deploy_paths({"resource": "system-closure"})
except ValueError as exc:
    assert "unknown runtime artifact resource" in str(exc), exc
else:
    raise AssertionError("obsolete static system closure resource was accepted")

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    launchd = tempdir / "launchd"
    libsystem = tempdir / "libSystem.B.dylib"
    libobjc = tempdir / "libobjc.A.dylib"
    for path in (launchd, libsystem, libobjc):
        path.write_bytes(b"\xcf\xfa\xed\xfeMach-O fixture\n")
    assert is_macho_binary(launchd)
    assert not is_fat_macho_binary(launchd)
    assert not is_macho_binary(tempdir / "missing")
    assert parse_macho_dylib_id(
        "/tmp/build/libobjc.A.dylib:\n/usr/lib/libobjc.A.dylib\n"
    ) == "/usr/lib/libobjc.A.dylib"
    assert parse_macho_dylib_dependencies(
        "fixture:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
    ) == ["/usr/lib/libSystem.B.dylib"]
    dependencies = {
        launchd: ["/usr/lib/libSystem.B.dylib"],
        libsystem: ["/usr/lib/libobjc.A.dylib"],
        libobjc: ["/usr/lib/libobjc.A.dylib"],
    }
    closure = resolve_macho_runtime_closure(
        {"/usr/libexec/darling/launchd": launchd},
        {
            "/usr/lib/libSystem.B.dylib": libsystem,
            "/usr/lib/libobjc.A.dylib": libobjc,
        },
        dependencies.__getitem__,
    )
    assert closure == {
        "/usr/libexec/darling/launchd": launchd,
        "/usr/lib/libSystem.B.dylib": libsystem,
        "/usr/lib/libobjc.A.dylib": libobjc,
    }
    try:
        resolve_macho_runtime_closure(
            {"/usr/libexec/darling/launchd": launchd},
            {"/usr/lib/libSystem.B.dylib": libsystem},
            dependencies.__getitem__,
        )
    except ValueError as exc:
        assert "no built provider" in str(exc), exc
        assert "libobjc.A.dylib" in str(exc), exc
    else:
        raise AssertionError("closure accepted an unresolved guest dylib")
    try:
        resolve_macho_runtime_closure(
            {"/usr/libexec/darling/launchd": launchd},
            {},
            lambda _path: ["@rpath/libmissing.dylib"],
        )
    except ValueError as exc:
        assert "non-absolute Mach-O dependency" in str(exc), exc
    else:
        raise AssertionError("closure accepted an unresolvable rpath dependency")

with tempfile.TemporaryDirectory() as temp:
    build_root = Path(temp)
    framework = build_root / "CoreFoundation"
    thin_framework = build_root / "CoreFoundation_x86_64"
    executable = build_root / "launchd"
    for path, magic in (
        (thin_framework, b"\xcf\xfa\xed\xfe"),
        (executable, b"\xcf\xfa\xed\xfe"),
        (framework, b"\xca\xfe\xba\xbe"),
    ):
        path.write_bytes(magic + b"Mach-O fixture\n")
    assert is_fat_macho_binary(framework)
    provider_test = make_test()
    install_names = {
        framework: "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        thin_framework: "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        executable: None,
    }
    provider_test._runtime_macho_inspect = lambda path, _mode: (
        f"{path}:\n{install_names[path]}\n" if install_names[path] else f"{path}:\n"
    )
    assert provider_test._runtime_macho_dylib_providers(build_root) == {
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation": framework
    }

with tempfile.TemporaryDirectory() as temp:
    build_root = Path(temp) / "build"
    output = build_root / "bin" / "darling"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"\xcf\xfa\xed\xfeMach-O fixture\n")
    output.chmod(0o755)
    manifest_path = build_root / "darling-rootless-bootstrap.json"

    def write_bootstrap_manifest(entries):
        manifest_path.write_text(json.dumps({"schema": 1, "entrypoints": entries}))

    entry = {
        "target": "darling",
        "guest_path": "/bin/darling",
        "host_path": str(output),
    }
    write_bootstrap_manifest([entry])
    assert load_rootless_bootstrap_manifest(build_root) == {"bin/darling": output}

    write_bootstrap_manifest([entry, entry])
    try:
        load_rootless_bootstrap_manifest(build_root)
    except ValueError as exc:
        assert "duplicate guest path" in str(exc), exc
    else:
        raise AssertionError("rootless bootstrap manifest accepted duplicate guest paths")

    escaped = {**entry, "host_path": str(Path(temp) / "outside")}
    write_bootstrap_manifest([escaped])
    try:
        load_rootless_bootstrap_manifest(build_root)
    except ValueError as exc:
        assert "escapes build root" in str(exc), exc
    else:
        raise AssertionError("rootless bootstrap manifest accepted an escaping host path")

    host_launcher = build_root / "bin" / "host-launcher"
    host_launcher.write_text("not a Mach-O binary\n")
    host_launcher.chmod(0o755)
    write_bootstrap_manifest([{**entry, "host_path": str(host_launcher)}])
    assert load_rootless_bootstrap_manifest(build_root) == {"bin/darling": host_launcher}

    non_executable = build_root / "bin" / "not-executable"
    non_executable.write_text("not executable\n")
    write_bootstrap_manifest([{**entry, "host_path": str(non_executable)}])
    try:
        load_rootless_bootstrap_manifest(build_root)
    except ValueError as exc:
        assert "not a built executable" in str(exc), exc
    else:
        raise AssertionError("rootless bootstrap manifest accepted a non-executable product")

with tempfile.TemporaryDirectory() as temp:
    profiles_path = Path(temp) / "runtime-profiles.yml"
    profiles_path.write_text(
        "runtime-profiles:\n"
        "  incomplete-closure-rootless:\n"
        "    source-profile: homebrew\n"
        "    source-module: darling\n"
        "    source-modules: [darling, darling/src/external/darlingserver, darling/src/external/xnu, darling/src/external/dyld, darling/src/external/corefoundation, darling/src/external/libsystem]\n"
        "    bootstrap: rootless-no-mount\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [darling]\n"
        "      resource: rootless-bootstrap\n"
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "resource must build only 'rootless_bootstrap'" in str(exc), exc
    else:
        raise AssertionError("rootless closure accepted without its objc build target")

with tempfile.TemporaryDirectory() as temp:
    profiles_path = Path(temp) / "runtime-profiles.yml"
    profiles_path.write_text(
        "runtime-profiles:\n"
        "  live-closure-owner-rootless:\n"
        "    source-profile: homebrew\n"
        "    source-module: darling\n"
        "    source-modules: [darling, darling/src/external/darlingserver, darling/src/external/xnu, darling/src/external/dyld, darling/src/external/libsystem]\n"
        "    bootstrap: rootless-no-mount\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [rootless_bootstrap]\n"
        "      resource: rootless-bootstrap\n"
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "darling/src/external/corefoundation" in str(exc), exc
    else:
        raise AssertionError("rootless closure accepted a live libsystem source")

with tempfile.TemporaryDirectory() as temp:
    profiles_path = Path(temp) / "runtime-profiles.yml"
    profiles_path.write_text(
        "runtime-profiles:\n"
        "  invalid-baseline:\n"
        "    purpose: prefix-baseline\n"
        "    source-profile: homebrew\n"
        "    source-module: darling\n"
        "    source-modules: [darling]\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [darling]\n"
        "      deploy: [bin/darling]\n"
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "prefix-baseline must use rootless-no-mount" in str(exc), exc
    else:
        raise AssertionError("privileged prefix baseline was accepted")

    profiles_path.write_text(
        "runtime-profiles:\n"
        "  invalid-timeout:\n"
        "    source-profile: homebrew\n"
        "    source-module: darling\n"
        "    source-modules: [darling]\n"
        "    bootstrap-smoke-timeout-seconds: 0\n"
        "    runtime-artifacts:\n"
        "    - build-targets: [darling]\n"
        "      deploy: [bin/darling]\n"
    )
    try:
        load_ctest_runtime_profiles(profiles_path)
    except ValueError as exc:
        assert "bootstrap-smoke-timeout-seconds must be a positive integer" in str(exc), exc
    else:
        raise AssertionError("zero bootstrap smoke timeout was accepted")

try:
    partition_ctest_runtime_profiles(
        actual_runtime_profiles,
        [{
            "name": "darling/invalid-baseline-use",
            "darling": True,
            "profiles": ["homebrew-prefix-baseline"],
        }],
    )
except ValueError as exc:
    assert "cannot select prefix-baseline" in str(exc), exc
else:
    raise AssertionError("CTest test accepted a durable prefix baseline provider")

groups = partition_ctest_runtime_profiles(
    runtime_profiles,
    [
        {"name": "darling/kernel", "darling": True, "profiles": ["kernel"]},
        {"name": "darling/server", "darling": True, "profiles": ["server"]},
        {"name": "darling/perf", "darling": True, "profiles": ["other"]},
        {"name": "host/plain", "darling": False, "profiles": []},
    ],
)
assert [(group["source-profile"], group["profiles"], group["tests"]) for group in groups] == [
    ("homebrew", ["kernel", "server"], ["darling/kernel", "darling/server"]),
    ("arch", ["other"], ["darling/perf"]),
    (None, [], ["host/plain"]),
]
groups = partition_ctest_runtime_profiles(
    runtime_profiles,
    [{"name": "darling/kernel", "darling": True, "profiles": ["kernel"]}],
    ["server"],
)
assert groups[0]["profiles"] == ["kernel", "server"]
try:
    partition_ctest_runtime_profiles(
        runtime_profiles,
        [{"name": "darling/missing", "darling": True, "profiles": []}],
    )
except ValueError as exc:
    assert "needs an explicit runtime-profile" in str(exc), exc
else:
    raise AssertionError("guest CTest test without a runtime provider was accepted")
try:
    partition_ctest_runtime_profiles(
        runtime_profiles,
        [{"name": "darling/kernel", "darling": True, "profiles": ["kernel"]}],
        ["other"],
    )
except ValueError as exc:
    assert "does not match any selected" in str(exc), exc
else:
    raise AssertionError("incompatible additional provider was accepted")
deploy_test = make_test()
deploy_test._resolve_darling_launcher = lambda _prefix: "/opt/darling-test/bin/darling"
with tempfile.TemporaryDirectory() as temp:
    closure_build = Path(temp)
    libsystem = closure_build / "libSystem.B.dylib"
    libsystem.write_text("closure anchor\n")
    assert deploy_test._runtime_red_find_build_output(
        closure_build, "usr/lib/libSystem.B.dylib"
    ) == libsystem
    try:
        deploy_test._runtime_red_find_build_output(
            closure_build, "usr/lib/system/libkxld.dylib"
        )
    except SystemExit as exc:
        assert "built artifact not found" in str(exc), exc
    else:
        raise AssertionError("missing runtime deployment artifact was accepted")
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "bin/darlingserver",
) == [
    prefix_for_targets / "bin/darlingserver",
]
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "usr/libexec/darling/mldr",
) == [
    prefix_for_targets / "libexec/darling/usr/libexec/darling/mldr",
    prefix_for_targets / "usr/libexec/darling/mldr",
]
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "usr/lib/dyld",
) == [
    prefix_for_targets / "libexec/darling/usr/lib/dyld",
    prefix_for_targets / "usr/lib/dyld",
]
assert deploy_test._runtime_red_deploy_targets(
    prefix_for_targets,
    "usr/lib/system/libsystem_kernel.dylib",
) == [
    prefix_for_targets / "libexec/darling/usr/lib/system/libsystem_kernel.dylib",
    prefix_for_targets / "usr/lib/system/libsystem_kernel.dylib",
]
try:
    runtime_deploy_targets(prefix_for_targets, "/absolute/bad")
except ValueError as exc:
    assert "must be relative" in str(exc)
else:
    raise AssertionError("absolute deploy path was accepted")

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    prefix = root / "prefix"
    deployed = prefix / "bin" / "darling"
    deployed.parent.mkdir(parents=True)
    deployed.write_text("old launcher\n")
    build_root = root / "build"
    artifact = build_root / "src" / "startup" / "darling"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("new launcher\n")
    proof = {"runtime-artifacts": [{"deploy": ["bin/darling"]}]}

    test = make_test()
    test._shutdown_runtime_prefix = lambda _prefix: True
    with test._runtime_red_deployed_artifacts(
        proof, build_root, prefix, label="bootstrap", restore_deployment=False
    ):
        assert deployed.read_text() == "new launcher\n"
    assert deployed.read_text() == "new launcher\n"

    deployed.write_text("old launcher\n")
    with test._runtime_red_deployed_artifacts(
        proof, build_root, prefix, label="CTest", restore_deployment=True
    ):
        assert deployed.read_text() == "new launcher\n"
    assert deployed.read_text() == "old launcher\n"

    try:
        with test._runtime_red_deployed_artifacts(
            proof, build_root, prefix, label="failed bootstrap", restore_deployment=False
        ):
            assert deployed.read_text() == "new launcher\n"
            raise RuntimeError("guest smoke failed")
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed bootstrap deployment unexpectedly passed")
    assert deployed.read_text() == "old launcher\n"

with tempfile.TemporaryDirectory() as temp:
    bundle_root = Path(temp)
    bundle = bundle_root / "20260709T000000Z-west-test-runtime_red_reason"
    bundle.mkdir()
    (bundle / "stdout.log").write_text("old runtime failure\nerrno=14\n")
    (bundle / "stderr.log").write_text("WEST_GUEST_STAGE=run\n")
    (bundle / "exit-status.txt").write_text("exit status: 1\n")

    test = make_test()
    test._bundle_root = str(bundle_root)
    invocation = {"name": "runtime_red_reason"}
    assert test._check_guest_runtime_red_failure(
        {"expect-output-contains": ["old runtime failure", "errno=14"]},
        invocation,
        since=time.time() - 10,
    )
    assert not test._check_guest_runtime_red_failure(
        {"expect-output-contains": ["different failure"]},
        invocation,
        since=time.time() - 10,
    )

test = make_test()
test._bundle_root = "/tmp/west-test-contract-no-bundles"
invocation = {"name": "runtime_red_reason_without_bundle"}
assert test._check_guest_runtime_red_failure(
    {"expect-output-contains": ["captured old runtime symptom"]},
    invocation,
    since=time.time() - 10,
    captured_output="runner stderr\ncaptured old runtime symptom\n",
)
assert not test._check_guest_runtime_red_failure(
    {"expect-output-contains": ["different failure"]},
    invocation,
    since=time.time() - 10,
    captured_output="runner stderr\ncaptured old runtime symptom\n",
)

with tempfile.TemporaryDirectory() as temp:
    trace = Path(temp) / "shellspawn.trace"
    trace.write_text("shellspawn-error errno=9\n")
    test = make_test()
    test._bundle_root = "/tmp/west-test-contract-no-bundles"
    assert test._check_guest_runtime_red_failure(
        {"expect-output-contains": ["shellspawn-error errno=9"]},
        {"name": "runtime_red_host_trace", "_host_trace_paths": [trace]},
        since=time.time() - 10,
        captured_output="runner stderr\n",
    )

with tempfile.TemporaryDirectory() as temp:
    trace = Path(temp) / "guest-fd.trace"
    trace.write_text("shellspawn-error errno=9\n")
    test = make_test()
    test._bundle_root = "/tmp/west-test-contract-no-bundles"
    invocation = test._with_runtime_diagnostics(
        {"name": "runtime_red_guest_fd_trace"},
        types.SimpleNamespace(diagnostic_trace_paths=(trace,)),
    )
    assert test._check_guest_runtime_red_failure(
        {"expect-output-contains": ["shellspawn-error errno=9"]},
        invocation,
        since=time.time() - 10,
        captured_output="runner stderr\n",
    )

test = make_test()
assert not test._guest_runtime_red_has_positive_reason({})
assert not test._guest_runtime_red_has_positive_reason({"expect-output-contains": []})
assert not test._guest_runtime_red_has_positive_reason({"expect-output-contains": [""]})
assert test._guest_runtime_red_has_positive_reason({"expect-output-contains": "old runtime symptom"})
assert test._guest_runtime_red_has_positive_reason({"expect-output-contains": ["old runtime symptom"]})
assert test._check_red_failure_phase(
    {"expect-failure-phase": "compile"},
    {"name": "phase_contract"},
    "compile",
)
assert test._check_red_failure_phase(
    {"expect-failure-phase": ["compile", "run"]},
    {"name": "phase_contract"},
    "run",
)
assert not test._check_red_failure_phase(
    {"expect-failure-phase": "run"},
    {"name": "phase_contract"},
    "compile",
)
missing_reasons = test._red_proof_audit(
    [
        (
            {"path": "xnu/missing.patch"},
            {
                "name": "missing_reason",
                "red-proof": {"mode": "guest-runtime-deploy"},
            },
        ),
        (
            {"path": "xnu/with_reason.patch"},
            {
                "name": "with_reason",
                "red-proof": {
                    "mode": "guest-runtime-deploy",
                    "expect-failure-phase": "run",
                    "expect-output-contains": ["old runtime symptom"],
                },
            },
        ),
        (
            {"path": "xnu/source_base.patch"},
            {
                "name": "source_base",
                "red-proof": {"mode": "source-base"},
            },
        ),
    ]
)
assert missing_reasons == [
    "xnu/missing.patch: missing_reason "
    "RED proof needs expect-failure-phase",
    "xnu/missing.patch: missing_reason RED proof needs expect-output-contains",
    "xnu/source_base.patch: source_base RED proof needs expect-failure-phase",
], missing_reasons

test = make_test()
test._prefix = "/tmp/prefix"
try:
    test._run_guest_runtime_deploy_proof(
        {"path": "xnu/missing.patch"},
        {"mode": "guest-runtime-deploy", "runtime-artifacts": []},
        {"name": "missing_reason", "guest_c_fixture": True},
    )
except SystemExit as exc:
    assert "guest-runtime-deploy RED proof needs expect-output-contains" in str(exc)
else:
    raise AssertionError("guest-runtime-deploy proof accepted without reason matcher")


test = make_test()
test._prefix = None
try:
    test._run_guest_runtime_deploy_proof(
        {"path": "darling/bootstrap.patch"},
        {
            "mode": "guest-runtime-deploy",
            "expect-output-contains": ["old bootstrap failure"],
        },
        {
            "name": "runtime_script_runner",
            "runner": "guest-runtime-script",
            "requires_resources": ["darling-prefix"],
        },
    )
except SystemExit as exc:
    assert "guest-runtime-deploy needs a Darling prefix" in str(exc), exc
else:
    raise AssertionError("guest-runtime-script was rejected before runtime deployment")


with tempfile.TemporaryDirectory() as temp:
    test = make_test()
    test._prefix = str(Path(temp) / "prefix")
    preflight_labels = []
    test._require_runtime_scratch_space = lambda label: (
        preflight_labels.append(label), test.die("runtime scratch unavailable")
    )[1]
    original_mkdtemp = west_test_module.tempfile.mkdtemp
    west_test_module.tempfile.mkdtemp = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("runtime RED scratch was created before capacity preflight")
    )
    try:
        try:
            test._run_guest_runtime_deploy_proof(
                {"path": "darling/example.patch"},
                {
                    "mode": "guest-runtime-deploy",
                    "expect-output-contains": ["expected old runtime symptom"],
                },
                {"name": "runtime_capacity_preflight", "guest_c_fixture": True},
            )
        except SystemExit as exc:
            assert str(exc) == "runtime scratch unavailable", exc
        else:
            raise AssertionError("runtime RED capacity preflight unexpectedly passed")
    finally:
        west_test_module.tempfile.mkdtemp = original_mkdtemp
    assert preflight_labels == [
        "darling/example.patch: runtime_capacity_preflight RED"
    ], preflight_labels

    green_labels = []
    test._require_runtime_scratch_space = lambda label: (
        green_labels.append(label), test.die("runtime scratch unavailable")
    )[1]
    try:
        test._run_guest_runtime_deploy_green(
            {"path": "darling/example.patch"},
            {"mode": "guest-runtime-deploy"},
            {"name": "runtime_capacity_preflight"},
        )
    except SystemExit as exc:
        assert str(exc) == "runtime scratch unavailable", exc
    else:
        raise AssertionError("runtime GREEN capacity preflight unexpectedly passed")
    assert green_labels == [
        "darling/example.patch: runtime_capacity_preflight GREEN"
    ], green_labels

    profile_test = make_test()
    profile_test._prefix = str(Path(temp) / "prefix")
    profile_test._active_profile = "homebrew"
    profile_test._require_runtime_scratch_space = lambda _label: None
    profile_test._preflight_runtime_profile_stack = lambda *_args: profile_test.die(
        "profile stack unavailable"
    )
    original_mkdtemp = west_test_module.tempfile.mkdtemp
    west_test_module.tempfile.mkdtemp = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("runtime scratch was created before profile applicability preflight")
    )
    try:
        try:
            profile_test._run_guest_runtime_deploy_proof(
                {"path": "darling/example.patch"},
                {
                    "mode": "guest-runtime-deploy",
                    "expect-output-contains": ["expected old runtime symptom"],
                },
                {"name": "runtime_profile_preflight", "guest_c_fixture": True},
            )
        except SystemExit as exc:
            assert str(exc) == "profile stack unavailable", exc
        else:
            raise AssertionError("runtime RED profile preflight unexpectedly passed")
        try:
            profile_test._run_guest_runtime_deploy_green(
                {"path": "darling/example.patch"},
                {"mode": "guest-runtime-deploy"},
                {"name": "runtime_profile_preflight"},
            )
        except SystemExit as exc:
            assert str(exc) == "profile stack unavailable", exc
        else:
            raise AssertionError("runtime GREEN profile preflight unexpectedly passed")
    finally:
        west_test_module.tempfile.mkdtemp = original_mkdtemp


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp) / "prefix"
    readiness = prefix / "private/var/tmp/runtime-ready"
    readiness.parent.mkdir(parents=True)
    readiness.write_text("stale\n")
    source_root = Path(temp) / "source"
    source_root.mkdir()
    resource_scope_test = make_test()
    resource_scope_test._prefix = str(prefix)
    resource_scope_test._require_runtime_scratch_space = lambda _label: None

    @contextmanager
    def source_forest(*_args, **_kwargs):
        assert not readiness.exists(), "runtime resources started after source preparation"
        yield source_root

    @contextmanager
    def deployed_artifacts(*_args, **_kwargs):
        yield

    resource_scope_test._guest_runtime_source_forest = source_forest
    resource_scope_test._runtime_red_build_artifacts = lambda *_args, **_kwargs: Path(temp) / "build"
    resource_scope_test._runtime_red_deployed_artifacts = deployed_artifacts

    def run_runtime_invocation(_invocation, env):
        assert env["WEST_READY_FILE"] == "/private/var/tmp/runtime-ready"
        readiness.write_text("ready\n")
        return 0

    resource_scope_test._run_invocation = run_runtime_invocation
    resource_scope_test._check_guest_runtime_green_success = lambda *_args, **_kwargs: True
    assert resource_scope_test._run_guest_runtime_deploy_green(
        {"path": "darling/example.patch"},
        {"mode": "guest-runtime-deploy"},
        {
            "name": "runtime_resource_scope",
            "host_temp_files": [
                {
                    "env": "WEST_READY_FILE",
                    "prefix-relative-path": "private/var/tmp/runtime-ready",
                    "guest-path": True,
                }
            ],
        },
    ) == 0
    assert not readiness.exists(), "runtime resource cleanup did not remove readiness file"


profile_test = make_test()
profile_test._preflight_runtime_profile_stack = (
    DarlingTest._preflight_runtime_profile_stack.__get__(profile_test, DarlingTest)
)
profile_test._profile_stack = lambda profile: ["homebrew", profile]
profile_calls = []
original_bounded = west_test_module.run_bounded
west_test_module.run_bounded = lambda args, **kwargs: (
    profile_calls.append((args, kwargs)) or ProcessResult(0)
)
try:
    profile_test._preflight_runtime_profile_stack("arch", "contract runtime")
    profile_test._preflight_runtime_profile_stack("arch", "contract runtime")
finally:
    west_test_module.run_bounded = original_bounded
assert [call[0] for call in profile_calls] == [
    ["west", "patch", "verify", "--profile", "homebrew"],
    ["west", "patch", "verify", "--profile", "arch"],
], profile_calls
assert all(call[1]["timeout_seconds"] == 300 for call in profile_calls), profile_calls

failed_profile_test = make_test()
failed_profile_test._preflight_runtime_profile_stack = (
    DarlingTest._preflight_runtime_profile_stack.__get__(failed_profile_test, DarlingTest)
)
failed_profile_test._profile_stack = lambda _profile: ["broken"]
original_bounded = west_test_module.run_bounded
west_test_module.run_bounded = lambda *_args, **_kwargs: ProcessResult(
    1, stdout="git am conflict\n"
)
try:
    try:
        failed_profile_test._preflight_runtime_profile_stack(
            "broken", "contract runtime"
        )
    except SystemExit as exc:
        assert str(exc) == (
            "Runtime deployment contract runtime cannot materialize source profile "
            "stack 'broken': 'broken' failed patch applicability preflight. Repair "
            "or rebase that profile with `west patch verify --profile broken` before "
            "retrying; this is not a runtime test failure."
        ), exc
    else:
        raise AssertionError("invalid runtime profile stack unexpectedly passed")
finally:
    west_test_module.run_bounded = original_bounded


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    build_dir = tempdir / "build"
    build_dir.mkdir()
    (build_dir / "CMakeCache.txt").write_text(
        "\n".join(
            [
                "CMAKE_GENERATOR:INTERNAL=Ninja",
                "CMAKE_BUILD_TYPE:STRING=Release",
                "CMAKE_C_COMPILER:FILEPATH=/usr/bin/clang",
                "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++",
                "DARLING_EUNION:BOOL=ON",
                "DARLING_RING_TRANSPORT:BOOL=ON",
                "DARLING_RPC_SLEEP_ACCOUNT:BOOL=OFF",
                "DARLING_GUEST_RECVSPIN:STRING=512",
                "DSERVER_RING_TRANSPORT:BOOL=ON",
            ]
        )
        + "\n"
    )
    old_build_dir = os.environ.get("DARLING_BUILD_DIR")
    os.environ["DARLING_BUILD_DIR"] = str(build_dir)
    try:
        args = make_test()._runtime_red_configure_args(
            {"runtime-artifacts": [{"deploy": ["usr/lib/system/libsystem_kernel.dylib"]}]},
            tempdir / "prefix",
        )
        ring_args = make_test()._runtime_red_configure_args(
            {
                "inherit-cmake-cache": [
                    "DARLING_RING_TRANSPORT",
                    "DSERVER_RING_TRANSPORT",
                ],
                "runtime-artifacts": [{"deploy": ["usr/lib/system/libsystem_kernel.dylib"]}],
            },
            tempdir / "prefix",
        )
        host_test = make_test()
        host_test._resolve_darling_launcher = (
            lambda _prefix: str(tempdir / "install/bin/darling")
        )
        host_args = host_test._runtime_red_configure_args(
            {"runtime-artifacts": [{"deploy": ["bin/darlingserver"]}]},
            tempdir / "prefix",
        )
        tool_args = make_test()._runtime_red_configure_args(
            {
                "cmake-defines": {
                    "DSERVER_TOOLS": True,
                    "WEST_EMPTY_DEFINE": None,
                    "WEST_STRING_DEFINE": "value",
                },
                "runtime-artifacts": [{"deploy": ["bin/dserverdbg"]}],
            },
            tempdir / "prefix",
        )
    finally:
        if old_build_dir is None:
            os.environ.pop("DARLING_BUILD_DIR", None)
        else:
            os.environ["DARLING_BUILD_DIR"] = old_build_dir
    assert "-DDARLING_EUNION=ON" in args, args
    assert "-DCMAKE_BUILD_TYPE=Debug" in args, args
    assert "-DDARLING_RING_TRANSPORT=OFF" in args, args
    assert "-DDARLING_RPC_SLEEP_ACCOUNT=OFF" in args, args
    assert "-DDARLING_GUEST_RECVSPIN=512" in args, args
    assert "-DDARLING_PATCH_PROFILE=homebrew" in args, args
    assert "-DDARLING_SKIP_DRIFT_GATE=ON" not in args, args
    assert "-DDSERVER_RING_TRANSPORT=OFF" in args, args
    assert "-DDARLING_RING_TRANSPORT=ON" in ring_args, ring_args
    assert "-DDSERVER_RING_TRANSPORT=ON" in ring_args, ring_args
    assert f"-DCMAKE_INSTALL_PREFIX={tempdir / 'prefix'}" in args, args
    assert f"-DCMAKE_INSTALL_PREFIX={tempdir / 'prefix'}" in host_args, host_args
    assert "-DDSERVER_TOOLS=ON" in tool_args, tool_args
    assert "-DWEST_EMPTY_DEFINE=" in tool_args, tool_args
    assert "-DWEST_STRING_DEFINE=value" in tool_args, tool_args

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    bad_bundle = tempdir / "bad-bundle"
    good_bundle = tempdir / "good-bundle"
    bad_bundle.mkdir()
    good_bundle.mkdir()
    (bad_bundle / "stdout.log").write_text("WEST_GUEST_SHELL_RC=1\n")
    (good_bundle / "stdout.log").write_text("GREEN_OK\nORACLE_RC=0\n")
    test = make_test()
    test._latest_debug_bundle = lambda _invocation, *, since: bad_bundle
    assert not test._check_guest_runtime_green_success(
        {"name": "green_reason_contract", "ok_marker": "GREEN_OK"},
        since=time.time(),
    )
    assert "GREEN output missing 'GREEN_OK'" in test.err_messages[-1]
    assert test._check_guest_runtime_green_success(
        {
            "name": "green_reason_contract",
            "ok_marker": "GREEN_OK",
            "host_trace_oracle": True,
        },
        since=time.time(),
    )
    test._latest_debug_bundle = lambda _invocation, *, since: good_bundle
    assert test._check_guest_runtime_green_success(
        {"name": "green_reason_contract", "ok_marker": "GREEN_OK"},
        since=time.time(),
    )

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    test = make_test()
    test._prefix = str(prefix)
    test._prefix_env = {"DARLING_NOOVERLAYFS": "1"}
    test._resolve_darling_launcher = lambda _prefix: "/tmp/fake-darling"
    invocation = {"requires_resources": ["darling-prefix"]}
    env = DarlingTest._execution_env(test, invocation)
    assert env["DPREFIX"] == str(prefix), env
    assert env["DARLING_PREFIX"] == str(prefix), env
    assert env["DARLING_NOOVERLAYFS"] == "1", env
    assert env["DARLING"] == "/tmp/fake-darling", env
    assert env["DARLING_LAUNCHER"] == "/tmp/fake-darling", env

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    test = make_test()
    test.topdir = str(tempdir)
    source_root = tempdir / "source"
    source_root.mkdir()
    calls = []
    old_run_bounded = west_test_module.run_bounded

    def quiet_success_run(args, **kwargs):
        calls.append((list(args), kwargs.get("capture_output"), kwargs.get("timeout_seconds")))
        return ProcessResult(
            0,
            stdout="\n".join(f"noisy stdout {index}" for index in range(300)),
            stderr="\n".join(f"noisy stderr {index}" for index in range(300)),
        )

    west_test_module.run_bounded = quiet_success_run
    stderr = io.StringIO()
    try:
        with redirect_stderr(stderr):
            build_root = test._runtime_red_build_artifacts(
                source_root,
                {
                    "runtime-artifacts": [
                        {"build-targets": ["target-a"], "deploy": ["bin/a"]},
                        {"build-targets": ["target-a", "target-b"], "deploy": ["bin/b"]},
                    ]
                },
                tempdir / "prefix",
                tempdir / "scratch",
                label="GREEN",
            )
    finally:
        west_test_module.run_bounded = old_run_bounded
    assert build_root == tempdir / "scratch/build"
    assert stderr.getvalue() == "", stderr.getvalue()
    assert len(calls) == 2, calls
    assert calls[0][1:] == (True, 1800), calls
    assert calls[1][0][-2:] == ["target-a", "target-b"], calls
    assert any(message == "  runtime phase start: GREEN configure" for message in test.inf_messages)
    assert any(message.startswith("  runtime phase complete: GREEN configure (") for message in test.inf_messages)
    assert any(message == "  runtime phase start: GREEN build" for message in test.inf_messages)
    assert any(message.startswith("  runtime phase complete: GREEN build (") for message in test.inf_messages)

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    test = make_test()
    test.topdir = str(tempdir)
    source_root = tempdir / "source"
    source_root.mkdir()
    results = iter(
        [
            ProcessResult(0),
            ProcessResult(1, stderr="ninja: error: unknown target 'rootless_bootstrap'\n"),
        ]
    )
    old_run_bounded = west_test_module.run_bounded
    west_test_module.run_bounded = lambda *_args, **_kwargs: next(results)
    try:
        try:
            test._runtime_red_build_artifacts(
                source_root,
                {"runtime-artifacts": [{"build-targets": ["rootless_bootstrap"]}]},
                tempdir / "prefix",
                tempdir / "scratch",
                allow_failure=True,
            )
        except RuntimeBuildFailure as exc:
            assert exc.phase == "build", exc.phase
            assert "unknown target 'rootless_bootstrap'" in process_output_text(exc.result)
        else:
            raise AssertionError("runtime RED build failure was not captured")
    finally:
        west_test_module.run_bounded = old_run_bounded

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    test = make_test()
    invocation = {"name": "dcc_cache_timeout_contract", "timeout_seconds": 1}
    old_run_bounded = guest_c_module.run_bounded

    def timed_out_dcc_command(args, **kwargs):
        assert list(args) == ["fake-dcc-builder"], args
        assert kwargs["cwd"] == tempdir, kwargs
        assert kwargs["timeout_seconds"] == 1, kwargs
        return ProcessResult(124, timed_out=True, stderr="DCC_BUILDER_STUCK\n")

    west_test_module.run_bounded = timed_out_dcc_command
    try:
        try:
            test._run_dcc_cache_command(
                invocation,
                "build",
                ["fake-dcc-builder"],
                tempdir,
            )
            raise AssertionError("timed out DCC command unexpectedly succeeded")
        except SystemExit as exc:
            assert "DCC cache build failed with rc 124" in str(exc), exc
    finally:
        west_test_module.run_bounded = old_run_bounded
    assert test._failure_phase == "setup", test._failure_phase
    assert any("DCC cache build timed out after 1s" in message for message in test.err_messages)
    assert any("DCC cache build failed with rc 124" in message for message in test.err_messages)

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    test = make_test()
    test.topdir = str(tempdir)
    old_run_bounded = west_test_module.run_bounded

    def timed_out_testkit_command(args, **kwargs):
        assert list(args) == ["cmake", "--build", "testkit-build"], args
        assert kwargs["cwd"] == tempdir, kwargs
        assert kwargs["timeout_seconds"] == 1800, kwargs
        return ProcessResult(124, timed_out=True, stderr="TESTKIT_BUILD_STUCK\n")

    west_test_module.run_bounded = timed_out_testkit_command
    try:
        try:
            test._run_testkit_build_command("build", ["cmake", "--build", "testkit-build"])
            raise AssertionError("timed out testkit command unexpectedly succeeded")
        except SystemExit as exc:
            assert "testkit build failed with rc 124" in str(exc), exc
    finally:
        west_test_module.run_bounded = old_run_bounded
    assert any("testkit build timed out after 1800s" in message for message in test.err_messages)
    assert any("testkit build failed with rc 124" in message for message in test.err_messages)

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    test = make_test()
    result = subprocess.CompletedProcess(
        ["fake"],
        7,
        stdout="\n".join(f"old stdout {index}" for index in range(150)),
        stderr="\n".join(f"tail stderr {index}" for index in range(120)),
    )
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        test._dump_command_tail("RED build", result)
    dumped = stderr.getvalue()
    assert "old stdout 0" not in dumped, dumped
    assert "old stdout 69" not in dumped, dumped
    assert "old stdout 70" in dumped, dumped
    assert "tail stderr 119" in dumped, dumped
    assert test.err_messages == ["RED build failed with rc 7"], test.err_messages

    test.err_messages.clear()
    result = subprocess.CompletedProcess(
        ["fake"],
        7,
        stdout="\n".join(
            [
                "old output",
                "FAILED: objc-runtime.mm.o",
                "clang++ ...",
                "objc-runtime.mm:128:2: error: mismatch in debug-ness macros",
            ]
            + [f"later output {index}" for index in range(250)]
        ),
    )
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        test._dump_command_tail("RED build", result)
    dumped = stderr.getvalue()
    assert "Actionable failure:" in dumped, dumped
    assert "objc-runtime.mm:128:2: error" in dumped, dumped

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    init_pid = tempdir / ".init.pid"
    init_pid.write_text("12345\n")
    old_checker = west_test_module.darling_init_pid_is_usable
    west_test_module.darling_init_pid_is_usable = lambda pid: pid != 12345
    try:
        make_test()._remove_stale_init_pid(tempdir)
    finally:
        west_test_module.darling_init_pid_is_usable = old_checker
    assert not init_pid.exists()

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    root_copy = prefix / "usr/lib/system/libsystem_kernel.dylib"
    base_copy = prefix / "libexec/darling/usr/lib/system/libsystem_kernel.dylib"
    root_copy.parent.mkdir(parents=True)
    base_copy.parent.mkdir(parents=True)
    root_copy.write_text("ORIGINAL\n")
    base_copy.write_text("ORIGINAL\n")

    test = make_test()
    test._prefix = str(prefix)
    calls = []
    resource_context_state = {"open": False}

    @contextmanager
    def fake_source_forest(patch, proof, *, omit_patch):
        calls.append(("source", patch["module"], proof["mode"], omit_patch))
        yield tempdir / "source/darling"

    def fake_build(
        source_root, proof, build_prefix, scratch_root, *, label="RED", allow_failure=False
    ):
        assert allow_failure is (label == "RED")
        calls.append(("build", source_root, build_prefix, scratch_root.exists(), label))
        output = scratch_root / "build/xnu/libsystem_kernel.dylib"
        output.parent.mkdir(parents=True)
        output.write_text(f"{label}\n")
        return scratch_root / "build"

    def fake_run(invocation, env=None):
        env = env or {}
        assert resource_context_state["open"], "runtime invocation escaped its prefix resource context"
        calls.append((
            "run",
            invocation["name"],
            root_copy.read_text(),
            base_copy.read_text(),
            env.get("RESOURCE_CONTEXT_MARKER"),
        ))
        return 77 if len([call for call in calls if call[0] == "run"]) == 1 else 0

    @contextmanager
    def fake_resource_context(invocation, env):
        merged = dict(env or {})
        merged["RESOURCE_CONTEXT_MARKER"] = invocation["name"]
        resource_context_state["open"] = True
        try:
            yield merged
        finally:
            resource_context_state["open"] = False

    test._guest_runtime_source_forest = fake_source_forest
    test._runtime_red_build_artifacts = fake_build
    test._resource_context = fake_resource_context
    test._run_invocation = fake_run
    test._check_guest_runtime_red_failure = (
        lambda _proof, _invocation, *, since, captured_output=None: True
    )

    patch = {"path": "xnu/example.patch", "module": "darling/src/external/xnu"}
    proof = {
        "mode": "guest-runtime-deploy",
        "expect-output-contains": ["old runtime symptom"],
        "runtime-artifacts": [
            {
                "module": "darling/src/external/xnu",
                "build-targets": ["libsystem_kernel"],
                "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
            }
        ],
    }
    invocation = {"guest_c_fixture": True, "name": "runtime_red_contract"}

    rc = test._run_guest_runtime_deploy_proof(patch, proof, invocation)
    assert rc == 0, rc
    assert root_copy.read_text() == "ORIGINAL\n"
    assert base_copy.read_text() == "ORIGINAL\n"
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", True), calls
    assert calls[1][0] == "build" and calls[1][3] is True and calls[1][4] == "RED", calls
    assert calls[2] == (
        "run",
        "runtime_red_contract",
        "RED\n",
        "RED\n",
        "runtime_red_contract",
    ), calls
    assert calls[3] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", False), calls
    assert calls[4][0] == "build" and calls[4][3] is True and calls[4][4] == "GREEN", calls
    assert calls[5] == (
        "run",
        "runtime_red_contract",
        "GREEN\n",
        "GREEN\n",
        "runtime_red_contract",
    ), calls

    root_copy.write_text("ORIGINAL\n")
    base_copy.write_text("ORIGINAL\n")
    calls.clear()

    def fake_run_prepared(invocation, env=None):
        env = env or {}
        calls.append(
            (
                "run",
                invocation["name"],
                root_copy.read_text(),
                base_copy.read_text(),
                env.get("WEST_GUEST_C_FIXTURE_PREPARE_ONLY"),
                env.get("WEST_GUEST_C_FIXTURE_RUN_ONLY"),
                env.get("WEST_GUEST_C_FIXTURE_ID"),
                env.get("RESOURCE_CONTEXT_MARKER"),
            )
        )
        if env.get("WEST_GUEST_C_FIXTURE_PREPARE_ONLY") == "1":
            return 0
        if env.get("WEST_GUEST_C_FIXTURE_RUN_ONLY") == "1":
            return 77
        return 0

    test._run_invocation = fake_run_prepared
    prepared_proof = dict(proof)
    prepared_proof["prepare-fixture-before-deploy"] = True
    rc = test._run_guest_runtime_deploy_proof(patch, prepared_proof, invocation)
    assert rc == 0, rc
    assert root_copy.read_text() == "ORIGINAL\n"
    assert base_copy.read_text() == "ORIGINAL\n"
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", True), calls
    assert calls[1][0] == "build" and calls[1][4] == "RED", calls
    assert calls[2][0:6] == (
        "run",
        "runtime_red_contract",
        "ORIGINAL\n",
        "ORIGINAL\n",
        "1",
        None,
    ), calls
    assert calls[3][0:6] == (
        "run",
        "runtime_red_contract",
        "RED\n",
        "RED\n",
        None,
        "1",
    ), calls
    assert calls[2][6] and calls[2][6] == calls[3][6], calls
    assert calls[2][7] == "runtime_red_contract", calls
    assert calls[3][7] == "runtime_red_contract", calls
    assert calls[4] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", False), calls
    assert calls[5][0] == "build" and calls[5][4] == "GREEN", calls
    assert calls[6][0:6] == (
        "run",
        "runtime_red_contract",
        "GREEN\n",
        "GREEN\n",
        None,
        None,
    ), calls

    root_copy.write_text("ORIGINAL\n")
    base_copy.write_text("ORIGINAL\n")
    calls.clear()
    red_runner_script = tempdir / "red-oracle.sh"
    red_runner_script.write_text("#!/usr/bin/env sh\nexit 1\n")
    test._project_path = lambda repo=None: tempdir

    def fake_run_with_red_runner(invocation, env=None):
        env = env or {}
        calls.append(
            (
                "run",
                invocation["name"],
                invocation.get("runner"),
                root_copy.read_text(),
                base_copy.read_text(),
                env.get("WEST_RUNTIME_SOURCE_ROOT"),
                env.get("WEST_GUEST_C_FIXTURE_RUN_ONLY"),
                list(invocation.get("requires_resources", [])),
                env.get("RESOURCE_CONTEXT_MARKER"),
            )
        )
        return 77 if invocation["name"] == "runtime_red_contract_red" else 0

    test._run_invocation = fake_run_with_red_runner
    red_runner_proof = dict(proof)
    red_runner_proof["red-runner"] = {
        "runner": "script",
        "repo": ".",
        "script": "red-oracle.sh",
    }
    rc = test._run_guest_runtime_deploy_proof(patch, red_runner_proof, invocation)
    assert rc == 0, rc
    assert calls[0] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", True), calls
    assert calls[1][0] == "build" and calls[1][4] == "RED", calls
    assert calls[2][0:5] == (
        "run",
        "runtime_red_contract_red",
        "script",
        "RED\n",
        "RED\n",
    ), calls
    assert calls[2][5] == str(tempdir / "source/darling"), calls
    assert calls[2][6] is None, calls
    assert "darling-prefix" in calls[2][7], calls
    assert calls[2][8] == "runtime_red_contract_red", calls
    assert calls[3] == ("source", "darling/src/external/xnu", "guest-runtime-deploy", False), calls
    assert calls[4][0] == "build" and calls[4][4] == "GREEN", calls
    assert calls[5][0:3] == ("run", "runtime_red_contract", None), calls
    assert calls[5][3:5] == ("GREEN\n", "GREEN\n"), calls

    symlink_parent = tempdir / "forest/darling/src/external/parent"
    symlink_parent.parent.mkdir(parents=True)
    symlink_parent.symlink_to(prefix, target_is_directory=True)
    nested_target = symlink_parent / "nested/project"
    assert test._has_symlink_parent(nested_target, tempdir / "forest/darling")

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    launcher = tempdir / "fake-darling"
    hold_pid = tempdir / "hold.pid"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = shutdown ]; then
\texit 0
fi
if [ "${1:-}" != shell ]; then
\texit 64
fi
if [ "${DARLING_PREFIX:-}" != "${DPREFIX:-}" ]; then
\tprintf 'bad DARLING_PREFIX=%s DPREFIX=%s\\n' "${DARLING_PREFIX:-}" "${DPREFIX:-}" >&2
\texit 65
fi
(sleep 30) &
echo "$!" > "${WEST_FAKE_HOLD_PID:?}"
printf 'FAKE_GUEST_COMMAND_DONE\\n' >&2
exit 0
"""
    )
    launcher.chmod(0o755)

    test = make_test()
    test._prefix = str(prefix)
    invocation = {
        "name": "guest_command_inherited_fd_contract",
        "cwd": tempdir,
        "guest_command_fixture": True,
        "guest_command": "/usr/bin/true",
        "guest_env_vars": {},
        "timeout_seconds": 1,
        "expect": {
            "returncode": 0,
            "output-contains": ["FAKE_GUEST_COMMAND_DONE"],
        },
    }
    env = os.environ.copy()
    env["DPREFIX"] = str(prefix)
    env["DARLING_LAUNCHER"] = str(launcher)
    env["WEST_FAKE_HOLD_PID"] = str(hold_pid)
    started = time.monotonic()
    rc = test._run_guest_command_fixture(invocation, env=env)
    elapsed = time.monotonic() - started
    try:
        if hold_pid.exists():
            os.kill(int(hold_pid.read_text()), signal.SIGKILL)
    except ProcessLookupError:
        pass
    assert rc == 0, (rc, test.err_messages)
    assert elapsed < 5, elapsed

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    launcher = tempdir / "fake-darling-any"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = shutdown ]; then
\texit 0
fi
if [ "${DARLING_PREFIX:-}" != "${DPREFIX:-}" ]; then
\tprintf 'bad DARLING_PREFIX=%s DPREFIX=%s\\n' "${DARLING_PREFIX:-}" "${DPREFIX:-}" >&2
\texit 65
fi
printf 'ANY_RETURN_MARKER\\n' >&2
exit 7
"""
    )
    launcher.chmod(0o755)

    test = make_test()
    test._prefix = str(prefix)
    invocation = {
        "name": "guest_command_any_returncode_contract",
        "cwd": tempdir,
        "guest_command_fixture": True,
        "guest_command": "/usr/bin/true",
        "guest_env_vars": {},
        "timeout_seconds": 1,
        "expect": {
            "returncode": "any",
            "output-contains": ["ANY_RETURN_MARKER"],
        },
    }
    env = os.environ.copy()
    env["DPREFIX"] = str(prefix)
    env["DARLING_LAUNCHER"] = str(launcher)
    rc = test._run_guest_command_fixture(invocation, env=env)
    assert rc == 0, (rc, test.err_messages)

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    child_pid = tempdir / "guest-child.pid"
    launcher = tempdir / "fake-darling-timeout"
    launcher.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = shutdown ]; then
\texit 0
fi
(sleep 30) &
echo "$!" > "${WEST_FAKE_TIMEOUT_CHILD_PID:?}"
printf 'GUEST_TIMEOUT_STARTED\\n' >&2
sleep 30
"""
    )
    launcher.chmod(0o755)

    test = make_test()
    test._prefix = str(prefix)
    invocation = {
        "name": "guest_command_timeout_kills_group_contract",
        "cwd": tempdir,
        "guest_command_fixture": True,
        "guest_command": "/usr/bin/true",
        "guest_env_vars": {},
        "timeout_seconds": 1,
        "expect": {
            "returncode": "timeout",
            "output-contains": ["GUEST_TIMEOUT_STARTED"],
        },
    }
    env = os.environ.copy()
    env["DPREFIX"] = str(prefix)
    env["DARLING_LAUNCHER"] = str(launcher)
    env["WEST_FAKE_TIMEOUT_CHILD_PID"] = str(child_pid)
    try:
        started = time.monotonic()
        rc = test._run_guest_command_fixture(invocation, env=env)
        elapsed = time.monotonic() - started
        assert rc == 0, (rc, test.err_messages)
        assert elapsed < 5, elapsed
        pid = int(child_pid.read_text())
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.05)
        assert not Path(f"/proc/{pid}").exists(), f"timed out guest child survived: {pid}"
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    fixture = tempdir / "fixture.c"
    fixture.write_text('int main(void) { return 0; }\\n')
    launcher = tempdir / "fake-darling"
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n")
    launcher.chmod(0o755)
    test = make_test()
    test._prefix = str(prefix)
    old_run_bounded = guest_c_module.run_bounded
    inspected = []

    def inspect_guest_c_runner(args, **kwargs):
        runner = Path(args[-1])
        content = runner.read_text()
        inspected.append(content)
        assert kwargs["timeout_seconds"] == 31, kwargs
        return ProcessResult(0)

    guest_c_module.run_bounded = inspect_guest_c_runner
    try:
        rc = test._run_guest_c_fixture(
            {
                "name": "guest_c_namespace_diagnostic_contract",
                "key": "guest-c-namespace-diagnostic",
                "display": "guest-c-namespace-diagnostic",
                "cwd": tempdir,
                "script_path": fixture,
                "guest_cc": "/usr/bin/clang",
                "guest_cflags": "",
                "guest_prelude": "",
                "guest_env_vars": {},
                "compile_flags": [],
                "link_flags": [],
                "run_args": [],
                "ok_marker": "",
                "host_trace_files": [],
                "host_temp_files": [],
                "host_stat_deltas": [],
                "host_trace_oracle": False,
                "timeout_seconds": 1,
                "diag": "bare",
            },
            env={"DPREFIX": str(prefix), "DARLING_LAUNCHER": str(launcher)},
        )
    finally:
        guest_c_module.run_bounded = old_run_bounded
    assert rc == 0, rc
    assert inspected, "guest-c runner was not generated"
    generated = inspected[0]
    assert "dump_namespace_state()" in generated, generated
    assert "WEST_GUEST_NAMESPACE_INIT_PID" in generated, generated
    assert "WEST_GUEST_NAMESPACE_MNT" in generated, generated
    assert "Cannot open mnt namespace file" in generated, generated
    assert "clear_stale_init_pid()" in generated, generated
    assert generated.count("clear_stale_init_pid") >= 3, generated
    assert '"$launch" shutdown >/dev/null 2>&1 || true' in generated, generated
    cleanup_pos = generated.index('WEST_GUEST_STAGE=cleanup')
    upload_pos = generated.index('WEST_GUEST_STAGE=upload')
    assert cleanup_pos < upload_pos, generated
    assert '"$launch" shutdown >/dev/null 2>&1 || true' in generated[
        cleanup_pos:upload_pos
    ], generated
    assert "dump_runtime_process_state()" in generated, generated
    assert "snapshot=\"$(mktemp /tmp/west-dserver-ps.XXXXXX)\"" in generated, generated
    assert "WEST_GUEST_DSERVER_EXE_SHA256" in generated, generated
    assert "WEST_GUEST_DSERVER_ARGS" in generated, generated
    assert "dump_runtime_file_state()" in generated, generated
    assert 'dump_file_sha launcher_server "$launcher_dir/darlingserver"' in generated, generated
    assert 'dump_file_sha install_dyld "$install_root/usr/lib/dyld"' in generated, generated
    assert 'dump_file_sha install_libsystem_kernel "$install_root/usr/lib/system/libsystem_kernel.dylib"' in generated, generated
    assert 'dump_file_sha prefix_libsystem_kernel "$DPREFIX/usr/lib/system/libsystem_kernel.dylib"' in generated, generated
    assert 'dump_file_sha prefix_dyld "$DPREFIX/usr/lib/dyld"' in generated, generated
    assert "WEST_GUEST_RPC_CLIENT_LOG_BEGIN" in generated, generated
    assert ": > /tmp/dserver-client-rpc.log" in generated, generated
    assert 'source "$guest_shell_helper"' in generated, generated
    assert 'darling_guest_shell "$launch" "$DPREFIX" "$seconds"' in generated, generated

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    prefix = tempdir / "prefix"
    prefix.mkdir()
    test = make_test()
    test._prefix = str(prefix)
    before = set(Path(tempfile.gettempdir()).glob("west-red-proof-runtime-*"))

    @contextmanager
    def fake_source_forest(_patch, _proof, *, omit_patch):
        assert omit_patch is True
        yield tempdir / "source/darling"

    def failing_build(
        _source_root, _proof, _build_prefix, scratch_root, *, label="RED", allow_failure=False
    ):
        assert label == "RED"
        assert allow_failure is True
        (scratch_root / "diagnostic.txt").write_text("kept\n")
        raise RuntimeError("forced build failure")

    test._guest_runtime_source_forest = fake_source_forest
    test._runtime_red_build_artifacts = failing_build

    try:
        test._run_guest_runtime_deploy_proof(
            {"path": "xnu/failing.patch", "module": "darling/src/external/xnu"},
            {
                "mode": "guest-runtime-deploy",
                "expect-output-contains": ["old runtime symptom"],
                "runtime-artifacts": [],
            },
            {"guest_c_fixture": True, "name": "runtime_red_keep_scratch"},
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced runtime-red build failure unexpectedly passed")

    after = set(Path(tempfile.gettempdir()).glob("west-red-proof-runtime-*"))
    kept = list(after - before)
    assert len(kept) == 1, kept
    assert (kept[0] / "diagnostic.txt").read_text() == "kept\n"
    shutil.rmtree(kept[0])

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    target = tempdir / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=target, check=True)
    (target / "file.txt").write_text("base\n")
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=target, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (target / "file.txt").write_text("base\nskipped\n")
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "skipped patch"], cwd=target, check=True)
    skipped_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    skipped_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (target / "dependent.txt").write_text("dependent\n")
    subprocess.run(["git", "add", "dependent.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "dependent patch"], cwd=target, check=True)
    dependent_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (target / "other.txt").write_text("kept\n")
    subprocess.run(["git", "add", "other.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "kept patch"], cwd=target, check=True)
    kept_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=target, check=True)
    (target / "rerolled.txt").write_text("rerolled\n")
    subprocess.run(["git", "add", "rerolled.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "skipped patch"], cwd=target, check=True)
    rerolled_subject_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=target, check=True)
    (target / "file.txt").write_text("base\nskipped\n")
    subprocess.run(["git", "add", "file.txt"], cwd=target, check=True)
    equivalent_commit_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2001-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2001-01-01T00:00:00+0000",
    }
    subprocess.run(
        ["git", "commit", "-q", "-m", "equivalent skipped patch"],
        cwd=target,
        env=equivalent_commit_env,
        check=True,
    )
    equivalent_skipped_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert equivalent_skipped_rev != skipped_rev

    profile_dir = tempdir / "patches/runtime"
    skipped_patch_file = profile_dir / "x/skipped.patch"
    rerolled_subject_patch_file = profile_dir / "x/rerolled-subject.patch"
    dependent_patch_file = profile_dir / "x/dependent.patch"
    kept_patch_file = profile_dir / "x/kept.patch"
    skipped_patch_file.parent.mkdir(parents=True)
    skipped_patch_file.write_text(skipped_patch)
    rerolled_subject_patch_file.write_text(rerolled_subject_patch)
    dependent_patch_file.write_text(dependent_patch)
    kept_patch_file.write_text(kept_patch)
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=target, check=True)

    test = make_test()
    test.manifest = types.SimpleNamespace(repo_abspath=str(tempdir))
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {
                "path": "x/skipped.patch",
                "module": "module",
                "source-commit": equivalent_skipped_rev,
            },
            {
                "path": "x/rerolled-subject.patch",
                "module": "module",
                "source-commit": "0000000000000000000000000000000000000000",
            },
            {"path": "x/dependent.patch", "module": "module"},
            {"path": "x/kept.patch", "module": "module"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"
    trace = tempdir / "profile-apply-trace.json"
    previous_trace = os.environ.get("GIT_TRACE2_EVENT")
    os.environ["GIT_TRACE2_EVENT"] = str(trace)
    try:
        test._apply_profile_module_patches(
            "runtime",
            "module",
            target,
            skip_patch_paths={"x/skipped.patch", "x/dependent.patch"},
        )
    finally:
        if previous_trace is None:
            del os.environ["GIT_TRACE2_EVENT"]
        else:
            os.environ["GIT_TRACE2_EVENT"] = previous_trace
    trace_text = trace.read_text()
    assert "maintenance run --auto" not in trace_text
    assert '"--patch"' not in trace_text
    assert '"--cherry-mark"' in trace_text
    assert (target / "file.txt").read_text() == "base\n"
    assert not (target / "dependent.txt").exists()
    assert (target / "rerolled.txt").read_text() == "rerolled\n"
    assert (target / "other.txt").read_text() == "kept\n"

    subprocess.run(["git", "reset", "--hard", "-q", skipped_rev], cwd=target, check=True)
    test = make_test()
    test.manifest = types.SimpleNamespace(repo_abspath=str(tempdir))
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {
                "path": "x/skipped.patch",
                "module": "module",
                "source-commit": equivalent_skipped_rev,
            },
            {
                "path": "x/rerolled-subject.patch",
                "module": "module",
                "source-commit": "0000000000000000000000000000000000000000",
            },
            {"path": "x/dependent.patch", "module": "module"},
            {"path": "x/kept.patch", "module": "module"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"
    assert not test._commit_is_ancestor(target, equivalent_skipped_rev)
    assert test._commit_has_equivalent_patch(target, equivalent_skipped_rev)
    test._apply_profile_module_patches("runtime", "module", target)
    assert (target / "file.txt").read_text() == "base\nskipped\n"
    assert (target / "rerolled.txt").read_text() == "rerolled\n"
    assert (target / "dependent.txt").read_text() == "dependent\n"
    assert (target / "other.txt").read_text() == "kept\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    repo = tempdir / "darling"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def independent_patch(path, contents, message):
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        (repo / path).write_text(contents)
        subprocess.run(["git", "add", path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
        return subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    skipped_patch = independent_patch("skipped.txt", "skipped\n", "skipped patch")
    kept_patch = independent_patch("kept.txt", "kept\n", "kept patch")
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)

    profile_dir = tempdir / "patches/runtime/darling"
    profile_dir.mkdir(parents=True)
    (profile_dir / "skipped.patch").write_text(skipped_patch)
    (profile_dir / "kept.patch").write_text(kept_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(
                name="darling",
                path="darling",
                abspath=str(repo),
                revision=base_rev,
            )
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "darling/skipped.patch", "module": "darling"},
            {"path": "darling/kept.patch", "module": "darling"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    patch = {
        "path": "darling/skipped.patch",
        "module": "darling",
        "source-base": base_rev,
    }
    proof = {"mode": "guest-runtime-deploy", "bad-profile": "current-minus-patch"}
    with test._guest_runtime_source_forest(patch, proof, omit_patch=True) as source_root:
        assert (source_root / "base.txt").read_text() == "base\n"
        assert not (source_root / "skipped.txt").exists()
        assert (source_root / "kept.txt").read_text() == "kept\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    repo = tempdir / "darling"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "fixed.txt").write_text("fixed\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "profile_contract.sh").write_text(
        "#!/usr/bin/env sh\n"
        "test -n \"$SRC_ROOT\"\n"
        "test -f \"$SRC_ROOT/fixed.txt\"\n"
    )
    (repo / "tests" / "profile_contract.sh").chmod(0o755)
    subprocess.run(["git", "add", "fixed.txt", "tests/profile_contract.sh"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixed patch"], cwd=repo, check=True)
    fixed_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)

    profile_dir = tempdir / "patches/runtime/darling"
    profile_dir.mkdir(parents=True)
    (profile_dir / "fixed.patch").write_text(fixed_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(
                name="darling",
                path="darling",
                abspath=str(repo),
                revision=base_rev,
            )
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "darling/fixed.patch", "module": "darling"},
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"
    test._execution_env = lambda _invocation: {}
    seen = []
    seen_assets = []

    def run_invocation(_invocation, env=None):
        if _invocation.get("runner") == "source-profile-script":
            script_path = _invocation["script_path"]
            assert script_path.is_file(), script_path
            assert script_path.parent.parent == _invocation["cwd"], _invocation
            seen_assets.append(script_path.read_text())
        source_root = Path(env["SRC_ROOT"])
        has_fixed = (source_root / "fixed.txt").exists()
        seen.append(has_fixed)
        if not has_fixed:
            print("old source failure")
        return 0 if has_fixed else 1

    test._run_invocation = run_invocation
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["old source failure"],
        },
        {"name": "source_profile_green", "shell": False},
    )
    assert rc == 0
    assert seen == [False, True], seen
    assert not (repo / "fixed.txt").exists(), "live checkout should not be the GREEN proof source"
    assert not (repo / "tests" / "profile_contract.sh").exists(), "live checkout should not contain the profile test asset"

    seen.clear()
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["old source failure"],
        },
        {
            "name": "source_profile_script",
            "shell": False,
            "runner": "source-profile-script",
            "repo": "darling",
            "script": "tests/profile_contract.sh",
            "cwd": repo,
            "script_path": repo / "tests" / "profile_contract.sh",
        },
    )
    assert rc == 0
    assert seen == [False, True], seen
    assert seen_assets == [
        "#!/usr/bin/env sh\n"
        "test -n \"$SRC_ROOT\"\n"
        "test -f \"$SRC_ROOT/fixed.txt\"\n",
        "#!/usr/bin/env sh\n"
        "test -n \"$SRC_ROOT\"\n"
        "test -f \"$SRC_ROOT/fixed.txt\"\n",
    ], seen_assets

    seen.clear()
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["different failure"],
        },
        {"name": "source_profile_green", "shell": False},
    )
    assert rc == 1
    assert any(
        "RED failure output missing 'different failure'" in message
        for message in test.err_messages
    ), test.err_messages

    subprocess.run(["git", "am", str(profile_dir / "fixed.patch")], cwd=repo, check=True)
    test._profile_is_applied = lambda _profile: True
    seen.clear()
    rc = test._run_source_base_proof(
        {"path": "darling/fixed.patch", "module": "darling", "source-base": base_rev},
        {
            "mode": "source-base",
            "source-env": "SRC_ROOT",
            "expect-output-contains": ["old source failure"],
        },
        {"name": "source_profile_green", "shell": False},
    )
    assert rc == 0
    assert seen == [False, True], seen

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    darling_repo = tempdir / "darling"
    xnu_repo = tempdir / "xnu"
    dserver_repo = tempdir / "darlingserver"
    for repo in (darling_repo, xnu_repo, dserver_repo):
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
        (repo / "base.txt").write_text(f"{repo.name} base\n")
        subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            f"file://{xnu_repo}",
            "src/external/xnu",
        ],
        cwd=darling_repo,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            f"file://{dserver_repo}",
            "src/external/darlingserver",
        ],
        cwd=darling_repo,
        check=True,
    )
    subprocess.run(
        ["git", "add", ".gitmodules", "src/external/xnu", "src/external/darlingserver"],
        cwd=darling_repo,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "nested source modules"],
        cwd=darling_repo,
        check=True,
    )

    darling_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=darling_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    xnu_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=xnu_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dserver_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dserver_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def patch_from(repo, base_rev, path, contents, message):
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        (repo / path).write_text(contents)
        subprocess.run(["git", "add", path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        patch_text = subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        subprocess.run(["git", "reset", "--hard", "-q", base_rev], cwd=repo, check=True)
        return patch_text, commit

    skipped_patch, _ = patch_from(xnu_repo, xnu_base, "skipped.txt", "skipped\n", "skipped patch")
    darling_patch, _ = patch_from(
        darling_repo,
        darling_base,
        "root_profile.txt",
        "profile root\n",
        "profile root",
    )
    dserver_patch, dserver_commit = patch_from(dserver_repo, dserver_base, "ring_abi.txt", "profile abi\n", "profile abi")

    profile_dir = tempdir / "patches/runtime"
    (profile_dir / "darling").mkdir(parents=True)
    (profile_dir / "xnu").mkdir(parents=True)
    (profile_dir / "darlingserver").mkdir(parents=True)
    (profile_dir / "darling/root-profile.patch").write_text(darling_patch)
    (profile_dir / "xnu/skipped.patch").write_text(skipped_patch)
    (profile_dir / "darlingserver/ring-abi.patch").write_text(dserver_patch)

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(name="darling", path="darling", abspath=str(darling_repo), revision="HEAD"),
            types.SimpleNamespace(name="xnu", path="darling/src/external/xnu", abspath=str(xnu_repo), revision=xnu_base),
            types.SimpleNamespace(name="darlingserver", path="darling/src/external/darlingserver", abspath=str(dserver_repo), revision=dserver_base),
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {"path": "darling/root-profile.patch", "module": "darling"},
            {"path": "xnu/skipped.patch", "module": "darling/src/external/xnu"},
            {
                "path": "darlingserver/ring-abi.patch",
                "module": "darling/src/external/darlingserver",
                "source-commit": dserver_commit,
            },
        ]
    }
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    with test._guest_runtime_source_forest(
        {
            "path": "xnu/skipped.patch",
            "module": "darling/src/external/xnu",
            "source-base": xnu_base,
        },
        {
            "mode": "guest-runtime-deploy",
            "bad-profile": "current-minus-patch",
            "source-modules": ["darling", "darling/src/external/darlingserver"],
        },
        omit_patch=True,
    ) as source_root:
        assert (source_root / "root_profile.txt").read_text() == "profile root\n"
        assert not (source_root / "src/external/xnu/skipped.txt").exists()
        assert (source_root / "src/external/darlingserver/ring_abi.txt").read_text() == "profile abi\n"

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    repo = tempdir / "darling"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(
                name="darling",
                path="darling",
                abspath=str(repo),
                revision=base_rev,
            )
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {"patches": []}
    test._profile_path = lambda profile: tempdir / "patches" / profile / "patches.yml"

    before = set(Path(tempfile.gettempdir()).glob("west-red-proof-source-*"))
    try:
        with test._guest_runtime_source_forest(
            {"path": "darling/example.patch", "module": "darling", "source-base": base_rev},
            {"mode": "guest-runtime-deploy", "bad-profile": "current-minus-patch"},
            omit_patch=True,
        ) as source_root:
            assert (source_root / "base.txt").read_text() == "base\n"
            raise RuntimeError("forced downstream failure")
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced downstream failure unexpectedly passed")

    after = set(Path(tempfile.gettempdir()).glob("west-red-proof-source-*"))
    kept = list(after - before)
    assert len(kept) == 1, kept
    assert (kept[0] / "darling/base.txt").read_text() == "base\n"
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(kept[0] / "darling")],
        cwd=repo,
        check=True,
    )
    shutil.rmtree(kept[0])

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    darling_repo = tempdir / "darling"
    libsystem_repo = tempdir / "libsystem"
    for repo in (darling_repo, libsystem_repo):
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
        (repo / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    libsystem_base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=libsystem_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (libsystem_repo / "profile-owner.txt").write_text("materialized profile owner\n")
    subprocess.run(["git", "add", "profile-owner.txt"], cwd=libsystem_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "profile libsystem"], cwd=libsystem_repo, check=True)
    libsystem_patch = subprocess.run(
        ["git", "format-patch", "-1", "--stdout"], cwd=libsystem_repo, check=True,
        capture_output=True, text=True,
    ).stdout
    subprocess.run(["git", "reset", "--hard", "-q", libsystem_base], cwd=libsystem_repo, check=True)

    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            f"file://{libsystem_repo}",
            "src/external/libsystem",
        ],
        cwd=darling_repo,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-qam", "nested libsystem"],
        cwd=darling_repo,
        check=True,
    )

    profile_dir = tempdir / "patches/runtime/darling/src/external/libsystem"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile-owner.patch").write_text(libsystem_patch)
    test = make_test()
    test.manifest = types.SimpleNamespace(
        repo_abspath=str(tempdir),
        projects=[
            types.SimpleNamespace(name="darling", path="darling", abspath=str(darling_repo), revision="HEAD"),
            types.SimpleNamespace(
                name="libsystem", path="darling/src/external/libsystem",
                abspath=str(libsystem_repo), revision=libsystem_base,
            ),
        ],
    )
    test._active_profile = "runtime"
    test._profile_stack = lambda profile: [profile]
    test._load_profile = lambda _profile: {
        "patches": [
            {
                "path": "darling/src/external/libsystem/profile-owner.patch",
                "module": "darling/src/external/libsystem",
            }
        ]
    }
    test._profile_path = lambda _profile: tempdir / "patches/runtime/patches.yml"
    with test._guest_runtime_source_forest(
        {"path": "darling/example.patch", "module": "darling", "source-base": "HEAD"},
        {"mode": "guest-runtime-deploy", "source-modules": ["darling", "darling/src/external/libsystem"]},
        omit_patch=False,
    ) as source_root:
        materialized = source_root / "src/external/libsystem"
        assert not materialized.is_symlink(), materialized
        assert (materialized / "profile-owner.txt").read_text() == "materialized profile owner\n"

print("PASS west-test-runtime-red-contract")

"""CMake-backed fixture helpers for ``west test`` metadata runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from test_ctest import ctest_label_args


Reporter = Callable[[str], None]


def archive_source_to(source_root: Path, destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=source_root,
        stdout=subprocess.PIPE,
    )
    try:
        tar = subprocess.run(
            ["tar", "-C", str(destination), "-xf", "-"],
            stdin=archive.stdout,
            check=False,
        )
    finally:
        if archive.stdout is not None:
            archive.stdout.close()
    archive_rc = archive.wait()
    return archive_rc or tar.returncode


def run_darling_cmake_target_fixture(
    invocation,
    *,
    env=None,
    executor: str | None = None,
    bundle_root: str | Path = "~/work/darling-debug",
    inf: Reporter = lambda message: None,
    err: Reporter = lambda message: None,
    die: Reporter | None = None,
) -> int:
    def fatal(message: str) -> None:
        if die is not None:
            die(message)
        raise RuntimeError(message)

    if invocation.get("diag", "bare") != "bare" and not invocation.get("ctest_label"):
        fatal(
            f"{invocation['name']}: darling-cmake-target-fixture currently "
            "supports diag:bare only"
        )
    run_env = env if env is not None else invocation.get("env")
    if not run_env:
        run_env = os.environ.copy()
    else:
        run_env = dict(run_env)
    source_root = invocation["cwd"]
    source_root_env = invocation.get("source_root_env")
    if source_root_env and run_env.get(source_root_env):
        source_root = Path(run_env[source_root_env])
    if not (source_root / "CMakeLists.txt").is_file():
        err(f"{invocation['name']}: CMakeLists.txt not found: {source_root}")
        return 1

    timeout_seconds = int(invocation.get("timeout_seconds", 600))
    with tempfile.TemporaryDirectory(
        prefix=f"west-darling-cmake-target-{invocation['name']}-"
    ) as temp:
        tempdir = Path(temp)
        project_root = tempdir / "project"
        source_copy = project_root / invocation["source_dir"]
        build_dir = tempdir / "build"
        bin_dir = tempdir / "bin"
        compile_log = tempdir / "compile-commands.jsonl"
        rc = archive_source_to(source_root, source_copy)
        if rc:
            return rc
        for fixture in invocation.get("fixture_files", []):
            source_fixture = source_copy / fixture
            if source_fixture.is_file():
                continue
            current_fixture = invocation["cwd"] / fixture
            if not current_fixture.is_file():
                err(f"{invocation['name']}: fixture file not found: {current_fixture}")
                return 1
            source_fixture.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current_fixture, source_fixture)

        write_darling_cmake_superproject(project_root, invocation)
        if invocation.get("required_compile_options"):
            bin_dir.mkdir()
            launcher = bin_dir / "west-c-compiler-launcher"
            write_cmake_compiler_launcher(launcher, compile_log)
        cmake_args = list(invocation.get("cmake_args", []))
        if invocation.get("required_compile_options"):
            cmake_args.append(f"-DCMAKE_C_COMPILER_LAUNCHER={launcher}")
        configure_args = [
            "cmake",
            "-S",
            str(project_root),
            "-B",
            str(build_dir),
            "-DCMAKE_BUILD_TYPE=Release",
            *cmake_args,
        ]
        build_args = [
            "cmake",
            "--build",
            str(build_dir),
            "--target",
            invocation["target"],
            "-j",
            str(os.environ.get("WEST_TEST_BUILD_JOBS", "2")),
            *invocation.get("build_args", []),
        ]
        commands = [
            ("cmake configure", configure_args),
            ("cmake build", build_args),
        ]
        if invocation.get("ctest_label"):
            ctest_args = ctest_label_args(build_dir, invocation["ctest_label"])
            if invocation.get("diag", "bare") != "bare":
                if not executor:
                    fatal(
                        f"{invocation['name']}: diag:{invocation['diag']} "
                        "requires darling-debug-runner. Build the west project "
                        "with `cargo build --release` in `darling-debug-runner`, "
                        "install it on PATH, or pass --executor."
                    )
                ctest_args = [
                    executor,
                    "run",
                    "--name",
                    f"west-test-{invocation['name']}",
                    "--bundle-root",
                    str(bundle_root),
                    "--timeout-seconds",
                    str(invocation.get("timeout_seconds", 600)),
                    "--",
                    *ctest_args,
                ]
            commands.append(("ctest label", ctest_args))
        else:
            commands.append(
                (
                    "run target",
                    [str(build_dir / invocation["run_binary"])],
                )
            )
        for label, command in commands:
            inf(f"  darling-cmake-target-fixture: {label}")
            try:
                result = subprocess.run(
                    command,
                    cwd=project_root,
                    env=run_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                err(
                    f"{invocation['name']}: {label} timed out after "
                    f"{timeout_seconds}s"
                )
                return 124
            if result.returncode:
                output = result.stdout + result.stderr
                tail = "\n".join(output.splitlines()[-160:])
                if tail:
                    sys.stderr.write(tail + "\n")
                err(
                    f"{invocation['name']}: {label} failed with rc "
                    f"{result.returncode}"
                )
                return result.returncode
        return check_required_compile_options(invocation, compile_log, err=err)


def write_darling_cmake_superproject(project_root: Path, invocation) -> None:
    source_dir = invocation["source_dir"]
    target = invocation["target"]
    fallback_sources = invocation.get("fallback_executable_sources", [])
    if not fallback_sources:
        fallback_sources = [f"{source_dir}/tests/{target}.c"]
    fallback_include_dirs = invocation.get("fallback_include_dirs", [])
    fallback_link_libraries = invocation.get("fallback_link_libraries", [])
    fallback_source_lines = "\n    ".join(fallback_sources)
    fallback_include_lines = " ".join(fallback_include_dirs)
    fallback_link_lines = " ".join(fallback_link_libraries)
    ctest_label = invocation.get("ctest_label") or ""
    ctest_fallback = ""
    if ctest_label:
        ctest_fallback = f"""
add_test(
    NAME west_{target}
    COMMAND {target}
)
set_tests_properties(west_{target} PROPERTIES
    LABELS "{ctest_label}"
)
"""
    host_include_lines = "\n".join(
        f'set(CMAKE_C_FLAGS "${{CMAKE_C_FLAGS}} -isystem {path}")'
        for path in host_c_include_dirs()
    )
    cmake = f"""cmake_minimum_required(VERSION 3.16)
project(west_darling_cmake_target_fixture C)

set(BUILD_TARGET_64BIT ON)
set(BUILD_TARGET_32BIT OFF)
set(TARGET_x86_64 ON)
set(TARGET_ARM64 OFF)
set(BUILD_TESTING ON)
set(CMAKE_CROSSCOMPILING_EMULATOR /usr/bin/env CACHE STRING "")
set(APPLE_TARGET_TRIPLET_PRIMARY west)
list(PREPEND CMAKE_MODULE_PATH "${{CMAKE_CURRENT_SOURCE_DIR}}")
enable_testing()

include(darling_exe)
add_library(system STATIC system_shim.c)
add_custom_target(ranlib)
add_custom_target(lipo)
add_custom_target(west-ar)
{host_include_lines}
add_subdirectory({source_dir})

if(NOT TARGET {target})
    add_executable({target}
    {fallback_source_lines}
    )
    if(NOT "{fallback_include_lines}" STREQUAL "")
        target_include_directories({target} PRIVATE {fallback_include_lines})
    endif()
    if(NOT "{fallback_link_lines}" STREQUAL "")
        target_link_libraries({target} {fallback_link_lines})
    endif()
endif()

{ctest_fallback}
set_target_properties({target} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${{CMAKE_BINARY_DIR}}/{source_dir}"
)
target_link_libraries({target} system)
"""
    module = """function(_west_darling_collect_sources out_var)
    set(srcs)
    set(mode)
    set(skip_next FALSE)
    foreach(arg IN LISTS ARGN)
        if(skip_next)
            set(skip_next FALSE)
            continue()
        endif()
        if(arg STREQUAL SOURCES OR arg STREQUAL OBJECTS)
            set(mode ${arg})
            continue()
        endif()
        if(arg STREQUAL FAT OR arg STREQUAL 32BIT_ONLY OR arg STREQUAL 64BIT_ONLY)
            continue()
        endif()
        if(arg STREQUAL LINK_FLAGS)
            set(skip_next TRUE)
            set(mode)
            continue()
        endif()
        if(arg STREQUAL SIBLINGS)
            set(mode SKIP)
            continue()
        endif()
        if(mode STREQUAL SKIP)
            continue()
        endif()
        if(mode STREQUAL SOURCES OR mode STREQUAL OBJECTS OR NOT mode)
            if(arg MATCHES "^\\\\$<TARGET_OBJECTS:([^>]+)>$")
                if(TARGET "${CMAKE_MATCH_1}")
                    list(APPEND srcs ${arg})
                endif()
            else()
                list(APPEND srcs ${arg})
            endif()
        endif()
    endforeach()
    set(${out_var} ${srcs} PARENT_SCOPE)
endfunction()

function(add_darling_static_library name)
    _west_darling_collect_sources(srcs ${ARGN})
    add_library(${name} STATIC ${srcs})
endfunction()

function(add_darling_object_library name)
    _west_darling_collect_sources(srcs ${ARGN})
    add_library(${name} OBJECT ${srcs})
endfunction()

function(add_darling_library name)
    _west_darling_collect_sources(srcs ${ARGN})
    add_library(${name} STATIC ${srcs})
endfunction()

function(add_circular name)
    _west_darling_collect_sources(srcs ${ARGN})
    add_library(${name} STATIC ${srcs})
endfunction()

function(add_darling_executable name)
    add_executable(${name} ${ARGN})
endfunction()
"""
    shim = """#include <stddef.h>

int
timingsafe_bcmp(const void *b1, const void *b2, size_t n)
{
\tconst unsigned char *p1 = b1;
\tconst unsigned char *p2 = b2;
\tunsigned char result = 0;

\tfor (size_t i = 0; i < n; i++)
\t\tresult |= p1[i] ^ p2[i];
\treturn result != 0;
}
"""
    (project_root / "CMakeLists.txt").write_text(cmake)
    (project_root / "darling_exe.cmake").write_text(module)
    (project_root / "system_shim.c").write_text(shim)


def host_c_include_dirs() -> list[str]:
    include_dirs: list[str] = []
    try:
        compiler_include = subprocess.check_output(
            ["cc", "-print-file-name=include"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        compiler_include = ""
    if compiler_include and Path(compiler_include).is_dir():
        include_dirs.append(compiler_include)
    try:
        machine = subprocess.check_output(["cc", "-dumpmachine"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        machine = ""
    candidates = []
    if machine:
        candidates.append(Path("/usr/include") / machine)
    candidates.append(Path("/usr/include"))
    for candidate in candidates:
        if candidate.is_dir():
            include_dirs.append(str(candidate))
    return list(dict.fromkeys(include_dirs))


def write_cmake_compiler_launcher(launcher: Path, log_path: Path) -> None:
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import subprocess\n"
        "import sys\n"
        f"with open({str(log_path)!r}, 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(subprocess.call(sys.argv[1:]))\n"
    )
    launcher.chmod(0o755)


def check_required_compile_options(invocation, log_path: Path, *, err: Reporter) -> int:
    checks = invocation.get("required_compile_options", [])
    if not checks:
        return 0
    if not log_path.is_file():
        err(f"{invocation['name']}: compiler launcher log not found")
        return 1
    entries = []
    for line in log_path.read_text().splitlines():
        try:
            args = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(args, list):
            entries.append([str(arg) for arg in args])
    for check in checks:
        source = check["source"]
        options = check.get("options", [])
        matches = [
            args
            for args in entries
            if any(arg == source or arg.endswith(f"/{source}") for arg in args)
        ]
        if not matches:
            err(f"{invocation['name']}: no compile command recorded for {source}")
            return 1
        if not any(all(option in args for option in options) for args in matches):
            err(
                f"{invocation['name']}: compile command for {source} missing "
                f"option(s): {', '.join(options)}"
            )
            return 1
    return 0

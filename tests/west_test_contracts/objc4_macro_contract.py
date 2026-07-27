#!/usr/bin/env python3
"""Compile the reviewed objc4 debug-build CMake definition in both modes."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


EXPRESSION = '"OBJC_IS_DEBUG_BUILD=$<IF:$<CONFIG:Debug>,1,0>"'


def run(*args: str, cwd: Path | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AssertionError(f"{' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")


def contract_source(candidate: Path) -> None:
    cmake = candidate / "runtime" / "CMakeLists.txt"
    text = cmake.read_text()
    assert "add_definitions(-DOBJC_NO_GC -DOBJC_IS_DEBUG_BUILD=1)" not in text
    assert 'CMAKE_C_FLAGS_DEBUG "-ggdb -DOBJC_IS_DEBUG_BUILD=1"' not in text
    assert 'CMAKE_CXX_FLAGS_DEBUG "-ggdb -DOBJC_IS_DEBUG_BUILD=1"' not in text
    assert text.count(EXPRESSION) == 1
    assert "target_compile_definitions(objc_obj PRIVATE" in text
    # This is an object-like definition.  There are no caller arguments or
    # statement-macro control-flow semantics to evaluate more than once.
    assert "OBJC_IS_DEBUG_BUILD(" not in text


def compile_mode(root: Path, mode: str) -> None:
    source = root / "source"
    source.mkdir(exist_ok=True)
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(objc4_macro_contract LANGUAGES CXX)\n"
        "add_library(objc_obj OBJECT runtime.cpp)\n"
        "add_library(consumer OBJECT consumer.cpp)\n"
        f"target_compile_definitions(objc_obj PRIVATE {EXPRESSION})\n"
    )
    (source / "runtime.cpp").write_text(
        "#if defined(CONTRACT_DEBUG)\n"
        "# if defined(NDEBUG)\n"
        "#  error debug must not define NDEBUG\n"
        "# endif\n"
        "# if !defined(OBJC_IS_DEBUG_BUILD) || OBJC_IS_DEBUG_BUILD != 1\n"
        "#  error debug definition must be one\n"
        "# endif\n"
        "#else\n"
        "# if !defined(NDEBUG)\n"
        "#  error release must define NDEBUG\n"
        "# endif\n"
        "# if !defined(OBJC_IS_DEBUG_BUILD) || OBJC_IS_DEBUG_BUILD != 0\n"
        "#  error release definition must be zero in the runtime target\n"
        "# endif\n"
        "#endif\n"
        "int objc4_macro_contract_runtime() { return 0; }\n"
    )
    (source / "consumer.cpp").write_text(
        "#if defined(OBJC_IS_DEBUG_BUILD)\n"
        "# error target-private runtime definition escaped to a consumer\n"
        "#endif\n"
        "int objc4_macro_contract_consumer() { return 0; }\n"
    )
    build = root / mode.lower()
    defines = ["-DCMAKE_BUILD_TYPE=" + mode]
    if mode == "Debug":
        defines.append("-DCMAKE_CXX_FLAGS_DEBUG=-DCONTRACT_DEBUG")
    run("cmake", "-S", str(source), "-B", str(build), "-G", "Ninja", *defines)
    run("cmake", "--build", str(build), "--target", "objc_obj", "consumer")
    ninja = (build / "build.ninja").read_text()
    if mode == "Debug":
        assert "-DOBJC_IS_DEBUG_BUILD=1" in ninja
        assert "-DOBJC_IS_DEBUG_BUILD=0" not in ninja
    else:
        assert "-DOBJC_IS_DEBUG_BUILD=0" in ninja


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    contract_source(args.candidate.resolve())
    with tempfile.TemporaryDirectory(prefix="objc4-macro-contract-") as temporary:
        root = Path(temporary)
        compile_mode(root, "Release")
        compile_mode(root, "Debug")
    print("objc4 macro contract: PASS")


if __name__ == "__main__":
    main()

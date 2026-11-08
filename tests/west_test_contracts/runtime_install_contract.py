"""Host consumer proof using a real compiler/CMake install, never a simulated guest boot."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

# West command hosting is replaced; tiny host-fixture images use a 1 MiB reserve.
# Every configure/build/install, file, executable invocation and integrity check is real.
west = types.ModuleType("west")
commands = types.ModuleType("west.commands")


class WestCommand:
    def __init__(self, *_args, **_kwargs):
        pass

    def die(self, message, exit_code=1):
        raise AssertionError(f"unexpected command validation failure: {message}")

    def inf(self, _message):
        pass

    def err(self, message):
        print(message, file=sys.stderr)


commands.WestCommand = WestCommand
sys.modules.setdefault("west", west)
sys.modules.setdefault("west.commands", commands)

from darling_build import DarlingBuild
from deploy_receipt import read_receipt, receipt_path, verify_receipt
from runtime_install import (
    RuntimeInstallationError, build_runtime, install_runtime, resolve_runtime,
)
from runtime_retention import registered_runtime, runtime_use
import runtime_retention as retention


CMAKE_PROJECT = r'''cmake_minimum_required(VERSION 3.18)
project(InstalledRuntimeHostContract C)
set(HOST_CONTRACT_VERSION "one" CACHE STRING "Host-only fixture identity")
add_executable(darling host_fixture.c)
add_executable(darlingserver host_fixture.c)
foreach(target darling darlingserver)
  target_compile_definitions(${target} PRIVATE HOST_CONTRACT_VERSION="${HOST_CONTRACT_VERSION}")
endforeach()
add_custom_target(build_audit ALL
  COMMAND "${CMAKE_COMMAND}" "-DAUDIT_PATH=${CMAKE_CURRENT_BINARY_DIR}/build-audit.log"
          -P "${CMAKE_CURRENT_SOURCE_DIR}/record-build.cmake"
  VERBATIM)
install(TARGETS darling darlingserver RUNTIME DESTINATION bin)
install(FILES payload.txt payload.alias DESTINATION libexec/darling)
install(FILES optional-data.txt DESTINATION libexec/darling/usr/share/darling/tests/eunion
        COMPONENT tests-data EXCLUDE_FROM_ALL)
install(CODE "file(APPEND \"${CMAKE_BINARY_DIR}/install-audit.log\" \"installed\\n\")")
install(CODE "file(WRITE \"${CMAKE_INSTALL_PREFIX}/libexec/darling/hook-output.txt\" \"normal install-hook output\\n\")")
'''

HOST_SOURCE = r'''#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--host-contract-version") == 0) {
        puts(HOST_CONTRACT_VERSION);
        return 0;
    }
    fputs("Host installation fixture: this is not a Darling guest runtime.\n", stderr);
    return 79;
}
'''


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=True, capture_output=True, text=True)


def initialize_repository(path: Path) -> str:
    run("git", "-C", str(path), "init", "-q")
    run("git", "-C", str(path), "add", ".")
    run("git", "-C", str(path), "-c", "user.name=host-contract",
        "-c", "user.email=host-contract@example.invalid", "commit", "-qm", "fixture inputs")
    return run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


def snapshot(root: Path) -> dict[str, tuple]:
    entries = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                value = ("symlink", mode, os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                value = ("file", mode, path.read_bytes())
            else:
                value = ("directory", mode)
            entries[path.relative_to(root).as_posix()] = value
    return entries


def configure(source: Path, build: Path, runtime: Path, version: str) -> None:
    run("cmake", "-S", str(source), "-B", str(build),
        f"-DCMAKE_INSTALL_PREFIX={runtime}", f"-DHOST_CONTRACT_VERSION={version}")


def build_command(manifest: Path, build: Path, runtime: Path, *, with_test_data=False):
    command = DarlingBuild()
    command.manifest = types.SimpleNamespace(repo_abspath=str(manifest))
    arguments = types.SimpleNamespace(
        build_dir=build, targets=None, install=True, runtime_root=runtime,
        with_test_data=with_test_data, jobs=1, json=True,
    )
    output = io.StringIO()
    returncode = 0
    with contextlib.redirect_stdout(output):
        try:
            command.do_run(arguments, [])
        except SystemExit as error:
            returncode = error.code
    return returncode, json.loads(output.getvalue())


def expect_install_refusal(build: Path, runtime: Path, manifest: Path) -> None:
    try:
        install_runtime(build, runtime, manifest_repo=manifest, with_test_data=True)
    except RuntimeInstallationError:
        pass
    else:
        raise AssertionError("published image was accepted for another install")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="west-real-install-contract-") as directory, patch.object(
        retention, "DEFAULT_FREE_FLOOR", 1 << 20,
    ):
        root = Path(directory)
        managed_state = root / "managed state"
        managed_state.mkdir()
        os.environ["DW_STATE_ROOT"] = str(managed_state)
        source = root / "host source with spaces"
        source.mkdir()
        (source / "CMakeLists.txt").write_text(CMAKE_PROJECT)
        (source / "host_fixture.c").write_text(HOST_SOURCE)
        (source / "record-build.cmake").write_text('file(APPEND "${AUDIT_PATH}" "built\\n")\n')
        (source / "payload.txt").write_text("normal image payload\n")
        (source / "payload.alias").symlink_to("payload.txt")
        (source / "optional-data.txt").write_text("optional installed test input\n")
        source_revision = initialize_repository(source)
        manifest = root / "manifest"
        manifest.mkdir()
        (manifest / "west.yml").write_text("manifest: {version: 1.2, projects: []}\n")
        manifest_revision = initialize_repository(manifest)
        build = root / "mutable build with spaces"
        image = root / "published image one"
        configure(source, build, image, "one")
        returncode, published = build_command(manifest, build, image)
        assert returncode == 0 and published["installed"] is True, published
        runtime = resolve_runtime(image)
        assert run(str(runtime.launcher), "--host-contract-version").stdout.strip() == "one"
        receipt = read_receipt(image)
        assert receipt is not None and verify_receipt(receipt)[0] == []
        assert receipt["source"]["revision"] == source_revision
        assert receipt["workspace"]["manifest_commit"] == manifest_revision
        optional_path = Path("libexec/darling/usr/share/darling/tests/eunion/optional-data.txt")
        assert not (image / optional_path).exists()
        alias = next(row for row in receipt["artifacts"] if row["path"].endswith("payload.alias"))
        assert alias["kind"] == "symlink" and alias["target"] == "payload.txt"
        published_bytes = snapshot(image)

        for overlapping in (image, image / "guest", root):
            try:
                runtime.environment(overlapping, base={})
            except RuntimeInstallationError:
                pass
            else:
                raise AssertionError("installation and guest state were allowed to overlap")
        assert snapshot(image) == published_bytes

        # Reconfigure and genuinely rebuild the same mutable B. The new binary
        # differs, while immutable installed A and its receipt remain valid.
        (source / "host_fixture.c").write_text(HOST_SOURCE + "\n/* second source candidate */\n")
        configure(source, build, image, "two")
        build_runtime(build, jobs=1)
        assert run(str(build / "darling"), "--host-contract-version").stdout.strip() == "two"
        assert run(str(runtime.launcher), "--host-contract-version").stdout.strip() == "one"
        assert verify_receipt(receipt)[0] == []
        install_audit = (build / "install-audit.log").read_bytes()
        build_audit = (build / "build-audit.log").read_bytes()
        expect_install_refusal(build, image, manifest)
        assert (build / "install-audit.log").read_bytes() == install_audit
        assert snapshot(image) == published_bytes
        returncode, rejected = build_command(manifest, build, image)
        assert returncode != 0 and rejected["status"] == "FAIL", rejected
        assert (build / "build-audit.log").read_bytes() == build_audit, "rejected install rebuilt first"
        assert (build / "install-audit.log").read_bytes() == install_audit, "rejected install executed CMake install"
        assert snapshot(image) == published_bytes, "published image was modified before refusal"

        # A genuinely new configured image can publish the next source/build,
        # including the optional component's separate normal CMake manifest.
        second = root / "published image two"
        configure(source, build, second, "two")
        returncode, published_second = build_command(manifest, build, second, with_test_data=True)
        assert returncode == 0 and published_second["installed"] is True, published_second
        second_runtime = resolve_runtime(second)
        assert run(str(second_runtime.launcher), "--host-contract-version").stdout.strip() == "two"
        second_receipt = read_receipt(second)
        assert second_receipt is not None and verify_receipt(second_receipt)[0] == []
        assert {item["name"] for item in second_receipt["build"]["install_manifests"]} == {
            "install_manifest.txt", "install_manifest_tests-data.txt",
        }
        installed_paths = {
            Path(line).relative_to(second).as_posix()
            for manifest_path in (build / "install_manifest.txt", build / "install_manifest_tests-data.txt")
            for line in manifest_path.read_text().splitlines()
        }
        assert {entry["path"] for entry in second_receipt["artifacts"]} == (
            installed_paths | {"libexec/darling/hook-output.txt"}
        )
        optional = second / optional_path
        original_data = optional.read_bytes()
        optional.write_bytes(b"corrupted optional test input")
        assert any(str(optional_path) in problem.label for problem in verify_receipt(second_receipt)[0])
        optional.write_bytes(original_data)
        assert verify_receipt(second_receipt)[0] == []

        # Newly added files can alter loader lookup even if receipted files
        # themselves are unchanged. They invalidate the immutable image.
        injected = second / "libexec/darling/unreceipted.dylib"
        injected.write_bytes(b"unexpected post-publication input")
        assert any("unreceipted.dylib" in problem.label for problem in verify_receipt(second_receipt)[0])
        injected.unlink()
        assert verify_receipt(second_receipt)[0] == []
        assert snapshot(image) == published_bytes and verify_receipt(receipt)[0] == []

        # Ordinary packaged images are allowed without manufacturing a workspace receipt.
        receipt_path(second).unlink()
        unrecorded = snapshot(second)
        package_runtime = resolve_runtime(second)
        assert package_runtime.identity is None and snapshot(second) == unrecorded

        # A NEW normal publication inside an explicitly declared DW_STATE_ROOT is
        # registered and receives a use lease; a sibling legacy root is not adopted.
        managed_image = managed_state / "published image three"
        configure(source, build, managed_image, "three")
        with patch.dict(os.environ, {"DW_STATE_ROOT": str(managed_state)}):
            returncode, published_managed = build_command(manifest, build, managed_image)
        assert returncode == 0 and published_managed["installed"] is True, published_managed
        assert isinstance(published_managed.get("retention"), list), published_managed
        assert all(
            row.get("action") != "error" for row in published_managed["retention"]
        ), published_managed["retention"]
        managed_status = registered_runtime(managed_image)
        assert managed_status is not None, managed_status
        assert managed_status["state"] == "published" and managed_status["problems"] == []
        assert registered_runtime(image) is None and registered_runtime(second) is None
        managed_snapshot = snapshot(managed_image)
        with runtime_use(managed_image) as registration:
            assert registration is not None
            assert registration["root"] == str(managed_image)
            assert registration["state"] == "published"
            assert resolve_runtime(managed_image).identity is not None
        assert snapshot(managed_image) == managed_snapshot

        # Exercise the actual publisher, not a separate collector invocation:
        # new normal installations evict old images, but never an active reader.
        managed_four = managed_state / "published image four"
        managed_five = managed_state / "published image five"
        with runtime_use(managed_image):
            for destination, version in ((managed_four, "four"), (managed_five, "five")):
                configure(source, build, destination, version)
                returncode, result = build_command(manifest, build, destination)
                assert returncode == 0 and result["installed"] is True, result
            assert snapshot(managed_image) == managed_snapshot
            assert run(str(managed_four / "bin/darling"), "--host-contract-version").stdout.strip() == "four"
            assert run(str(managed_five / "bin/darling"), "--host-contract-version").stdout.strip() == "five"
        managed_six = managed_state / "published image six"
        configure(source, build, managed_six, "six")
        returncode, result = build_command(manifest, build, managed_six)
        assert returncode == 0 and result["installed"] is True, result
        assert not managed_image.exists() and not managed_four.exists(), result
        assert run(str(managed_five / "bin/darling"), "--host-contract-version").stdout.strip() == "five"
        assert run(str(managed_six / "bin/darling"), "--host-contract-version").stdout.strip() == "six"
        assert snapshot(image) == published_bytes and snapshot(second) == unrecorded

    print("PASS real CMake/compiler/install immutable runtime consumer contract (host only; no guest boot)")


if __name__ == "__main__":
    main()

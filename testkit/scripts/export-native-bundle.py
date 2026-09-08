#!/usr/bin/env python3
"""Relocate the final configured CTest model, not a second test registry."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = "@DARLING_NATIVE_BUNDLE_ROOT@"
SYSTEM_PATHS = ("/bin", "/usr/bin", "/usr/lib", "/System/Library", "/dev/null")
PATH_TOKEN = re.compile(r"(?<![A-Za-z0-9_./@])/(?!/)(?:[^\s;:\"'<>|]+)")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cmake(value):
    if isinstance(value, bool):
        value = "TRUE" if value else "FALSE"
    elif isinstance(value, list):
        value = ";".join(str(item).replace(";", "\\;") for item in value)
    else:
        value = str(value)
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    value = value.replace("\n", "\\n").replace("\r", "\\r")
    return '"' + value.replace(ROOT, "${_DARLING_NATIVE_ROOT}") + '"'


class Relocator:
    def __init__(self, metadata, output):
        self.output = output
        self.binary = metadata["binary_root"]
        self.paths = sorted(metadata["paths"], key=lambda item: len(item["source"]), reverse=True)
        self.scopes = {scope["source"]: scope for scope in metadata["scopes"]}
        for item in self.paths:
            destination = Path(item["destination"])
            if destination.is_absolute() or ".." in destination.parts:
                raise ValueError(f"unsafe bundle destination: {destination}")
            if not (output / destination).exists():
                raise ValueError(f"installed dependency missing: {destination}")
            installed = output / destination
            dependencies = [installed]
            if installed.is_dir():
                dependencies.extend(installed.rglob("*"))
            for dependency in dependencies:
                if dependency.is_symlink():
                    link = Path(os.readlink(dependency))
                    if link.is_absolute() or not dependency.resolve().is_relative_to(output):
                        raise ValueError(f"unrelocatable installed symlink: {dependency}")
                    if not dependency.exists():
                        raise ValueError(f"dangling installed symlink: {dependency}")
        self.directories = set()

    def mapped_path(self, path):
        for item in self.paths:
            source = item["source"].rstrip("/")
            if path == source or (item["kind"] == "directory" and path.startswith(source + "/")):
                return ROOT + "/" + item["destination"] + path[len(source):]
        return None

    def relocate(self, value, *, cwd=None, directory=False):
        if isinstance(value, list):
            return [self.relocate(item, cwd=cwd, directory=directory) for item in value]
        if not isinstance(value, str):
            return value
        if directory and os.path.isabs(value):
            value = os.path.normpath(value)
            mapped = self.mapped_path(value)
            if mapped:
                return mapped
            if value == self.binary or value.startswith(self.binary + "/"):
                relative = os.path.relpath(value, self.binary)
                destination = "work" if relative == "." else "work/" + relative
                self.directories.add(destination)
                return ROOT + "/" + destination
        if cwd and not os.path.isabs(value) and (Path(cwd) / value).exists():
            path = str((Path(cwd) / value).resolve())
            mapped = self.mapped_path(path)
            if mapped:
                return mapped
            raise ValueError(f"relative dependency is not installed: {value} (cwd {cwd})")
        # Replace longest paths first, including paths embedded in env assignments
        # or --option=/path arguments. A path boundary prevents prefix collisions.
        for item in self.paths:
            source = item["source"].rstrip("/")
            suffix = r"(?=$|[\s;:\"'])" if item["kind"] == "file" else r"(?=$|[/\s;:\"'])"
            value = re.sub(re.escape(source) + suffix,
                           lambda _: ROOT + "/" + item["destination"], value)
        for token in PATH_TOKEN.findall(value.replace(ROOT, "BUNDLE")):
            if any(token == path or token.startswith(path + "/") for path in SYSTEM_PATHS):
                continue
            raise ValueError(f"unrelocatable external dependency {token!r} in {value!r}; declare it in RESOURCES")
        return value

    def scope(self, test, graph):
        node = test.get("backtrace")
        while node is not None:
            trace = graph["nodes"][node]
            source = str(Path(graph["files"][trace["file"]]).parent)
            if source in self.scopes:
                return source
            node = trace.get("parent")
        raise ValueError(f"cannot recover source scope for {test['name']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", default="")
    parser.add_argument("--ctest", default="ctest")
    args = parser.parse_args()
    metadata_path = args.build / f"native-bundle-paths-{args.config}.json"
    if not metadata_path.exists():
        metadata_path = args.build / "native-bundle-paths-.json"
    metadata = json.loads(metadata_path.read_text())
    command = [args.ctest, "--test-dir", str(args.build), "--show-only=json-v1"]
    if args.config:
        command += ["-C", args.config]
    model = json.loads(subprocess.run(command, check=True, stdout=subprocess.PIPE).stdout)
    output = args.output.resolve()
    relocator = Relocator(metadata, output)
    executables = {item["source"] for item in metadata["paths"]
                   if item["kind"] == "file" and item["destination"].startswith("testcase/")}
    selected = [test for test in model["tests"]
                if test["name"].startswith("macos/") and executables.intersection(test.get("command", []))]
    if not selected:
        raise ValueError("no installable native CTest cases")
    if sys.platform == "darwin":
        # An absolute non-system dylib install name cannot be relocated by
        # rewriting CTest. Refuse it rather than depend on the build machine.
        for item in metadata["paths"]:
            if item["source"] not in executables:
                continue
            executable = output / item["destination"]
            pending = [executable]
            visited = set()
            while pending:
                binary = pending.pop()
                if binary in visited:
                    continue
                visited.add(binary)
                libraries = subprocess.run(
                    ["/usr/bin/otool", "-L", str(binary)], check=True,
                    stdout=subprocess.PIPE, text=True,
                ).stdout
                for line in libraries.splitlines():
                    if not line.startswith("\t"):
                        continue
                    library = line.strip().split(" (", 1)[0]
                    if library.startswith(("/usr/lib/", "/System/Library/")):
                        continue
                    if library.startswith("@loader_path/"):
                        dependency = binary.parent / library[len("@loader_path/"):]
                    elif library.startswith("@executable_path/"):
                        dependency = executable.parent / library[len("@executable_path/"):]
                    else:
                        raise ValueError(f"unrelocatable dylib install name in {binary}: {library}; use a bundled @loader_path dependency")
                    dependency = dependency.resolve()
                    if not dependency.is_relative_to(output) or not dependency.is_file():
                        raise ValueError(f"missing bundled dylib for {binary}: {library}")
                    pending.append(dependency)
    suites = {}
    for test in selected:
        scope = relocator.scope(test, model["backtraceGraph"])
        props = {item["name"]: item["value"] for item in test.get("properties", [])}
        cwd = props.get("WORKING_DIRECTORY", relocator.scopes[scope]["binary"])
        props["WORKING_DIRECTORY"] = cwd
        argv = []
        for index, arg in enumerate(test["command"]):
            # The helper's CTest discovery root is not its working directory.
            if index and test["command"][index - 1] == "--ctest-root":
                if arg != metadata["binary_root"]:
                    raise ValueError("native verdict has an unexpected CTest root")
                argv.append(ROOT)
            else:
                argv.append(relocator.relocate(arg, cwd=cwd))
        relocated_props = {key: relocator.relocate(value, directory=key == "WORKING_DIRECTORY")
                           for key, value in props.items()}
        suites.setdefault(scope, []).append((test["name"], argv, relocated_props))
    # Filtering may not discard any setup/cleanup that CTest would run.
    selected_names = {test["name"] for test in selected}
    required_fixtures = set()
    for tests in suites.values():
        for name, _, props in tests:
            missing = set(props.get("DEPENDS", [])) - selected_names
            if missing:
                raise ValueError(f"{name}: dependency tests are not native installable: {sorted(missing)}")
            required_fixtures.update(props.get("FIXTURES_REQUIRED", []))
    for test in model["tests"]:
        for prop in test.get("properties", []):
            if (prop["name"] in ("FIXTURES_SETUP", "FIXTURES_CLEANUP")
                    and required_fixtures.intersection(prop["value"])
                    and test not in selected):
                raise ValueError(f"{test['name']}: fixture participant is not native installable")
    root_lines = [
        "# Generated from final CTest JSON; source registrations remain authoritative.",
        "# Direct CTest leaves LIST_DIR empty; subdirs resolves against its cwd.",
    ]
    scope_identity = []
    for scope, tests in sorted(suites.items()):
        relative = os.path.relpath(scope, metadata["source_root"])
        scope_id = hashlib.sha256(relative.encode()).hexdigest()[:16]
        directory = output / "_ctest" / scope_id
        directory.mkdir(parents=True, exist_ok=True)
        lines = ['get_filename_component(_DARLING_NATIVE_ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)']
        names = set()
        for name, argv, props in tests:
            if name in names:
                raise ValueError(f"duplicate CTest name in source scope {scope}: {name}")
            names.add(name)
            lines.append("add_test(" + " ".join(cmake(value) for value in [name, *argv]) + ")")
            lines.append("set_tests_properties(" + cmake(name) + " PROPERTIES " +
                         " ".join(cmake(value) for pair in props.items() for value in pair) + ")")
        (directory / "CTestTestfile.cmake").write_text("\n".join(lines) + "\n")
        root_lines.extend([
            "if(CMAKE_CURRENT_LIST_DIR)",
            f'  subdirs("${{CMAKE_CURRENT_LIST_DIR}}/_ctest/{scope_id}")',
            "else()",
            f'  subdirs("_ctest/{scope_id}")',
            "endif()",
        ])
        scope_identity.append({"source_scope": relative, "ctest_directory": f"_ctest/{scope_id}"})
    for directory in relocator.directories:
        (output / directory).mkdir(parents=True, exist_ok=True)
    sources = {}
    for target in metadata["targets"]:
        for source in target["sources"].split(";"):
            path = Path(source)
            if not path.is_absolute():
                path = Path(target["source_dir"]) / path
            if not path.is_file():
                raise ValueError(f"cannot digest registered native source: {path}")
            sources[str(path)] = digest(path)
    for source in model.get("backtraceGraph", {}).get("files", []):
        path = Path(source)
        if path.is_file():
            sources[str(path)] = digest(path)
    cache = {}
    for line in (args.build / "CMakeCache.txt").read_text().splitlines():
        match = re.match(r"([^/#][^:]*):[^=]*=(.*)", line)
        if match and (match[1].startswith(("CMAKE_C_", "CMAKE_OSX_", "CMAKE_BUILD_TYPE", "CMAKE_SYSTEM_", "CMAKE_EXE_LINKER_FLAGS"))):
            cache[match[1]] = match[2]
    cache.update(metadata["build_identity"])
    compiler = cache.get("CMAKE_C_COMPILER")
    compiler_version = subprocess.run([compiler, "--version"], check=True, stdout=subprocess.PIPE,
                                      text=True).stdout if compiler else None
    sdk = {"configured_root": cache.get("CMAKE_OSX_SYSROOT", "")}
    if sys.platform == "darwin":
        sdk_name = sdk["configured_root"] or "macosx"
        for field, option in (("path", "--show-sdk-path"), ("version", "--show-sdk-version")):
            sdk[field] = subprocess.run(
                ["/usr/bin/xcrun", "--sdk", sdk_name, option], check=True,
                stdout=subprocess.PIPE, text=True,
            ).stdout.strip()
    identity = {"schema": 1, "source_root": metadata["source_root"], "build_root": metadata["binary_root"],
                "configuration": args.config, "source_files": sources,
                "source_digest": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
                "compiler_version": compiler_version, "sdk": sdk, "cmake": cache,
                "targets": metadata["targets"], "scopes": scope_identity,
                "configured_ctest_digest": hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()}
    (output / "native-build.json").write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    (output / "CTestTestfile.cmake").write_text("\n".join(root_lines) + "\n")
    print(f"Exported {len(selected)} native CTest cases in {len(suites)} source scopes to {output}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"Native bundle export failed: {exc}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Behavioral safety contract for every registered West extension help path."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import types
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_RUNTIME_MARKER = "WEST_EXTENSION_HELP_CONTRACT_RUNTIME"
PROCESS_TOKEN_ENV = "WEST_EXTENSION_HELP_CONTRACT_TOKEN"
HELP_TIMEOUT_SECONDS = 10
OUTPUT_LIMIT = 16_000
OUTPUT_FILE_LIMIT = 1024 * 1024
SETUP_TIMEOUT_SECONDS = 10
TERMINATION_TIMEOUT_SECONDS = 2
HASH_CHUNK_SIZE = 64 * 1024
SHEBANG_LIMIT = 256
CONTRACT_NAME = "west-extension-help-contract"
REQUIRED_DW_HELP_PATHS = {
    ("dw", "summary"),
    ("dw", "beads"),
    ("dw", "restore"),
    ("dw", "handoff"),
}


def _note(message: str) -> None:
    """Report an environment resolution to the operator once per run.

    The re-executed pass inherits the marker and stays quiet, so a resolution
    is stated once, by the pass that resolved it.
    """
    if os.environ.get(CONTRACT_RUNTIME_MARKER) != "1":
        print(f"{CONTRACT_NAME}: {message}", file=sys.stderr)


def _first_line(path: Path) -> bytes:
    """Return the first line of a file as bytes.

    The resolved west can be a launcher rather than a script, so this never
    decodes: reading a binary with a strict codec is what used to raise
    UnicodeDecodeError from inside this contract.
    """
    try:
        with path.open("rb") as stream:
            return stream.readline(SHEBANG_LIMIT)
    except OSError as error:
        raise RuntimeError(f"cannot read the resolved west executable {path}: {error}") from error


def _shebang_interpreter(script: Path) -> Path | None:
    """Return the interpreter a script's `#!` line names, or None when it has none."""
    first_line = _first_line(script)
    if not first_line.startswith(b"#!"):
        return None
    try:
        shebang = first_line.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError(
            f"the `#!` line of {script} is not UTF-8: {first_line!r}"
        ) from error
    interpreter = Path(shebang[2:].strip())
    if not interpreter.is_file():
        raise RuntimeError(
            f"the interpreter named by the `#!` line of {script} does not exist: {interpreter}"
        )
    return interpreter


def _dispatched_west_entry_point(executable: Path) -> Path | None:
    """Return the entry script a launcher on PATH runs for west, or None.

    `which west` can name a launcher rather than a Python entry script: the mise
    shim is a symlink to the mise binary, which selects a tool by the name it was
    invoked with and needs mise's own data directory, so it also fails under the
    isolated HOME this contract gives every help invocation. mise names the
    console script its shim dispatches to.
    """
    launcher = shutil.which("mise")
    if launcher is None:
        return None
    try:
        completed = subprocess.run(
            [launcher, "which", "west"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=SETUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    entry_point = Path(lines[-1]).absolute()
    if entry_point == executable or not entry_point.is_file():
        return None
    return entry_point


def _imports_west(interpreter: Path) -> bool:
    """Return whether an interpreter can import west."""
    try:
        completed = subprocess.run(
            [str(interpreter), "-c", "import west"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SETUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _west_executable() -> Path:
    """Resolve west to the Python entry script this workspace runs.

    `which west` is honored as it stands. When it names a launcher instead, the
    launcher's own dispatcher names the entry script it runs, and that script is
    what this contract executes: it carries the interpreter pin, and unlike the
    launcher it does not depend on its tool manager's data directory.
    """
    executable = shutil.which("west")
    if executable is None:
        raise RuntimeError(
            "west is not available in PATH; run this contract in the pinned tool environment"
        )
    path = Path(executable).absolute()
    if _shebang_interpreter(path) is not None:
        return path
    entry_point = _dispatched_west_entry_point(path)
    if entry_point is not None and _shebang_interpreter(entry_point) is not None:
        _note(f"{path} is not a Python entry script; running {entry_point} instead")
        return entry_point
    return path


def _west_interpreter(executable: Path) -> Path:
    """Return the interpreter that provides the pinned west.

    An entry script names it in its `#!` line. A launcher that names none leaves
    the interpreter running this contract, which the host tier provides, but only
    once it has been shown to import west: the pin is a claim about west, not
    about a shebang this environment may not have.
    """
    interpreter = _shebang_interpreter(executable)
    if interpreter is not None:
        return interpreter
    dispatched = _dispatched_west_entry_point(executable)
    if dispatched is not None:
        interpreter = _shebang_interpreter(dispatched)
        if interpreter is not None:
            return interpreter
    running = Path(sys.executable)
    if _imports_west(running):
        _note(f"{executable} names no interpreter; pinning west to {running}, which imports west")
        return running
    raise RuntimeError(
        f"cannot identify the interpreter that provides west: {executable} names none and "
        f"{running} does not import west"
    )


def _ensure_pinned_runtime(executable: Path) -> None:
    interpreter = _west_interpreter(executable)
    already_running = Path(sys.executable).absolute() == interpreter.absolute()
    if already_running:
        return
    if os.environ.get(CONTRACT_RUNTIME_MARKER) == "1":
        raise RuntimeError(
            f"failed to enter pinned West runtime {interpreter}; current interpreter is {sys.executable}"
        )
    environment = os.environ.copy()
    environment[CONTRACT_RUNTIME_MARKER] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execve(
        interpreter,
        [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]],
        environment,
    )


def _write_git_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[user]\n"
        "name = West Help Contract\n"
        "email = west-help-contract.invalid\n"
        "[core]\n"
        "hooksPath = /dev/null\n"
        "[commit]\n"
        "gpgSign = false\n"
        "[tag]\n"
        "gpgSign = false\n",
        encoding="utf-8",
    )


def _sanitized_git_environment(
    base: dict[str, str],
    *,
    home: Path,
    global_config: Path,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in base.items()
        if not key.startswith("GIT_")
    }
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "SSH_ASKPASS": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _git_command(real_git: Path, *arguments: str) -> list[str]:
    return [str(real_git), *arguments]


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SETUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"command timed out after {SETUP_TIMEOUT_SECONDS} seconds: {command!r}"
        ) from error
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def _initialize_git_repository(
    real_git: Path,
    repository: Path,
    marker_name: str,
    *,
    git_home: Path,
) -> None:
    repository.mkdir(parents=True, exist_ok=True)
    git_home.mkdir(parents=True, exist_ok=True)
    (git_home / ".config").mkdir(exist_ok=True)
    global_config = git_home / "gitconfig"
    _write_git_config(global_config)
    environment = _sanitized_git_environment(
        os.environ,
        home=git_home,
        global_config=global_config,
    )
    (repository / marker_name).write_text(f"{marker_name}\n", encoding="utf-8")
    _run_checked(
        _git_command(real_git, "init", "-q", "--initial-branch=contract"),
        cwd=repository,
        environment=environment,
    )
    _run_checked(
        _git_command(real_git, "add", "--all"),
        cwd=repository,
        environment=environment,
    )
    _run_checked(
        _git_command(real_git, "commit", "-q", "-m", "contract fixture"),
        cwd=repository,
        environment=environment,
    )


def _write_guard(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "printf '%s' \"$0\" >>\"$WEST_HELP_GUARD_LOG\"\n"
        "for argument in \"$@\"; do printf '\\t%s' \"$argument\" >>\"$WEST_HELP_GUARD_LOG\"; done\n"
        "printf '\\n' >>\"$WEST_HELP_GUARD_LOG\"\n"
        "exit 97\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _copy_manifest_fixture(sandbox: Path, real_git: Path) -> tuple[Path, Path, Path]:
    topdir = sandbox / "workspace"
    git_home = sandbox / "git-home"
    manifest = topdir / "darling-workspace"
    topdir.mkdir(parents=True)
    manifest.mkdir()
    shutil.copy2(ROOT / "west.yml", manifest / "west.yml")
    shutil.copy2(ROOT / "west-commands.yml", manifest / "west-commands.yml")
    shutil.copytree(
        ROOT / "west_commands",
        manifest / "west_commands",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    # These are absolute-path execution boundaries in dw.py. Replacing them in
    # the disposable copy makes the historical handoff-help bug observable and
    # harmless while keeping the real extension implementation under test.
    _write_guard(manifest / "bin" / "dw")
    _write_guard(manifest / "scripts" / "west_workspace.py")
    _initialize_git_repository(
        real_git,
        manifest,
        "manifest-fixture",
        git_home=git_home,
    )

    darling = topdir / "darling"
    _initialize_git_repository(
        real_git,
        darling,
        "darling-fixture",
        git_home=git_home,
    )

    west_config = topdir / ".west" / "config"
    west_config.parent.mkdir()
    west_config.write_text(
        "[manifest]\npath = darling-workspace\nfile = west.yml\n",
        encoding="utf-8",
    )
    return topdir, manifest, darling


def _load_registered_commands(manifest: Path) -> list[tuple[str, type]]:
    import yaml

    registry = yaml.safe_load((manifest / "west-commands.yml").read_text(encoding="utf-8"))
    entries = registry.get("west-commands")
    if not isinstance(entries, list) or not entries:
        raise AssertionError("west-commands.yml has no registered extension commands")

    module_directory = str(manifest / "west_commands")
    sys.path.insert(0, module_directory)
    modules: dict[Path, types.ModuleType] = {}
    commands: list[tuple[str, type]] = []
    for entry_number, entry in enumerate(entries):
        module_path = (manifest / entry["file"]).resolve()
        module = modules.get(module_path)
        if module is None:
            module_name = f"_west_help_contract_{entry_number}_{module_path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec is None or spec.loader is None:
                raise AssertionError(f"cannot load registered command module: {module_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            modules[module_path] = module
        for command_entry in entry.get("commands", []):
            command_name = command_entry["name"]
            command_class = getattr(module, command_entry["class"])
            commands.append((command_name, command_class))
    if len({name for name, _command_class in commands}) != len(commands):
        raise AssertionError("west-commands.yml contains duplicate command names")
    return commands


def _nested_paths(parser: argparse.ArgumentParser, prefix: tuple[str, ...]):
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child_parser in action.choices.items():
            child_path = (*prefix, name)
            yield child_path
            yield from _nested_paths(child_parser, child_path)


def _discover_help_paths(commands: list[tuple[str, type]]) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for registered_name, command_class in commands:
        root_parser = argparse.ArgumentParser(prog="west")
        parser_adder = root_parser.add_subparsers(dest="command", required=True)
        command = command_class()
        command_parser = command.do_add_parser(parser_adder)
        if registered_name not in parser_adder.choices:
            raise AssertionError(
                f"registered command {registered_name!r} did not construct a parser with the same name"
            )
        if command_parser is not parser_adder.choices[registered_name]:
            raise AssertionError(f"{registered_name!r} returned a parser other than its registered parser")
        paths.append((registered_name,))
        paths.extend(_nested_paths(command_parser, (registered_name,)))
    if len(set(paths)) != len(paths):
        raise AssertionError(f"parser discovery produced duplicate help paths: {paths!r}")
    return paths


def _expect_exit_code(call, expected: int) -> None:
    try:
        call()
    except SystemExit as error:
        if error.code != expected:
            raise AssertionError(f"expected SystemExit({expected}), got {error.code!r}") from error
    else:
        raise AssertionError(f"expected SystemExit({expected})")


def _assert_dw_dispatch_contract(command_class: type, topdir: Path, manifest_repo: Path) -> None:
    root_parser = argparse.ArgumentParser(prog="west")
    parser_adder = root_parser.add_subparsers(dest="command", required=True)
    parser_command = command_class()
    parser_command.do_add_parser(parser_adder)
    if parser_command.accepts_unknown_args is not True:
        raise AssertionError("dw must continue accepting unknown passthrough arguments")

    with redirect_stderr(StringIO()):
        _expect_exit_code(lambda: root_parser.parse_known_args(["dw"]), 2)

    projects = [
        types.SimpleNamespace(
            groups=["private"],
            path="darling",
            userdata={"kind": "darling-source"},
        ),
        types.SimpleNamespace(
            groups=[],
            path="darling/src/child",
            userdata={"kind": "darling-source"},
        ),
        types.SimpleNamespace(
            groups=[],
            path="workspace-tool",
            userdata={"kind": "workspace-tool"},
        ),
    ]
    parser_command.manifest = types.SimpleNamespace(
        repo_abspath=manifest_repo,
        projects=projects,
        is_active=lambda project: project is not projects[2],
    )
    parser_command.topdir = str(topdir)
    messages: list[str] = []
    parser_command.inf = messages.append
    command_module = sys.modules[parser_command.__class__.__module__]

    summary_args, summary_unknown = root_parser.parse_known_args(["dw", "summary"])
    if summary_unknown:
        raise AssertionError(f"summary unexpectedly produced unknown arguments: {summary_unknown!r}")
    with mock.patch.object(command_module.subprocess, "run") as run:
        parser_command.do_run(summary_args, summary_unknown)
        run.assert_not_called()
    expected_messages = [
        f"workspace: {topdir}",
        f"manifest:  {manifest_repo}",
        "projects:  3 (2 active)",
        "private:   1",
    ]
    if messages != expected_messages:
        raise AssertionError(f"summary output changed: {messages!r}")

    beads_args, beads_unknown = root_parser.parse_known_args(
        ["dw", "beads", "--json", "show", "dar-1"]
    )
    if beads_args.args != ["--json", "show", "dar-1"] or beads_unknown:
        raise AssertionError(
            "beads child tail did not retain its parser boundary: "
            f"{beads_args.args!r}, {beads_unknown!r}"
        )
    beads_result = types.SimpleNamespace(returncode=17)
    with mock.patch.object(
        command_module.subprocess,
        "run",
        return_value=beads_result,
    ) as run:
        _expect_exit_code(
            lambda: parser_command.do_run(beads_args, beads_unknown),
            17,
        )
    beads_call = run.call_args
    if beads_call.args[0] != ["br", "--json", "show", "dar-1"]:
        raise AssertionError(f"beads argv/order changed: {beads_call.args[0]!r}")
    if beads_call.kwargs["cwd"] != manifest_repo or beads_call.kwargs["check"] is not False:
        raise AssertionError(f"beads subprocess options changed: {beads_call!r}")
    if beads_call.kwargs["env"]["BEADS_DIR"] != str(manifest_repo / ".beads"):
        raise AssertionError(f"beads environment changed: {beads_call.kwargs['env']!r}")
    parent_args, parent_unknown = root_parser.parse_known_args(
        ["dw", "--json", "beads", "show"]
    )
    if parent_args.args != ["show"] or parent_unknown != ["--json"]:
        raise AssertionError(
            "dw parent unknown arguments crossed the child boundary: "
            f"{parent_args.args!r}, {parent_unknown!r}"
        )
    parent_result = types.SimpleNamespace(returncode=18)
    with mock.patch.object(
        command_module.subprocess,
        "run",
        return_value=parent_result,
    ) as run:
        _expect_exit_code(
            lambda: parser_command.do_run(parent_args, parent_unknown),
            18,
        )
    if run.call_args.args[0] != ["br", "show", "--json"]:
        raise AssertionError(
            f"dw parent/child argv ordering changed: {run.call_args.args[0]!r}"
        )

    with redirect_stdout(StringIO()):
        _expect_exit_code(
            lambda: root_parser.parse_known_args(["dw", "beads", "--help"]),
            0,
        )

    backend_help_args, backend_help_unknown = root_parser.parse_known_args(
        ["dw", "beads", "show", "dar-1", "--help"]
    )
    if backend_help_args.args != ["show", "dar-1", "--help"] or backend_help_unknown:
        raise AssertionError(
            "beads trailing backend help crossed the child boundary: "
            f"{backend_help_args.args!r}, {backend_help_unknown!r}"
        )
    backend_help_result = types.SimpleNamespace(returncode=19)
    with mock.patch.object(
        command_module.subprocess,
        "run",
        return_value=backend_help_result,
    ) as run:
        _expect_exit_code(
            lambda: parser_command.do_run(
                backend_help_args,
                backend_help_unknown,
            ),
            19,
        )
    if run.call_args.args[0] != ["br", "show", "dar-1", "--help"]:
        raise AssertionError(
            f"beads trailing backend help argv changed: {run.call_args.args[0]!r}"
        )

    restore_args, restore_unknown = root_parser.parse_known_args(
        ["dw", "restore", "--force", "project-one"]
    )
    if restore_args.args != ["--force", "project-one"] or restore_unknown:
        raise AssertionError(
            "restore child tail did not retain its parser boundary: "
            f"{restore_args.args!r}, {restore_unknown!r}"
        )
    restore_result = types.SimpleNamespace(returncode=23)
    with mock.patch.object(
        command_module.subprocess,
        "run",
        return_value=restore_result,
    ) as run:
        _expect_exit_code(
            lambda: parser_command.do_run(restore_args, restore_unknown),
            23,
        )
    expected_restore = [
        str(manifest_repo / "scripts" / "west_workspace.py"),
        "--topdir",
        str(topdir),
        "--manifest-repo",
        str(manifest_repo),
        "restore",
        "--force",
        "project-one",
    ]
    if run.call_args.args[0] != expected_restore:
        raise AssertionError(f"restore argv/order changed: {run.call_args.args[0]!r}")
    if run.call_args.kwargs != {"cwd": manifest_repo, "check": False}:
        raise AssertionError(f"restore subprocess options changed: {run.call_args!r}")

    handoff_args, handoff_unknown = root_parser.parse_known_args(
        ["dw", "handoff", "--verbose", "portable"]
    )
    if handoff_args.args != ["--verbose", "portable"] or handoff_unknown:
        raise AssertionError(
            "handoff child tail did not retain its parser boundary: "
            f"{handoff_args.args!r}, {handoff_unknown!r}"
        )
    handoff_result = types.SimpleNamespace(returncode=29)
    with mock.patch.object(
        command_module.subprocess,
        "run",
        return_value=handoff_result,
    ) as run:
        _expect_exit_code(
            lambda: parser_command.do_run(handoff_args, handoff_unknown),
            29,
        )
    expected_handoff = [
        str(manifest_repo / "bin" / "dw"),
        "handoff",
        "--verbose",
        "portable",
    ]
    handoff_call = run.call_args
    if handoff_call.args[0] != expected_handoff:
        raise AssertionError(f"handoff argv/order changed: {handoff_call.args[0]!r}")
    if handoff_call.kwargs["cwd"] != manifest_repo or handoff_call.kwargs["check"] is not False:
        raise AssertionError(f"handoff subprocess options changed: {handoff_call!r}")
    expected_source = str(topdir / "darling")
    if handoff_call.kwargs["env"]["DW_DARLING_SRC"] != expected_source:
        raise AssertionError(f"handoff environment changed: {handoff_call.kwargs['env']!r}")
    expected_closure = json.dumps([".", "src/child"], separators=(",", ":"))
    if handoff_call.kwargs["env"]["DW_HANDOFF_EXPECTED_PROJECTS"] != expected_closure:
        raise AssertionError(
            f"handoff closure environment changed: {handoff_call.kwargs['env']!r}"
        )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_tree(root: Path) -> dict[str, tuple]:
    snapshot: dict[str, tuple] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", mode)
        elif path.is_file():
            digest = _file_digest(path)
            snapshot[relative] = ("file", mode, metadata.st_size, digest)
        else:
            snapshot[relative] = ("other", mode, metadata.st_mode)
    return snapshot


def _git_snapshot(
    real_git: Path,
    repository: Path,
    *,
    git_home: Path,
) -> tuple[str, str, str]:
    environment = _sanitized_git_environment(
        os.environ,
        home=git_home,
        global_config=git_home / "gitconfig",
    )
    status = _run_checked(
        _git_command(
            real_git,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        cwd=repository,
        environment=environment,
    )
    refs = _run_checked(
        _git_command(
            real_git,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ),
        cwd=repository,
        environment=environment,
    )
    head = _run_checked(
        _git_command(real_git, "rev-parse", "HEAD"),
        cwd=repository,
        environment=environment,
    )
    return status, refs, head


def _process_snapshot(sandbox: Path | None, token: str) -> dict[int, tuple[str, str, str]]:
    result: dict[int, tuple[str, str, str]] = {}
    token_marker = f"{PROCESS_TOKEN_ENV}={token}".encode()
    sandbox_bytes = os.fsencode(str(sandbox)) if sandbox is not None else None
    proc = Path("/proc")
    if not proc.is_dir():
        raise RuntimeError("/proc is unavailable; process side effects cannot be inspected")
    try:
        (proc / "self" / "environ").read_bytes()
        os.readlink(proc / "self" / "cwd")
        (proc / "self" / "stat").read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(
            "/proc process inspection is unavailable; refusing to skip process checks"
        ) from error
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes()
            command_bytes = (entry / "cmdline").read_bytes()
            cwd = os.readlink(entry / "cwd")
            stat_fields = (entry / "stat").read_text(encoding="utf-8").split()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        relevant = token_marker in environ
        if sandbox_bytes is not None:
            relevant = relevant or sandbox_bytes in environ or sandbox_bytes in command_bytes
            relevant = relevant or str(cwd).startswith(str(sandbox))
        if relevant:
            command = command_bytes.replace(b"\0", b" ").decode(errors="replace").strip()
            start_time = stat_fields[21] if len(stat_fields) > 21 else "unknown"
            result[pid] = (start_time, command, cwd)
    return result


def _terminate_processes(processes: dict[int, tuple[str, str, str]]) -> None:
    for pid in processes:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _read_bounded_stream(stream) -> str:
    stream.flush()
    size = stream.seek(0, os.SEEK_END)
    if size <= OUTPUT_LIMIT:
        stream.seek(0)
        payload = stream.read()
    else:
        half = OUTPUT_LIMIT // 2
        stream.seek(0)
        head = stream.read(half)
        stream.seek(-half, os.SEEK_END)
        tail = stream.read(half)
        payload = head + b"\n... output truncated ...\n" + tail
    return payload.decode("utf-8", errors="replace")


def _limit_child_output() -> None:
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (OUTPUT_FILE_LIMIT, OUTPUT_FILE_LIMIT),
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)


def _run_help(
    west: Path,
    path: tuple[str, ...],
    *,
    topdir: Path,
    environment: dict[str, str],
    sandbox: Path,
    token: str,
    output_directory: Path,
) -> tuple[int, str, str, bool, bool]:
    with (
        tempfile.TemporaryFile(dir=output_directory) as stdout_file,
        tempfile.TemporaryFile(dir=output_directory) as stderr_file,
    ):
        process = subprocess.Popen(
            [str(west), *path, "--help"],
            cwd=topdir,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
            preexec_fn=_limit_child_output,
        )
        timed_out = False
        try:
            process.wait(timeout=HELP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError(
                        f"unable to reap timed-out help process {process.pid}"
                    ) from error
            _terminate_processes(_process_snapshot(sandbox, token))
        stdout_file.flush()
        stderr_file.flush()
        output_overflowed = (
            os.fstat(stdout_file.fileno()).st_size >= OUTPUT_FILE_LIMIT
            or os.fstat(stderr_file.fileno()).st_size >= OUTPUT_FILE_LIMIT
            or process.returncode == -signal.SIGXFSZ
        )
        stdout = _read_bounded_stream(stdout_file)
        stderr = _read_bounded_stream(stderr_file)
    return process.returncode, stdout, stderr, timed_out, output_overflowed


def _tree_difference(before: dict[str, tuple], after: dict[str, tuple]) -> str:
    changed = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    if not changed:
        return ""
    details = []
    for path in changed[:20]:
        details.append(f"{path}: {before.get(path)!r} -> {after.get(path)!r}")
    if len(changed) > 20:
        details.append(f"... and {len(changed) - 20} more paths")
    return "\n".join(details)


def _assert_all_real_help_paths(
    west: Path,
    paths: list[tuple[str, ...]],
    *,
    sandbox: Path,
    topdir: Path,
    manifest: Path,
    real_git: Path,
    guard_log: Path,
    prefix: Path,
    job_state: Path,
    token: str,
    preexisting_failures: list[str],
) -> None:
    guard_bin = sandbox / "guard-bin"
    guard_bin.mkdir()
    for executable in (
        "br",
        "repo",
        "darling",
        "cmake",
        "ctest",
        "make",
        "ninja",
        "sudo",
        "pkill",
        "killall",
    ):
        _write_guard(guard_bin / executable)
    guard_log.write_text("", encoding="utf-8")

    prefix.mkdir()
    (prefix / "sentinel").write_text("prefix untouched\n", encoding="utf-8")
    job_state.mkdir()
    (job_state / "sentinel").write_text("job state untouched\n", encoding="utf-8")

    isolated_home = sandbox / "home"
    isolated_tmp = sandbox / "tmp"
    git_home = sandbox / "git-home"
    output_directory = sandbox / "output"
    west_global_config = sandbox / "west-global-config"
    west_system_config = sandbox / "west-system-config"
    git_global_config = sandbox / "git-global-config"
    _write_git_config(git_global_config)
    west_global_config.write_text("", encoding="utf-8")
    west_system_config.write_text("", encoding="utf-8")
    isolated_home.mkdir()
    isolated_tmp.mkdir()
    output_directory.mkdir()
    for directory in ("config", "cache", "data", "state"):
        (isolated_home / f".xdg-{directory}").mkdir()

    environment = _sanitized_git_environment(
        os.environ,
        home=isolated_home,
        global_config=git_global_config,
    )
    environment.update(
        {
            "PATH": os.pathsep.join((str(guard_bin), environment.get("PATH", ""))),
            "HOME": str(isolated_home),
            "TMPDIR": str(isolated_tmp),
            "XDG_CONFIG_HOME": str(isolated_home / ".xdg-config"),
            "XDG_CACHE_HOME": str(isolated_home / ".xdg-cache"),
            "XDG_DATA_HOME": str(isolated_home / ".xdg-data"),
            "XDG_STATE_HOME": str(isolated_home / ".xdg-state"),
            "DARLING_PREFIX": str(prefix),
            "DPREFIX": str(prefix),
            "WEST_JOB_STATE_DIR": str(job_state),
            "WEST_HELP_GUARD_LOG": str(guard_log),
            PROCESS_TOKEN_ENV: token,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",
            "WEST_CONFIG_LOCAL": str(topdir / ".west" / "config"),
            "WEST_CONFIG_GLOBAL": str(west_global_config),
            "WEST_CONFIG_SYSTEM": str(west_system_config),
        }
    )

    failures = list(preexisting_failures)
    for help_path in paths:
        before_git = _git_snapshot(
            real_git,
            manifest,
            git_home=git_home,
        )
        before_tree = _snapshot_tree(sandbox)
        before_prefix = _snapshot_tree(prefix)
        before_job = _snapshot_tree(job_state)
        before_guard = guard_log.read_text(encoding="utf-8")
        before_processes = _process_snapshot(sandbox, token)

        returncode, stdout, stderr, timed_out, output_overflowed = _run_help(
            west,
            help_path,
            topdir=topdir,
            environment=environment,
            sandbox=sandbox,
            token=token,
            output_directory=output_directory,
        )

        after_git = _git_snapshot(
            real_git,
            manifest,
            git_home=git_home,
        )
        after_tree = _snapshot_tree(sandbox)
        after_prefix = _snapshot_tree(prefix)
        after_job = _snapshot_tree(job_state)
        after_guard = guard_log.read_text(encoding="utf-8")
        after_processes = _process_snapshot(sandbox, token)

        differences: list[str] = []
        if timed_out:
            differences.append(f"timed out after {HELP_TIMEOUT_SECONDS} seconds")
        if returncode != 0:
            differences.append(f"return code was {returncode}, expected 0")
        if output_overflowed:
            differences.append(
                f"captured output reached the {OUTPUT_FILE_LIMIT}-byte hard limit"
            )
        if "usage:" not in stdout.lower() and "usage:" not in stderr.lower():
            differences.append("output did not contain argparse help usage")
        tree_difference = _tree_difference(before_tree, after_tree)
        if tree_difference:
            differences.append(f"filesystem changed:\n{tree_difference}")
        if before_git != after_git:
            differences.append(f"manifest Git status/refs changed: {before_git!r} -> {after_git!r}")
        if before_prefix != after_prefix:
            differences.append("Darling prefix sentinel changed")
        if before_job != after_job:
            differences.append("West job sentinel changed")
        if before_guard != after_guard:
            differences.append(
                "guarded backend was dispatched:\n" + after_guard[len(before_guard) :]
            )
        if before_processes != after_processes:
            differences.append(
                f"task-owned process state changed: {before_processes!r} -> {after_processes!r}"
            )
            leaked = {
                pid: state
                for pid, state in after_processes.items()
                if before_processes.get(pid) != state
            }
            _terminate_processes(leaked)
        if differences:
            rendered_path = "west " + " ".join(help_path) + " --help"
            failures.append(
                f"{rendered_path}\n"
                + "\n".join(differences)
                + f"\nstdout:\n{stdout}"
                + f"\nstderr:\n{stderr}"
            )

    if failures:
        raise AssertionError("\n\n".join(failures))


def main() -> None:
    west = _west_executable()
    _ensure_pinned_runtime(west)
    real_git_name = shutil.which("git")
    if real_git_name is None:
        raise RuntimeError("git is required to build and inspect the disposable workspace")
    real_git = Path(real_git_name).resolve()
    token = uuid.uuid4().hex

    try:
        with tempfile.TemporaryDirectory(prefix="west-extension-help-contract-") as directory:
            sandbox = Path(directory)
            discovery_sandbox = sandbox / "discovery"
            discovery_topdir, discovery_manifest, _discovery_darling = (
                _copy_manifest_fixture(discovery_sandbox, real_git)
            )
            commands = _load_registered_commands(discovery_manifest)
            discovered_paths = _discover_help_paths(commands)
            missing_dw_paths = REQUIRED_DW_HELP_PATHS.difference(discovered_paths)
            paths = [
                *discovered_paths,
                *sorted(missing_dw_paths),
            ]
            discovery_failures = []
            if missing_dw_paths:
                discovery_failures.append(
                    "dw parser construction omitted required help paths: "
                    f"{sorted(missing_dw_paths)!r}"
                )
            command_classes = dict(commands)
            if "dw" not in command_classes:
                raise AssertionError("west-commands.yml does not register dw")

            # Real West gets a fresh fixture which parser discovery and direct
            # dispatch assertions have never imported or exercised.
            runtime_sandbox = sandbox / "runtime"
            runtime_topdir, runtime_manifest, _runtime_darling = (
                _copy_manifest_fixture(runtime_sandbox, real_git)
            )
            _assert_all_real_help_paths(
                west,
                paths,
                sandbox=runtime_sandbox,
                topdir=runtime_topdir,
                manifest=runtime_manifest,
                real_git=real_git,
                guard_log=runtime_sandbox / "guard.log",
                prefix=runtime_sandbox / "prefix",
                job_state=runtime_sandbox / "job-state",
                token=token,
                preexisting_failures=discovery_failures,
            )
            _assert_dw_dispatch_contract(
                command_classes["dw"],
                discovery_topdir,
                discovery_manifest,
            )
    finally:
        _terminate_processes(_process_snapshot(None, token))

    print(f"PASS {CONTRACT_NAME} ({len(paths)} help paths)")


if __name__ == "__main__":
    main()

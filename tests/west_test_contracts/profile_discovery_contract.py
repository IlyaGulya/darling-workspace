"""Focused contract for dynamic profile discovery and Bash value completion."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = ROOT / "west_commands"
sys.path.insert(0, str(COMMANDS))
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    def __init__(self, name: str, help_text: str, description: str, **kwargs) -> None:
        self.name = name
        self.help = help_text
        self.description = description
        self.accepts_unknown_args = kwargs.get("accepts_unknown_args", False)

    def die(self, message: str, exit_code: int = 1) -> None:
        raise SystemExit(f"{exit_code}: {message}")


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands import dev
from west_commands import patch as patch_command
import profile_catalog
from west_commands import test as test_command


def write_patch_profile(
    root: Path,
    name: str,
    count: int = 1,
    description: str | None = None,
    base_profile: str | None = None,
) -> Path:
    path = root / "patches" / name / "patches.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "version": 1,
        "description": description if description is not None else f"{name} description",
        "patches": [{"module": f"module-{index}"} for index in range(count)],
    }
    if base_profile is not None:
        data["base-profile"] = base_profile
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def runtime_definition(source_profile: str, source_module: str, *, purpose: str = "runtime") -> dict:
    return {
        "source-profile": source_profile,
        "source-module": source_module,
        "source-modules": [source_module],
        "runtime-artifacts": [
            {
                "module": source_module,
                "build-targets": ["demo-runtime"],
                "deploy": ["usr/lib/demo-runtime.dylib"],
            }
        ],
        "purpose": purpose,
    }


def write_runtime_profiles(root: Path, profiles: dict[str, dict]) -> Path:
    path = root / "testkit" / "runtime-profiles.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"runtime-profiles": profiles}, sort_keys=False))
    return path


def build_parser(command: WestCommand) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    command.do_add_parser(parser.add_subparsers(dest="command", required=True))
    return parser


def expect_parse_error(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    with redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(argv)
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError(f"parser accepted invalid arguments: {argv!r}")


def expect_catalog_error(root: Path, fragment: str) -> None:
    try:
        profile_catalog.collect_profile_catalog(root)
    except profile_catalog.ProfileCatalogError as error:
        assert fragment in str(error), (fragment, str(error))
    else:
        raise AssertionError(f"catalog accepted invalid fixture expecting {fragment!r}")


def catalog_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="profile-catalog-contract-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "zeta", count=2)
        write_patch_profile(root, "shared", count=3, base_profile="zeta")
        write_patch_profile(root, "alpha space", count=0, description="spaced profile")
        ignored = root / "patches" / "nested" / "deeper" / "patches.yml"
        ignored.parent.mkdir(parents=True)
        ignored.write_text("patches: []\n")
        common_runtime = runtime_definition("shared", "darling/shared")
        write_runtime_profiles(
            root,
            {
                "z-runtime": runtime_definition("zeta", "darling/zeta"),
                "shared": common_runtime,
                "shared alias": common_runtime,
                "a-runtime": runtime_definition("alpha space", "darling/alpha"),
            },
        )

        patch_loads: list[Path] = []
        runtime_loads: list[Path] = []
        real_patch_loader = profile_catalog.test_manifest.load_test_profile
        real_runtime_loader = profile_catalog.load_ctest_runtime_profiles

        def load_patch(path: Path):
            patch_loads.append(path)
            return real_patch_loader(path)

        def load_runtime(path: Path):
            runtime_loads.append(path)
            return real_runtime_loader(path)

        profile_catalog.test_manifest.load_test_profile = load_patch
        profile_catalog.load_ctest_runtime_profiles = load_runtime
        real_subprocess_run = subprocess.run

        def forbid_process(*_args, **_kwargs):
            raise AssertionError("profile discovery started a subprocess")

        subprocess.run = forbid_process
        try:
            payload = profile_catalog.collect_profile_catalog(root)
            repeated = profile_catalog.collect_profile_catalog(root)
        finally:
            subprocess.run = real_subprocess_run
            profile_catalog.test_manifest.load_test_profile = real_patch_loader
            profile_catalog.load_ctest_runtime_profiles = real_runtime_loader

        assert payload == repeated
        assert payload["schema_version"] == 1
        assert payload["operation"] == "profiles"
        assert payload["state"] == "valid"
        assert payload["inputs"] == {"kind": "all"}
        assert payload["returncode"] == 0
        assert [(row["kind"], row["name"]) for row in payload["profiles"]] == [
            ("patch", "alpha space"),
            ("patch", "shared"),
            ("patch", "zeta"),
            ("runtime", "a-runtime"),
            ("runtime", "shared"),
            ("runtime", "shared alias"),
            ("runtime", "z-runtime"),
        ]
        assert len(patch_loads) == 6
        assert len(runtime_loads) == 2
        assert all("deeper" not in str(path) for path in patch_loads)

        patch_row = next(
            row for row in payload["profiles"]
            if row["kind"] == "patch" and row["name"] == "shared"
        )
        assert patch_row == {
            "kind": "patch",
            "name": "shared",
            "description": "shared description",
            "base_profile": "zeta",
            "patch_count": 3,
            "path": "patches/shared/patches.yml",
        }
        runtime_rows = [
            row for row in payload["profiles"] if row["kind"] == "runtime"
        ]
        assert {row["name"] for row in runtime_rows} >= {"shared", "shared alias"}
        assert next(row for row in runtime_rows if row["name"] == "shared alias") == {
            "kind": "runtime",
            "name": "shared alias",
            "source_profile": "shared",
            "source_module": "darling/shared",
            "purpose": "runtime",
            "path": "testkit/runtime-profiles.yml",
        }
        assert profile_catalog.profile_names(
            profile_catalog.collect_profile_catalog(root, "patch")
        ) == ["alpha space", "shared", "zeta"]
        assert [
            row["name"]
            for row in profile_catalog.collect_profile_catalog(root, "runtime")["profiles"]
        ] == ["a-runtime", "shared", "shared alias", "z-runtime"]
        runtime_payload = profile_catalog.collect_profile_catalog(
            root, "runtime", ["runtime"]
        )
        assert runtime_payload["inputs"] == {
            "kind": "runtime",
            "purposes": ["runtime"],
        }
        assert profile_catalog.profile_names(runtime_payload) == [
            "a-runtime",
            "shared",
            "shared alias",
            "z-runtime",
        ]
        try:
            profile_catalog.collect_profile_catalog(root, "patch", ["prefix-baseline"])
        except profile_catalog.ProfileCatalogError as error:
            assert "require --kind runtime or --kind all" in str(error)
        else:
            raise AssertionError("patch-only catalog accepted a runtime purpose filter")
    bootstrap_payload = profile_catalog.collect_profile_catalog(
        ROOT,
        "runtime",
        list(profile_catalog.BOOTSTRAP_RUNTIME_PROFILE_PURPOSES),
    )
    assert bootstrap_payload["profiles"]
    assert {
        row["purpose"] for row in bootstrap_payload["profiles"]
    } <= set(profile_catalog.BOOTSTRAP_RUNTIME_PROFILE_PURPOSES)

    with tempfile.TemporaryDirectory(prefix="patch-only-catalog-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "patch only")
        assert profile_catalog.profile_names(
            profile_catalog.collect_profile_catalog(root, "patch")
        ) == ["patch only"]

    with tempfile.TemporaryDirectory(prefix="runtime-only-catalog-") as temporary:
        root = Path(temporary)
        write_runtime_profiles(
            root, {"runtime only": runtime_definition("source", "darling")}
        )
        assert profile_catalog.profile_names(
            profile_catalog.collect_profile_catalog(root, "runtime")
        ) == ["runtime only"]


def malformed_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="profile-catalog-malformed-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "valid")
        write_runtime_profiles(root, {"valid": runtime_definition("valid", "darling")})
        (root / "patches" / "valid" / "patches.yml").write_text("patches: wrong\n")
        expect_catalog_error(root, "invalid patch profile")

    with tempfile.TemporaryDirectory(prefix="profile-catalog-yaml-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "valid")
        runtime_path = root / "testkit" / "runtime-profiles.yml"
        runtime_path.parent.mkdir(parents=True)
        runtime_path.write_text("runtime-profiles: [\\n")
        expect_catalog_error(root, "invalid runtime profile manifest")

    with tempfile.TemporaryDirectory(prefix="profile-catalog-runtime-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "valid")
        write_runtime_profiles(root, {"bad/name": runtime_definition("valid", "darling")})
        expect_catalog_error(root, "unsafe runtime profile")

    with tempfile.TemporaryDirectory(prefix="profile-catalog-module-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "valid")
        write_runtime_profiles(root, {"valid": runtime_definition("valid", "../outside")})
        expect_catalog_error(root, "unsafe source-module")

    with tempfile.TemporaryDirectory(prefix="profile-catalog-name-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "unsafe\nname")
        write_runtime_profiles(root, {"valid": runtime_definition("valid", "darling")})
        expect_catalog_error(root, "unsafe patch profile")
    with tempfile.TemporaryDirectory(prefix="profile-description-control-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "valid", description="unsafe\u001b]0;title\u0007")
        write_runtime_profiles(root, {"valid": runtime_definition("valid", "darling")})
        expect_catalog_error(root, "unsafe description")

    with tempfile.TemporaryDirectory(prefix="profile-catalog-option-name-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "-unsafe")
        write_runtime_profiles(root, {"valid": runtime_definition("valid", "darling")})
        expect_catalog_error(root, "unsafe patch profile")

    with tempfile.TemporaryDirectory(prefix="profile-catalog-io-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "valid")
        real_loader = profile_catalog.test_manifest.load_test_profile

        def deny_profile_read(_path: Path):
            raise PermissionError("denied by contract")

        profile_catalog.test_manifest.load_test_profile = deny_profile_read
        try:
            try:
                profile_catalog.collect_profile_catalog(root, "patch")
            except profile_catalog.ProfileCatalogOperationalError as error:
                assert "cannot read patch profile" in str(error)
            else:
                raise AssertionError("catalog mislabeled an I/O failure as valid input")
        finally:
            profile_catalog.test_manifest.load_test_profile = real_loader

    with tempfile.TemporaryDirectory(prefix="profile-catalog-path-") as temporary:
        root = Path(temporary) / "manifest"
        outside = Path(temporary) / "outside"
        write_patch_profile(outside, "escaped")
        (root / "patches").mkdir(parents=True)
        (root / "patches" / "escaped").symlink_to(outside / "patches" / "escaped")
        write_runtime_profiles(root, {"valid": runtime_definition("valid", "darling")})
        expect_catalog_error(root, "unsafe profile manifest path")


def parser_contract() -> None:
    dev_parser = build_parser(dev.DarlingDev())
    assert dev_parser.parse_args(["dev", "status"]).profile == "homebrew"
    assert dev_parser.parse_args(
        ["dev", "check", "quick", "--evidence", "out.json"]
    ).profile == "homebrew"
    assert dev_parser.parse_args(
        [
            "dev",
            "package",
            "--receipt",
            "receipt.json",
            "--output",
            "out",
            "--evidence",
            "evidence.json",
        ]
    ).profile == "homebrew"
    profiles = dev_parser.parse_args(["dev", "profiles"])
    assert (
        profiles.kind,
        profiles.purpose,
        profiles.json,
        profiles.names,
        profiles.completion,
    ) == (
        "all",
        [],
        False,
        False,
        None,
    )
    filtered_profiles = dev_parser.parse_args(
        [
            "dev",
            "profiles",
            "--kind",
            "runtime",
            "--purpose",
            "prefix-baseline",
            "--purpose",
            "guest-toolchain-provisioning",
        ]
    )
    assert filtered_profiles.purpose == [
        "prefix-baseline",
        "guest-toolchain-provisioning",
    ]
    dev_actions = dev_parser._subparsers._group_actions[0].choices[
        "dev"
    ]._subparsers._group_actions[0].choices
    for action_name in ("status", "check", "package"):
        action = next(
            candidate
            for candidate in dev_actions[action_name]._actions
            if "--profile" in candidate.option_strings
        )
        assert action.profile_completion_kind == "patch"
    for mutually_exclusive in (
        ["dev", "profiles", "--json", "--names"],
        ["dev", "profiles", "--names", "--completion", "bash"],
    ):
        expect_parse_error(dev_parser, mutually_exclusive)

    patch_parser = build_parser(patch_command.DarlingPatch())
    assert patch_parser.parse_args(["patch", "list"]).profile == "homebrew"
    expect_parse_error(patch_parser, ["patch", "explain"])
    assert patch_parser.parse_args(
        ["patch", "explain", "--profile", "alpha space"]
    ).profile == "alpha space"
    patch_profile_actions = [
        action
        for subparser in patch_parser._subparsers._group_actions[0].choices[
            "patch"
        ]._subparsers._group_actions[0].choices.values()
        for action in subparser._actions
        if "--profile" in action.option_strings
    ]
    assert patch_profile_actions
    assert all(
        action.profile_completion_kind == "patch"
        for action in patch_profile_actions
    )

    test_parser = build_parser(test_command.DarlingTest())
    defaults = test_parser.parse_args(["test"])
    assert defaults.profile is None
    assert defaults.with_runtime_profile == []
    assert defaults.bootstrap_runtime_profile is None
    assert defaults.prefix_profile is None
    parsed, unknown = test_parser.parse_known_args(
        [
            "test",
            "--profile",
            "alpha space",
            "--with-runtime-profile",
            "runtime one",
            "--with-runtime-profile",
            "runtime two",
            "--bootstrap-runtime-profile",
            "baseline",
            "--prefix-profile",
            "literal shortcut",
            "--unknown-ctest-option",
        ]
    )
    assert unknown == ["--unknown-ctest-option"]
    assert parsed.profile == "alpha space"
    assert parsed.with_runtime_profile == ["runtime one", "runtime two"]
    assert parsed.bootstrap_runtime_profile == "baseline"
    assert parsed.prefix_profile == "literal shortcut"

    catalog_actions = {
        option: action
        for action in test_parser._subparsers._group_actions[0].choices["test"]._actions
        for option in action.option_strings
        if hasattr(action, "profile_completion_kind")
    }
    assert catalog_actions["--profile"].profile_completion_kind == "patch"
    assert catalog_actions["--with-runtime-profile"].profile_completion_kind == "runtime"
    assert catalog_actions["--bootstrap-runtime-profile"].profile_completion_kind == "runtime"
    prefix_action = next(
        action
        for action in test_parser._subparsers._group_actions[0].choices["test"]._actions
        if "--prefix-profile" in action.option_strings
    )
    assert not hasattr(prefix_action, "profile_completion_kind")
    assert "--prefix-profile" not in profile_catalog.PROFILE_COMPLETION_KINDS


def dev_output_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="profile-dev-output-") as temporary:
        root = Path(temporary)
        for index in range(7):
            write_patch_profile(root, f"patch {index}", count=index)
        write_runtime_profiles(
            root,
            {
                f"runtime {index}": runtime_definition(
                    f"patch {index % 7}", f"darling/runtime-{index}"
                )
                for index in range(4)
            },
        )
        parser = build_parser(dev.DarlingDev())
        command = dev.DarlingDev()
        command.manifest = SimpleNamespace(repo_abspath=str(root))
        command.topdir = str(root.parent)
        emitted: list[str] = []
        command.inf = emitted.append
        command.err = lambda message: (_ for _ in ()).throw(AssertionError(message))
        real_west_argv = dev._west_argv
        dev._west_argv = lambda: ["/recorded/west launcher"]
        try:
            completion_args = parser.parse_args(
                ["dev", "profiles", "--completion", "bash"]
            )
            command.do_run(completion_args, [])
            assert emitted.pop() == profile_catalog.bash_profile_completion().rstrip(
                "\n"
            )

            json_args = parser.parse_args(
                ["dev", "profiles", "--kind", "runtime", "--json"]
            )
            command.do_run(json_args, [])
            exact = json.loads(emitted.pop())
            assert exact == profile_catalog.collect_profile_catalog(root, "runtime")
            filtered_args = parser.parse_args(
                [
                    "dev",
                    "profiles",
                    "--kind",
                    "runtime",
                    "--purpose",
                    "missing-purpose",
                    "--json",
                ]
            )
            command.do_run(filtered_args, [])
            filtered = json.loads(emitted.pop())
            assert filtered["inputs"] == {
                "kind": "runtime",
                "purposes": ["missing-purpose"],
            }
            assert filtered["profiles"] == []

            names_args = parser.parse_args(
                ["dev", "profiles", "--names", "--kind", "patch"]
            )
            command.do_run(names_args, [])
            assert emitted.pop().splitlines() == [f"patch {index}" for index in range(7)]

            human_args = parser.parse_args(["dev", "profiles"])
            command.do_run(human_args, [])
            assert emitted[0] == "west dev profiles: valid patch=7 runtime=4"
            assert shlex.split(emitted[1].removeprefix("details: ")) == [
                "/recorded/west launcher",
                "dev",
                "profiles",
                "--kind=all",
                "--json",
            ]
            rows = [
                line
                for line in emitted
                if line.startswith(("patch   ", "runtime "))
            ]
            assert len(rows) == dev._HUMAN_PREVIEW_LIMIT
            assert all(
                len(line) <= dev._HUMAN_ITEM_CHARACTER_LIMIT for line in rows
            )
            assert emitted[-1] == "... 3 additional profiles omitted"
            assert emitted == [
                "west dev profiles: valid patch=7 runtime=4",
                "details: "
                + shlex.join(
                    [
                        "/recorded/west launcher",
                        "dev",
                        "profiles",
                        "--kind=all",
                        "--json",
                    ]
                ),
                *[
                    f"patch   patch {index} patches={index} "
                    f"path=patches/patch {index}/patches.yml "
                    f"description=patch {index} description"
                    for index in range(7)
                ],
                "runtime runtime 0 source=patch 0:darling/runtime-0 "
                "purpose=runtime path=testkit/runtime-profiles.yml",
                "... 3 additional profiles omitted",
            ]
        finally:
            dev._west_argv = real_west_argv

    with tempfile.TemporaryDirectory(prefix="profile-dev-error-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "bad")
        write_runtime_profiles(root, {"bad": runtime_definition("bad", "darling")})
        (root / "patches" / "bad" / "patches.yml").write_text("patches: invalid\n")
        parser = build_parser(dev.DarlingDev())
        command = dev.DarlingDev()
        command.manifest = SimpleNamespace(repo_abspath=str(root))
        command.topdir = str(root.parent)
        emitted: list[str] = []
        command.inf = emitted.append
        args = parser.parse_args(["dev", "profiles", "--json"])
        try:
            command.do_run(args, [])
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError("invalid JSON catalog did not exit 1")
        failure = json.loads(emitted.pop())
        assert failure["schema_version"] == 1
        assert failure["operation"] == "profiles"
        assert failure["state"] == "invalid"
        assert failure["returncode"] == 1
        assert failure["action"] == "profiles"
        assert failure["error"]["type"] == "ProfileCatalogError"

    with tempfile.TemporaryDirectory(prefix="profile-dev-io-error-") as temporary:
        root = Path(temporary)
        write_patch_profile(root, "valid")
        parser = build_parser(dev.DarlingDev())
        command = dev.DarlingDev()
        command.manifest = SimpleNamespace(repo_abspath=str(root))
        command.topdir = str(root.parent)
        emitted: list[str] = []
        command.inf = emitted.append
        real_loader = profile_catalog.test_manifest.load_test_profile

        def deny_profile_read(_path: Path):
            raise PermissionError("denied by contract")

        profile_catalog.test_manifest.load_test_profile = deny_profile_read
        try:
            args = parser.parse_args(["dev", "profiles", "--kind", "patch", "--json"])
            try:
                command.do_run(args, [])
            except SystemExit as error:
                assert error.code == 1
            else:
                raise AssertionError("profile I/O failure did not exit 1")
        finally:
            profile_catalog.test_manifest.load_test_profile = real_loader
        failure = json.loads(emitted.pop())
        assert failure["operation"] == "profiles"
        assert failure["state"] == "operational_error"
        assert failure["error"]["type"] == "ProfileCatalogOperationalError"


def bash_completion_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="profile-completion-contract-") as temporary:
        root = Path(temporary)
        completion = root / "completion.bash"
        completion.write_text(profile_catalog.bash_profile_completion())
        patch_names = root / "patch.names"
        runtime_names = root / "runtime.names"
        patch_names.write_text("alpha\nalpha space\nbeta\n")
        runtime_names.write_text("run one\nbootstrap two\n")
        bootstrap_names = root / "bootstrap.names"
        bootstrap_names.write_text("bootstrap two\n")
        compopt_log = root / "compopt.log"
        west = root / "west"
        west.write_text(
            """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$WEST_CALL_LOG"
if [[ "${FAIL_WEST:-}" == 1 ]]; then
    echo 'catalog failure should be quiet' >&2
    exit 9
fi
kind=""
purpose_count=0
while (($#)); do
    case "$1" in
        --kind)
            kind=$2
            shift 2
            ;;
        --purpose)
            ((purpose_count += 1))
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done
if [[ "$kind" == patch ]]; then
    cat "$PATCH_NAMES"
elif [[ "$kind" == runtime && "$purpose_count" -gt 0 ]]; then
    cat "$BOOTSTRAP_NAMES"
elif [[ "$kind" == runtime ]]; then
    cat "$RUNTIME_NAMES"
else
    exit 7
fi
"""
        )
        west.chmod(0o755)
        log = root / "calls.log"

        def complete(words: list[str], *, fail: bool = False) -> tuple[list[str], str]:
            compopt_log.write_text("")
            log.write_text("")
            array = " ".join(shlex.quote(word) for word in words)
            script = f"""
compopt() {{ printf '%s\\n' "$*" >> "$COMPOPT_CALL_LOG"; }}
source "$COMPLETION_SCRIPT"
COMP_WORDS=({array})
COMP_CWORD={len(words) - 1}
COMPREPLY=()
_darling_west_profile_values
printf '%s\\0' "${{COMPREPLY[@]}}"
"""
            environment = dict(os.environ)
            environment.update(
                {
                    "PATH": f"{root}:{environment.get('PATH', '')}",
                    "COMPLETION_SCRIPT": str(completion),
                    "WEST_CALL_LOG": str(log),
                    "FAIL_WEST": "1" if fail else "0",
                    "PATCH_NAMES": str(patch_names),
                    "RUNTIME_NAMES": str(runtime_names),
                    "BOOTSTRAP_NAMES": str(bootstrap_names),
                    "COMPOPT_CALL_LOG": str(compopt_log),
                }
            )
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            replies = [
                item.decode()
                for item in result.stdout.split(b"\0")
                if item
            ]
            return replies, result.stderr.decode()

        assert complete(["west", "test", "--profile", ""])[0] == [
            "alpha",
            "alpha space",
            "beta",
        ]
        assert log.read_text().splitlines() == ["dev profiles --names --kind patch"]
        assert compopt_log.read_text().splitlines() == [
            "+o default +o bashdefault"
        ]
        assert complete(["west", "patch", "apply", "--profile", "alpha s"])[0] == [
            "alpha space"
        ]
        assert complete(["west", "dev", "status", "--profile=be"])[0] == [
            "--profile=beta"
        ]
        assert complete(
            ["west", "dev", "status", "--profile", "=", "be"]
        )[0] == ["beta"]
        assert complete(["west", "-v", "test", "--profile", "alpha"])[0] == [
            "alpha",
            "alpha space",
        ]
        assert complete(
            ["west", "--verbose", "--quiet", "test", "--profile", "be"]
        )[0] == ["beta"]
        assert complete(
            ["west", "-z", "/tmp/workspace", "dev", "status", "--profile", "be"]
        )[0] == ["beta"]
        assert complete(
            [
                "west",
                "--zephyr-base",
                "dev",
                "test",
                "--with-runtime-profile",
                "run",
            ]
        )[0] == ["run one"]
        patch_names.write_text("alpha\nalpha space\nbeta\ngamma new\n")
        assert complete(["west", "test", "--profile", "gamma"])[0] == [
            "gamma new"
        ]
        runtime_replies, runtime_stderr = complete(
            ["west", "test", "--with-runtime-profile", "run"]
        )
        assert runtime_replies == ["run one"], (
            runtime_replies,
            runtime_stderr,
            log.read_text(),
        )
        assert complete(
            ["west", "test", "--bootstrap-runtime-profile=bootstrap"]
        )[0] == ["--bootstrap-runtime-profile=bootstrap two"]
        assert log.read_text().splitlines() == [
            "dev profiles --names --kind runtime "
            "--purpose guest-toolchain-provisioning --purpose prefix-baseline"
        ]
        replies, stderr = complete(["west", "test", "--profile", ""], fail=True)
        assert replies == []
        assert stderr == ""
        assert compopt_log.read_text().splitlines() == [
            "+o default +o bashdefault"
        ]
        assert complete(["west", "test", "--prefix-profile", ""])[0] == []
        assert log.read_text() == ""
        assert complete(["west", "status", "--profile", ""])[0] == []
        assert complete(["west", "dev", "start", "--profile", ""])[0] == []
        assert log.read_text() == ""
        assert compopt_log.read_text() == ""


catalog_contract()
malformed_contract()
parser_contract()
dev_output_contract()
bash_completion_contract()
print("PASS profile-discovery-contract")

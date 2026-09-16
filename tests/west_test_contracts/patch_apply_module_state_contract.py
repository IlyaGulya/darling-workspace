"""Apply and clean report every module out of state, not just the first.

Dying on the first misplaced module makes an operator with several of them pay
for the same diagnosis once per run: each run ends before anything is written,
so the state does not move and the next run rediscovers one module. The audited
session hit exactly that, with three in a row. This contract drives the real
branch/HEAD check over synthetic repositories, proves the bad model reports one
and the current path reports all, and checks that the collection itself mutates
nothing.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
sys.path.insert(0, str(ROOT / "ci"))
west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

import patch as patch_command


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


class Recorder:
    """Stands in for the command's die(), which would exit the process."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def die(self, message: str) -> None:
        self.messages.append(message)
        raise SystemExit(1)


def build(repo: Path, branch: str) -> None:
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Contract")
    git(repo, "config", "user.email", "contract@example.invalid")
    (repo / "state").write_text("base\n")
    git(repo, "add", "state")
    git(repo, "commit", "-q", "-m", "base")
    if branch != "main":
        git(repo, "checkout", "-q", "-b", branch)


def command_for(repos: dict[str, Path], recorder: Recorder):
    command = patch_command.DarlingPatch.__new__(patch_command.DarlingPatch)
    command._base_profile = None
    command._repo = lambda module: repos[module]
    command._base_revision = lambda _module: "main"
    # Cleanliness is a separate concern with its own coverage; this contract is
    # about which state each module is in, so the good module passes it trivially.
    command._ensure_clean = lambda *_args, **_kwargs: None
    command.die = recorder.die
    return command


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    profile = "homebrew"
    # Three modules, two of them on a branch this profile cannot accept.
    repos = {
        "darling": root / "darling",
        "darling/child": root / "child",
        "darling/other": root / "other",
    }
    build(repos["darling"], "fix/misplaced-one")
    build(repos["darling/child"], "fix/misplaced-two")
    build(repos["darling/other"], "main")
    # The acceptable non-integration state is detached at the base revision; a
    # module sitting on an ordinary branch is still a blocker.
    git(repos["darling/other"], "checkout", "-q", "--detach", "main")
    modules = ["darling", "darling/child", "darling/other"]

    recorder = Recorder()
    command = command_for(repos, recorder)

    # The bad model: the previous loop died on the first blocker, which is why
    # three misplaced modules took three runs to find.
    bad_model: list[str] = []
    for module in modules:
        try:
            command._ensure_generated_context(module, profile)
        except RuntimeError as error:
            bad_model.append(str(error))
            break
    assert len(bad_model) == 1, bad_model

    blockers = command._module_state_blockers(modules, profile)
    assert len(blockers) == 2, blockers
    assert "darling: " in blockers[0] and "fix/misplaced-one" in blockers[0], blockers
    assert "darling/child: " in blockers[1], blockers
    assert "fix/misplaced-two" in blockers[1], blockers
    # The module that is in state is not reported.
    assert not any("darling/other" in blocker for blocker in blockers), blockers

    # The message the operator sees names the count and every module, in order.
    try:
        command._die_on_module_state_blockers(profile, blockers)
    except SystemExit:
        pass
    else:
        raise AssertionError("a blocked apply exited zero")
    assert len(recorder.messages) == 1, recorder.messages
    message = recorder.messages[0]
    assert message.startswith("2 module(s) are not in the state homebrew requires:"), message
    assert "  darling: " in message and "  darling/child: " in message, message
    assert message.index("darling: ") < message.index("darling/child: "), message

    # Collecting is read-only: no integration branch, no moved HEAD, no writes.
    expected_branch = {
        "darling": "fix/misplaced-one",
        "darling/child": "fix/misplaced-two",
        "darling/other": "",
    }
    for module, repo in repos.items():
        assert git(repo, "branch", "--show-current") == expected_branch[module], module
        assert git(repo, "status", "--porcelain") == "", module
        assert "integration/homebrew" not in git(repo, "branch", "--list", "integration/*"), module

print("PASS patch-apply-module-state-contract")

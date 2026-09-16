"""Behavioral contract for timed, bounded, explicitly cacheable host commands."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
from test_profile import ProfileOperationsMixin
SPEC = importlib.util.spec_from_file_location("run_host_tier", ROOT / "ci/run-host-tier.py")
assert SPEC is not None and SPEC.loader is not None
host_tier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host_tier)

with patch.dict(os.environ, {}, clear=True), patch.object(
    host_tier.os, "cpu_count", return_value=32
):
    assert host_tier._worker_count(100) == 8
    assert host_tier._worker_count(3) == 3

with patch.dict(
    os.environ, {"DARLING_HOST_TIER_WORKERS": "12"}, clear=True
), patch.object(host_tier.os, "cpu_count", return_value=32):
    assert host_tier._worker_count(12) == 12
    assert host_tier._worker_count(32) == 12

for value, command_count in (("0", 3), ("4", 3), ("many", 3)):
    with patch.dict(os.environ, {"DARLING_HOST_TIER_WORKERS": value}, clear=True):
        try:
            host_tier._worker_count(command_count)
        except SystemExit:
            continue
        raise AssertionError(
            f"worker count {value!r} for {command_count} commands was accepted"
        )

with tempfile.TemporaryDirectory(prefix="host-tier-contract-") as raw:
    root = Path(raw)
    worker = root / "worker.py"
    worker.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "value = int(path.read_text()) if path.exists() else 0\n"
        "path.write_text(str(value + 1))\n"
    )
    cache = root / "cache"
    key = "a" * 64
    cacheable_a = root / "cacheable-a"
    cacheable_b = root / "cacheable-b"
    volatile = root / "volatile"
    commands = [
        host_tier.HostCommand("cacheable-a", [os.sys.executable, str(worker), str(cacheable_a)], True),
        host_tier.HostCommand("cacheable-b", [os.sys.executable, str(worker), str(cacheable_b)], True),
        host_tier.HostCommand("volatile", [os.sys.executable, str(worker), str(volatile)], False),
    ]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert host_tier.run_commands(commands, workers=3, cache_root=cache, cache_key=key) == 0
        assert host_tier.run_commands(commands, workers=3, cache_root=cache, cache_key=key) == 0
    assert cacheable_a.read_text() == "1"
    assert cacheable_b.read_text() == "1"
    assert volatile.read_text() == "2"
    lines = output.getvalue().splitlines()
    assert any("host tier command complete: cacheable-a" in line and "cache=miss" in line for line in lines)
    assert any("host tier command complete: cacheable-a" in line and "cache=hit" in line for line in lines)
    assert any("host tier command complete: volatile" in line and "elapsed_ms=" in line for line in lines)
    markers = list(cache.glob("*.json"))
    assert len(markers) == 2, markers
    for marker in markers:
        value = json.loads(marker.read_text())
        assert value["schema_version"] == 1
        assert value["cache_key"] == key
        assert value["returncode"] == 0

# The weight table is a declaration about commands that exist. A weight keyed on
# a typo would be silently ignored and the command it meant to throttle would
# keep oversubscribing the machine, so the table is checked against the tier's
# own registration lists.
registered = {Path(contract).stem for contract in host_tier.CONTRACTS} | {
    name for name, _, _ in host_tier.EXPLICIT_CONTRACTS
}
sweeps = {"homebrew-host-metadata", "wget-residual-host-metadata"}
declared = set(host_tier.CONTRACT_WEIGHTS) - sweeps
assert declared <= registered, sorted(declared - registered)
assert host_tier._weight_for("a-command-nobody-weighted") == 1
# The table has to match the commands the tier really builds, not just the
# registration lists: everything the builder emits is checked against it, and
# two commands must never share a name, or a weight would apply twice.
built = host_tier.build_commands([], prematerialized=None)
assert host_tier._validate_weights(built) == []
assert len({command.name for command in built}) == len(built)
assert [command.weight for command in built if command.name == "homebrew-host-metadata"] == [3]
assert [command.weight for command in built if command.name == "wget-residual-host-metadata"] == [3]
assert all(command.weight >= 1 for command in built)
assert all(
    command.weight == 1 or command.name in host_tier.CONTRACT_WEIGHTS for command in built
)

# A command's weight is slots it holds while running, so a heavy command cannot
# run beside anything that leaves it no room. Two commands that each cost the
# whole budget must therefore serialize: if the weight were ignored, their
# start/end records would interleave. This is the property that keeps a command
# driving other runners from being scheduled next to a peer.
with tempfile.TemporaryDirectory(prefix="host-tier-weight-contract-") as raw:
    root = Path(raw)
    logger = root / "logger.py"
    logger.write_text(
        "import sys, time\n"
        "name, log = sys.argv[1], sys.argv[2]\n"
        "with open(log, 'a') as stream:\n"
        "    stream.write(f'{name} start\\n')\n"
        "    stream.flush()\n"
        "time.sleep(1.5)\n"
        "with open(log, 'a') as stream:\n"
        "    stream.write(f'{name} end\\n')\n"
        "    stream.flush()\n"
    )
    log = root / "log"
    log.write_text("")
    weighted = [
        host_tier.HostCommand(
            name,
            [os.sys.executable, str(logger), name, str(log)],
            False,
            2,
        )
        for name in ("heavy-a", "heavy-b")
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        assert (
            host_tier.run_commands(
                weighted, workers=2, cache_root=None, cache_key=None
            )
            == 0
        )
    order = [line.split()[0] for line in log.read_text().splitlines()]
    assert order == ["heavy-a", "heavy-a", "heavy-b", "heavy-b"] or order == [
        "heavy-b",
        "heavy-b",
        "heavy-a",
        "heavy-a",
    ], order

with tempfile.TemporaryDirectory(prefix="host-profile-contract-") as raw:
    root = Path(raw)
    workspace = root / "materialized-v2/homebrew/guest"
    manifest_repo = workspace / "darling-workspace"
    project = workspace / "darling"
    for repo in (manifest_repo, project):
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "host-tier@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Host Tier Contract"],
            cwd=repo,
            check=True,
        )
        (repo / "value").write_text("value\n")
        subprocess.run(["git", "add", "value"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "value"], cwd=repo, check=True)
    manifest_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=manifest_repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    project_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    (workspace / "tier-workspace-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "west-acceptance-tier-workspace",
                "profile": "homebrew",
                "manifest_revision": manifest_revision,
                "projects": [{"path": "darling", "revision": project_revision}],
                "generated_locks": [
                    {"path": "patches/homebrew/west.lock.yml", "sha256": "0" * 64}
                ],
            }
        )
    )

    class ProfileHost(ProfileOperationsMixin):
        manifest = type("Manifest", (), {"repo_abspath": str(manifest_repo)})()

        def _profile_modules(self, _profile):
            return {"darling"}

        def _project_path(self, module):
            return workspace / module

    environment = {
        "WEST_MATERIALIZED_WORKSPACE_LOCK": str(root / "materialized-v2/homebrew/.guest.lock"),
        "WEST_PREMATERIALIZED_PROFILE": "homebrew",
    }
    with patch.dict(os.environ, environment, clear=False):
        assert ProfileHost()._profile_is_applied("homebrew")
        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", "unexpected"],
            cwd=project,
            check=True,
        )
        assert not ProfileHost()._profile_is_applied("homebrew")

print("PASS host-tier-contract")

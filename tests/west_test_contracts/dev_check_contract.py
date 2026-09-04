#!/usr/bin/env python3
"""Behavioral transaction contract for the ``west dev check`` core."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import dev_check


FAKE_WEST = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def append(authority, argv):
    path = Path(os.environ["DEV_CHECK_FAKE_LOG"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"authority": authority, "argv": argv}) + "\n")


def interrupted(role):
    marker = Path(os.environ["DEV_CHECK_INTERRUPT_MARKERS"])
    marker.mkdir(parents=True, exist_ok=True)
    (marker / role).write_text("SIGINT\n", encoding="utf-8")
    os._exit(130)


def parallel_barrier(name):
    value = os.environ.get("DEV_CHECK_PARALLEL_BARRIER")
    if not value:
        return
    root = Path(value)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.ready").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 5
    while len(list(root.glob("*.ready"))) < 3:
        if time.monotonic() >= deadline:
            raise SystemExit(97)
        time.sleep(0.01)


def assert_isolated(stage):
    forbidden = {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
    }
    if forbidden.intersection(os.environ):
        raise SystemExit(96)
    if (
        Path(os.environ["HOME"]).name != stage
        or Path(os.environ["XDG_CACHE_HOME"]).name != stage
        or Path(os.environ["XDG_CONFIG_HOME"]).name != stage
        or os.environ.get("GIT_CONFIG_COUNT") != "0"
        or os.environ.get("GIT_CONFIG_GLOBAL") != "/dev/null"
        or os.environ.get("GIT_CONFIG_NOSYSTEM") != "1"
    ):
        raise SystemExit(95)



def hold_with_cleanup(name):
    root = Path(os.environ["DEV_CHECK_PARALLEL_CLEANUP"])
    root.mkdir(parents=True, exist_ok=True)
    orphan = root / f"{name}.orphan"
    orphan.write_text("owned\n", encoding="utf-8")
    try:
        (root / f"{name}.holding").write_text("holding\n", encoding="utf-8")
        time.sleep(60)
    finally:
        orphan.unlink(missing_ok=True)
        (root / f"{name}.cleaned").write_text("cleaned\n", encoding="utf-8")


def wait_for_holders():
    root = Path(os.environ["DEV_CHECK_PARALLEL_CLEANUP"])
    deadline = time.monotonic() + 5
    while len(list(root.glob("*.holding"))) < 2:
        if time.monotonic() >= deadline:
            raise SystemExit(94)
        time.sleep(0.01)

if sys.argv[1:] == ["--interrupt-descendant"]:
    signal.signal(signal.SIGINT, lambda _signum, _frame: interrupted("descendant"))
    Path(os.environ["DEV_CHECK_INTERRUPT_READY"]).write_text(
        str(os.getpid()), encoding="utf-8"
    )
    while True:
        time.sleep(60)

argv = sys.argv[1:]
if argv[-1:] == ["--version"]:
    sys.stdout.write("West version: fixture-1.0\n")
    raise SystemExit(0)
append("west", argv)
mode = os.environ.get("DEV_CHECK_FAKE_MODE", "pass")
if argv[:2] == ["patch", "verify"]:
    if os.environ.get("DEV_CHECK_PARALLEL_BARRIER"):
        assert_isolated("candidate")
    parallel_barrier("patch-verify")
    if mode in {"fail-parallel", "interrupt-parallel"}:
        wait_for_holders()
    sys.stdout.write("patch verify raw evidence\n")
    if mode == "fail-parallel":
        raise SystemExit(43)
    if mode == "interrupt-parallel":
        os.kill(os.getppid(), signal.SIGINT)
        time.sleep(60)
if argv[:1] == ["test"] and "--materialize-profile" in argv:
    if os.environ.get("DEV_CHECK_PARALLEL_BARRIER"):
        assert_isolated("host")
    parallel_barrier("host-materialized-test")
    sys.stderr.write("host materialized raw evidence\n")
    if mode in {"fail-parallel", "interrupt-parallel"}:
        hold_with_cleanup("host-materialized-test")
if mode == "fail-check" and argv[:2] == ["patch", "check"]:
    sys.stdout.write("O" * 20000 + "\nSTDOUT-END\n")
    sys.stderr.write("E" * 21000 + "\nSTDERR-END\n")
    raise SystemExit(37)
if mode == "interrupt" and argv[:2] == ["patch", "check"]:
    signal.signal(signal.SIGINT, lambda _signum, _frame: interrupted("leader"))
    ready = Path(os.environ["DEV_CHECK_INTERRUPT_READY"])
    subprocess.Popen([sys.executable, __file__, "--interrupt-descendant"])
    deadline = time.monotonic() + 5
    while not ready.exists():
        if time.monotonic() >= deadline:
            raise SystemExit(98)
        time.sleep(0.01)
    os.kill(os.getppid(), signal.SIGINT)
    while True:
        time.sleep(60)
if argv[:1] == ["test"] and "--list" in argv:
    sys.stdout.write("fixture.case: [env:host diag:contract kind:behavior]\n")
if argv[:2] == ["patch", "apply"] and "--lock-first-evidence" in argv:
    ids = json.loads((Path.cwd() / "fixture-ids.json").read_text())
    (Path.cwd() / "patches" / "homebrew" / "west.lock.yml").write_text(
        "manifest:\n  projects: []\n# generated candidate\n",
        encoding="utf-8",
    )
    module_repo = Path.cwd().parent / "fixture" / "module"
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "candidate integration"],
        cwd=module_repo,
        check=True,
    )
    applied_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=module_repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    lock_evidence = Path(argv[argv.index("--lock-first-evidence") + 1])
    lock_evidence.parent.mkdir(parents=True, exist_ok=True)
    lock_evidence.write_text(
        json.dumps(
            {
                "evidence_schema_version": 2,
                "verdict": "VALID",
                "batch_id": "dev-check-contract-batch",
                "expected_count": 1,
                "module_order": ["fixture/module"],
                "series_order": [
                    {"module": "fixture/module", "patch": "fixture/change.patch"}
                ],
                "series": [
                    {
                        "module": "fixture/module",
                        "patch": "fixture/change.patch",
                        "base": ids["base"],
                        "source": ids["source"],
                        "canonical_tree": ids["tree"],
                        "applied_commit": applied_commit,
                        "applied_tree": ids["tree"],
                        "verdict": "VALID",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
if len(argv) >= 2 and argv[:2] == ["patch", "export-locks"]:
    output = Path(argv[argv.index("--output") + 1])
    output.mkdir(parents=True)
    if mode == "package-fail":
        (output / "partial").write_text("transaction-owned\n", encoding="utf-8")
        raise SystemExit(41)
    ids = json.loads((Path.cwd() / "fixture-ids.json").read_text())
    source = ids["source"]
    mbox_data = subprocess.run(
        [
            "git",
            "format-patch",
            "--stdout",
            "--no-stat",
            "--full-index",
            f"{source}^!",
        ],
        cwd=ids["module_repo"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    patch_id = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=mbox_data,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.decode().split()[0]
    mbox = output / "mbox" / "fixture" / "module" / "fixture" / "change.mbox"
    mbox.parent.mkdir(parents=True)
    mbox.write_bytes(mbox_data)
    evidence = {
        "export_schema_version": 1,
        "profile": argv[argv.index("--profile") + 1],
        "verdict": "VALID",
        "mode": "immutable-lock-format-patch",
        "batch_id": "dev-check-contract-batch",
        "expected_count": 1,
        "module_order": ["fixture/module"],
        "series_order": [
            {"module": "fixture/module", "patch": "fixture/change.patch"}
        ],
        "series": [
            {
                "module": "fixture/module",
                "patch": "fixture/change.patch",
                "lock": "fixture.lock.yml",
                "base": ids["base"],
                "source": source,
                "ordered_commits": [source],
                "commit_count": 1,
                "resulting_tree": ids["tree"],
                "mbox": "mbox/fixture/module/fixture/change.mbox",
                "sha256": hashlib.sha256(mbox_data).hexdigest(),
                "stable_patch_ids": [patch_id],
            }
        ],
        "clean_odb": {
            "module_count": 1,
            "immutable_fetch_transactions": 1,
            "alternates": 0,
            "shallow": 0,
            "partial": 0,
        },
    }
    if mode == "package-semantic-lie":
        evidence["series"][0]["resulting_tree"] = "9" * 40
    (output / "evidence.json").write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
'''

FAKE_ORACLE = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import sys
from pathlib import Path
import time


def parallel_barrier(name):
    value = os.environ.get("DEV_CHECK_PARALLEL_BARRIER")
    if not value:
        return
    root = Path(value)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.ready").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 5
    while len(list(root.glob("*.ready"))) < 3:
        if time.monotonic() >= deadline:
            raise SystemExit(97)
        time.sleep(0.01)



def assert_isolated(stage):
    forbidden = {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
    }
    if forbidden.intersection(os.environ):
        raise SystemExit(96)
    if (
        Path(os.environ["HOME"]).name != stage
        or Path(os.environ["XDG_CACHE_HOME"]).name != stage
        or Path(os.environ["XDG_CONFIG_HOME"]).name != stage
        or os.environ.get("GIT_CONFIG_COUNT") != "0"
        or os.environ.get("GIT_CONFIG_GLOBAL") != "/dev/null"
        or os.environ.get("GIT_CONFIG_NOSYSTEM") != "1"
    ):
        raise SystemExit(95)


def hold_with_cleanup(name):
    root = Path(os.environ["DEV_CHECK_PARALLEL_CLEANUP"])
    root.mkdir(parents=True, exist_ok=True)
    orphan = root / f"{name}.orphan"
    orphan.write_text("owned\n", encoding="utf-8")
    try:
        (root / f"{name}.holding").write_text("holding\n", encoding="utf-8")
        time.sleep(60)
    finally:
        orphan.unlink(missing_ok=True)
        (root / f"{name}.cleaned").write_text("cleaned\n", encoding="utf-8")

parallel_barrier("immutable-oracle")
assert_isolated("oracle")
if os.environ.get("DEV_CHECK_FAKE_MODE") in {
    "fail-parallel",
    "interrupt-parallel",
}:
    hold_with_cleanup("immutable-oracle")
sys.stdout.write("immutable oracle raw evidence\n")
log = Path(os.environ["DEV_CHECK_FAKE_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"authority": "oracle", "argv": sys.argv[1:]}) + "\n")
output = Path(sys.argv[sys.argv.index("--output") + 1])
workspace = Path(sys.argv[sys.argv.index("--workspace") + 1])
frozen_sha256 = hashlib.sha256((workspace / "west.lock.yml").read_bytes()).hexdigest()
ids = json.loads((workspace / "fixture-ids.json").read_text())
generated_path = workspace / "patches" / "homebrew" / "west.lock.yml"
generated_data = b"manifest:\n  projects: []\n# generated candidate\n"
generated_row = {
    "profile": "homebrew",
    "path": "patches/homebrew/west.lock.yml",
    "semantic_sha256": "c" * 64,
}
output.parent.mkdir(parents=True, exist_ok=True)
series = {
    "module": "fixture/module",
    "patch": "fixture/change.patch",
    "base": ids["base"],
    "source": ids["source"],
    "canonical_tree": ids["tree"],
    "applied_commit": ids["source"],
    "applied_tree": ids["tree"],
    "verdict": "VALID",
}
output.write_text(
    json.dumps(
        {
            "oracle_schema_version": 2,
            "mode": "immutable-cherry-pick-oracle",
            "profile": "homebrew",
            "profile_order": ["homebrew"],
            "batches": [
                {
                    "profile": "homebrew",
                    "batch_id": "dev-check-contract-batch",
                    "expected_count": 1,
                    "module_order": ["fixture/module"],
                    "series_order": [
                        {"module": "fixture/module", "patch": "fixture/change.patch"}
                    ],
                    "series": [series],
                    "verdict": "VALID",
                }
            ],
            "modules": [
                {
                    "module": "fixture/module",
                    "commit": ids["source"],
                    "tree": ids["tree"],
                }
            ],
            "generated_profile_locks": [generated_row],
            "frozen_manifest_sha256": frozen_sha256,
            "clean_odb": {
                "module_count": 1,
                "immutable_fetch_transactions": 1,
                "alternates": 0,
                "shallow": 0,
                "partial": 0,
            },
            "cleanup": {"root": "removed", "worktrees": "removed", "refs": "removed"},
            "verdict": "VALID",
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
'''

FAKE_TIER = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

with Path(os.environ["DEV_CHECK_FAKE_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"authority": "tier", "argv": sys.argv[1:]}) + "\n")
'''

FAKE_ACCEPTANCE = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
import shutil
import sys
from pathlib import Path

name = Path(__file__).name
if (
    name == "patch_stack_lock_first_acceptance.py"
    and sys.argv[1:2] == ["seed-source-refs"]
):
    authority = "seed"
else:
    authority = {
        "bootstrap-west.sh": "bootstrap",
        "patch_stack_acceptance.py": "capture",
        "patch_stack_lock_first_acceptance.py": "compare",
    }[name]
with Path(os.environ["DEV_CHECK_FAKE_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"authority": authority, "argv": sys.argv[1:]}) + "\n")
if authority == "seed":
    pass
elif authority == "bootstrap":
    workspace = Path.cwd()
    ids = json.loads((workspace / "fixture-ids.json").read_text())
    destination = workspace.parent / "fixture" / "module"
    shutil.copytree(ids["module_repo"], destination)
elif authority == "capture":
    workspace = Path(sys.argv[sys.argv.index("--workspace") + 1])
    modules = Path(sys.argv[sys.argv.index("--modules") + 1])
    manifest = Path(sys.argv[sys.argv.index("--manifest") + 1])
    ids = json.loads((workspace / "fixture-ids.json").read_text())
    generated_path = workspace / "patches" / "homebrew" / "west.lock.yml"
    generated_data = generated_path.read_bytes()
    generated_row = {
        "profile": "homebrew",
        "path": "patches/homebrew/west.lock.yml",
        "size": len(generated_data),
        "sha256": hashlib.sha256(generated_data).hexdigest(),
        "semantic_sha256": "c" * 64,
    }
    modules.parent.mkdir(parents=True, exist_ok=True)
    modules.write_text(
        json.dumps(
            {
                "profile": "homebrew",
                "modules": [
                    {
                        "module": "fixture/module",
                        "west_name": "fixture-module",
                        "path": "fixture/module",
                        "integration_profile": "homebrew",
                        "integration_oid": subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=workspace.parent / "fixture" / "module",
                            check=True,
                            text=True,
                            stdout=subprocess.PIPE,
                        ).stdout.strip(),
                        "tree": ids["tree"],
                        "status": "",
                    }
                ],
            },


            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    frozen = (workspace / "west.lock.yml").read_bytes()
    manifest.write_text(
        json.dumps(
            {
                "workspace_commit": commit,
                "frozen_manifest_sha256": hashlib.sha256(frozen).hexdigest(),
                "generated_profile_locks": [generated_row],
                "validated_nested_children": {},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
elif authority == "compare":
    result = Path(sys.argv[sys.argv.index("--result") + 1])
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(
        json.dumps(
            {
                "evidence_schema_version": 2,
                "verdict": "VALID",
                "batch_id": "dev-check-contract-batch",
                "expected_count": 1,
                "module_order": ["fixture/module"],
                "module_count": 1,
                "control_mode": "immutable-cherry-pick-oracle",
                "candidate_mode": "default-lock-first",
                "lock_first_evidence": "lock-first-evidence.json",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
'''


def executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if content.startswith("#!/usr/bin/env python3"):
        content = content.replace(
            "#!/usr/bin/env python3", f"#!{sys.executable}", 1
        )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=os.environ.copy(),
        check=True,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def load_log(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def clear_log(path: Path) -> None:
    path.unlink(missing_ok=True)


def must_reject(callback: Callable[[], object], fragment: str) -> None:
    try:
        callback()
    except dev_check.DevCheckError as error:
        assert fragment in str(error), error
    else:
        raise AssertionError(f"dev check accepted invalid input requiring {fragment!r}")

def package_copy(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def rewrite_package_index(
    package: Path, change: Callable[[dict[str, object]], None]
) -> None:
    index_path = package / "package-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    change(index)
    index_path.write_text(
        json.dumps(index, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    sums_path = package / "SHA256SUMS"
    sums = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        sums[relative] = digest
    sums["package-index.json"] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    sums_path.write_text(
        "".join(f"{digest}  {relative}\n" for relative, digest in sorted(sums.items())),
        encoding="utf-8",
    )


def reseal_package(package: Path) -> None:
    index_path = package / "package-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for row in index["files"]:
        data = (package / row["path"]).read_bytes()
        row["sha256"] = hashlib.sha256(data).hexdigest()
        row["bytes"] = len(data)
    index_path.write_text(
        json.dumps(index, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    sums = []
    for path in sorted(
        (item for item in package.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(package).as_posix(),
    ):
        relative = path.relative_to(package).as_posix()
        if relative == "SHA256SUMS":
            continue
        sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
    (package / "SHA256SUMS").write_text("".join(sums), encoding="utf-8")


def mutate_receipt_artifact(
    receipt: dict[str, object],
    name: str,
    change: Callable[[dict[str, object]], None],
) -> None:
    artifacts = receipt["acceptance_artifacts"]
    assert isinstance(artifacts, dict)
    row = artifacts[name]
    assert isinstance(row, dict)
    value = json.loads(row["content"])
    change(value)
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    row["content"] = data.decode()
    row["sha256"] = hashlib.sha256(data).hexdigest()
    row["bytes"] = len(data)


def mutate_packaged_artifact(
    package: Path,
    name: str,
    change: Callable[[dict[str, object]], None],
) -> None:
    receipt_path = package / "check-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    row = receipt["acceptance_artifacts"][name]
    value = json.loads(row["content"])
    change(value)
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    row["content"] = data.decode()
    row["sha256"] = hashlib.sha256(data).hexdigest()
    row["bytes"] = len(data)
    index = json.loads((package / "package-index.json").read_text(encoding="utf-8"))
    relative = next(
        item["path"] for item in index["acceptance"]["artifacts"] if item["name"] == name
    )
    (package / relative).write_bytes(data)
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    reseal_package(package)


def mutate_packaged_receipt(
    package: Path, change: Callable[[dict[str, object]], None]
) -> None:
    receipt_path = package / "check-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    change(receipt)
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    reseal_package(package)


def assert_plan(
    plan: dict[str, object], expected: list[tuple[str, list[str]]]
) -> None:
    assert plan["schema_version"] == 1
    assert plan["state"] == "planned"
    assert plan["returncode"] is None
    assert plan["results"] == []
    steps = plan["steps"]
    assert isinstance(steps, list)
    assert [(step["name"], step["argv"]) for step in steps] == expected
    assert all(Path(step["cwd"]).is_absolute() for step in steps)
    assert all(isinstance(step["env"], dict) for step in steps)
    for step in steps:
        assert step["read_only"] is (step["effect"] == "read-only")
        assert step["mutating"] is (step["effect"] != "read-only")
        assert isinstance(step["timeout_seconds"], int) and step["timeout_seconds"] > 0


def durable(path: Path, receipt: dict[str, object]) -> None:
    assert path.is_file() and not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == receipt


with tempfile.TemporaryDirectory(prefix="dev-check-contract-") as temporary:
    sandbox = Path(temporary)
    home = sandbox / "home"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("GIT_"):
            os.environ.pop(key)
    os.environ.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GH_TOKEN": "must-not-reach-parallel-stage",
            "GITHUB_TOKEN": "must-not-reach-parallel-stage",
            "GIT_ASKPASS": "/must/not/reach/parallel-stage",
            "SSH_ASKPASS": "/must/not/reach/parallel-stage",
            "SSH_AUTH_SOCK": "/must/not/reach/parallel-stage",
            "LC_ALL": "C.UTF-8",
        }
    )

    repo = sandbox / "active-manifest"
    outside = sandbox / "durable-evidence"
    repo.mkdir()
    outside.mkdir()
    fake_west = repo / "fixture-bin" / "west-authority"
    oracle = repo / "tests" / "patch_stack_immutable_oracle.py"
    tier_runner = repo / "ci" / "run-test-tier.sh"
    bootstrap = repo / "ci" / "bootstrap-west.sh"
    capture = repo / "ci" / "patch_stack_acceptance.py"
    compare = repo / "ci" / "patch_stack_lock_first_acceptance.py"
    mapping = repo / "locks" / "patch-stack" / "lock-first-series-v2.yml"
    executable(fake_west, FAKE_WEST)
    executable(oracle, FAKE_ORACLE)
    executable(tier_runner, FAKE_TIER)
    executable(bootstrap, FAKE_ACCEPTANCE)
    executable(capture, FAKE_ACCEPTANCE)
    executable(compare, FAKE_ACCEPTANCE)
    module_repo = sandbox / "fixture" / "module"
    module_repo.mkdir(parents=True)
    git(module_repo, "init", "-q")
    git(module_repo, "config", "user.name", "Fixture Module")
    git(module_repo, "config", "user.email", "fixture-module@example.invalid")
    (module_repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(module_repo, "add", ".")
    git(module_repo, "commit", "-qm", "fixture base")
    module_base = git(module_repo, "rev-parse", "HEAD")
    (module_repo / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    git(module_repo, "add", ".")
    git(module_repo, "commit", "-qm", "fixture change")
    module_source = git(module_repo, "rev-parse", "HEAD")
    module_tree = git(module_repo, "rev-parse", "HEAD^{tree}")
    fixture_ids = {
        "base": module_base,
        "source": module_source,
        "tree": module_tree,
        "module_repo": str(module_repo),
    }
    (repo / "fixture-ids.json").write_text(
        json.dumps(fixture_ids, sort_keys=True) + "\n", encoding="utf-8"
    )
    mapping.parent.mkdir(parents=True)
    mapping.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": "homebrew",
                "batch_id": "dev-check-contract-batch",
                "expected_count": 1,
                "series": [
                    {
                        "profile": "homebrew",
                        "module": "fixture/module",
                        "patch": "fixture/change.patch",
                        "lock": "fixture.lock.yml",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (mapping.parent / "lock-first-profiles-v1.yml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": [
                    {
                        "profile": "homebrew",
                        "mapping": mapping.name,
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (mapping.parent / "fixture.lock.yml").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "project": {"name": "fixture-module", "path": "."},
                "upstream": {"url": str(module_repo), "base_commit": module_base},
                "mirror": {
                    "url": str(module_repo),
                    "base_ref": f"refs/tags/patch-stack/v1/bases/{module_base}",
                    "base_oid": module_base,
                    "source_ref": f"refs/tags/patch-stack/v1/sources/{module_source}",
                    "source_oid": module_source,
                },
                "source_commit": module_source,
                "ordered_commits": [module_source],
                "expected_tree": module_tree,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "west.yml").write_text("manifest: {}\n", encoding="utf-8")
    (repo / "west.lock.yml").write_text("manifest: {}\n", encoding="utf-8")
    for profile in ("focused", "homebrew"):
        patch_dir = repo / "patches" / profile
        patch_dir.mkdir(parents=True)
        patch_data = f"{profile} fixture\n"
        patch_path = patch_dir / "fixture.patch"
        patch_path.write_text(patch_data, encoding="utf-8")
        (patch_dir / "patches.yml").write_text(
            json.dumps(
                {
                    "version": 1,
                    "patches": [
                        {
                            "path": "fixture.patch",
                            "sha256sum": hashlib.sha256(
                                patch_data.encode("utf-8")
                            ).hexdigest(),
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if profile == "homebrew":
            (patch_dir / "west.lock.yml").write_text(
                "manifest:\n  projects: []\n", encoding="utf-8"
            )
    (repo / "fixture.txt").write_text("manifest fixture\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "locks/patch-stack/*.ignored\n", encoding="utf-8"
    )
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Dev Check Contract")
    git(repo, "config", "user.email", "dev-check-contract@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "committed manifest fixture")
    assert git(repo, "status", "--porcelain") == ""

    log = outside / "authority.jsonl"
    os.environ["DEV_CHECK_FAKE_LOG"] = str(log)
    os.environ["DEV_CHECK_FAKE_MODE"] = "pass"
    west = [str(fake_west)]
    prefix = sandbox / "runtime-prefix"
    build_dir = sandbox / "runtime-build"
    prefix.mkdir()
    build_dir.mkdir()
    descendant_result = dev_check._run_process(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, time; "
                "subprocess.Popen("
                "['sleep', '60'], stdin=subprocess.DEVNULL, "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(0.05)"
            ),
        ],
        sandbox,
        10,
        {},
    )
    assert descendant_result["returncode"] == 125
    assert descendant_result["process_group_quiescent"] is False


    quick_evidence = outside / "quick-plan.json"
    quick = dev_check.build_check_plan(
        repo,
        west,
        "quick",
        "focused",
        "core-bead",
        "series/one.patch",
        quick_evidence,
        None,
        None,
    )
    assert_plan(
        quick,
        [
            (
                "patch-check",
                [
                    *west,
                    "patch",
                    "check",
                    "--profile",
                    "focused",
                    "--strict",
                    "--strict-quality",
                ],
            ),
            (
                "patch-export-check",
                [
                    *west,
                    "patch",
                    "export",
                    "--profile",
                    "focused",
                    "--patch",
                    "series/one.patch",
                    "--check",
                ],
            ),
            (
                "test-list",
                [
                    *west,
                    "test",
                    "--profile",
                    "focused",
                    "--bead",
                    "core-bead",
                    "--patch",
                    "series/one.patch",
                    "--list",
                ],
            ),
        ],
    )
    assert not quick_evidence.exists()
    clear_log(log)
    quick_receipt = dev_check.execute_check(quick)
    assert quick_receipt["state"] == "committed"
    assert quick_receipt["returncode"] == 0
    selection = quick_receipt["results"][2]["selection_oracle"]
    assert selection == {
        "required": True,
        "observed_listing_rows": 1,
        "selected": True,
    }
    durable(quick_evidence, quick_receipt)
    assert load_log(log) == [
        {"authority": "west", "argv": argv[1:]}
        for _name, argv in [
            (step["name"], step["argv"]) for step in quick["steps"]
        ]
    ]
    clear_log(log)

    check_receipt_path = outside / "canonical-check.json"
    canonical = dev_check.build_check_plan(
        repo,
        west,
        "canonical",
        "homebrew",
        None,
        None,
        check_receipt_path,
        None,
        None,
    )
    canonical_expected = [
        (
            "patch-check",
            [
                *west,
                "patch",
                "check",
                "--profile",
                "homebrew",
                "--strict",
                "--strict-quality",
            ],
        ),
        (
            "patch-export-check",
            [
                *west,
                "patch",
                "export",
                "--profile",
                "homebrew",
                "--check",
            ],
        ),
        (
            "test-list",
            [*west, "test", "--profile", "homebrew", "--list"],
        ),
        (
            "patch-verify",
            [*west, "patch", "verify", "--profile", "homebrew"],
        ),
        (
            "host-materialized-test",
            [
                *west,
                "test",
                "--profile",
                "homebrew",
                "--env",
                "host",
                "--materialize-profile",
            ],
        ),
    ]
    assert_plan(canonical, canonical_expected)
    canonical_receipt = dev_check.execute_check(canonical)
    assert canonical_receipt["state"] == "committed"
    assert canonical_receipt["returncode"] == 0
    assert [row["name"] for row in canonical_receipt["results"]] == [
        name for name, _argv in canonical_expected
    ]
    package_snapshot = canonical_receipt["inputs"]["package_snapshot"]
    assert package_snapshot["manifest_repo"] == str(repo)
    assert package_snapshot["dirty"] is False
    assert set(package_snapshot["content"]) == {
        "west.yml",
        "west.lock.yml",
        "patches/homebrew",
        "locks/patch-stack",
    }
    durable(check_receipt_path, canonical_receipt)
    assert load_log(log) == [
        {"authority": "west", "argv": argv[1:]} for _name, argv in canonical_expected
    ]

    parallel_barrier = outside / "parallel-barrier"
    os.environ["DEV_CHECK_PARALLEL_BARRIER"] = str(parallel_barrier)
    acceptance_path = outside / "acceptance-check.json"
    acceptance = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        acceptance_path,
        prefix,
        build_dir,
    )
    scratch = outside / f".dev-check-{acceptance['transaction_id']}"
    control_parent = scratch / "control"
    control = control_parent / "darling-workspace"
    candidate_parent = scratch / "lock-first"
    candidate = candidate_parent / "darling-workspace"
    artifacts = scratch / "evidence"
    oracle_output = artifacts / "immutable-oracle.json"
    modules = artifacts / "lock-first-modules.json"
    manifest = artifacts / "lock-first-manifest.json"
    lock_evidence = artifacts / "lock-first-evidence.json"
    comparison = artifacts / "acceptance-result.json"
    head = acceptance["inputs"]["package_snapshot"]["manifest_head"]
    acceptance_expected = [
        (
            "patch-check",
            [
                *west,
                "patch",
                "check",
                "--profile",
                "homebrew",
                "--strict",
                "--strict-quality",
            ],
        ),
        (
            "patch-export-check",
            [*west, "patch", "export", "--profile", "homebrew", "--check"],
        ),
        ("test-list", [*west, "test", "--profile", "homebrew", "--list"]),
        (
            "doctor",
            [
                *west,
                "darling-doctor",
                "--prefix",
                str(prefix),
                "--build-dir",
                str(build_dir),
                "--full",
            ],
        ),
        (
            "acceptance-clone-control",
            [
                "git",
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                str(repo),
                str(control),
            ],
        ),
        (
            "acceptance-checkout-control",
            ["git", "-C", str(control), "checkout", "--detach", head],
        ),
        (
            "acceptance-clone-candidate",
            [
                "git",
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                str(repo),
                str(candidate),
            ],
        ),
        (
            "acceptance-checkout-candidate",
            ["git", "-C", str(candidate), "checkout", "--detach", head],
        ),
        (
            "acceptance-bootstrap-candidate",
            [str(candidate / "ci" / "bootstrap-west.sh")],
        ),
        (
            "acceptance-configure-candidate-identity",
            [
                *west,
                "forall",
                "-c",
                "git config user.name 'West Dev Acceptance' && "
                "git config user.email 'west-dev-acceptance@example.invalid'",
            ],
        ),
        (
            "acceptance-seed-candidate-refs",
            [
                str(Path(sys.executable).resolve()),
                str(
                    candidate
                    / "ci"
                    / "patch_stack_lock_first_acceptance.py"
                ),
                "seed-source-refs",
                "--source-workspace",
                str(repo.parent),
                "--candidate-workspace",
                str(candidate_parent),
                "--profile",
                "homebrew",
            ],
        ),
        (
            "patch-verify",
            [*west, "patch", "verify", "--profile", "homebrew"],
        ),
        (
            "host-materialized-test",
            [
                *west,
                "test",
                "--profile",
                "homebrew",
                "--env",
                "host",
                "--materialize-profile",
            ],
        ),
        (
            "immutable-oracle",
            [
                str(Path(sys.executable).resolve()),
                str(control / "tests" / "patch_stack_immutable_oracle.py"),
                "--workspace",
                str(control),
                "--profile",
                "homebrew",
                "--mapping",
                str(control / "locks" / "patch-stack" / "lock-first-series-v2.yml"),
                "--output",
                str(oracle_output),
            ],
        ),
        (
            "acceptance-candidate-apply",
            [
                *west,
                "patch",
                "apply",
                "--profile",
                "homebrew",
                "--lock-first-evidence",
                str(lock_evidence),
            ],
        ),
        (
            "acceptance-capture",
            [
                str(Path(sys.executable).resolve()),
                str(candidate / "ci" / "patch_stack_acceptance.py"),
                "capture",
                "--workspace",
                str(candidate),
                "--profile",
                "homebrew",
                "--modules",
                str(modules),
                "--manifest",
                str(manifest),
            ],
        ),
        (
            "acceptance-compare",
            [
                str(Path(sys.executable).resolve()),
                str(candidate / "ci" / "patch_stack_lock_first_acceptance.py"),
                "compare-immutable-oracle",
                "--oracle",
                str(oracle_output),
                "--candidate",
                str(modules),
                "--candidate-manifest",
                str(manifest),
                "--evidence",
                str(lock_evidence),
                "--mapping",
                str(candidate / "locks" / "patch-stack" / "lock-first-series-v2.yml"),
                "--candidate-workspace",
                str(candidate_parent),
                "--manifest-workspace",
                str(candidate),
                "--transaction-root",
                str(scratch),
                "--result",
                str(comparison),
            ],
        ),
        (
            "acceptance-host-tier",
            [str(candidate / "ci" / "run-test-tier.sh"), "host"],
        ),
        (
            "acceptance-guest-smoke",
            [str(candidate / "ci" / "run-test-tier.sh"), "guest-smoke"],
        ),
    ]
    assert_plan(acceptance, acceptance_expected)
    def isolated_env(stage: str) -> dict[str, str]:
        environment = dict(
            acceptance["inputs"]["acceptance_checkpoint"]["identity"][
                "parallel_environment"
            ]
        )
        environment.update(
            {
                "CCACHE_DIR": str(scratch / "cache" / stage / "ccache"),
                "GIT_CONFIG_COUNT": "0",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": str(scratch / "home" / stage),
                "TMPDIR": "/tmp",
                "XDG_CACHE_HOME": str(scratch / "cache" / stage),
                "XDG_CONFIG_HOME": str(scratch / "config" / stage),
            }
        )
        return environment

    host_env = isolated_env("host")
    oracle_env = isolated_env("oracle")
    candidate_env = isolated_env("candidate")
    assert [(step["cwd"], step["env"]) for step in acceptance["steps"]] == [
        *((str(repo), {}) for _ in range(4)),
        (str(control_parent), oracle_env),
        (str(control), oracle_env),
        (str(candidate_parent), candidate_env),
        *((str(candidate), candidate_env) for _ in range(5)),
        (str(repo), host_env),
        (str(control), oracle_env),
        *((str(candidate), candidate_env) for _ in range(5)),
    ]
    clear_log(log)
    acceptance_receipt = dev_check.execute_check(acceptance)
    assert {
        path.name for path in parallel_barrier.glob("*.ready")
    } == {
        "patch-verify.ready",
        "host-materialized-test.ready",
        "immutable-oracle.ready",
    }
    assert acceptance_receipt["state"] == "committed"
    assert acceptance_receipt["returncode"] == 0
    assert acceptance_receipt["scratch"]["state"] == "cleaned"
    assert set(acceptance_receipt["acceptance_artifacts"]) == {
        "immutable_oracle",
        "lock_first_evidence",
        "comparison",
        "module_map",
        "candidate_manifest",
    }
    for name, artifact in acceptance_receipt["acceptance_artifacts"].items():
        raw = artifact["content"].encode("utf-8")
        assert artifact["bytes"] == len(raw)
        assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
        embedded = json.loads(raw)
        if name == "immutable_oracle":
            assert embedded["verdict"] == "VALID"
            assert embedded["oracle_schema_version"] == 2
            assert embedded["mode"] == "immutable-cherry-pick-oracle"
            assert embedded["profile"] == "homebrew"
        elif name in {"lock_first_evidence", "comparison"}:
            assert embedded["verdict"] == "VALID"
            assert embedded["evidence_schema_version"] == 2
        elif name == "module_map":
            assert embedded["profile"] == "homebrew"
            assert embedded["modules"]
        else:
            assert embedded["workspace_commit"] == head
    semantic_receipt = json.loads(json.dumps(acceptance_receipt))
    parent_module = "fixture/module"
    child_module = "fixture/module/nested"
    parent_candidate_tree = "d" * 40
    child_tree = "e" * 40
    child_commit = "f" * 40
    child_patch = "fixture/nested-change.patch"

    def add_oracle_child(value: dict[str, object]) -> None:
        batch = value["batches"][-1]
        batch["expected_count"] = 2
        batch["module_order"].append(child_module)
        batch["series_order"].append(
            {"module": child_module, "patch": child_patch}
        )
        child_series = dict(batch["series"][0])
        child_series.update(
            {
                "module": child_module,
                "patch": child_patch,
                "canonical_tree": child_tree,
                "applied_commit": child_commit,
                "applied_tree": child_tree,
            }
        )
        batch["series"].append(child_series)
        value["modules"].append(
            {"module": child_module, "commit": child_commit, "tree": child_tree}
        )
        value["clean_odb"]["module_count"] = 2
        value["clean_odb"]["immutable_fetch_transactions"] = 2

    def add_candidate_child(value: dict[str, object]) -> None:
        value["expected_count"] = 2
        value["module_order"].append(child_module)
        value["series_order"].append(
            {"module": child_module, "patch": child_patch}
        )
        child_series = dict(value["series"][0])
        child_series.update(
            {
                "module": child_module,
                "patch": child_patch,
                "canonical_tree": child_tree,
                "applied_commit": child_commit,
                "applied_tree": child_tree,
            }
        )
        value["series"].append(child_series)

    mutate_receipt_artifact(semantic_receipt, "immutable_oracle", add_oracle_child)
    mutate_receipt_artifact(semantic_receipt, "lock_first_evidence", add_candidate_child)
    mutate_receipt_artifact(
        semantic_receipt,
        "comparison",
        lambda value: (
            value.__setitem__("expected_count", 2),
            value["module_order"].append(child_module),
            value.__setitem__("module_count", 2),
        ),
    )

    def add_candidate_module(value: dict[str, object]) -> None:
        value["modules"][0]["tree"] = parent_candidate_tree
        value["modules"].append(
            {
                "module": child_module,
                "west_name": "fixture-module-nested",
                "path": child_module,
                "integration_profile": "homebrew",
                "integration_oid": child_commit,
                "tree": child_tree,
                "status": "",
            }
        )

    mutate_receipt_artifact(semantic_receipt, "module_map", add_candidate_module)
    mutate_receipt_artifact(
        semantic_receipt,
        "candidate_manifest",
        lambda value: value.__setitem__(
            "validated_nested_children", {parent_module: []}
        ),
    )
    dev_check._validate_acceptance_closure(semantic_receipt, "homebrew")
    invalid_semantic_receipt = json.loads(json.dumps(semantic_receipt))
    mutate_receipt_artifact(
        invalid_semantic_receipt,
        "module_map",
        lambda value: value["modules"][1].__setitem__("tree", "a" * 40),
    )
    must_reject(
        lambda: dev_check._validate_acceptance_closure(
            invalid_semantic_receipt, "homebrew"
        ),
        "module trees differ",
    )

    durable(acceptance_path, acceptance_receipt)
    assert not scratch.exists()
    fake_kinds = {
        "patch-check": ("west", 1),
        "patch-export-check": ("west", 1),
        "test-list": ("west", 1),
        "patch-verify": ("west", 1),
        "host-materialized-test": ("west", 1),
        "doctor": ("west", 1),
        "acceptance-bootstrap-candidate": ("bootstrap", 1),
        "acceptance-configure-candidate-identity": ("west", 1),
        "acceptance-seed-candidate-refs": ("seed", 2),
        "immutable-oracle": ("oracle", 2),
        "acceptance-candidate-apply": ("west", 1),
        "acceptance-capture": ("capture", 2),
        "acceptance-compare": ("compare", 2),
        "acceptance-host-tier": ("tier", 1),
        "acceptance-guest-smoke": ("tier", 1),
    }
    expected_acceptance_log = []
    for name, argv in acceptance_expected:
        if name in fake_kinds:
            authority, stripped = fake_kinds[name]
            expected_acceptance_log.append(
                {"authority": authority, "argv": argv[stripped:]}
            )
    observed_acceptance_log = load_log(log)
    parallel_start = 7
    parallel_end = parallel_start + len(dev_check._CHECKPOINT_STEP_NAMES)
    assert observed_acceptance_log[:parallel_start] == expected_acceptance_log[
        :parallel_start
    ]
    assert sorted(
        observed_acceptance_log[parallel_start:parallel_end],
        key=lambda row: (row["authority"], row["argv"]),
    ) == sorted(
        expected_acceptance_log[parallel_start:parallel_end],
        key=lambda row: (row["authority"], row["argv"]),
    )
    assert observed_acceptance_log[parallel_end:] == expected_acceptance_log[
        parallel_end:
    ]

    checkpoint_binding = acceptance["inputs"]["acceptance_checkpoint"]
    assert set(checkpoint_binding) == {
        "schema_version",
        "key",
        "path",
        "identity",
    }
    checkpoint_identity = checkpoint_binding["identity"]
    assert set(checkpoint_identity) == {
        "schema_version",
        "tool_version",
        "workspace_commit",
        "west_argv",
        "west_version",
        "parallel_environment",
        "west_package",
        "workspace_tree",
        "tool_files",
        "host_tools",
        "profile",
        "profile_graph",
        "mappings",
        "patches",
        "locks",
        "frozen_manifest_sha256",
    }
    assert checkpoint_identity["workspace_commit"] == head
    assert checkpoint_identity["west_argv"] == west
    assert checkpoint_identity["west_version"] == "West version: fixture-1.0"
    assert checkpoint_identity["west_package"] is None
    assert checkpoint_identity["parallel_environment"]["LC_ALL"] == "C.UTF-8"
    assert "GH_TOKEN" not in checkpoint_identity["parallel_environment"]
    assert checkpoint_identity["profile"] == "homebrew"
    assert [row["profile"] for row in checkpoint_identity["profile_graph"]] == [
        "homebrew"
    ]
    assert checkpoint_identity["tool_files"]
    for row in checkpoint_identity["tool_files"]:
        path = Path(row["path"])
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
        assert path.stat().st_size == row["bytes"]
    assert {"cc", "cxx", "cmake", "ctest", "bash"}.issubset(
        {row["name"] for row in checkpoint_identity["host_tools"]}
    )
    for row in checkpoint_identity["host_tools"]:
        path = Path(row["path"])
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
        assert path.stat().st_size == row["bytes"]
    assert checkpoint_identity["mappings"]
    assert checkpoint_identity["patches"]
    for field in ("profile_graph", "mappings", "patches", "locks"):
        for row in checkpoint_identity[field]:
            path = repo / row.get("manifest", row.get("path", ""))
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    identity_repo = outside / "identity-manifest"
    subprocess.run(
        ["git", "clone", "-q", str(repo), str(identity_repo)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    git(identity_repo, "config", "user.email", "dev-check@example.invalid")
    git(identity_repo, "config", "user.name", "Dev Check")
    identity_patch = identity_repo / "patches" / "homebrew" / "fixture.patch"
    identity_patch.write_text(
        identity_patch.read_text(encoding="utf-8") + "identity change\n",
        encoding="utf-8",
    )
    identity_manifest_path = (
        identity_repo / "patches" / "homebrew" / "patches.yml"
    )
    identity_manifest = json.loads(
        identity_manifest_path.read_text(encoding="utf-8")
    )
    identity_manifest["patches"][0]["sha256sum"] = hashlib.sha256(
        identity_patch.read_bytes()
    ).hexdigest()
    identity_manifest_path.write_text(
        json.dumps(identity_manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    git(identity_repo, "add", "patches/homebrew")
    git(identity_repo, "commit", "-qm", "change checkpoint identity")
    changed_identity_plan = dev_check.build_check_plan(
        identity_repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        outside / "changed-identity-check.json",
        prefix,
        build_dir,
    )
    changed_binding = changed_identity_plan["inputs"]["acceptance_checkpoint"]
    assert changed_binding["key"] != checkpoint_binding["key"]
    assert changed_binding["identity"]["patches"] != checkpoint_identity["patches"]
    alternate_launcher_plan = dev_check.build_check_plan(
        repo,
        [*west, "--alternate-launcher"],
        "acceptance",
        "homebrew",
        None,
        None,
        outside / "alternate-launcher-check.json",
        prefix,
        build_dir,
    )
    alternate_binding = alternate_launcher_plan["inputs"][
        "acceptance_checkpoint"
    ]
    assert alternate_binding["key"] != checkpoint_binding["key"]
    assert alternate_binding["identity"]["west_argv"] != (
        checkpoint_identity["west_argv"]
    )
    previous_cflags = os.environ.get("CFLAGS")
    os.environ["CFLAGS"] = "-Ocheckpoint-environment"
    changed_environment_plan = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        outside / "changed-environment-check.json",
        prefix,
        build_dir,
    )
    if previous_cflags is None:
        os.environ.pop("CFLAGS")
    else:
        os.environ["CFLAGS"] = previous_cflags
    changed_environment_binding = changed_environment_plan["inputs"][
        "acceptance_checkpoint"
    ]
    assert changed_environment_binding["key"] != checkpoint_binding["key"]
    assert changed_environment_binding["identity"]["parallel_environment"] != (
        checkpoint_identity["parallel_environment"]
    )
    assert checkpoint_identity["locks"]
    checkpoint_path = Path(checkpoint_binding["path"])
    assert checkpoint_path.is_file() and not checkpoint_path.is_symlink()
    assert stat.S_IMODE(checkpoint_path.stat().st_mode) == 0o600
    assert acceptance_receipt["checkpoint"]["state"] == "published"
    assert acceptance_receipt["checkpoint"]["reason"] == "published"

    reused_path = outside / "acceptance-check-reused.json"
    reused_plan = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        reused_path,
        prefix,
        build_dir,
    )
    assert (
        reused_plan["inputs"]["acceptance_checkpoint"]["key"]
        == checkpoint_binding["key"]
    )
    clear_log(log)
    progress_events = []
    reused_receipt = dev_check.execute_check(
        reused_plan, progress=progress_events.append
    )
    assert reused_receipt["state"] == "committed"
    assert reused_receipt["checkpoint"] == {
        "schema_version": 1,
        "key": checkpoint_binding["key"],
        "path": str(checkpoint_path),
        "state": "reused",
        "reason": "valid",
    }
    reused_results = {
        result["name"]: result for result in reused_receipt["results"]
    }
    assert [
        name
        for name in dev_check._CHECKPOINT_STEP_NAMES
        if reused_results[name].get("checkpoint_reused") is True
    ] == list(dev_check._CHECKPOINT_STEP_NAMES)
    assert [
        event["name"] for event in progress_events if event["phase"] == "reuse"
    ] == list(dev_check._CHECKPOINT_STEP_NAMES)
    original_results = {
        result["name"]: result for result in acceptance_receipt["results"]
    }
    for name in dev_check._CHECKPOINT_STEP_NAMES:
        assert reused_results[name]["stdout"] == original_results[name]["stdout"]
        assert reused_results[name]["stderr"] == original_results[name]["stderr"]
    assert not {
        event["name"]
        for event in progress_events
        if event["phase"] == "start"
    }.intersection(dev_check._CHECKPOINT_STEP_NAMES)
    assert {
        "acceptance-candidate-apply",
        "acceptance-capture",
        "acceptance-compare",
        "acceptance-host-tier",
        "acceptance-guest-smoke",
    }.issubset(
        {
            event["name"]
            for event in progress_events
            if event["phase"] == "start"
        }
    )
    observed_reuse_log = load_log(log)
    expected_reuse_log = []
    for step in reused_plan["steps"]:
        name = step["name"]
        if name in fake_kinds and name not in dev_check._CHECKPOINT_STEP_NAMES:
            authority, stripped = fake_kinds[name]
            expected_reuse_log.append(
                {"authority": authority, "argv": step["argv"][stripped:]}
            )
    assert observed_reuse_log == expected_reuse_log
    assert (
        reused_receipt["acceptance_artifacts"]["immutable_oracle"]["sha256"]
        == acceptance_receipt["acceptance_artifacts"]["immutable_oracle"]["sha256"]
    )
    durable(reused_path, reused_receipt)
    acceptance_path = reused_path
    acceptance_receipt = reused_receipt

    invalid_checkpoint_receipt = json.loads(json.dumps(reused_receipt))
    invalid_checkpoint_receipt["checkpoint"]["state"] = "published"
    must_reject(
        lambda: dev_check._validate_checkpoint_receipt(
            invalid_checkpoint_receipt,
            invalid_checkpoint_receipt["inputs"],
            invalid_checkpoint_receipt["results"],
        ),
        "verdict",
    )
    invalid_reuse_receipt = json.loads(json.dumps(reused_receipt))
    reused_patch_verify = next(
        result
        for result in invalid_reuse_receipt["results"]
        if result["name"] == "patch-verify"
    )
    del reused_patch_verify["checkpoint_source_duration_ns"]
    must_reject(
        lambda: dev_check._validate_checkpoint_receipt(
            invalid_reuse_receipt,
            invalid_reuse_receipt["inputs"],
            invalid_reuse_receipt["results"],
        ),
        "provenance",
    )
    corrupt_path = outside / "acceptance-check-corrupt-cache.json"
    corrupt_plan = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        corrupt_path,
        prefix,
        build_dir,
    )
    corrupt_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    corrupt_oracle = json.loads(corrupt_checkpoint["oracle"]["content"])
    corrupt_oracle["clean_odb"]["alternates"] = 1
    corrupt_content = json.dumps(corrupt_oracle, sort_keys=True) + "\n"
    corrupt_checkpoint["oracle"] = {
        "bytes": len(corrupt_content.encode("utf-8")),
        "sha256": hashlib.sha256(corrupt_content.encode("utf-8")).hexdigest(),
        "content": corrupt_content,
    }
    checkpoint_path.write_text(
        json.dumps(corrupt_checkpoint, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(parallel_barrier)
    clear_log(log)
    corrupt_receipt = dev_check.execute_check(corrupt_plan)
    assert corrupt_receipt["state"] == "committed"
    assert corrupt_receipt["checkpoint"]["state"] == "published"
    assert corrupt_receipt["checkpoint"]["reason"] == "published"
    assert {
        path.name for path in parallel_barrier.glob("*.ready")
    } == {
        "patch-verify.ready",
        "host-materialized-test.ready",
        "immutable-oracle.ready",
    }
    assert checkpoint_path.is_file()
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["key"] == (
        checkpoint_binding["key"]
    )

    safe_checkpoint = checkpoint_path.with_suffix(".safe")
    checkpoint_path.rename(safe_checkpoint)
    checkpoint_path.symlink_to(safe_checkpoint)
    unsafe_path = outside / "acceptance-check-unsafe-cache.json"
    unsafe_plan = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        unsafe_path,
        prefix,
        build_dir,
    )
    unsafe_receipt = dev_check.execute_check(unsafe_plan)
    assert unsafe_receipt["state"] == "failed"
    assert unsafe_receipt["returncode"] == 1
    assert "checkpoint path is unsafe" in unsafe_receipt["error"]
    assert unsafe_receipt["scratch"]["state"] == "cleaned"
    assert not (
        outside / f".dev-check-{unsafe_plan['transaction_id']}"
    ).exists()
    checkpoint_path.unlink()
    safe_checkpoint.rename(checkpoint_path)
    checkpoint_path.unlink()
    checkpoint_path.symlink_to(outside / "missing-checkpoint-target")
    dangling_path = outside / "acceptance-check-dangling-cache.json"
    dangling_plan = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        dangling_path,
        prefix,
        build_dir,
    )
    dangling_receipt = dev_check.execute_check(dangling_plan)
    assert dangling_receipt["state"] == "failed"
    assert "checkpoint path is unsafe" in dangling_receipt["error"]
    assert dangling_receipt["scratch"]["state"] == "cleaned"

    checkpoint_path.unlink()
    shutil.rmtree(parallel_barrier)
    clear_log(log)
    os.environ["DEV_CHECK_FAKE_MODE"] = "fail-parallel"
    parallel_cleanup = outside / "parallel-cleanup"
    os.environ["DEV_CHECK_PARALLEL_CLEANUP"] = str(parallel_cleanup)
    failed_parallel_path = outside / "acceptance-check-parallel-failure.json"
    failed_parallel_plan = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        failed_parallel_path,
        prefix,
        build_dir,
    )
    failed_parallel_receipt = dev_check.execute_check(failed_parallel_plan)
    os.environ["DEV_CHECK_FAKE_MODE"] = "pass"
    assert failed_parallel_receipt["state"] == "failed"
    assert failed_parallel_receipt["returncode"] == 43
    assert failed_parallel_receipt["duration_ms"] < 10_000
    assert failed_parallel_receipt["scratch"]["state"] == "cleaned"
    failed_parallel_results = {
        result["name"]: result for result in failed_parallel_receipt["results"]
    }
    assert failed_parallel_results["patch-verify"]["returncode"] == 43
    for name in ("host-materialized-test", "immutable-oracle"):
        assert failed_parallel_results[name]["returncode"] == 125
        assert failed_parallel_results[name]["cancelled_by_peer"] is True
        assert failed_parallel_results[name]["process_group_quiescent"] is True
    assert "acceptance-candidate-apply" not in failed_parallel_results
    observed_parallel_cleanup = {
        path.name for path in parallel_cleanup.glob("*.cleaned")
    }
    assert observed_parallel_cleanup == {
        "host-materialized-test.cleaned",
        "immutable-oracle.cleaned",
    }, observed_parallel_cleanup
    assert not list(parallel_cleanup.glob("*.orphan"))
    assert not checkpoint_path.exists()

    shutil.rmtree(parallel_barrier)
    shutil.rmtree(parallel_cleanup)
    clear_log(log)
    os.environ["DEV_CHECK_FAKE_MODE"] = "interrupt-parallel"
    interrupted_parallel_path = (
        outside / "acceptance-check-parallel-interrupt.json"
    )
    interrupted_parallel_plan = dev_check.build_check_plan(
        repo,
        west,
        "acceptance",
        "homebrew",
        None,
        None,
        interrupted_parallel_path,
        prefix,
        build_dir,
    )
    interrupted_parallel_receipt = dev_check.execute_check(
        interrupted_parallel_plan
    )
    os.environ["DEV_CHECK_FAKE_MODE"] = "pass"
    assert interrupted_parallel_receipt["state"] == "interrupted"
    assert interrupted_parallel_receipt["returncode"] == 130
    assert interrupted_parallel_receipt["scratch"]["state"] == "cleaned"
    interrupted_parallel_results = {
        result["name"]: result
        for result in interrupted_parallel_receipt["results"]
    }
    assert set(dev_check._CHECKPOINT_STEP_NAMES).issubset(
        interrupted_parallel_results
    )
    for name in dev_check._CHECKPOINT_STEP_NAMES:
        assert interrupted_parallel_results[name][
            "process_group_quiescent"
        ] is True
    assert "acceptance-candidate-apply" not in interrupted_parallel_results
    assert {
        path.name for path in parallel_cleanup.glob("*.cleaned")
    } == {
        "host-materialized-test.cleaned",
        "immutable-oracle.cleaned",
    }
    assert not list(parallel_cleanup.glob("*.orphan"))

    def acceptance_plan(
        *,
        profile: str = "homebrew",
        bead: str | None = None,
        patch: str | None = None,
        selected_prefix: Path | None = prefix,
        selected_build: Path | None = build_dir,
    ) -> object:
        return dev_check.build_check_plan(
            repo,
            west,
            "acceptance",
            profile,
            bead,
            patch,
            outside / "rejected-acceptance.json",
            selected_prefix,
            selected_build,
        )

    must_reject(lambda: acceptance_plan(profile="focused"), "require profile 'homebrew'")
    must_reject(lambda: acceptance_plan(bead="one"), "cannot be narrowed")
    must_reject(lambda: acceptance_plan(patch="one.patch"), "cannot be narrowed")
    must_reject(lambda: acceptance_plan(selected_prefix=None), "require both prefix")
    must_reject(lambda: acceptance_plan(selected_build=None), "require both prefix")
    must_reject(
        lambda: dev_check.build_check_plan(
            repo,
            west,
            "quick",
            "homebrew",
            None,
            None,
            repo / "inside-repo.json",
            None,
            None,
        ),
        "outside the active manifest repository",
    )
    manifest_fixture = repo / "fixture.txt"
    manifest_fixture.write_text("dirty manifest fixture\n", encoding="utf-8")
    must_reject(
        lambda: acceptance_plan(),
        "acceptance checks require a clean manifest repository",
    )
    manifest_fixture.write_text("manifest fixture\n", encoding="utf-8")
    assert git(repo, "status", "--porcelain") == ""

    ignored_input = repo / "locks" / "patch-stack" / "fixture.ignored"
    ignored_input.write_text("ignored package input\n", encoding="utf-8")
    assert git(repo, "status", "--porcelain") == ""
    must_reject(
        lambda: acceptance_plan(),
        "acceptance package inputs contain ignored files",
    )
    ignored_input.unlink()

    failure_path = outside / "failed-check.json"
    failure_plan = dev_check.build_check_plan(
        repo,
        west,
        "quick",
        "homebrew",
        None,
        None,
        failure_path,
        None,
        None,
    )
    clear_log(log)
    os.environ["DEV_CHECK_FAKE_MODE"] = "fail-check"
    failed = dev_check.execute_check(failure_plan)
    durable(failure_path, failed)
    assert failed["state"] == "failed"
    assert failed["returncode"] == 37
    assert len(failed["results"]) == 1
    failure_result = failed["results"][0]
    assert failure_result["name"] == "patch-check"
    assert failure_result["returncode"] == 37
    stdout_data = ("O" * 20000 + "\nSTDOUT-END\n").encode()
    stderr_data = ("E" * 21000 + "\nSTDERR-END\n").encode()
    for capture, raw, ending in (
        (failure_result["stdout"], stdout_data, "STDOUT-END\n"),
        (failure_result["stderr"], stderr_data, "STDERR-END\n"),
    ):
        assert capture["bytes"] == len(raw)
        assert capture["sha256"] == hashlib.sha256(raw).hexdigest()
        assert capture["truncated"] is True
        assert capture["tail"].endswith(ending)
        assert len(capture["tail"].encode()) <= 16 * 1024
    assert load_log(log) == [
        {"authority": "west", "argv": failure_plan["steps"][0]["argv"][1:]}
    ]

    interrupt_path = outside / "interrupted-check.json"
    interrupt_plan = dev_check.build_check_plan(
        repo,
        west,
        "quick",
        "homebrew",
        None,
        None,
        interrupt_path,
        None,
        None,
    )
    markers = outside / "interrupt-markers"
    ready = outside / "interrupt-descendant.pid"
    os.environ["DEV_CHECK_INTERRUPT_MARKERS"] = str(markers)
    os.environ["DEV_CHECK_INTERRUPT_READY"] = str(ready)
    os.environ["DEV_CHECK_FAKE_MODE"] = "interrupt"
    clear_log(log)
    interrupted = dev_check.execute_check(interrupt_plan)
    durable(interrupt_path, interrupted)
    assert interrupted["state"] == "interrupted"
    assert interrupted["returncode"] == 130
    assert interrupted["results"][0]["returncode"] == 130
    assert interrupted["results"][0]["interrupted"] is True
    assert (markers / "leader").read_text(encoding="utf-8") == "SIGINT\n"
    assert (markers / "descendant").read_text(encoding="utf-8") == "SIGINT\n"

    raced_check_evidence = outside / "raced-check-evidence.json"
    raced_check_plan = dev_check.build_check_plan(
        repo,
        west,
        "quick",
        "homebrew",
        None,
        None,
        raced_check_evidence,
        None,
        None,
    )
    foreign_evidence = b"foreign evidence survives\n"
    real_create_json = dev_check._create_json
    evidence_race_injected = [False]

    def inject_foreign_evidence(
        path: Path, payload: dict[str, object], directory_fd: int
    ) -> dict[str, int]:
        if path == raced_check_evidence and not evidence_race_injected[0]:
            evidence_race_injected[0] = True
            path.write_bytes(foreign_evidence)
        return real_create_json(path, payload, directory_fd)

    dev_check._create_json = inject_foreign_evidence
    try:
        must_reject(
            lambda: dev_check.execute_check(raced_check_plan),
            "appeared before initial publication",
        )
    finally:
        dev_check._create_json = real_create_json
    assert evidence_race_injected == [True]
    assert raced_check_evidence.read_bytes() == foreign_evidence

    real_rename_exchange = dev_check._rename_exchange
    for exchange_race_mode in ("unlink", "replace"):
        exchange_evidence = outside / f"exchange-{exchange_race_mode}.json"
        exchange_plan = dev_check.build_check_plan(
            repo,
            west,
            "quick",
            "homebrew",
            None,
            None,
            exchange_evidence,
            None,
            None,
        )
        exchange_race_injected = [False]
        exchange_foreign = b"foreign after exchange\n"

        def race_destination_after_exchange(
            directory_fd: int, left: str, right: str
        ) -> None:
            real_rename_exchange(directory_fd, left, right)
            if (
                not exchange_race_injected[0]
                and right == exchange_evidence.name
            ):
                exchange_race_injected[0] = True
                os.unlink(right, dir_fd=directory_fd)
                if exchange_race_mode == "replace":
                    descriptor = os.open(
                        right,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    try:
                        os.write(descriptor, exchange_foreign)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)

        dev_check._rename_exchange = race_destination_after_exchange
        try:
            must_reject(
                lambda: dev_check.execute_check(exchange_plan),
                "evidence identity changed",
            )
        finally:
            dev_check._rename_exchange = real_rename_exchange
        assert exchange_race_injected == [True]
        if exchange_race_mode == "replace":
            assert exchange_evidence.read_bytes() == exchange_foreign
        else:
            assert not exchange_evidence.exists()

    substituted_temp_evidence = outside / "substituted-temp-evidence.json"
    substituted_temp_plan = dev_check.build_check_plan(
        repo,
        west,
        "quick",
        "homebrew",
        None,
        None,
        substituted_temp_evidence,
        None,
        None,
    )
    real_write_json_temp = dev_check._write_json_temp
    temp_write_count = [0]
    substituted_temp_path: list[Path] = []
    foreign_temp_data = b"foreign temporary evidence\n"

    def substitute_closed_temp(
        directory_fd: int, name: str, data: bytes
    ) -> int:
        descriptor = real_write_json_temp(directory_fd, name, data)
        temp_write_count[0] += 1
        if temp_write_count[0] == 2:
            os.unlink(name, dir_fd=directory_fd)
            foreign_descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(foreign_descriptor, foreign_temp_data)
                os.fsync(foreign_descriptor)
            finally:
                os.close(foreign_descriptor)
            substituted_temp_path.append(outside / name)
        return descriptor

    dev_check._write_json_temp = substitute_closed_temp
    try:
        must_reject(
            lambda: dev_check.execute_check(substituted_temp_plan),
            "temporary identity changed before update",
        )
    finally:
        dev_check._write_json_temp = real_write_json_temp
    assert temp_write_count == [2]
    assert len(substituted_temp_path) == 1
    assert substituted_temp_path[0].read_bytes() == foreign_temp_data

    os.environ["DEV_CHECK_FAKE_MODE"] = "package-ok"
    must_reject(
        lambda: dev_check.build_package_plan(
            repo,
            west,
            "homebrew",
            check_receipt_path,
            outside / "weak-gate-output",
            outside / "weak-gate-receipt.json",
            "quick",
        ),
        "requires an acceptance check receipt",
    )
    must_reject(
        lambda: dev_check.build_package_plan(
            repo,
            west,
            "homebrew",
            check_receipt_path,
            outside / "too-weak-output",
            outside / "too-weak-receipt.json",
            "acceptance",
        ),
        "below required tier",
    )
    must_reject(
        lambda: dev_check.build_package_plan(
            repo,
            west,
            "focused",
            acceptance_path,
            outside / "wrong-profile-output",
            outside / "wrong-profile-receipt.json",
            "acceptance",
        ),
        "profile does not match",
    )
    must_reject(
        lambda: dev_check.build_package_plan(
            repo,
            west,
            "focused",
            quick_evidence,
            outside / "narrowed-output",
            outside / "narrowed-package-receipt.json",
            "quick",
        ),
        "requires an acceptance check receipt",
    )

    existing_output = outside / "existing-output"
    existing_output.mkdir()
    existing_marker = existing_output / "preserve"
    existing_marker.write_text("preexisting\n", encoding="utf-8")
    must_reject(
        lambda: dev_check.build_package_plan(
            repo,
            west,
            "homebrew",
            acceptance_path,
            existing_output,
            outside / "existing-output-receipt.json",
            "canonical",
        ),
        "already exists",
    )
    assert existing_marker.read_text(encoding="utf-8") == "preexisting\n"
    must_reject(
        lambda: dev_check.build_package_plan(
            repo,
            west,
            "homebrew",
            acceptance_path,
            repo / "forbidden-package",
            outside / "forbidden-package-receipt.json",
            "canonical",
        ),
        "outside the active manifest repository",
    )

    package_output = outside / "package"
    package_receipt_path = outside / "package-receipt.json"
    check_receipt_digest = hashlib.sha256(acceptance_path.read_bytes()).hexdigest()
    package_plan = dev_check.build_package_plan(
        repo,
        west,
        "homebrew",
        acceptance_path,
        package_output,
        package_receipt_path,
        "acceptance",
    )
    assert package_plan["inputs"]["receipt_sha256"] == check_receipt_digest
    assert package_plan["inputs"]["receipt_transaction_id"] == acceptance_receipt[
        "transaction_id"
    ]
    assert package_plan["inputs"]["output"] == str(package_output)
    package_root = Path(package_plan["inputs"]["staging_root"])
    package_staging = Path(package_plan["inputs"]["staging"])
    package_marker = Path(package_plan["inputs"]["marker"])
    assert package_root.parent == package_output.parent
    assert package_staging.parent == package_root
    assert package_marker.parent == package_root
    assert_plan(
        package_plan,
        [
            (
                "patch-export-locks",
                [
                    *west,
                    "patch",
                    "export-locks",
                    "--profile",
                    "homebrew",
                    "--output",
                    str(package_staging),
                ],
            )
        ],
    )
    clear_log(log)
    packaged = dev_check.execute_package(package_plan)
    durable(package_receipt_path, packaged)
    assert packaged["state"] == "committed", packaged.get("error")
    assert packaged["returncode"] == 0
    assert packaged["results"][0]["returncode"] == 0
    assert packaged["staging"]["path"] == str(package_staging)
    assert packaged["staging"]["state"] == "published"
    assert packaged["ownership"]["root"] == str(package_root)
    assert packaged["ownership"]["marker"] == str(package_marker)
    assert packaged["ownership"]["state"] == "retained-through-durable-commit"
    assert not package_root.exists()
    assert not package_staging.exists() and not package_marker.exists()
    package_binding = packaged["package"]
    assert package_binding["output"] == str(package_output)

    verified = dev_check.verify_package(package_output)
    assert verified["schema_version"] == 1
    assert verified["operation"] == "package-verify"
    assert verified["state"] == "valid"
    assert verified["returncode"] == 0
    assert verified["profile"] == "homebrew"
    package_index = verified["package_index"]
    assert package_index["manifest"]["commit"] == acceptance_receipt["inputs"][
        "package_snapshot"
    ]["manifest_head"]
    assert package_index["manifest"]["tree"] == acceptance_receipt["inputs"][
        "package_snapshot"
    ]["manifest_tree"]
    assert set(package_index["source_closure"]) == {
        "paths",
        "generated_locks",
        "manifest_bundle",
        "module_bundles",
        "lock_bindings",
    }
    assert package_index["acceptance"]["cleanup"] == "complete"
    assert package_index["checks"]
    assert (package_output / "package-index.json").is_file()
    assert (package_output / "SHA256SUMS").is_file()
    assert (package_output / "check-receipt.json").read_bytes() == acceptance_path.read_bytes()
    generated_binding = package_index["source_closure"]["generated_locks"][0]
    assert (
        package_output / generated_binding["path"]
    ).read_bytes() == (
        package_output / generated_binding["source_copy"]
    ).read_bytes()
    assert (
        package_output / generated_binding["path"]
    ).read_bytes() != (
        package_output / "source" / generated_binding["source_path"]
    ).read_bytes()

    extra_package = package_copy(package_output, outside / "verify-extra")
    (extra_package / "foreign").write_text("foreign\n", encoding="utf-8")
    must_reject(lambda: dev_check.verify_package(extra_package), "extra files")

    missing_package = package_copy(package_output, outside / "verify-missing")
    (missing_package / "evidence.json").unlink()
    must_reject(lambda: dev_check.verify_package(missing_package), "files are missing")
    assert package_index["integrity_boundary"] == {
        "mode": "local-owner-mutable",
        "publication": "verified-before-and-after-atomic-rename",
        "consumer_requirement": "verify-package immediately before use",
        "verify_operation": "package-verify",
    }
    candidate_module = package_index["source_closure"]["module_bundles"][0]
    assert candidate_module["candidate_object_authority"] == (
        "validated-acceptance-receipt"
    )
    assert candidate_module["candidate_integration_commit"] != module_source
    assert (
        subprocess.run(
            [
                "git",
                "cat-file",
                "-e",
                f"{candidate_module['candidate_integration_commit']}^{{commit}}",
            ],
            cwd=module_repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        != 0
    )

    tampered_package = package_copy(package_output, outside / "verify-tampered")
    tampered_mbox = tampered_package / package_index["export"]["mboxes"][0]
    tampered_mbox.write_bytes(tampered_mbox.read_bytes() + b"tampered\n")
    must_reject(lambda: dev_check.verify_package(tampered_package), "digest mismatch")

    stale_log_package = package_copy(package_output, outside / "verify-stale-log")
    mutate_packaged_receipt(
        stale_log_package,
        lambda value: (
            value["results"][0]["stdout"].__setitem__("tail", "stale"),
            value["results"][0].__setitem__("stdout_tail", "stale"),
        ),
    )
    must_reject(
        lambda: dev_check.verify_package(stale_log_package),
        "index bindings",
    )

    mismatch_package = package_copy(package_output, outside / "verify-receipt-mismatch")
    mutate_packaged_receipt(
        mismatch_package,
        lambda value: value.__setitem__("transaction_id", "0" * 32),
    )
    must_reject(lambda: dev_check.verify_package(mismatch_package), "steps differ")

    wrong_module_package = package_copy(package_output, outside / "verify-module")
    mutate_packaged_artifact(
        wrong_module_package,
        "module_map",
        lambda value: value["modules"][0].__setitem__("tree", "9" * 40),
    )
    must_reject(
        lambda: dev_check.verify_package(wrong_module_package),
        "module trees differ",
    )

    wrong_manifest_package = package_copy(package_output, outside / "verify-manifest")
    mutate_packaged_artifact(
        wrong_manifest_package,
        "candidate_manifest",
        lambda value: value.__setitem__("workspace_commit", "9" * 40),
    )
    must_reject(
        lambda: dev_check.verify_package(wrong_manifest_package),
        "manifest commit differs",
    )
    missing_generated_package = package_copy(
        package_output, outside / "verify-generated-missing"
    )
    mutate_packaged_artifact(
        missing_generated_package,
        "candidate_manifest",
        lambda value: value.__setitem__("generated_profile_locks", []),
    )
    mutate_packaged_receipt(
        missing_generated_package,
        lambda value: value["acceptance_artifacts"]["candidate_manifest"].__setitem__(
            "generated_locks", []
        ),
    )
    must_reject(
        lambda: dev_check.verify_package(missing_generated_package),
        "candidate generated lock extension is invalid",
    )

    duplicate_generated_package = package_copy(
        package_output, outside / "verify-generated-duplicate"
    )
    mutate_packaged_artifact(
        duplicate_generated_package,
        "immutable_oracle",
        lambda value: value["generated_profile_locks"].append(
            dict(value["generated_profile_locks"][0])
        ),
    )
    mutate_packaged_artifact(
        duplicate_generated_package,
        "candidate_manifest",
        lambda value: value["generated_profile_locks"].append(
            dict(value["generated_profile_locks"][0])
        ),
    )
    mutate_packaged_receipt(
        duplicate_generated_package,
        lambda value: value["acceptance_artifacts"]["candidate_manifest"][
            "generated_locks"
        ].append(
            dict(
                value["acceptance_artifacts"]["candidate_manifest"][
                    "generated_locks"
                ][0]
            )
        ),
    )
    must_reject(
        lambda: dev_check.verify_package(duplicate_generated_package),
        "invalid or duplicated",
    )


    wrong_lock_package = package_copy(package_output, outside / "verify-lock")
    mutate_packaged_artifact(
        wrong_lock_package,
        "lock_first_evidence",
        lambda value: value["series"][0].__setitem__("base", "9" * 40),
    )
    must_reject(
        lambda: dev_check.verify_package(wrong_lock_package),
        "lock-first evidence base differs",
    )

    wrong_mapping_package = package_copy(package_output, outside / "verify-mapping")
    rewrite_package_index(
        wrong_mapping_package,
        lambda value: value["source_closure"]["paths"][3].__setitem__(
            "sha256", "9" * 64
        ),
    )
    must_reject(
        lambda: dev_check.verify_package(wrong_mapping_package),
        "package source closure index differs from packaged bytes",
    )

    incomplete_cleanup_package = package_copy(
        package_output, outside / "verify-incomplete-cleanup"
    )
    assert package_binding["publication_verification"] == {
        "verified_after_rename": True,
        "package_index_sha256": verified["package_index_sha256"],
        "output_identity": package_binding["output_identity"],
    }
    assert package_binding["verify_before_use"]["argv"] == [
        *west,
        "dev",
        "verify-package",
        str(package_output),
        "--json",
    ]
    assert "local package files remain mutable" in package_binding[
        "verify_before_use"
    ]["reason"]
    mutate_packaged_receipt(
        incomplete_cleanup_package,
        lambda value: value["scratch"].__setitem__("state", "cleanup-failed"),
    )
    must_reject(
        lambda: dev_check.verify_package(incomplete_cleanup_package),
        "cleanup verdict is incomplete",
    )
    assert package_binding["check_receipt"] == {
        "path": str(package_output / "check-receipt.json"),
        "source_path": str(acceptance_path),
        "sha256": check_receipt_digest,
        "transaction_id": acceptance_receipt["transaction_id"],
        "tier": "acceptance",
    }
    assert package_binding["package_snapshot"] == acceptance_receipt["inputs"][
        "package_snapshot"
    ]
    assert package_binding["output_identity"] == {
        "device": package_output.stat().st_dev,
        "inode": package_output.stat().st_ino,
    }
    export_evidence_path = package_output / "evidence.json"
    export_evidence_data = export_evidence_path.read_bytes()
    assert package_binding["export_evidence"]["path"] == str(export_evidence_path)
    assert package_binding["export_evidence"]["evidence_sha256"] == hashlib.sha256(
        export_evidence_data
    ).hexdigest()
    exported = json.loads(export_evidence_data)
    series = exported["series"][0]
    source_tamper_package = package_copy(
        package_output, outside / "verify-source-resealed"
    )
    source_mapping = (
        source_tamper_package
        / "source"
        / "locks"
        / "lock-first-series-v2.yml"
    )
    source_mapping.write_bytes(source_mapping.read_bytes() + b" ")
    reseal_package(source_tamper_package)
    must_reject(
        lambda: dev_check.verify_package(source_tamper_package),
        "packaged source bytes differ",
    )

    bundle_tamper_package = package_copy(
        package_output, outside / "verify-bundle-resealed"
    )
    module_bundle = bundle_tamper_package / "bundles" / "modules" / "0000.bundle"
    module_bundle.write_bytes(module_bundle.read_bytes() + b"tampered")
    reseal_package(bundle_tamper_package)
    must_reject(
        lambda: dev_check.verify_package(bundle_tamper_package),
        "package Git bundle is invalid",
    )

    mbox = package_output / series["mbox"]
    assert series["sha256"] == hashlib.sha256(mbox.read_bytes()).hexdigest()
    assert load_log(log) == [
        {"authority": "west", "argv": package_plan["steps"][0]["argv"][1:]}
    ]

    neighbor = outside / "neighbor"
    neighbor.mkdir()
    neighbor_marker = neighbor / "preserve"
    neighbor_marker.write_text("neighbor-owned\n", encoding="utf-8")

    semantic_output = outside / "semantic-lie-package"
    semantic_receipt = outside / "semantic-lie-receipt.json"
    semantic_plan = dev_check.build_package_plan(
        repo,
        west,
        "homebrew",
        acceptance_path,
        semantic_output,
        semantic_receipt,
        "acceptance",
    )
    os.environ["DEV_CHECK_FAKE_MODE"] = "package-semantic-lie"
    semantic_result = dev_check.execute_package(semantic_plan)
    durable(semantic_receipt, semantic_result)
    assert semantic_result["state"] == "failed"
    assert semantic_result["returncode"] == 1
    assert "semantic identity differs" in semantic_result["error"]
    assert not semantic_output.exists()
    assert not Path(semantic_plan["inputs"]["staging_root"]).exists()
    assert neighbor_marker.read_text(encoding="utf-8") == "neighbor-owned\n"

    mutation_output = outside / "boundary-mutation-package"
    mutation_receipt = outside / "boundary-mutation-receipt.json"
    mutation_plan = dev_check.build_package_plan(
        repo,
        west,
        "homebrew",
        acceptance_path,
        mutation_output,
        mutation_receipt,
        "acceptance",
    )
    real_fsync_package_tree = dev_check._fsync_package_tree
    mutation_injected = [False]

    def mutate_child_after_fsync(package: Path) -> None:
        real_fsync_package_tree(package)
        if not mutation_injected[0]:
            mutation_injected[0] = True
            evidence_path = package / "evidence.json"
            evidence_path.write_bytes(evidence_path.read_bytes() + b" ")

    os.environ["DEV_CHECK_FAKE_MODE"] = "package-ok"
    dev_check._fsync_package_tree = mutate_child_after_fsync
    try:
        mutation_result = dev_check.execute_package(mutation_plan)
    finally:
        dev_check._fsync_package_tree = real_fsync_package_tree
    durable(mutation_receipt, mutation_result)
    assert mutation_injected == [True]
    assert mutation_result["state"] == "failed"
    assert mutation_result["returncode"] == 1
    assert "digest mismatch" in mutation_result["error"]
    assert not mutation_output.exists()
    assert not Path(mutation_plan["inputs"]["staging_root"]).exists()

    publish_failure_output = outside / "publish-failure-package"
    publish_failure_receipt = outside / "publish-failure-receipt.json"
    publish_failure_plan = dev_check.build_package_plan(
        repo,
        west,
        "homebrew",
        acceptance_path,
        publish_failure_output,
        publish_failure_receipt,
        "acceptance",
    )
    publish_failure_staging = Path(publish_failure_plan["inputs"]["staging"])
    publish_failure_marker = Path(publish_failure_plan["inputs"]["marker"])
    real_atomic_json = dev_check._atomic_json
    injected = [False]

    def fail_first_committed_package_write(
        path: Path,
        payload: dict[str, object],
        directory_fd: int,
        expected_identity: dict[str, int],
    ) -> dict[str, int]:
        if (
            not injected[0]
            and path == publish_failure_receipt
            and payload.get("operation") == "package"
            and payload.get("state") == "committed"
        ):
            injected[0] = True
            raise OSError("injected durable commit failure")
        return real_atomic_json(
            path, payload, directory_fd, expected_identity
        )

    os.environ["DEV_CHECK_FAKE_MODE"] = "package-ok"
    clear_log(log)
    dev_check._atomic_json = fail_first_committed_package_write
    try:
        publish_failure = dev_check.execute_package(publish_failure_plan)
    finally:
        dev_check._atomic_json = real_atomic_json
    assert injected == [True]
    durable(publish_failure_receipt, publish_failure)
    assert publish_failure["state"] == "failed"
    assert publish_failure["returncode"] == 1
    assert publish_failure["results"][0]["returncode"] == 0
    assert publish_failure["staging"]["state"] == "cleaned"
    assert publish_failure["ownership"]["state"] == "cleaned"
    assert not publish_failure_output.exists()
    assert not publish_failure_staging.exists()
    assert not publish_failure_marker.exists()
    assert neighbor_marker.read_text(encoding="utf-8") == "neighbor-owned\n"
    assert load_log(log) == [
        {
            "authority": "west",
            "argv": publish_failure_plan["steps"][0]["argv"][1:],
        }
    ]

    post_commit_output = outside / "post-commit-interrupt-package"
    post_commit_receipt = outside / "post-commit-interrupt-receipt.json"
    post_commit_plan = dev_check.build_package_plan(
        repo,
        west,
        "homebrew",
        acceptance_path,
        post_commit_output,
        post_commit_receipt,
        "acceptance",
    )
    post_commit_root = Path(post_commit_plan["inputs"]["staging_root"])
    post_commit_interrupt = [False]

    def interrupt_after_committed_package_write(
        path: Path,
        payload: dict[str, object],
        directory_fd: int,
        expected_identity: dict[str, int],
    ) -> dict[str, int]:
        identity = real_atomic_json(
            path, payload, directory_fd, expected_identity
        )
        if (
            not post_commit_interrupt[0]
            and path == post_commit_receipt
            and payload.get("operation") == "package"
            and payload.get("state") == "committed"
        ):
            post_commit_interrupt[0] = True
            signal.raise_signal(signal.SIGINT)
        return identity

    os.environ["DEV_CHECK_FAKE_MODE"] = "package-ok"
    dev_check._atomic_json = interrupt_after_committed_package_write
    try:
        post_commit_result = dev_check.execute_package(post_commit_plan)
    finally:
        dev_check._atomic_json = real_atomic_json
    assert post_commit_interrupt == [True]
    durable(post_commit_receipt, post_commit_result)
    assert post_commit_result["state"] == "committed"
    assert post_commit_result["returncode"] == 0
    assert post_commit_output.is_dir()
    assert not post_commit_root.exists()

    failed_output = outside / "failed-package"
    failed_package_receipt = outside / "failed-package-receipt.json"
    failure_package_plan = dev_check.build_package_plan(
        repo,
        west,
        "homebrew",
        acceptance_path,
        failed_output,
        failed_package_receipt,
        "acceptance",
    )
    failed_root = Path(failure_package_plan["inputs"]["staging_root"])
    failed_staging = Path(failure_package_plan["inputs"]["staging"])
    failed_marker = Path(failure_package_plan["inputs"]["marker"])
    foreign_staging_file = failed_staging / "foreign"
    real_run_process = dev_check._run_process
    staging_race_injected = [False]

    def inject_staging_after_export_failure(
        argv: list[str],
        cwd: Path,
        timeout_seconds: int,
        environment: dict[str, str],
        stdout_full_limit: int = 0,
    ) -> dict[str, object]:
        outcome = real_run_process(
            argv, cwd, timeout_seconds, environment, stdout_full_limit
        )
        if (
            not staging_race_injected[0]
            and argv == failure_package_plan["steps"][0]["argv"]
        ):
            staging_race_injected[0] = True
            (failed_staging / "partial").unlink()
            failed_staging.rmdir()
            failed_staging.mkdir()
            foreign_staging_file.write_text(
                "foreign staging survives\n", encoding="utf-8"
            )
        return outcome

    os.environ["DEV_CHECK_FAKE_MODE"] = "package-fail"
    clear_log(log)
    dev_check._run_process = inject_staging_after_export_failure
    try:
        failed_package = dev_check.execute_package(failure_package_plan)
    finally:
        dev_check._run_process = real_run_process
    durable(failed_package_receipt, failed_package)
    assert failed_package["state"] == "failed"
    assert failed_package["returncode"] == 41
    assert failed_package["results"][0]["returncode"] == 41
    assert not failed_output.exists() and not failed_output.is_symlink()
    assert staging_race_injected == [True]
    assert failed_root.is_dir() and failed_marker.is_file()
    assert foreign_staging_file.read_text(encoding="utf-8") == "foreign staging survives\n"
    assert neighbor_marker.read_text(encoding="utf-8") == "neighbor-owned\n"
    assert existing_marker.read_text(encoding="utf-8") == "preexisting\n"
    assert package_output.is_dir() and package_receipt_path.is_file()
    assert load_log(log) == [
        {
            "authority": "west",
            "argv": failure_package_plan["steps"][0]["argv"][1:],
        }
    ]

    swap_parent = outside / "swapped-output-parent"
    swap_parent.mkdir()
    saved_swap_parent = outside / "original-output-parent"
    swap_output = swap_parent / "package"
    swap_receipt = outside / "swapped-output-parent-receipt.json"
    swap_plan = dev_check.build_package_plan(
        repo,
        west,
        "homebrew",
        acceptance_path,
        swap_output,
        swap_receipt,
        "acceptance",
    )
    swap_staging = Path(swap_plan["inputs"]["staging"])
    swap_root_name = Path(swap_plan["inputs"]["staging_root"]).name
    foreign_parent_file = swap_parent / "foreign"
    parent_swap_injected = [False]

    def swap_output_parent_during_export(
        argv: list[str],
        cwd: Path,
        timeout_seconds: int,
        environment: dict[str, str],
        stdout_full_limit: int = 0,
    ) -> dict[str, object]:
        if (
            not parent_swap_injected[0]
            and argv == swap_plan["steps"][0]["argv"]
        ):
            parent_swap_injected[0] = True
            swap_parent.rename(saved_swap_parent)
            swap_parent.mkdir()
            foreign_parent_file.write_text("foreign parent survives\n", encoding="utf-8")
        return real_run_process(
            argv, cwd, timeout_seconds, environment, stdout_full_limit
        )

    os.environ["DEV_CHECK_FAKE_MODE"] = "package-ok"
    dev_check._run_process = swap_output_parent_during_export
    try:
        swapped_package = dev_check.execute_package(swap_plan)
    finally:
        dev_check._run_process = real_run_process
    durable(swap_receipt, swapped_package)
    assert parent_swap_injected == [True]
    assert swapped_package["state"] == "failed"
    assert swapped_package["returncode"] == 1
    assert not swap_output.exists()
    assert foreign_parent_file.read_text(encoding="utf-8") == "foreign parent survives\n"
    assert swap_staging.is_dir()
    assert not (saved_swap_parent / swap_root_name).exists()

    evidence_parent = outside / "swapped-evidence-parent"
    evidence_parent.mkdir()
    saved_evidence_parent = outside / "original-evidence-parent"
    swapped_evidence = evidence_parent / "check.json"
    evidence_parent_plan = dev_check.build_check_plan(
        repo,
        west,
        "quick",
        "homebrew",
        None,
        None,
        swapped_evidence,
        None,
        None,
    )
    evidence_parent_swap = [False]
    foreign_evidence_after_swap = b"foreign replacement evidence\n"

    def swap_evidence_parent_after_step(
        argv: list[str],
        cwd: Path,
        timeout_seconds: int,
        environment: dict[str, str],
        stdout_full_limit: int = 0,
    ) -> dict[str, object]:
        outcome = real_run_process(
            argv, cwd, timeout_seconds, environment, stdout_full_limit
        )
        if (
            not evidence_parent_swap[0]
            and argv == evidence_parent_plan["steps"][0]["argv"]
        ):
            evidence_parent_swap[0] = True
            evidence_parent.rename(saved_evidence_parent)
            evidence_parent.mkdir()
            swapped_evidence.write_bytes(foreign_evidence_after_swap)
        return outcome

    os.environ["DEV_CHECK_FAKE_MODE"] = "pass"
    dev_check._run_process = swap_evidence_parent_after_step
    try:
        must_reject(
            lambda: dev_check.execute_check(evidence_parent_plan),
            "evidence parent changed",
        )
    finally:
        dev_check._run_process = real_run_process
    assert evidence_parent_swap == [True]
    assert swapped_evidence.read_bytes() == foreign_evidence_after_swap
    pinned_evidence = saved_evidence_parent / swapped_evidence.name
    assert json.loads(pinned_evidence.read_text(encoding="utf-8"))["state"] != "committed"

    swapped_inode_evidence = outside / "swapped-evidence-inode.json"
    evidence_inode_plan = dev_check.build_check_plan(
        repo,
        west,
        "quick",
        "homebrew",
        None,
        None,
        swapped_inode_evidence,
        None,
        None,
    )
    evidence_inode_swap = [False]
    foreign_evidence_inode = b"foreign evidence inode survives\n"

    def swap_evidence_inode_after_step(
        argv: list[str],
        cwd: Path,
        timeout_seconds: int,
        environment: dict[str, str],
        stdout_full_limit: int = 0,
    ) -> dict[str, object]:
        outcome = real_run_process(
            argv, cwd, timeout_seconds, environment, stdout_full_limit
        )
        if (
            not evidence_inode_swap[0]
            and argv == evidence_inode_plan["steps"][0]["argv"]
        ):
            evidence_inode_swap[0] = True
            swapped_inode_evidence.unlink()
            swapped_inode_evidence.write_bytes(foreign_evidence_inode)
        return outcome

    dev_check._run_process = swap_evidence_inode_after_step
    try:
        must_reject(
            lambda: dev_check.execute_check(evidence_inode_plan),
            "evidence identity changed",
        )
    finally:
        dev_check._run_process = real_run_process
    assert evidence_inode_swap == [True]
    assert swapped_inode_evidence.read_bytes() == foreign_evidence_inode

    assert git(repo, "status", "--porcelain") == ""
    assert all(
        not path.is_relative_to(repo)
        for path in (
            quick_evidence,
            check_receipt_path,
            acceptance_path,
            failure_path,
            interrupt_path,
            package_output,
            package_receipt_path,
            publish_failure_output,
            publish_failure_receipt,
            failed_package_receipt,
        )
    )

print("west dev check contract: PASS")

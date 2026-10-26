#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 -B - "$repo" <<'PY'
"""Drive real tier control flow with inert external-command boundaries."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix="ci-tier-contract-") as raw:
    state = Path(raw)
    repo = state / "repo"
    (repo / "ci").mkdir(parents=True)
    (repo / "scripts").mkdir()
    for name in ("run-test-tier.sh", "rootless-prefix.sh", "cleanup-rootless-prefixes.sh", "run-rootless-bootstrap-diagnostic.sh"):
        shutil.copy2(source / "ci" / name, repo / "ci" / name)
    bin_dir = state / "bin"
    bin_dir.mkdir()
    log = state / "commands.jsonl"
    stub = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
name = {"run-host-tier.py": "host-runner", "collect-rootless-diagnostics.sh": "collector", "west-job.sh": "job"}.get(name, name)
args = sys.argv[1:]
with open(os.environ["CI_CONTRACT_LOG"], "a") as out:
    out.write(json.dumps({"name": name, "args": args, "no_clt": os.environ.get("WEST_TEST_FORBID_GUEST_TOOLCHAIN")}) + "\\n")
def option(key):
    return args[args.index(key) + 1] if key in args else None
if name == "west":
    if args[:2] == ["list", "darling"]:
        print(os.environ["CI_SOURCE_ROOT"])
    elif args[:1] == ["darling-bootstrap"]:
        assert option("--runtime-root") == os.environ["DARLING_RUNTIME_ROOT"]
        assert option("--build-dir") == os.environ["DARLING_BUILD_DIR"]
        assert option("--prefix")
        if os.environ.get("CI_BOOTSTRAP_RC"):
            sys.exit(int(os.environ["CI_BOOTSTRAP_RC"]))
        if "--guest-toolchain" in args:
            assert os.environ.get("WEST_TEST_FORBID_GUEST_TOOLCHAIN") != "1"
            clt = Path(option("--prefix")) / "Library/Developer/CommandLineTools"
            (clt / "usr/bin").mkdir(parents=True)
            (clt / "SDKs/MacOSX.sdk").mkdir(parents=True)
            clang = clt / "usr/bin/clang"
            clang.touch()
            clang.chmod(0o755)
    elif args[:1] == ["test"] and "--prefix" in args:
        assert option("--runtime-root") == os.environ["DARLING_RUNTIME_ROOT"]
        if "--cleanup-prefix" in args:
            sys.exit(int(os.environ.get("CI_CLEANUP_RC", "0")))
        assert "--workload" in args or "--guest-macho-validation-group" in args
        if os.environ.get("CI_WORKLOAD_RC"):
            sys.exit(int(os.environ["CI_WORKLOAD_RC"]))
        if os.environ.get("WEST_TEST_FORBID_GUEST_TOOLCHAIN") == "1":
            assert "--guest-toolchain" not in args
elif name == "collector":
    assert Path(args[1]).is_dir()
    Path(args[0]).mkdir(parents=True, exist_ok=True)
elif name == "strace":
    # The observer must preserve the real bootstrap command and its status.
    index = args.index("west")
    sys.exit(__import__("subprocess").call(args[index:]))
'''
    for name in ("west", "cmake", "ctest", "strace", "host-runner", "collector", "job"):
        path = bin_dir / name
        path.write_text(stub)
        path.chmod(0o755)
    for target, tool in ((repo / "ci/run-host-tier.py", "host-runner"), (repo / "ci/collect-rootless-diagnostics.sh", "collector"), (repo / "scripts/west-job.sh", "job")):
        target.symlink_to(bin_dir / tool)
    runtime = state / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "libexec/darling").mkdir(parents=True)
    for name in ("darling", "darlingserver"):
        path = runtime / "bin" / name
        path.touch()
        path.chmod(0o755)
    env = os.environ.copy()
    for key in ("WEST_TEST_FORBID_GUEST_TOOLCHAIN", "DARLING_SMOKE_PREFIX", "DARLING_REGRESSION_PREFIX", "DARLING_CORPUS_PREFIX", "DARLING_TOOLCHAIN_PREFIX", "DARLING_DIAGNOSTIC_PREFIX", "GITHUB_OUTPUT"):
        env.pop(key, None)
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", DW_STATE_ROOT=str(state / "owned"), DARLING_TIER_DEFER_GLOBAL_CLEANUP="0", DARLING_RUNTIME_ROOT=str(runtime), DARLING_BUILD_DIR=str(state / "build"), CI_SOURCE_ROOT=str(state / "product"), CI_CONTRACT_LOG=str(log))

    def invoke(tier, *args, expected=0, **overrides):
        log.write_text("")
        child = env | overrides
        result = subprocess.run([str(repo / "ci/run-test-tier.sh"), tier, *args], env=child, capture_output=True, text=True)
        assert result.returncode == expected, (tier, result.returncode, result.stdout, result.stderr)
        return [json.loads(line) for line in log.read_text().splitlines()]

    def west_calls(records, command):
        return [row for row in records if row["name"] == "west" and row["args"][:1] == [command]]

    def prefix_of(row):
        args = row["args"]
        return Path(args[args.index("--prefix") + 1])

    def guest_calls(records):
        return [row for row in west_calls(records, "test") if "--workload" in row["args"] or "--guest-macho-validation-group" in row["args"]]

    # No installed-runtime requirement for host/native entrypoints.
    rows = invoke("host", "--jobs", "2", DARLING_RUNTIME_ROOT="")
    assert rows == [{"name": "host-runner", "args": ["--jobs", "2"], "no_clt": None}]
    rows = invoke("guest-smoke")
    boot = west_calls(rows, "darling-bootstrap")
    assert len(boot) == 1 and "--no-build" in boot[0]["args"]
    guests = guest_calls(rows)
    assert [row["args"][row["args"].index("--workload") + 1] for row in guests] == ["rootless_prefix_initialization_guest", "rootless_prebuilt_macho_regression"]
    assert all(row["no_clt"] == "1" for row in boot + guests)
    assert len({str(prefix_of(row)) for row in boot + guests}) == 1
    assert not prefix_of(boot[0]).exists()
    assert not Path(str(prefix_of(boot[0])) + ".west-tier-owner").exists()

    for tier in ("guest-full", "guest-macho-validation"):
        rows = invoke(tier)
        guests = guest_calls(rows)
        assert len(guests) == 1
        args = guests[0]["args"]
        assert args[args.index("--guest-macho-validation-group") + 1] == "homebrew"
        assert "--guest-macho-evidence-dir" in args and "--workload" not in args
        assert all(row["no_clt"] == "1" for row in west_calls(rows, "darling-bootstrap") + guests)
        assert not prefix_of(guests[0]).exists()
        invoke(tier, "unexpected-selector", expected=2)

    rows = invoke("guest-toolchain")
    boot = west_calls(rows, "darling-bootstrap")[0]
    assert boot["args"][boot["args"].index("--guest-toolchain") + 1] == "darling-command-line-tools"
    guests = guest_calls(rows)
    assert len(guests) == 1 and "rootless_guest_toolchain_compile_execute" in guests[0]["args"]
    assert all(row["no_clt"] is None for row in [boot, *guests])
    evidence_index = next(i for i, row in enumerate(rows) if row["name"] == "collector")
    cleanup_index = next(i for i, row in enumerate(rows) if "--cleanup-prefix" in row["args"])
    assert evidence_index < cleanup_index
    assert not prefix_of(boot).exists()

    # A fresh product install is configured stock and built/installed by bootstrap.
    fresh = state / "new-runtime"
    rows = invoke("guest-smoke", DARLING_RUNTIME_ROOT=str(fresh))
    configure = next(row for row in rows if row["name"] == "cmake")
    assert "-DCOMPONENTS=stock" in configure["args"]
    assert f"-DCMAKE_INSTALL_PREFIX={fresh}" in configure["args"]
    assert "--no-build" not in west_calls(rows, "darling-bootstrap")[0]["args"]
    assert "-DCMAKE_BUILD_TYPE=Release" in configure["args"]
    assert "-DENABLE_TESTS=ON" not in configure["args"]
    rows = invoke("guest-full", DARLING_RUNTIME_ROOT=str(state / "regression-runtime"))
    configure = next(row for row in rows if row["name"] == "cmake")
    assert "-DENABLE_TESTS=ON" not in configure["args"]
    assert "--with-test-data" in west_calls(rows, "darling-bootstrap")[0]["args"]

    # Failures retain the owned prefix/evidence and preserve the original status.
    rows = invoke("guest-smoke", expected=17, CI_WORKLOAD_RC="17")
    failed_prefix = prefix_of(west_calls(rows, "darling-bootstrap")[0])
    assert failed_prefix.is_dir()
    assert any("--cleanup-prefix" in row["args"] for row in rows)
    rows = invoke("guest-smoke", expected=1, CI_CLEANUP_RC="9")
    assert prefix_of(west_calls(rows, "darling-bootstrap")[0]).is_dir()
    rows = invoke("guest-smoke", expected=23, CI_BOOTSTRAP_RC="23")
    assert not guest_calls(rows)
    assert prefix_of(west_calls(rows, "darling-bootstrap")[0]).is_dir()

    rows = invoke("guest-smoke", DARLING_TIER_DEFER_GLOBAL_CLEANUP="1")
    assert not any("--gc" in row["args"] or row["name"] == "job" for row in rows)
    rows = invoke("acceptance-cleanup")
    assert any("--gc-runtime-evidence" in row["args"] for row in rows)
    assert any(row["name"] == "job" for row in rows)
    invoke("acceptance-cleanup", expected=2, DARLING_TIER_DEFER_GLOBAL_CLEANUP="invalid")
    rows = invoke("guest-smoke", expected=1, DARLING_RUNTIME_ROOT="")
    assert not rows
    rows = invoke("guest-smoke", expected=2, DARLING_RUNTIME_ROOT="relative")
    assert not rows

    # Failure diagnostics observe the selected installation, never construct another.
    log.write_text("")
    result = subprocess.run([str(repo / "ci/run-rootless-bootstrap-diagnostic.sh"), str(state / "trace")], env=env | {"CI_BOOTSTRAP_RC": "27"}, capture_output=True, text=True)
    assert result.returncode == 27, result.stderr
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(row["name"] == "strace" for row in rows)
    assert "--no-build" in west_calls(rows, "darling-bootstrap")[0]["args"]
    assert any("--cleanup-prefix" in row["args"] for row in rows)

    rows = invoke("macos", DARLING_TESTKIT_BUILD=str(state / "native-build"))
    assert [row["name"] for row in rows] == ["cmake", "cmake", "ctest"]
    assert rows[-1]["args"][-2:] == ["-L", "env:macos"]
    rows = invoke("macos-package", str(state / "oracle"))
    assert rows[-1]["name"] == "cmake" and rows[-1]["args"][:1] == ["--install"]
print("PASS ci-test-tiers-contract")
PY

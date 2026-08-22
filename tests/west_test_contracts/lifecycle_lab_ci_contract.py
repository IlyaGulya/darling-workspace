#!/usr/bin/env python3
"""Behavioral contract for the resource-only Lifecycle Lab runner and CI routing."""

from __future__ import annotations

import json
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
owned = Path(sys.argv[2]).resolve()
runner = repo / "ci/lifecycle_lab.py"

if os.environ.get("LIFECYCLE_LAB_WRAPPER_PROBE") == "1":
    print("LIFECYCLE_LAB_WRAPPER_NO_MISE_VALID")
    raise SystemExit(0)


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def run(name: str, command: list[str], expected: int, *, result: bool = True, extra: list[str] | None = None) -> tuple[Path | None, dict | None, subprocess.CompletedProcess[str]]:
    result_path = owned / f"{name}.json" if result else None
    argv = [sys.executable, "-B", str(runner), "--timeout-seconds", "1", "--rss-mib", "256", "--output-bytes", "65536", "--cleanup-seconds", "2"]
    if result_path:
        argv += ["--result", str(result_path)]
    if extra:
        argv += extra
    completed = subprocess.run([*argv, "--", *command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
    require(completed.returncode == expected, f"{name}: rc={completed.returncode}: {completed.stderr}")
    value = json.loads(result_path.read_text()) if result_path else None
    return result_path, value, completed


_, passed, output = run("pass", ["/bin/sh", "-c", "printf 'REAL_PASS\\n'"], 0)
require(passed is not None and passed["status"] == "PASS" and passed["exit_code"] == 0, "pass result invalid")
require("REAL_PASS" in output.stdout and "LIFECYCLE_LAB_RESULT status=PASS" in output.stdout, "stable pass marker missing")

failure_dir = owned / "failure-artifacts"
_, failed, _ = run(
    "failure", ["/bin/sh", "-c", "printf 'REAL_FAILURE\\n'; exit 7"], 7,
    extra=["--failure-artifacts", str(failure_dir)],
)
require(failed is not None and failed["status"] == "FAIL" and failed["exit_code"] == 7, "exit code not preserved")
require((failure_dir / "command.log").read_text() == "REAL_FAILURE\n", "failure log not retained")

_, unsupported, _ = run("unsupported", ["/bin/sh", "-c", "printf 'UNSUPPORTED\\n'; exit 2"], 2)
require(unsupported is not None and unsupported["status"] == "FAIL" and unsupported["exit_code"] == 2, "unsupported became pass")

success_artifacts = owned / "success-artifacts"
_, _, _ = run(
    "success-no-artifacts",
    ["/bin/sh", "-c", "mkdir -p \"$LIFECYCLE_LAB_ARTIFACT_STAGING\"; printf corpus >\"$LIFECYCLE_LAB_ARTIFACT_STAGING/seed\""],
    0,
    extra=["--failure-artifacts", str(success_artifacts)],
)
require(not success_artifacts.exists(), "success retained artifacts")

_, timeout, _ = run("timeout", ["/bin/sh", "-c", "sleep 5"], 124)
require(timeout is not None and timeout["reason"] == "timeout", "timeout not typed")

_, bounded, _ = run("output", [sys.executable, "-c", "print('X'*70000)"], 124)
require(bounded is not None and bounded["reason"] == "output", "output ceiling not enforced")

rss_result = owned / "rss.json"
completed = subprocess.run(
    [sys.executable, "-B", str(runner), "--timeout-seconds", "5", "--rss-mib", "1", "--result", str(rss_result), "--", sys.executable, "-c", "import time; x=bytearray(32*1024*1024); time.sleep(3)"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
)
require(completed.returncode == 124 and json.loads(rss_result.read_text())["reason"] == "rss", "RSS ceiling not enforced")

_, socket_tail, _ = run(
    "socket-tail",
    [sys.executable, "-c", "import os,socket; s=socket.socket(socket.AF_UNIX); s.bind(os.environ['LIFECYCLE_LAB_TASK_ROOT']+'/tail.sock')"],
    125,
)
require(socket_tail is not None and socket_tail["reason"] == "cleanup", "socket cleanup failure hidden")

escaped_pid = owned / "escaped.pid"
escape_code = (
    "import subprocess,sys; "
    "p=subprocess.Popen(['sleep','30'],start_new_session=True,env={},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
    "open(sys.argv[1],'w').write(str(p.pid))"
)
_, escaped, _ = run("setsid-empty-env", [sys.executable, "-c", escape_code, str(escaped_pid)], 0)
pid = int(escaped_pid.read_text())
deadline = time.monotonic() + 2
while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
require(not Path(f"/proc/{pid}").exists(), "setsid/empty-env descendant escaped cleanup")
require(escaped["cleanup"]["ledger_empty"] is True, "kernel process ledger was not proven empty")

thread_exec_pid = owned / "thread-exec.pid"
thread_exec_code = (
    "import os,sys,threading; "
    "t=threading.Thread(target=lambda:None); t.start(); t.join(); "
    "open(sys.argv[1],'w').write(str(os.getpid())); "
    "os.setsid(); os.execve('/bin/sleep',['sleep','30'],{})"
)
_, thread_exec, _ = run(
    "thread-exit-setsid-exec-empty-env",
    [sys.executable, "-c", thread_exec_code, str(thread_exec_pid)],
    124,
)
thread_pid = int(thread_exec_pid.read_text())
require(not Path(f"/proc/{thread_pid}").exists(), "thread exit retired TGID before setsid/exec cleanup")
require(thread_exec["cleanup"]["ledger_empty"] is True, "thread-exit process authority was not drained")

# Native ordered-fork regression: the intermediate is reaped before the
# controller can rely on opening its pidfd, but its queued child FORK must
# still inherit root ownership. The bounded churn also exercises numeric PID
# reuse without weakening the pidfd-only signaling boundary.
lineage_source = owned / "lineage-churn.c"
lineage_binary = owned / "lineage-churn"
lineage_pid = owned / "lineage-leaf.pid"
lineage_source.write_text(textwrap.dedent(r"""
    #include <errno.h>
    #include <fcntl.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <sys/types.h>
    #include <sys/wait.h>
    #include <unistd.h>

    static void reap(pid_t pid) {
        int status;
        while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    }

    int main(int argc, char **argv) {
        if (argc != 2) return 2;
        for (int i = 0; i < 20000; ++i) {
            pid_t pid = fork();
            if (pid < 0) return 3;
            if (pid == 0) _exit(0);
            reap(pid);
        }
        pid_t intermediate = fork();
        if (intermediate < 0) return 4;
        if (intermediate == 0) {
            pid_t leaf = fork();
            if (leaf < 0) _exit(5);
            if (leaf == 0) {
                if (setsid() < 0) _exit(6);
                char *const leaf_argv[] = {"sleep", "30", NULL};
                char *const empty_env[] = {NULL};
                execve("/bin/sleep", leaf_argv, empty_env);
                _exit(7);
            }
            int fd = open(argv[1], O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
            if (fd < 0) _exit(8);
            dprintf(fd, "%ld\n", (long)leaf);
            fsync(fd);
            close(fd);
            _exit(0);
        }
        reap(intermediate);
        return 0;
    }
"""), encoding="utf-8")
subprocess.run(["cc", "-std=c11", "-D_GNU_SOURCE", "-O2", "-Wall", "-Wextra", "-Werror", str(lineage_source), "-o", str(lineage_binary)], check=True)
lineage_result = owned / "lineage-churn.json"
lineage_run = subprocess.run(
    [sys.executable, "-B", str(runner), "--timeout-seconds", "45", "--cleanup-seconds", "6",
     "--rss-mib", "256", "--output-bytes", "65536", "--result", str(lineage_result),
     "--", str(lineage_binary), str(lineage_pid)],
    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
)
require(lineage_run.returncode == 0, f"native lineage/churn failed: rc={lineage_run.returncode}: {lineage_run.stderr}")
leaf_pid = int(lineage_pid.read_text())
require(not Path(f"/proc/{leaf_pid}").exists(), "reaped intermediate lost setsid/exec leaf ownership")
lineage_report = json.loads(lineage_result.read_text())
require(lineage_report["cleanup"]["ledger_empty"] is True, "20k churn lineage ledger was not drained")

# A reused numeric PID must never redirect a signal. Exercise the exact
# pre-pidfd-send identity guard with a deliberately stale starttime.
spec = importlib.util.spec_from_file_location("lifecycle_lab", runner)
require(spec is not None and spec.loader is not None, "runner module unavailable")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
authority = module.ProcessOwnership.__new__(module.ProcessOwnership)
authority.lost = False
current_pidfd = os.pidfd_open(os.getpid(), 0)
authority.identities = {os.getpid(): module.OwnedProcess(module.process_identity(os.getpid()) + 1, current_pidfd)}
authority.lock = __import__("threading").Lock()
pidfd_send_called = False
original_pidfd_send = module.signal.pidfd_send_signal
def forbidden_pidfd_send(*_args: object) -> None:
    global pidfd_send_called
    pidfd_send_called = True
module.signal.pidfd_send_signal = forbidden_pidfd_send
authority._signal_process(os.getpid(), authority.identities[os.getpid()], 0)
module.signal.pidfd_send_signal = original_pidfd_send
require(authority.lost and not pidfd_send_called, "PID reuse identity mismatch redirected a signal")
os.close(current_pidfd)

# A numeric TGID reused after its ordered EXIT receives a new generation; a
# stale lineage generation cannot become signal authority.
authority.lineage = {}
authority.next_generation = 1
first = authority._begin_lineage(424242)
authority.lineage.pop(424242)
second = authority._begin_lineage(424242)
require(second.generation > first.generation, "PID reuse retained stale lineage generation")

no_result = owned / "must-not-exist.json"
_, _, _ = run("optional-result", ["/bin/true"], 0, result=False)
require(not no_result.exists(), "result became mandatory")

workflow = (repo / ".github/workflows/lifecycle-lab.yml").read_text()
wrapper = repo / "tests/run-lifecycle-lab-ci-contract.sh"
probe_environment = os.environ.copy()
probe_environment.update({"PATH": "/usr/bin:/bin", "LIFECYCLE_LAB_WRAPPER_PROBE": "1"})
wrapper_probe = subprocess.run(
    [str(wrapper)], env=probe_environment, text=True, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, timeout=10,
)
require(wrapper_probe.returncode == 0, f"wrapper requires mise: {wrapper_probe.stderr}")
require("LIFECYCLE_LAB_WRAPPER_NO_MISE_VALID" in wrapper_probe.stdout, "direct Python wrapper probe missing")
require(not list(owned.glob("dlc.*")), "direct wrapper bypassed task-root cleanup")
for command in (
    "tests/run-lifecycle-operation-boundary-contract.sh",
    "tests/run-lifecycle-trace-contract.sh",
    "tests/run-lifecycle-explorer-contract.sh",
    "tests/run-lifecycle-fuzz-contract.sh",
    "ci/run-lifecycle-fuzz-campaign.sh asan",
    "ci/run-lifecycle-fuzz-campaign.sh tsan",
    "tests/run-lifecycle-fuzz-ub-gate.sh",
    "ci/run-lifecycle-fuzz-campaign.sh fuzz",
    "tests/run-lifecycle-real-kernel-contract.sh",
    "tests/run-lifecycle-guest-ready-contract.sh",
):
    require(command in workflow, f"CI does not directly own {command}")
require(workflow.count("if: failure()") == 3, "artifacts are not failure-only")
mise_pin = "jdx/mise-action@5228313ee0372e111a38da051671ca30fc5a96db"
require(workflow.count(mise_pin) == 2, "mise-action is not pinned at both call sites")
mise_refs = re.findall(r"^\s*- uses: (jdx/mise-action@\S+)\s*$", workflow, re.MULTILINE)
require(mise_refs == [mise_pin, mise_pin], "mutable or malformed mise-action ref accepted")
fetch = "cargo fetch --locked --manifest-path lifecycle/operation-boundary/Cargo.toml"
for job, first_consumer in (
    ("deterministic:", "ci/run-lifecycle-lab.sh $LAB_ARGS"),
    ("scheduled:", "ci/run-lifecycle-lab.sh $LAB_ARGS"),
    ("landing:", "tests/run-lifecycle-real-kernel-contract.sh"),
):
    start = workflow.index(f"  {job}")
    following = [workflow.find(f"  {name}:", start + 1) for name in ("deterministic", "scheduled", "landing")]
    end_candidates = [position for position in following if position > start]
    section = workflow[start:min(end_candidates) if end_candidates else len(workflow)]
    require(section.count(fetch) == 1, f"{job} lacks one locked Cargo fetch")
    require(section.index(fetch) < section.index(first_consumer), f"{job} fetch follows offline consumer")
require("if: always()" not in workflow and "artifact-manifest" not in workflow, "ordinary artifact package survived")
require(not (repo / "lifecycle/lab-ci-v1.json").exists(), "command policy survived simplification")
require(not (repo / "schemas/lifecycle-lab-result-v1.schema.json").exists(), "result schema survived simplification")
require(not (repo / "schemas/lifecycle-lab-artifact-v1.schema.json").exists(), "artifact schema survived simplification")

cargo = shutil.which("cargo")
require(cargo is not None, "cargo unavailable for fresh-home contract")
fresh_environment = os.environ.copy()
fresh_environment.update({
    "CARGO_HOME": str(owned / "empty-cargo-home"),
    "CARGO_TARGET_DIR": str(owned / "cargo-target"),
})
manifest = repo / "lifecycle/operation-boundary/Cargo.toml"
fetched = subprocess.run(
    [cargo, "fetch", "--locked", "--manifest-path", str(manifest)],
    cwd=repo, env=fresh_environment, text=True, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, timeout=120,
)
require(fetched.returncode == 0, f"fresh Cargo fetch failed: {fetched.stderr}")
offline = subprocess.run(
    [str(repo / "tests/run-lifecycle-operation-boundary-contract.sh")],
    cwd=repo, env=fresh_environment, text=True, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, timeout=180,
)
require(offline.returncode == 0, f"fresh fetch -> offline boundary failed:\n{offline.stdout}\n{offline.stderr}")

print("LIFECYCLE_LAB_CI_CONTRACT_VALID negatives=14 cleanup_escape=PASS pidfd_identity=PASS lineage_churn=20000 cargo_bootstrap=PASS")

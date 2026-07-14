"""Rootless prefix bootstrap orchestration and diagnostics.

The West command facade delegates this layer so prefix provisioning remains a
domain workflow rather than a collection of command-line branches.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from guest_toolchain import (
    COMMAND_LINE_TOOLS_RESOURCE,
    GuestToolchainError,
    require_guest_toolchain_provisioning_allowed,
)
from test_execution import process_output_text, run_bounded
from test_guest_execution import run_guest_argv, run_guest_shell
from test_runtime_identity import runtime_identity


_BOOTSTRAP_FATAL_SIGNAL = re.compile(
    r"--- (?P<signal>SIG(?:SEGV|BUS|ILL|ABRT)) \{(?P<details>[^}]*)\} ---"
)
RETAINED_RUNTIME_PROFILE_MARKER = ".west-runtime-profile.json"


def bootstrap_trace_fatal_signal(trace_dir: Path) -> str | None:
    """Return a guest fault recorded by an opt-in bootstrap strace, if any."""

    for trace in sorted(trace_dir.glob("bootstrap*")):
        if not trace.is_file():
            continue
        match = _BOOTSTRAP_FATAL_SIGNAL.search(trace.read_text(errors="replace"))
        if match is None:
            continue
        fault = re.search(r"si_addr=([^, }]+)", match.group("details"))
        location = f" at {fault.group(1)}" if fault is not None else ""
        return f"{match.group('signal')}{location}"
    return None


def bootstrap_syscall_stall_summary(trace_dir: Path) -> str | None:
    """Summarize observed terminal syscall states without inferring a lost RPC.

    ``MSG_DONTWAIT`` naturally produces bursts of ``EAGAIN`` between a guest
    request and its reply.  A trace tail alone cannot distinguish that normal
    polling from a dropped reply, so report the ordering of the last request
    and delivered reply instead of calling either case a spin.
    """

    states = []
    for trace_path in sorted(trace_dir.glob("bootstrap.*")):
        if not trace_path.is_file():
            continue
        try:
            content = trace_path.read_text(errors="replace")
        except OSError:
            continue
        exec_match = re.search(r'execve\("(?P<program>[^"]+)', content)
        if exec_match is None:
            continue
        program = Path(exec_match.group("program")).name
        lines = content.splitlines()
        if lines and lines[-1].startswith("+++ exited with "):
            continue
        tail = lines[-128:]
        has_empty_rpc_receive = any(
            "recvmsg(" in line and "MSG_DONTWAIT" in line and "EAGAIN" in line
            for line in tail
        )
        last_rpc_send = max(
            (index for index, line in enumerate(lines) if "sendmsg(" in line),
            default=-1,
        )
        last_delivered_reply = max(
            (
                index
                for index, line in enumerate(lines)
                if "recvmsg(" in line
                and "MSG_DONTWAIT" in line
                and "EAGAIN" not in line
                and " = " in line
            ),
            default=-1,
        )
        if has_empty_rpc_receive:
            if last_rpc_send > last_delivered_reply:
                state = "awaiting reply to most recent RPC"
            elif last_delivered_reply >= 0:
                state = "polling after a delivered RPC reply"
            else:
                state = "polling empty RPC receive without a request"
        elif any("sched_yield(" in line for line in tail):
            state = "spinning while waiting for a thread checkin"
        elif any("epoll_wait(" in line for line in tail):
            state = "waiting for an epoll event"
        elif any("poll(" in line and ", -1" in line for line in tail):
            state = "waiting for a socket event"
        else:
            continue
        pid = trace_path.name.removeprefix("bootstrap.")
        states.append(f"{program}[{pid}]: {state}")
    return " | ".join(states[:8]) if states else None


class RuntimeProfileDeployment:
    """One materialized runtime provider currently deployed under a prefix."""

    def __init__(
        self,
        *,
        name: str,
        prefix: Path,
        build_root: Path,
        env: dict[str, str],
        diagnostic_trace_paths: tuple[Path, ...] = (),
    ):
        self.name = name
        self.prefix = prefix
        self.build_root = build_root
        self.env = env
        self.diagnostic_trace_paths = diagnostic_trace_paths


class RuntimeProviderFailure(RuntimeError):
    """A declared runtime provider failed before the guest test could run."""

    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind


class BootstrapRuntimeProfileMixin:
    def _bootstrap_runtime_profile(
        self, profile_name: str, *, executable: str | None = None
    ) -> None:
        """Retain one declared runtime provider only after a real guest smoke."""

        prefix_text = getattr(self, "_prefix", None)
        if not prefix_text:
            self.die(
                "--bootstrap-runtime-profile requires --prefix, --prefix-profile, or DPREFIX "
                "(for example: --prefix-profile homebrew)"
            )
        if not profile_name:
            self.die("--bootstrap-runtime-profile needs a runtime provider name")
        definition = self._ctest_runtime_profile_definitions().get(profile_name)
        if definition is None:
            self.die(f"unknown prefix baseline runtime profile: {profile_name}")
        if definition.get("purpose") not in {
            "prefix-baseline",
            "guest-toolchain-provisioning",
        }:
            self.die(
                f"runtime profile {profile_name} is not a bootstrap-capable provider; "
                "bootstrap only accepts minimal or guest-toolchain provisioning profiles"
            )
        smoke_timeout_seconds = (
            getattr(self, "_bootstrap_timeout_seconds", None)
            or definition["bootstrap-smoke-timeout-seconds"]
        )
        trace_dir = getattr(self, "_bootstrap_syscall_trace", None)
        stack_sample_dir = getattr(self, "_bootstrap_stack_sample", None)
        command_prefix: tuple[str, ...] = ()
        if trace_dir is not None:
            if shutil.which("strace") is None:
                self.die("--bootstrap-syscall-trace requires strace on the host")
            trace_dir = self._resolve_bootstrap_diagnostic_dir(trace_dir)
            trace_dir.mkdir(parents=True, exist_ok=True)
            command_prefix = (
                "strace",
                "-D",
                "-ff",
                "-i",
                "-tt",
                "-v",
                "-s",
                "160",
                "-o",
                str(trace_dir / "bootstrap"),
            )
            self.inf(f"prefix bootstrap syscall trace: {trace_dir}")
        elif stack_sample_dir is not None:
            command_prefix = self._bootstrap_stack_sample_command(
                stack_sample_dir,
                sample_name="bootstrap",
                label="prefix bootstrap",
            )
        with self._prefix_resource_context(True):
            with self._runtime_profile_deployment_context(
                [profile_name],
                label_prefix="Prefix bootstrap",
                retain_deployment=True,
                provision_guest_toolchain=False,
            ) as deployment:
                target = executable or "login shell"
                self.inf(
                    f"prefix bootstrap phase start: guest {target} "
                    f"(timeout {smoke_timeout_seconds}s)"
                )
                if executable is None:
                    result = run_guest_shell(
                        deployment.env["DARLING_LAUNCHER"],
                        prefix_text,
                        "set -eu\nprintf '%s\\n' WEST_PREFIX_BOOTSTRAP_OK",
                        cwd=Path(self.topdir),
                        env=deployment.env,
                        timeout_seconds=smoke_timeout_seconds,
                        capture_output=True,
                        command_prefix=command_prefix,
                        heartbeat_seconds=30,
                        heartbeat=lambda elapsed: self._emit_bootstrap_heartbeat(
                            deployment.prefix, target, elapsed
                        ),
                        output_line=lambda stream, line: self.inf(
                            f"prefix bootstrap guest {stream}: {line}"
                        ),
                    )
                else:
                    result = run_guest_argv(
                        deployment.env["DARLING_LAUNCHER"],
                        prefix_text,
                        (executable,),
                        cwd=Path(self.topdir),
                        env=deployment.env,
                        timeout_seconds=smoke_timeout_seconds,
                        capture_output=True,
                        command_prefix=command_prefix,
                        heartbeat_seconds=30,
                        heartbeat=lambda elapsed: self._emit_bootstrap_heartbeat(
                            deployment.prefix, target, elapsed
                        ),
                        output_line=lambda stream, line: self.inf(
                            f"prefix bootstrap guest {stream}: {line}"
                        ),
                    )
                if stack_sample_dir is not None:
                    self._render_bootstrap_stack_sample(
                        stack_sample_dir,
                        sample_name="bootstrap",
                        label="prefix bootstrap",
                    )
                diagnostic_dir = trace_dir or stack_sample_dir
                if diagnostic_dir is not None:
                    self._capture_bootstrap_server_trace(
                        deployment.prefix,
                        diagnostic_dir,
                        label="prefix bootstrap",
                    )
                output = process_output_text(result)
                bootstrap_command = [
                    deployment.env["DARLING_LAUNCHER"],
                    "exec" if executable is not None else "shell",
                    executable or "set -eu\nprintf '%s\\n' WEST_PREFIX_BOOTSTRAP_OK",
                ]
                bootstrap_artifacts = []
                if diagnostic_dir is not None:
                    bootstrap_artifacts = sorted(
                        path for path in Path(diagnostic_dir).iterdir() if path.is_file()
                    )
                bootstrap_artifacts.extend(
                    path
                    for path in (
                        deployment.prefix / ".west-rootless-boot.log",
                        deployment.prefix / "private/var/tmp/.west-rootless-boot.log",
                        deployment.prefix / ".west-rootless-guest-fd.log",
                    )
                    if path.is_file()
                )

                def record_bootstrap_failure(summary: str) -> None:
                    evidence = getattr(self, "_active_runtime_evidence", None)
                    if evidence is not None:
                        diagnostic_output = output
                        if getattr(evidence, "directory", None) is not None:
                            diagnostic_output = (
                                f"{output.rstrip()}\n\n" if output else ""
                            ) + self._bootstrap_runtime_state(deployment.prefix)
                        evidence.record_failure_detail(
                            phase="bootstrap",
                            summary=summary,
                            returncode=result.returncode,
                            command=bootstrap_command,
                            output=diagnostic_output,
                            artifacts=bootstrap_artifacts,
                        )

                trace_fault = (
                    bootstrap_trace_fatal_signal(trace_dir)
                    if trace_dir is not None
                    else None
                )
                if trace_fault is not None:
                    record_bootstrap_failure(
                        f"prefix bootstrap guest smoke crashed before its verdict: {trace_fault}"
                    )
                    self.die(
                        "prefix bootstrap guest smoke crashed before its verdict: "
                        f"{trace_fault}; syscall trace: {trace_dir}"
                    )
                if result.timed_out:
                    if trace_dir is not None:
                        diagnostic_hint = f"; syscall trace: {trace_dir}"
                    elif stack_sample_dir is not None:
                        diagnostic_hint = f"; stack sample: {stack_sample_dir}"
                    else:
                        diagnostic_hint = ""
                    stall = (
                        bootstrap_syscall_stall_summary(trace_dir)
                        if trace_dir is not None
                        else None
                    )
                    stall_hint = f"; syscall state: {stall}" if stall is not None else ""
                    record_bootstrap_failure(
                        f"prefix bootstrap guest smoke timed out after {smoke_timeout_seconds}s"
                    )
                    self.die(
                        "prefix bootstrap guest smoke timed out after "
                        f"{smoke_timeout_seconds}s{diagnostic_hint}{stall_hint}"
                    )
                if result.returncode != 0:
                    record_bootstrap_failure("prefix bootstrap guest smoke returned non-zero")
                    self.die(
                        "prefix bootstrap guest smoke failed "
                        f"with rc {result.returncode}: {output[-1000:]}"
                    )
                if executable is None and "WEST_PREFIX_BOOTSTRAP_OK" not in output:
                    record_bootstrap_failure(
                        "prefix bootstrap guest smoke returned without its verdict marker"
                    )
                    self.die("prefix bootstrap guest smoke returned without its verdict marker")
                self.inf(f"prefix bootstrap phase complete: guest {target}")
                doctor = run_bounded(
                    [
                        "west",
                        "darling-doctor",
                        "--prefix",
                        str(deployment.prefix),
                        "--build-dir",
                        str(deployment.build_root),
                        "--no-baseline-file",
                    ],
                    cwd=Path(self.topdir),
                    env=None,
                    timeout_seconds=60,
                    capture_output=True,
                )
                doctor_output = process_output_text(doctor)
                if doctor.timed_out:
                    self.die("prefix bootstrap doctor timed out after 60s")
                if doctor.returncode != 0:
                    self.die(
                        "prefix bootstrap doctor failed "
                        f"with rc {doctor.returncode}: {doctor_output[-1000:]}"
                    )
                if definition.get("guest-toolchain") == COMMAND_LINE_TOOLS_RESOURCE:
                    self.inf(
                        "prefix bootstrap phase start: guest toolchain "
                        f"{COMMAND_LINE_TOOLS_RESOURCE}"
                    )
                    try:
                        require_guest_toolchain_provisioning_allowed()
                        self._ensure_guest_toolchain(
                            deployment.prefix,
                            Path(deployment.env["DARLING_LAUNCHER"]),
                            deployment.env,
                        )
                    except GuestToolchainError as error:
                        self.die(
                            f"guest toolchain {COMMAND_LINE_TOOLS_RESOURCE} failed: "
                            f"{error}"
                        )
                    self.inf(
                        "prefix bootstrap phase complete: guest toolchain "
                        f"{COMMAND_LINE_TOOLS_RESOURCE}"
                    )
                self.inf(
                    f"prefix bootstrap passed for {prefix_text}: {profile_name} ({target})"
                )
                marker_path = deployment.prefix / RETAINED_RUNTIME_PROFILE_MARKER
                fingerprint = runtime_identity(
                    topdir=Path(self.topdir),
                    profile_name=profile_name,
                    definition=definition,
                    launcher=deployment.prefix / "bin/darling",
                )
                if not fingerprint.get("launcher-sha256"):
                    self.die(
                        "prefix bootstrap did not produce a hashable launcher: "
                        f"{deployment.prefix / 'bin/darling'}"
                    )
                marker_path.write_text(
                    json.dumps(
                        {
                            "schema": 2,
                            "profile": profile_name,
                            "source-profile": definition["source-profile"],
                            "guest-toolchain": definition.get("guest-toolchain"),
                            "fingerprint": fingerprint,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

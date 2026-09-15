"""Contract for the parameterised runtime load fixture and its profile binding.

The fixture's own runs need a Darling prefix, so this contract binds everything
that is observable on the host:

* the parameters declared by ``patches/runtime-load-smoke/patches.yml`` reach
  the fixture's validated plan and drive its generated program, so the declared
  ``sources``, ``concurrency`` and ``rounds`` are exactly the load the guest
  work is sized to;
* a different declared setting produces the different load it asks for, so a
  parameter that stops being honoured cannot hide behind a matching default;
* an unusable parameter is rejected before any transport work starts;
* the fixture reuses the shared guest transport (``run_guest_shell_argv``)
  rather than inventing one, and carries its toolchain root into the argv;
* the profile binds the same fixture under both ring transports.

The generated program is executed here with a substituted transport and a stub
CommandLineTools tree, which is how the load volumes above are observed without
a prefix.  The prefix-backed run of the same fixture is the requester's step.
"""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from west_commands.test_manifest import load_test_profile  # noqa: E402
import _runtime_load_probe as probe  # noqa: E402


PROFILE = ROOT / "patches/runtime-load-smoke/patches.yml"
STUB_DELAY = "0.5"

TRANSPORTS = ("homebrew-ring-on", "homebrew-ring-off")
PARAMETER_LIMITS = {
    "RUNTIME_LOAD_SOURCES": probe.SOURCES_LIMIT,
    "RUNTIME_LOAD_CONCURRENCY": probe.CONCURRENCY_LIMIT,
    "RUNTIME_LOAD_ROUNDS": probe.ROUNDS_LIMIT,
}
assert set(PARAMETER_LIMITS) == set(probe.PARAMETERS), (
    "the profile must declare exactly the parameters the fixture validates"
)

# The heavier setting the fixture must also accept. Every parameter differs from
# the fixture defaults, so a default substituted for a declared value cannot
# pass this arm by accident, and the observed work is three times the fast one.
HEAVIER_SETTING = {
    "RUNTIME_LOAD_SOURCES": "12",
    "RUNTIME_LOAD_CONCURRENCY": "6",
    "RUNTIME_LOAD_ROUNDS": "4",
}

COMPILE_STUB = r"""#!/bin/bash
out=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) shift; out=$1 ;;
    esac
    shift
done
if [ -n "${RUNTIME_LOAD_STUB_COMPILE_LOG:-}" ]; then
    printf 'S\n' >> "$RUNTIME_LOAD_STUB_COMPILE_LOG"
fi
sleep "${RUNTIME_LOAD_STUB_DELAY:-0}"
if [ -n "${RUNTIME_LOAD_STUB_COMPILE_LOG:-}" ]; then
    printf 'E\n' >> "$RUNTIME_LOAD_STUB_COMPILE_LOG"
fi
printf 'stub-object\n' > "$out"
"""

LINK_STUB = r"""#!/bin/bash
out=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) shift; out=$1 ;;
    esac
    shift
done
if [ -n "${RUNTIME_LOAD_STUB_LINK_LOG:-}" ]; then
    printf 'S\n' >> "$RUNTIME_LOAD_STUB_LINK_LOG"
fi
sleep "${RUNTIME_LOAD_STUB_DELAY:-0}"
if [ -n "${RUNTIME_LOAD_STUB_LINK_LOG:-}" ]; then
    printf 'E\n' >> "$RUNTIME_LOAD_STUB_LINK_LOG"
fi
unit=${out##*linked-}
cat > "$out" <<'STUB_UNIT'
#!/bin/bash
if [ -n "${RUNTIME_LOAD_STUB_EXEC_LOG:-}" ]; then
    printf 'X\n' >> "$RUNTIME_LOAD_STUB_EXEC_LOG"
fi
payload=$(cat)
if [ -n "$payload" ]; then
    printf '%s\n' "$payload"
fi
printf 'unit __STUB_UNIT_INDEX__\n'
STUB_UNIT
sed "s/__STUB_UNIT_INDEX__/$unit/" "$out" > "$out.next"
mv "$out.next" "$out"
chmod +x "$out"
"""


def write_stub_toolchain(root: Path) -> None:
    binary_dir = root / "usr/bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    (root / "SDKs/MacOSX.sdk").mkdir(parents=True, exist_ok=True)
    for name, body in (("clang", COMPILE_STUB), ("ld", LINK_STUB)):
        path = binary_dir / name
        path.write_text(body)
        path.chmod(0o755)


def concurrency_profile(log: Path) -> tuple[int, int]:
    """Return (invocations, peak simultaneous invocations) from a stub log."""

    events = log.read_text().split()
    depth = 0
    peak = 0
    invocations = 0
    for event in events:
        if event == "S":
            invocations += 1
            depth += 1
            peak = max(peak, depth)
        elif event == "E":
            depth -= 1
            assert depth >= 0, "stub log has an unmatched end event"
    assert depth == 0, "stub log has unterminated invocations"
    return invocations, peak


def check_profile_binding() -> list[tuple[str, dict, tuple[int, int, int]]]:
    profile = load_test_profile(PROFILE)
    patches = profile.get("patches", [])
    assert patches, "the smoke profile declares no carrier binding"

    bound: dict[str, dict] = {}
    for patch in patches:
        for test in patch.get("tests", []):
            transport = test.get("runtime-profile")
            assert transport, f"{test.get('name')}: test declares no runtime transport"
            assert transport not in bound, f"transport {transport} is bound twice"
            bound[transport] = test
    assert set(bound) == set(TRANSPORTS), (
        "the smoke profile must bind the fixture under both ring transports"
    )

    declared_tests = []
    for transport in TRANSPORTS:
        test = bound[transport]
        assert test.get("env") == "darling", f"{transport}: the load fixture runs in the guest"
        assert test.get("runner") == "python", f"{transport}: runner must be the python shape"
        assert test.get("script") == "tests/_runtime_load_probe.py", (
            f"{transport}: the binding must name the load fixture"
        )
        assert (ROOT / test["script"]).is_file(), f"{transport}: fixture asset is missing"
        assert test.get("repo") == "darling-workspace", f"{transport}: fixture lives in the workspace"
        assert "darling-prefix" in (test.get("requires") or []), (
            f"{transport}: a prefix-backed run must declare the prefix requirement"
        )
        assert test.get("coverage-tier") == "runtime", f"{transport}: fixture covers runtime behavior"
        assert test.get("diag"), f"{transport}: the run must declare a diagnostic mode"
        assert int(test.get("timeout-seconds", 0)) > 0, f"{transport}: the run needs a deadline"

        parameters = declared_parameters(test)
        expected = tuple(int(parameters[name]) for name in probe.PARAMETERS)
        plan = probe.load_plan(parameters)
        assert (plan.sources, plan.concurrency, plan.rounds) == expected, (
            f"{transport}: declared parameters did not reach the fixture plan"
        )
        declared_tests.append((transport, test, expected))
    return declared_tests


def declared_parameters(test: dict) -> dict[str, str]:
    declared = {}
    for name in probe.PARAMETERS:
        raw = test.get("env-vars", {}).get(name)
        assert raw is not None, f"{test.get('name')}: declaration is missing {name}"
        text = str(raw).strip()
        assert text.lstrip("+").isdigit(), f"{test.get('name')}: {name}={raw!r} is not an integer"
        declared[name] = text
    return declared


def run_fixture(parameters: dict[str, str], case_root: Path, *, delay: str = STUB_DELAY):
    """Run the fixture through its own entry point with a substituted transport."""

    prefix = case_root / "prefix"
    prefix.mkdir()
    work_root = case_root / "work"
    work_root.mkdir()
    stub_root = case_root / "stub"
    write_stub_toolchain(stub_root)
    logs = {
        "compile": case_root / "compile.log",
        "link": case_root / "link.log",
        "exec": case_root / "exec.log",
    }
    environment = {
        **os.environ,
        **parameters,
        "DPREFIX": str(prefix),
        "DARLING_LAUNCHER": str(case_root / "unused-launcher"),
        "RUNTIME_LOAD_WORK_ROOT": str(work_root),
        "RUNTIME_LOAD_CLT_ROOT": str(stub_root),
        "RUNTIME_LOAD_STUB_DELAY": delay,
        "RUNTIME_LOAD_STUB_COMPILE_LOG": str(logs["compile"]),
        "RUNTIME_LOAD_STUB_LINK_LOG": str(logs["link"]),
        "RUNTIME_LOAD_STUB_EXEC_LOG": str(logs["exec"]),
    }
    calls: list[tuple] = []
    recorder = probe.run_guest_shell_argv
    captured: list[str] = []

    def host_transport(launcher, guest_prefix, guest_argv, *, cwd, env, timeout_seconds):
        calls.append(tuple(guest_argv))
        program = guest_argv[guest_argv.index("-c") + 1]
        completed = subprocess.run(
            ["/bin/bash", "-c", program, "runtime-load-probe", guest_argv[-1]],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
        )
        captured.append(completed.stdout)
        sys.stdout.write(completed.stdout)
        sys.stdout.flush()
        if completed.stderr:
            sys.stderr.write(completed.stderr)
            sys.stderr.flush()
        return completed

    probe.run_guest_shell_argv = host_transport
    try:
        returncode = probe.main(env=environment)
    finally:
        probe.run_guest_shell_argv = recorder
    return calls, returncode, logs, stub_root, "".join(captured)


def assert_load(label, parameters, expected, case_root) -> None:
    sources, concurrency, rounds = expected
    calls, returncode, logs, stub_root, output = run_fixture(parameters, case_root)
    assert returncode == 0, f"{label}: the generated load program failed"
    assert len(calls) == 1, f"{label}: the fixture must use the shared transport exactly once"
    argv = calls[0]
    assert argv[:2] == ("/usr/bin/env", "-i"), f"{label}: transport argv is not the shared shape"
    assert argv[argv.index("/bin/bash") + 1] == "-c", f"{label}: guest program is not shell -c"
    assert argv[-2] == "runtime-load-probe", f"{label}: guest program name changed"
    assert f"RUNTIME_LOAD_CLT_ROOT={stub_root}" in argv, (
        f"{label}: the toolchain root did not reach the guest argv"
    )

    for marker in ("RUNTIME_LOAD_TOOLCHAIN_OK",):
        assert marker in output, f"{label}: missing verdict marker {marker}"
    for round_number in range(1, rounds + 1):
        for marker in (
            f"RUNTIME_LOAD_SOURCES_OK round={round_number} sources={sources}",
            f"RUNTIME_LOAD_COMPILE_OK round={round_number} sources={sources} "
            f"concurrency={concurrency}",
            f"RUNTIME_LOAD_LINK_OK round={round_number} outputs={sources}",
            f"RUNTIME_LOAD_EXEC_OK round={round_number} spawns={sources}",
            f"RUNTIME_LOAD_DESCRIPTOR_OK round={round_number} "
            f"descriptors={probe.DESCRIPTOR_COUNT}",
        ):
            assert marker in output, f"{label}: missing stage marker {marker}"
    verdict = (
        f"RUNTIME_LOAD_OK sources={sources} concurrency={concurrency} rounds={rounds}"
    )
    assert verdict in output, f"{label}: missing verdict line {verdict}"

    expected_invocations = sources * rounds
    expected_peak = min(concurrency, sources)
    for stage in ("compile", "link"):
        invocations, peak = concurrency_profile(logs[stage])
        assert invocations == expected_invocations, (
            f"{label}: {stage} ran {invocations} times for a declared {expected_invocations}"
        )
        assert peak == expected_peak, (
            f"{label}: peak {stage} concurrency {peak}, declared {expected_peak}"
        )
    exec_spawns = logs["exec"].read_text().split()
    assert len(exec_spawns) == expected_invocations, (
        f"{label}: fork/exec storm ran {len(exec_spawns)} processes "
        f"for a declared {expected_invocations}"
    )


def check_declared_load_runs(transport, test, expected, case_root: Path) -> None:
    assert_load(transport, declared_parameters(test), expected, case_root)


def check_heavier_setting(case_root: Path) -> None:
    expected = tuple(int(HEAVIER_SETTING[name]) for name in probe.PARAMETERS)
    defaults = (probe.DEFAULT_SOURCES, probe.DEFAULT_CONCURRENCY, probe.DEFAULT_ROUNDS)
    assert expected != defaults, (
        "the heavier setting must differ from the fixture defaults to be meaningful"
    )
    assert_load("heavier", HEAVIER_SETTING, expected, case_root)


def check_invalid_parameters_rejected() -> None:
    for name, limit in PARAMETER_LIMITS.items():
        for bad in ("0", "abc", str(limit + 1)):
            with tempfile.TemporaryDirectory(prefix="runtime-load-invalid-") as temporary:
                completed = subprocess.run(
                    [sys.executable, "-B", str(probe.__file__)],
                    env={**os.environ, name: bad},
                    text=True,
                    capture_output=True,
                    cwd=temporary,
                )
            assert completed.returncode != 0, f"{name}={bad} was accepted"
            assert name in completed.stderr, (
                f"{name}={bad} was rejected without naming the parameter: {completed.stderr!r}"
            )
            assert "RUNTIME_LOAD_INVALID" in completed.stderr, (
                f"{name}={bad} was not rejected as an invalid parameter: {completed.stderr!r}"
            )

    def forbidden_transport(*args, **kwargs):
        raise AssertionError("the transport must not start for an invalid parameter")

    recorder = probe.run_guest_shell_argv
    probe.run_guest_shell_argv = forbidden_transport
    try:
        for name, limit in PARAMETER_LIMITS.items():
            suppressed = io.StringIO()
            with contextlib.redirect_stderr(suppressed):
                returncode = probe.main(
                    env={
                        "DPREFIX": "/nonexistent-prefix",
                        "DARLING_LAUNCHER": "/nonexistent-launcher",
                        name: str(limit + 1),
                    }
                )
            assert returncode != 0, f"{name} above its limit was accepted"
            assert name in suppressed.getvalue(), (
                f"{name} was rejected without naming the parameter"
            )
    finally:
        probe.run_guest_shell_argv = recorder


def main() -> int:
    declared_tests = check_profile_binding()
    with tempfile.TemporaryDirectory(prefix="parallel-load-contract-") as temporary:
        root = Path(temporary)
        for index, (transport, test, expected) in enumerate(declared_tests, start=1):
            case_root = root / f"declared-{index}"
            case_root.mkdir()
            check_declared_load_runs(transport, test, expected, case_root)
        heavier_root = root / "heavier"
        heavier_root.mkdir()
        check_heavier_setting(heavier_root)
    check_invalid_parameters_rejected()
    print("PASS parallel-load-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

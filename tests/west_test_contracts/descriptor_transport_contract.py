"""Contract for window-scoped RPC descriptor-transport observation.

The gate's whole value is that it counts descriptor transfers *inside* one
declared window and cannot be satisfied by traffic elsewhere in the run, so the
contract is written against synthetic captures: the same trace shape strace
produces for the real fixture, with the descriptor deliberately moved around.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_descriptor_transport import (
    DescriptorTraceError,
    EXPECT_AT_LEAST_ONE,
    EXPECT_NONE,
    WindowSpec,
    analyze_trace,
    capture_command,
    marker_guest_path,
    marker_token,
    parse_trace_events,
    summary_text,
    transcript_marker,
    window_specs,
)

DECLARATION = {
    "windows": [
        {"id": "console-open-control", "expect-descriptor-messages": "at-least-one"},
        {"id": "vchroot-fdless-valid", "expect-descriptor-messages": "none"},
    ]
}

TRANSCRIPT = """\
VCHROOT_ROOT_FD=4
DSERVER_DESCRIPTOR_WINDOW BEGIN console-open-control
VCHROOT_FDLESS_CONTROL_CONSOLE fd=6 errno=0
DSERVER_DESCRIPTOR_WINDOW END console-open-control
DSERVER_DESCRIPTOR_WINDOW BEGIN vchroot-fdless-valid
VCHROOT_VALID_FD rv=0 errno=0
DSERVER_DESCRIPTOR_WINDOW END vchroot-fdless-valid
VCHROOT_FDLESS_VALID_OK
"""

# The synthetic lines keep the parts the analyzer reads: the syscall prefix, the
# expanded marker path and the SCM_RIGHTS ancillary text strace prints for a
# descriptor transfer.
DESCRIPTOR_REPLY = (
    "recvmsg(1048575<UNIX:[1,2]>, {msg_name=NULL, msg_namelen=0, "
    "msg_iov=[{iov_base=\"\\x10\\x00\\x00\\x00\", iov_len=48}], msg_iovlen=1, "
    "msg_control=[{cmsg_len=20, cmsg_level=SOL_SOCKET, cmsg_type=SCM_RIGHTS, "
    "cmsg_data=[6<UNIX-STREAM:[7->8]>]}], msg_controllen=24, msg_flags=0}, "
    "MSG_DONTWAIT) = 12"
)
CALL_WITHOUT_DESCRIPTOR = (
    "sendmsg(1048575<UNIX:[1,2]>, {msg_name={sa_family=AF_UNIX, "
    "sun_path=\"/proc/99/fd/3/.darlingserver.sock\"}, msg_namelen=110, "
    "msg_iov=[{iov_base=\"\\x09\\x00\\x00\\x00\", iov_len=32}], msg_iovlen=1, "
    "msg_controllen=0, msg_flags=0}, 0) = 32"
)
CALL_WITH_DESCRIPTOR = (
    "sendmsg(1048575<UNIX:[1,2]>, {msg_name={sa_family=AF_UNIX, "
    "sun_path=\"/proc/99/fd/3/.darlingserver.sock\"}, msg_namelen=110, "
    "msg_iov=[{iov_base=\"\\x09\\x00\\x00\\x00\", iov_len=24}], msg_iovlen=1, "
    "msg_control=[{cmsg_len=20, cmsg_level=SOL_SOCKET, cmsg_type=SCM_RIGHTS, "
    "cmsg_data=[4<pipe:[11]>]}], msg_controllen=24, msg_flags=0}, 0) = 24"
)
BATCH_WITH_DESCRIPTOR = (
    "sendmmsg(7<UNIX-STREAM:[9->10]>, [{msg_hdr={msg_name=NULL, msg_namelen=0, "
    "msg_iov=[{iov_base=\"\\x04\\x00\\x00\\x00\", iov_len=4}], msg_iovlen=1, "
    "msg_control=[{cmsg_len=28, cmsg_level=SOL_SOCKET, cmsg_type=SCM_RIGHTS, "
    "cmsg_data=[6</dev/null>, 1</tmp/x>]}], msg_controllen=32, msg_flags=0}}], "
    "1, 0) = 1"
)


def marker_line(prefix: str, window_id: str, phase: str, timestamp: str) -> str:
    return (
        f"{timestamp} readlinkat(AT_FDCWD</work>, "
        f"\"{prefix}{marker_guest_path(window_id, phase)}\", 0x7ffd, 64) = -1 "
        "ENOENT (No such file or directory)"
    )


def capture(
    *,
    descriptor_in_vchroot: bool = False,
    descriptor_in_console: bool = True,
    call_in_vchroot: bool = True,
    trailing_descriptor: bool = True,
) -> tuple[dict[str, Path], str]:
    """Return one synthetic capture's files by name."""

    lines = [
        "13:00:00.100000 " + DESCRIPTOR_REPLY,
        marker_line("/tmp/prefix", "console-open-control", "begin", "13:00:00.200000"),
        "13:00:00.300000 " + CALL_WITHOUT_DESCRIPTOR,
        marker_line("/tmp/prefix", "console-open-control", "end", "13:00:00.400000"),
        marker_line("/tmp/prefix", "vchroot-fdless-valid", "begin", "13:00:00.500000"),
    ]
    if call_in_vchroot:
        lines.append(
            "13:00:00.600000 "
            + (CALL_WITH_DESCRIPTOR if descriptor_in_vchroot else CALL_WITHOUT_DESCRIPTOR)
        )
    else:
        lines.append("<... readlinkat resumed>) = -1 ENOENT (No such file or directory)")
    lines.extend(
        [
            "13:00:00.700000 recvmsg(1048575<UNIX:[1,2]>, {msg_name=NULL, msg_namelen=0, "
            "msg_iov=[{iov_base=\"\\x09\\x00\\x00\\x00\", iov_len=48}], msg_iovlen=1, "
            "msg_controllen=0, msg_flags=0}, MSG_DONTWAIT) = 8",
            marker_line("/tmp/prefix", "vchroot-fdless-valid", "end", "13:00:00.800000"),
        ]
    )
    if trailing_descriptor:
        lines.append("13:00:00.900000 " + BATCH_WITH_DESCRIPTOR)
    if descriptor_in_console:
        lines.insert(3, "13:00:00.350000 " + DESCRIPTOR_REPLY)
    return {"trace.4242": "\n".join(lines) + "\n"}, TRANSCRIPT


def write_capture(directory: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        (directory / name).write_text(content)
    return directory


def analyze(directory: Path, transcript: str = TRANSCRIPT):
    return analyze_trace(directory, window_specs(DECLARATION), transcript)


def line_for(report, window_id: str) -> str:
    for line in report.summary_lines():
        if line.startswith(f"WEST_DESCRIPTOR_TRACE window={window_id} "):
            return line
    raise AssertionError(f"no summary line for {window_id}")


# --- declaration validation -------------------------------------------------

specs = window_specs(DECLARATION)
assert specs == (
    WindowSpec("console-open-control", EXPECT_AT_LEAST_ONE),
    WindowSpec("vchroot-fdless-valid", EXPECT_NONE),
), specs

for bad, fragment in (
    ([], "must be a mapping"),
    ({}, "needs a non-empty windows list"),
    ({"windows": []}, "needs a non-empty windows list"),
    ({"windows": [{"id": "a"}]}, "expect-descriptor-messages must be one of"),
    ({"windows": [{"id": "a", "expect-descriptor-messages": "sometimes"}]}, "must be one of"),
    ({"windows": ["a"]}, "must be a mapping"),
    ({"windows": [], "extra": 1}, "has unknown keys: extra"),
    ({"windows": [{"id": "a", "expect-descriptor-messages": "none", "x": 1}]}, "unknown keys: x"),
    ({"windows": [{"id": "a", "expect-descriptor-messages": "none"}] * 2}, "repeats window id"),
    ({"windows": [{"id": "a/b", "expect-descriptor-messages": "none"}]}, "id matching"),
    ({"windows": [{"id": "a b", "expect-descriptor-messages": "none"}]}, "id matching"),
    ({"windows": [{"id": "", "expect-descriptor-messages": "none"}]}, "id matching"),
):
    try:
        window_specs(bad)
    except DescriptorTraceError as error:
        assert fragment in str(error), (bad, fragment, str(error))
    else:
        raise AssertionError(f"declaration accepted: {bad!r}")

# --- marker protocol --------------------------------------------------------

assert marker_token("vchroot-fdless-valid", "begin") == (
    "darling-descriptor-window-vchroot-fdless-valid-begin"
)
assert marker_guest_path("vchroot-fdless-valid", "END".lower()) == (
    "/tmp/darling-descriptor-window-vchroot-fdless-valid-end"
)
assert transcript_marker("vchroot-fdless-valid", "BEGIN") == (
    "DSERVER_DESCRIPTOR_WINDOW BEGIN vchroot-fdless-valid"
)
try:
    transcript_marker("vchroot-fdless-valid", "middle")
except DescriptorTraceError:
    pass
else:
    raise AssertionError("unknown marker phase accepted")

# The token must survive the prefix expansion the guest applies to the path.
events = parse_trace_events(
    write_capture(
        Path(tempfile.mkdtemp()),
        {
            "trace.7": marker_line(
                "/tmp/darling-rootless-long-prefix-name", "vchroot-fdless-valid",
                "begin", "13:00:00.000000",
            )
            + "\n13:00:00.000001 " + CALL_WITH_DESCRIPTOR + "\n"
        },
    )
    / "trace.7"
)
assert [event.marker for event in events if event.marker] == [
    ("vchroot-fdless-valid", "BEGIN")
], events
assert [event.descriptor for event in events if event.direction] == [True], events
assert all(event.marker is None for event in events if event.direction), events

# --- the capture command ----------------------------------------------------

command = capture_command("/tmp/trace-dir", prefix_length=40)
assert command[:2] == ["strace", "-f"], command
assert "--seccomp-bpf" in command, command
assert "-ff" in command and "-yy" in command and "-tt" in command, command
assert command[command.index("-s") + 1] == "256", command
trace_filter = command[command.index("-e") + 1]
for syscall in ("sendmsg", "recvmsg", "sendmmsg", "recvmmsg", "readlink", "readlinkat"):
    assert syscall in trace_filter, (syscall, trace_filter)
assert command[command.index("-o") + 1] == "/tmp/trace-dir/trace", command
assert capture_command("/tmp/x", prefix_length=300)[
    capture_command("/tmp/x", prefix_length=300).index("-s") + 1
] == "428", capture_command("/tmp/x", prefix_length=300)

# --- green oracle -----------------------------------------------------------

with tempfile.TemporaryDirectory() as temp:
    files, transcript = capture()
    directory = write_capture(Path(temp), files)
    report = analyze(directory, transcript)
    assert report.ok, report.failures
    assert line_for(report, "vchroot-fdless-valid").endswith(
        "call-messages=1 descriptor-messages=0 expect=none verdict=ok trace-file=trace.4242"
    ), line_for(report, "vchroot-fdless-valid")
    assert line_for(report, "console-open-control").endswith(
        "call-messages=1 descriptor-messages=1 expect=at-least-one verdict=ok "
        "trace-file=trace.4242"
    ), line_for(report, "console-open-control")
    assert report.summary_lines()[-1] == "WEST_DESCRIPTOR_TRACE_OK windows=2"
    # One descriptor-bearing message per window is described in full.
    described = [line for line in report.summary_lines() if "_DESCRIPTOR " in line]
    assert len(described) == 1 and "SCM_RIGHTS" in described[0], described

# --- the gate fails when it should ------------------------------------------

# A descriptor inside the vchroot window is exactly the pre-port behavior.
with tempfile.TemporaryDirectory() as temp:
    files, transcript = capture(descriptor_in_vchroot=True)
    report = analyze(write_capture(Path(temp), files), transcript)
    assert not report.ok, report.summary_lines()
    assert report.results[1].failures == (
        "expected no descriptor-bearing message, saw 1",
    ), report.results
    assert (
        "WEST_DESCRIPTOR_TRACE_FAILURE window=vchroot-fdless-valid "
        "expected no descriptor-bearing message" in summary_text(report)
    ), summary_text(report)
    # The control window is unaffected by the vchroot failure.
    assert report.results[0].ok, report.results

# The negative control must actually observe a descriptor, otherwise a zero
# count on the vchroot window would prove nothing.
with tempfile.TemporaryDirectory() as temp:
    files, transcript = capture(descriptor_in_console=False)
    report = analyze(write_capture(Path(temp), files), transcript)
    assert [result.ok for result in report.results] == [False, True], report.results
    assert report.results[0].failures == (
        "expected at least one descriptor-bearing message, saw none",
    ), report.results

# A call that stopped being made must not pass a zero-descriptor assertion.
with tempfile.TemporaryDirectory() as temp:
    files, transcript = capture(call_in_vchroot=False)
    report = analyze(write_capture(Path(temp), files), transcript)
    assert [result.ok for result in report.results] == [True, False], report.results
    assert report.results[1].failures == (
        "no call-direction message inside the window "
        "(the operation under test made no RPC)",
    ), report.results

# A truncated marker path is reported as such instead of as a missing window.
with tempfile.TemporaryDirectory() as temp:
    files, transcript = capture()
    files["trace.4242"] = files["trace.4242"].replace(
        "/tmp/prefix/tmp/darling-descriptor-window-vchroot-fdless-valid-begin\", 0x7ffd, 64)",
        "/tmp/prefix/tmp/darling-descriptor-window-vchro\", 0x7ffd, 64)",
    )
    report = analyze(write_capture(Path(temp), files), transcript)
    assert not report.ok, report.summary_lines()
    assert report.results[1].failures == (
        "window marker path was truncated in the trace "
        "(raise the strace string limit or shorten the prefix)",
    ), report.results

# Missing markers are a capture failure, not a pass.
with tempfile.TemporaryDirectory() as temp:
    files, transcript = capture()
    files["trace.4242"] = "\n".join(
        line
        for line in files["trace.4242"].splitlines()
        if "darling-descriptor-window" not in line
    ) + "\n"
    report = analyze(write_capture(Path(temp), files), transcript)
    assert [result.ok for result in report.results] == [False, False], report.results
    assert all(
        result.failures == ("window markers are missing from the trace",)
        for result in report.results
    ), report.results

# An empty capture directory must not pass vacuously.
with tempfile.TemporaryDirectory() as temp:
    report = analyze(Path(temp), TRANSCRIPT)
    assert not report.ok and len(report.failures) == 2, report.failures

# The guest transcript is the guest-side declaration: a window the fixture never
# reached cannot be satisfied by an unrelated instruction stream.
with tempfile.TemporaryDirectory() as temp:
    files, _ = capture()
    report = analyze(
        write_capture(Path(temp), files),
        TRANSCRIPT.replace(
            "DSERVER_DESCRIPTOR_WINDOW BEGIN vchroot-fdless-valid\n", ""
        ),
    )
    assert [result.ok for result in report.results] == [True, False], report.results
    assert report.results[1].failures == (
        "guest transcript has no BEGIN marker "
        "('DSERVER_DESCRIPTOR_WINDOW BEGIN vchroot-fdless-valid')",
    ), report.results

# Traffic in another process's trace file cannot satisfy a window.
with tempfile.TemporaryDirectory() as temp:
    files, transcript = capture()
    files["trace.4343"] = (
        marker_line("/tmp/prefix", "vchroot-fdless-valid", "begin", "13:00:00.000000")
        + "13:00:00.000001 "
        + CALL_WITH_DESCRIPTOR
        + "\n"
        + marker_line("/tmp/prefix", "vchroot-fdless-valid", "end", "13:00:00.000002")
        + "\n"
    )
    report = analyze(write_capture(Path(temp), files), transcript)
    assert report.ok, report.results
    # The scoping picks the first window in trace order, which is the fixture's.
    assert [result.trace_file for result in report.results] == [
        "trace.4242",
        "trace.4242",
    ], report.results

print("PASS west-test-descriptor-transport-contract")

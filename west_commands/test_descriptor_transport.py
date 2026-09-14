"""Window-scoped RPC descriptor-transport observation for guest fixtures.

darlingserver transfers file descriptors between the guest and the server as
``SCM_RIGHTS`` ancillary data on the per-thread RPC socket.  A guest fixture can
declare a ``descriptor-trace`` gate to make that transport observable: it wraps
one operation in a *window*, the runner captures the launcher tree under
``strace``, and this module scopes the per-process trace files to the windows
and counts the descriptor-bearing messages inside each one.

A window is delimited twice, and the two records must agree:

* the fixture prints ``DSERVER_DESCRIPTOR_WINDOW BEGIN|END <id>`` to its stdout,
  which is the guest-visible declaration that the operation ran, and
* the fixture resolves a unique path token with ``readlink``, which the guest
  emulation performs as a raw Linux ``readlinkat`` in the fixture's own process,
  so the token lands in that process's trace file and brackets its RPC traffic.

Only messages between a window's two markers are attributed to it, so an
unrelated descriptor transfer elsewhere in the run cannot satisfy the gate.  A
window that observed no call-direction message fails the gate: a call that
stopped being made must not pass a "transfers no descriptor" assertion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

# The token is a path component, so it stays inside one path element and cannot
# be confused with the surrounding prefix expansion.
MARKER_TOKEN_PREFIX = "darling-descriptor-window"
MARKER_GUEST_DIR = "/tmp"

# Message syscalls the RPC transport uses.  The library batches through
# sendmmsg/recvmmsg on some paths, so a descriptor can hide in a batched call.
MESSAGE_SYSCALLS = ("sendmsg", "recvmsg", "sendmmsg", "recvmmsg")
MARKER_SYSCALLS = ("readlink", "readlinkat")
DESCRIPTOR_ANCILLARY = "SCM_RIGHTS"

EXPECT_NONE = "none"
EXPECT_AT_LEAST_ONE = "at-least-one"
EXPECTATIONS = (EXPECT_NONE, EXPECT_AT_LEAST_ONE)

TRANSCRIPT_PREFIX = "DSERVER_DESCRIPTOR_WINDOW"
WINDOW_PHASES = ("BEGIN", "END")

MINIMUM_STRING_LIMIT = 256
STRING_LIMIT_SLACK = 128
MAX_DESCRIPTOR_DETAIL_LINES = 8

_TIMESTAMP = re.compile(r"^(?P<stamp>\d{2}:\d{2}:\d{2}\.\d+)\s+")
_SYSCALL = re.compile(r"^(?P<name>[a-z_][a-z_0-9]*)\(|^<\.\.\. (?P<resumed>[a-z_][a-z_0-9]*) resumed>")
_TOKEN = re.compile(
    rf"{re.escape(MARKER_TOKEN_PREFIX)}-(?P<id>[A-Za-z0-9_.-]+)-(?P<phase>begin|end)\b"
)
_TRACE_FILE = re.compile(r"^trace\.(?P<pid>\d+)$")


class DescriptorTraceError(ValueError):
    """A descriptor-trace declaration or capture is unusable."""


@dataclass(frozen=True)
class WindowSpec:
    """One declared observation window."""

    window_id: str
    expect: str


@dataclass(frozen=True)
class TraceEvent:
    """One parsed strace line that matters to the gate."""

    path: Path
    line_number: int
    syscall: str
    direction: str | None
    descriptor: bool
    marker: tuple[str, str] | None
    text: str


@dataclass(frozen=True)
class WindowResult:
    """Observed contents of one declared window."""

    window_id: str
    expect: str
    trace_file: str | None
    call_messages: int
    descriptor_messages: int
    descriptor_lines: tuple[str, ...]
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class Report:
    """The complete gate verdict."""

    results: tuple[WindowResult, ...]

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            failure for result in self.results for failure in result.failures
        )

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary_lines(self) -> list[str]:
        """Return the stable, greppable evidence lines for a run log."""

        lines: list[str] = []
        for result in self.results:
            lines.append(
                "WEST_DESCRIPTOR_TRACE "
                f"window={result.window_id} "
                f"call-messages={result.call_messages} "
                f"descriptor-messages={result.descriptor_messages} "
                f"expect={result.expect} "
                f"verdict={'ok' if result.ok else 'failed'} "
                f"trace-file={result.trace_file or '<none>'}"
            )
            for detail in result.descriptor_lines[:MAX_DESCRIPTOR_DETAIL_LINES]:
                lines.append(
                    f"WEST_DESCRIPTOR_TRACE_DESCRIPTOR window={result.window_id} {detail}"
                )
            hidden = len(result.descriptor_lines) - MAX_DESCRIPTOR_DETAIL_LINES
            if hidden > 0:
                lines.append(
                    f"WEST_DESCRIPTOR_TRACE_DESCRIPTOR window={result.window_id} "
                    f"... {hidden} more descriptor-bearing message(s)"
                )
            for failure in result.failures:
                lines.append(f"WEST_DESCRIPTOR_TRACE_FAILURE window={result.window_id} {failure}")
        if self.ok:
            lines.append(f"WEST_DESCRIPTOR_TRACE_OK windows={len(self.results)}")
        else:
            lines.append("WEST_DESCRIPTOR_TRACE_FAILED " + "; ".join(self.failures))
        return lines


def window_specs(declaration: Any) -> tuple[WindowSpec, ...]:
    """Validate one ``descriptor-trace`` metadata block into window specs.

    Raises :class:`DescriptorTraceError` with a message that names the offending
    field, so both the manifest checker and the runner can report it verbatim.
    """

    if not isinstance(declaration, dict):
        raise DescriptorTraceError("must be a mapping")
    unknown = sorted(set(declaration) - {"windows"})
    if unknown:
        raise DescriptorTraceError(f"has unknown keys: {', '.join(unknown)}")
    windows = declaration.get("windows")
    if not isinstance(windows, list) or not windows:
        raise DescriptorTraceError("needs a non-empty windows list")
    specs: list[WindowSpec] = []
    seen: set[str] = set()
    for index, window in enumerate(windows):
        location = f"windows[{index}]"
        if not isinstance(window, dict):
            raise DescriptorTraceError(f"{location} must be a mapping")
        unknown = sorted(set(window) - {"id", "expect-descriptor-messages"})
        if unknown:
            raise DescriptorTraceError(
                f"{location} has unknown keys: {', '.join(unknown)}"
            )
        window_id = window.get("id")
        if not isinstance(window_id, str) or not window_id:
            raise DescriptorTraceError(
                f"{location} needs an id matching [A-Za-z0-9_.-]+"
            )
        composed = _TOKEN.fullmatch(marker_token(window_id, "begin"))
        if composed is None or composed.group("id") != window_id:
            raise DescriptorTraceError(
                f"{location} needs an id matching [A-Za-z0-9_.-]+"
            )
        if window_id in seen:
            raise DescriptorTraceError(f"{location} repeats window id {window_id!r}")
        seen.add(window_id)
        expect = window.get("expect-descriptor-messages")
        if expect not in EXPECTATIONS:
            raise DescriptorTraceError(
                f"{location}.expect-descriptor-messages must be one of "
                f"{', '.join(EXPECTATIONS)}"
            )
        specs.append(WindowSpec(window_id=window_id, expect=str(expect)))
    return tuple(specs)


def marker_token(window_id: str, phase: str) -> str:
    """Return the trace token for one window marker."""

    return f"{MARKER_TOKEN_PREFIX}-{window_id}-{phase.lower()}"


def marker_guest_path(window_id: str, phase: str) -> str:
    """Return the guest path the fixture resolves to publish one marker."""

    return f"{MARKER_GUEST_DIR}/{marker_token(window_id, phase)}"


def transcript_marker(window_id: str, phase: str) -> str:
    """Return the stdout marker the fixture prints for one window boundary."""

    if phase not in WINDOW_PHASES:
        raise DescriptorTraceError(f"unknown window phase {phase!r}")
    return f"{TRANSCRIPT_PREFIX} {phase} {window_id}"


def capture_command(trace_dir: Path | str, *, prefix_length: int = 0) -> list[str]:
    """Return the host ``strace`` prefix that captures one runner tree.

    ``--seccomp-bpf`` keeps the untraced syscalls out of the ptrace path; without
    it the runtime's adaptive RPC spin loops time out under capture.
    """

    string_limit = max(MINIMUM_STRING_LIMIT, int(prefix_length) + STRING_LIMIT_SLACK)
    return [
        "strace",
        "-f",
        "-ff",
        "-tt",
        "-yy",
        "-x",
        "-s",
        str(string_limit),
        "--seccomp-bpf",
        "-e",
        f"trace={','.join((*MESSAGE_SYSCALLS, *MARKER_SYSCALLS))}",
        "-o",
        str(Path(trace_dir) / "trace"),
    ]


def trace_files(trace_dir: Path) -> list[Path]:
    """Return the per-process trace files of one capture, in stable order."""

    return sorted(
        (path for path in Path(trace_dir).glob("trace.*") if _TRACE_FILE.match(path.name)),
        key=lambda path: path.name,
    )


def parse_trace_events(path: Path) -> list[TraceEvent]:
    """Parse one strace ``-ff`` file into message and marker events."""

    events: list[TraceEvent] = []
    for number, raw in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        line = raw.rstrip()
        match = _TIMESTAMP.match(line)
        text = line[match.end():] if match is not None else line
        called = _SYSCALL.match(text)
        if called is None:
            continue
        syscall = called.group("name") or called.group("resumed") or ""
        marker: tuple[str, str] | None = None
        direction: str | None = None
        if syscall in MARKER_SYSCALLS:
            token = _TOKEN.search(text)
            if token is not None:
                marker = (token.group("id"), token.group("phase").upper())
        elif syscall in MESSAGE_SYSCALLS:
            direction = "call" if syscall.startswith("send") else "reply"
        else:
            continue
        events.append(
            TraceEvent(
                path=path,
                line_number=number,
                syscall=syscall,
                direction=direction,
                descriptor=DESCRIPTOR_ANCILLARY in text,
                marker=marker,
                text=text,
            )
        )
    return events


def _find_window(
    events: Sequence[TraceEvent], spec: WindowSpec
) -> tuple[TraceEvent | None, TraceEvent | None, bool]:
    """Return a window's begin/end markers and whether only a truncated token matched."""

    begins = [
        event
        for event in events
        if event.marker is not None and event.marker == (spec.window_id, "BEGIN")
    ]
    ends = [
        event
        for event in events
        if event.marker is not None and event.marker == (spec.window_id, "END")
    ]
    for begin in begins:
        for end in ends:
            if end.path == begin.path and end.line_number > begin.line_number:
                return begin, end, False
    # A truncated marker path drops the token suffix, so the regex above misses
    # it while the line still names the token prefix.
    truncated = any(
        event.direction is None
        and event.marker is None
        and MARKER_TOKEN_PREFIX in event.text
        for event in events
    )
    return None, None, truncated


def analyze_trace(
    trace_dir: Path | str,
    specs: Sequence[WindowSpec],
    transcript: str,
) -> Report:
    """Scope one capture to the declared windows and count descriptor messages.

    ``transcript`` is the guest fixture's stdout; its printed window markers are
    the guest-side declaration that the operation ran, so the trace cannot
    satisfy a window the fixture never reached.
    """

    events: list[TraceEvent] = []
    for path in trace_files(trace_dir):
        events.extend(parse_trace_events(path))

    results: list[WindowResult] = []
    for spec in specs:
        failures: list[str] = []
        for phase in WINDOW_PHASES:
            if transcript_marker(spec.window_id, phase) not in transcript:
                failures.append(
                    f"guest transcript has no {phase} marker "
                    f"({transcript_marker(spec.window_id, phase)!r})"
                )
        begin, end, truncated = _find_window(events, spec)
        if begin is None or end is None:
            if truncated:
                failures.append(
                    "window marker path was truncated in the trace "
                    "(raise the strace string limit or shorten the prefix)"
                )
            elif not failures:
                failures.append("window markers are missing from the trace")
            results.append(
                WindowResult(
                    window_id=spec.window_id,
                    expect=spec.expect,
                    trace_file=None,
                    call_messages=0,
                    descriptor_messages=0,
                    descriptor_lines=(),
                    failures=tuple(failures),
                )
            )
            continue

        inside = [
            event
            for event in events
            if event.path == begin.path
            and begin.line_number < event.line_number < end.line_number
            and event.direction is not None
        ]
        call_messages = [event for event in inside if event.direction == "call"]
        described = [event for event in inside if event.descriptor]
        if not call_messages:
            failures.append(
                "no call-direction message inside the window "
                "(the operation under test made no RPC)"
            )
        if spec.expect == EXPECT_NONE and described:
            failures.append(
                f"expected no descriptor-bearing message, saw {len(described)}"
            )
        elif spec.expect == EXPECT_AT_LEAST_ONE and not described:
            failures.append(
                "expected at least one descriptor-bearing message, saw none"
            )
        results.append(
            WindowResult(
                window_id=spec.window_id,
                expect=spec.expect,
                trace_file=begin.path.name,
                call_messages=len(call_messages),
                descriptor_messages=len(described),
                descriptor_lines=tuple(
                    f"syscall={event.syscall} trace-line={event.line_number} {event.text}"
                    for event in described
                ),
                failures=tuple(failures),
            )
        )
    return Report(results=tuple(results))


def summary_text(report: Report) -> str:
    """Return the gate evidence as one block of text."""

    return "\n".join(report.summary_lines())

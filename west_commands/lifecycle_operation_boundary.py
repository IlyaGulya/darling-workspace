"""Thin West adapter for the Rust lifecycle operation boundary.

The lifecycle authority lives in ``lifecycle/operation-boundary``.  This
module intentionally contains no lifecycle syscalls, fd ownership, or policy
logic.  It only transports a JSON request to the Rust binary for West-side
contracts and returns the structured JSON response.
"""

from __future__ import annotations

import json
import os
import subprocess
import selectors
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class RustBoundaryUnavailable(RuntimeError):
    """The Rust boundary binary is not available in the requested context."""


_TRANSPORT_TIMEOUT_SECONDS = 5.0
_TRANSPORT_INPUT_LIMIT = 64 * 1024
_TRANSPORT_OUTPUT_LIMIT = 64 * 1024


@dataclass(frozen=True)
class RustBoundaryAdapter:
    """JSON transport only; the Rust process owns all lifecycle semantics."""

    repository_root: Path
    binary: Path | None = None
    timeout_seconds: float = _TRANSPORT_TIMEOUT_SECONDS

    @property
    def executable(self) -> Path:
        if self.binary is not None:
            return self.binary
        configured = os.environ.get("DARLING_LIFECYCLE_BOUNDARY_BIN")
        if configured:
            return Path(configured)
        return self.repository_root / "lifecycle" / "operation-boundary" / "target" / "debug" / "lifecycle-boundary"

    def invoke(self, request: Mapping[str, Any]) -> dict[str, Any]:
        executable = self.executable
        if not executable.is_file():
            raise RustBoundaryUnavailable(f"Rust lifecycle boundary is not built: {executable}")
        payload = (json.dumps(dict(request), sort_keys=True) + "\n").encode()
        if len(payload) > _TRANSPORT_INPUT_LIMIT:
            raise RuntimeError(
                f"Rust lifecycle boundary input exceeds {_TRANSPORT_INPUT_LIMIT} byte transport limit"
            )
        if not 0 < self.timeout_seconds <= _TRANSPORT_TIMEOUT_SECONDS:
            raise ValueError("transport timeout must be within the hard bound")
        deadline = time.monotonic() + self.timeout_seconds
        process = subprocess.Popen(
            [str(executable)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        selector = selectors.DefaultSelector()
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stdin = process.stdin
        stdout = process.stdout
        stderr = process.stderr
        for stream in (stdin, stdout, stderr):
            os.set_blocking(stream.fileno(), False)
        selector.register(stdin, selectors.EVENT_WRITE, "stdin")
        selector.register(stdout, selectors.EVENT_READ, "stdout")
        selector.register(stderr, selectors.EVENT_READ, "stderr")
        output = {"stdout": bytearray(), "stderr": bytearray()}
        offset = 0
        try:
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RustBoundaryUnavailable(
                        f"Rust lifecycle boundary exceeded {self.timeout_seconds}s transport deadline"
                    )
                if not selector.get_map():
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired as error:
                        raise RustBoundaryUnavailable(
                            f"Rust lifecycle boundary exceeded {self.timeout_seconds}s transport deadline"
                        ) from error
                    continue
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        try:
                            written = os.write(stdin.fileno(), payload[offset:])
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError) as error:
                            raise RuntimeError(
                                "Rust lifecycle boundary rejected the request transport"
                            ) from error
                        if written <= 0:
                            raise RuntimeError("Rust lifecycle boundary closed request transport")
                        offset += written
                        if offset == len(payload):
                            selector.unregister(stdin)
                            stdin.close()
                        continue
                    try:
                        chunk = os.read(key.fileobj.fileno(), 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream = output[key.data]
                    if len(stream) + len(chunk) > _TRANSPORT_OUTPUT_LIMIT:
                        process.kill()
                        process.wait()
                        raise RuntimeError(
                            "Rust lifecycle boundary exceeded the 64 KiB transport output limit"
                        )
                    stream.extend(chunk)
        finally:
            selector.close()
            for stream in (stdin, stdout, stderr):
                if not stream.closed:
                    stream.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        process.wait()
        stdout_value = bytes(output["stdout"]).decode()
        stderr_value = bytes(output["stderr"]).decode()
        if process.returncode != 0:
            raise RuntimeError(
                f"Rust lifecycle boundary failed rc={process.returncode}: "
                f"{stderr_value.strip()}"
            )
        try:
            value = json.loads(stdout_value)
        except json.JSONDecodeError as error:
            raise RuntimeError("Rust lifecycle boundary returned invalid JSON") from error
        if not isinstance(value, dict):
            raise RuntimeError("Rust lifecycle boundary response is not an object")
        return value


def policy_path(repository_root: Path) -> Path:
    """Return the review policy; parsing/validation remains Rust-owned."""

    return repository_root / "lifecycle" / "operation-boundary-v1.json"

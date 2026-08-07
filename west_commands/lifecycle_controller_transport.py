"""Bounded transport-only adapter for the Rust lifecycle controller.

The adapter owns no lifecycle authority.  It validates the protocol envelope,
holds the executable by FD before spawning, passes inherited anchor FDs, and
returns only a schema-validated response with the same transaction/closure
handshake.  All timeout cleanup is limited to the controller process group.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, ValidationError


class LifecycleControllerTransportError(RuntimeError):
    """The bounded controller transport failed closed."""


MAX_TRANSPORT_INPUT_BYTES = 64 * 1024
MAX_TRANSPORT_OUTPUT_BYTES = 64 * 1024
MAX_TRANSPORT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class LifecycleControllerTransport:
    binary: Path
    response_schema: Path
    expected_controller_closure_sha256: str
    expected_runtime_identity_digest: str
    request_nonce: str
    timeout_seconds: float = 5.0
    max_input_bytes: int = 64 * 1024
    max_output_bytes: int = 64 * 1024

    def _validate_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(request)
        for field, expected in (
            ("controller_closure_sha256", self.expected_controller_closure_sha256),
            ("runtime_identity_digest", self.expected_runtime_identity_digest),
            ("request_nonce", self.request_nonce),
        ):
            if value.get(field) != expected:
                raise LifecycleControllerTransportError(f"controller {field} handshake mismatch")
        if not isinstance(value.get("transaction_id"), str) or not value["transaction_id"]:
            raise LifecycleControllerTransportError("controller transaction identity is missing")
        if value.get("profile") != "rootless" or value.get("operation") != "REQUEST_SHUTDOWN":
            raise LifecycleControllerTransportError("controller request domain is unsupported")
        for field in ("anchor_fd", "evidence_fd"):
            descriptor = value.get(field)
            if descriptor is not None and (type(descriptor) is not int or descriptor < 0):
                raise LifecycleControllerTransportError("controller request FD is malformed")
        return value

    def _open_executable(self) -> int:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.binary,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not (info.st_mode & stat.S_IXUSR):
                os.close(descriptor)
                descriptor = None
                raise LifecycleControllerTransportError("Rust controller executable is not a regular executable")
            return descriptor
        except (OSError, TypeError) as error:
            if descriptor is not None:
                os.close(descriptor)
            raise LifecycleControllerTransportError("Rust controller executable cannot be retained") from error

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired as error:
            process.kill()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired as hard_error:
                raise LifecycleControllerTransportError("controller process did not terminate within unwind budget") from hard_error
            raise LifecycleControllerTransportError("controller process exceeded transport deadline") from error

    def _validate_response(self, request: Mapping[str, Any], response: object) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise LifecycleControllerTransportError("controller response is not an object")
        try:
            schema = json.loads(self.response_schema.read_text())
            Draft202012Validator(schema).validate(response)
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise LifecycleControllerTransportError("controller response schema validation failed") from error
        for field in ("transaction_id", "controller_closure_sha256", "runtime_identity_digest", "request_nonce"):
            if response.get(field) != request.get(field):
                raise LifecycleControllerTransportError(f"controller response {field} mismatch")
        return response

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        inherited_fds: Iterable[int] = (),
    ) -> dict[str, Any]:
        value = self._validate_request(request)
        try:
            payload = (json.dumps(value, sort_keys=True) + "\n").encode()
        except (TypeError, ValueError) as error:
            raise LifecycleControllerTransportError("controller request is not JSON encodable") from error
        if type(self.timeout_seconds) is not float or not 0 < self.timeout_seconds <= MAX_TRANSPORT_TIMEOUT_SECONDS:
            raise LifecycleControllerTransportError("transport timeout exceeds hard bound")
        if type(self.max_input_bytes) is not int or not 0 < self.max_input_bytes <= MAX_TRANSPORT_INPUT_BYTES:
            raise LifecycleControllerTransportError("controller input budget exceeds policy ceiling")
        if type(self.max_output_bytes) is not int or not 0 < self.max_output_bytes <= MAX_TRANSPORT_OUTPUT_BYTES:
            raise LifecycleControllerTransportError("controller output budget exceeds policy ceiling")
        if len(payload) > self.max_input_bytes:
            raise LifecycleControllerTransportError("controller request exceeds transport budget")
        try:
            inherited = tuple(inherited_fds)
            if any(type(fd) is not int for fd in inherited):
                raise ValueError("non-integer descriptor")
            if len(set(inherited)) != len(inherited):
                raise ValueError("duplicate descriptor")
            fds = tuple(sorted(inherited))
            expected_fds = {int(request["anchor_fd"])}
            if request.get("evidence_fd") is not None:
                expected_fds.add(int(request["evidence_fd"]))
        except (KeyError, TypeError, ValueError) as error:
            raise LifecycleControllerTransportError("controller FD envelope is malformed") from error
        if any(fd < 0 for fd in fds):
            raise LifecycleControllerTransportError("controller inherited FD is invalid")
        if request.get("evidence_fd") == request.get("anchor_fd"):
            raise LifecycleControllerTransportError("controller anchor/evidence capabilities must be distinct")
        if set(fds) != expected_fds:
            raise LifecycleControllerTransportError("controller inherited FD set is not exact")

        executable_fd = self._open_executable()
        process: subprocess.Popen[bytes] | None = None
        streams: tuple[Any, ...] = ()
        selector = selectors.DefaultSelector()
        output = {"stdout": bytearray(), "stderr": bytearray()}
        offset = 0
        try:
            # The held descriptor, not the mutable binary pathname, is the
            # executable identity.  close_fds remains true for all other FDs.
            executable = f"/proc/self/fd/{executable_fd}"
            process = subprocess.Popen(
                [executable],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=tuple(sorted(set(fds + (executable_fd,)))),
                start_new_session=True,
            )
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            streams = (process.stdin, process.stdout, process.stderr)
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            deadline = time.monotonic() + self.timeout_seconds
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LifecycleControllerTransportError("controller transport deadline exceeded")
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        if offset == len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        try:
                            offset += os.write(key.fileobj.fileno(), payload[offset:])
                        except BlockingIOError:
                            continue
                        continue
                    try:
                        chunk = os.read(key.fileobj.fileno(), 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream = output[key.data]
                    total_output = len(output["stdout"]) + len(output["stderr"])
                    if total_output + len(chunk) > self.max_output_bytes:
                        raise LifecycleControllerTransportError("controller response exceeds transport budget")
                    stream.extend(chunk)
        except BaseException:
            if process is not None:
                self._terminate(process)
            raise
        finally:
            selector.close()
            for stream in streams:
                if not stream.closed:
                    stream.close()
            os.close(executable_fd)
        assert process is not None
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired as error:
            self._terminate(process)
            raise LifecycleControllerTransportError("controller did not exit within wait budget") from error
        if process.returncode != 0:
            raise LifecycleControllerTransportError(
                f"Rust lifecycle controller failed: {bytes(output['stderr']).decode(errors='replace').strip()}"
            )
        try:
            response = json.loads(bytes(output["stdout"]).decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LifecycleControllerTransportError("controller response is not JSON") from error
        return self._validate_response(value, response)

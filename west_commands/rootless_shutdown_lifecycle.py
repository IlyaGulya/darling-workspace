"""Rootless shutdown consumer bridge for dar-4ush.7.

This module is deliberately only a domain adapter.  It does not discover
processes, send signals, inspect prefixes, or implement a second reducer.  A
Rootless acceptance producer supplies an already captured lifecycle trace and
the Rust boundary remains the sole authority for replay and recovery
semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from west_commands.lifecycle_operation_boundary import RustBoundaryAdapter


class RootlessShutdownConsumerError(ValueError):
    """The supplied trace is not a Rootless shutdown trace."""


_REQUIRED_EVENT_KINDS = {"intent_declared", "signal_sent", "terminal"}


def _rootless_envelope(trace: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Validate shared Rootless provenance and return the typed event envelope."""

    provenance = trace.get("provenance")
    identity = provenance.get("source_identity") if isinstance(provenance, Mapping) else None
    if not isinstance(provenance, Mapping) or provenance.get("kind") not in {
        "golden-scenario",
        "historical-observation",
    }:
        raise RootlessShutdownConsumerError("Rootless shutdown provenance is missing or invalid")
    if not isinstance(identity, Mapping) or identity.get("profile") != "rootless":
        raise RootlessShutdownConsumerError("Rootless shutdown provenance requires profile=rootless")
    if identity.get("module") != "rootless-shutdown":
        raise RootlessShutdownConsumerError(
            "Rootless shutdown provenance requires module=rootless-shutdown"
        )
    evidence_id = provenance.get("evidence_id")
    if not isinstance(evidence_id, str) or not evidence_id.startswith(
        "dar-4ush/rootless-shutdown/"
    ):
        raise RootlessShutdownConsumerError(
            "Rootless shutdown provenance requires a rootless-shutdown evidence id"
        )
    scenario = trace.get("scenario")
    if not isinstance(scenario, str) or not scenario.startswith("rootless-shutdown-"):
        raise RootlessShutdownConsumerError(
            "Rootless shutdown trace requires a rootless-shutdown scenario"
        )

    events = trace.get("events")
    if not isinstance(events, list) or not events:
        raise RootlessShutdownConsumerError("Rootless shutdown trace has no events")
    if any(not isinstance(event, Mapping) for event in events):
        raise RootlessShutdownConsumerError("Rootless shutdown trace contains an invalid event")
    typed_events = [event for event in events if isinstance(event, Mapping)]
    if typed_events[-1].get("kind") != "terminal":
        raise RootlessShutdownConsumerError("Rootless shutdown trace must end in terminal")
    return typed_events


def _require_request_shutdown(events: list[Mapping[str, Any]]) -> None:
    intent_events = [event for event in events if event.get("kind") == "intent_declared"]
    if not intent_events or any(
        not isinstance(event.get("data"), Mapping)
        or event["data"].get("intent") != "REQUEST_SHUTDOWN"
        for event in intent_events
    ):
        raise RootlessShutdownConsumerError(
            "Rootless shutdown requires intent=REQUEST_SHUTDOWN"
        )


def _consumer_shape(trace: Mapping[str, Any]) -> None:
    """Check the signal-bearing Rootless domain envelope."""

    events = _rootless_envelope(trace)
    kinds = {event.get("kind") for event in events}
    missing = _REQUIRED_EVENT_KINDS - kinds
    if missing:
        raise RootlessShutdownConsumerError(
            f"Rootless shutdown trace is missing events: {sorted(missing)}"
        )
    _require_request_shutdown(events)
    signals = [
        event
        for event in events
        if event.get("kind") == "signal_sent"
    ]
    if not any(
        isinstance(event.get("data"), Mapping)
        and isinstance(event["data"].get("capability"), str)
        and event["data"]["capability"].startswith("cap.")
        for event in signals
    ):
        raise RootlessShutdownConsumerError(
            "Rootless shutdown signal must reference a capability"
        )


def _gone_consumer_shape(trace: Mapping[str, Any]) -> None:
    """Check the signal-free already-GONE Rootless domain envelope."""

    events = _rootless_envelope(trace)
    _require_request_shutdown(events)
    if any(event.get("kind") == "signal_sent" for event in events):
        raise RootlessShutdownConsumerError(
            "Rootless GONE consumer rejects signal-bearing traces"
        )
    initial = trace.get("initial")
    catalog = initial.get("capability_catalog") if isinstance(initial, Mapping) else None
    root_entries = [
        entry
        for entry in catalog or []
        if isinstance(entry, Mapping) and entry.get("kind") == "SESSION_ROOT_PIDFD"
    ]
    if len(root_entries) != 1 or not isinstance(root_entries[0].get("capability_id"), str):
        raise RootlessShutdownConsumerError(
            "Rootless GONE shutdown requires one authoritative SESSION_ROOT_PIDFD"
        )
    root_capability = root_entries[0]["capability_id"]
    gone_events = [
        event
        for event in events
        if event.get("kind") == "identity_revalidated"
        and isinstance(event.get("data"), Mapping)
        and event["data"].get("result") == "GONE"
    ]
    if not any(
        event["data"].get("capability") == root_capability for event in gone_events
    ):
        raise RootlessShutdownConsumerError(
            "Rootless GONE shutdown requires identity_revalidated=GONE for the session root"
        )


@dataclass(frozen=True)
class RootlessShutdownSignalConsumer:
    """Route signal-bearing Rootless shutdown observations to Rust."""

    repository_root: Path
    adapter: RustBoundaryAdapter | None = None

    def replay(self, trace: Mapping[str, Any]) -> dict[str, Any]:
        _consumer_shape(trace)
        adapter = self.adapter or RustBoundaryAdapter(self.repository_root)
        result = adapter.invoke({"op": "replay_trace", "trace": dict(trace)})
        if result.get("trace_id") != trace.get("trace_id"):
            raise RootlessShutdownConsumerError(
                "Rust lifecycle boundary returned a different trace identity"
            )
        return result


@dataclass(frozen=True)
class RootlessShutdownGoneConsumer:
    """Route signal-free already-GONE Rootless observations to Rust."""

    repository_root: Path
    adapter: RustBoundaryAdapter | None = None

    def replay(self, trace: Mapping[str, Any]) -> dict[str, Any]:
        _gone_consumer_shape(trace)
        adapter = self.adapter or RustBoundaryAdapter(self.repository_root)
        result = adapter.invoke({"op": "replay_trace", "trace": dict(trace)})
        if result.get("trace_id") != trace.get("trace_id"):
            raise RootlessShutdownConsumerError(
                "Rust lifecycle boundary returned a different trace identity"
            )
        return result

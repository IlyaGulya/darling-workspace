"""Typed resource providers for ``west test`` metadata runs.

This module owns resource selection and ordering.  The concrete provider bodies
still call back into ``DarlingTest`` while the migration is in progress; keeping
the registry here gives each resource a domain name and a single place to move
implementation details into as the runner is split further.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceProvider:
    """A resource that may wrap a metadata test invocation."""

    name: str

    def active(self, invocation: dict[str, Any]) -> bool:
        raise NotImplementedError

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        raise NotImplementedError


class DccCacheProvider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("dcc-cache")

    def active(self, invocation: dict[str, Any]) -> bool:
        return invocation.get("dcc_cache") is not None

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        return command._dcc_cache_context(invocation, env)


class EunionPrefixProvider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("darling-eunion-prefix")

    def active(self, invocation: dict[str, Any]) -> bool:
        return self.name in set(invocation.get("requires_resources", []))

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        return command._eunion_prefix_context(invocation, env)


class HostTraceProvider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("host-trace-files")

    def active(self, invocation: dict[str, Any]) -> bool:
        return bool(invocation.get("host_trace_files"))

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        return command._host_trace_context(invocation, env)


class HostTempProvider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("host-temp-files")

    def active(self, invocation: dict[str, Any]) -> bool:
        return bool(invocation.get("host_temp_files"))

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        return command._host_temp_context(invocation, env)


class HostStatProvider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("host-stat-deltas")

    def active(self, invocation: dict[str, Any]) -> bool:
        return bool(invocation.get("host_stat_deltas"))

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        return command._host_stat_context(invocation, env)


RESOURCE_PROVIDERS: tuple[ResourceProvider, ...] = (
    HostTempProvider(),
    HostTraceProvider(),
    HostStatProvider(),
    DccCacheProvider(),
    EunionPrefixProvider(),
)


def active_resource_provider_names(invocation: dict[str, Any]) -> list[str]:
    """Return provider names in the order they will wrap a test invocation."""

    return [
        provider.name
        for provider in RESOURCE_PROVIDERS
        if provider.active(invocation)
    ]


@contextmanager
def resource_context(command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
    """Build the nested resource context for a metadata test invocation."""

    active = [
        provider
        for provider in RESOURCE_PROVIDERS
        if provider.active(invocation)
    ]
    if not active:
        yield env
        return

    current_env = env
    with ExitStack() as stack:
        for provider in active:
            provided_env = stack.enter_context(
                provider.context(command, invocation, current_env)
            )
            if provided_env is not None:
                current_env = provided_env
        yield current_env

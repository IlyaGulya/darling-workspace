"""Typed resource providers for ``west test`` metadata runs.

This module owns resource selection and ordering.  The concrete provider bodies
still call back into ``DarlingTest`` while the migration is in progress; keeping
the registry here gives each resource a domain name and a single place to move
implementation details into as the runner is split further.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

try:
    from .test_stock_stack_cache import stock_stack_context
    from .test_execution import run_bounded
except ImportError:
    from test_stock_stack_cache import stock_stack_context
    from test_execution import run_bounded


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


class DescriptorTraceProvider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("descriptor-trace")

    def active(self, invocation: dict[str, Any]) -> bool:
        return bool(invocation.get("descriptor_trace"))

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        return command._descriptor_trace_context(invocation, env)


class HomebrewLz4Provider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("homebrew-lz4")

    def active(self, invocation: dict[str, Any]) -> bool:
        return self.name in set(invocation.get("requires_resources", []))

    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        # Setup restores an identity-matched stock-stack snapshot or stages the
        # pinned inputs exactly as before; teardown captures a complete stack.
        return stock_stack_context(command, invocation, env)


class DserverRpcProvider(ResourceProvider):
    def __init__(self) -> None:
        super().__init__("dserver-rpc")

    def active(self, invocation: dict[str, Any]) -> bool:
        return self.name in set(invocation.get("requires_resources", []))

    @contextmanager
    def context(self, command: Any, invocation: dict[str, Any], env: dict[str, str] | None):
        server = command._project_path("darling/src/external/darlingserver")
        product = command._project_path("darling")
        xnu = command._project_path("darling/src/external/xnu")
        child_env = dict(os.environ if env is None else env)
        with tempfile.TemporaryDirectory(prefix="west-dserver-rpc-") as scratch:
            root = Path(scratch)
            public = root / "include/darlingserver/rpc.h"
            internal = root / "internal-include/darlingserver/rpc.internal.h"
            source = root / "src/rpc.c"
            for output in (public, internal, source):
                output.parent.mkdir(parents=True, exist_ok=True)
            generated = run_bounded(
                [sys.executable, str(server / "scripts/generate-rpc-wrappers.py"),
                 str(public), str(internal), str(source),
                 '"../include/darlingserver/rpc.h"'],
                cwd=server,
                env=child_env,
                timeout_seconds=int(invocation.get("timeout_seconds", 600)),
            )
            if generated.returncode:
                command._record_failure_phase(invocation, "setup")
                command.die(f"{invocation['name']}: generating production RPC headers failed")
            overlay = root / "prefix/darling"
            overlay.mkdir(parents=True)
            (overlay / "emulation").symlink_to(
                xnu / "darling/src/libsystem_kernel/emulation/include",
                target_is_directory=True,
            )
            # CPATH follows explicit fixture -I directories, preserving the
            # external-boundary adapters while using the real protocol ABI.
            includes = [
                str(public.parents[1]), str(server / "include"),
                str(product / "src/startup/mldr/elfcalls"), str(overlay.parent),
            ]
            if child_env.get("CPATH"):
                includes.append(child_env["CPATH"])
            child_env["CPATH"] = os.pathsep.join(includes)
            yield child_env


RESOURCE_PROVIDERS: tuple[ResourceProvider, ...] = (
    HostTempProvider(),
    HostTraceProvider(),
    HostStatProvider(),
    DescriptorTraceProvider(),
    DserverRpcProvider(),
    DccCacheProvider(),
    EunionPrefixProvider(),
    HomebrewLz4Provider(),
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

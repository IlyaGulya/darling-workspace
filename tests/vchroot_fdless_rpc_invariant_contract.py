#!/usr/bin/env python3
"""FD-bearing RPC invariant for the descriptor-free vchroot migration.

darlingserver's scripts/generate-rpc-wrappers.py is the single source of truth
for which RPCs transport file descriptors: a parameter whose type is the
literal '@fd' is transferred as an SCM_RIGHTS descriptor by the library, and it
can appear in the call direction, in the reply direction, or both.

The contract is semantic rather than a raw count, so unrelated RPCs may join or
leave the descriptor-bearing set without breaking it:

  * vchroot must not carry a descriptor in either direction, and
  * every call listed below must still carry one, which is what keeps a global
    "descriptors stopped working" regression from passing silently.

This is a source-contract audit (coverage-tier: source), not behavioral
coverage.  The observable runtime fact that the guest vchroot call sends zero
SCM_RIGHTS is a separate, deferred acceptance gate; this contract only proves
that the RPC schema no longer declares a descriptor for vchroot and that the
other descriptor-bearing calls are untouched.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

# Calls that must keep transporting a descriptor, together with the direction
# the descriptor travels in.  Asserted one by one so that a silently dropped
# descriptor fails this contract even while the vchroot assertion still passes.
UNAFFECTED_FD_BEARING_CALLS = {
    "checkin": "call",
    "checkout": "call",
    "console_open": "reply",
    "debug_list_members": "reply",
    "debug_list_messages": "reply",
    "debug_list_ports": "reply",
    "debug_list_processes": "reply",
    "kqchan_mach_port_open": "reply",
    "kqchan_proc_open": "reply",
}

DESCRIPTOR_TYPE = "@fd"
MIGRATED_CALL = "vchroot"
SCHEMA_NAME = "calls"
SCHEMA_FILE = "scripts/generate-rpc-wrappers.py"


def schema_list(tree: ast.AST) -> ast.List:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == SCHEMA_NAME and isinstance(node.value, ast.List):
                    return node.value
    raise SystemExit(f"contract error: no module-level '{SCHEMA_NAME}' list in the RPC schema")


def descriptor_parameters(params: ast.AST) -> list[str]:
    if not isinstance(params, (ast.List, ast.Tuple)):
        return []
    names = []
    for param in params.elts:
        if not isinstance(param, ast.Tuple) or len(param.elts) < 2:
            continue
        type_node = param.elts[1]
        if (
            isinstance(type_node, ast.Constant)
            and type_node.value == DESCRIPTOR_TYPE
            and isinstance(param.elts[0], ast.Constant)
        ):
            names.append(str(param.elts[0].value))
    return names


def descriptor_bearing(calls: ast.List) -> dict[str, dict[str, list[str]]]:
    """call name -> {direction: descriptor parameter names}."""
    found: dict[str, dict[str, list[str]]] = {}
    for entry in calls.elts:
        if not isinstance(entry, ast.Tuple) or len(entry.elts) < 2:
            continue
        name_node = entry.elts[0]
        if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
            continue
        directions: dict[str, list[str]] = {}
        for direction, index in (("call", 1), ("reply", 2)):
            if len(entry.elts) <= index:
                continue
            names = descriptor_parameters(entry.elts[index])
            if names:
                directions[direction] = names
        if directions:
            found[name_node.value] = directions
    return found


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    dserver = pathlib.Path(
        os.environ.get("DSERVER_SRC_ROOT", root.parent / "darling" / "src" / "external" / "darlingserver")
    )
    schema = dserver / SCHEMA_FILE
    if not schema.is_file():
        print(f"FAIL  rpc schema not found: {schema}")
        return 1

    bearing = descriptor_bearing(schema_list(ast.parse(schema.read_text())))
    failures = 0

    migrated = bearing.get(MIGRATED_CALL)
    if migrated:
        described = ", ".join(f"{direction}={','.join(names)}" for direction, names in migrated.items())
        print(f"FAIL  {MIGRATED_CALL} still transports a descriptor ({described})")
        failures += 1
    else:
        print(f"PASS  {MIGRATED_CALL} transports no descriptor in either direction")

    for name, direction in sorted(UNAFFECTED_FD_BEARING_CALLS.items()):
        found = bearing.get(name, {}).get(direction)
        if found:
            print(f"PASS  {name} still transports a descriptor in its {direction} ({', '.join(found)})")
        else:
            print(f"FAIL  {name} no longer transports a {direction} descriptor")
            failures += 1

    if not bearing:
        print("FAIL  no call transports a descriptor: descriptor transport looks globally removed")
        failures += 1

    print(f"INFO  descriptor-bearing calls: {len(bearing)} -> {', '.join(sorted(bearing))}")

    if failures:
        print(f"VCHROOT_FDLESS_RPC_INVARIANT_FAILED ({failures} failure(s))")
        return 1
    print("VCHROOT_FDLESS_RPC_INVARIANT_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

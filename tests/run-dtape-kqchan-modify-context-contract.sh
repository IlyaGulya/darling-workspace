#!/usr/bin/env bash
# dar-dtape-explicit-context-6to3.4a focused host/source contract.
#
# The kqchan Mach-port MODIFY (touch) path carries no ambient execution-context
# consumer, which is why the fake impersonate()/impersonate(nullptr) pair around
# it is removable. Concretely, over the closure the server actually runs:
#
#   Kqchan::MachPort::_modify            (src/kqchan.cpp)
#     -> dtape_kqchan_mach_port_modify   (duct-tape/src/kqchan.c)
#          -> filt_machporttouch         (duct-tape/xnu/osfmk/ipc/ipc_pset.c)
#
# none of those bodies may read current_thread()/current_task()/thread_self_trap/
# currentProcess()/currentThread(), and the product call site must not call
# impersonate(). Comments and string literals are ignored: the .4a change records
# the property in the code itself, so a mention must not count as a consumer.
#
# The contract is only meaningful with its boundary: the FILL/READ path
# (filt_machportprocess, and Kqchan::MachPort::_read) DOES consume the requester
# thread, so that side still carries an explicit requester (owned by
# dar-dtape-explicit-context-6to3.4b). Asserting the boundary here means the
# contract fails loudly if the extractor drifts or if both sides are silently
# stripped, instead of passing vacuously.
set -euo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
darling_root="${DARLING_SRC_ROOT:-$workspace_root/../darling}"

if [ ! -d "$darling_root" ]; then
	echo "dtape-kqchan-modify-context: darling source not found: $darling_root" >&2
	exit 2
fi

dserver="$darling_root/src/external/darlingserver"
for f in src/kqchan.cpp duct-tape/src/kqchan.c duct-tape/xnu/osfmk/ipc/ipc_pset.c; do
	if [ ! -f "$dserver/$f" ]; then
		echo "dtape-kqchan-modify-context: missing source file: $dserver/$f" >&2
		exit 2
	fi
done

python3 -B - "$dserver" <<'PY'
import re
import sys
from pathlib import Path

dserver = Path(sys.argv[1])


def mask(text):
    """Blank comments and string/char literals, preserving offsets.

    The kqchan log lines embed braces inside string literals ("{receiveBuffer=")
    and the .4a comment names current_thread()/impersonate(); both must not be
    read as code or they corrupt brace counting and the token scan.
    """
    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                out[index] = " "
                index += 1
        elif character == "/" and index + 1 < length and text[index + 1] == "*":
            out[index] = out[index + 1] = " "
            index += 2
            while index < length and not (text[index] == "*" and index + 1 < length and text[index + 1] == "/"):
                if text[index] != "\n":
                    out[index] = " "
                index += 1
            if index < length:
                out[index] = out[index + 1] = " "
                index += 2
        elif character in "\"'":
            quote = character
            out[index] = " "
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\":
                    out[index] = " "
                    index += 1
                    if index < length:
                        out[index] = " "
                        index += 1
                    continue
                if text[index] != "\n":
                    out[index] = " "
                index += 1
            if index < length:
                out[index] = " "
                index += 1
        else:
            index += 1
    return "".join(out)


def bodies(path, signature):
    """Return (raw, masked) for the function body starting at signature."""
    text = (dserver / path).read_text()
    masked_text = mask(text)
    start = text.find(signature)
    if start < 0:
        raise SystemExit(f"dtape-kqchan-modify-context: signature not found in {path}: {signature}")
    open_index = masked_text.find("{", start)
    if open_index < 0:
        raise SystemExit(f"dtape-kqchan-modify-context: no body brace for {signature}")
    depth = 0
    for index in range(open_index, len(masked_text)):
        character = masked_text[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[open_index : index + 1], masked_text[open_index : index + 1]
    raise SystemExit(f"dtape-kqchan-modify-context: unterminated body for {signature}")


CONTEXT = re.compile(r"\bcurrent_thread\s*\(|\bcurrent_task\s*\(|thread_self_trap\b|"
                     r"\bcurrentProcess\s*\(|\bcurrentThread\s*\(")

modify_raw, modify = bodies("src/kqchan.cpp", "DarlingServer::Kqchan::MachPort::_modify(")
touch_raw, touch = bodies("duct-tape/src/kqchan.c", "void dtape_kqchan_mach_port_modify(")
_, touch_filter = bodies("duct-tape/xnu/osfmk/ipc/ipc_pset.c", "filt_machporttouch(")

# Boundary with .4b (dar-dtape-explicit-context-6to3.5): the read/fill path now carries
# the requester EXPLICITLY (tests/run-dtape-kqchan-fill-context-contract.sh owns that
# behavioral proof). The old hidden transport -- impersonate() around the read -- must be
# gone, or this .4a contract would be satisfied by a partial revert that re-hides it.
read_raw, read_path = bodies("src/kqchan.cpp", "DarlingServer::Kqchan::MachPort::_read(")
# The ordinary entrypoint keeps its ambient signature and forwards current_thread(); the
# explicit helper must exist for the kqchan path to call.
_, fill_filter = bodies("duct-tape/xnu/osfmk/ipc/ipc_pset.c", "filt_machportprocess(")

failures = []

if "filt_machporttouch(" not in touch_raw:
    failures.append("dtape_kqchan_mach_port_modify no longer calls filt_machporttouch")
for label, text in (("Kqchan::MachPort::_modify", modify),
                    ("dtape_kqchan_mach_port_modify", touch),
                    ("filt_machporttouch", touch_filter)):
    hit = CONTEXT.search(text)
    if hit:
        failures.append(f"{label} reads ambient context: {hit.group(0)}")
if "impersonate" in modify:
    failures.append("Kqchan::MachPort::_modify still calls impersonate()")

# Boundary: the ambient entrypoint still forwards current_thread(); the .4b path must not
# impersonate, and the explicit helper must be present (the fill must call it).
if not CONTEXT.search(fill_filter):
    failures.append("boundary lost: the ambient filt_machportprocess entrypoint no longer forwards current_thread()")
if "filt_machportprocess_on_thread" not in (dserver / "duct-tape/xnu/osfmk/ipc/ipc_pset.c").read_text():
    failures.append("boundary lost: filt_machportprocess_on_thread is absent")
with (dserver / "duct-tape/src/kqchan.c").open() as handle:
    if "filt_machportprocess_on_thread(" not in handle.read():
        failures.append("boundary lost: dtape_kqchan_mach_port_fill does not call the explicit filter")
if "impersonate" in read_path:
    failures.append("Kqchan::MachPort::_read still carries the hidden impersonate() transport (owned by .4b)")

if failures:
    for failure in failures:
        print(f"DTAPE-KQCHAN-MODIFY-CONTEXT FAIL: {failure}", file=sys.stderr)
    raise SystemExit(1)

print("DTAPE-KQCHAN-MODIFY-CONTEXT PASS: touch path is context-free; read path carries an explicit requester")
PY

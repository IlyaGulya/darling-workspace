"""Runtime identity for a Ring claim.

WHY THIS EXISTS: a prefix was once accepted as a Ring product purely because the provider it was
bootstrapped with was *named* like one. The provider actually selected a legacy source profile whose
runtime had no Ring transport at all, and 9/9 plus 199/200 boots were recorded against it. A name is
not a witness.

The witness is the runtime's own recorded configuration:

* PRIMARY -- the retained runtime identity's ``ring-defines`` (``DARLING_RING_TRANSPORT`` and
  ``DSERVER_RING_TRANSPORT`` as they were at BUILD time), plus ``source-mode`` so a manifest-native
  product is distinguishable from a legacy one.
* CORROBORATION -- the deployed artifact byte identity (the receipt hashes) and, when asked, the
  Ring-era symbols present in the deployed loader/server. String evidence alone is deliberately NOT
  sufficient: build metadata already states the defines, and a grep answers a different question
  ("does this file contain this text") than "was this runtime built with Ring".
* REFUSAL -- an unknown or absent define is NOT "off" and NOT "on": it is ``unknown``, and a caller
  that wants to label a result RING must refuse.

No new framework: these are pure functions over values the runtime profile already records.
"""

from __future__ import annotations

from typing import Any

RING_ON = "on"
RING_OFF = "off"
RING_UNKNOWN = "unknown"

_RING_DEFINES = ("DARLING_RING_TRANSPORT", "DSERVER_RING_TRANSPORT")


def _truthy(value: Any) -> bool | None:
    """Interpret a CMake boolean as it was actually recorded."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "on", "true", "yes", "y"}:
            return True
        if lowered in {"0", "off", "false", "no", "n", ""}:
            return False
    return None


def ring_status(identity: dict[str, Any] | None, *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify a runtime as RING on/off/unknown from its own recorded configuration.

    ``identity`` is the retained runtime identity (or the runtime profile marker's ``fingerprint``);
    ``receipt`` is optional and only corroborates that a deployment exists at all.
    """

    result: dict[str, Any] = {
        "status": RING_UNKNOWN,
        "witnesses": {},
        "refusals": [],
    }
    if not isinstance(identity, dict):
        result["refusals"].append("no runtime identity was recorded")
        return result

    defines = identity.get("ring-defines")
    if not isinstance(defines, dict):
        # A legacy identity recorded no ring-defines at all: that is UNKNOWN, not off. The
        # distinction matters because "off" is a statement about a build and "unknown" is a
        # statement about the evidence.
        result["refusals"].append(
            "runtime identity records no ring-defines; Ring status cannot be established from it"
        )
        result["witnesses"]["source-mode"] = identity.get("source-mode")
        result["witnesses"]["source-profile"] = identity.get("source-profile")
        if isinstance(receipt, dict):
            result["witnesses"]["receipt-artifacts"] = len(receipt.get("artifacts", []))
        return result

    values = {}
    for name in _RING_DEFINES:
        value = _truthy(defines.get(name))
        values[name] = value
        result["witnesses"][name] = defines.get(name)
        if value is None:
            result["refusals"].append(f"{name} is not recorded as a boolean")
    if any(value is None for value in values.values()):
        return result

    if all(values.values()):
        result["status"] = RING_ON
    elif not any(values.values()):
        result["status"] = RING_OFF
    else:
        # One switch on and the other off is not a Ring runtime; it is a configuration that must
        # never be labelled either way.
        result["refusals"].append(
            "the two Ring switches disagree: "
            + ", ".join(f"{name}={values[name]}" for name in _RING_DEFINES)
        )
    result["witnesses"]["source-mode"] = identity.get("source-mode")
    result["witnesses"]["manifest-commit"] = identity.get("manifest-commit")
    if isinstance(receipt, dict):
        result["witnesses"]["receipt-artifacts"] = len(receipt.get("artifacts", []))
    return result


def require_ring_runtime(identity: dict[str, Any] | None, *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the Ring status, refusing to proceed when it is not established as ON."""

    status = ring_status(identity, receipt=receipt)
    if status["status"] != RING_ON:
        reasons = "; ".join(status["refusals"]) or "the recorded configuration says Ring is off"
        raise ValueError(
            "refusing to label this runtime a Ring product: "
            f"status={status['status']} ({reasons})"
        )
    return status

"""The checked-in registries regenerate from their derivation inputs.

Each generator under scripts/ derives its registry from the tree and compares
it to the checked-in file.  This contract proves the positive direction (every
registry is current on this tree, byte for byte) and the negative direction
(a deliberately drifted number, a flipped digest, or a stale entry makes the
corresponding check fail naming the field and leaves the file alone).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
LEGACY = ROOT / "locks" / "patch-stack" / "legacy-runtime-profile-inventory-v1.json"
FORENSIC = ROOT / "locks" / "patch-stack" / "archive-forensic-exceptions-v1.yml"
INVENTORY = ROOT / "lifecycle" / "namespace-writer-inventory-v1.json"
AUDIT = ROOT / "lifecycle" / "namespace-writer-candidate-audit-v1.json"

TIMEOUT = 900


def fail(message: str) -> None:
    raise AssertionError(message)


def run_generator(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), *arguments],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        timeout=TIMEOUT,
    )


def require_current(script: str, *arguments: str) -> str:
    """Run a generator check and require a clean, field-complete pass."""

    result = run_generator(script, *arguments, "--check")
    if result.returncode != 0:
        fail(f"{script}: check failed on the current tree:\n{result.stdout}{result.stderr}")
    if "derivation PASS" not in result.stdout:
        fail(f"{script}: check passed without the derivation marker: {result.stdout!r}")
    return result.stdout


def require_drift(
    script: str, arguments: list[str], field: str, fixture: Path, expected: str
) -> None:
    """Run a generator check against a drifted fixture and require the named field."""

    before = fixture.read_bytes()
    result = run_generator(script, *arguments, "--check")
    if result.returncode == 0:
        fail(f"{script}: drifted {fixture.name} was accepted: {result.stdout!r}")
    message = result.stdout + result.stderr
    if field not in message:
        fail(f"{script}: drift report does not name {field!r}:\n{message}")
    if expected not in message:
        fail(f"{script}: drift report does not show {expected}:\n{message}")
    if fixture.read_bytes() != before:
        fail(f"{script}: a failing check rewrote {fixture.name}")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def legacy_inventory() -> dict:
    return json.loads(LEGACY.read_text(encoding="utf-8"))


def candidate_audit() -> dict:
    return json.loads(AUDIT.read_text(encoding="utf-8"))


def main() -> None:
    require_current("generate_legacy_runtime_inventory.py")
    require_current("generate_archive_forensic_exceptions.py")
    require_current("generate_namespace_writer_registry.py")

    with tempfile.TemporaryDirectory(prefix="registry-derivation-contract-") as temporary:
        scratch = Path(temporary)

        drifted = legacy_inventory()
        drifted["profiles"][1]["ordered_commit_count"] += 1
        legacy_number = scratch / "legacy-number.json"
        write_json(legacy_number, drifted)
        require_drift(
            "generate_legacy_runtime_inventory.py",
            ["--registry", str(legacy_number)],
            "profiles[1].ordered_commit_count",
            legacy_number,
            "registry=",
        )

        stale = legacy_inventory()
        stale["profiles"][0]["modules"].append(
            {"path": "darling/src/external/vanished", "series_count": 1, "ordered_commit_count": 1}
        )
        legacy_stale = scratch / "legacy-stale.json"
        write_json(legacy_stale, stale)
        require_drift(
            "generate_legacy_runtime_inventory.py",
            ["--registry", str(legacy_stale)],
            "profiles[0].modules[8].path",
            legacy_stale,
            "only in registry",
        )

        forensic = FORENSIC.read_text(encoding="utf-8").replace(
            "missing_blob: 024ca655535df09af16e052c927001ac50484aff",
            "missing_blob: 024ca655535df09af16e052c927001ac50484af0",
        )
        if forensic == FORENSIC.read_text(encoding="utf-8"):
            fail("perf forensic exception fixture did not match the registry")
        forensic_path = scratch / "archive-forensic-exceptions-v1.yml"
        forensic_path.write_text(forensic, encoding="utf-8")
        require_drift(
            "generate_archive_forensic_exceptions.py",
            ["--registry", str(forensic_path)],
            "exceptions[0].missing_blob",
            forensic_path,
            "registry=",
        )

        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
        digest = inventory["excluded_mutations"][0]["sha256"]
        inventory["excluded_mutations"][0]["sha256"] = (
            ("0" if digest[0] != "0" else "1") + digest[1:]
        )
        inventory_digest = scratch / "namespace-writer-inventory-v1.json"
        write_json(inventory_digest, inventory)
        require_drift(
            "generate_namespace_writer_registry.py",
            ["--inventory", str(inventory_digest)],
            "excluded_mutations[0].sha256",
            inventory_digest,
            "derived=",
        )

        audit = candidate_audit()
        stale_entry = json.loads(json.dumps(audit["entries"][0]))
        stale_entry["path"] = "src/zz-registry-derivation-stale.c"
        audit["entries"].append(stale_entry)
        audit_stale = scratch / "namespace-writer-candidate-audit-v1.json"
        write_json(audit_stale, audit)
        require_drift(
            "generate_namespace_writer_registry.py",
            ["--audit", str(audit_stale)],
            f"entries[{len(audit['entries']) - 1}].path",
            audit_stale,
            "only in registry",
        )

        flipped = candidate_audit()
        source = flipped["entries"][0]["sha256"]
        flipped["entries"][0]["sha256"] = ("f" if source[0] != "f" else "e") + source[1:]
        audit_digest = scratch / "namespace-writer-candidate-audit-digest.json"
        write_json(audit_digest, flipped)
        require_drift(
            "generate_namespace_writer_registry.py",
            ["--audit", str(audit_digest)],
            "entries[0].sha256",
            audit_digest,
            "derived=",
        )

    print("registry derivation contract: PASS")


if __name__ == "__main__":
    main()

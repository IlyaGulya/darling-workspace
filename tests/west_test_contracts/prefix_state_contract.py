#!/usr/bin/env python3
"""Behavioral matrix for the shared bounded prefix-state reader."""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
from prefix_state import PrefixStateError, read_prefix_state, read_prefix_state_model
from test_runtime_deploy import RuntimeDeploymentService


def write_state(prefix: Path, version: int, *, inode: int | None = None) -> Path:
    identity = prefix.stat(); state = prefix / f".darling-prefix-state-v{version}"
    extra = ""
    if version == 3:
        sidecar = prefix.with_name(f"{prefix.name}.eunion-sidecar-v1")
        sidecar.mkdir(exist_ok=True)
        sidecar_identity = sidecar.stat()
        extra = (
            f"sidecar_device={sidecar_identity.st_dev}\nsidecar_inode={sidecar_identity.st_ino}\n"
            f"owner_uid={identity.st_uid}\nowner_gid={identity.st_gid}\n"
            "provenance=darling-runtime-prefix-sidecar-v1\n"
        )
    state.write_text(f"DARLING_PREFIX_STATE_V{version}\nschema_version={version}\ngeneration=7\n"
                     "runtime_mode=rootless-eunion\n"
                     f"prefix_device={identity.st_dev}\nprefix_inode={identity.st_ino if inode is None else inode}\n{extra}")
    state.chmod(0o600); return state


def rejected(prefix: Path) -> None:
    try: read_prefix_state(prefix)
    except PrefixStateError: return
    raise AssertionError("hostile prefix state was accepted")


class DeploymentRejected(RuntimeError):
    pass


def deployment_service() -> RuntimeDeploymentService:
    def die(message: str) -> None:
        raise DeploymentRejected(message)

    return RuntimeDeploymentService(SimpleNamespace(die=die))


def deployment_rejected(prefix: Path) -> None:
    try:
        deployment_service()._runtime_mode_marker_required(
            {"runtime-mode": "rootless-eunion"}, prefix
        )
    except DeploymentRejected:
        return
    raise AssertionError("guest-runtime-deploy accepted hostile prefix state")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="prefix-state-contract-") as directory:
        root = Path(directory)
        v3 = root / "v3"; v3.mkdir(); write_state(v3, 3)
        assert (
            read_prefix_state(v3).schema_version,
            read_prefix_state(v3).runtime_mode,
            read_prefix_state(v3).generation,
        ) == (3, "rootless-eunion", 7)
        assert read_prefix_state_model(v3).kind == "v3"
        assert deployment_service()._runtime_mode_marker_required(
            {"runtime-mode": "rootless-eunion"}, v3
        )[0] is False
        v2 = root / "v2"; v2.mkdir(); write_state(v2, 2)
        assert read_prefix_state(v2).schema_version == 2
        assert read_prefix_state_model(v2).kind == "v2"
        assert deployment_service()._runtime_mode_marker_required(
            {"runtime-mode": "rootless-eunion"}, v2
        )[0] is False
        malformed = root / "malformed"; malformed.mkdir(); write_state(malformed, 3).write_text("bad\n"); rejected(malformed)
        deployment_rejected(malformed)
        ambiguous = root / "ambiguous"; ambiguous.mkdir(); write_state(ambiguous, 3); write_state(ambiguous, 2); rejected(ambiguous)
        deployment_rejected(ambiguous)
        hostile = root / "hostile"; hostile.mkdir(); target = root / "target"; target.write_text("x")
        os.symlink(target, hostile / ".darling-prefix-state-v3"); rejected(hostile)
        deployment_rejected(hostile)
        mismatch = root / "mismatch"; mismatch.mkdir(); write_state(mismatch, 3, inode=mismatch.stat().st_ino + 1); rejected(mismatch)
        deployment_rejected(mismatch)
        wrong_mode = root / "wrong-mode"; wrong_mode.mkdir(); wrong = write_state(wrong_mode, 3)
        wrong.write_text(wrong.read_text().replace("rootless-eunion", "privileged-eunion"))
        deployment_rejected(wrong_mode)
        state_marker = root / "state-marker"; state_marker.mkdir(); write_state(state_marker, 3)
        (state_marker / ".darling-runtime-mode-v1").write_text(
            "DARLING_RUNTIME_MODE_V1=rootless-eunion\n"
        )
        deployment_rejected(state_marker)
        extra_field = root / "extra-field"; extra_field.mkdir(); extra = write_state(extra_field, 3)
        extra.write_text(extra.read_text() + "forged=accepted\n"); rejected(extra_field)
        sidecar_swap = root / "sidecar-swap"; sidecar_swap.mkdir(); write_state(sidecar_swap, 3)
        original_sidecar = sidecar_swap.with_name(f"{sidecar_swap.name}.eunion-sidecar-v1")
        original_sidecar.rename(original_sidecar.with_name(f"{original_sidecar.name}.old"))
        original_sidecar.mkdir(); rejected(sidecar_swap)
        legacy = root / "legacy"; legacy.mkdir(); (legacy / "payload").write_text("x")
        legacy_marker = legacy / ".darling-runtime-mode-v1"
        legacy_marker.write_text(
            "DARLING_RUNTIME_MODE_V1=rootless-eunion\n"
        )
        legacy_marker.chmod(0o600)
        assert read_prefix_state_model(legacy).kind == "legacy-marker"
        assert deployment_service()._runtime_mode_marker_required(
            {"runtime-mode": "rootless-eunion"}, legacy
        )[0] is False
    print("shared prefix-state contract: PASS")


if __name__ == "__main__": main()

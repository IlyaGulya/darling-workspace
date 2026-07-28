"""Behavioral contracts for disposable Git worktrees used by ``west patch``."""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.patch import (
    DarlingPatch,
    generated_patch_artifacts,
    legacy_automation_trailers,
)
from west_commands.patch_git import (
    PATCH_APPLICATION_GIT_OPTIONS,
    TEMPORARY_PATCH_GIT_OPTIONS,
)


assert PATCH_APPLICATION_GIT_OPTIONS == (
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)
assert TEMPORARY_PATCH_GIT_OPTIONS == (
    *PATCH_APPLICATION_GIT_OPTIONS,
    "-c",
    "user.name=West Test",
    "-c",
    "user.email=west-test@example.invalid",
)


def runtime_patch(artifact):
    return {
        "module": "darling",
        "tests": [
            {
                "name": "rootless_bootstrap_resource_contract",
                "runs": "guest",
                "runner": "guest-runtime-script",
                "script": "tests/west_test_contracts/patch_verify_contract.py",
                "red-proof": {
                    "mode": "guest-runtime-deploy",
                    "runtime-artifacts": [artifact],
                }
            }
        ]
    }


metadata_command = DarlingPatch.__new__(DarlingPatch)
metadata_command._project_path = lambda _repo: ROOT
rootless_artifact = {
    "module": "darling",
    "build-targets": ["rootless_bootstrap"],
    "resource": "rootless-bootstrap",
}
rootless_errors = metadata_command._validate_test_metadata(runtime_patch(rootless_artifact))
assert rootless_errors == [], rootless_errors
bootstrap_test = runtime_patch(rootless_artifact)
bootstrap_test["tests"][0]["red"] = True
bootstrap_test["tests"][0]["red-proof"] = {
    "mode": "guest-runtime-deploy",
    "expect-failure-phase": "bootstrap",
    "expect-output-contains": ["Rootless shellspawn did not become ready"],
    "runtime-artifacts": [rootless_artifact],
}
assert metadata_command._validate_test_metadata(bootstrap_test) == []
for invalid_artifact, expected in (
    ({**rootless_artifact, "deploy": ["bin/darling"]}, "must not declare deploy paths"),
    ({**rootless_artifact, "build-targets": ["darling"]}, "must build only"),
    ({**rootless_artifact, "resource": "unknown"}, "has unknown resource"),
):
    errors = metadata_command._validate_test_metadata(runtime_patch(invalid_artifact))
    assert any(expected in error for error in errors), errors

provider_test = runtime_patch(rootless_artifact)
provider_test["tests"][0]["red"] = True
provider_test["tests"][0]["red-proof"] = {
    "mode": "guest-runtime-deploy",
    "expect-failure-phase": "provider",
    "expect-output-contains": ["installer failed"],
    "runtime-artifacts": [rootless_artifact],
}
errors = metadata_command._validate_test_metadata(provider_test)
assert any("provider failure requires" in error for error in errors), errors
provider_test["tests"][0]["red-proof"]["provider-under-test"] = True
assert metadata_command._validate_test_metadata(provider_test) == []
provider_test["tests"][0]["red-proof"]["expect-failure-phase"] = "run"
errors = metadata_command._validate_test_metadata(provider_test)
assert any("provider-under-test requires" in error for error in errors), errors

snapshot_patch = b"""diff --git a/tests/PERF18-lanes-snapshot.json b/tests/PERF18-lanes-snapshot.json
new file mode 100644
--- /dev/null
+++ b/tests/PERF18-lanes-snapshot.json
@@ -0,0 +1 @@
+{\"lanes\": 128}
diff --git a/tests/ring_census_gate_test.cpp b/tests/ring_census_gate_test.cpp
new file mode 100644
--- /dev/null
+++ b/tests/ring_census_gate_test.cpp
@@ -0,0 +1 @@
+int main() { return 0; }
"""
assert generated_patch_artifacts(snapshot_patch) == [
    "tests/PERF18-lanes-snapshot.json"
]
deleted_snapshot_patch = snapshot_patch.replace(
    b"new file mode 100644", b"deleted file mode 100644", 1
)
assert generated_patch_artifacts(deleted_snapshot_patch) == []

legacy_trailer_patch = b"""From 0123456789abcdef Mon Sep 17 00:00:00 2001
Subject: [PATCH] fixture

Behavioral explanation.

Co-Authored-By: Claude Example <claude@example.invalid>
---
 file.c | 1 +
 1 file changed, 1 insertion(+)
"""
assert legacy_automation_trailers(legacy_trailer_patch) == [
    "Co-Authored-By: Claude Example <claude@example.invalid>"
]
assert legacy_automation_trailers(
    b"+Co-Authored-By: Claude Example <claude@example.invalid>\n"
) == []

artifact_command = DarlingPatch.__new__(DarlingPatch)
artifact_command.die = lambda message: (_ for _ in ()).throw(AssertionError(message))
try:
    artifact_command._check_export_artifacts(
        {"path": "test/artifact.patch"}, snapshot_patch
    )
except AssertionError as exc:
    assert "generated evidence artifact" in str(exc), exc
    assert "PERF18-lanes-snapshot.json" in str(exc), exc
else:
    raise AssertionError("patch export accepted a generated snapshot artifact")

try:
    artifact_command._check_export_artifacts(
        {"path": "test/legacy-trailer.patch"}, legacy_trailer_patch
    )
except AssertionError as exc:
    assert "legacy automation trailer" in str(exc), exc
else:
    raise AssertionError("patch export accepted a legacy automation trailer")

source_revision_patch = {
    "module": "darling/src/external/xnu",
    "tests": [
        {
            "name": "source_revision_contract",
            "runs": "host",
            "runner": "source-contract-script",
            "script": "tests/source_revision_contract.sh",
            "red": True,
            "red-proof": {
                "mode": "source-base",
                "source-env": "XNU_SRC_ROOT",
                "source-revision": "deadbeef",
            },
        }
    ],
}
source_revision_errors = metadata_command._validate_test_metadata(source_revision_patch)
assert source_revision_errors == [], source_revision_errors
guest_source_revision_patch = {
    "module": "darling/src/external/xnu",
    "tests": [
        {
            "name": "guest_source_revision_contract",
            "runs": "guest",
            "runner": "guest-c-fixture",
            "script": "tests/eunion_mkdir_opaque_guest.c",
            "ok-marker": "WEST_EUNION_MKDIR_OPAQUE_OK",
            "red": True,
            "requires": ["darling-prefix"],
            "red-proof": {
                "mode": "guest-runtime-deploy",
                "bad-profile": "current-minus-patch",
                "source-revision": "deadbeef",
                "runtime-artifacts": [
                    {
                        "module": "darling/src/external/xnu",
                        "build-targets": ["system_kernel"],
                        "deploy": ["usr/lib/system/libsystem_kernel.dylib"],
                    }
                ],
            },
        }
    ],
}
guest_source_revision_errors = metadata_command._validate_test_metadata(guest_source_revision_patch)
assert guest_source_revision_errors == [], guest_source_revision_errors
for invalid_proof, expected in (
    (
        {"mode": "self", "source-revision": "deadbeef"},
        "requires mode: source-base or guest-runtime-deploy",
    ),
    ({"mode": "source-base", "source-revision": ""}, "non-empty revision"),
):
    invalid_patch = {
        **source_revision_patch,
        "tests": [{**source_revision_patch["tests"][0], "red-proof": invalid_proof}],
    }
    errors = metadata_command._validate_test_metadata(invalid_patch)
    assert any(expected in error for error in errors), errors


patch_source = (ROOT / "west_commands/patch.py").read_text()
runtime_source = (ROOT / "west_commands/test_runtime_source.py").read_text()
export_source = (ROOT / "west_commands/patch_stack_export.py").read_text()
assert "patch_stack_lock_first.materialize_batch_into(" in patch_source
assert "applicability never executes" in patch_source
assert "patch_stack_lock_first.materialize_batch_into(" in runtime_source
assert "Historical archives are never executable inputs." in runtime_source
assert '"format-patch"' in export_source
assert "validate_fetched_lock(" in export_source
for retired in (
    "git_for_patch_application",
    "git_for_temporary_patch_application",
    "--legacy-mbox",
):
    assert retired not in patch_source
    assert retired not in runtime_source


print("PASS west-patch-verify-contract")

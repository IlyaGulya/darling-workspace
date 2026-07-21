#!/usr/bin/env python3
"""Keep the lock-first acceptance tier manual and structurally exact."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
workflow = (ROOT / ".github/workflows/patch-stack-lock-first.yml").read_text()
assert "on:\n  workflow_dispatch:" in workflow
assert "push:" not in workflow and "schedule:" not in workflow
assert "if: github.event_name == 'workflow_dispatch'" in workflow
assert "runs-on: ubuntu-latest" in workflow and "timeout-minutes: 25" in workflow
assert "jdx/mise-action@5228313ee0372e111a38da051671ca30fc5a96db" in workflow
assert "working_directory: darling-dev/darling-workspace" in workflow
assert "cache: false" in workflow and "cache_save: false" in workflow
assert "actions/cache" not in workflow and "MISE_CACHE_DIR" not in workflow
assert workflow.count("west patch apply --profile homebrew") == 2
assert "west patch apply --profile homebrew --lock-first" in workflow
assert "mise exec -- uv --version" in workflow
assert "mise exec -- west --version" in workflow
assert "mise_toml_sha256=" in workflow and "sha256sum mise.toml" in workflow
assert "python3 -m venv" not in workflow and "pip install west" not in workflow
assert "python3 -c 'import west'" not in workflow
assert "LOCK_FIRST_TOOLS" not in workflow
for line in workflow.splitlines():
    stripped = line.strip()
    assert not stripped.startswith("west "), line
assert '\n            HOME="$home" west ' not in workflow
assert 'HOME="$home" mise exec' not in workflow
verification = workflow.split("- name: Verify isolated West under empty homes", 1)[1].split("- name: Configure repository-local identity", 1)[0]
assert 'mise -C "$workspace" exec -- env HOME="$home" west --version' in verification
assert "mise exec --" not in verification, "verification may not launch outside project mise.toml"
assert 'mise -C "$workspace" exec -- env HOME="$home" ci/bootstrap-west.sh' in workflow
assert 'mise -C "$workspace" exec -- env HOME="$home" west list' in workflow
assert 'mise -C "$workspace" exec -- env HOME="$LOCK_FIRST_ROOT/control-home"' in workflow
assert 'mise -C "$workspace" exec -- env HOME="$LOCK_FIRST_ROOT/lock-first-home"' in workflow
assert "--lock-first-evidence \"$LOCK_FIRST_ROOT/evidence/lock-first-evidence.json\"" in workflow
assert "--shadow-lock" not in workflow
assert "git clone --no-local --no-hardlinks" in workflow and "fetch-depth: 0" in workflow
assert "cleanup_status=" in workflow and "if: always()" in workflow
assert "west-patch-shadow-* west-lock-materialize-* west-patch-lock-first-*" in workflow
assert "patch-stack-lock-first-acceptance" in workflow
acceptance = (ROOT / "ci/patch_stack_shadow_acceptance.py").read_text()
assert '"lock-first-evidence.json"' in acceptance
assert 'evidence.parent.glob(f"{evidence.stem}*{evidence.suffix}")' in acceptance
print("patch-stack lock-first hosted-workflow contract: PASS")

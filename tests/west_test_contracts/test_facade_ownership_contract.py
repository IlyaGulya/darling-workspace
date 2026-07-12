"""Architecture guard keeping domain mutations out of the west test facade."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FACADE = ROOT / "west_commands/test.py"
source = FACADE.read_text()
tree = ast.parse(source)

assert len(source.splitlines()) <= 5400, "test.py grew beyond its reviewed facade budget"

violations = []
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in {
            ("os", "replace"),
            ("os", "kill"),
            ("fcntl", "flock"),
        }:
            violations.append((node.lineno, f"{owner.id}.{node.func.attr}"))
    if isinstance(node, (ast.List, ast.Tuple)):
        literals = [
            item.value for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        if len(literals) >= 2 and literals[:2] == ["git", "worktree"]:
            violations.append((node.lineno, "git worktree"))

assert not violations, f"domain mutation returned to test.py: {violations}"

for owner in (
    "RuntimeSourceMaterializer",
    "RuntimeDeploymentService",
    "RuntimeProofStateMachine",
    "PrefixLifecycleOwner",
):
    assert owner in source, f"test.py no longer delegates to {owner}"

print("PASS test-facade-ownership-contract")

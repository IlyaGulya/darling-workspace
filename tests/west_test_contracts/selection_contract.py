"""Pure selection contract for patch metadata."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_selection import select_metadata_tests


profile = {
    "patches": [
        {
            "path": "a.patch",
            "bead": "dar-a",
            "tests": [
                {"name": "host-red", "env": "host", "red": True, "diag": "bare"},
                {"name": "guest-green", "env": "darling", "red": False, "diag": "guarded"},
            ],
        },
        {"path": "b.patch", "bead": "dar-b", "tests": []},
        {"path": "c.patch", "bead": "dar-c", "test-exception": {"reason": "doc"}},
    ]
}

all_selected = select_metadata_tests(
    profile,
    patch_path=None,
    bead=None,
    env=None,
    diag=None,
    red_only=False,
    resolved_diag=lambda test: test["diag"],
)
assert [test["name"] for _, test in all_selected.selected] == ["host-red", "guest-green"]
assert [patch["path"] for patch in all_selected.missing] == ["b.patch"]

red_host = select_metadata_tests(
    profile,
    patch_path="a.patch",
    bead="dar-a",
    env="host",
    diag="bare",
    red_only=True,
    resolved_diag=lambda test: test["diag"],
)
assert red_host.found_patch
assert [test["name"] for _, test in red_host.selected] == ["host-red"]

absent = select_metadata_tests(
    profile,
    patch_path="missing.patch",
    bead=None,
    env=None,
    diag=None,
    red_only=False,
    resolved_diag=lambda test: test["diag"],
)
assert not absent.found_patch

print("PASS west-test-selection-contract")

"""Pure workload selection retains distinct declarations and CTest variants."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_manifest import load_workloads, normalize_workloads
from test_selection import select_metadata_tests, select_workload_tests


catalog = normalize_workloads({
    "version": 1,
    "workloads": [
        {"id": "host-model", "name": "same", "module": "darling", "bead": "dar-a", "env": "host", "diag": "bare"},
        {"id": "guest-green", "name": "same", "module": "darling/src/external/xnu", "bead": "dar-a", "env": "darling", "diag": "guarded", "smoke": True},
        {"id": "blocked", "module": "darling", "bead": "dar-b", "blocked": True},
        {"id": "ctest", "module": "darling", "ctest-label": "suite"},
        {"id": "prebuilt", "module": "darling", "env": "darling", "runner": "guest-macho-fixture", "validation-group": "homebrew"},
    ],
    "aliases": {"pair": ["guest-green", "host-model"], "overlap": ["host-model"]},
})


def names(selection):
    return [test["id"] for _, test in selection.selected]


selected = select_workload_tests(catalog)
assert names(selected) == ["host-model", "guest-green", "ctest", "prebuilt"]
assert [test["id"] for _, test in selected.blocked] == ["blocked"]
assert names(select_workload_tests(catalog, workload="pair")) == ["guest-green", "host-model"]
assert names(select_workload_tests(catalog, workload=["pair", "overlap", "guest-green"])) == ["guest-green", "host-model"]
assert names(select_workload_tests(catalog, bead="dar-a", env="host", diag="bare")) == ["host-model"]
assert names(select_workload_tests(catalog, env="darling", label="smoke:true")) == ["guest-green"]
assert names(select_workload_tests(catalog, label="name:same")) == ["host-model", "guest-green"]
assert names(select_workload_tests(catalog, label="workload:guest-green")) == ["guest-green"]
assert names(select_workload_tests(catalog, submodule="xnu")) == ["guest-green"]
assert names(select_workload_tests(catalog, label="stress:true")) == []
assert names(select_workload_tests(catalog, validation_group="homebrew")) == ["prebuilt"]
owner, test = select_workload_tests(catalog, workload="guest-green").selected[0]
assert owner == {"id": "guest-green", "path": "guest-green", "module": "darling/src/external/xnu", "bead": "dar-a"}
assert test["name"] == "same", "display names do not become identity"

# Registration-owned selectors apply after CTest variants are discovered.
assert names(select_workload_tests(catalog, workload="ctest", env="darling", diag="forensic", label="smoke", defer_ctest=True)) == ["ctest"]
variants = [
    {"id": "ctest", "env": "host", "diag": "bare", "_ctest": {"labels": ["suite"]}},
    {"id": "ctest", "env": "darling", "diag": "guarded", "_ctest": {"labels": ["suite", "smoke:true"]}},
]
resolved = select_metadata_tests({"path": "ctest", "module": "darling"}, variants, env="darling", diag="guarded", label="smoke:true")
assert resolved.selected[0][1] is variants[1]
assert len(resolved.selected) == 1

for kwargs in ({"workload": "missing"}, {"validation_group": "invalid"}):
    try:
        select_workload_tests(catalog, **kwargs)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted invalid selection: {kwargs}")

real_catalog = load_workloads(ROOT / "testkit/workloads.yml")
assert names(select_workload_tests(real_catalog, workload="homebrew-prepare")) == [
    "homebrew-readiness-smoke", "rootless_guest_toolchain_compile_execute"]
assert names(select_workload_tests(real_catalog, workload="homebrew-preflight")) == ["homebrew_build_tools_preflight"]
assert names(select_workload_tests(real_catalog, workload="homebrew-source")) == ["rootless_homebrew_source_install"]
assert names(select_workload_tests(real_catalog, workload="exact-capture")) == ["guest-exact-capture"]
assert names(select_workload_tests(real_catalog, workload="installed-runtime")) == ["installed-runtime"]
print("PASS west-test-selection-contract")

#!/usr/bin/env python3
"""Generate docs/test-ownership-census.md.

One row per entry in ``patches/*/patches.yml``. The census records, for every
patch entry: its profile/bead, the tests the metadata declares (with the
resolved ``use:`` profile, markers and RED proof), where that behavior is
covered today in the long-term authority, and the entry's retirement
disposition.

Authority sources read (never written):

  * ``testkit/CMakeLists.txt`` -- ``add_compat_test()`` / ``add_test()`` names,
    BEAD labels, SOURCE assets, runtime profiles and OK markers.
  * ``testkit/runtime-profiles.yml`` -- the runtime provider names.
  * ``ci/run-host-tier.py`` -- the host-tier CONTRACTS / EXPLICIT_CONTRACTS /
    EXCLUDED_CONTRACTS registry (the "tests/ host contracts" the tier runs).
  * ``tests/`` -- the workspace test-asset inventory.
  * ``ci/run-test-tier.sh`` and ``.github/workflows/`` -- additional tier
    entrypoints used only to decide whether a tests/ asset is referenced.

Disposition semantics are stated in the generated header. The generator is
deterministic: the same tree always produces the same census.

Read-only with respect to product state: this script only reads the tree and
writes the census file.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_manifest import load_test_profile  # noqa: E402  (west's own loader)

TESTKIT_CMAKE = ROOT / "testkit" / "CMakeLists.txt"
RUNTIME_PROFILES = ROOT / "testkit" / "runtime-profiles.yml"
HOST_TIER = ROOT / "ci" / "run-host-tier.py"
TEST_TIER = ROOT / "ci" / "run-test-tier.sh"

# ---------------------------------------------------------------------------
# Authority: CTest registrations


def parse_testkit_registrations(text: str) -> list[dict]:
    """Return every CTest registration the testkit CMakeLists declares.

    A registration is an ``add_compat_test()`` or ``add_test()`` call, plus the
    ``_simple_xnu_guest_cases`` table whose rows are stamped by a foreach loop.
    """
    registrations: list[dict] = []

    for match in re.finditer(r"add_compat_test\(\s*\n(.*?)\n\s*\)", text, re.S):
        body = match.group(1)
        fields: dict[str, str] = {}
        for key in ("NAME", "SOURCE", "BEAD", "ENVS", "RUNTIME_PROFILE", "OK_MARKER"):
            key_match = re.search(rf"^\s*{key}\s+(\S.*?)\s*$", body, re.M)
            if key_match:
                fields[key] = key_match.group(1).strip()
        if "NAME" in fields:
            registrations.append(
                {
                    "origin": "add_compat_test",
                    "name": fields.get("NAME", ""),
                    "bead": fields.get("BEAD"),
                    "source": fields.get("SOURCE"),
                    "envs": fields.get("ENVS"),
                    "runtime_profile": fields.get("RUNTIME_PROFILE"),
                    "ok_marker": fields.get("OK_MARKER"),
                }
            )

    for match in re.finditer(r"add_test\(NAME\s+([A-Za-z0-9_./$-]+)", text):
        registrations.append(
            {
                "origin": "add_test",
                "name": match.group(1),
                "bead": None,
                "source": None,
                "envs": None,
                "runtime_profile": None,
                "ok_marker": None,
            }
        )

    # _simple_xnu_guest_cases rows: "bead|name|source_rel|marker"
    for match in re.finditer(
        r'"([A-Za-z0-9_.-]+)\|([A-Za-z0-9_]+)\|([^|"]+)\|([^"|]+)"', text
    ):
        registrations.append(
            {
                "origin": "simple-xnu-case",
                "name": match.group(2),
                "bead": match.group(1),
                "source": match.group(3),
                "envs": "darling",
                "runtime_profile": None,
                "ok_marker": match.group(4),
            }
        )

    return registrations


def basename_of(value: str | None) -> str | None:
    if not value:
        return None
    # Drop CMake variable indirection but keep the trailing path shape.
    cleaned = value.replace("${_workspace_root}", "").replace("${DARLING_SRC}", "")
    cleaned = cleaned.strip("${}\"")
    if "/" not in cleaned:
        return None
    return os.path.basename(cleaned)


# ---------------------------------------------------------------------------
# Authority: host tier contracts


def parse_host_tier(text: str) -> dict[str, set[str]]:
    """Return run / explicit / excluded contract paths from ci/run-host-tier.py."""
    run = set(re.findall(r'"(tests/[^"]+\.(?:sh|py))"', text))
    # CONTRACTS and EXPLICIT_CONTRACTS both name runnable contracts; the
    # EXCLUDED_CONTRACTS dict names deliberately-unrun ones.
    excluded_block = re.search(r"EXCLUDED_CONTRACTS = \{(.*?)\n\}", text, re.S)
    excluded: set[str] = set()
    if excluded_block:
        excluded = set(re.findall(r'"(tests/[^"]+\.(?:sh|py))"', excluded_block.group(1)))
    runnable = run - excluded
    return {"runnable": runnable, "excluded": excluded}


def parse_runtime_profiles(text: str) -> set[str]:
    profiles: set[str] = set()
    for match in re.finditer(r"^  ([a-z0-9][a-z0-9-]+):\s*$", text, re.M):
        profiles.add(match.group(1))
    return profiles


def parse_source_registrations(darling_src: Path) -> dict[str, set[str]]:
    """Return source-owned CTest names and bead labels from the checkout.

    docs/test-infra.md keeps fixture sources and their CMake/CTest
    registrations colocated with the owning project, so registrations outside
    ``testkit/`` are authority too.
    """
    names: set[str] = set()
    beads: set[str] = set()
    if not darling_src.is_dir():
        return {"names": names, "beads": beads}
    for cmake in darling_src.rglob("CMakeLists.txt"):
        try:
            text = cmake.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        names.update(re.findall(r"add_test\(\s*NAME\s+([A-Za-z0-9_./-]+)", text))
        names.update(re.findall(r"add_compat_test\(\s*\n\s*NAME\s+([A-Za-z0-9_./-]+)", text))
        beads.update(re.findall(r"bead:([A-Za-z0-9_.-]+)", text))
    return {"names": names, "beads": beads}


# ---------------------------------------------------------------------------
# Reference index: which tests/ assets the tier entrypoints mention


def reference_index() -> dict[str, set[str]]:
    """Map every basename mentioned by a tier entrypoint to its sources."""
    refs: dict[str, set[str]] = {}
    sources = [HOST_TIER, TEST_TIER, TESTKIT_CMAKE]
    sources += [Path(p) for p in glob.glob(str(ROOT / ".github" / "workflows" / "*"))]
    for path in sources:
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for token in re.findall(r"[A-Za-z0-9_./-]+\.(?:c|cc|cpp|sh|py)", text):
            refs.setdefault(os.path.basename(token), set()).add(
                str(path.relative_to(ROOT))
            )
    return refs


# ---------------------------------------------------------------------------
# Classification


def resolved_axes(test: dict) -> dict:
    """Return the axes the metadata actually carries after ``use:`` resolution."""
    script = test.get("script") or test.get("source-script") or test.get("source-file")
    return {
        "name": test.get("name"),
        "runner": test.get("runner"),
        "kind": test.get("kind"),
        "runs": test.get("runs") or test.get("env"),
        "diag": test.get("diag"),
        "red": bool(test.get("red")),
        "coverage_tier": test.get("coverage-tier"),
        "runtime_profile": test.get("runtime-profile"),
        "ok_marker": test.get("ok-marker"),
        "script": script,
        "target": test.get("target"),
        "guest_command": test.get("guest-command"),
        "ctest": test.get("ctest"),
        "ctest_name": test.get("ctest-name"),
        "ctest_label": test.get("ctest-label"),
        "use": test.get("use") or test.get("extends"),
        "requires": test.get("requires"),
        "expect": test.get("expect"),
    }


def classify_test(
    test: dict,
    entry_bead: str | None,
    *,
    reg_names: set[str],
    reg_beads: set[str],
    reg_source_basenames: set[str],
    runnable_contracts: set[str],
    excluded_contracts: set[str],
    workspace_tests: set[str],
    referenced: dict[str, set[str]],
    source_names: set[str] | None = None,
    source_beads: set[str] | None = None,
) -> dict:
    """Return the coverage classification for one declared test."""
    source_names = source_names or set()
    source_beads = source_beads or set()
    axes = resolved_axes(test)
    script = axes["script"]
    script_in_workspace = script in workspace_tests if script else None
    script_basename = os.path.basename(script) if script else None
    referrers = referenced.get(script_basename or "", set())

    if axes["ctest"] or axes["ctest_name"] or axes["ctest_label"]:
        reference = str(axes["ctest"] or axes["ctest_name"] or axes["ctest_label"])
        base = re.sub(r"^(darling|macos|host)/", "", reference)
        resolved = "unverified"
        if reference.startswith("bead:"):
            bead = reference.split(":", 1)[1]
            if bead in reg_beads or bead in source_beads:
                resolved = "resolved (bead label)"
        elif base in reg_names:
            resolved = "resolved (testkit registration)"
        elif base in source_names:
            resolved = "resolved (source-owned registration)"
        elif "eunion-host" in reference:
            resolved = "resolved (testkit label)"
        return {
            "axes": axes,
            "coverage": "CTEST-REFERENCE",
            "evidence": f"ctest reference {reference} [{resolved}]",
            "referrers": sorted(referrers),
        }

    if axes["name"] and (axes["name"] in reg_names or axes["name"] in source_names):
        where = "testkit" if axes["name"] in reg_names else "source-owned"
        return {
            "axes": axes,
            "coverage": "CTEST-REGISTERED",
            "evidence": f"{where} add_compat_test/add_test NAME {axes['name']}",
            "referrers": sorted(referrers),
        }

    if script_basename and script_basename in reg_source_basenames:
        return {
            "axes": axes,
            "coverage": "CTEST-REGISTERED",
            "evidence": f"testkit SOURCE {script_basename}",
            "referrers": sorted(referrers),
        }

    if script and script in runnable_contracts:
        return {
            "axes": axes,
            "coverage": "TIER-CONTRACT",
            "evidence": f"ci/run-host-tier.py runs {script}",
            "referrers": sorted(referrers),
        }

    if script and script in excluded_contracts:
        return {
            "axes": axes,
            "coverage": "EXCLUDED-CONTRACT",
            "evidence": f"ci/run-host-tier.py deliberately excludes {script}",
            "referrers": sorted(referrers),
        }

    # Bead coverage is checked last: a registration with the same bead covers
    # the entry's behavior even when the declared test name differs.
    if entry_bead and (entry_bead in reg_beads or entry_bead in source_beads):
        where = "testkit" if entry_bead in reg_beads else "source-owned"
        return {
            "axes": axes,
            "coverage": "CTEST-REGISTERED",
            "evidence": f"{where} BEAD {entry_bead}",
            "referrers": sorted(referrers),
        }

    if script_in_workspace:
        return {
            "axes": axes,
            "coverage": "AUTHORITY-ASSET-UNWIRED",
            "evidence": f"asset {script} exists in tests/ but no CTest registration"
            + (f"; referenced by {sorted(referrers)}" if referrers else ""),
            "referrers": sorted(referrers),
        }

    if script:
        return {
            "axes": axes,
            "coverage": "PATCH-INTERNAL",
            "evidence": f"asset {script} lives in a patched module, not tests/",
            "referrers": sorted(referrers),
        }

    return {
        "axes": axes,
        "coverage": "NO-ASSET",
        "evidence": "no script asset (command/target/object check)",
        "referrers": sorted(referrers),
    }


PACKAGING_EXCEPTION_REASONS = {
    "harness-only",
    "instrumentation-only",
    "test-instrumentation",
}
PACKAGING_TEST_HINTS = ("west patch status", "drift gate", "drift-gate")


def disposition_for(entry: dict, classified: list[dict]) -> tuple[str, str, bool]:
    """Return (disposition, reason, at_risk) for one patch entry."""
    tests = entry.get("tests") or []
    exception = entry.get("test-exception") or {}
    reason_text = str(exception.get("reason", ""))
    exception_blob = " ".join(str(v) for v in exception.values()).lower()
    entry_blob = " ".join(
        [str(entry.get("path", ""))]
        + [f"{c['axes']['name']} {c['axes']['script']}" for c in classified]
    ).lower()

    coverages = [c["coverage"] for c in classified]

    # Machinery-only rows, independent of any behavior.
    if not tests:
        if reason_text in PACKAGING_EXCEPTION_REASONS:
            return (
                "PACKAGING-ONLY",
                f"patch-metadata test machinery: {reason_text} "
                "(gate harness / RED-proof instrumentation)",
                False,
            )
        return (
            "MIGRATED",
            f"MIGRATION REQUIRED: no automated test; test-exception claims coverage: {reason_text}",
            True,
        )

    if any(any(hint in b for hint in PACKAGING_TEST_HINTS) for b in (entry_blob, exception_blob)):
        return (
            "PACKAGING-ONLY",
            "patch-drift gate (west patch status) over the patch stack",
            False,
        )

    if "CTEST-REFERENCE" in coverages:
        return (
            "ALREADY_REGISTERED",
            "metadata binds an existing CTest registration",
            False,
        )
    if "CTEST-REGISTERED" in coverages:
        evidence = next(c["evidence"] for c in classified if c["coverage"] == "CTEST-REGISTERED")
        return ("MIGRATED", f"covered in testkit: {evidence}", False)
    if "TIER-CONTRACT" in coverages:
        evidence = next(c["evidence"] for c in classified if c["coverage"] == "TIER-CONTRACT")
        return ("MIGRATED", f"covered by tests/ host contract: {evidence}", False)

    # Behavioral tests that only patch metadata runs: the migration backlog.
    if all(c["coverage"] == "NO-ASSET" for c in classified):
        return (
            "MIGRATED",
            "MIGRATION REQUIRED: no script asset; only a patch-metadata runner covers it",
            True,
        )
    return (
        "MIGRATED",
        "MIGRATION REQUIRED: only patch metadata covers it; no CTest/testkit registration",
        True,
    )


def submodule_label(module: str | None) -> str:
    if not module:
        return "unknown"
    tail = module.rstrip("/").split("/")[-1]
    return "darling" if tail in {"darling", ""} else tail


def proposed_registration(entry: dict, classified: list[dict]) -> list[str]:
    """Return concrete proposed registrations for at-risk declared tests.

    Each proposal names the label, the run mode and the oracle that would
    preserve the behavior.
    """
    proposals: list[str] = []
    submod = submodule_label(entry.get("module"))
    for item in classified:
        axes = item["axes"]
        if item["coverage"] in {"CTEST-REGISTERED", "CTEST-REFERENCE", "TIER-CONTRACT"}:
            continue
        name = axes["name"] or f"{entry['bead']}-case"
        runner = axes["runner"] or "?"
        script = axes["script"]
        bead = entry.get("bead") or "<none>"
        runs = axes["runs"] or ("guest" if "guest" in runner else "host")
        oracle = axes["ok_marker"] or _expect_oracle(axes) or "exit status"

        if runs in {"guest", "darling", "macos"}:
            envs = "darling macos" if runs == "macos" else "darling"
            rp = axes["runtime_profile"] or "homebrew-rootless-no-mount"
            marker = f" OK_MARKER \"{axes['ok_marker']}\"" if axes["ok_marker"] else ""
            if script and script.endswith(".sh"):
                source = f"<guest C fixture wrapping {os.path.basename(script)}>"
            elif script:
                source = script
            elif axes["guest_command"]:
                source = f"guest-command {axes['guest_command']}"
            else:
                source = "<extract from patch>"
            proposals.append(
                f"add_compat_test(NAME {name} SOURCE {source} ENVS {envs} "
                f"BEAD {bead} RUNTIME_PROFILE {rp}{marker} INSTALL)  "
                f"[run mode: {'guest+macos' if envs == 'darling macos' else 'guest'} CTest; "
                f"label env:darling;bead:{bead};submod:{submod}; oracle: {oracle}]"
            )
        elif script and script.endswith(".py"):
            proposals.append(
                f"EXPLICIT_CONTRACTS += (\"{name}\", \"{script}\", True) in ci/run-host-tier.py  "
                f"[run mode: host contract; label env:host;bead:{bead}; oracle: exit status]"
            )
        elif script and script.endswith(".sh"):
            if item["coverage"] == "PATCH-INTERNAL":
                destination = f"tests/{os.path.basename(script)}"
                proposals.append(
                    f"extract {script} to {destination}, then `CONTRACTS += \"{destination}\"` in "
                    f"ci/run-host-tier.py  [run mode: host contract; label env:host;bead:{bead};"
                    f"submod:{submod}; oracle: exit status]"
                )
            else:
                proposals.append(
                    f"CONTRACTS += \"{script}\" in ci/run-host-tier.py  "
                    f"[run mode: host contract; label env:host;bead:{bead};submod:{submod}; "
                    f"oracle: exit status]"
                )
        elif script and script.endswith((".c", ".cc", ".cpp")):
            proposals.append(
                f"add_compat_test(NAME {name} SOURCE {script} ENVS host BEAD {bead} DIAG bare)  "
                f"[run mode: host CTest; label env:host;bead:{bead};submod:{submod}; oracle: {oracle}]"
            )
        else:
            target = axes["target"] or script or "<build target>"
            proposals.append(
                f"add_compat_test(NAME {name} SOURCE {target} ENVS host BEAD {bead} DIAG bare)  "
                f"[run mode: host build/CTest; label env:host;bead:{bead};submod:{submod}; "
                f"oracle: build succeeds / {oracle}]"
            )
    return proposals


def _expect_oracle(axes: dict) -> str | None:
    expect = axes.get("expect")
    if not isinstance(expect, dict):
        return None
    contains = expect.get("output-contains")
    if contains:
        return "output contains " + "; ".join(str(c) for c in contains)
    rc = expect.get("returncode")
    if rc is not None:
        return f"returncode {rc}"
    return None


# ---------------------------------------------------------------------------
# Rendering


def test_identity(axes: dict) -> str:
    return (
        axes["name"]
        or axes["ctest_name"]
        or axes["ctest_label"]
        or axes["ctest"]
        or "<unnamed>"
    )


def render_test(axes: dict) -> str:
    parts = [test_identity(axes)]
    axes_bits = []
    if axes["use"]:
        axes_bits.append(f"use={axes['use']}")
    if axes["runner"]:
        axes_bits.append(axes["runner"])
    if axes["kind"]:
        axes_bits.append(axes["kind"])
    if axes["runs"]:
        axes_bits.append(f"runs={axes['runs']}")
    if axes["coverage_tier"]:
        axes_bits.append(f"tier={axes['coverage_tier']}")
    if axes["runtime_profile"]:
        axes_bits.append(f"rt={axes['runtime_profile']}")
    if axes["red"]:
        axes_bits.append("RED")
    if axes["ok_marker"]:
        axes_bits.append(f"marker={axes['ok_marker']}")
    if axes["ctest"] or axes["ctest_name"] or axes["ctest_label"]:
        axes_bits.append(
            "ctest=" + str(axes["ctest"] or axes["ctest_name"] or axes["ctest_label"])
        )
    expect = _expect_oracle(axes)
    if expect:
        axes_bits.append(f"expect={expect}")
    if axes["guest_command"]:
        axes_bits.append(f"cmd={axes['guest_command']}")
    if axes["requires"]:
        axes_bits.append("requires=" + ",".join(str(r) for r in axes["requires"]))
    return f"{parts[0]} ({', '.join(axes_bits)})"


def escape(text: str) -> str:
    """Make a value safe for one markdown table cell."""
    return (
        str(text)
        .replace("|", "\\|")
        .replace("\r", "")
        .replace("\n", " ; ")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "docs" / "test-ownership-census.md"))
    args = parser.parse_args()

    testkit_text = TESTKIT_CMAKE.read_text()
    registrations = parse_testkit_registrations(testkit_text)
    reg_names = {r["name"] for r in registrations if r["name"]}
    reg_beads = {r["bead"] for r in registrations if r["bead"]}
    reg_source_basenames = {
        basename_of(r["source"]) for r in registrations if basename_of(r["source"])
    }
    tier = parse_host_tier(HOST_TIER.read_text())
    runtime_profiles = parse_runtime_profiles(RUNTIME_PROFILES.read_text())
    referenced = reference_index()
    darling_src = Path(os.environ.get("DARLING_SRC", str(ROOT.parent / "darling")))
    source_regs = parse_source_registrations(darling_src)

    workspace_tests = {
        os.path.relpath(path, ROOT)
        for path in glob.glob(str(ROOT / "tests" / "**" / "*"), recursive=True)
        if os.path.isfile(path)
    }

    rows: list[dict] = []
    for profile_path in sorted(glob.glob(str(ROOT / "patches" / "*" / "patches.yml"))):
        profile = Path(profile_path).parent.name
        data = load_test_profile(Path(profile_path))
        raw = yaml.safe_load(Path(profile_path).read_text()) or {}
        raw_by_path = {p.get("path"): p for p in raw.get("patches", [])}
        for patch in data.get("patches", []):
            classified = [
                classify_test(
                    test,
                    patch.get("bead"),
                    reg_names=reg_names,
                    reg_beads=reg_beads,
                    reg_source_basenames=reg_source_basenames,
                    runnable_contracts=tier["runnable"],
                    excluded_contracts=tier["excluded"],
                    workspace_tests=workspace_tests,
                    referenced=referenced,
                    source_names=source_regs["names"],
                    source_beads=source_regs["beads"],
                )
                for test in (patch.get("tests") or [])
            ]
            # West's loader drops `use:` after merging the profile; re-attach the
            # raw declaration so the census reports the metadata as written.
            raw_tests = (raw_by_path.get(patch.get("path")) or {}).get("tests") or []
            for index, item in enumerate(classified):
                if index < len(raw_tests) and isinstance(raw_tests[index], dict):
                    item["axes"]["use"] = (
                        raw_tests[index].get("use") or raw_tests[index].get("extends")
                    )
            disposition, reason, at_risk = disposition_for(patch, classified)
            rows.append(
                {
                    "profile": profile,
                    "path": patch["path"],
                    "bead": patch.get("bead"),
                    "module": patch.get("module"),
                    "publication": patch.get("publication-status"),
                    "tests": classified,
                    "exception": patch.get("test-exception"),
                    "disposition": disposition,
                    "reason": reason,
                    "at_risk": at_risk,
                    "proposals": proposed_registration(patch, classified),
                }
            )

    histogram: dict[str, int] = {}
    for row in rows:
        histogram[row["disposition"]] = histogram.get(row["disposition"], 0) + 1

    at_risk_rows = [r for r in rows if r["at_risk"]]
    at_risk_tests = sum(1 for r in rows if r["at_risk"] for _ in r["tests"])

    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append("# Test ownership census")
    lines.append("")
    lines.append(
        "Machine-generated by `scripts/generate_test_ownership_census.py`. "
        "Do not hand-edit; re-run the generator instead."
    )
    lines.append("")
    lines.append(f"- Generated: {now}")
    lines.append(f"- Rows (one per `patches/*/patches.yml` entry): **{len(rows)}**")
    lines.append(
        "- Authority read: `testkit/CMakeLists.txt`, `testkit/runtime-profiles.yml`, "
        "`ci/run-host-tier.py`, `ci/run-test-tier.sh`, `tests/`, `.github/workflows/`"
    )
    lines.append(
        "- Frozen (read-only) packaging layer: `patches/`, `locks/patch-stack/`, the patch "
        "materializer/export/preflight commands"
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "Every declared test in patch metadata is resolved with West's own loader "
        "(`west_commands/test_manifest.load_test_profile`), so `use:` profiles, artifact/resource "
        "profiles and compact RED axes are expanded exactly as `west test` expands them. Each "
        "declared test is then classified by what covers it in the long-term authority:"
    )
    lines.append("")
    lines.append("| coverage | meaning |")
    lines.append("| --- | --- |")
    lines.append("| `CTEST-REGISTERED` | an `add_compat_test()`/`add_test()` registration in `testkit/CMakeLists.txt` matches the test name, its source asset, or the entry's bead |")
    lines.append("| `CTEST-REFERENCE` | the test is a `ctest:`/`ctest-name:`/`ctest-label:` binding into an existing CTest registration |")
    lines.append("| `TIER-CONTRACT` | the declared script is a `tests/` host contract `ci/run-host-tier.py` runs |")
    lines.append("| `EXCLUDED-CONTRACT` | the declared script is a host contract the tier deliberately excludes |")
    lines.append("| `AUTHORITY-ASSET-UNWIRED` | the asset is already in the workspace `tests/` authority but no CTest registration runs it |")
    lines.append("| `PATCH-INTERNAL` | the declared asset lives inside a patched module, not in `tests/` |")
    lines.append("| `NO-ASSET` | no script asset (a build target, guest command, cmake configure or object check) |")
    lines.append("")
    lines.append(
        "Source-owned CMake/CTest registrations elsewhere in the checkout are also authority "
        "(docs/test-infra.md: *keep fixture sources and source-owned CMake/CTest registrations "
        "colocated with their owning project*). The generator scans the sibling `darling` checkout "
        f"for `add_test()` names and `bead:` labels: it found {len(source_regs['names'])} source-owned "
        f"test names and {len(source_regs['beads'])} source bead labels "
        f"({', '.join(sorted(source_regs['beads'])) or 'none'})."
    )
    lines.append("")
    lines.append("### Disposition")
    lines.append("")
    lines.append("- **MIGRATED** — the behavior is preserved as a long-term authority test. `at risk = yes`")
    lines.append("  marks the rows whose coverage exists *only* in patch metadata today: they are the")
    lines.append("  migration-first backlog and carry a proposed registration below.")
    lines.append("- **ALREADY_REGISTERED** — the patch metadata is itself a reference (`ctest:`) into the")
    lines.append("  existing CTest suite; no migration is or was needed.")
    lines.append("- **INTENTIONALLY_RETIRED <reason>** — the behavior is deliberately dropped for a product")
    lines.append("  reason (not a machinery reason).")
    lines.append("- **PACKAGING-ONLY <machinery>** — the row exists only to exercise the packaging /")
    lines.append("  patch-metadata machinery (the `west patch status` drift gate, the RED-proof gate harness,")
    lines.append("  or runtime instrumentation); the machinery is named.")
    lines.append("")
    lines.append(
        "A behavioral test never disappears without a disposition and a reason: every row below is"
    )
    lines.append(
        "accounted for, and the at-risk rows state the CTest/testkit registration that would keep"
    )
    lines.append("their behavior.")
    lines.append("")

    # ---- Migration-first section ------------------------------------------
    lines.append("## Migration-first backlog (behaviors covered only by patch metadata)")
    lines.append("")
    lines.append(
        f"**{len(at_risk_rows)}** of **{len(rows)}** entries ({at_risk_tests} declared tests) are"
    )
    lines.append(
        "covered only by a patch-metadata test. When the metadata test runner retires, nothing in"
    )
    lines.append("CTest/testkit would run these; they must be migrated first.")
    lines.append("")
    lines.append("Ordered by migration cost:")
    lines.append("")
    tier1 = sum(1 for r in at_risk_rows for t in r["tests"] if t["coverage"] == "AUTHORITY-ASSET-UNWIRED")
    tier2 = sum(1 for r in at_risk_rows for t in r["tests"] if t["coverage"] == "PATCH-INTERNAL")
    tier3 = sum(1 for r in at_risk_rows for t in r["tests"] if t["coverage"] == "NO-ASSET")
    no_test = sum(1 for r in at_risk_rows if not r["tests"])
    lines.append("| tier | at-risk rows' declared tests | meaning |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| 1 — register the existing asset | {tier1} | the fixture already lives in `tests/`; an `add_compat_test()`/EXPLICIT_CONTRACTS entry runs it |"
    )
    lines.append(
        f"| 2 — extract then register | {tier2} | the asset lives inside a patched module and must move into `tests/` (or its owning project) first |"
    )
    lines.append(
        f"| 3 — write a new test | {tier3} | no script asset; a build target, guest command, cmake configure or object check has no CTest registration |"
    )
    lines.append(
        f"| 3b — no test at all | {no_test} rows | the entry declares no test; its `test-exception` is a coverage claim, not a test |"
    )
    lines.append("")
    lines.append("| profile | entry | bead | declared test(s) at risk | asset | proposed registration |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in at_risk_rows:
        risk_tests = [
            t for t in row["tests"]
            if t["coverage"] not in {"CTEST-REGISTERED", "CTEST-REFERENCE", "TIER-CONTRACT"}
        ]
        names = "<br>".join(escape(render_test(t["axes"])) for t in risk_tests) or "<em>none declared</em>"
        kinds = ", ".join(sorted({t["coverage"] for t in risk_tests})) or "NO-ASSET"
        proposals = "<br>".join(escape(p) for p in row["proposals"]) or "<em>add an automated test</em>"
        lines.append(
            f"| {row['profile']} | `{escape(row['path'])}` | `{row['bead']}` | {names} | {kinds} | {proposals} |"
        )
    lines.append("")

    # ---- Histogram ---------------------------------------------------------
    lines.append("## Disposition histogram")
    lines.append("")
    lines.append("| disposition | rows |")
    lines.append("| --- | --- |")
    for key in ("MIGRATED", "ALREADY_REGISTERED", "INTENTIONALLY_RETIRED", "PACKAGING-ONLY"):
        lines.append(f"| {key} | {histogram.get(key, 0)} |")
    lines.append(f"| **total** | **{len(rows)}** |")
    lines.append("")
    lines.append(
        f"Of the MIGRATED rows, {len(at_risk_rows)} are at risk (patch-metadata-only); the other "
        f"{histogram.get('MIGRATED', 0) - len(at_risk_rows)} are already covered by a testkit "
        "registration or a tests/ host contract."
    )
    lines.append("")
    lines.append(
        f"`INTENTIONALLY_RETIRED` is **{histogram.get('INTENTIONALLY_RETIRED', 0)}**: no entry "
        "declares a product behavior that this census can show is deliberately dropped. Every "
        "behavioral row is either already in the authority or on the migration-first backlog, so "
        "nothing is silently retired. A row may only move to `INTENTIONALLY_RETIRED` with a "
        "product reason (a superseded feature or an abandoned experiment), never to shed work."
    )
    lines.append("")

    # ---- Full census -------------------------------------------------------
    lines.append("## Full census")
    lines.append("")
    lines.append(
        "| # | profile | entry | bead | declared tests (resolved) | covered today | disposition | at risk |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, row in enumerate(rows, start=1):
        if row["tests"]:
            declared = "<br>".join(escape(render_test(t["axes"])) for t in row["tests"])
        else:
            exc = row["exception"] or {}
            declared = (
                "<em>none</em> — test-exception: " + escape(str(exc.get("reason", "none")))
            )
        covered = "<br>".join(
            escape(f"{test_identity(t['axes'])}: {t['coverage']} — {t['evidence']}")
            for t in row["tests"]
        ) or "nothing (no test declared)"
        lines.append(
            f"| {index} | {row['profile']} | `{escape(row['path'])}` | `{row['bead']}` | {declared} "
            f"| {covered} | {row['disposition']} — {escape(row['reason'])} "
            f"| {'yes' if row['at_risk'] else 'no'} |"
        )
    lines.append("")

    # ---- Runtime profiles / unknowns --------------------------------------
    lines.append("## Could not be determined")
    lines.append("")
    unknowns = [
        "The at-risk count treats *any* test declared in `patches/*/patches.yml` as a "
        "patch-metadata test, even when its fixture already lives in `tests/`. That is the literal "
        "reading of \"covered only by a patch-metadata test\": the asset survives the freeze, but "
        "nothing in CTest/testkit runs it. The migration-first tiers above separate those (cheap to "
        "register) from assets that must be extracted first (tier 2).",
        "Whether an `AUTHORITY-ASSET-UNWIRED` fixture will actually be run after the metadata "
        "runner retires is not observable from static text: the census records that no CTest "
        "registration runs it today.",
        "`CTEST-REFERENCE` rows are recorded with the declared binding text and a static resolution "
        "check (exact name, `<env>/<name>` against a testkit registration, `bead:<id>` against "
        "testkit or source-owned labels, or the `eunion-host` label). A discovered CTest index "
        "needs a configured build, which this read-only census does not create; bindings whose "
        "target only exists after materializing a profile are reported `unverified`.",
        "Patch entries with a `test-exception` and no declared test have no test to classify; the "
        "census records the exception's own coverage claim and marks the row at risk unless the "
        "claim is patch-metadata machinery.",
        "The `test-profiles:`/`artifact-profiles:`/`resource-profiles:` bindings resolve through "
        "West's loader, but several declared scripts name assets inside patched modules that are "
        "only materialized by applying the patch; those are reported as `PATCH-INTERNAL`. The "
        "census does not apply any patch to check the asset exists in the materialized tree.",
        "The source-owned scan reads `CMakeLists.txt` files under the sibling `darling` checkout "
        "as it stands (unpatched). A registration that only exists in a materialized profile is "
        "not seen, so source-owned coverage may be under-counted.",
    ]
    for item in unknowns:
        lines.append(f"- {item}")
    lines.append("")
    lines.append(
        "Runtime profiles named by declared tests and defined in `testkit/runtime-profiles.yml`: "
        + ", ".join(f"`{p}`" for p in sorted(runtime_profiles))
        + "."
    )
    lines.append("")

    output = Path(args.output)
    output.write_text("\n".join(lines) + "\n")
    try:
        shown = output.relative_to(ROOT)
    except ValueError:
        shown = output
    print(
        f"wrote {shown}: {len(rows)} rows, "
        f"{len(at_risk_rows)} at risk, histogram {histogram}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

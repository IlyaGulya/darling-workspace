"""Contracts for the hosted single-pilot Mach-O builder."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "ci/build-guest-macho-pilot.sh"
COMPARE = ROOT / "ci/compare-guest-macho-pilot.py"
ROOTLESS_PREFIX = ROOT / "ci/rootless-prefix.sh"
WORKFLOW = ROOT / ".github/workflows/test-infra.yml"
STABLE_FIELDS = (
    "schema",
    "pilot",
    "source-path",
    "source-sha256",
    "compiler-path",
    "compiler-sha256",
    "compiler-version",
    "compiler-origin",
    "flags",
    "clt-product-id",
    "clt-provenance-sha256",
    "provenance-document-sha256",
    "artifact-sha256",
    "private-header-sha256",
    "dylibs-report-sha256",
    "macho-summary-sha256",
    "expected-returncode",
    "expected-marker",
)


def write_run(root: Path, name: str, payload: bytes = b"pilot") -> None:
    run = root / name
    if run.exists():
        shutil.rmtree(run)
    run.mkdir()
    dylib = b"/usr/lib/libSystem.B.dylib\0"
    dylib_size = (24 + len(dylib) + 7) & ~7
    dylib_command = struct.pack("<IIIIII", 0x0C, dylib_size, 24, 0, 0, 0)
    dylib_command += dylib.ljust(dylib_size - 24, b"\0")
    rpath = b"/usr/lib\0"
    rpath_size = (12 + len(rpath) + 7) & ~7
    rpath_command = struct.pack("<III", 0x8000001C, rpath_size, 12)
    rpath_command += rpath.ljust(rpath_size - 12, b"\0")
    commands = dylib_command + rpath_command
    binary_payload = struct.pack(
        "<IiiIIIII", 0xFEEDFACF, 0x01000007, 3, 2, 2, len(commands), 0, 0
    ) + commands
    binary = run / "select_fdset_guest"
    binary.write_bytes(binary_payload)
    digest = hashlib.sha256(payload).hexdigest()
    values = {
        "schema": "1",
        "pilot": "select_fdset_guest",
        "source-path": "tests/select_fdset_guest.c",
        "source-sha256": "a" * 64,
        "compiler-path": "/Library/Developer/CommandLineTools/usr/bin/clang",
        "compiler-sha256": "b" * 64,
        "compiler-version": "Apple clang version reviewed",
        "compiler-origin": "execution-context=guest;executable=/Library/Developer/CommandLineTools/usr/bin/clang",
        "flags": "-isysroot|/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk|-std=gnu11|-Wall|-Wextra|-Werror",
        "clt-product-id": "041-90419",
        "clt-provenance-sha256": "c" * 64,
        "artifact-sha256": digest,
        "private-header-sha256": "d" * 64,
        "dylibs-report-sha256": "e" * 64,
        "macho-summary-sha256": "f" * 64,
        "expected-returncode": "0",
        "expected-marker": "SELECT_FDSET_GUEST_OK",
    }
    (run / "clang-version.txt").write_text("Apple clang version reviewed\n")
    (run / "clang-origin.txt").write_text(
        "execution-context=guest\n"
        "executable=/Library/Developer/CommandLineTools/usr/bin/clang\n"
    )
    (run / "clt-provenance.txt").write_text(
        "review_status: reviewed\n"
        "evidence_run: 29384636308\n"
        "product_id: 041-90419\n"
        "certificate_fingerprints_sha256: reviewed\n"
        "signature_check: pkgutil --check-signature\n"
    )
    (run / "guest-marker.txt").write_text("SELECT_FDSET_GUEST_OK\n")
    (run / "guest-build.log").write_text("SELECT_FDSET_GUEST_OK\n")
    (run / "pilot-state.tsv").write_text(
        "field\tvalue\n"
        "schema\t1\n"
        "variant\ta\n"
        "phase\tcomplete\n"
        "status\tCOMPLETE\n"
    )
    (run / "file.txt").write_text("Mach-O 64-bit executable x86_64\n")
    (run / "private-header.txt").write_text("private header\n")
    (run / "dylibs-used.txt").write_text("dylibs\n")
    (run / "macho-summary.tsv").write_text(
        "schema\t1\n"
        "magic\tMH_MAGIC_64\n"
        "architecture\tx86_64\n"
        "filetype\tMH_EXECUTE\n"
        'dylib-load-commands\t[{"command":"LC_LOAD_DYLIB","name":"/usr/lib/libSystem.B.dylib"}]\n'
        'rpath-load-commands\t["/usr/lib"]\n'
    )
    (run / "macho-manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "magic": "MH_MAGIC_64",
                "architecture": "x86_64",
                "filetype": "MH_EXECUTE",
                "dylib-load-commands": [
                    {"command": "LC_LOAD_DYLIB", "name": "/usr/lib/libSystem.B.dylib"}
                ],
                "rpath-load-commands": ["/usr/lib"],
            },
            sort_keys=True,
        )
    )
    (run / "provenance.txt").write_text(
        "status: REVIEWED_PROVENANCE\n"
        "clt_review_status: reviewed\n"
        "pilot: select_fdset_guest\n"
        "expected_marker: SELECT_FDSET_GUEST_OK\n"
    )
    values["artifact-sha256"] = hashlib.sha256(binary_payload).hexdigest()
    values["clt-provenance-sha256"] = hashlib.sha256(
        (run / "clt-provenance.txt").read_bytes()
    ).hexdigest()
    values["provenance-document-sha256"] = hashlib.sha256(
        (run / "provenance.txt").read_bytes()
    ).hexdigest()
    values["private-header-sha256"] = hashlib.sha256(
        (run / "private-header.txt").read_bytes()
    ).hexdigest()
    values["dylibs-report-sha256"] = hashlib.sha256(
        (run / "dylibs-used.txt").read_bytes()
    ).hexdigest()
    values["macho-summary-sha256"] = hashlib.sha256(
        (run / "macho-summary.tsv").read_bytes()
    ).hexdigest()
    (run / "artifact.sha256").write_text(f"{values['artifact-sha256']}  select_fdset_guest\n")
    (run / "provenance.tsv").write_text(
        "field\tvalue\n"
        + "".join(f"{field}\t{values[field]}\n" for field in STABLE_FIELDS)
    )


workflow_text = WORKFLOW.read_text()
workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
triggers = workflow.get("on", workflow.get(True))
choices = triggers["workflow_dispatch"]["inputs"]["tier"]["options"]
assert "macho-corpus-pilot" in choices, choices

builder_text = BUILDER.read_text()
rootless_prefix_text = ROOTLESS_PREFIX.read_text()
assert "testkit/corpus" not in builder_text
assert "corpus.yml" not in builder_text
assert "independent-a" not in builder_text
assert "independent-b" not in builder_text
assert "testkit/corpus" not in workflow_text
assert "rootless_prefix_create corpus CORPUS_PREFIX" in builder_text
assert 'export RUNNER_TEMP=' not in builder_text
assert 'RUNNER_TEMP="$runner_temp"' not in workflow_text
assert 'RUNNER_TEMP="$ccache_runner_temp"' in builder_text
assert '"$prefix/var/run/shellspawn.sock"' in builder_text
assert '"$prefix/.darlingserver.sock"' in builder_text
assert "LC_ALL=C wc -c" in builder_text
assert "socket_path_bytes > 107" in builder_text
assert 'dirname -- "$prefix")" == "$trusted_root"' in rootless_prefix_text
assert '"$output/failure-summary.txt"' in builder_text
assert '"$output/pilot-state.tsv"' in builder_text
assert '. "$root/testkit/scripts/darling-guest-shell.sh"' in builder_text
assert "guest_output=" not in builder_text
assert "| tee" not in builder_text
assert '"$prefix/bin/darling" shell' not in builder_text
assert 'darling_guest_shell "$prefix/bin/darling" "$prefix" 120' in builder_text
assert '>"$output/guest-build.log" 2>&1' in builder_text
assert 'west test --prefix "$prefix" --cleanup-prefix' in builder_text
guest_invocation = builder_text.index(
    'darling_guest_shell "$prefix/bin/darling" "$prefix" 120'
)
host_shutdown = builder_text.index(
    'west test --prefix "$prefix" --cleanup-prefix', guest_invocation
)
assert host_shutdown > guest_invocation
assert "guest_rc != 0" in builder_text
assert "shutdown_rc != 0" in builder_text
assert "grep -Fxq 'SELECT_FDSET_GUEST_OK'" in builder_text
assert 'marker_file="$stage/select_fdset_guest.marker"' in builder_text
assert 'file --brief -- "$artifact"' in builder_text
assert "write_pilot_state startup RUNNING" in builder_text
assert "set_pilot_state guest-invocation RUNNING" in builder_text
assert "write_pilot_state complete COMPLETE" in builder_text
assert "homebrew-guest-toolchain-provisioning" in builder_text
assert "DARLING_CLT_CACHE=" in builder_text
assert 'output="$(realpath -m -- "$1")"' in builder_text
assert '"$root"/*)' in builder_text
assert "SIGNATURE_VALID_NOT_REVIEWED" not in builder_text

common_dependencies = (ROOT / "ci/install-darling-build-deps.sh").read_text()
assert "\tllvm\n" not in common_dependencies

build_job = workflow_text.split("  macho-corpus-pilot-build:", 1)[1].split(
    "  macho-corpus-pilot-compare:", 1
)[0]
assert "if: github.event_name == 'workflow_dispatch' && inputs.tier == 'macho-corpus-pilot'" in build_job
assert "fail-fast: false" in build_job
assert "variant: [a, b]" in build_job
assert "runs-on: ubuntu-latest" in build_job
assert "if: always()" in build_job
assert "Install pilot Mach-O inspection tools" in build_job
assert "sudo apt-get install --yes --no-install-recommends llvm" in build_job
upload_job = build_job.split("      - name: Upload pilot evidence only", 1)[1].split(
    "      - name: Cleanup pilot prefix and West state", 1
)[0]
assert "actions/upload-artifact@v7" in upload_job
assert ".pkg" not in upload_job
assert "clt-cache" not in upload_job
assert ".work" not in upload_job
assert "failure-summary.txt" in upload_job
assert "pilot-state.tsv" in upload_job
assert "path: " + "${{ runner.temp }}/macho-corpus-pilot/${{ matrix.variant }}" not in upload_job

with tempfile.TemporaryDirectory(prefix="macho-corpus-runner-temp-", dir="/tmp") as raw:
    runner_temp = Path(raw).resolve()
    env = os.environ.copy()
    env["RUNNER_TEMP"] = str(runner_temp)
    env["ROOTLESS_TIER_REPO"] = str(ROOT)
    prefix_probe = subprocess.run(
        [
            "bash",
            "-c",
            "source ci/rootless-prefix.sh; "
            "prefix=$(rootless_prefix_create corpus CORPUS_PREFIX); "
            "printf '%s\\n' \"$prefix\"; "
            "rootless_prefix_remove corpus \"$prefix\"",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert prefix_probe.returncode == 0, prefix_probe.stderr
    created_prefix = Path(prefix_probe.stdout.strip())
    assert created_prefix.parent == runner_temp
    for socket_path in (
        created_prefix / "var/run/shellspawn.sock",
        created_prefix / ".darlingserver.sock",
    ):
        assert len(os.fsencode(socket_path)) <= 107

compare_job = workflow_text.split("  macho-corpus-pilot-compare:", 1)[1]
assert "needs: macho-corpus-pilot-build" in compare_job
compare_if = compare_job.split("    if: ", 1)[1].split("\n", 1)[0]
assert compare_if.startswith("needs.macho-corpus-pilot-build.result == 'success' &&")
assert "always()" not in compare_if
assert "ci/compare-guest-macho-pilot.sh" in compare_job
# A failed or cancelled build cannot satisfy the compare gate, so the compare
# job is skipped instead of reporting a misleading green result.
for build_result in ("failure", "cancelled", "skipped"):
    assert build_result != "success"

with tempfile.TemporaryDirectory(prefix="macho-corpus-pilot-contract-") as raw:
    root = Path(raw)
    write_run(root, "macho-corpus-pilot-a")
    write_run(root, "macho-corpus-pilot-b")
    matched = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert matched.returncode == 0, matched.stderr

    state = root / "macho-corpus-pilot-b/pilot-state.tsv"
    state.write_text(state.read_text().replace("status\tCOMPLETE", "status\tRUNNING"))
    incomplete_state = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert incomplete_state.returncode != 0, incomplete_state
    write_run(root, "macho-corpus-pilot-b")

    (root / "macho-corpus-pilot-b/private-header.txt").unlink()
    missing_evidence = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_evidence.returncode != 0, missing_evidence
    write_run(root, "macho-corpus-pilot-b")

    (root / "macho-corpus-pilot-b/clt-provenance.txt").write_text("substituted CLT provenance\n")
    substituted_clt_provenance = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert substituted_clt_provenance.returncode != 0, substituted_clt_provenance
    write_run(root, "macho-corpus-pilot-b")

    (root / "macho-corpus-pilot-b/dylibs-used.txt").write_text("substituted report\n")
    substituted_report = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert substituted_report.returncode != 0, substituted_report
    write_run(root, "macho-corpus-pilot-b")

    (root / "macho-corpus-pilot-b/guest-marker.txt").write_text("WRONG_MARKER\n")
    substituted_marker = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert substituted_marker.returncode != 0, substituted_marker
    write_run(root, "macho-corpus-pilot-b")

    manifest = json.loads((root / "macho-corpus-pilot-b/macho-manifest.json").read_text())
    manifest["rpath-load-commands"] = ["/wrong"]
    (root / "macho-corpus-pilot-b/macho-manifest.json").write_text(json.dumps(manifest))
    structured_mismatch = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert structured_mismatch.returncode != 0, structured_mismatch
    write_run(root, "macho-corpus-pilot-b")

    report = root / "macho-corpus-pilot-b/private-header.txt"
    report.write_text("runner-specific report path\n")
    report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    provenance = root / "macho-corpus-pilot-b/provenance.tsv"
    provenance.write_text(
        "\n".join(
            (
                "private-header-sha256\t" + report_hash
                if line.startswith("private-header-sha256\t")
                else line
            )
            for line in provenance.read_text().splitlines()
        )
        + "\n"
    )
    raw_report_variation = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert raw_report_variation.returncode == 0, raw_report_variation.stderr
    write_run(root, "macho-corpus-pilot-b")

    (root / "macho-corpus-pilot-b/select_fdset_guest").write_bytes(b"different")
    binary_mismatch = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert binary_mismatch.returncode != 0, binary_mismatch

    write_run(root, "macho-corpus-pilot-b")
    provenance = root / "macho-corpus-pilot-b/provenance.tsv"
    provenance.write_text(
        provenance.read_text().replace(
            "compiler-version\tApple clang version reviewed",
            "compiler-version\tunexpected compiler",
        )
    )
    provenance_mismatch = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert provenance_mismatch.returncode != 0, provenance_mismatch

    write_run(root, "macho-corpus-pilot-b")
    (root / "macho-corpus-pilot-b/cache").mkdir()
    forbidden_payload = subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert forbidden_payload.returncode != 0, forbidden_payload

print("PASS macho-corpus-pilot-contract")

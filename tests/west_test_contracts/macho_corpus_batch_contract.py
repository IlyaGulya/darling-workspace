"""Contracts for the Phase 3B compile-only Mach-O batch."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path



ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "ci/build-guest-macho-batch.sh"
COMPARE = ROOT / "ci/compare-guest-macho-batch.py"
TSV_FIELD_HELPER = ROOT / "ci/read-tsv-field.py"
GUEST_SCRIPT_GENERATOR = ROOT / "ci/emit-guest-macho-batch-script.py"
sys.path.insert(0, str(ROOT / "ci"))
import guest_macho_batch_specs as specs_module

compare_loader = importlib.util.spec_from_file_location("compare_guest_macho_batch", COMPARE)
assert compare_loader and compare_loader.loader
compare_module = importlib.util.module_from_spec(compare_loader)
sys.modules[compare_loader.name] = compare_module
compare_loader.loader.exec_module(compare_module)

FIXTURE_SPECS = specs_module.FIXTURE_SPECS
ANCHOR_SHA256 = specs_module.ANCHOR_ARTIFACT_SHA256
FIXTURE_FILES = set(compare_module.FIXTURE_FILES)
ROOT_FILES = set(compare_module.ROOT_FILES)
MAX_BATCH_GUEST_LOG_BYTES = compare_module.MAX_BATCH_GUEST_LOG_BYTES


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_binary(payload: bytes) -> bytes:
    dylib = b"/usr/lib/libSystem.B.dylib\0"
    dylib_size = (24 + len(dylib) + 7) & ~7
    dylib_command = struct.pack("<IIIIII", 0x0C, dylib_size, 24, 0, 0, 0)
    dylib_command += dylib.ljust(dylib_size - 24, b"\0")
    rpath = b"/usr/lib\0"
    rpath_size = (12 + len(rpath) + 7) & ~7
    rpath_command = struct.pack("<III", 0x8000001C, rpath_size, 12)
    rpath_command += rpath.ljust(rpath_size - 12, b"\0")
    commands = dylib_command + rpath_command
    header = struct.pack(
        "<IiiIIIII", 0xFEEDFACF, 0x01000007, 3, 2, 2, len(commands), 0, 0
    ) + commands
    return header + payload




def write_run(root: Path, name: str) -> None:
    run = root / name
    if run.exists():
        shutil.rmtree(run)
    fixtures = run / "fixtures"
    fixtures.mkdir(parents=True)
    rows = []
    anchor = ROOT / "testkit/fixtures/guest-macho/v1/bin/select_fdset_guest"
    for spec in FIXTURE_SPECS:
        fixture = fixtures / spec.name
        fixture.mkdir()
        binary = fixture / spec.name
        if spec.name == "select_fdset_guest":
            shutil.copyfile(anchor, binary)
            assert sha256(binary) == ANCHOR_SHA256
        else:
            binary.write_bytes(synthetic_binary(spec.name.encode()))
        manifest = compare_module.parsed_manifest(binary)
        (fixture / "macho-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        (fixture / "macho-summary.tsv").write_text(
            "".join(
                f"{key}\t{value}\n"
                for key, value in compare_module.expected_summary(manifest).items()
            )
        )
        version = "Apple clang version 17.0.0 (guest reviewed)\n"
        origin = (
            "execution-context=guest\n"
            "executable=/Library/Developer/CommandLineTools/usr/bin/clang\n"
        )
        clt = (
            "review_status: reviewed\n"
            "evidence_run: 29384636308\n"
            "product_id: 041-90419\n"
            "certificate_fingerprints_sha256: reviewed\n"
            "signature_check: pkgutil --check-signature\n"
        )
        compile_status = (
            "field\tvalue\n"
            f"fixture\t{spec.name}\n"
            "compile-status\tPASS\n"
            "compile-exit-code\t0\n"
        )
        if spec.name == "select_fdset_guest":
            runtime_log = f"{spec.expected_marker}\n"
            runtime_evidence = (
                "field\tvalue\n"
                f"fixture\t{spec.name}\n"
                "runtime-mode\tanchor\n"
                "runtime-status\tOBSERVED\n"
                "runtime-exit-code\t0\n"
                f"observed-marker\t{spec.expected_marker}\n"
            )
            runtime_mode = "anchor"
            runtime_status = "OBSERVED"
            observed_marker = spec.expected_marker
        else:
            runtime_log = "RUNTIME_NOT_RUN_COMPILE_ONLY\n"
            runtime_evidence = (
                "field\tvalue\n"
                f"fixture\t{spec.name}\n"
                "runtime-mode\tcompile-only\n"
                "runtime-status\tNOT_RUN\n"
                "runtime-exit-code\tNOT_RUN\n"
                "observed-marker\tNOT_OBSERVED\n"
            )
            runtime_mode = "compile-only"
            runtime_status = "NOT_RUN"
            observed_marker = "NOT_OBSERVED"
        (fixture / "clang-version.txt").write_text(version)
        (fixture / "clang-origin.txt").write_text(origin)
        (fixture / "clt-provenance.txt").write_text(clt)
        (fixture / "compile-status.tsv").write_text(compile_status)
        (fixture / "compile.log").write_text("clang compile complete\n")
        (fixture / "runtime.log").write_text(runtime_log)
        (fixture / "runtime-evidence.tsv").write_text(runtime_evidence)
        (fixture / "file.txt").write_text("Mach-O 64-bit executable x86_64\n")
        (fixture / "private-header.txt").write_text("structured private header\n")
        (fixture / "dylibs-used.txt").write_text("structured dylibs and rpaths\n")
        (fixture / "artifact.sha256").write_text(f"{sha256(binary)}  {spec.name}\n")
        provenance_document = (
            "status: REVIEWED_PROVENANCE\n"
            "clt_review_status: reviewed\n"
            "batch: guest-macho-phase-3b\n"
            f"fixture: {spec.name}\n"
            f"expected_marker: {spec.expected_marker}\n"
            f"runtime_mode: {runtime_mode}\n"
            f"runtime_status: {runtime_status}\n"
            f"observed_marker: {observed_marker}\n"
        )
        (fixture / "provenance.txt").write_text(provenance_document)
        values = {
            "schema": "2",
            "fixture": spec.name,
            "source-project": spec.source_project,
            "source-path": spec.source_path,
            "source-revision": "a" * 40,
            "source-sha256": spec.source_sha256,
            "compile-flags": "|".join(spec.compile_flags),
            "link-flags": "|".join(spec.link_flags) or "-",
            "runtime-launcher-sha256": "c" * 64,
            "runtime-server-sha256": "d" * 64,
            "runtime-root": "/installed/runtime",
            "compiler-path": "/Library/Developer/CommandLineTools/usr/bin/clang",
            "compiler-sha256": "b" * 64,
            "compiler-version": " ".join(version.split()),
            "compiler-origin": ";".join(origin.splitlines()),
            "clt-product-id": "041-90419",
            "clt-provenance-sha256": sha256(fixture / "clt-provenance.txt"),
            "provenance-document-sha256": sha256(fixture / "provenance.txt"),
            "artifact-sha256": sha256(binary),
            "compile-status-sha256": sha256(fixture / "compile-status.tsv"),
            "compile-log-sha256": sha256(fixture / "compile.log"),
            "runtime-evidence-sha256": sha256(fixture / "runtime-evidence.tsv"),
            "runtime-log-sha256": sha256(fixture / "runtime.log"),
            "private-header-sha256": sha256(fixture / "private-header.txt"),
            "dylibs-report-sha256": sha256(fixture / "dylibs-used.txt"),
            "macho-summary-sha256": sha256(fixture / "macho-summary.tsv"),
            "expected-returncode": "0",
            "expected-marker": spec.expected_marker,
            "runtime-mode": runtime_mode,
            "runtime-status": runtime_status,
            "observed-marker": observed_marker,
        }
        (fixture / "provenance.tsv").write_text(
            "field\tvalue\n"
            + "".join(f"{key}\t{value}\n" for key, value in values.items())
        )
        rows.append(
            "\t".join(
                (
                    spec.name,
                    spec.source_project,
                    spec.source_path,
                    "a" * 40,
                    spec.source_sha256,
                    sha256(binary),
                    "c" * 64,
                    "d" * 64,
                    spec.expected_marker,
                )
            )
        )
    (run / "batch-manifest.tsv").write_text(
        "fixture\tsource-project\tsource-path\tsource-revision\tsource-sha256\tartifact-sha256\truntime-launcher-sha256\truntime-server-sha256\texpected-marker\n"
        + "\n".join(rows)
        + "\n"
    )
    (run / "batch-state.tsv").write_text(
        "field\tvalue\n"
        "schema\t1\n"
        f"variant\t{name.rsplit('-', 1)[-1]}\n"
        "fixture-count\t14\n"
        "phase\tcomplete\n"
        "status\tCOMPLETE\n"
    )
    (run / "failure-summary.txt").write_text(
        "status: success\n"
        f"variant: {name.rsplit('-', 1)[-1]}\n"
        "fixture-count: 14\n"
        "exit-code: 0\n"
        "phase: evidence\n"
    )
    (run / "batch-guest.log").write_text("Warning: guest diagnostic output\n")


def compare(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(COMPARE), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


specs_module.validate_specs()
assert len(FIXTURE_SPECS) == 14
for spec in FIXTURE_SPECS:
    if spec.source_project == "darling-workspace":
        assert sha256(ROOT / spec.source_path) == spec.source_sha256
assert next(spec for spec in FIXTURE_SPECS if spec.name == "ulock_eintr_retry_guest").compile_flags[-1] == "-pthread"

with tempfile.TemporaryDirectory(prefix="macho-corpus-batch-startup-contract-") as raw:
    root = Path(raw)
    fake_bin = root / "bin"
    fake_bin.mkdir()
    missing_spec = FIXTURE_SPECS[0]
    header = (
        "name\tsource_project\tsource_path\tsource_sha256\tcompile_flags\t"
        "link_flags\texpected_marker"
    )
    row = "\t".join(
        (
            missing_spec.name,
            missing_spec.source_project,
            "tests/phase3b-missing-source.c",
            missing_spec.source_sha256,
            json.dumps(missing_spec.compile_flags, separators=(",", ":")),
            json.dumps(missing_spec.link_flags, separators=(",", ":")),
            missing_spec.expected_marker,
        )
    )
    python_wrapper = fake_bin / "python3"
    python_wrapper.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *guest_macho_batch_specs.py*--emit-tsv*)\n"
        f"    printf '%s\\n' {shlex.quote(header)}\n"
        f"    printf '%s\\n' {shlex.quote(row)}\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    python_wrapper.chmod(0o755)
    west_wrapper = fake_bin / "west"
    west_wrapper.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f"  topdir) printf '%s\\n' {shlex.quote(str(ROOT.parent))} ;;\n"
        f"  config) printf '%s\\n' {shlex.quote(ROOT.name)} ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    west_wrapper.chmod(0o755)
    output = root / "batch-output"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["RUNNER_TEMP"] = str(root / "runner-temp")
    result = subprocess.run(
        [str(BUILDER), str(output), "a"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    state = dict(
        line.split("\t", 1)
        for line in (output / "batch-state.tsv").read_text().splitlines()[1:]
    )
    assert state["status"] == "FAILED"
    assert state["phase"] == "source-validation"
    summary = dict(
        line.split(": ", 1)
        for line in (output / "failure-summary.txt").read_text().splitlines()
    )
    assert summary["status"] == "failure"
    assert int(summary["exit-code"]) != 0
    assert summary["phase"] == "source-validation"

with tempfile.TemporaryDirectory(prefix="macho-corpus-batch-contract-") as raw:
    root = Path(raw)
    write_run(root, "macho-corpus-batch-a")
    write_run(root, "macho-corpus-batch-b")
    compile_status = root / "macho-corpus-batch-a/fixtures/abort_with_payload_no_group_broadcast/compile-status.tsv"
    anchor_runtime_status = root / ".anchor-runtime-status.tsv"
    anchor_runtime_status.write_text(
        "field\tvalue\n"
        "fixture\tselect_fdset_guest\n"
        "runtime-status\tEXECUTED\n"
        "runtime-exit-code\t0\n"
    )

    def helper_field(path: Path, field: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TSV_FIELD_HELPER), str(path), field],
            capture_output=True,
            text=True,
            check=False,
        )

    assert helper_field(compile_status, "compile-status").stdout.strip() == "PASS"
    assert helper_field(anchor_runtime_status, "runtime-exit-code").stdout.strip() == "0"
    malformed = root / "malformed.tsv"
    malformed.write_text("field\tvalue\ncompile-status\tPASS\textra\n")
    assert helper_field(malformed, "compile-status").returncode != 0
    duplicate = root / "duplicate.tsv"
    duplicate.write_text("field\tvalue\ncompile-status\tPASS\ncompile-status\tPASS\n")
    assert helper_field(duplicate, "compile-status").returncode != 0
    missing = root / "missing.tsv"
    missing.write_text("field\tvalue\nfixture\tmissing\n")
    assert helper_field(missing, "compile-status").returncode != 0

    fake_compiler = root / "fake-clang"
    fake_compiler.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"${1:-}\" = --version ]; then\n"
        "  printf '%s\\n' 'fake guest clang'\n"
        "  exit 0\n"
        "fi\n"
        "output=\n"
        "previous=\n"
        "for argument in \"$@\"; do\n"
        "  if [ \"$previous\" = -o ]; then output=\"$argument\"; previous=; continue; fi\n"
        "  if [ \"$argument\" = -o ]; then previous=-o; fi\n"
        "done\n"
        "[ -n \"$output\" ]\n"
        "printf '%s\\n' '#!/bin/sh' 'printf \"%s\\n\" SELECT_FDSET_GUEST_OK' > \"$output\"\n"
        "chmod 0755 \"$output\"\n"
    )
    fake_compiler.chmod(0o755)
    fake_sources = root / "fake-sources"
    fake_sources.mkdir()
    fake_manifest = root / "fake-guest-manifest.tsv"
    fake_rows = []
    for name in ("fake_fixture", "select_fdset_guest"):
        source = fake_sources / f"{name}.c"
        source.write_text("int main(void) { return 0; }\n")
        fake_rows.append(
            "\t".join(
                (
                    name,
                    str(source),
                    str(root / name),
                    str(root / f"{name}.clang-version"),
                    str(root / f"{name}.clang-origin"),
                    "[]",
                    "[]",
                )
            )
        )
    fake_manifest.write_text(
        "name\tguest-source\tguest-binary\tversion\torigin\tcompile-flags\tlink-flags\n"
        + "\n".join(fake_rows)
        + "\n"
    )
    generated_script = root / "generated-batch-guest.sh"
    generated = subprocess.run(
        [sys.executable, str(GUEST_SCRIPT_GENERATOR), str(generated_script), str(fake_compiler), str(fake_manifest)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    generated_text = generated_script.read_text()
    references = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", generated_text))
    assert references <= {"compile_failed", "anchor_compile_rc", "compile_rc", "runtime_rc"}
    assert "$anchor_binary" not in generated_text
    executed = subprocess.run(
        [str(generated_script)],
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert executed.returncode == 0, executed.stderr
    anchor_status = root / "select_fdset_guest.runtime-status.tsv"
    assert helper_field(anchor_status, "runtime-exit-code").stdout.strip() == "0"
    assert (root / "select_fdset_guest.runtime.log").read_text() == "SELECT_FDSET_GUEST_OK\n"
    assert helper_field(root / "fake_fixture.compile-status.tsv", "compile-status").stdout.strip() == "PASS"
    assert helper_field(root / "select_fdset_guest.compile-status.tsv", "compile-status").stdout.strip() == "PASS"

    matched = compare(root)
    assert matched.returncode == 0, matched.stderr

    assert {path.name for path in (root / "macho-corpus-batch-a").iterdir()} == ROOT_FILES

    (root / "macho-corpus-batch-a/batch-guest.log").unlink()
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-a")

    (root / "macho-corpus-batch-a/unexpected-root-file").write_text("unexpected\n")
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-a")

    (root / "macho-corpus-batch-a/batch-guest.log").unlink()
    (root / "macho-corpus-batch-a/batch-guest.log").symlink_to(
        root / "macho-corpus-batch-b/batch-guest.log"
    )
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-a")

    (root / "macho-corpus-batch-a/batch-guest.log").write_bytes(
        b"x" * (MAX_BATCH_GUEST_LOG_BYTES + 1)
    )
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-a")

    shutil.rmtree(root / "macho-corpus-batch-b/fixtures/select_fdset_guest")
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    (root / "macho-corpus-batch-b/fixtures/extra_fixture").mkdir()
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    manifest_path = root / "macho-corpus-batch-b/fixtures/select_fdset_guest/macho-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["rpath-load-commands"] = ["/substituted"]
    manifest_path.write_text(json.dumps(manifest))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    provenance = root / "macho-corpus-batch-b/fixtures/select_fdset_guest/provenance.tsv"
    provenance.write_text(
        provenance.read_text().replace("source-revision\t" + "a" * 40, "source-revision\t" + "b" * 40)
    )
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    provenance = root / "macho-corpus-batch-b/fixtures/select_fdset_guest/provenance.tsv"
    provenance.write_text(
        provenance.read_text().replace("runtime-launcher-sha256\t" + "c" * 64, "runtime-launcher-sha256\t" + "e" * 64)
    )
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    compile_status = root / "macho-corpus-batch-b/fixtures/darwin_priority_guest/compile-status.tsv"
    compile_status.write_text(compile_status.read_text().replace("compile-status\tPASS", "compile-status\tFAILED"))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    compile_status = root / "macho-corpus-batch-b/fixtures/darwin_priority_guest/compile-status.tsv"
    compile_status.write_text(compile_status.read_text().replace("compile-exit-code\t0", "compile-exit-code\t7"))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    runtime_evidence = root / "macho-corpus-batch-b/fixtures/darwin_priority_guest/runtime-evidence.tsv"
    runtime_evidence.write_text(runtime_evidence.read_text().replace("runtime-exit-code\tNOT_RUN", "runtime-exit-code\t0"))
    provenance = root / "macho-corpus-batch-b/fixtures/darwin_priority_guest/provenance.tsv"
    provenance_lines = []
    for line in provenance.read_text().splitlines():
        if line.startswith("runtime-evidence-sha256\t"):
            line = "runtime-evidence-sha256\t" + sha256(runtime_evidence)
        provenance_lines.append(line)
    provenance.write_text("\n".join(provenance_lines) + "\n")
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    provenance = root / "macho-corpus-batch-b/fixtures/darwin_priority_guest/provenance.tsv"
    provenance.write_text(provenance.read_text().replace("runtime-status\tNOT_RUN", "runtime-status\tBROKEN"))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    runtime_evidence = root / "macho-corpus-batch-b/fixtures/darwin_priority_guest/runtime-evidence.tsv"
    runtime_evidence.write_text(
        runtime_evidence.read_text().replace(
            "runtime-mode\tcompile-only\nruntime-status\tNOT_RUN\n",
            "runtime-mode\tanchor\nruntime-status\tOBSERVED\n",
        ).replace("observed-marker\tNOT_OBSERVED", "observed-marker\tDARWIN_PRIORITY_GUEST_OK")
    )
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    summary = root / "macho-corpus-batch-b/failure-summary.txt"
    summary.write_text(summary.read_text().replace("exit-code: 0", "exit-code: 1"))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    summary = root / "macho-corpus-batch-b/failure-summary.txt"
    summary.write_text(summary.read_text().replace("phase: evidence", "phase: complete"))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    state = root / "macho-corpus-batch-b/batch-state.tsv"
    state.write_text(state.read_text().replace("phase\tcomplete", "phase\tevidence"))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")

    binary = root / "macho-corpus-batch-b/fixtures/fd_guard_ebadf_guest/fd_guard_ebadf_guest"
    binary.write_bytes(binary.read_bytes() + b"mismatch")
    assert compare(root).returncode != 0

    # Independent images may have different roots, but must execute identical
    # installed launcher/server bytes for the A/B provenance comparison.
    write_run(root, "macho-corpus-batch-b")
    right = root / "macho-corpus-batch-b"
    for provenance in (right / "fixtures").glob("*/provenance.tsv"):
        provenance.write_text(provenance.read_text().replace(
            "runtime-root\t/installed/runtime", "runtime-root\t/another/installation"
        ))
    assert compare(root).returncode == 0
    for provenance in (right / "fixtures").glob("*/provenance.tsv"):
        provenance.write_text(provenance.read_text().replace(
            "runtime-launcher-sha256\t" + "c" * 64,
            "runtime-launcher-sha256\t" + "e" * 64,
        ))
    batch = right / "batch-manifest.tsv"
    batch.write_text(batch.read_text().replace("c" * 64, "e" * 64))
    assert compare(root).returncode != 0
    write_run(root, "macho-corpus-batch-b")
    provenance = right / "fixtures/select_fdset_guest/provenance.tsv"
    provenance.write_text(provenance.read_text().replace(
        "runtime-root\t/installed/runtime", "runtime-root\trelative"
    ))
    assert compare(root).returncode != 0

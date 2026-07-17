"""Executable contract for grouped guest Mach-O validation evidence."""

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from guest_macho_validation import (
    expected_marker_from_corpus,
    finalize_guest_macho_evidence,
    write_guest_macho_evidence,
)

assert expected_marker_from_corpus(
    {
        "repo_root": str(ROOT),
        "corpus": "testkit/fixtures/guest-macho/v1/corpus.yml",
        "fixture": "select_fdset_guest",
    }
) == "SELECT_FDSET_GUEST_OK"


with tempfile.TemporaryDirectory(prefix="west-guest-macho-validation-contract-") as raw:
    evidence_dir = Path(raw)
    invocation = {
        "guest_macho_fixture": True,
        "fixture": "select_fdset_guest",
        "name": "select_fdset_guest_prebuilt",
        "expected_marker": "SELECT_FDSET_GUEST_OK",
    }
    write_guest_macho_evidence(
        evidence_dir,
        invocation,
        type("Result", (), {"returncode": 0, "output": "SELECT_FDSET_GUEST_OK\n"})(),
        "SELECT_FDSET_GUEST_OK",
    )
    write_guest_macho_evidence(
        evidence_dir,
        {
            **invocation,
            "fixture": "darwin_priority_guest",
            "name": "darwin_priority_guest_prebuilt",
            "expected_marker": "DARWIN_PRIORITY_GUEST_OK",
        },
        type("Result", (), {"returncode": 7, "output": "fixture failed\n"})(),
        "DARWIN_PRIORITY_GUEST_OK",
    )

    results = (evidence_dir / "fixture-results.tsv").read_text()
    assert results.splitlines()[0] == "fixture\tname\trc\tmarker\texpected-marker"
    assert "select_fdset_guest\tselect_fdset_guest_prebuilt\t0\tOBSERVED\tSELECT_FDSET_GUEST_OK" in results
    assert "darwin_priority_guest\tdarwin_priority_guest_prebuilt\t7\tNOT_OBSERVED\tDARWIN_PRIORITY_GUEST_OK" in results
    assert (evidence_dir / "select_fdset_guest.log").read_text() == "SELECT_FDSET_GUEST_OK\n"
    assert (evidence_dir / "darwin_priority_guest.log").read_text() == "fixture failed\n"
    try:
        write_guest_macho_evidence(
            evidence_dir,
            invocation,
            type("Result", (), {"returncode": 0, "output": "SELECT_FDSET_GUEST_OK\n"})(),
            "SELECT_FDSET_GUEST_OK",
        )
    except ValueError as error:
        assert "more than once" in str(error)
    else:
        raise AssertionError("guest Mach-O evidence accepted a duplicate fixture")

    substring_dir = evidence_dir / "substring"
    write_guest_macho_evidence(
        substring_dir,
        invocation,
        type("Result", (), {"returncode": 0, "output": "PREFIX_SELECT_FDSET_GUEST_OK_SUFFIX\n"})(),
        "SELECT_FDSET_GUEST_OK",
    )
    assert "NOT_OBSERVED" in (substring_dir / "fixture-results.tsv").read_text()

    failed_marker_dir = evidence_dir / "failed-marker"
    write_guest_macho_evidence(
        failed_marker_dir,
        invocation,
        type("Result", (), {"returncode": 7, "output": "SELECT_FDSET_GUEST_OK\n"})(),
        "SELECT_FDSET_GUEST_OK",
    )
    assert "NOT_OBSERVED" in (failed_marker_dir / "fixture-results.tsv").read_text()

    for missing_marker in ("", None):
        try:
            write_guest_macho_evidence(
                evidence_dir / "missing-marker",
                invocation,
                type("Result", (), {"returncode": 0, "output": ""})(),
                missing_marker,
            )
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("empty/missing marker was accepted")

    group_dir = evidence_dir / "group"
    write_guest_macho_evidence(
        group_dir,
        invocation,
        type("Result", (), {"returncode": 0, "output": "SELECT_FDSET_GUEST_OK\n"})(),
        "SELECT_FDSET_GUEST_OK",
    )
    assert finalize_guest_macho_evidence(
        group_dir, "perf", ["select_fdset_guest"]
    ) == 1
    assert "FAILED" in (group_dir / "group-result.tsv").read_text()

    perf_dir = evidence_dir / "perf"
    perf_invocation = {
        "fixture": "fork_checkin_signal_storm_guest",
        "name": "fork_checkin_signal_storm_guest_prebuilt",
    }
    write_guest_macho_evidence(
        perf_dir,
        perf_invocation,
        type("Result", (), {"returncode": 0, "output": "FORK_CHECKIN_SIGNAL_STORM_OK\n"})(),
        "FORK_CHECKIN_SIGNAL_STORM_OK",
    )
    assert finalize_guest_macho_evidence(
        perf_dir, "perf", ["fork_checkin_signal_storm_guest"]
    ) == 0
    assert "perf\tPASS\t1\tall fixtures passed" in (perf_dir / "group-result.tsv").read_text()

print("PASS guest-macho-validation-contract")

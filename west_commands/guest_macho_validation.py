"""Evidence helpers for grouped prebuilt guest Mach-O validation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

try:
    from .test_guest_macho import GuestMachoFixtureError, load_guest_macho_fixture
except ImportError:  # Loaded as a West extension module, not a package.
    from test_guest_macho import GuestMachoFixtureError, load_guest_macho_fixture
from test_selection import select_metadata_tests


VALIDATION_GROUP_FIXTURES = {
    "homebrew": frozenset(
        {
            "abort_with_payload_no_group_broadcast",
            "select_fdset_guest",
            "getattrlist_name_objtype_guest",
            "darwin_priority_guest",
            "socket_siocgifconf_guest",
            "bzero_return_register_guest",
            "sigexc_sa_restart_guest",
            "sigexc_default_resend_self_guest",
            "ulock_eintr_retry_guest",
            "vchroot_pathnull_guard_guest",
            "chown_disabled_null_guard_guest",
            "fd_guard_ebadf_guest",
            "rootless_no_mount_guest",
        }
    ),
    "perf": frozenset({"fork_checkin_signal_storm_guest"}),
}
FIXTURE_RESULTS_HEADER = "fixture\tname\trc\tmarker\texpected-marker"
GROUP_RESULT_HEADER = "group\tstatus\tfixture-count\tdetail"


def add_cli_arguments(parser) -> None:
    """Register the grouped validation options owned by this domain."""

    parser.add_argument(
        "--guest-macho-validation-group",
        choices=("homebrew", "perf"),
        help="select one exact manual guest Mach-O validation group",
    )
    parser.add_argument(
        "--guest-macho-evidence-dir",
        metavar="DIR",
        help="write per-fixture evidence for --guest-macho-validation-group",
    )


def validate_cli_selection(args) -> None:
    group = args.guest_macho_validation_group
    if group and not args.profile:
        raise ValueError(
            "--guest-macho-validation-group requires --profile homebrew"
        )
    if group and args.profile != "homebrew":
        raise ValueError(
            "--guest-macho-validation-group requires --profile homebrew"
        )
    if group and args.env != "darling":
        raise ValueError("--guest-macho-validation-group requires --env darling")
    if args.guest_macho_evidence_dir and not group:
        raise ValueError(
            "--guest-macho-evidence-dir requires --guest-macho-validation-group"
        )
    if group and args.patch:
        raise ValueError(
            "--guest-macho-validation-group selects the exact group; do not combine it with --patch"
        )
    if group and any(
        getattr(args, option, None)
        for option in ("bead", "diag", "label", "red_only")
    ):
        raise ValueError(
            "--guest-macho-validation-group selects the exact group; do not combine it with filters"
        )


def validate_selected_group(selected: list[tuple[dict, dict]], group: str) -> None:
    if not selected:
        raise ValueError(f"guest Mach-O validation group has no registrations: {group}")
    fixtures = [test.get("fixture") for _, test in selected]
    if len(fixtures) != len(set(fixtures)):
        raise ValueError("guest Mach-O validation group contains duplicate fixtures")
    if set(fixtures) != VALIDATION_GROUP_FIXTURES.get(group, frozenset()):
        raise ValueError(f"guest Mach-O validation group has the wrong fixture set: {group}")
    if any(test.get("runner") != "guest-macho-fixture" for _, test in selected):
        raise ValueError("guest Mach-O validation group selected a non-Mach-O test")
    if any(test.get("source-profile") != group for _, test in selected):
        raise ValueError(
            "guest Mach-O validation group selected an incompatible source-profile"
        )


def capture_invocation(command, invocation, env, evidence_dir: Path) -> int:
    expected_marker = expected_marker_from_corpus(invocation)
    result = command._run_invocation_captured(invocation, env=env)
    print(result.output, end="")
    write_guest_macho_evidence(evidence_dir, invocation, result, expected_marker)
    return result.returncode


def expected_marker_from_corpus(invocation: dict) -> str:
    try:
        fixture = load_guest_macho_fixture(
            Path(invocation["repo_root"]), invocation["corpus"], invocation["fixture"]
        )
    except (GuestMachoFixtureError, KeyError) as error:
        raise ValueError(f"trusted corpus marker is unavailable: {error}") from error
    if len(fixture.expected_stdout) != 1:
        raise ValueError("trusted corpus fixture must declare exactly one expected marker")
    marker = fixture.expected_stdout[0]
    if not marker or marker != marker.strip() or len(marker.splitlines()) != 1:
        raise ValueError("trusted corpus marker must be one non-empty line")
    return marker


def select_metadata_tests_for_command(
    command,
    profile: str,
    patch_path: str | None,
    bead: str | None,
    env: str | None,
    diag: str | None,
    label: str | None,
    red_only: bool,
    validation_group: str | None = None,
):
    selection = select_metadata_tests(
        command._load_profile(profile),
        patch_path=patch_path,
        bead=bead,
        env=env,
        diag=diag,
        label=label,
        validation_group=validation_group,
        red_only=red_only,
        resolved_diag=command._resolved_diag,
    )
    if patch_path and not selection.found_patch:
        command.die(f"{profile}: patch not found or has no selected tests: {patch_path}")
    return selection.selected, selection.missing


def write_guest_macho_evidence(
    evidence_dir: Path, invocation: dict, result, expected_marker: str
) -> None:
    """Persist one fixture's output and verdict in the group evidence directory."""

    evidence_dir.mkdir(parents=True, exist_ok=True)
    fixture = invocation["fixture"]
    if not expected_marker or expected_marker != expected_marker.strip() or len(expected_marker.splitlines()) != 1:
        raise ValueError("expected marker must be one non-empty line")
    results_path = evidence_dir / "fixture-results.tsv"
    if results_path.exists():
        rows = results_path.read_text().splitlines()[1:]
        if any(row.split("\t", 1)[0] == fixture for row in rows if row):
            raise ValueError(f"guest Mach-O fixture ran more than once: {fixture}")
        if not results_path.read_text().splitlines()[:1] == [FIXTURE_RESULTS_HEADER]:
            raise ValueError("fixture-results.tsv has an invalid header")
    (evidence_dir / f"{fixture}.log").write_text(str(result.output))
    observed = (
        "OBSERVED"
        if result.returncode == 0 and expected_marker in str(result.output).splitlines()
        else "NOT_OBSERVED"
    )
    with results_path.open("a") as output:
        if results_path.stat().st_size == 0:
            output.write(f"{FIXTURE_RESULTS_HEADER}\n")
        output.write(
            f"{fixture}\t{invocation['name']}\t{result.returncode}\t"
            f"{observed}\t{expected_marker}\n"
        )


def _write_group_result(evidence_dir: Path, group: str, status: str, count: int, detail: str) -> None:
    detail = detail.replace("\t", " ").replace("\n", " ")
    fd, temporary = tempfile.mkstemp(prefix=".group-result.", suffix=".tmp", dir=evidence_dir)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(f"{GROUP_RESULT_HEADER}\n{group}\t{status}\t{count}\t{detail}\n")
        os.replace(temporary, evidence_dir / "group-result.tsv")
    finally:
        Path(temporary).unlink(missing_ok=True)


def finalize_guest_macho_evidence(
    evidence_dir: Path, group: str, expected_fixtures: list[str]
) -> int:
    """Reject incomplete group evidence and publish PASS only after full validation."""

    try:
        if len(expected_fixtures) != len(set(expected_fixtures)):
            raise ValueError("selected fixtures contain duplicates")
        if set(expected_fixtures) != VALIDATION_GROUP_FIXTURES[group]:
            raise ValueError("selected fixtures do not match the validation group allowlist")
        results_path = evidence_dir / "fixture-results.tsv"
        if not results_path.is_file() or results_path.is_symlink():
            raise ValueError("fixture-results.tsv is missing")
        lines = results_path.read_text().splitlines()
        if not lines or lines[0] != FIXTURE_RESULTS_HEADER:
            raise ValueError("fixture-results.tsv has an invalid header")
        rows = []
        for line in lines[1:]:
            fields = line.split("\t")
            if len(fields) != 5 or not all(fields):
                raise ValueError("fixture-results.tsv contains a malformed row")
            rows.append(fields)
        fixtures = [row[0] for row in rows]
        if len(fixtures) != len(set(fixtures)):
            raise ValueError("fixture-results.tsv contains a duplicate fixture")
        if set(fixtures) != VALIDATION_GROUP_FIXTURES[group]:
            raise ValueError("fixture-results.tsv has missing or extra fixtures")
        for fixture, name, rc, marker, expected_marker in rows:
            if name != f"{fixture}_prebuilt":
                raise ValueError(f"unexpected test name for fixture: {fixture}")
            if rc != "0" or marker != "OBSERVED" or not expected_marker.strip():
                raise ValueError(f"fixture did not pass marker acceptance: {fixture}")
            log = evidence_dir / f"{fixture}.log"
            if not log.is_file() or log.is_symlink():
                raise ValueError(f"fixture log is missing: {fixture}")
        _write_group_result(evidence_dir, group, "PASS", len(rows), "all fixtures passed")
        return 0
    except (KeyError, OSError, ValueError) as error:
        _write_group_result(evidence_dir, group, "FAILED", 0, str(error))
        return 1

"""Parse and exercise guarded metadata display commands as shell arguments."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "run-west-test-metadata-contract.sh"
PROBE = "--metadata-display-contract-probe"


def _shell_words(line: str) -> list[str] | None:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _guarded_payload(words: list[str]) -> list[str] | None:
    for index, word in enumerate(words[:-1]):
        if Path(word).name not in {"darling-debug-runner", "<darling-debug-runner>"}:
            continue
        if words[index + 1] != "run":
            continue
        try:
            separator = words.index("--", index + 2)
        except ValueError:
            return None
        return words[separator + 1 :]
    return None


def guarded_ctest_selection(display: str) -> tuple[str, str] | None:
    """Return (build-dir, selection-args) from a guarded ctest listing line.

    A metadata test backed by a CTest selection is listed as the debug runner
    wrapping the resolved ctest command. The command may carry the declared
    label (``-L``) or the exact index the label resolved to (``-I``), because
    pinning the resolved test is what keeps a later catalogue change from
    silently running a different one. Callers compare the two selections
    instead of pinning one spelling of the command.
    """
    for line in display.splitlines():
        words = _shell_words(line)
        if words is None:
            continue
        payload = _guarded_payload(words)
        if not payload or payload[0] != "ctest":
            continue
        build = None
        selection: list[str] = []
        index = 1
        while index < len(payload):
            word = payload[index]
            if word == "--test-dir" and index + 1 < len(payload):
                build = payload[index + 1]
                index += 2
                continue
            if word in {"-L", "-I", "-R"} and index + 1 < len(payload):
                selection.extend([word, payload[index + 1]])
                index += 2
                continue
            index += 1
        if build is None or not selection:
            return None
        return build, " ".join(selection)
    return None


def matches(display: str, mode: str, label: str | None = None) -> bool:
    for line in display.splitlines():
        words = _shell_words(line)
        if words is None:
            continue
        payload = _guarded_payload(words)
        if mode == "guarded" and payload:
            return True
        if mode == "guarded-ctest" and payload and payload[0] == "ctest":
            if label is None:
                return False
            if any(
                option == "-L" and index + 1 < len(payload) and payload[index + 1] == label
                for index, option in enumerate(payload)
            ):
                return True
    return False


def accepts(mode: str, display: str, label: str | None = None) -> bool:
    command = ["bash", str(SCRIPT), PROBE, mode]
    if label is not None:
        command.append(label)
    result = subprocess.run(
        command,
        input=f"{display}\n",
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0


def contract() -> None:
    quoted_runner = (
        "<guarded> '<darling-debug-runner>' run --name test "
        "--bundle-root <temp>/bundle --timeout-seconds 120 --cwd <workspace> "
        "-- python contract.py"
    )
    resolved_runner = (
        "<guarded> /opt/darling/bin/darling-debug-runner run --name test "
        "--bundle-root <temp>/bundle --timeout-seconds 120 -- python contract.py"
    )
    spaced_resolved_runner = (
        "<guarded> '/opt/darling tools/darling-debug-runner' run --name test "
        "--bundle-root '<temp>/bundle root' --timeout-seconds 120 -- python contract.py"
    )
    bare_runner = "<bare> python contract.py"
    wrapped_bare_runner = (
        "<bare> '<darling-debug-runner>' run --name test -- python contract.py"
    )

    assert accepts("guarded", quoted_runner)
    assert accepts("guarded", resolved_runner)
    assert accepts("guarded", spaced_resolved_runner)
    assert accepts("bare", bare_runner)
    assert not accepts("bare", wrapped_bare_runner)
    assert not accepts(
        "guarded", "<guarded> <darling-debug-runner> run -- python contract.py"
    )
    assert not accepts(
        "guarded", "<guarded> darling-debug-runner' run -- python contract.py"
    )
    assert not accepts("guarded", "<guarded> '<darling-debug-runner>' run --")

    label = "bead:dar-gwn.5"
    ctest = (
        "<ctest-label> '<darling-debug-runner>' run --name west_ctest_label_contract "
        "--bundle-root <temp>/bundle --timeout-seconds 120 --cwd <workspace> "
        "-- ctest --test-dir <temp>/build --output-on-failure -L bead:dar-gwn.5"
    )
    spaced_ctest = ctest.replace(
        "'<darling-debug-runner>'", "'/opt/darling tools/darling-debug-runner'"
    )
    assert accepts("guarded-ctest", ctest, label)
    assert accepts("guarded-ctest", spaced_ctest, label)
    assert accepts(
        "guarded-ctest",
        ctest.replace("bead:dar-gwn.5", "'suite[1]'"),
        "suite[1]",
    )
    assert not accepts(
        "guarded-ctest",
        ctest.replace("bead:dar-gwn.5", "bead:dar-gwn.50"),
        label,
    )
    assert not accepts("guarded-ctest", ctest.replace(" -- ctest", " ctest"), label)
    assert not accepts("guarded-ctest", ctest.replace(" -- ctest", " -- python"), label)
    assert not accepts(
        "guarded-ctest", ctest.replace(" -L bead:dar-gwn.5", ""), label
    )
    print("PASS metadata-display-contract")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--match", choices=("guarded", "guarded-ctest"))
    parser.add_argument("--label")
    parser.add_argument(
        "--print-ctest-selection",
        action="store_true",
        help="print '<build-dir>\\t<selection-args>' for a guarded ctest listing",
    )
    args = parser.parse_args()
    if args.print_ctest_selection:
        found = guarded_ctest_selection(sys.stdin.read())
        if found is None:
            print("no guarded ctest selection in the listing", file=sys.stderr)
            return 1
        print(f"{found[0]}\t{found[1]}")
        return 0
    if args.match:
        return 0 if matches(sys.stdin.read(), args.match, args.label) else 1
    contract()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

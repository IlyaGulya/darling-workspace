#!/usr/bin/env python3
"""Read-only census of which canonical invocation selects each flat workload.

The census uses West's production workload loader and selector. It reads CI
tier commands, host sweeps and documented operator commands, then attributes
each distinct stable workload id without constructing source profiles or a
runtime. Unselected declarations fail with their environment and selectors.
``--verify-lists N`` optionally compares safe discovery counts with West.
"""

from __future__ import annotations

import argparse
import ast
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))
from test_manifest import ManifestError, load_workloads
from test_selection import metadata_test_labels, select_workload_tests

TIER_SOURCE = "ci/run-test-tier.sh"
SWEEP_SOURCE = "ci/run-host-tier.py"
DOC_SOURCE = "docs/test-infra.md"

# ``west test`` options that consume the following token. A flag this census
# does not list stays flag-only, which at worst leaves its value as a positional
# token the census ignores; a selection pin is never one of those.
VALUE_FLAGS = frozenset(
    {
        "--diagnostic",
        "--prefix",
        "--bootstrap-executable",
        "--bootstrap-syscall-trace",
        "--bootstrap-stack-sample",
        "--bootstrap-timeout-seconds",
        "--ctest-timeout-seconds",
        "--guest-macho-validation-group",
        "--guest-macho-evidence-dir",
        "--bundle-root",
        "--keep-last",
        "--max-bundle-mb",
        "--proof-scratch-root",
        "--proof-scratch-max-age-hours",
        "--proof-scratch-keep-last",
        "--runtime-evidence",
        "--runtime-evidence-id",
        "--runtime-evidence-root",
        "--output-junit",
        "--executor",
        "-j",
        "--jobs",
        "--runtime-root",
        "--workload",
        "--bead",
        "--env",
        "--diag",
        "--label",
        "--submodule",
    }
)

# Selectors that pin what an invocation can select. An invocation naming none of
# them selects the default suite, which proves nothing about one declaration.
PIN_FLAGS = (
    "--workload",
    "--bead",
    "--env",
    "--diag",
    "--label",
    "--submodule",
    "--guest-macho-validation-group",
)

# Placeholders and unexpanded shell variables. A pin this census cannot resolve
# is reported instead of guessed at.
UNRESOLVED = re.compile(r"[<>]|\$\{|\$[A-Za-z_@]|\.\.\.|^\s*$")

# Prefix lifecycle operations: they provision, clean or collect, and West
# rejects them in combination with a metadata selection, so they select nothing.
LIFECYCLE_FLAGS = (
    "--cleanup-prefix",
    "--gc",
    "--gc-runtime-evidence",
)

# ``"${@:2}"`` and friends: selectors the operator supplies at the call site.
PASSTHROUGH = re.compile(r"\$\{@|\$\*|^\$@$")

# One flat workload row from the structured runner's listing.
LISTING_ROW = re.compile(r"^\S+: \S+ \[")


@dataclass(frozen=True)
class Invocation:
    """One command that selects declared tests, with the selectors it names."""

    source: str
    argv: tuple[str, ...]
    workloads: tuple[str, ...] = ()
    bead: str | None = None
    env: str | None = None
    diag: str | None = None
    label_pattern: str | None = None
    submodules: tuple[str, ...] = ()
    group: str | None = None
    fuzz: bool = False
    stress: bool = False
    lifecycle: bool = False

    @property
    def command(self) -> str:
        return f"west test {' '.join(self.argv)}".strip()

    @property
    def label(self) -> str:
        return f"{self.source}: {self.command}"

    @property
    def pinned(self) -> bool:
        """Whether an invocation names a meaningful canonical selection axis."""
        return bool(
            self.workloads
            or self.bead
            or self.env
            or self.diag
            or self.label_pattern
            or self.submodules
            or self.group
            or self.fuzz
            or self.stress
        )

    def pins(self) -> list[str]:
        named = []
        for flag, value in (
            ("--bead", self.bead),
            ("--env", self.env),
            ("--diag", self.diag),
            ("--label", self.label_pattern),
            ("--guest-macho-validation-group", self.group),
        ):
            if value:
                named.append(f"{flag} {value}")
        named.extend(f"--submodule {value}" for value in self.submodules)
        named.extend(f"--workload {value}" for value in self.workloads)
        if self.fuzz:
            named.append("--fuzz")
        if self.stress:
            named.append("--stress")
        return named

    def metadata_selection(self, catalog: dict) -> list[tuple[dict, dict]]:
        """Use the same source-independent selector as ``west test``."""
        selection = select_workload_tests(
            catalog,
            workload=self.workloads or None,
            bead=self.bead,
            env=self.env,
            diag=self.diag,
            label=self.label_pattern,
            validation_group=self.group,
            resolved_diag=resolved_diag,
            defer_ctest=False,
        )
        selected = []
        for owner, test in selection.selected:
            labels = metadata_test_labels(owner, test, resolved_diag)
            if self.fuzz and "fuzz:true" not in labels:
                continue
            if self.stress and "stress:true" not in labels:
                continue
            if self.submodules and not any(
                f"submod:{value}" in labels or f"submod:{value.rsplit('/', 1)[-1]}" in labels
                for value in self.submodules
            ):
                continue
            selected.append((owner, test))
        return selected


@dataclass(frozen=True)
class Declaration:
    """One declared test, identified by where it is declared."""

    workload: str
    name: str
    env: str
    blocked: bool
    identity: int

    @property
    def shown(self) -> str:
        return f"{self.workload}: {self.name} [env:{self.env}]"


def resolved_diag(test: dict) -> str:
    """Mirror ``west test``'s diagnostic default for label filtering."""
    return test.get("diag") or ("guarded" if test.get("env") == "darling" else "bare")


def shell_assignments(text: str) -> dict[str, str]:
    """Collect literal ``name=value`` assignments so ``$name`` pins resolve."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match is None:
            continue
        value = match.group(2).strip().strip("\"'")
        if not value or " " in value or "$" in value:
            continue
        values[match.group(1)] = value
    return values


def expand_shell(token: str, values: dict[str, str]) -> str:
    """Expand ``$name``/``${name}`` from literal assignments, leaving the rest."""
    return re.sub(
        r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
        lambda match: values.get(match.group(1), match.group(0)),
        token,
    )


def command_tail(text: str) -> str:
    """Return the ``west test`` command in a line, without trailing shell.

    Quoting matters: a pattern like ``--label 'name:(a|b)'`` carries the very
    characters that separate shell commands, so the split has to respect quotes.
    """
    match = re.search(r"\bwest test\b", text)
    if match is None:
        return ""
    tail = text[match.end():]
    quote = ""
    for position, character in enumerate(tail):
        if quote:
            quote = "" if character == quote else quote
            continue
        if character in "'\"":
            quote = character
            continue
        if character in "|;&)":
            return tail[:position].strip()
    return re.split(r"\s#", tail, maxsplit=1)[0].strip()


def tier_commands(workspace: Path) -> list[str]:
    """Return the ``west test`` commands of the tier phases in ci/run-test-tier.sh."""
    path = workspace / TIER_SOURCE
    if not path.is_file():
        return []
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    values = shell_assignments(text)
    return [
        expand_shell(command_tail(line), values)
        for line in text.splitlines()
        if "west test" in line
    ]


def sweep_commands(workspace: Path) -> list[str]:
    """Return the ``west test`` argv lists ci/run-host-tier.py launches."""
    path = workspace / SWEEP_SOURCE
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return []
    commands = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "HostCommand":
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.List):
            continue
        argv = []
        for element in node.args[1].elts:
            if isinstance(element, ast.Starred):
                continue
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                argv.append(element.value)
        if argv[:2] == ["west", "test"]:
            commands.append(" ".join(argv[2:]))
    return commands


def documented_commands(workspace: Path) -> list[str]:
    """Return the ``west test`` commands docs/test-infra.md gives operators."""
    path = workspace / DOC_SOURCE
    if not path.is_file():
        return []
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    candidates = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL):
        candidates.extend(line for line in block.splitlines() if "west test" in line)
    candidates.extend(span for span in re.findall(r"`([^`\n]+)`", text) if "west test" in span)
    return [command_tail(candidate) for candidate in candidates]


def parse_invocation(source: str, command: str) -> tuple[Invocation | None, str | None]:
    """Parse one command into an invocation, or state why it cannot be used."""
    try:
        tokens = shlex.split(command)
    except ValueError as error:
        return None, f"{source}: west test {command}: {error}"
    flags: dict[str, list[str]] = {}
    named: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if PASSTHROUGH.search(token):
            index += 1
            continue
        if not token.startswith("-"):
            index += 1
            continue
        name, separator, inline = token.partition("=")
        named.add(name)
        if name not in VALUE_FLAGS:
            index += 1
            continue
        if separator:
            value = inline
            index += 1
        elif index + 1 < len(tokens):
            value = tokens[index + 1]
            index += 2
        else:
            return None, f"{source}: west test {command}: {name} has no value"
        flags.setdefault(name, []).append(value)
    for flag in PIN_FLAGS:
        for value in flags.get(flag, []):
            if UNRESOLVED.search(value):
                return None, f"{source}: west test {command}: {flag} {value} is not a concrete selector"
    if "--changed" in named:
        return (
            None,
            f"{source}: west test {command}: --changed selects whatever this checkout diverged in, "
            "which a static census cannot enumerate",
        )

    def single(flag: str) -> str | None:
        return (flags.get(flag) or [None])[0]

    invocation = Invocation(
        source=source,
        argv=tuple(tokens),
        workloads=tuple(flags.get("--workload") or ()),
        bead=single("--bead"),
        env=single("--env"),
        diag=single("--diag"),
        label_pattern=single("--label"),
        submodules=tuple(flags.get("--submodule") or ()),
        group=single("--guest-macho-validation-group"),
        fuzz="--fuzz" in named,
        stress="--stress" in named,
        lifecycle=bool(named & set(LIFECYCLE_FLAGS)),
    )
    return invocation, None


def enumerate_invocations(
    workspace: Path,
) -> tuple[list[Invocation], list[str], list[str], list[str]]:
    """Return pinned invocations, blanket sweeps, lifecycle commands and skips."""
    candidates = [(TIER_SOURCE, command) for command in tier_commands(workspace)]
    candidates += [(SWEEP_SOURCE, command) for command in sweep_commands(workspace)]
    candidates += [(DOC_SOURCE, command) for command in documented_commands(workspace)]
    invocations: list[Invocation] = []
    unpinned: list[str] = []
    lifecycle: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for source, command in candidates:
        invocation, reason = parse_invocation(source, command)
        if reason is not None:
            unresolved.append(reason)
            continue
        if invocation is None:
            continue
        if invocation.command in seen:
            continue
        seen.add(invocation.command)
        if invocation.pinned:
            invocations.append(invocation)
        elif invocation.lifecycle:
            lifecycle.append(invocation.label)
        else:
            unpinned.append(invocation.label)
    return invocations, unpinned, lifecycle, unresolved


def load_declarations(workspace: Path) -> tuple[dict, list[Declaration]]:
    """Load every stable workload id through West's own catalog loader."""
    manifest = workspace / "testkit" / "workloads.yml"
    try:
        catalog = load_workloads(manifest)
    except (ManifestError, OSError) as error:
        raise SystemExit(f"selection census: {manifest}: {error}") from error
    declarations = [
        Declaration(
            workload=test["id"],
            name=test["name"],
            env=str(test.get("env") or "ctest"),
            blocked=bool(test.get("blocked")),
            identity=id(test),
        )
        for test in catalog["workloads"]
    ]
    return catalog, declarations


def collapse(invocations: list[Invocation]) -> list[Invocation]:
    """Drop invocations whose selectors are identical to an earlier one."""
    unique: list[Invocation] = []
    seen: set[tuple] = set()
    for invocation in invocations:
        key = (
            invocation.workloads,
            invocation.bead,
            invocation.env,
            invocation.diag,
            invocation.label_pattern,
            invocation.submodules,
            invocation.group,
            invocation.fuzz,
            invocation.stress,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(invocation)
    return unique


def invocation_selection(
    invocations: list[Invocation], catalog: dict
) -> tuple[dict[int, list[Invocation]], list[tuple[Invocation, int]]]:
    """Map every declaration identity to the invocations that select it."""
    selected: dict[int, list[Invocation]] = {}
    counts: list[tuple[Invocation, int]] = []
    for invocation in collapse(invocations):
        try:
            found = {id(test) for _owner, test in invocation.metadata_selection(catalog)}
        except ValueError as error:
            raise SystemExit(f"selection census: {invocation.label}: {error}") from error
        for identity in found:
            selected.setdefault(identity, []).append(invocation)
        counts.append((invocation, len(found)))
    return selected, counts


def unselected_reason(declaration: Declaration, invocations: list[Invocation]) -> str:
    """State, as cheaply as this census can, why nothing selects a declaration."""
    if declaration.blocked:
        return "the declaration is marked blocked in metadata"
    compatible = [
        invocation for invocation in invocations
        if invocation.env in (None, declaration.env) or declaration.env == "ctest"
    ]
    if not compatible:
        return f"no invocation names this environment: env:{declaration.env}"
    return (
        f"every invocation for env:{declaration.env} is narrower; "
        f"the first pins {', '.join(compatible[0].pins())}"
    )


def listing_argv(invocation: Invocation) -> list[str] | None:
    """Return the argv for a safe ``--list`` re-run, or None when it is not safe.

    A discovery run must not touch a prefix or resolve a shell variable this
    census only saw as text.
    """
    unsafe = ("--prefix", "--prove-red")
    for token in invocation.argv:
        if "$" in token or token.startswith(unsafe):
            return None
    return list(invocation.argv)


def verify_lists(
    counts: list[tuple[Invocation, int]], limit: int, workspace: Path
) -> list[str]:
    """Compare this census's per-invocation counts with West's own listing."""
    if not limit:
        return []
    problems: list[str] = []
    checked = 0
    for invocation, count in counts:
        if checked == limit:
            break
        argv = listing_argv(invocation)
        if argv is None:
            continue
        try:
            completed = subprocess.run(
                ["west", "test", *argv, "--list"],
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"{invocation.label}: west test --list did not finish in 600s")
            checked += 1
            continue
        checked += 1
        if completed.returncode != 0:
            tail = completed.stdout.strip().splitlines()
            problems.append(
                f"{invocation.label}: west test --list failed with {completed.returncode}: "
                f"{tail[-1] if tail else 'no output'}"
            )
            continue
        observed = sum(1 for line in completed.stdout.splitlines() if LISTING_ROW.match(line))
        if observed != count:
            problems.append(
                f"{invocation.label}: census counted {count} declared test(s), "
                f"west test --list reported {observed}"
            )
    skipped = limit - checked
    note = f"; {skipped} requested listing(s) are unsafe to re-run" if skipped > 0 else ""
    print(f"verified {checked} invocation listing(s) against west{note}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=ROOT,
        help="workspace holding testkit/, ci/ and docs/ (default: this checkout)",
    )
    parser.add_argument(
        "--verify-lists",
        type=int,
        default=0,
        metavar="N",
        help="run at most N 'west test --list' discovery commands and compare their counts",
    )
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not (workspace / "testkit" / "workloads.yml").is_file():
        raise SystemExit(f"selection census: no testkit/workloads.yml under {workspace}")
    if not 0 <= args.verify_lists <= 8:
        raise SystemExit("selection census: --verify-lists accepts 0 to 8 commands")

    invocations, unpinned, lifecycle, unresolved = enumerate_invocations(workspace)
    catalog, declarations = load_declarations(workspace)
    selected, counts = invocation_selection(invocations, catalog)

    print(
        f"selection census: {len(declarations)} declared workload(s), "
        f"{len(counts)} pinned invocation(s)"
    )
    print(f"selected by an invocation: {sum(1 for value in selected.values() if value)}")
    for invocation, count in counts:
        print(f"  {count:4d}  {invocation.label}")
    for label in unpinned:
        print(f"  unpinned, selects the default suite: {label}")
    for label in lifecycle:
        print(f"  no selection, prefix lifecycle operation: {label}")
    for reason in unresolved:
        print(f"  unresolved: {reason}")
    print("declared test attribution:")
    missing = []
    for declaration in declarations:
        selectors = selected.get(declaration.identity, [])
        if not selectors:
            missing.append(declaration)
        attribution = "; ".join(invocation.label for invocation in selectors)
        print(f"  {declaration.shown} <- {attribution or 'NOTHING SELECTS THIS'}")

    if not missing:
        print("unselected: 0")
    else:
        print(f"unselected: {len(missing)}")
        by_env: dict[str, int] = {}
        for declaration in missing:
            by_env[declaration.env] = by_env.get(declaration.env, 0) + 1
        summary = ", ".join(
            f"{env} {count}" for env, count in sorted(by_env.items())
        )
        print(f"  by environment: {summary}")
        for declaration in missing:
            print(f"  {declaration.shown}: {unselected_reason(declaration, invocations)}")

    problems = verify_lists(counts, args.verify_lists, workspace)
    for problem in problems:
        print(f"selection census verification failed: {problem}")
    if missing:
        print(
            f"selection census failed: {len(missing)} declared test(s) are selected by no "
            "invocation, so nothing would run them"
        )
    if missing or problems:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

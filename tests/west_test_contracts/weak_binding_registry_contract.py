#!/usr/bin/env python3
"""Fail-closed census of definitions that can satisfy a guest binding while doing nothing.

The defect class this contract guards is ADR-0004
(docs/adr/0004-one-implementation-per-public-concurrency-symbol.md): an exported
weak definition, or an exported placeholder body, still satisfies the guest's
dynamic binding.  The guest then binds a definition that does nothing while a
real implementation elsewhere is never reached.  It was measured on 2026-10-10:
libsystem_kernel exported `os_unfair_lock_lock`/`os_unfair_lock_unlock` as empty
weak functions, so every guest `os_unfair_lock` was a no-op and exclusion failed
2798 of 2800 rounds.  A real definition elsewhere does not win the binding.

This module owns the scan grammar; the reviewed classification lives in
lifecycle/weak-binding-registry-v1.json and scripts/generate_weak_binding_registry.py
derives the digests and counts inside it.  The contract fails, naming the file
and the entry, when:

  * a discovered candidate has no reviewed classification (an addition nobody
    reviewed),
  * a classified entry is no longer discovered (a silent disappearance),
  * a retired definition is discovered again (the removed stub came back),
  * an anchored source digest moved (the classification was reviewed against
    bytes that are gone), or
  * the registry's own grammar, vocabulary, counts or digest disagree with what
    this contract recomputes.

Candidate grammar (the two arms of the ADR-0004 shape):

  weak arm
      a file-scope, non-static definition carrying ``__attribute__((weak))``.
      Visibility is deliberately NOT considered: the measured instance carried
      no visibility attribute at all and still won a guest binding, so the
      conservative reading is to record every weak definition rather than to
      assume one is unreachable.
  placeholder arm
      a file-scope, non-static definition that is explicitly default-visible
      (``VISIBLE``, which is the macro for
      ``__attribute__((visibility("default")))``, or the attribute spelled out)
      and whose whole body is empty, ``return 0;``, ``return -ENOSYS;`` or
      ``return NULL;``.  Explicit default visibility is required here because
      the emulation libraries compile with ``-fvisibility=hidden``: an unmarked
      placeholder cannot reach the export table, and admitting unmarked
      examples would put every unexported helper in the forest in the census.

`fault_mode` is the measured character of a discovered body, separate from the
reviewed `body` field: `void` (empty), `silent` (a bare placeholder return),
`loud` (diagnostics only, with an optional placeholder return) or `none` (a real
body).  It is what makes "one loud, three silent" in the ADR evidence a checked
value rather than prose.  A loud body is not itself a candidate: it reports what
it did not do, so it is not the "does nothing" shape the placeholder arm admits.
It is recorded here because `kqueue_impl` has that body AND weak linkage, which
is the arm that discovers it.

Usage:

  python3 -B tests/west_test_contracts/weak_binding_registry_contract.py
      validate the checked-in registry against the Darling source forest
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
DARLING = ROOT.parent / "darling"
REGISTRY_PATH = ROOT / "lifecycle" / "weak-binding-registry-v1.json"

SCHEMA_VERSION = 1
KIND = "weak-or-placeholder-binding-candidate-registry"
COVERAGE_TIER = "source"

SOURCE_FOREST = {"repository": "darling", "root": "src"}

# The scan grammar.  The registry carries a reviewed copy of every constant
# below; a disagreement is a drift the reviewer must resolve, never a value this
# contract may quietly adopt from the file.
TRANSLATION_UNIT_EXTENSIONS = [".c", ".cc", ".cpp", ".cxx", ".m", ".mm"]
PRUNED_PATH_COMPONENTS = ["."]
WEAK_LINKAGE_MARKER = "__attribute__((weak))"
DEFAULT_VISIBILITY_MARKERS = ["VISIBLE", '__attribute__((visibility("default")))']
PLACEHOLDER_BODY_LITERALS = ["return 0;", "return -ENOSYS;", "return NULL;"]
PLACEHOLDER_BODY_GRAMMAR = ["<empty>", *PLACEHOLDER_BODY_LITERALS]
DIAGNOSTIC_CALLS = [
    "__simple_printf",
    "__simple_kprintf",
    "__simple_abort",
    "printf",
    "fprintf",
    "abort",
    "__builtin_unreachable",
    "__builtin_trap",
]
# How image_role decides "imported".  The attribute is spelled through a
# target-dependent macro in this forest (the emulation's WEAK_IMPORT is
# weak_import for the Darwin targets and plain weak on the ELF side), so the
# macro is derived from the forest instead of being pinned here.
WEAK_IMPORT_GRAMMAR = (
    "A name counts as imported when the anchored source declares it with "
    '__attribute__((weak_import)) or with a macro the forest defines as exactly that attribute '
    "(the emulation's WEAK_IMPORT, weak_import on the Darwin targets and plain weak on the ELF side)."
)

STATUSES = ["classified", "retired"]
ORIGINS = ["darling", "upstream-apple", "third-party"]
KINDS = ["public-api", "internal-hook", "fixture"]
# "real-supplied-elsewhere" is the shape ADR-0004 names: the definition is a
# link-order shim and the recorded strong_side supplies the implementation that
# the binding actually reaches.  "real" is a weak definition that is itself an
# implementation, weak only as an override point.
BODIES = ["silent", "loud", "real-supplied-elsewhere", "real"]
FAULT_MODES = ["void", "silent", "loud", "none"]
# Whether the guest image DEFINES the name or merely IMPORTS it.  The kqueue fix
# flips exactly that: a weak definition that a guest could bind becomes a
# weak_import declaration the caller tests for NULL, and the name is then
# supplied by another image (or by nobody, if the import was dropped).
IMAGE_ROLES = ["defined", "imported", "absent"]

DISCOVERY_ARMS = ["weak-linkage", "placeholder-body"]

ENTRY_FIELDS = [
    "name",
    "repository",
    "path",
    "sha256",
    "status",
    "origin",
    "kind",
    "body",
    "discovery",
    "fault_mode",
    "image_role",
    "strong_side",
    "caller_dependency",
    "retirement",
]
STRONG_SIDE_FIELDS = ["repository", "path", "symbol", "sha256"]
RETIREMENT_FIELDS = [
    "repository",
    "commit",
    "branch",
    "date",
    "removed_declaration",
    "note",
]
DOCUMENT_FIELDS = [
    "schema_version",
    "kind",
    "coverage_tier",
    "rule",
    "source_forest",
    "scan",
    "vocabulary",
    "counting",
    "counts",
    "entries_digest",
    "entries",
]
SCAN_FIELDS = [
    "translation_unit_extensions",
    "pruned_path_components",
    "weak_linkage_marker",
    "default_visibility_markers",
    "placeholder_body_grammar",
    "placeholder_body_literals",
    "diagnostic_calls",
    "weak_import_grammar",
    "candidate_rule",
]
VOCABULARY_FIELDS = [
    "status",
    "origin",
    "kind",
    "body",
    "fault_mode",
    "image_role",
    "discovery",
]
COUNT_FIELDS = ["candidate_count", "classified_count", "retired_count"]

_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

_WEAK_ATTRIBUTE = re.compile(r"__attribute__\s*\(\(\s*weak\s*\)\)")
_DEFAULT_VISIBILITY = re.compile(r'\bVISIBLE\b|visibility\s*\(\s*"default"\s*\)')
# The bounded prefilter: a candidate's declaration necessarily carries one of
# the grammar's anchors, so a translation unit without one cannot hold a
# candidate.  It runs on the raw bytes before any decoding, which is what keeps
# the whole-forest scan affordable.
_MARKER_PREFILTER = re.compile(
    rb'__attribute__\s*\(\(\s*weak|VISIBLE|visibility\s*\(\s*"default"'
)
_PLACEHOLDER_BODY = re.compile(
    r"\A\s*(?:return\s+(?:0|-ENOSYS|NULL)\s*;)?\s*\Z"
)

# C constructs that can look like a definition to a brace scan but are not one.
_RESERVED = frozenset(
    {"if", "while", "for", "switch", "else", "do", "return", "sizeof", "case", "__attribute__"}
)
# C++ exception specifications trail the parameter list and are not the body.
_TRAILING_SPECIFIERS = ("throw", "noexcept")


class BindingRegistryError(AssertionError):
    """The registry disagrees with the source forest, or it is malformed."""


# --------------------------------------------------------------- repository


def _repository_root(repository: str) -> Path:
    if repository == "darling-workspace":
        return ROOT
    if repository == "darling":
        return DARLING
    raise BindingRegistryError(f"unknown repository in registry entry: {repository!r}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_path(repository: str, relative: str) -> Path:
    return _repository_root(repository) / relative


# ------------------------------------------------------------ source grammar


def _strip_source(text: str, *, keep_strings: bool) -> str:
    """Return the source with comments blanked, optionally blanking string bodies.

    Both renderings keep every index aligned, so a definition found in the
    structural rendering can be inspected in the literal one (the visibility
    attribute spells a string, ``visibility("default")``).
    """

    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == "/" and index + 1 < length and text[index + 1] == "/":
            end = text.find("\n", index)
            end = length if end < 0 else end
            out.append(" " * (end - index))
            index = end
        elif character == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            out.append("".join("\n" if char == "\n" else " " for char in text[index:end]))
            index = end
        elif character == '"':
            end = index + 1
            while end < length:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == '"':
                    end += 1
                    break
                end += 1
            out.append(text[index:end] if keep_strings else '"' + " " * (end - index - 2) + '"')
            index = end
        elif character == "'":
            end = index + 1
            while end < length:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == "'":
                    end += 1
                    break
                end += 1
            out.append(text[index:end] if keep_strings else " " * (end - index))
            index = end
        else:
            out.append(character)
            index += 1
    return "".join(out)


def _matching_open_paren(text: str, close: int) -> int | None:
    depth = 0
    index = close
    while index >= 0:
        if text[index] == ")":
            depth += 1
        elif text[index] == "(":
            depth -= 1
            if depth == 0:
                return index
        index -= 1
    return None


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    index = opening
    length = len(text)
    while index < length:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return length


def _identifier_before(text: str, position: int) -> tuple[str, int]:
    """Return the identifier ending just before ``position`` and where it starts."""

    index = position - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    end = index + 1
    while index >= 0 and (text[index].isalnum() or text[index] == "_"):
        index -= 1
    return text[index + 1 : end], index + 1


def _signature(text: str, close: int) -> tuple[str, int] | None:
    """Return the declared name and its start for the signature ending at ``close``.

    A C++ exception specification (``throw()``, ``throw(std::bad_alloc)``,
    ``noexcept``) sits between the parameter list and the body, so the first
    parenthesis group before the body is not always the parameter list.
    """

    while True:
        opening = _matching_open_paren(text, close)
        if opening is None:
            return None
        name, start = _identifier_before(text, opening)
        if name in _TRAILING_SPECIFIERS:
            previous = text.rfind(")", 0, opening)
            if previous < 0:
                return None
            close = previous
            continue
        return name, start


def _preceding_character(text: str, position: int) -> str:
    index = position - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    return text[index] if index >= 0 else ""


def _preceding_word(text: str, position: int) -> str:
    index = position - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    end = index + 1
    while index >= 0 and (text[index].isalnum() or text[index] == "_"):
        index -= 1
    return text[index + 1 : end]


def _declaration_start(text: str, before: int) -> int:
    """Return the start of the declaration whose name begins at ``before``.

    The scan walks back to the end of the previous file-scope statement, so a
    constructor initializer list or a class body cannot be mistaken for a
    declaration prefix.
    """

    depth = 0
    index = before
    while index >= 0:
        character = text[index]
        if character in ")]":
            depth += 1
        elif character in "([":
            depth -= 1
        elif depth == 0 and character in ";}{":
            return index + 1
        elif depth == 0 and character == "\n" and text[index + 1 : index + 2] == "#":
            return index + 1
        index -= 1
    return 0


def _definition_sites(structural: str, literal: str) -> list[dict[str, str]]:
    """Return every file-scope function definition in a translation unit."""

    sites: list[dict[str, str]] = []
    length = len(structural)
    index = 0
    while True:
        close = structural.find(")", index)
        if close < 0:
            return sites
        opening = close + 1
        while opening < length and structural[opening] in " \t\r\n":
            opening += 1
        if opening >= length or structural[opening] != "{":
            index = close + 1
            continue
        signature = _signature(structural, close)
        if signature is None:
            index = close + 1
            continue
        name, name_start = signature
        preceding = _preceding_character(structural, name_start)
        if not name or name[0].isdigit() or name in _RESERVED or preceding in ",:.=(":
            index = opening + 1
            continue
        if _preceding_word(structural, name_start) == "operator":
            name = f"operator {name}"
        start = _declaration_start(structural, name_start)
        declaration = literal[start:name_start]
        # A declaration that begins with a preprocessor line is not one: the
        # backward scan stops at the newline before the directive, so the
        # directive itself is still in front of it.  Dropping it keeps the
        # visibility and weak checks reading a declaration and nothing else.
        if declaration.lstrip().startswith("#"):
            newline = declaration.find("\n")
            declaration = declaration[newline + 1 :] if newline >= 0 else ""
        body_end = _matching_brace(structural, opening)
        sites.append(
            {
                "name": name,
                "declaration": declaration,
                "body": structural[opening + 1 : body_end],
            }
        )
        index = body_end + 1


def _fault_mode(body: str) -> str:
    """Return the measured character of a definition body."""

    text = " ".join(body.split())
    if not text:
        return "void"
    if _PLACEHOLDER_BODY.match(text):
        return "silent"
    remainder = text
    for call in DIAGNOSTIC_CALLS:
        remainder = re.sub(rf"\b{re.escape(call)}\s*\([^;]*\)\s*;", " ", remainder)
    remainder = " ".join(remainder.split())
    if not remainder or _PLACEHOLDER_BODY.match(remainder):
        return "loud"
    return "none"


def _placeholder_body(body: str) -> bool:
    return bool(_PLACEHOLDER_BODY.match(body))


_WEAK_IMPORT_ATTRIBUTE = re.compile(r"__attribute__\s*\(\(\s*weak_import\s*\)\)")
# The forest spells the weak-import attribute through a target-dependent macro
# (base.h defines WEAK_IMPORT as weak_import for the Darwin targets and as weak
# on the ELF side), so the macro is derived from the forest rather than pinned.
_WEAK_IMPORT_DEFINE = re.compile(
    r"(?m)^[ \t]*#[ \t]*define[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+"
    r"__attribute__\s*\(\(\s*weak_import\s*\)\)"
)
# The same pattern over raw bytes: a translation unit that merely *uses* a weak
# import must not be decoded, and only a file that defines one can name a macro.
_WEAK_IMPORT_DEFINE_BYTES = re.compile(
    rb"(?m)^[ \t]*#[ \t]*define[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]+"
    rb"__attribute__\s*\(\(\s*weak_import\s*\)\)"
)


def weak_import_names(source: str, macros: Iterable[str] = ()) -> set[str]:
    """Return the names a translation unit declares as weak imports.

    A weak import is the post-ADR-0004 shape: the image does not define the
    name, the caller tests it for NULL, and another image supplies it.  A
    retired entry must say which of those it is, so the registry records what
    the removal left behind rather than only that something is gone.
    """

    structural = _strip_source(source, keep_strings=False)
    patterns = [r"__attribute__\s*\(\(\s*weak_import\s*\)\)"]
    patterns.extend(rf"\b{re.escape(macro)}\b" for macro in sorted(macros))
    marker = re.compile("|".join(patterns))
    names: set[str] = set()
    for match in marker.finditer(structural):
        end = structural.find(";", match.start())
        statement = structural[match.start() : len(structural) if end < 0 else end]
        names.update(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", statement))
    return names


def discovery_arms(weak: bool, placeholder: bool) -> str:
    """Return the canonical discovery label for a candidate."""

    arms = []
    if placeholder:
        arms.append("placeholder-body")
    if weak:
        arms.append("weak-linkage")
    return "+".join(arms)


# The macro scan must also see headers, because that is where a weak-import
# macro is defined; discovery itself only reads translation units.
MACRO_SCAN_EXTENSIONS = sorted(
    set(TRANSLATION_UNIT_EXTENSIONS) | {".h", ".hpp", ".hh", ".hxx"}
)


def _marker_prefilter(data: bytes) -> bool:
    """Whether a translation unit can contain the grammar's anchors at all."""

    return bool(_MARKER_PREFILTER.search(data))


def forest_facts(root: Path | None = None) -> dict[str, Any]:
    """Return the forest's candidates and weak-import macros in one pass.

    The scan is memoized per process: the classification and the drift checks
    both need it, and reading the whole forest twice would double the contract's
    cost for the same answer.
    """

    key = str(root if root is not None else "")
    cached = _FOREST_CACHE.get(key)
    if cached is not None:
        return cached
    facts = _scan_forest(root)
    _FOREST_CACHE[key] = facts
    return facts


def discover(root: Path | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """Return every candidate in the Darling source forest, keyed by (path, name)."""

    return forest_facts(root)["candidates"]


def weak_import_macros(root: Path | None = None) -> set[str]:
    """Return the macros the forest defines as exactly the weak_import attribute."""

    return forest_facts(root)["weak_import_macros"]


_FOREST_CACHE: dict[str, dict[str, Any]] = {}


def _scan_forest(root: Path | None) -> dict[str, Any]:
    forest = (
        root
        if root is not None
        else _repository_root(SOURCE_FOREST["repository"]) / SOURCE_FOREST["root"]
    )
    if not forest.is_dir():
        raise BindingRegistryError(
            f"the reviewed source forest is missing: {forest}; the registry cannot be checked "
            "against a tree that is not there"
        )
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    macros: set[str] = set()
    repository_root = _repository_root(SOURCE_FOREST["repository"])
    for directory, subdirectories, files in os.walk(forest):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if not any(name.startswith(pruned) for pruned in PRUNED_PATH_COMPONENTS)
        )
        for filename in sorted(files):
            suffix = os.path.splitext(filename)[1]
            if suffix not in MACRO_SCAN_EXTENSIONS:
                continue
            path = Path(directory) / filename
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"weak_import" in data and _WEAK_IMPORT_DEFINE_BYTES.search(data):
                macros.update(
                    _WEAK_IMPORT_DEFINE.findall(
                        _strip_source(data.decode("utf-8", errors="replace"), keep_strings=False)
                    )
                )
            if suffix not in TRANSLATION_UNIT_EXTENSIONS:
                continue
            if not _marker_prefilter(data):
                continue
            raw = data.decode("utf-8", errors="replace")
            structural = _strip_source(raw, keep_strings=False)
            literal = _strip_source(raw, keep_strings=True)
            for site in _definition_sites(structural, literal):
                declaration = site["declaration"]
                if re.search(r"\bstatic\b", declaration):
                    continue
                weak = bool(_WEAK_ATTRIBUTE.search(declaration))
                placeholder = _placeholder_body(site["body"])
                if not weak and not (_DEFAULT_VISIBILITY.search(declaration) and placeholder):
                    continue
                key = (path.relative_to(repository_root).as_posix(), site["name"])
                candidates[key] = {
                    "discovery": discovery_arms(weak, placeholder),
                    "fault_mode": _fault_mode(site["body"]),
                }
    return {"candidates": candidates, "weak_import_macros": macros}


# ----------------------------------------------------------------- registry


def load_registry(path: Path | None = None) -> dict[str, Any]:
    target = path if path is not None else REGISTRY_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BindingRegistryError(f"{target}: cannot read registry: {error}") from error
    if not isinstance(payload, dict):
        raise BindingRegistryError(f"{target}: registry is not a JSON object")
    return payload


def entries_of(registry: dict[str, Any]) -> list[Any]:
    entries = registry.get("entries")
    if not isinstance(entries, list):
        raise BindingRegistryError("registry has no entries list")
    return entries


def counts_of(registry: dict[str, Any]) -> dict[str, int]:
    return {
        "candidate_count": registry.get("counts", {}).get("candidate_count"),
        "classified_count": registry.get("counts", {}).get("classified_count"),
        "retired_count": registry.get("counts", {}).get("retired_count"),
    }


def entries_digest(entries: list[dict[str, Any]]) -> str:
    """Return the digest over the registry's entries, the one the generator binds."""

    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_label(entry: dict[str, Any]) -> str:
    return f"{entry.get('path', '<no path>')}:{entry.get('name', '<no name>')}"


def _expected_derived(
    entry: dict[str, Any], discovered: dict[tuple[str, str], dict[str, Any]]
) -> tuple[Any, Any]:
    """Return the (discovery, fault_mode) this entry's source state derives."""

    if entry.get("status") == "classified":
        found = discovered.get((entry.get("path"), entry.get("name")))
        if found is None:
            return None, None
        return found["discovery"], found["fault_mode"]
    return None, None


# ------------------------------------------------------------- classification


def classification_problems(registry: dict[str, Any]) -> list[str]:
    """Return the problems a regeneration of the digests cannot fix.

    These are the ones that force a human decision: an unreviewed candidate, a
    classification that no longer exists in the tree, a retired definition that
    came back, or a registry whose own shape drifted from the reviewed layout.
    """

    problems: list[str] = []
    if list(registry) != DOCUMENT_FIELDS:
        problems.append(
            f"registry: fields {list(registry)} differ from the reviewed layout {DOCUMENT_FIELDS}"
        )
    for field, expected in (
        ("schema_version", SCHEMA_VERSION),
        ("kind", KIND),
        ("coverage_tier", COVERAGE_TIER),
    ):
        if registry.get(field) != expected:
            problems.append(f"registry.{field}: {registry.get(field)!r} is not {expected!r}")
    if registry.get("source_forest") != SOURCE_FOREST:
        problems.append(
            f"registry.source_forest: {registry.get('source_forest')!r} is not the reviewed "
            f"forest {SOURCE_FOREST!r}"
        )
    scan = registry.get("scan")
    expected_scan = {
        "translation_unit_extensions": TRANSLATION_UNIT_EXTENSIONS,
        "pruned_path_components": PRUNED_PATH_COMPONENTS,
        "weak_linkage_marker": WEAK_LINKAGE_MARKER,
        "default_visibility_markers": DEFAULT_VISIBILITY_MARKERS,
        "placeholder_body_grammar": PLACEHOLDER_BODY_GRAMMAR,
        "placeholder_body_literals": PLACEHOLDER_BODY_LITERALS,
        "diagnostic_calls": DIAGNOSTIC_CALLS,
        "weak_import_grammar": WEAK_IMPORT_GRAMMAR,
    }
    if not isinstance(scan, dict) or list(scan) != SCAN_FIELDS:
        problems.append(f"registry.scan: fields {list(scan) if isinstance(scan, dict) else scan} differ from {SCAN_FIELDS}")
    else:
        for field, expected in expected_scan.items():
            if scan.get(field) != expected:
                problems.append(
                    f"registry.scan.{field}: the registry states {scan.get(field)!r} but this "
                    f"contract scans with {expected!r}; review the grammar change"
                )
    vocabulary = registry.get("vocabulary")
    expected_vocabulary = {
        "status": STATUSES,
        "origin": ORIGINS,
        "kind": KINDS,
        "body": BODIES,
        "fault_mode": FAULT_MODES,
        "image_role": IMAGE_ROLES,
        "discovery": DISCOVERY_ARMS,
    }
    if not isinstance(vocabulary, dict) or list(vocabulary) != VOCABULARY_FIELDS:
        problems.append(
            f"registry.vocabulary: fields "
            f"{list(vocabulary) if isinstance(vocabulary, dict) else vocabulary} differ from "
            f"{VOCABULARY_FIELDS}"
        )
    else:
        for field, expected in expected_vocabulary.items():
            if sorted(vocabulary.get(field, [])) != sorted(expected):
                problems.append(
                    f"registry.vocabulary.{field}: {vocabulary.get(field)!r} is not {expected!r}"
                )
    counting = registry.get("counting")
    if not isinstance(counting, dict) or list(counting) != COUNT_FIELDS:
        problems.append(f"registry.counting: fields {counting} differ from {COUNT_FIELDS}")

    discovered = discover()
    entries = entries_of(registry)
    seen: dict[tuple[Any, Any], str] = {}
    classified_keys: set[tuple[str, str]] = set()
    retired_keys: set[tuple[str, str]] = set()
    previous: tuple[str, str] | None = None
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"registry.entries[{index}]: entry is not a JSON object")
            continue
        label = _entry_label(entry)
        if list(entry) != ENTRY_FIELDS:
            problems.append(
                f"{label}: fields {list(entry)} differ from the reviewed layout {ENTRY_FIELDS}"
            )
            continue
        key = (entry["path"], entry["name"])
        if key in seen:
            problems.append(f"{label}: duplicate entry, already declared by {seen[key]}")
        seen[key] = label
        if previous is not None and key < previous:
            problems.append(f"{label}: entries are not sorted by (path, name); {label} follows {previous}")
        previous = key
        if entry["repository"] != SOURCE_FOREST["repository"]:
            problems.append(
                f"{label}: repository {entry['repository']!r} is outside the reviewed forest"
            )
        if entry["status"] not in STATUSES:
            problems.append(f"{label}: status {entry['status']!r} is not one of {STATUSES}")
        if entry["origin"] not in ORIGINS:
            problems.append(f"{label}: origin {entry['origin']!r} is not one of {ORIGINS}")
        if entry["kind"] not in KINDS:
            problems.append(f"{label}: kind {entry['kind']!r} is not one of {KINDS}")
        if entry["body"] not in BODIES:
            problems.append(f"{label}: body {entry['body']!r} is not one of {BODIES}")
        if entry["image_role"] not in IMAGE_ROLES:
            problems.append(f"{label}: image_role {entry['image_role']!r} is not one of {IMAGE_ROLES}")
        if not isinstance(entry["caller_dependency"], str) or not entry["caller_dependency"].strip():
            problems.append(f"{label}: caller_dependency must state which caller depends on the name")
        try:
            source = _relative_path(entry["repository"], entry["path"])
        except BindingRegistryError as error:
            problems.append(f"{label}: {error}")
            continue
        if not source.is_file() or source.is_symlink():
            problems.append(f"{label}: the anchored source is not a regular file: {source}")
            continue
        found = discovered.get(key)
        if entry["status"] == "classified":
            classified_keys.add(key)
            if found is None:
                problems.append(
                    f"{label}: the reviewed classification is no longer discovered in the tree; a "
                    "classified entry may not silently disappear"
                )
        elif entry["status"] == "retired":
            retired_keys.add(key)
            declaration = (entry.get("retirement") or {}).get("removed_declaration", "")
            source_text = source.read_text(encoding="utf-8", errors="replace")
            if found is not None:
                problems.append(
                    f"{label}: this definition was reviewed as RETIRED but the tree defines it "
                    "again; the ADR-0004 stub came back"
                )
            if declaration and " ".join(declaration.split()) in " ".join(source_text.split()):
                problems.append(f"{label}: the retired declaration is present again in {entry['path']}")
            imports = weak_import_names(source_text, weak_import_macros())
            if entry["image_role"] == "defined":
                problems.append(
                    f"{label}: a retired entry cannot be image_role defined: retirement means the "
                    "image no longer defines the name"
                )
            elif entry["image_role"] == "imported" and entry["name"] not in imports:
                problems.append(
                    f"{label}: image_role is imported but {entry['path']} declares no weak import of "
                    f"{entry['name']!r}; the removal left a shape this registry does not describe"
                )
            elif entry["image_role"] == "absent" and entry["name"] in imports:
                problems.append(
                    f"{label}: image_role is absent but {entry['path']} still weak-imports "
                    f"{entry['name']!r}"
                )
        strong_side = entry["strong_side"]
        if strong_side is None:
            if entry["body"] == "real-supplied-elsewhere":
                problems.append(
                    f"{label}: body real-supplied-elsewhere requires the strong side's location"
                )
        else:
            if list(strong_side) != STRONG_SIDE_FIELDS:
                problems.append(
                    f"{label}: strong_side fields {list(strong_side)} differ from {STRONG_SIDE_FIELDS}"
                )
            else:
                if entry["body"] == "silent":
                    problems.append(
                        f"{label}: body silent contradicts a recorded strong side; a placeholder with "
                        "a reachable strong side is real-supplied-elsewhere"
                    )
                try:
                    strong_source: Path | None = _relative_path(
                        strong_side["repository"], strong_side["path"]
                    )
                except BindingRegistryError as error:
                    problems.append(f"{label}: {error}")
                    strong_source = None
                if strong_source is not None and (
                    not strong_source.is_file() or strong_source.is_symlink()
                ):
                    problems.append(
                        f"{label}: the anchored strong side is not a regular file: {strong_source}"
                    )
                elif strong_source is not None:
                    text = strong_source.read_text(encoding="utf-8", errors="replace")
                    if strong_side["symbol"] not in text:
                        problems.append(
                            f"{label}: {strong_side['path']} does not name {strong_side['symbol']!r}"
                        )
        retirement = entry["retirement"]
        if entry["status"] == "retired":
            if not isinstance(retirement, dict) or list(retirement) != RETIREMENT_FIELDS:
                problems.append(
                    f"{label}: a retired entry must carry {RETIREMENT_FIELDS} and nothing else"
                )
            else:
                if not _COMMIT.match(str(retirement["commit"])):
                    problems.append(f"{label}: retirement.commit {retirement['commit']!r} is not a full commit id")
                if not _DATE.match(str(retirement["date"])):
                    problems.append(f"{label}: retirement.date {retirement['date']!r} is not a date")
                for field in ("repository", "branch", "note"):
                    if not str(retirement[field]).strip():
                        problems.append(f"{label}: retirement.{field} is empty")
        elif retirement is not None:
            problems.append(f"{label}: only a retired entry may carry a retirement record")

    for key in sorted(set(discovered) - classified_keys - retired_keys):
        problems.append(
            f"{key[0]}:{key[1]}: candidate discovered in the tree with no reviewed classification; "
            "classify it before this registry is accepted"
        )
    return problems


def drift_problems(registry: dict[str, Any]) -> list[str]:
    """Return the derived-value disagreements the generator splices.

    The contract still fails on them: an anchored digest that moved is a
    classification reviewed against bytes that are gone, and it must be seen
    before the generator rewrites the binding.
    """

    problems: list[str] = []
    discovered = discover()
    entries = entries_of(registry)
    for entry in entries:
        if not isinstance(entry, dict) or list(entry) != ENTRY_FIELDS:
            continue
        label = _entry_label(entry)
        try:
            source: Path | None = _relative_path(entry["repository"], entry["path"])
        except BindingRegistryError:
            source = None
        if source is not None and source.is_file() and not source.is_symlink():
            recorded = entry["sha256"]
            current = _sha256_file(source)
            if recorded != current:
                problems.append(
                    f"{label}: anchored source digest moved: registry={recorded} tree={current}"
                )
        strong_side = entry["strong_side"]
        if isinstance(strong_side, dict) and list(strong_side) == STRONG_SIDE_FIELDS:
            try:
                strong_source: Path | None = _relative_path(
                    strong_side["repository"], strong_side["path"]
                )
            except BindingRegistryError:
                strong_source = None
            if strong_source is not None and strong_source.is_file() and not strong_source.is_symlink():
                current = _sha256_file(strong_source)
                if strong_side["sha256"] != current:
                    problems.append(
                        f"{label}: anchored strong-side digest moved: "
                        f"registry={strong_side['sha256']} tree={current}"
                    )
        discovery, fault_mode = _expected_derived(entry, discovered)
        if entry["discovery"] != discovery:
            problems.append(
                f"{label}: discovery is {entry['discovery']!r} but the tree derives {discovery!r}"
            )
        if entry["fault_mode"] != fault_mode:
            problems.append(
                f"{label}: fault_mode is {entry['fault_mode']!r} but the tree derives {fault_mode!r}"
            )
        # A discovered candidate is a definition by construction, so the only
        # role the tree can derive is "defined".  A retired entry records what
        # the removal left behind, which is reviewed data and checked above.
        expected_role = "defined" if entry["status"] == "classified" else entry["image_role"]
        if entry["image_role"] != expected_role:
            problems.append(
                f"{label}: image_role is {entry['image_role']!r} but the tree derives "
                f"{expected_role!r}"
            )
    counts = counts_of(registry)
    derived_counts = {
        "candidate_count": len(discovered),
        "classified_count": sum(
            1 for entry in entries if isinstance(entry, dict) and entry.get("status") == "classified"
        ),
        "retired_count": sum(
            1 for entry in entries if isinstance(entry, dict) and entry.get("status") == "retired"
        ),
    }
    for field in COUNT_FIELDS:
        if counts.get(field) != derived_counts[field]:
            problems.append(
                f"registry.counts.{field}: registry={counts.get(field)!r} tree={derived_counts[field]!r}"
            )
    digest = entries_digest(entries)
    if registry.get("entries_digest") != digest:
        problems.append(
            f"registry.entries_digest: registry={registry.get('entries_digest')!r} derived={digest!r}"
        )
    return problems


def audit(registry: dict[str, Any]) -> list[str]:
    """Return every classification and drift problem, each naming its entry."""

    return classification_problems(registry) + drift_problems(registry)


def validate(registry: dict[str, Any] | None = None) -> None:
    payload = registry if registry is not None else load_registry()
    problems = audit(payload)
    if problems:
        raise BindingRegistryError(
            "the weak-binding registry disagrees with the tree:\n  " + "\n  ".join(problems)
        )


def main() -> None:
    registry = load_registry()
    validate(registry)
    print("weak-binding registry contract: PASS")


if __name__ == "__main__":
    main()

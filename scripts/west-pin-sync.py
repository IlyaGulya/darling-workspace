#!/usr/bin/env python3
"""Pin sync -- make west.yml's revisions match the darling superproject's gitlinks.

The bookkeeping tax of this workspace's shape is that one commit lands in three places: the payload
or component commit inside a submodule, the gitlink bump in the darling superproject, and the
`revision:` in this manifest.  The first two are recorded by git; the third is edited by hand, and
forgetting it is silent until a clean `west update` produces a different tree.

`check` reads the gitlinks and prints the before/after table without writing; `sync` writes only the
`revision:` lines that disagree, and refuses to lower a pin (a west revision that is not an ancestor
of the gitlink) unless `--allow-downgrade` says so out loud, because a lowered pin silently
un-supplies a payload that the workspace is already running.

Reads the gitlinks with `git ls-tree` of the superproject, the manifest as text (comments and
formatting are preserved -- only the revision value of a named project is ever replaced), and never
writes anything but the manifest.

Usage:
  west-pin-sync.py check [--manifest west.yml] [--darling DIR] [--ref REF | --from-index]
                         [--project NAME]... [--allow-downgrade]
  west-pin-sync.py sync  [same] [--dry-run]
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys

NAME_RE = re.compile(r"^(\s*)-\s*name:\s*(\S+)\s*$")
SECTION_RE = re.compile(r"^(\s*)([a-z][a-z0-9-]*):\s*$")
PATH_RE = re.compile(r"^(\s*)path:\s*(\S+)\s*$")
REV_RE = re.compile(r"^(\s*)revision:\s*(\S+)(.*)$")
PROJECTS_PREFIX = "darling/"


def run(args, cwd=None):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def parse_projects(text):
    """[{name, path, revision, name_line, path_line, rev_line}] from the manifest TEXT.

    Text, not YAML: the manifest is hand-maintained with comments that a round trip through a YAML
    emitter would delete, and this tool's contract is to touch nothing but a revision value.  Only
    the `projects:` section is read, so the identically shaped `remotes:` entries are not mistaken
    for projects.
    """
    lines = text.splitlines()
    projects = []
    in_projects = False
    section_indent = 0
    current = None
    dash_indent = 0
    for index, line in enumerate(lines):
        section = SECTION_RE.match(line)
        if section:
            indent = len(section.group(1))
            if section.group(2) == "projects":
                in_projects, section_indent, current, dash_indent = True, indent, None, 0
                continue
            if in_projects and indent <= section_indent:
                in_projects, current = False, None
                continue
        if not in_projects:
            continue
        name = NAME_RE.match(line)
        if name:
            indent = len(name.group(1))
            if current is None or indent <= dash_indent:
                current = {"name": name.group(2), "path": None, "revision": None,
                           "name_line": index, "path_line": None, "rev_line": None}
                projects.append(current)
                dash_indent = indent
            continue
        if current is None:
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= dash_indent:
            current = None
            continue
        path = PATH_RE.match(line)
        if path and current["path"] is None:
            current["path"] = path.group(2)
            current["path_line"] = index
            continue
        rev = REV_RE.match(line)
        if rev and current["revision"] is None:
            current["revision"] = rev.group(2)
            current["rev_line"] = index
    return projects


def gitlinks(darling, ref=None, from_index=False):
    """relpath -> gitlink sha for every submodule the superproject records."""
    if from_index:
        code, out, err = run(["git", "-C", darling, "ls-files", "-s"])
        if code != 0:
            raise SystemExit(f"west-pin-sync.py: git ls-files failed in {darling}: {err.strip()}")
        links = {}
        for line in out.splitlines():
            fields = line.split("\t")
            if len(fields) != 2:
                continue
            meta = fields[0].split()
            if len(meta) == 3 and meta[0] == "160000":
                links[fields[1]] = meta[1]
        return links
    code, out, err = run(["git", "-C", darling, "ls-tree", "-r", ref or "HEAD"])
    if code != 0:
        raise SystemExit(f"west-pin-sync.py: git ls-tree failed in {darling}: {err.strip()}")
    links = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == "commit":
            links[path] = fields[2]
    return links


def relationship(darling, rel, west_rev, gitlink):
    """"forward", "west-ahead", "same" or "unknown" between a manifest pin and a gitlink.

    Determined from the submodule's own repository when it is checked out; when it is not, the
    relationship is unknown and reported as such rather than guessed.
    """
    if west_rev == gitlink:
        return "same"
    repo = os.path.join(darling, rel)
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        return "unknown"
    code, _, _ = run(["git", "-C", repo, "merge-base", "--is-ancestor", west_rev, gitlink])
    if code == 0:
        return "forward"
    code, _, _ = run(["git", "-C", repo, "merge-base", "--is-ancestor", gitlink, west_rev])
    if code == 0:
        return "west-ahead"
    return "unknown"


def resolve_selection(projects, only):
    """(matched, unmatched): the --project names/paths that name a manifest project, and those that do not.

    A filter that matches NOTHING must never read as success.  Measured 2026-10-12: a mistyped name
    (`darling-src-external-IOKitUser` for `darling-src-external-iokituser`) selected no row, printed an
    empty `summary:` and `RESULT: no change -- every writable revision already matches its gitlink`,
    exit 0, while west.yml kept the pre-rung revision and the workspace doctor then failed the next
    bootstrap with UNDECLARED drift.  Same failure shape as a reader that reports success about a set
    it never looked at.
    """
    known = {p["name"] for p in projects}
    known |= {p["path"] for p in projects if p["path"]}
    matched, unmatched = [], []
    for want in only or ():
        (matched if want in known else unmatched).append(want)
    return matched, unmatched


def suggest(want, known):
    """The near-match to name in an unmatched-selection error, or None.

    Two passes: difflib on the raw spelling, then on a case/separator-folded spelling, which is what
    catches the measured case (`...-IOKitUser` vs `...-iokituser`) that a case-sensitive compare misses.
    """
    close = difflib.get_close_matches(want, sorted(known), n=1, cutoff=0.5)
    if close:
        return close[0]
    fold = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    folded = {}
    for name in sorted(known):
        folded.setdefault(fold(name), name)
    close = difflib.get_close_matches(fold(want), sorted(folded), n=1, cutoff=0.6)
    return folded[close[0]] if close else None


def selection_text(selection, total):
    """"all N project(s)" or "M of N project(s): a, b, c" for the report and the RESULT lines."""
    if not selection:
        return f"all {total} project(s)"
    return (f"{len(selection)} of {total} project(s), by --project: " + ", ".join(selection))


def allowlist_entries(root):
    """(path, [name-or-path]) of the doctor's declared-drift allowlist: comments and blanks skipped."""
    path = os.path.join(root, "doctor-allow-drift.txt")
    try:
        with open(path, encoding="utf-8") as stream:
            entries = []
            for line in stream:
                entry = line.split("#", 1)[0].strip()
                if entry:
                    entries.append(entry)
    except OSError:
        return path, []
    return path, entries


def plan(projects, links, darling, allow_downgrade, only=None):
    """[(project, rel, west_rev, gitlink, relationship, action)] for every manifest project."""
    rows = []
    for project in projects:
        path = project["path"] or ""
        if only and project["name"] not in only and path not in only:
            continue
        if path == "darling" or not path.startswith(PROJECTS_PREFIX):
            rows.append((project, path, project["revision"], None, "not-a-child", "leave"))
            continue
        rel = path[len(PROJECTS_PREFIX):]
        gitlink = links.get(rel)
        if gitlink is None:
            rows.append((project, rel, project["revision"], None, "no-gitlink", "leave"))
            continue
        if project["revision"] is None or project["rev_line"] is None:
            rows.append((project, rel, None, gitlink, "no-revision", "leave"))
            continue
        rel_kind = relationship(darling, rel, project["revision"], gitlink)
        if project["revision"] == gitlink:
            action = "none"
        elif rel_kind == "west-ahead" and not allow_downgrade:
            action = "refuse-downgrade"
        else:
            action = "rewrite"
        rows.append((project, rel, project["revision"], gitlink, rel_kind, action))
    return rows


def print_table(rows, manifest_path, links_source, selection=None, total=None):
    print(f"pin sync -- west.yml revisions against the gitlinks of the darling superproject")
    print(f"  manifest      : {manifest_path}")
    print(f"  gitlinks from : {links_source}")
    if total is not None:
        # WHICH projects this run actually examined: a selection that matched nothing must never be
        # able to print "no change" about the tree.
        print(f"  examined      : {selection_text(selection, total)}")
    print()
    header = f"{'PROJECT':<44} {'WEST.YML':<12} {'GITLINK':<12} {'RELATION':<13} ACTION"
    print(header)
    print("-" * len(header))
    for project, rel, west_rev, gitlink, rel_kind, action in rows:
        short = (west_rev or "-")[:10]
        print(f"{project['name']:<44} {short:<12} {(gitlink or '-')[:10]:<12} "
              f"{rel_kind:<13} {action}")
    counts = {}
    for _p, _r, _w, _g, _k, action in rows:
        counts[action] = counts.get(action, 0) + 1
    print()
    print("summary: " + ", ".join(f"{count} {name}" for name, count in sorted(counts.items())))
    return counts


def apply_rows(text, rows):
    """Replace only the revision value of the rows whose action is 'rewrite'; return (text, changed)."""
    lines = text.splitlines(keepends=True)
    changed = []
    for project, rel, west_rev, gitlink, rel_kind, action in rows:
        if action != "rewrite":
            continue
        index = project["rev_line"]
        line = lines[index]
        m = REV_RE.match(line)
        if not m:
            raise SystemExit(f"west-pin-sync.py: the revision line moved under project "
                             f"{project['name']}; nothing written")
        ending = line[len(line.rstrip("\r\n")):]
        lines[index] = f"{m.group(1)}revision: {gitlink}{m.group(3)}{ending}"
        changed.append((project["name"], west_rev, gitlink))
    if not changed:
        return text, changed
    return "".join(lines), changed


def verify_only_revisions_changed(before, after, rows):
    """A rewrite must not touch anything but the revision lines it planned to touch."""
    planned = {project["rev_line"] for project, _r, _w, _g, _k, action in rows
               if action == "rewrite"}
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if len(before_lines) != len(after_lines):
        raise SystemExit("west-pin-sync.py: the rewrite changed the number of lines; not written")
    for index, (a, b) in enumerate(zip(before_lines, after_lines)):
        if a != b and index not in planned:
            raise SystemExit(f"west-pin-sync.py: the rewrite would touch line {index + 1}, which is "
                             f"not a planned revision line; not written")
    return True


def default_darling(manifest_path):
    """The superproject beside the manifest: <manifest dir>/../darling in a West workspace.

    Falling back to a search upward for the workspace marker (`.west/`) makes the default work when
    the manifest is named by --manifest from a copy; a manifest used from anywhere else needs
    --darling, and the header prints which directory was resolved.
    """
    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    for base in (os.path.dirname(manifest_dir), manifest_dir):
        candidate = os.path.join(base, "darling")
        if os.path.exists(os.path.join(candidate, ".git")):
            return candidate
    directory = manifest_dir
    for _ in range(6):
        if os.path.isdir(os.path.join(directory, ".west")):
            return os.path.join(directory, "darling")
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return os.path.join(os.path.dirname(manifest_dir), "darling")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("command", choices=("check", "sync"),
                    help="check: report only, exit 1 when out of sync; sync: rewrite the revisions")
    ap.add_argument("--manifest", default=None,
                    help="west.yml to read (and, for sync, rewrite); default: ./west.yml")
    ap.add_argument("--darling", default=None,
                    help="the darling superproject to read gitlinks from; default: ./darling")
    ap.add_argument("--ref", default=None,
                    help="the superproject revision whose tree to read (default: HEAD)")
    ap.add_argument("--from-index", action="store_true",
                    help="read the STAGED gitlinks (git ls-files -s) instead of a committed tree")
    ap.add_argument("--project", action="append", default=None,
                    help="restrict to a project name (or west path); repeatable")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="also rewrite a pin whose revision is AHEAD of the gitlink (a downgrade)")
    ap.add_argument("--dry-run", action="store_true", help="with sync: print without writing")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest_path = args.manifest or os.path.join(root, "west.yml")
    darling = args.darling or default_darling(manifest_path)
    if not os.path.isfile(manifest_path):
        print(f"west-pin-sync.py: no manifest at {manifest_path}", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(darling, ".git")):
        print(f"west-pin-sync.py: no git superproject at {darling}", file=sys.stderr)
        return 2

    text = open(manifest_path, encoding="utf-8").read()
    projects = parse_projects(text)
    if not projects:
        print(f"west-pin-sync.py: no projects parsed out of {manifest_path}", file=sys.stderr)
        return 2
    links = gitlinks(darling, args.ref, args.from_index)
    source = (f"git ls-files -s (the STAGED gitlinks) in {darling}" if args.from_index
              else f"git ls-tree -r {args.ref or 'HEAD'} in {darling}")
    selection, unmatched = resolve_selection(projects, args.project)
    if unmatched:
        names = sorted({p["name"] for p in projects})
        for want in unmatched:
            hint = suggest(want, names)
            if len(names) <= 30:
                known = "known names: " + ", ".join(names)
            else:
                near = difflib.get_close_matches(want, names, n=5, cutoff=0.4)
                near += [suggest(want, names)] if not near and hint else []
                known = (f"{len(names)} projects in this manifest; nearest names: "
                         + ", ".join(dict.fromkeys(near)))
            print(f"west-pin-sync.py: no project named {want!r}; {known}"
                  + (f" -- did you mean {hint!r}?" if hint else ""), file=sys.stderr)
        print("west-pin-sync.py: nothing was examined, so nothing can be reported about the tree; "
              "--project also accepts a west path (e.g. darling/src/external/iokituser)",
              file=sys.stderr)
        return 2
    rows = plan(projects, links, darling, args.allow_downgrade, selection or None)
    counts = print_table(rows, manifest_path, source, selection, len(projects))
    examined = selection_text(selection, len(projects))

    refusals = [row for row in rows if row[5] == "refuse-downgrade"]
    if refusals:
        print("refused to LOWER these pins (the manifest revision is ahead of the gitlink, so the "
              "gitlink is the older tree -- the payload or component commit is not recorded in the "
              "superproject yet, or the pin is deliberately ahead); pass --allow-downgrade to do it "
              "anyway:")
        for project, rel, west_rev, gitlink, rel_kind, _action in refusals:
            print(f"  {project['name']:<44} west {west_rev[:10]} ahead of gitlink {gitlink[:10]} "
                  f"({rel})")
        print()

    if args.command == "check":
        if counts.get("rewrite"):
            print(f"RESULT: out of sync -- {counts['rewrite']} revision(s) disagree with the "
                  f"gitlinks (forward moves); run 'west-pin-sync.py sync'")
            return 1
        if counts.get("refuse-downgrade"):
            print(f"RESULT: no change to write -- {counts['refuse-downgrade']} pin(s) are AHEAD of "
                  "their gitlink and were refused; the manifest disagrees with the superproject "
                  "there, and only --allow-downgrade would act on it")
            return 0
        print(f"RESULT: in sync -- nothing to write; examined {examined}")
        return 0

    new_text, changed = apply_rows(text, rows)
    if not changed:
        print(f"RESULT: no change -- nothing to write; examined {examined}")
        return 0
    verify_only_revisions_changed(text, new_text, rows)
    for name, before, after in changed:
        print(f"  {name:<44} {before[:10]} -> {after[:10]}")
    if args.dry_run:
        print("RESULT: dry run -- nothing written")
        return 0
    with open(manifest_path, "w", encoding="utf-8") as stream:
        stream.write(new_text)
    print(f"RESULT: wrote {len(changed)} revision(s) into {manifest_path}; nothing else was touched")
    # A fixed project that is still listed in the doctor's declared-drift allowlist is hidden from the
    # next drift check: the allowlist line was for the drift that has just been fixed.
    allowlist_path, listed = allowlist_entries(root)
    fixed = {name for name, _before, _after in changed}
    stale = sorted(fixed & set(listed)) + sorted(
        {row[1] for row in rows if row[0]["name"] in fixed} & set(listed))
    if stale:
        print(f"  NOTE: {len(stale)} project(s) just fixed are still listed in {allowlist_path}: "
              + ", ".join(stale))
        print("        a stale allowlist line hides the NEXT real drift for that project -- remove it "
              "now that the pin and the gitlink agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())

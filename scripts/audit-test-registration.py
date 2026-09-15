#!/usr/bin/env python3
"""Registration/applicability audit and reviewed-applicability maintenance.

OUTPUT writes the native inventory evidence: every concrete patch test binding,
its declaration identity and the reviewed applicability it resolves to.
`report`, `carry` and `rekey` maintain audits/native-test-applicability.json.
None of them builds a product target or executes a test. Generated evidence
belongs in OUTPUT, not in source control.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import subprocess
import sys

DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1]
APPLICABILITY_RECORD = Path("audits/native-test-applicability.json")
REVIEW_COMMANDS = ("report", "carry", "rekey")
REVIEW_USAGE = (
    "applicability review commands (`report` is read-only, `carry` and `rekey` rewrite the "
    "reviewed record): report | carry --move OLD=NEW [--move ...] [--drop KEY ...] | "
    "rekey --shift OLD=NEW [--shift ...] [--drop KEY ...]"
)
REVIEW_DESCRIPTION = {
    "report": "Print every binding's declaration identity and every applicability review gap.",
    "carry": "Move reviewed applicability policy between the keys a reviewer names explicitly.",
    "rekey": "Re-record reviewed applicability under shifted ordinals of one patch entry.",
}


def identity(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def display_path(path, workspace):
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def west_project_paths(workspace):
    return dict(line.split("\t", 1) for line in subprocess.check_output(
        ["west", "list", "-f", "{name}\t{path}"], cwd=workspace, text=True).splitlines())


def declaration_identity(declaration):
    """Hash a declaration exactly as the reviewed applicability record stores it.

    The hash covers the normalized test entry and its relative West project path,
    never the key that holds the binding, so a declaration keeps its identity when
    a profile is renamed or an ordinal shifts and changes only when the
    declaration itself changes.
    """
    declaration_bytes = json.dumps(declaration, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode()
    return hashlib.sha256(declaration_bytes).hexdigest()


def declared_bindings(workspace, project_paths):
    """Normalize every concrete patches/*/patches.yml entry with its declaration identity."""
    sys.path.insert(0, str(workspace / "west_commands"))
    from test_manifest import load_test_profile
    from test_ctest import is_ctest_binding

    profiles = []
    patches = []
    bindings = []
    for manifest in sorted((workspace / "patches").glob("*/patches.yml")):
        data = load_test_profile(manifest)
        profile = manifest.parent.name
        profiles.append({"profile": profile, "manifest": str(manifest.relative_to(workspace)),
                         "sha256": identity(manifest), "base_profile": data.get("base-profile")})
        for patch in data.get("patches", []):
            owner = {"profile": profile, "patch": patch["path"], "module": patch["module"],
                     "bead": patch.get("bead"), "source_commit": patch.get("source-commit")}
            tests = patch.get("tests") or []
            patches.append({**owner, "test_count": len(tests),
                            "exception": patch.get("test-exception"),
                            "publication_status": patch.get("publication-status")})
            for ordinal, test in enumerate(tests, 1):
                repo = test.get("repo", patch["module"])
                repo_root = workspace if repo in {"darling-workspace", "manifest"} else workspace.parent / project_paths.get(repo, repo)
                script = test.get("script")
                asset = repo_root / script if script else None
                runner = test.get("runner") or ("ctest" if is_ctest_binding(test) else "command" if test.get("command") else "script" if script else "unspecified")
                bindings.append({**owner, "binding_id": f"{profile}:{patch['path']}:{ordinal}",
                                 "name": test.get("name"), "runner": runner,
                                 "environment": test.get("env", "host"),
                                 "coverage_tier": test.get("coverage-tier"),
                                 "blocked": test.get("blocked"), "red": bool(test.get("red")),
                                 "red_proof": test.get("red-proof"),
                                 "repo": repo, "script": script,
                                 "asset_path": str(asset) if asset else None,
                                 "asset_exists": asset.exists() if asset else None,
                                 "asset_sha256": identity(asset) if asset else None,
                                 "ctest_selector": test.get("ctest-label") or test.get("ctest-name"),
                                 "requirements": test.get("requires", []),
                                 "identity_sha256": declaration_identity(
                                     {"project": project_paths.get(repo, repo), "test": test}),
                                 "normalized_test": test})
    return {"profiles": profiles, "patches": patches, "bindings": bindings}


def reviewed_policy_groups(spec):
    groups = {group["policy"]: group for group in spec["groups"]}
    if len(groups) != len(spec["groups"]):
        raise ValueError("duplicate applicability policy group")
    unknown = sorted({review["policy"] for review in spec["bindings"].values()} - set(groups))
    if unknown:
        raise ValueError(
            "applicability record names undefined policy group(s): " + ", ".join(unknown))
    return groups


def review_state(bindings, spec):
    """Classify every applicability review gap for a declaration census.

    All gaps are collected instead of stopping at the first: reporting only the
    first makes a metadata edit look like a single missing review while the rest
    of the profile's gaps stay invisible, and orphans left behind by a
    re-recording would never be reported at all.
    """
    groups = reviewed_policy_groups(spec)
    recorded = spec["bindings"]
    bindings_without_policy = []
    identity_mismatches = []
    policies = {}
    for row in bindings:
        review = recorded.get(row["binding_id"])
        if review is None:
            bindings_without_policy.append(row["binding_id"])
        elif review["identity_sha256"] != row["identity_sha256"]:
            identity_mismatches.append(row["binding_id"])
        else:
            policies[row["binding_id"]] = {"policy": groups[review["policy"]]["policy"],
                                           "reason": review["reason"]}
    policy_records_without_binding = sorted(
        set(recorded) - {row["binding_id"] for row in bindings})
    problems = sorted(
        [f"unreviewed binding (no policy recorded): {key}" for key in bindings_without_policy]
        + [f"binding declaration changed since its applicability review: {key}"
           for key in identity_mismatches]
        + [f"applicability policy entry has no binding (orphaned by a rename or re-recording): {key}"
           for key in policy_records_without_binding])
    return {"policies": policies, "bindings_without_policy": bindings_without_policy,
            "identity_mismatches": identity_mismatches,
            "policy_records_without_binding": policy_records_without_binding,
            "problems": problems}


def inventory_command(argv):
    parser = argparse.ArgumentParser(description=__doc__, epilog=REVIEW_USAGE)
    parser.add_argument("output", type=Path, help="external directory for generated evidence")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    output = args.output.resolve()
    project_paths = west_project_paths(workspace)
    census = declared_bindings(workspace, project_paths)
    profiles, patches, bindings = census["profiles"], census["patches"], census["bindings"]

    output.mkdir(parents=True, exist_ok=True)
    configure = subprocess.run(
        ["cmake", "-S", str(workspace / "testkit"), "-B", str(output / "ctest-build"),
         "-DBUILD_TESTING=ON"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (output / "configure.log").write_text(configure.stdout)
    configure.check_returncode()
    discovery = subprocess.run(
        ["ctest", "--test-dir", str(output / "ctest-build"), "--show-only=json-v1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (output / "discovery.log").write_text(discovery.stderr)
    discovery.check_returncode()
    ctest_path = output / "ctest.json"
    ctest_path.write_text(discovery.stdout)
    ctest = json.loads(discovery.stdout)
    ctest_cases = []
    if ctest:
        for test in ctest.get("tests", []):
            properties = {p["name"]: p["value"] for p in test.get("properties", [])}
            labels = properties.get("LABELS", [])
            command = test.get("command", [])
            source = None
            if "--source" in command:
                index = command.index("--source")
                if index + 1 < len(command):
                    source = command[index + 1]
            ctest_cases.append({"name": test["name"], "labels": labels,
                                "command": command, "source": source,
                                "properties": properties})

    policy_spec = json.loads((workspace / APPLICABILITY_RECORD).read_text())
    state = review_state(bindings, policy_spec)

    for row in bindings:
        matching = [case for case in ctest_cases if row["asset_path"] and case["source"]
                    and Path(case["source"]).resolve() == Path(row["asset_path"]).resolve()]
        row["workspace_ctest_same_source"] = [case["name"] for case in matching]
        row["registration_surface"] = "source-ctest-runner" if row["runner"] != "ctest" and row["ctest_selector"] else "workspace-ctest-selector" if row["runner"] == "ctest" else "direct-metadata-runner"

    for row in bindings:
        policy = state["policies"].get(row["binding_id"])
        if policy is None:
            continue
        row["native_policy"] = policy
        row["audit_execution_status"] = "not_run"
        if row["asset_exists"] is False:
            row["asset_resolution"] = "unresolved_in_current_checkout"
            if row["source_commit"] and row["repo"] not in {"darling-workspace", "manifest"}:
                root = workspace.parent / project_paths.get(row["repo"], row["repo"])
                check = subprocess.run(["git", "-C", str(root), "cat-file", "-e",
                                        f"{row['source_commit']}:{row['script']}"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                row["declared_commit_asset"] = "present" if check.returncode == 0 else "unresolved"
                if check.returncode:
                    row["declared_commit_asset_diagnostic"] = check.stderr.strip()

    if state["problems"]:
        listing = "\n".join(f"  - {gap}" for gap in state["problems"])
        raise ValueError(
            f"{len(state['problems'])} applicability problem(s); all of them are listed here, "
            f"not only the first:\n{listing}\n"
            "Review each one and record its policy together with the current declaration identity "
            f"in {APPLICABILITY_RECORD}.")

    summary = {
        "profiles": len(profiles), "patch_entries": len(patches), "test_bindings": len(bindings),
        "patches_without_tests": [p for p in patches if not p["test_count"]],
        "environments": dict(collections.Counter(r["environment"] for r in bindings)),
        "runners": dict(collections.Counter(r["runner"] for r in bindings)),
        "coverage_tiers": dict(collections.Counter(r["coverage_tier"] for r in bindings)),
        "blocked_bindings": sum(bool(r["blocked"]) for r in bindings),
        "unresolved_script_assets_in_current_checkout": [{k: r[k] for k in ("binding_id", "name", "asset_path")} for r in bindings if r["asset_exists"] is False],
        "workspace_ctest_entries": len(ctest_cases),
        "native_policy": dict(collections.Counter(r["native_policy"]["policy"] for r in bindings)),
        "same_source_metadata_ctest_bindings": sum(bool(r["workspace_ctest_same_source"]) for r in bindings),
    }
    snapshot = {"scope": "All concrete patches/*/patches.yml entries normalized independently, not an expanded runtime stack. Shared fixtures remain separate patch bindings. CTest scope is the configured workspace testkit with default options; source-repo CTest builds and disabled conditional suites are not executed or exhaustively discovered. Missing assets and unresolved applicability are not silently excluded. No run verdict is inferred from metadata, labels or declarations.",
                "normalizer_sha256": identity(workspace / "west_commands/test_manifest.py"),
                "policy_sha256": identity(workspace / APPLICABILITY_RECORD),
                "profiles": profiles, "patches": patches, "bindings": bindings,
                "workspace_ctest": ctest_cases, "summary": summary}
    output.mkdir(parents=True, exist_ok=True)
    (output / "inventory.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


def review_parser(command):
    parser = argparse.ArgumentParser(
        prog=f"{Path(__file__).name} {command}", description=REVIEW_DESCRIPTION[command])
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help="West workspace whose declarations are reviewed")
    parser.add_argument("--record", type=Path, default=None,
                        help="record to read or rewrite, default "
                             f"{APPLICABILITY_RECORD} under the workspace")
    if command == "report":
        return parser
    flag = "--move" if command == "carry" else "--shift"
    parser.add_argument(flag, dest="moves", action="append", default=[], metavar="OLD=NEW",
                        help="explicit OLD=NEW mapping: the reviewer names the keys, "
                             "the tool never guesses them")
    parser.add_argument("--drop", dest="drops", action="append", default=[], metavar="KEY",
                        help="drop the superseded review at KEY explicitly")
    return parser


def parse_moves(parser, command, arguments, drops):
    flag = "--move" if command == "carry" else "--shift"
    if not arguments and not drops:
        parser.error(f"{command} needs at least one {flag} OLD=NEW or --drop KEY")
    moves = {}
    for argument in arguments:
        old, separator, new = argument.partition("=")
        if not separator or not old or not new:
            parser.error(f"{flag} takes OLD=NEW, got {argument!r}")
        if old == new:
            parser.error(f"{flag} {argument!r} names one key as both source and target")
        if old in moves:
            parser.error(f"{flag} moves {old} twice")
        if new in moves.values():
            parser.error(f"{flag} moves two records onto {new}; name one source per target")
        moves[old] = new
    return moves


def report_command(command, workspace, record_path, spec, bindings, state):
    recorded = spec["bindings"]
    document = {
        "scope": "Read-only. identity_sha256 covers the normalized test declaration and its "
                 "relative West project path, so a binding keeps its identity when its profile "
                 "or ordinal changes. No product build, no test execution, no record write.",
        "record": display_path(record_path, workspace),
        "counts": {
            "bindings": len(bindings),
            "policy_records": len(recorded),
            "reviewed_bindings": len(state["policies"]),
            "bindings_without_policy": len(state["bindings_without_policy"]),
            "identity_mismatches": len(state["identity_mismatches"]),
            "policy_records_without_binding": len(state["policy_records_without_binding"]),
            "problems": len(state["problems"]),
        },
        "bindings": [{"binding_id": row["binding_id"], "name": row["name"],
                      "identity_sha256": row["identity_sha256"],
                      "recorded_identity_sha256": recorded.get(
                          row["binding_id"], {}).get("identity_sha256")}
                     for row in bindings],
        "bindings_without_policy": state["bindings_without_policy"],
        "identity_mismatches": state["identity_mismatches"],
        "policy_records_without_binding": state["policy_records_without_binding"],
        "problems": state["problems"],
    }
    print(json.dumps(document, indent=2))
    if state["problems"]:
        print(f"{command}: {len(state['problems'])} applicability problem(s); bindings_without_policy, "
              f"identity_mismatches and policy_records_without_binding name them", file=sys.stderr)
        return 1
    return 0


def apply_review(command, workspace, record_path, spec, bindings, moves, drops):
    """Apply a reviewer-supplied mapping to the reviewed applicability record.

    The mapping is always explicit: the tool moves a recorded policy only between
    the keys a reviewer names, and only when the declaration behind the target key
    is the one the review was written for. It never invents a policy for a key that
    had none and never drops a review the mapping did not name.
    """
    recorded = spec["bindings"]
    bindings_by_key = {row["binding_id"]: row for row in bindings}
    sources = set(moves)
    targets = set(moves.values())
    unique_drops = list(dict.fromkeys(drops))
    label = display_path(record_path, workspace)

    def refuse(message):
        print(f"{command}: refusing to change {label}: {message}", file=sys.stderr)
        return 1

    # Structural checks first: they read the reviewer's keys and the current
    # declarations, so a malformed mapping is refused even when its targets
    # already carry the review it names.
    for old, new in moves.items():
        if command == "rekey" and old.rsplit(":", 1)[0] != new.rsplit(":", 1)[0]:
            return refuse(f"rekey re-records a pure index shift, so {old} and {new} must be "
                          f"ordinals of the same profile and patch entry")
        if new not in bindings_by_key:
            return refuse(f"{new} declares no test in the current tree: refusing to record an "
                          f"applicability decision for a key with no binding")
    for key in unique_drops:
        if key in sources:
            return refuse(f"{key} is both moved and dropped: a record cannot be carried and "
                          f"deleted, name each key once")

    # Idempotent: when every target key already carries the review of the
    # declaration it names, the mapping has been applied and nothing is written.
    if moves and all(key in recorded and key in bindings_by_key
                     and recorded[key]["identity_sha256"] == bindings_by_key[key]["identity_sha256"]
                     for key in targets):
        print(f"{command}: already applied; every target key already reviews its current "
              f"declaration, {label} unchanged")
        return 0

    for old, new in moves.items():
        review = recorded.get(old)
        if review is None:
            return refuse(f"no applicability policy is recorded for {old}: refusing to invent "
                          f"one for {new}")
        if review["identity_sha256"] != bindings_by_key[new]["identity_sha256"]:
            return refuse(f"{new} declares a different test than the review recorded at {old}: "
                          f"refusing to carry a review across keys whose declarations differ in "
                          f"more than the key")
        if review.get("name") != bindings_by_key[new]["name"]:
            return refuse(f"the review recorded at {old} names {review.get('name')!r} while {new} "
                          f"declares {bindings_by_key[new]['name']!r}")
        if new in recorded and new not in sources and new not in unique_drops:
            return refuse(f"{new} already has a reviewed record that this mapping neither moves "
                          f"nor drops: name it with --drop to replace it or as a source of its "
                          f"own move")

    result = {}
    for key, entry in recorded.items():
        if key in unique_drops:
            continue
        result[moves.get(key, key)] = entry
    unchanged = result == recorded
    for old, new in moves.items():
        entry = recorded[old]
        print(f"{command}: carried {old} -> {new} (policy {entry['policy']}, reason verbatim)")
    for key in unique_drops:
        entry = recorded.get(key)
        if entry is None:
            print(f"{command}: {key} already has no review record")
        else:
            print(f"{command}: dropped {key} (name {entry.get('name')!r}, policy {entry['policy']})")
    if unchanged:
        print(f"{command}: no change, {label} unchanged")
        return 0
    spec["bindings"] = result
    record_path.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"{command}: wrote {label} with {len(result)} review record(s); run "
          f"`{Path(__file__).name} report` to confirm it matches the current declarations")
    return 0


def review_command(command, argv):
    parser = review_parser(command)
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    record_path = (args.record or workspace / APPLICABILITY_RECORD).resolve()
    bindings = declared_bindings(workspace, west_project_paths(workspace))["bindings"]
    spec = json.loads(record_path.read_text())
    state = review_state(bindings, spec)
    if command == "report":
        return report_command(command, workspace, record_path, spec, bindings, state)
    moves = parse_moves(parser, command, args.moves, args.drops)
    return apply_review(command, workspace, record_path, spec, bindings, moves, args.drops)


def main(argv):
    if argv[:1] and argv[0] in REVIEW_COMMANDS:
        return review_command(argv[0], argv[1:])
    return inventory_command(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

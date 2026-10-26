#!/usr/bin/env python3
"""Focused behavioral contract for the profile-free dev check/package core."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import dev_check


FAKE_WEST = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
log = Path(os.environ["DEV_CHECK_FAKE_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"argv": argv}) + "\n")
if argv[:1] == ["list"]:
    repo = Path(os.environ["DEV_CHECK_MANIFEST_REPO"])
    source = Path(os.environ["DEV_CHECK_SOURCE_ROOT"])
    source_sha = os.environ["DEV_CHECK_SOURCE_HEAD"]
    for name, path, sha in (
        ("manifest", repo, os.environ["DEV_CHECK_MANIFEST_HEAD"]),
        ("darling", source, source_sha),
        ("darling-dependency", source / "dependency", os.environ["DEV_CHECK_DEPENDENCY_HEAD"]),
        ("darling-docs", source / "docs" / "standalone", os.environ["DEV_CHECK_DOCS_HEAD"]),
    ):
        print(f"{name}\t{path}\t{sha}\tcloned")
    raise SystemExit(0)
if argv[:1] == ["test"] and "--list" in argv:
    print("fixture.case: [env:host diag:bare kind:behavior]")
    raise SystemExit(0)
if argv[:1] == ["test"]:
    print("workload pass")
    raise SystemExit(0)
if argv[-1:] == ["--version"]:
    print("West version: fixture-1.0")
    raise SystemExit(0)
raise SystemExit(2)
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


class FakeRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.launcher = root / "bin" / "darling"
        self.system_root = root / "libexec" / "darling"
        self.identity = {"fixture": "installed-runtime"}

    def environment(self, prefix: Path, base=None) -> dict[str, str]:
        return {
            "DPREFIX": str(prefix),
            "DARLING_PREFIX": str(prefix),
            "DARLING_RUNTIME_ROOT": str(self.root),
        }


def make_repo(root: Path) -> tuple[Path, Path]:
    repo = root / "manifest"
    source = root / "darling"
    source.mkdir()
    (source / "README").write_text("source fixture\n", encoding="utf-8")
    (source / ".gitignore").write_text("docs/\n", encoding="utf-8")
    repo.mkdir()
    (repo / "west.yml").write_text("manifest:\n  projects: []\n", encoding="utf-8")
    (repo / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.name", "Dev Check Contract")
    git(repo, "config", "user.email", "dev-check-contract@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    git(source, "config", "user.name", "Dev Check Contract")
    git(source, "config", "user.email", "dev-check-contract@example.invalid")
    dependency = source / "dependency"
    dependency.mkdir()
    (dependency / "README").write_text("tracked dependency fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(dependency)], check=True)
    git(dependency, "config", "user.name", "Dev Check Contract")
    git(dependency, "config", "user.email", "dev-check-contract@example.invalid")
    git(dependency, "add", ".")
    git(dependency, "commit", "-qm", "dependency fixture")
    git(source, "add", ".")
    git(source, "commit", "-qm", "source fixture")
    docs = source / "docs" / "standalone"
    docs.mkdir(parents=True)
    (docs / "README").write_text("independent West docs fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(docs)], check=True)
    git(docs, "config", "user.name", "Dev Check Contract")
    git(docs, "config", "user.email", "dev-check-contract@example.invalid")
    git(docs, "add", ".")
    git(docs, "commit", "-qm", "independent docs fixture")
    return repo, source


def build_plan(repo: Path, west: list[str], tier: str, evidence: Path, *, workloads=(), runtime_root=None, prefix=None, source_root=None, build_dir=None):
    return dev_check.build_check_plan(
        manifest_repo=repo,
        west_argv=west,
        tier=tier,
        workloads=list(workloads),
        runtime_root=runtime_root,
        bead=None,
        evidence=evidence,
        prefix=prefix,
        build_dir=build_dir,
        source_root=source_root,
    )


def assert_no_profiles(steps) -> None:
    for step in steps:
        text = " ".join(step["argv"])
        assert "--profile" not in text
        assert "--patch" not in text
        assert "--materialize-profile" not in text
        assert "patch" not in step["name"]


def selected_source_contract(
    root: Path, repo: Path, source: Path, west: list[str],
    runtime_root: Path, prefix: Path, runtime: FakeRuntime,
) -> None:
    outside = root / "outside"
    candidate = root / "selected-product"
    canonical_before = git(source, "diff", "--binary", "HEAD", "--")
    canonical_head = git(source, "rev-parse", "HEAD")
    unrelated = root / "unrelated-source"
    unrelated.mkdir()
    (unrelated / "README").write_text("not the Darling product\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(unrelated)], check=True)
    git(unrelated, "config", "user.name", "Unrelated Source Fixture")
    git(unrelated, "config", "user.email", "unrelated@example.invalid")
    git(unrelated, "add", ".")
    git(unrelated, "commit", "-qm", "unrelated source fixture")
    try:
        build_plan(
            repo, west, "quick", outside / "unrelated-context.json", source_root=unrelated
        )
    except dev_check.DevCheckError as error:
        assert "active West pin" in str(error)
    else:
        raise AssertionError("an unrelated repository was accepted as the selected product")
    git(source, "worktree", "add", "--detach", str(candidate), "HEAD")
    git(
        source / "dependency", "worktree", "add", "--detach",
        str(candidate / "dependency"), "HEAD",
    )
    (candidate / "README").write_text("committed candidate product change\n", encoding="utf-8")
    git(candidate, "add", "README")
    git(candidate, "commit", "-qm", "candidate product change")
    nested = candidate / "dependency"
    (nested / "README").write_text("nested tracked change A\n", encoding="utf-8")
    frozen = build_plan(
        repo, west, "quick", outside / "nested-frozen.json", source_root=candidate
    )
    snapshot = frozen["inputs"]["package_snapshot"]
    projects = {row["name"]: row for row in snapshot["projects"]}
    assert frozen["inputs"]["source_root"] == str(candidate)
    assert projects["darling"]["path"] == str(candidate)
    assert projects["darling"]["canonical_path"] == str(source)
    assert projects["darling"]["sha"] == canonical_head
    assert projects["darling"]["head"] == git(candidate, "rev-parse", "HEAD")
    assert projects["darling"]["head"] != projects["darling"]["sha"]
    assert projects["darling-dependency"]["path"] == str(nested)
    docs = source / "docs" / "standalone"
    assert projects["darling-docs"]["path"] == str(docs)
    assert not (candidate / "docs" / "standalone").exists()
    assert [entry["project"] for entry in snapshot["project_changes"]] == ["darling-dependency"]
    before_root = snapshot["source_changes"]
    (nested / "README").write_text("nested tracked change B\n", encoding="utf-8")
    after_root = dev_check._git_changes_snapshot(candidate, "selected product")
    assert after_root == before_root, "fixture must defeat a root-only -dirty snapshot"
    try:
        dev_check.execute_check(frozen)
    except dev_check.DevCheckError:
        pass
    else:
        raise AssertionError("nested tracked A-to-B mutation escaped the frozen check")

    staged_module = nested / "new-module.c"
    staged_module.write_text("int STAGED_NESTED_SOURCE = 1;\n", encoding="utf-8")
    git(nested, "add", staged_module.name)
    staged_module.write_text("int STAGED_NESTED_SOURCE = 2;\n", encoding="utf-8")
    index_frozen = build_plan(
        repo, west, "quick", outside / "nested-index-frozen.json", source_root=candidate
    )
    index_snapshot = index_frozen["inputs"]["package_snapshot"]
    before_index = index_snapshot["project_changes"][0]["changes"]
    git(nested, "add", staged_module.name)
    after_index = dev_check._git_changes_snapshot(nested, "selected nested project")
    assert after_index["working_tree_diff"] == before_index["working_tree_diff"]
    assert after_index["staged_diff"] != before_index["staged_diff"]
    assert dev_check._git_changes_snapshot(candidate, "selected product") == index_snapshot["source_changes"]
    try:
        dev_check.execute_check(index_frozen)
    except dev_check.DevCheckError:
        pass
    else:
        raise AssertionError("nested index-only mutation escaped the frozen check")

    docs_frozen = build_plan(
        repo, west, "quick", outside / "docs-frozen.json", source_root=candidate
    )
    (docs / "README").write_text("independent docs tracked change\n", encoding="utf-8")
    try:
        dev_check.execute_check(docs_frozen)
    except dev_check.DevCheckError:
        pass
    else:
        raise AssertionError("independent active West docs mutation escaped the frozen check")

    build = root / "selected-build"
    build.mkdir()
    (build / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={candidate}\n", encoding="utf-8"
    )
    from_build = build_plan(
        repo, west, "quick", outside / "build-context.json", build_dir=build
    )
    assert from_build["inputs"]["source_root"] == str(candidate)
    original_identity = runtime.identity
    try:
        runtime.identity = {
            "fixture": "installed-runtime", "source": {"directory": str(candidate)}
        }
        from_receipt = build_plan(
            repo, west, "quick", outside / "receipt-context.json", runtime_root=runtime_root
        )
        assert from_receipt["inputs"]["source_root"] == str(candidate)
        (build / "CMakeCache.txt").write_text(
            f"CMAKE_HOME_DIRECTORY:INTERNAL={source}\n", encoding="utf-8"
        )
        build_precedence = build_plan(
            repo, west, "quick", outside / "build-precedence.json",
            runtime_root=runtime_root, build_dir=build,
        )
        assert build_precedence["inputs"]["source_root"] == str(source)
        explicit_precedence = build_plan(
            repo, west, "quick", outside / "explicit-precedence.json",
            runtime_root=runtime_root, build_dir=build, source_root=candidate,
        )
        assert explicit_precedence["inputs"]["source_root"] == str(candidate)
    finally:
        runtime.identity = original_identity

    acceptance = build_plan(
        repo, west, "acceptance", outside / "selected-acceptance.json",
        workloads=["homebrew-source"], runtime_root=runtime_root, prefix=prefix,
        source_root=candidate,
    )
    receipt = dev_check.execute_check(acceptance)
    assert receipt["state"] == "committed"
    package = outside / "selected-review-package"
    planned = dev_check.build_package_plan(
        manifest_repo=repo, west_argv=west,
        receipt=outside / "selected-acceptance.json", output=package,
        evidence=outside / "selected-package.json", required_tier="acceptance",
    )
    packaged = dev_check.execute_package(planned)
    assert packaged["state"] == "committed"
    assert dev_check.verify_package(package)["state"] == "valid"
    module_files = sorted(path.name for path in (package / "changes" / "projects").iterdir())
    assert module_files == [
        "project-darling-dependency-staged.diff",
        "project-darling-dependency-working.diff",
        "project-darling-docs-staged.diff",
        "project-darling-docs-working.diff",
    ]
    assert b"nested tracked change B" in (
        package / "changes" / "projects" / "project-darling-dependency-working.diff"
    ).read_bytes()
    assert b"STAGED_NESTED_SOURCE = 2" in (
        package / "changes" / "projects" / "project-darling-dependency-staged.diff"
    ).read_bytes()
    assert b"independent docs tracked change" in (
        package / "changes" / "projects" / "project-darling-docs-working.diff"
    ).read_bytes()
    tampered = outside / "tampered-module"
    shutil.copytree(package, tampered)
    module_diff = tampered / "changes" / "projects" / "project-darling-dependency-working.diff"
    module_diff.write_bytes(module_diff.read_bytes() + b"tampered module diff\n")
    try:
        dev_check.verify_package(tampered)
    except dev_check.DevCheckError:
        pass
    else:
        raise AssertionError("tampered nested-project diff was accepted")
    assert git(source, "rev-parse", "HEAD") == canonical_head
    assert git(source, "diff", "--binary", "HEAD", "--") == canonical_before


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dev-check-contract-") as temporary:
        root = Path(temporary)
        repo, source = make_repo(root)
        runtime_root = root / "runtime"
        prefix = root / "prefix"
        prefix.mkdir()
        fake_west = root / "west"
        fake_west.write_text(FAKE_WEST, encoding="utf-8")
        fake_west.chmod(fake_west.stat().st_mode | stat.S_IXUSR)
        log = root / "west.jsonl"
        os.environ.update(
            {
                "DEV_CHECK_FAKE_LOG": str(log),
                "DEV_CHECK_MANIFEST_REPO": str(repo),
                "DEV_CHECK_SOURCE_ROOT": str(source),
                "DEV_CHECK_SOURCE_HEAD": git(source, "rev-parse", "HEAD"),
                "DEV_CHECK_MANIFEST_HEAD": git(repo, "rev-parse", "HEAD"),
                "DEV_CHECK_DEPENDENCY_HEAD": git(source / "dependency", "rev-parse", "HEAD"),
                "DEV_CHECK_DOCS_HEAD": git(source / "docs" / "standalone", "rev-parse", "HEAD"),
            }
        )
        runtime = FakeRuntime(runtime_root)
        original_resolver = dev_check.resolve_runtime
        dev_check.resolve_runtime = lambda root=None: runtime
        try:
            outside = root / "outside"
            outside.mkdir()
            quick = build_plan(repo, [str(fake_west)], "quick", outside / "quick.json")
            assert quick["inputs"]["workloads"] == []
            assert quick["inputs"]["source_root"] == str(source)
            assert "profile" not in quick["inputs"]
            assert "patch" not in quick["inputs"]
            assert [step["name"] for step in quick["steps"]] == ["test-list"]
            assert_no_profiles(quick["steps"])
            assert "--source-root" in quick["steps"][0]["argv"]
            clean_snapshot = quick["inputs"]["package_snapshot"]
            secret = b"PRIVATE_UNTRACKED_EVIDENCE_MUST_NOT_BE_ARCHIVED"
            for checkout in (repo, source, source / "dependency"):
                generated = checkout / ".west-state"
                generated.mkdir()
                (generated / "private.bin").write_bytes(secret)
                os.mkfifo(generated / "blocked-pipe")
                (generated / "external-link").symlink_to(outside, target_is_directory=True)
                with (generated / "large-state.bin").open("wb") as stream:
                    stream.truncate(32 * 1024 * 1024)
            new_manifest = repo / "new-source.py"
            new_source = source / "new-source.c"
            new_manifest.write_text("STAGED_MANIFEST_SOURCE = True\n", encoding="utf-8")
            new_source.write_text("int STAGED_PRODUCT_SOURCE = 1;\n", encoding="utf-8")
            untracked = build_plan(
                repo, [str(fake_west)], "quick", outside / "untracked.json"
            )
            assert untracked["inputs"]["package_snapshot"] == clean_snapshot
            for changes in (clean_snapshot, clean_snapshot["source_changes"]):
                assert changes["dirty"] is False
                assert changes["untracked"] == {
                    "coverage": "excluded",
                    "inventory": "not-collected",
                    "new_source": "stage-before-review",
                }
            (repo / ".west-state" / "private.bin").write_bytes(secret + b"-changed")
            (source / ".west-state" / "extra-evidence.txt").write_bytes(secret)
            (source / "dependency" / ".west-state" / "more-state.txt").write_bytes(secret)
            churned = build_plan(
                repo, [str(fake_west)], "quick", outside / "untracked-churned.json"
            )
            assert churned["inputs"]["package_snapshot"] == clean_snapshot
            quick_receipt = dev_check.execute_check(quick)
            assert quick_receipt["state"] == "committed"
            assert quick_receipt["returncode"] == 0
            assert quick_receipt["results"][0]["selection_oracle"] == {
                "required": True,
                "selected": True,
            }

            canonical = build_plan(repo, [str(fake_west)], "canonical", outside / "canonical.json")
            assert [step["name"] for step in canonical["steps"]] == ["test-list", "host-test"]
            assert_no_profiles(canonical["steps"])
            canonical_receipt = dev_check.execute_check(canonical)
            assert canonical_receipt["state"] == "committed"
            assert canonical_receipt["returncode"] == 0

            git(repo, "add", new_manifest.name)
            git(source, "add", new_source.name)
            (repo / "fixture.txt").write_text("tracked manifest change\n", encoding="utf-8")
            (source / "README").write_text("tracked product change\n", encoding="utf-8")

            acceptance = build_plan(
                repo,
                [str(fake_west)],
                "acceptance",
                outside / "acceptance.json",
                workloads=["homebrew-source"],
                runtime_root=runtime_root,
                prefix=prefix,
            )
            assert [step["name"] for step in acceptance["steps"]] == [
                "test-list",
                "host-test",
                "workload:homebrew-source",
            ]
            workload = acceptance["steps"][-1]["argv"]
            assert "--workload" in workload
            assert "--runtime-root" in workload
            assert "--prefix" in workload
            assert "--source-root" in workload
            assert_no_profiles(acceptance["steps"])
            reviewed_snapshot = acceptance["inputs"]["package_snapshot"]
            assert reviewed_snapshot != clean_snapshot
            for changes in (reviewed_snapshot, reviewed_snapshot["source_changes"]):
                assert changes["dirty"] is True
                assert changes["working_tree_diff"]["bytes"] > 0
                assert changes["staged_diff"]["bytes"] > 0
            acceptance_receipt = dev_check.execute_check(acceptance)
            assert acceptance_receipt["state"] == "committed"
            assert acceptance_receipt["returncode"] == 0
            receipt_path = outside / "acceptance.json"

            package_plan = dev_check.build_package_plan(
                manifest_repo=repo,
                west_argv=[str(fake_west)],
                receipt=receipt_path,
                output=outside / "review-package",
                evidence=outside / "package.json",
                required_tier="acceptance",
            )
            assert "profile" not in package_plan["inputs"]
            assert "patch" not in package_plan["inputs"]
            (repo / ".west-state" / "late-evidence.txt").write_bytes(secret)
            (source / ".west-state" / "private.bin").unlink()
            package_receipt = dev_check.execute_package(package_plan)
            assert package_receipt["state"] == "committed"
            package = outside / "review-package"
            assert (package / "check-receipt.json").is_file()
            assert (package / "source-review.json").is_file()
            assert (package / "manifest" / "west.yml").is_file()
            assert not (package / "changes" / "untracked").exists()
            assert not (package / "changes" / "source-untracked").exists()
            for path in package.rglob("*"):
                if path.is_file():
                    assert secret not in path.read_bytes(), path
            assert b"STAGED_MANIFEST_SOURCE" in (
                package / "changes" / "manifest-staged.diff"
            ).read_bytes()
            assert b"STAGED_PRODUCT_SOURCE" in (
                package / "changes" / "source-staged.diff"
            ).read_bytes()
            assert b"tracked manifest change" in (
                package / "changes" / "manifest-working.diff"
            ).read_bytes()
            assert b"tracked product change" in (
                package / "changes" / "source-working.diff"
            ).read_bytes()
            verified = dev_check.verify_package(package)
            assert verified["state"] == "valid"
            assert verified["operation"] == "package-verify"

            tampered = outside / "tampered"
            shutil.copytree(package, tampered)
            with (tampered / "source-review.json").open("a", encoding="utf-8") as stream:
                stream.write("tamper\n")
            try:
                dev_check.verify_package(tampered)
            except dev_check.DevCheckError:
                pass
            else:
                raise AssertionError("tampered package was accepted")

            tampered_diff = outside / "tampered-source-diff"
            shutil.copytree(package, tampered_diff)
            with (tampered_diff / "changes" / "source-staged.diff").open("ab") as stream:
                stream.write(b"tampered staged addition\n")
            try:
                dev_check.verify_package(tampered_diff)
            except dev_check.DevCheckError:
                pass
            else:
                raise AssertionError("tampered staged source diff was accepted")

            racing_output = outside / "racing-package"
            racing = dev_check.build_package_plan(
                manifest_repo=repo,
                west_argv=[str(fake_west)],
                receipt=receipt_path,
                output=racing_output,
                evidence=outside / "racing-package.json",
                required_tier="acceptance",
            )
            racing_output.mkdir()
            sentinel = racing_output / "foreign.txt"
            sentinel.write_bytes(b"other publisher owns this output\n")
            try:
                dev_check.execute_package(racing)
            except dev_check.DevCheckError:
                pass
            else:
                raise AssertionError("package publication overwrote an existing output")
            assert list(racing_output.iterdir()) == [sentinel]
            assert sentinel.read_bytes() == b"other publisher owns this output\n"

            source_changed = build_plan(
                repo, [str(fake_west)], "quick", outside / "source-changed.json"
            )
            (source / "README").write_text("later tracked source change\n", encoding="utf-8")
            try:
                dev_check.execute_check(source_changed)
            except dev_check.DevCheckError:
                pass
            else:
                raise AssertionError("changed tracked product source plan was executed")

            before = (repo / "west.yml").read_text(encoding="utf-8")
            changed = build_plan(
                repo, [str(fake_west)], "quick", outside / "changed.json"
            )
            (repo / "west.yml").write_text(before + "# changed\n", encoding="utf-8")
            try:
                dev_check.execute_check(changed)
            except dev_check.DevCheckError:
                pass
            else:
                raise AssertionError("changed source plan was executed")
            selected_source_contract(
                root, repo, source, [str(fake_west)], runtime_root, prefix, runtime
            )
        finally:
            dev_check.resolve_runtime = original_resolver
    print("dev check source workflow contract: PASS")


if __name__ == "__main__":
    main()

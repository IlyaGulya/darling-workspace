"""Exercise the real local installer against isolated manifest/source layouts."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SetupLocalContract(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dw setup-local ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest"
        (self.manifest / "bin").mkdir(parents=True)
        (self.manifest / "source").mkdir()
        shutil.copy2(ROOT / "bin/dw", self.manifest / "bin/dw")
        shutil.copy2(ROOT / "source/AGENTS.md", self.manifest / "source/AGENTS.md")
        self.skills = self.manifest / ".agents/skills"
        (self.skills / "fixture").mkdir(parents=True)
        self.skill = self.skills / "fixture/SKILL.md"
        self.skill.write_text("canonical fixture skill\n")
        self.env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            self.env.pop(key, None)
        self.source = self.make_source("selected source")

    def git(self, source: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(source), *args], env=self.env,
            text=True, capture_output=True, check=True, timeout=20,
        )

    def make_source(self, name: str, *, separate: bool = False) -> Path:
        source = self.root / name
        source.mkdir()
        args = ["-c", "init.templateDir=", "init", "-q"]
        if separate:
            args.append(f"--separate-git-dir={self.root / (name + ' git storage')}")
        self.git(source, *args)
        (source / ".gitignore").write_text("/product-build/\n")
        self.git(source, "add", ".gitignore")
        exclude = self.exclude(source)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("# existing local exclude\n/local-only\n")
        return source

    def exclude(self, source: Path) -> Path:
        return Path(self.git(
            source, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude",
        ).stdout.strip())

    def install(self, source: Path | None = None, *, succeeds: bool = True) -> None:
        source = self.source if source is None else source
        result = subprocess.run(
            ["bash", str(self.manifest / "bin/dw"), "setup-local"],
            cwd=self.manifest, env={**self.env, "DW_DARLING_SRC": str(source)},
            text=True, capture_output=True, timeout=20,
        )
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(str(source), result.stderr)

    def assert_installed(self, source: Path) -> None:
        portable = source / ".agents/skills"
        compatibility = source / ".claude/skills"
        self.assertTrue(portable.is_symlink())
        self.assertEqual(portable.resolve(), self.skills.resolve())
        self.assertTrue(compatibility.is_symlink())
        self.assertEqual(os.readlink(compatibility), "../.agents/skills")
        self.assertEqual((compatibility / "fixture/SKILL.md").read_bytes(), self.skill.read_bytes())
        self.assertEqual((source / ".gitignore").read_text(), "/product-build/\n")
        for path in ("AGENTS.md", ".agents/skills", ".claude/skills"):
            self.git(source, "check-ignore", "--no-index", "--quiet", path)

    def test_fresh_install_and_repeat_share_canonical_skills(self) -> None:
        self.install()
        self.assert_installed(self.source)
        self.assertEqual(
            (self.source / "AGENTS.md").read_bytes(),
            (self.manifest / "source/AGENTS.md").read_bytes(),
        )
        exclude_before = self.exclude(self.source).read_bytes()
        self.install()
        self.assertEqual(self.exclude(self.source).read_bytes(), exclude_before)
        self.skill.write_text("updated canonical fixture skill\n")
        self.assert_installed(self.source)

    def test_preserves_custom_instructions_and_neighbor_files(self) -> None:
        instructions = self.source / "AGENTS.md"
        instructions.write_text("local instructions owned by the user\n")
        neighbors = [
            self.source / "CLAUDE.md",
            self.source / ".agents/local-config",
            self.source / ".claude/settings.json",
        ]
        for path in neighbors:
            path.parent.mkdir(exist_ok=True)
            path.write_text("local configuration\n")
        self.install()
        self.install()
        self.assertEqual(instructions.read_text(), "local instructions owned by the user\n")
        for path in neighbors:
            self.assertEqual(path.read_text(), "local configuration\n")
        self.assert_installed(self.source)

    def test_preserves_existing_instruction_symlinks(self) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                source = self.make_source(f"instruction-link-{dangling}")
                owned = source / "local-instructions"
                if not dangling:
                    owned.write_text("custom instruction target\n")
                (source / "AGENTS.md").symlink_to("local-instructions")
                self.install(source)
                self.assertEqual(os.readlink(source / "AGENTS.md"), "local-instructions")
                self.assertEqual(owned.exists(), not dangling)
                if not dangling:
                    self.assertEqual(owned.read_text(), "custom instruction target\n")
                self.assert_installed(source)

    def test_accepts_matching_relative_portable_link(self) -> None:
        portable = self.source / ".agents/skills"
        portable.parent.mkdir()
        target = os.path.relpath(self.skills, portable.parent)
        portable.symlink_to(target)
        self.install()
        self.assertEqual(os.readlink(portable), target)
        self.assert_installed(self.source)

    def test_preserves_conflicting_skill_paths_before_installing(self) -> None:
        for parent in (".agents", ".claude"):
            for kind in ("file", "directory", "wrong-link", "dangling-link"):
                with self.subTest(parent=parent, kind=kind):
                    source = self.make_source(f"conflict-{parent}-{kind}")
                    path = source / parent / "skills"
                    path.parent.mkdir()
                    owned = source / "owned-skills"
                    if kind == "file":
                        path.write_text("owned skill file\n")
                    elif kind == "directory":
                        path.mkdir()
                        (path / "custom-skill").write_text("owned skill directory\n")
                    else:
                        if kind == "wrong-link":
                            owned.mkdir()
                            (owned / "custom-skill").write_text("owned link target\n")
                        path.symlink_to(owned)
                    exclude_before = self.exclude(source).read_bytes()
                    self.install(source, succeeds=False)
                    if kind == "file":
                        self.assertEqual(path.read_text(), "owned skill file\n")
                    elif kind == "directory":
                        self.assertFalse(path.is_symlink())
                        self.assertEqual((path / "custom-skill").read_text(), "owned skill directory\n")
                    else:
                        self.assertEqual(os.readlink(path), str(owned))
                        self.assertEqual(owned.exists(), kind == "wrong-link")
                        if kind == "wrong-link":
                            self.assertEqual((owned / "custom-skill").read_text(), "owned link target\n")
                    self.assertFalse((source / "AGENTS.md").exists())
                    other = ".claude" if parent == ".agents" else ".agents"
                    self.assertFalse((source / other).exists())
                    self.assertEqual(self.exclude(source).read_bytes(), exclude_before)
                    self.assertEqual((source / ".gitignore").read_text(), "/product-build/\n")

    def test_preserves_conflicting_parent_paths(self) -> None:
        for parent in (".agents", ".claude"):
            for kind in ("file", "directory-link", "dangling-link"):
                with self.subTest(parent=parent, kind=kind):
                    source = self.make_source(f"parent-{parent}-{kind}")
                    path = source / parent
                    owned = source / "owned-parent"
                    if kind == "file":
                        path.write_text("owned parent file\n")
                    else:
                        if kind == "directory-link":
                            owned.mkdir()
                        path.symlink_to(owned)
                    self.install(source, succeeds=False)
                    if kind == "file":
                        self.assertEqual(path.read_text(), "owned parent file\n")
                    else:
                        self.assertEqual(os.readlink(path), str(owned))
                        if owned.exists():
                            self.assertEqual(list(owned.iterdir()), [])
                    self.assertFalse((source / "AGENTS.md").exists())

    def test_resolves_gitfile_excludes(self) -> None:
        source = self.make_source("gitfile source", separate=True)
        self.assertTrue((source / ".git").is_file())
        self.install(source)
        self.assert_installed(source)
        before = self.exclude(source).read_bytes()
        self.install(source)
        self.assertEqual(self.exclude(source).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

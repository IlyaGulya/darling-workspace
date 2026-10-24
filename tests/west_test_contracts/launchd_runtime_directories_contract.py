#!/usr/bin/env python3
"""Exercise launchd's real directory initializer against an isolated host filesystem.

Only the filesystem root is remapped; the product's initialization and error
handling execute unchanged. The consumer performs an atomic system-preference
write, matching the parent-directory requirement of Foundation's file writer.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT.parent / "darling/src/launchd/src/rootless_runtime.c"
DRIVER = r'''
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>

static const char *root;
static int rooted_path(const char *path, char *buffer, size_t capacity) {
    int count = snprintf(buffer, capacity, "%s%s", root, path);
    if (count < 0 || (size_t)count >= capacity) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return 0;
}
static int rooted_mkdir(const char *path, mode_t mode) {
    char buffer[PATH_MAX];
    return rooted_path(path, buffer, sizeof(buffer)) ? -1 : mkdir(buffer, mode);
}
static int rooted_stat(const char *path, struct stat *status) {
    char buffer[PATH_MAX];
    return rooted_path(path, buffer, sizeof(buffer)) ? -1 : stat(buffer, status);
}
static int rooted_chmod(const char *path, mode_t mode) {
    char buffer[PATH_MAX];
    return rooted_path(path, buffer, sizeof(buffer)) ? -1 : chmod(buffer, mode);
}
#define mkdir(path, mode) rooted_mkdir(path, mode)
#define stat(path, status) rooted_stat(path, status)
#define chmod(path, mode) rooted_chmod(path, mode)
#include ROOTLESS_RUNTIME_SOURCE

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    root = argv[1];
    return rootless_runtime_prepare();
}
'''


class LaunchdRuntimeDirectoriesContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="launchd-directories-contract-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        driver = cls.root / "driver.c"
        driver.write_text(DRIVER)
        cls.program = cls.root / "prepare-runtime"
        subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
             "-DROOTLESS_RUNTIME_SOURCE=" + json.dumps(str(SOURCE)),
             str(driver), "-o", str(cls.program)],
            check=True, capture_output=True, text=True, timeout=30,
        )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=self.root, prefix="prefix-")
        self.addCleanup(temporary.cleanup)
        self.prefix = Path(temporary.name)

    def prepare(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.program), str(self.prefix)],
            capture_output=True, text=True, timeout=10,
        )

    def test_fresh_boot_allows_atomic_system_preference_write(self) -> None:
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        preferences = self.prefix / "Library/Preferences"
        payload = b"a consumer's system-wide preference\n"
        with tempfile.NamedTemporaryFile(dir=preferences, delete=False) as stream:
            stream.write(payload)
            temporary = Path(stream.name)
        target = preferences / "com.example.runtime-contract.plist"
        os.replace(temporary, target)
        self.assertEqual(target.read_bytes(), payload)
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(target.read_bytes(), payload)

    def test_obstructing_file_is_reported_without_destroying_it(self) -> None:
        blocker = self.prefix / "Library/Preferences"
        blocker.parent.mkdir()
        blocker.write_bytes(b"must survive initialization\n")
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, errno.ENOTDIR, prepared.stderr)
        self.assertEqual(blocker.read_bytes(), b"must survive initialization\n")


if __name__ == "__main__":
    unittest.main()

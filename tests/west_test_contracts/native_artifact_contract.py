"""Execute a compiled fixture after mode-normalizing artifact transport."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TIER = ROOT / "ci/run-test-tier.sh"


def run(*args, check=True, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=check, text=True,
                          capture_output=True, timeout=60, **kwargs)


def snapshot(root):
    entries = {}
    for path in root.rglob("*"):
        name = str(path.relative_to(root))
        if path.is_symlink():
            entries[name] = ("symlink", os.readlink(path))
        else:
            entries[name] = (stat.S_IMODE(path.stat().st_mode),
                             hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "directory")
    return entries


with tempfile.TemporaryDirectory(prefix="native-artifact-contract-") as temporary:
    top = Path(temporary)
    bundle = top / "installed"
    (bundle / "testcase").mkdir(parents=True)
    resources = bundle / "resources"
    resources.mkdir()
    (resources / "binary payload.dat").write_bytes(b"\x00\xff\nresource\x00")
    (resources / "binary payload.dat").chmod(0o640)
    (resources / "data link").symlink_to("binary payload.dat")
    resources.chmod(0o750)
    (bundle / ".fixture-data").write_bytes(b"hidden resource\x00\xff")
    (bundle / "compat-install-manifest.tsv").write_text("transport\ttransport.probe\tTRANSPORT_OK\n")
    source = top / "probe.c"
    source.write_text(r'''
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
int main(int argc, char **argv) {
    (void)argc;
    const char *slash = strrchr(argv[0], '/');
    if (!slash) return 2;
    char path[4096];
    snprintf(path, sizeof(path), "%.*s/../resources/data link", (int)(slash - argv[0]), argv[0]);
    const unsigned char expected[] = {0, 255, 10, 'r', 'e', 's', 'o', 'u', 'r', 'c', 'e', 0};
    unsigned char actual[sizeof(expected) + 1];
    struct stat status;
    FILE *input = fopen(path, "rb");
    if (!input) return 3;
    size_t count = fread(actual, 1, sizeof(actual), input);
    int failed = ferror(input);
    fclose(input);
    if (failed || count != sizeof(expected) || memcmp(actual, expected, sizeof(expected))) return 4;
    if (stat(path, &status) || (status.st_mode & 0777) != 0640) return 5;
    puts("TRANSPORT_OK");
    return 0;
}
''')
    executable = bundle / "testcase/transport.probe"
    run("cc", "-std=c99", source, "-o", executable)
    executable.chmod(0o751)
    baseline = run(TIER, "macos-installed", bundle)
    assert "PASS macos/transport" in baseline.stdout

    # The old raw-directory route fails specifically because execute bits vanish.
    raw = top / "raw-download"
    shutil.copytree(bundle, raw, symlinks=True)
    for path in raw.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    rejected = run(TIER, "macos-installed", raw, check=False)
    assert rejected.returncode == 1 and "installed testcase is not executable:" in rejected.stderr
    print("RED raw artifact: installed testcase is not executable")

    archive = top / "oracle.tar"
    run(TIER, "macos-archive", bundle, archive)
    downloaded = top / "downloaded.tar"
    shutil.copyfile(archive, downloaded)
    downloaded.chmod(0o644)  # Artifact service permissions apply only to the container.
    extracted = top / "extracted"
    run(TIER, "macos-extract", downloaded, extracted, umask=0o077)
    assert snapshot(extracted) == snapshot(bundle), "archive changed file bytes, modes or symlink targets"
    fixed = run(TIER, "macos-installed", extracted)
    assert "PASS macos/transport" in fixed.stdout

    # Extraction must not merge into an existing destination, even an empty one.
    existing = top / "existing"
    existing.mkdir()
    assert run(TIER, "macos-extract", downloaded, existing, check=False).returncode != 0
    assert not list(existing.iterdir()), "refused destination was modified"
    print(f"GREEN archived artifact: compiled fixture executed on {platform.system()} {platform.machine()}")

print("PASS native-artifact-contract")

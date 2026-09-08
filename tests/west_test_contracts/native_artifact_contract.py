"""Execute a compiled fixture after mode-normalizing artifact transport."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TIER = ROOT / "ci/run-test-tier.sh"


def run(*args, check=True, **kwargs):
    result = subprocess.run([str(arg) for arg in args], text=True,
                            capture_output=True, timeout=60, **kwargs)
    if check and result.returncode:
        raise AssertionError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


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
    (resources / "._literal-data").write_bytes(b"literal hidden asset, not archive metadata")
    if platform.system() == "Darwin":
        # BSD tar must not manufacture extra AppleDouble files from host metadata.
        run("xattr", "-w", "org.darling.artifact-contract", "metadata-only",
            resources / "binary payload.dat")
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
    baseline = run(executable)
    assert baseline.stdout.splitlines() == ["TRANSPORT_OK"]

    # The old raw-directory route fails specifically because execute bits vanish.
    raw = top / "raw-download"
    shutil.copytree(bundle, raw, symlinks=True)
    for path in raw.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    try:
        run(raw / "testcase/transport.probe")
    except PermissionError:
        pass
    else:
        raise AssertionError("mode-normalizing transport preserved execution unexpectedly")
    print("RED raw artifact: operating system refused execution after execute bits vanished")

    archive = top / "oracle.tar"
    run(TIER, "macos-archive", bundle, archive)
    downloaded = top / "downloaded.tar"
    shutil.copyfile(archive, downloaded)
    downloaded.chmod(0o644)  # Artifact service permissions apply only to the container.
    with tarfile.open(downloaded) as payload:
        members = {Path(member.name).as_posix().rstrip("/") for member in payload.getmembers()
                   if Path(member.name).as_posix() != "."}
    assert members == set(snapshot(bundle)), "archive manufactured or omitted resource files"
    extracted = top / "extracted"
    run(TIER, "macos-extract", downloaded, extracted, umask=0o077)
    assert snapshot(extracted) == snapshot(bundle), "archive changed file bytes, modes or symlink targets"
    fixed = run(extracted / "testcase/transport.probe")
    assert fixed.stdout.splitlines() == ["TRANSPORT_OK"]

    # Extraction must not merge into an existing destination, even an empty one.
    existing = top / "existing"
    existing.mkdir()
    assert run(TIER, "macos-extract", downloaded, existing, check=False).returncode != 0
    assert not list(existing.iterdir()), "refused destination was modified"
    print(f"GREEN archived artifact: compiled fixture executed on {platform.system()} {platform.machine()}")

print("PASS native-artifact-contract")

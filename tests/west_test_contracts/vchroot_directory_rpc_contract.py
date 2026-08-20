"""Generated ABI contract for the retained vchroot-directory RPC."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DSERVER = Path(
    os.environ.get(
        "DARLING_DSERVER_SOURCE",
        ROOT.parent / "darling/src/external/darlingserver",
    )
).resolve()
GENERATOR = DSERVER / "scripts/generate-rpc-wrappers.py"
MLDR = ROOT.parent / "darling/src/startup/mldr/mldr.c"


def generate(generator: Path, output: Path) -> tuple[Path, Path, Path]:
    public = output / "include/darlingserver/rpc.h"
    internal = output / "internal/darlingserver/rpc.h"
    source = output / "rpc.c"
    subprocess.run(
        ["python3", str(generator), str(public), str(internal), str(source),
         "dserver-rpc-defs.h"],
        check=True,
        timeout=20,
    )
    return public, internal, source


def compile_consumer(root: Path, architecture: str, *, must_succeed: bool) -> None:
    probe = root / f"probe-{architecture}.c"
    probe.write_text(
        "#include <darlingserver/rpc.h>\n"
        "int probe(void) { int fd = -1; return dserver_rpc_vchroot_directory(&fd); }\n",
        encoding="utf-8",
    )
    command = ["clang", "-std=c11", "-Werror", "-fsyntax-only",
               "-I", str(root / "include"), str(probe)]
    if architecture == "i386":
        command.insert(1, "-m32")
    result = subprocess.run(command, text=True, capture_output=True, timeout=20)
    if (result.returncode == 0) != must_succeed:
        raise AssertionError(
            f"unexpected {architecture} generator result: {result.stderr}"
        )


with tempfile.TemporaryDirectory(prefix="vchroot-rpc-contract-") as temporary:
    root = Path(temporary)
    public, internal, source = generate(GENERATOR, root / "green")
    for artifact in (public, internal, source):
        text = artifact.read_text(encoding="utf-8")
        assert "vchroot_directory" in text
    for architecture in ("x86_64", "i386"):
        compile_consumer(root / "green", architecture, must_succeed=True)

    generator_text = GENERATOR.read_text(encoding="utf-8")
    declaration = """\
\t# Return a duplicate of the calling process's retained, session-bound
\t# vchroot directory.  The server owns the authoritative descriptor; the
\t# generated transport transfers only a new SCM_RIGHTS reference.
\t('vchroot_directory', [], [
\t\t('directory_fd', '@fd'),
\t]),

"""
    assert generator_text.count(declaration) == 1
    bad_generator = root / "generator-without-vchroot.py"
    bad_generator.write_text(generator_text.replace(declaration, ""), encoding="utf-8")
    generate(bad_generator, root / "red")
    for architecture in ("x86_64", "i386"):
        compile_consumer(root / "red", architecture, must_succeed=False)

mldr_text = MLDR.read_text(encoding="utf-8")
assert "index < DARLING_GUEST_NAMESPACE_AUTHORITY_DESCRIPTOR_COUNT" in mldr_text
assert "index < DARLING_GUEST_NAMESPACE_DESCRIPTOR_COUNT" in mldr_text
assert ": DARLING_GUEST_NAMESPACE_VCHROOT_FD" in mldr_text

print("VCHROOT_DIRECTORY_RPC_GENERATOR_VALID")

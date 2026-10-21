#!/usr/bin/env python3
"""Behavioral contract for the one runtime-artifact deploy table.

Pins the property that made two tools necessary: a component must be installed into EVERY
runtime copy the loader can use, and the install must be verified, not assumed. Runs the real
`install_and_verify` in a temporary prefix, plus the shell wrapper, so a later change to the
table or the wrapper is caught by behavior rather than by matching source text.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
MANIFEST = HERE.parents[2]
sys.path.insert(0, str(MANIFEST / "west_commands"))

from runtime_artifacts import COMPONENTS, install_and_verify  # noqa: E402


def build_component(build: Path, name: str, payload: bytes = b"runtime-bytes") -> None:
    source = build / COMPONENTS[name].source
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)


def make_destinations(prefix: Path, name: str, *, skip_last: bool = False) -> list[Path]:
    made = []
    destinations = COMPONENTS[name].destinations
    for index, relative in enumerate(destinations):
        if skip_last and index == len(destinations) - 1:
            continue
        destination = prefix / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        made.append(destination)
    return made


def test_default_path_installs_every_copy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build, prefix = root / "build", root / "prefix"
        for name in COMPONENTS:
            build_component(build, name, f"payload:{name}".encode())
            make_destinations(prefix, name)

        out = io.StringIO()
        rc = install_and_verify(prefix, build, list(COMPONENTS), out=out)
        assert rc == 0, out.getvalue()
        for name, component in COMPONENTS.items():
            want = (build / component.source).read_bytes()
            for relative in component.destinations:
                got = (prefix / relative).read_bytes()
                assert got == want, f"{name}: {relative} was not replaced"
        assert out.getvalue().count(": ok ") == sum(len(c.destinations) for c in COMPONENTS.values())


def test_missing_parent_is_skipped_not_created() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build, prefix = root / "build", root / "prefix"
        build_component(build, "mldr")
        created = make_destinations(prefix, "mldr", skip_last=True)

        out = io.StringIO()
        rc = install_and_verify(prefix, build, ["mldr"], out=out)
        assert rc == 0, out.getvalue()
        assert created and all(path.read_bytes() == b"runtime-bytes" for path in created)
        missing = prefix / COMPONENTS["mldr"].destinations[-1]
        assert not missing.exists(), "a destination whose parent was absent must not be created"
        assert "skip (no directory)" in out.getvalue()


def test_unbuilt_and_unknown_are_errors() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build, prefix = root / "build", root / "prefix"
        build.mkdir()
        prefix.mkdir()
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            assert install_and_verify(prefix, build, ["libxpc"], out=io.StringIO()) == 2
            assert install_and_verify(prefix, build, ["not-a-component"], out=io.StringIO()) == 2
        finally:
            sys.stderr = old
        assert "NOT BUILT" in err.getvalue()
        assert "unknown component" in err.getvalue()


def test_install_failure_is_reported() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build, prefix = root / "build", root / "prefix"
        build_component(build, "launchd")
        destination = make_destinations(prefix, "launchd")[0]
        destination.mkdir()  # a directory where the file belongs: copy must fail

        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            rc = install_and_verify(prefix, build, ["launchd"], out=io.StringIO())
        finally:
            sys.stderr = old
        assert rc == 1, err.getvalue()
        assert "INSTALL FAILED" in err.getvalue()


def test_wrapper_reaches_the_same_implementation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build, prefix = root / "build", root / "prefix"
        build_component(build, "corefoundation", b"cf-bytes")
        destinations = make_destinations(prefix, "corefoundation")

        result = subprocess.run(
            [str(MANIFEST / "scripts/darling-deploy-verify.sh"),
             "--prefix", str(prefix), "--build", str(build), "--component", "corefoundation"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert f"corefoundation: ok" in result.stdout
        for destination in destinations:
            assert destination.read_bytes() == b"cf-bytes"


def main() -> None:
    checks = [
        test_default_path_installs_every_copy,
        test_missing_parent_is_skipped_not_created,
        test_unbuilt_and_unknown_are_errors,
        test_install_failure_is_reported,
        test_wrapper_reaches_the_same_implementation,
    ]
    for check in checks:
        check()
        print(f"ok {check.__name__}")


if __name__ == "__main__":
    main()

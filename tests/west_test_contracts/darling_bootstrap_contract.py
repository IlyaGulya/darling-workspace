"""Bootstrap host error/state/integrity contracts; positive guest boot belongs to real Darling acceptance."""
from __future__ import annotations
import argparse

import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
west = types.ModuleType("west")
commands = types.ModuleType("west.commands")


class WestCommand:
    def __init__(self, name, _help, description, **_kwargs):
        self.name = name
        self.description = description
        self.errors = []

    def die(self, message, exit_code=1):
        self.errors.append(message)
        raise SystemExit(exit_code)

    def inf(self, _message):
        pass

    def err(self, message):
        self.errors.append(message)


commands.WestCommand = WestCommand
sys.modules.setdefault("west", west)
sys.modules.setdefault("west.commands", commands)

from darling_bootstrap import DarlingBootstrap
from deploy_receipt import receipt_path, write_receipt


def snapshot(root: Path) -> dict[str, tuple]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (stat.S_IMODE(path.stat().st_mode), path.read_bytes())
        for path in root.rglob("*") if path.is_file()
    }


def package_layout(root: Path) -> Path:
    """Ordinary host files for pre-launch rejection tests, NOT a guest runtime.

    Actual host executables make the executable/layout prerequisite real. No
    fixture ever echoes a guest marker, emulates shell/shutdown, or asserts argv
    forwarding. Every invocation below must stop in argument/installation checks.
    """
    (root / "bin").mkdir(parents=True)
    shutil.copy2(Path(sys.executable).resolve(), root / "bin/darling")
    shutil.copy2(Path(sys.executable).resolve(), root / "bin/darlingserver")
    (root / "libexec/darling").mkdir(parents=True)
    payload = root / "libexec/darling/payload"
    payload.write_bytes(b"recorded host fixture bytes")
    return payload


class _ParserAdder:
    def add_parser(self, name, **kwargs):
        return argparse.ArgumentParser(prog=name, **kwargs)


def invoke(runtime: Path, prefix: Path, **overrides):
    command = DarlingBootstrap()
    command.topdir = str(ROOT.parent)
    command.manifest = types.SimpleNamespace(repo_abspath=str(ROOT))
    values = dict(
        runtime_root=runtime, prefix=prefix, build_dir=None, no_build=True,
        guest_toolchain=None, with_test_data=False, smoke_timeout_seconds=1,
        jobs=1, json=True,
    )
    values.update(overrides)
    output = io.StringIO()
    returncode = 0
    with contextlib.redirect_stdout(output):
        try:
            parser = command.do_add_parser(_ParserAdder())
            parsed = parser.parse_args(["--runtime-root", str(runtime), "--prefix", str(prefix)])
            for key, value in values.items():
                if key not in ("runtime_root", "prefix"):
                    setattr(parsed, key, value)
            command.do_run(parsed, [])
        except SystemExit as error:
            returncode = error.code
    text = output.getvalue().strip()
    return returncode, json.loads(text) if text else None, command.errors


def rejected_installation(runtime: Path, prefix: Path, **overrides):
    before = snapshot(runtime)
    returncode, result, _ = invoke(runtime, prefix, **overrides)
    assert returncode != 0 and result is not None, result
    assert result["status"] == "FAIL" and result["phase"] == "installation", result
    assert result["smoke"] == "NOT_RUN" and result["build_performed"] is False, result
    assert result["installed"] is False and result["cleanup"] is None, result
    assert not prefix.exists(), "installation refusal entered guest state"
    assert snapshot(runtime) == before, "bootstrap repaired or overwrote a refused installation"
    return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="west-bootstrap-consumer-contract-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"

        # Real argument/state failures happen before publication or guest ownership.
        for prefix, overrides in (
            (Path("relative guest"), {}),
            (str(root) + "/./guest dot", {}),
            (str(root) + "/parent/../guest dotdot", {}),
            (runtime, {}),
            (runtime / "overlapping guest", {}),
            (root / "guest timeout", {"smoke_timeout_seconds": 0}),
        ):
            returncode, result, errors = invoke(runtime, prefix, **overrides)
            assert returncode != 0 and errors and result is None
            assert not runtime.exists()

        rejected_installation(runtime, root / "guest missing build")
        unconfigured = root / "unconfigured build"
        unconfigured.mkdir()
        rejected_installation(runtime, root / "guest unconfigured", build_dir=unconfigured)
        assert not runtime.exists()

        partial = root / "partial installation"
        partial.mkdir()
        (partial / "foreign-file").write_bytes(b"retain partial installation")
        rejected_installation(partial, root / "guest partial")

        # A published image is not a build destination, even when --build-dir is supplied.
        published = root / "published host layout"
        payload = package_layout(published)
        rejected_installation(
            published, root / "guest attempted rebuild", build_dir=unconfigured, no_build=False,
        )
        assert not (unconfigured / "CMakeCache.txt").exists()

        # Optional package state cannot be fabricated by reusing an immutable image.
        rejected_installation(published, root / "guest missing data", with_test_data=True)
        assert not receipt_path(published).exists(), "ordinary package acquired fake workspace provenance"

        receipt_path(published).write_text(json.dumps({"schema_version": 1}))
        rejected_installation(published, root / "guest legacy receipt")
        write_receipt(published, {"schema_version": 2, "runtime_root": str(root / "other image")})
        rejected_installation(published, root / "guest copied receipt")

        # Corruption is measured from live fixture bytes, not accepted digest pins.
        metadata = payload.stat()
        recorded = {
            "schema_version": 2, "runtime_root": str(published),
            "artifacts": [{
                "path": payload.relative_to(published).as_posix(), "kind": "file",
                "mode": stat.S_IMODE(metadata.st_mode), "size": metadata.st_size,
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
            }],
        }
        write_receipt(published, recorded)
        payload.write_bytes(b"changed installed host fixture bytes")
        rejected_installation(published, root / "guest corrupt image")

    print("PASS bootstrap consumer error/state/integrity contract (no guest execution)")


if __name__ == "__main__":
    main()

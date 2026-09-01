#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from west_commands.owned_scratch import (  # noqa: E402
    OwnedScratchRoot,
    ScratchSafetyError,
    discard_exact,
    garbage_collect,
)


def run(command: list[str], *, env: dict[str, str], expect: int = 0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    if completed.returncode != expect:
        raise AssertionError(
            f"command rc={completed.returncode}, expected={expect}: {command}\n"
            f"stdout={completed.stdout[-2000:]}\nstderr={completed.stderr[-2000:]}"
        )
    return completed


def make_root(namespace: Path, name: str) -> tuple[Path, tuple[int, int]]:
    owner = OwnedScratchRoot.create(namespace=namespace, kind="contract", prefix=f"{name}-")
    sentinel = owner.path / "sentinel"
    sentinel.write_bytes(name.encode())
    identity = (sentinel.stat().st_dev, sentinel.stat().st_ino)
    root = owner.path
    owner.close()
    return root, identity


def unchanged(root: Path, identity: tuple[int, int], payload: bytes) -> None:
    sentinel = root / "sentinel"
    assert sentinel.read_bytes() == payload
    assert (sentinel.stat().st_dev, sentinel.stat().st_ino) == identity


def expect_retain(namespace: Path, helper: Path, label: str) -> None:
    root, identity = make_root(namespace, label)
    previous = os.environ.get("DARLING_SCRATCH_CENSUS_HELPER")
    os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(helper)
    try:
        try:
            discard_exact(namespace, root)
        except ScratchSafetyError:
            pass
        else:
            raise AssertionError(f"hostile helper accepted: {label}")
    finally:
        if previous is None:
            os.environ.pop("DARLING_SCRATCH_CENSUS_HELPER", None)
        else:
            os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = previous
    unchanged(root, identity, label.encode())
    shutil.rmtree(root)


def main() -> None:
    real_helper_value = os.environ.get("SCRATCH_CENSUS_TEST_HELPER")
    if not real_helper_value:
        raise AssertionError("contract requires SCRATCH_CENSUS_TEST_HELPER")
    real_helper = Path(real_helper_value).resolve(strict=True)
    mise = shutil.which("mise")
    if not mise:
        raise AssertionError("mise unavailable")

    with tempfile.TemporaryDirectory(prefix="scratch-census-provision-") as raw:
        base = Path(raw)
        fixture = base / "workspace"
        fixture.mkdir()
        (fixture / "scripts").mkdir()
        (fixture / "lifecycle/operation-boundary").mkdir(parents=True)
        shutil.copy2(REPO / "mise.toml", fixture / "mise.toml")
        shutil.copy2(
            REPO / "scripts/provision-scratch-census.sh",
            fixture / "scripts/provision-scratch-census.sh",
        )
        shutil.copy2(
            REPO / "scripts/run-cargo-with-parent-death.py",
            fixture / "scripts/run-cargo-with-parent-death.py",
        )
        (fixture / "lifecycle/operation-boundary/Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.0.0'\n")

        fake_bin = base / "fake-bin"
        fake_bin.mkdir()
        cargo_log = base / "cargo.log"
        fake_cargo = fake_bin / "cargo"
        fake_cargo.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_CARGO_LOG"
case "${FAKE_CARGO_MODE:-success}" in
  fail) exit 42 ;;
  interrupt) kill -INT "$PPID"; exit 130 ;;
  linger)
    printf '%s\\n' "$$" > "$FAKE_CARGO_PID_FILE"
    sleep 300 &
    printf '%s\\n' "$!" > "$FAKE_CARGO_CHILD_PID_FILE"
    wait
    ;;
  boundary)
    printf '%s\\n' "$$" > "$FAKE_CARGO_PID_FILE"
    sleep 300 &
    printf '%s\\n' "$!" > "$FAKE_CARGO_CHILD_PID_FILE"
    printf '%s\\n' "$PPID" > "$FAKE_CARGO_SUPERVISOR_PID_FILE"
    setup=$(awk '{print $4}' "/proc/$PPID/stat")
    printf '%s\\n' "$setup" > "$FAKE_CARGO_SETUP_PID_FILE"
    kill -KILL "$setup"
    wait
    ;;
esac
target=""
while (($#)); do
  if [[ "$1" == --target-dir ]]; then target=$2; shift 2; else shift; fi
done
mkdir -p "$target/release"
cp "$REAL_SCRATCH_HELPER" "$target/release/darling-scratch-census"
chmod 0755 "$target/release/darling-scratch-census"
"""
        )
        fake_cargo.chmod(0o755)

        home = base / "home"
        xdg_data = base / "xdg-data"
        xdg_config = base / "xdg-config"
        temporary = base / "tmp"
        home.mkdir()
        xdg_data.mkdir()
        xdg_config.mkdir()
        temporary.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "XDG_DATA_HOME": str(xdg_data),
                "XDG_CONFIG_HOME": str(xdg_config),
                "TMPDIR": str(temporary),
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FAKE_CARGO_LOG": str(cargo_log),
                "REAL_SCRATCH_HELPER": str(real_helper),
                "FAKE_CARGO_PID_FILE": str(base / "cargo.pid"),
                "FAKE_CARGO_CHILD_PID_FILE": str(base / "cargo-child.pid"),
                "FAKE_CARGO_SUPERVISOR_PID_FILE": str(base / "cargo-supervisor.pid"),
                "FAKE_CARGO_SETUP_PID_FILE": str(base / "cargo-setup.pid"),
            }
        )
        env.pop("DARLING_SCRATCH_CENSUS_HELPER", None)
        run([mise, "-C", str(fixture), "trust"], env=env)

        # Cargo is optional and its absence is detected before .darling-tools exists.
        no_cargo_path = f"/usr/bin:/bin"
        missing_env = env | {"PATH": no_cargo_path}
        run(
            [mise, "-C", str(fixture), "run", "setup-scratch-census"],
            env=missing_env,
            expect=127,
        )
        assert not (fixture / ".darling-tools").exists()

        namespace = base / "scratch"
        root, identity = make_root(namespace, "before-setup")
        old_path = os.environ.get("PATH", "")
        old_helper = os.environ.pop("DARLING_SCRATCH_CENSUS_HELPER", None)
        os.environ["PATH"] = str(fake_bin)
        try:
            result = garbage_collect(namespace=namespace, ttl_seconds=0, keep=0)
            assert not result.removed and result.retained
        finally:
            os.environ["PATH"] = old_path
            if old_helper is not None:
                os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = old_helper
        unchanged(root, identity, b"before-setup")
        shutil.rmtree(root)

        installed = fixture / ".darling-tools/darling-scratch-census"
        run(
            [mise, "-C", str(fixture), "run", "setup-scratch-census"],
            env=env | {"FAKE_CARGO_MODE": "fail"},
            expect=42,
        )
        assert not installed.exists() and not installed.is_symlink()
        run([mise, "-C", str(fixture), "run", "setup-scratch-census"], env=env)
        info = installed.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
        assert info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0 and os.access(installed, os.X_OK)
        first_payload = installed.read_bytes()

        # A failed or interrupted rebuild never reaches the atomic publication.
        for mode, expected in (("fail", 42), ("interrupt", 130)):
            failed_env = env | {"FAKE_CARGO_MODE": mode}
            run(
                [mise, "-C", str(fixture), "run", "setup-scratch-census"],
                env=failed_env,
                expect=expected,
            )
            assert installed.read_bytes() == first_payload

        run([mise, "-C", str(fixture), "run", "setup-scratch-census"], env=env)
        assert installed.read_bytes() == first_payload

        # A hard setup-parent death kills the Cargo process group. The next
        # serialized setup collects only the fixed private stale stage.
        linger = subprocess.Popen(
            [mise, "-C", str(fixture), "run", "setup-scratch-census"],
            env=env | {"FAKE_CARGO_MODE": "linger"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cargo_pid_file = base / "cargo.pid"
        cargo_child_file = base / "cargo-child.pid"
        deadline = time.monotonic() + 10
        while not (cargo_pid_file.exists() and cargo_child_file.exists()):
            if time.monotonic() >= deadline:
                linger.kill()
                raise AssertionError("lingering Cargo fixture did not start")
            time.sleep(0.01)
        cargo_pid = int(cargo_pid_file.read_text())
        cargo_child = int(cargo_child_file.read_text())
        supervisor_pid = int(Path(f"/proc/{cargo_pid}/stat").read_text().split()[3])
        setup_pid = int(Path(f"/proc/{supervisor_pid}/stat").read_text().split()[3])
        os.kill(setup_pid, signal.SIGKILL)
        linger.communicate(timeout=10)
        for pid in (cargo_pid, cargo_child, supervisor_pid, setup_pid):
            deadline = time.monotonic() + 10
            while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not Path(f"/proc/{pid}").exists(), f"provisioning child survived: {pid}"
        assert (fixture / ".darling-tools/.scratch-census-stage").is_dir()
        assert installed.read_bytes() == first_payload
        run([mise, "-C", str(fixture), "run", "setup-scratch-census"], env=env)
        assert not (fixture / ".darling-tools/.scratch-census-stage").exists()

        # Exercise the exact Popen-to-handler boundary repeatedly. The fake
        # Cargo kills its setup parent immediately after spawn, before it does
        # any build work; pending SIGTERM must reach the installed handler.
        boundary_files = [
            base / "cargo.pid",
            base / "cargo-child.pid",
            base / "cargo-supervisor.pid",
            base / "cargo-setup.pid",
        ]
        for _iteration in range(8):
            for path in boundary_files:
                path.unlink(missing_ok=True)
            boundary = subprocess.Popen(
                [mise, "-C", str(fixture), "run", "setup-scratch-census"],
                env=env | {"FAKE_CARGO_MODE": "boundary"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 10
            while not all(path.exists() for path in boundary_files):
                if boundary.poll() is not None or time.monotonic() >= deadline:
                    boundary.kill()
                    raise AssertionError("spawn-boundary fixture did not publish identities")
                time.sleep(0.005)
            identities = [int(path.read_text()) for path in boundary_files]
            boundary.communicate(timeout=10)
            for pid in identities:
                deadline = time.monotonic() + 10
                while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                assert not Path(f"/proc/{pid}").exists(), f"spawn-boundary process survived: {pid}"
            assert installed.read_bytes() == first_payload
        run([mise, "-C", str(fixture), "run", "setup-scratch-census"], env=env)
        assert not (fixture / ".darling-tools/.scratch-census-stage").exists()

        # Ordinary mise execution resolves the stable install and never invokes Cargo.
        cargo_calls = cargo_log.read_text().splitlines()
        resolved = run(
            [mise, "-C", str(fixture), "exec", "--", "sh", "-c", "printf '%s' \"$DARLING_SCRATCH_CENSUS_HELPER\""],
            env=env,
        ).stdout.strip()
        assert Path(resolved).samefile(installed)
        os.environ["DARLING_SCRATCH_CENSUS_HELPER"] = str(installed)

        shadow_bin = fixture / ".darling-tools/bin"
        shadow_bin.mkdir()
        for name in ("west", "uv"):
            planted = shadow_bin / name
            planted.write_text("#!/bin/sh\nexit 99\n")
            planted.chmod(0o755)
            selected = run(
                [mise, "-C", str(fixture), "exec", "--", "sh", "-c", f"command -v {name}"],
                env=env,
            ).stdout.strip()
            assert selected and not Path(selected).samefile(planted)

        driver = base / "gc-driver.py"
        driver.write_text(
            """import os, sys
from pathlib import Path
sys.path.insert(0, os.environ['SOURCE_REPO'])
from west_commands.owned_scratch import discard_exact, garbage_collect
namespace = Path(os.environ['SCRATCH_NAMESPACE'])
root = Path(os.environ['SCRATCH_ROOT'])
if '--dry-run' in sys.argv:
    result = garbage_collect(namespace=namespace, ttl_seconds=0, keep=0, dry_run=True)
    assert root in result.removed and root.exists()
else:
    discard_exact(namespace, root)
"""
        )
        west_env = env | {
            "SOURCE_REPO": str(REPO),
            "SCRATCH_NAMESPACE": str(namespace),
        }
        dry_root, _ = make_root(namespace, "dry-run")
        west_env["SCRATCH_ROOT"] = str(dry_root)
        run([mise, "-C", str(fixture), "exec", "--", "python3", "-B", str(driver), "--dry-run"], env=west_env)
        assert dry_root.exists()
        discard_exact(namespace, dry_root)
        clean_root, _ = make_root(namespace, "cleanup")
        west_env["SCRATCH_ROOT"] = str(clean_root)
        run([mise, "-C", str(fixture), "exec", "--", "python3", "-B", str(driver)], env=west_env)
        assert not clean_root.exists()
        assert cargo_log.read_text().splitlines() == cargo_calls

        hostile = base / "hostile"
        hostile.mkdir()
        target = hostile / "target"
        target.write_text("x")
        target.chmod(0o755)

        def reset_installed() -> None:
            if installed.exists() or installed.is_symlink():
                installed.unlink()
            installed.write_bytes(first_payload)
            installed.chmod(0o755)

        installed.unlink()
        installed.symlink_to(target)
        expect_retain(namespace, installed, "symlink")
        calls_before = cargo_log.read_text().splitlines()
        run([mise, "-C", str(fixture), "run", "setup-scratch-census"], env=env, expect=1)
        assert installed.is_symlink() and cargo_log.read_text().splitlines() == calls_before
        installed.unlink()
        os.mkfifo(installed)
        expect_retain(namespace, installed, "fifo")
        run([mise, "-C", str(fixture), "run", "setup-scratch-census"], env=env, expect=1)
        assert stat.S_ISFIFO(installed.lstat().st_mode)
        installed.unlink()
        installed.write_bytes(first_payload)
        installed.chmod(0o777)
        expect_retain(namespace, installed, "writable")
        run([mise, "-C", str(fixture), "run", "setup-scratch-census"], env=env, expect=1)
        assert stat.S_IMODE(installed.stat().st_mode) == 0o777
        installed.unlink()
        installed.write_text("#!/bin/sh\nprintf '%s\\n' '{\"protocol_version\":999,\"outcomes\":[]}'\n")
        installed.chmod(0o755)
        expect_retain(namespace, installed, "incompatible")
        reset_installed()

        assert not list(Path("/tmp").glob("darling-scratch-census-build.*"))
        assert not any(namespace.iterdir())
    print("SCRATCH_CENSUS_PROVISION_CONTRACT_VALID cases=8")


if __name__ == "__main__":
    main()

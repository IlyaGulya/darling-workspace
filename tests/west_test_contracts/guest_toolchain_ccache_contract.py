"""Exercise fingerprinted compiler-cache preparation and observable statistics."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def assert_prepare_probe_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="west-ccache-prepare-contract-") as raw:
        root = Path(raw)
        env = {
            **os.environ,
            "GITHUB_ENV": str(root / "github-env"),
            "GITHUB_OUTPUT": str(root / "github-output"),
            "GITHUB_SHA": "contract",
            "RUNNER_ARCH": "X64",
            "RUNNER_OS": "Linux",
            "RUNNER_TEMP": str(root),
            "HOME": str(root / "home"),
        }
        result = subprocess.run(
            [str(ROOT / "ci/guest-toolchain-ccache.sh"), "prepare", "cold"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        output = (root / "github-output").read_text()
        values = dict(line.split("=", 1) for line in output.splitlines())
        for name in (
            "clang_path",
            "clangxx_path",
            "clang_resolved_path",
            "clangxx_resolved_path",
        ):
            assert Path(values[name]).is_absolute(), name
        assert values["clang_resolved_path"] == str(
            Path(values["clang_path"]).resolve(strict=True)
        )
        assert values["clangxx_resolved_path"] == str(
            Path(values["clangxx_path"]).resolve(strict=True)
        )
        assert values["clang_fingerprint"] == hashlib.sha256(
            Path(values["clang_resolved_path"]).read_bytes()
        ).hexdigest()
        assert values["clangxx_fingerprint"] == hashlib.sha256(
            Path(values["clangxx_resolved_path"]).read_bytes()
        ).hexdigest()
        assert len(values["compiler_fingerprint"]) == 64
        assert set(values["compiler_fingerprint"]) <= set("0123456789abcdef")
        env_values = dict(
            line.split("=", 1)
            for line in (root / "github-env").read_text().splitlines()
        )
        assert env_values["CCACHE_COMPILER_FINGERPRINT"] == values[
            "compiler_fingerprint"
        ]
        assert env_values["CCACHE_COMPILERCHECK"] == (
            f"string:{values['compiler_fingerprint']}"
        )

        if values["clang_resolved_path"] == values["clangxx_resolved_path"]:
            assert values["clang_path"] != values["clangxx_path"]

        assert Path(values['cache_dir']).is_relative_to(root)
        assert env_values['CCACHE_DIR'] == values['cache_dir']
        assert values['primary_key'].startswith(values['restore_prefix'])
        cold_cache = Path(values['cache_dir'])
        cold_key = values['primary_key']
        for path in (root / 'github-env', root / 'github-output'):
            path.unlink()
        warm = subprocess.run(
            [str(ROOT / 'ci/guest-toolchain-ccache.sh'), 'prepare', 'warm'],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )
        assert warm.returncode == 0, warm.stderr
        warm_values = dict(line.split('=', 1) for line in (root / 'github-output').read_text().splitlines())
        warm_environment = dict(line.split('=', 1) for line in (root / 'github-env').read_text().splitlines())
        assert Path(warm_values['cache_dir']).is_relative_to(root / 'home')
        assert Path(warm_values['cache_dir']) != cold_cache
        assert warm_values['primary_key'] == cold_key, 'storage location must not change content compatibility'
        assert warm_values['compiler_fingerprint'] == values['compiler_fingerprint']
        statistics = root / 'statistics'
        result = subprocess.run(
            [str(ROOT / 'ci/guest-toolchain-ccache.sh'), 'stats', str(statistics)],
            cwd=ROOT, env={**env, **warm_environment}, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (statistics / 'ccache-stats.txt').is_file()
        assert (statistics / 'ccache-stats-human.txt').is_file()
        recorded = dict(line.split('=', 1) for line in (statistics / 'ccache-environment.txt').read_text().splitlines())
        assert recorded['CCACHE_DIR'] == warm_values['cache_dir']
        assert recorded['CCACHE_COMPILERCHECK'] == warm_environment['CCACHE_COMPILERCHECK']


assert_prepare_probe_contract()
print('PASS guest-toolchain-ccache-contract')

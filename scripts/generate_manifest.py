#!/usr/bin/env python3
"""Generate a repo manifest from a Darling checkout."""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


OUTPUT_LIMIT = 4 * 1024 * 1024
COMMAND_TIMEOUT = 120


@dataclass(frozen=True)
class BoundedResult:
    returncode: int
    stdout: str
    stderr: str
    overflow: bool


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run_bounded(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = COMMAND_TIMEOUT,
) -> BoundedResult:
    effective_env = os.environ.copy() if env is None else env.copy()
    effective_env["GIT_OPTIONAL_LOCKS"] = "0"
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=effective_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    overflow = False
    deadline = time.monotonic() + timeout
    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                raise subprocess.TimeoutExpired(command, timeout)
            events = streams.select(min(remaining, 0.1))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in streams.get_map().values()]
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    streams.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                available = max(0, OUTPUT_LIMIT - total)
                if available:
                    captured[key.data].extend(chunk[:available])
                    total += min(len(chunk), available)
                if len(chunk) > available:
                    overflow = True
                    _kill_process_group(process)
                    break
            if overflow:
                break
        if not overflow:
            remaining = max(0.001, deadline - time.monotonic())
            process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            _kill_process_group(process)
        raise
    finally:
        streams.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    return BoundedResult(
        process.returncode,
        captured["stdout"].decode("utf-8", errors="replace"),
        captured["stderr"].decode("utf-8", errors="replace"),
        overflow,
    )


def git(repo: Path, *args: str, required: bool = True) -> str:
    try:
        result = run_bounded(["git", "-C", str(repo), *args])
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"git timed out in {repo}: {' '.join(args)}") from error
    if result.overflow:
        raise SystemExit(f"git output limit exceeded in {repo}: {' '.join(args)}")
    if required and result.returncode:
        raise SystemExit(f"git failed in {repo}: {' '.join(args)}")
    return result.stdout.strip() if result.returncode == 0 else ""


def public_base(repo: Path, head: str, *, remote: str = "origin") -> str | None:
    if not git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", required=False):
        return None
    for ref in (f"refs/remotes/{remote}/main", f"refs/remotes/{remote}/master"):
        base = git(repo, "rev-parse", "--verify", ref, required=False)
        if not base or base == head:
            continue
        try:
            result = run_bounded(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, head]
            )
        except subprocess.TimeoutExpired as error:
            raise SystemExit(f"git merge-base timed out in {repo}") from error
        if result.overflow:
            raise SystemExit(f"git merge-base output limit exceeded in {repo}")
        if result.returncode == 0:
            return base
    return None


def github_name(url: str) -> str:
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    else:
        path = urlparse(url).path.lstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name.removesuffix(".git")


def submodule_url(source: Path, relative: str) -> str:
    target = Path(relative)
    for parent in [target.parent, *target.parents]:
        base = source / parent
        modules = base / ".gitmodules"
        if not modules.is_file():
            continue
        local_path = str(target.relative_to(parent))
        try:
            result = run_bounded(
                ["git", "config", "-f", str(modules), "--get-regexp", r"^submodule\..*\.path$"]
            )
        except subprocess.TimeoutExpired as error:
            raise SystemExit(f"git config timed out for {modules}") from error
        if result.overflow:
            raise SystemExit(f"git config output limit exceeded for {modules}")
        for line in result.stdout.splitlines():
            key, value = line.split(maxsplit=1)
            if value == local_path:
                url_key = key.removesuffix(".path") + ".url"
                try:
                    url_result = run_bounded(
                        ["git", "config", "-f", str(modules), "--get", url_key]
                    )
                except subprocess.TimeoutExpired as error:
                    raise SystemExit(f"git config timed out for {modules}") from error
                if url_result.overflow or url_result.returncode:
                    raise SystemExit(f"git config failed for {modules}")
                return url_result.stdout.strip()
    raise SystemExit(f"cannot find submodule URL for {relative}")


def projects(source: Path) -> list[tuple[str, str, str, bool]]:
    output = git(source, "submodule", "status", "--recursive")
    result = [
        (
            ".",
            git(source, "rev-parse", "HEAD"),
            git(source, "remote", "get-url", "origin"),
            True,
        )
    ]
    for line in output.splitlines():
        marker = line[0]
        fields = line[1:].split()
        if len(fields) >= 2:
            sha, relative = fields[:2]
            repo = source / relative
            if marker == "-":
                origin = submodule_url(source, relative)
                initialized = False
            else:
                sha = git(repo, "rev-parse", "HEAD")
                origin = git(repo, "remote", "get-url", "origin")
                initialized = True
            result.append((relative, sha, origin, initialized))
    return result


def render_manifest(records: list[tuple[str, str, str]]) -> str:
    """Render an ordered, already-frozen repository plan."""
    manifest = ET.Element("manifest")
    ET.SubElement(
        manifest,
        "remote",
        name="darling",
        fetch="https://github.com/darlinghq/",
    )
    ET.SubElement(
        manifest,
        "default",
        remote="darling",
        **{"sync-j": "16", "sync-tags": "false"},
    )
    seen: set[tuple[str, str]] = set()
    for relative, revision, origin in records:
        name = github_name(origin)
        path = "darling" if relative == "." else f"darling/{relative}"
        key = (name, path)
        if key in seen:
            continue
        seen.add(key)
        ET.SubElement(
            manifest,
            "project",
            {"name": name, "path": path, "revision": revision},
        )
    ET.indent(manifest, space="  ")
    return ET.tostring(manifest, encoding="unicode", xml_declaration=True) + "\n"


def write_manifest(records: list[tuple[str, str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_manifest(records), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    records = []
    for relative, revision, origin, initialized in projects(source):
        repo = source if relative == "." else source / relative
        if args.base and initialized:
            revision = public_base(repo, revision) or revision
        records.append((relative, revision, origin))
    write_manifest(records, args.output)


if __name__ == "__main__":
    main()

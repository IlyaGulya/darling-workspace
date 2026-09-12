#!/usr/bin/env python3
"""Disposable real-client HTTPS experiment; West owns prefix lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

workspace = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(workspace / "west_commands"))
from test_guest_execution import run_guest_shell_argv

prefix = Path(os.environ["DPREFIX"])
launcher = os.environ["DARLING_LAUNCHER"]
work = Path(tempfile.mkdtemp(prefix="github-https-", dir=prefix / "private/var/tmp"))
guest_work = "/" + work.relative_to(prefix).as_posix()
report = {"prefix": str(prefix), "work": str(work), "identity": [], "requests": []}
failed = False

def run(name, argv, timeout=80):
    result = run_guest_shell_argv(launcher, prefix, argv, cwd=workspace,
                                env=dict(os.environ), timeout_seconds=timeout,
                                capture_output=True)
    stdout = result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout
    stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr
    (work / (name + ".stdout")).write_text(stdout)
    (work / (name + ".stderr")).write_text(stderr)
    print(f"CLIENT_RESULT {name} rc={result.returncode} timeout={result.timed_out}", flush=True)
    print(stdout, end="", flush=True)
    print(stderr, end="", file=sys.stderr, flush=True)
    return result, stdout, stderr

for name, argv in (
    ("curl-version", ["/usr/bin/curl", "--version"]),
    ("wget-version", ["/usr/local/bin/wget", "--version"]),
    ("openssl-version", ["/usr/local/opt/openssl@3/bin/openssl", "version"]),
    ("curl-libraries", ["/Library/Developer/CommandLineTools/usr/bin/otool", "-L", "/usr/bin/curl"]),
    ("wget-libraries", ["/Library/Developer/CommandLineTools/usr/bin/otool", "-L", "/usr/local/bin/wget"]),
):
    result, stdout, stderr = run(name, argv, 45)
    report["identity"].append({"name": name, "rc": result.returncode, "stdout": stdout, "stderr": stderr})
    failed |= result.returncode != 0 or result.timed_out

endpoints = (
    ("github", "https://github.com/", 200),
    ("raw", "https://raw.githubusercontent.com/octocat/Hello-World/master/README", 200),
    ("registry", "https://ghcr.io/v2/", 401),
    ("archive", "https://github.com/octocat/Hello-World/archive/refs/heads/master.tar.gz", 200),
)
for client in ("curl", "wget"):
    for endpoint, url, expected in endpoints:
        name = client + "-" + endpoint
        output = guest_work + "/" + name + ".body"
        if client == "curl":
            argv = ["/usr/bin/curl", "--proto", "=https", "--proto-redir", "=https",
                    "--connect-timeout", "15", "--max-time", "60", "--retry", "0",
                    "--location", "--verbose", "--output", output,
                    "--write-out", "\nHTTPS_RESULT %{http_code} %{ssl_verify_result} %{url_effective}\n", url]
        else:
            argv = ["/usr/local/bin/wget", "--timeout=30", "--tries=1", "--max-redirect=10",
                    "--https-only", "--server-response", "-O", output, url]
        print(f"REQUEST_BEGIN client={client} endpoint={endpoint} expected_http={expected} url={url}", flush=True)
        result, stdout, stderr = run(name, argv)
        body_path = work / (name + ".body")
        body = body_path.read_bytes() if body_path.is_file() else b""
        if client == "curl":
            matches = re.findall(r"^HTTPS_RESULT (\d+) (\d+) (\S+)$", stdout, re.MULTILINE)
            status, verify, final_url = matches[-1] if matches else ("0", "-1", "")
            ok = result.returncode == 0 and verify == "0" and int(status) == expected
        else:
            matches = re.findall(r"^\s*HTTP/\S+ (\d+)", stderr, re.MULTILINE)
            status = matches[-1] if matches else "0"
            final_url = None
            ok = int(status) == expected and result.returncode == (6 if expected == 401 else 0)
        if expected == 200:
            ok &= bool(body)
        if endpoint == "archive":
            ok &= body.startswith(b"\x1f\x8b") and "codeload.github.com" in (stdout + stderr)
        ok &= not result.timed_out
        record = {"client": client, "endpoint": endpoint, "url": url, "expected_http": expected,
                  "http_status": int(status), "rc": result.returncode, "timed_out": result.timed_out,
                  "effective_url": final_url, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                  "pass": bool(ok)}
        report["requests"].append(record)
        print("HTTPS_VERDICT " + json.dumps(record, sort_keys=True), flush=True)
        failed |= not ok
report["pass"] = not failed
(work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
print("HTTPS_EVIDENCE=" + str(work), flush=True)
print("GITHUB_HTTPS_MATRIX_" + ("FAIL" if failed else "OK"), flush=True)
raise SystemExit(1 if failed else 0)

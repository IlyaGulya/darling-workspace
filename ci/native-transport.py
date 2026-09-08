#!/usr/bin/env python3
"""Run an installed CTest bundle locally or over an owned SSH workspace."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET


class InfrastructureError(Exception):
    pass


def dump(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def duration(name, default):
    try:
        value = float(os.environ.get(name, default))
    except ValueError as error:
        raise InfrastructureError(f"{name} must be positive seconds") from error
    if not 0 < value < 86400:
        raise InfrastructureError(f"{name} must be between 0 and 86400 seconds")
    return value


def results_directory(bundle):
    configured = os.environ.get("DARLING_NATIVE_RESULTS_DIR")
    destination = Path(configured).expanduser().resolve() if configured else Path(tempfile.gettempdir()).resolve()
    if destination.is_relative_to(bundle):
        raise InfrastructureError("results directory must be outside the immutable bundle")
    if configured:
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        return destination
    return Path(tempfile.mkdtemp(prefix="darling-native-results-", dir=destination))


def run(command, timeout, *, output=None, input_file=None, cwd=None, env=None,
        cancel=None, stderr=subprocess.STDOUT):
    """Bound one process group; never signal processes outside that group."""
    with tempfile.TemporaryFile() as capture:
        destination = output if output is not None else capture
        process = subprocess.Popen(command, stdin=input_file or subprocess.DEVNULL,
                                   stdout=destination, stderr=stderr,
                                   cwd=cwd, env=env, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline or (cancel and cancel.exists()):
                    raise InfrastructureError(f"command timed out or cancelled: {command[0]}")
                time.sleep(0.1)
        finally:
            # Descendants of CTest/ssh are ours, even if their parent exited first.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if output is None:
            capture.seek(0)
            return process.returncode, capture.read()
        return process.returncode, b""


def checked(command, timeout=15, **kwargs):
    code, content = run(command, timeout, **kwargs)
    if code:
        raise InfrastructureError(f"command exited {code}: {command[0]}: "
                                  + content.decode(errors="replace").strip())
    return content.decode().strip()


def bundle_identity(bundle):
    if not bundle.is_dir():
        raise InfrastructureError(f"bundle is not a directory: {bundle}")
    for required in ("CTestTestfile.cmake", "native-build.json", "testcase"):
        if not (bundle / required).exists():
            raise InfrastructureError(f"bundle is missing {required}")
    digest = hashlib.sha256()
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            if os.path.isabs(os.readlink(path)):
                raise InfrastructureError(f"bundle symlink is not relocatable: {relative}")
            try:
                path.resolve(strict=True).relative_to(bundle)
            except (ValueError, FileNotFoundError) as error:
                raise InfrastructureError(f"bundle symlink escapes or is dangling: {relative}") from error
            payload = os.readlink(path).encode()
            kind = b"link"
        elif path.is_file():
            kind = b"file-executable" if path.stat().st_mode & 0o111 else b"file"
            payload = None
        elif path.is_dir():
            continue
        else:
            raise InfrastructureError(f"unsupported bundle asset: {relative}")
        digest.update(kind + b"\0" + relative.encode() + b"\0")
        size = len(payload) if payload is not None else path.stat().st_size
        digest.update(str(size).encode() + b"\0")
        if payload is not None:
            digest.update(payload)
        else:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    identity = json.loads((bundle / "native-build.json").read_text())
    return {"bundle_sha256": digest.hexdigest(), "native_build": identity,
            "digest_format": "sha256:sorted-relative-path,type,size,content:nul-delimited:v1"}


def ctest_arguments(arguments):
    # Selection, scheduling and test options are passed unchanged. Modes that
    # replace execution or redirect its inputs/evidence cannot be evidence runs.
    forbidden = ("--test-dir", "--output-junit", "--output-log", "--show-only",
                 "--script", "--build-and-test", "--test-action", "--preset",
                 "--list-presets", "--help", "--version", "--dashboard",
                 "--interactive-debug-mode")
    for argument in arguments:
        if argument == "--" or argument.startswith(forbidden) or (
                argument.startswith("-") and not argument.startswith("--")
                and len(argument) > 1 and argument[1] in "NOSDMT"):
            raise InfrastructureError(f"CTest option replaces managed execution: {argument}")


def native_assets(bundle, tests):
    descriptions = {}
    magics = {bytes.fromhex(value) for value in (
        "feedface", "cefaedfe", "feedfacf", "cffaedfe",
        "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}
    assets = set()
    for path in (bundle / "testcase").rglob("*"):
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic not in magics:
            raise InfrastructureError(f"installed testcase is not Mach-O: {path}")
        description = checked(["/usr/bin/file", "-b", str(path)])
        if not description.startswith("Mach-O"):
            raise InfrastructureError(f"invalid Mach-O testcase: {path}: {description}")
        descriptions[path.relative_to(bundle).as_posix()] = description
        assets.add(path.resolve())
    if not assets:
        raise InfrastructureError("bundle contains no executable Mach-O testcases")
    for test in tests:
        if not any(Path(arg).is_absolute() and Path(arg).resolve() in assets
                   for arg in test.get("command", [])):
            raise InfrastructureError(f"CTest test has no installed Mach-O command asset: {test['name']}")
    return descriptions


def local(bundle, arguments, results, report):
    report.update(bundle_identity(bundle))
    dump(results / "native-build.json", report["native_build"])
    report["execution"] = {"system": platform.system(), "architecture": platform.machine()}
    if platform.system() != "Darwin":
        raise InfrastructureError("native evidence requires Darwin, not " + platform.system())
    report["execution"].update({"os_build": checked(["sw_vers", "-buildVersion"]),
                                "os_version": checked(["sw_vers", "-productVersion"]),
                                "kernel": platform.release()})
    rosetta_code, rosetta_output = run(["sysctl", "-in", "sysctl.proc_translated"], 10)
    translated = rosetta_output.decode().strip()
    if rosetta_code or translated not in ("", "0", "1"):
        raise InfrastructureError("cannot determine Rosetta process translation status")
    report["execution"]["rosetta"] = translated == "1"
    ctest = os.environ.get("DARLING_NATIVE_CTEST", "ctest")
    report["ctest_version"] = checked([ctest, "--version"])
    version = re.search(r"ctest version (\d+)\.(\d+)", report["ctest_version"])
    if not version or tuple(map(int, version.groups())) < (3, 21):
        raise InfrastructureError("CTest 3.21 or newer is required for JUnit evidence")
    ctest_arguments(arguments)
    work = results / "ctest"
    work.mkdir()
    filename = str(bundle / "CTestTestfile.cmake")
    delimiter = "="
    while "]" + delimiter + "]" in filename:
        delimiter += "="
    (work / "CTestTestfile.cmake").write_text(f"include([{delimiter}[{filename}]{delimiter}])\n")
    base = [ctest, "--test-dir", str(work)]
    cancel = results / ".cancel"
    timeout = duration("DARLING_NATIVE_TIMEOUT_SECONDS", 1800)
    code, discovery = run(base + arguments + ["--show-only=json-v1"], min(timeout, 60), cancel=cancel)
    (results / "discovery.json").write_bytes(discovery)
    if code:
        raise InfrastructureError(f"CTest discovery exited {code}")
    tests = json.loads(discovery)["tests"]
    selected = [test for test in tests if not any(
        prop["name"] == "DISABLED" and prop["value"]
        for prop in test.get("properties", []))]
    report["selected_tests"] = [test["name"] for test in selected]
    if not selected:
        raise InfrastructureError("CTest selection contains no enabled tests")
    report["macho_assets"] = native_assets(bundle, selected)
    command = base + arguments + ["--output-on-failure", "--no-tests=error",
                                  "--output-junit", str(results / "ctest-junit.xml")]
    report["ctest_arguments"] = arguments
    dump(results / "execution.json", report)
    environment = os.environ.copy()
    environment["DARLING_NATIVE_RESULTS_DIR"] = str(results)
    environment["DARLING_NATIVE_CTEST_ROOT"] = str(work)
    with (results / "ctest-output.log").open("wb") as output:
        code, _ = run(command, timeout, output=output, env=environment, cancel=cancel)
    report["ctest_returncode"] = code
    diagnostics = []
    for path in sorted((results / "infrastructure").glob("*.json")):
        diagnostic = json.loads(path.read_text())
        if (not isinstance(diagnostic, dict) or diagnostic.get("schema_version") != 1
                or diagnostic.get("kind") != "native_helper_infrastructure_error"
                or not isinstance(diagnostic.get("error"), str)):
            raise InfrastructureError(f"invalid native helper infrastructure diagnostic: {path.name}")
        diagnostics.append(diagnostic)
    report["infrastructure_diagnostics"] = diagnostics
    junit = results / "ctest-junit.xml"
    if not junit.is_file():
        raise InfrastructureError("CTest did not produce JUnit evidence")
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    active = [case for case in cases if case.find("skipped") is None]
    failures = [case for case in active if case.find("failure") is not None]
    errors = [case for case in active if case.find("error") is not None]
    if not set(report["selected_tests"]).issubset(
            {case.attrib.get("name") for case in cases}):
        raise InfrastructureError("CTest JUnit does not cover the enabled selection")
    case_results = []
    for case in cases:
        failure = case.find("failure")
        error = case.find("error")
        entry = {"name": case.attrib.get("name"),
                 "status": "skipped" if case.find("skipped") is not None else "passed"}
        problem = error if error is not None else failure
        if problem is not None:
            entry["status"] = "infrastructure_error" if error is not None else "failed"
            entry["ctest_failure"] = dict(problem.attrib)
            # These are CTest's own JUnit exception descriptions, not testcase
            # output or a parallel assertion/oracle implementation.
            description = " ".join(problem.attrib.values()).strip().lower()
            if "timeout" in description:
                entry["failure_kind"] = "timeout"
            elif any(kind in description for kind in (
                    "segmentation", "segfault", "illegal", "interrupt",
                    "numerical", "other fault", "sigabrt", "sigkill", "subprocess aborted",
                    "subprocess terminated", "subprocess killed")):
                entry["failure_kind"] = "signal"
            elif any(kind in description for kind in ("not run", "bad command", "failed to start")):
                entry["failure_kind"] = "launch_error"
            else:
                entry["failure_kind"] = "test_failure"
        case_results.append(entry)
    report["case_results"] = case_results
    if diagnostics:
        affected = {item.get("test_name") for item in diagnostics}
        for entry in case_results:
            if entry["name"] in affected:
                entry.update(status="infrastructure_error", failure_kind="helper_infrastructure")
        raise InfrastructureError("native verdict helper infrastructure failure: "
                                  + "; ".join(item["error"] for item in diagnostics))
    if errors or code not in (0, 8) or bool(code) != bool(failures):
        raise InfrastructureError(f"CTest execution failed outside semantic verdicts (exit {code})")
    report["status"] = "test_failed" if failures else ("passed" if active else "skipped")
    report["tests"] = {"executed": len(active), "failed": len(failures),
                       "passed": len(active) - len(failures),
                       "skipped": len(cases) - len(active)}
    return 1 if failures else 0


def owned_directory(path, token):
    root = Path(path)
    if not re.fullmatch(r"/tmp/darling-native\.[A-Za-z0-9_]+", str(root)) or root.is_symlink():
        raise InfrastructureError("invalid remote owned directory")
    if (root / ".owner").read_text() != token:
        raise InfrastructureError("remote ownership token mismatch")
    return root


def collect_remote(path, token):
    root = owned_directory(path, token)
    results = root / "results"
    # Transfer/setup can fail before the installed runner starts. Still collect
    # an empty owned evidence directory so its workspace can be safely removed.
    results.mkdir(mode=0o700, exist_ok=True)
    (results / ".cancel").touch()
    deadline = time.monotonic() + 15
    while (results / ".running").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if (results / ".running").exists():
        raise InfrastructureError("remote runner has not stopped; preserving owned workspace")
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        archive.add(results, arcname="remote")


def extract_results(archive, destination):
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "remote":
                raise InfrastructureError("unsafe remote diagnostic archive path")
            if not member.isfile() and not member.isdir():
                raise InfrastructureError("unsupported remote diagnostic archive member")
        stream.extractall(destination)


def ssh_run(host, bundle, arguments, results, report):
    if not host or host.startswith("-") or any(char.isspace() for char in host):
        raise InfrastructureError("SSH host must be a hostname or configured SSH alias")
    report.update(bundle_identity(bundle))
    for required in ("native-runner.py", "run-macos-installed-tests.sh"):
        if not (bundle / required).is_file():
            raise InfrastructureError(f"bundle is missing installed runner asset: {required}")
    ctest_arguments(arguments)
    timeout = duration("DARLING_NATIVE_TIMEOUT_SECONDS", 1800)
    transfer_timeout = duration("DARLING_NATIVE_TRANSFER_TIMEOUT_SECONDS", 120)
    ssh = ["ssh", "-T", "-oBatchMode=yes", "-oConnectTimeout=15",
           "-oServerAliveInterval=10", "-oServerAliveCountMax=3", host]
    remote_path = os.environ.get("DARLING_NATIVE_REMOTE_PATH")
    remote_environment = ["env"] + (["PATH=" + remote_path] if remote_path else [])

    def remote(command, bound=transfer_timeout, **kwargs):
        return run(ssh + [shlex.join(command)], bound, **kwargs)

    root = None
    token = uuid.uuid4().hex
    cleaned = False
    try:
        with Path(__file__).open("rb") as controller, (results / "bootstrap-output.log").open("wb") as errors:
            code, content = remote(remote_environment + ["python3", "-c",
                "import pathlib,shutil,sys,tempfile; "
                "root=pathlib.Path(tempfile.mkdtemp(prefix='darling-native.', dir='/tmp')); "
                "(root/'.owner').write_text(sys.argv[1]); "
                "shutil.copyfileobj(sys.stdin.buffer, (root/'controller.py').open('wb')); "
                "print(root)", token], input_file=controller, stderr=errors)
        candidate = content.decode().strip()
        if code or not re.fullmatch(r"/tmp/darling-native\.[A-Za-z0-9_]+", candidate):
            raise InfrastructureError("SSH workspace creation failed: " + candidate)
        root = candidate
        report["remote_workspace"] = root
        # Only our checked bundle enters this fresh, token-owned directory.
        archive_path = results / "bundle.tar"
        with tarfile.open(archive_path, "w") as archive:
            archive.add(bundle, arcname="bundle")
        with archive_path.open("rb") as payload, (results / "transfer-output.log").open("wb") as output:
            code, _ = remote(["env", "COPYFILE_DISABLE=1", "tar", "--no-mac-metadata", "--no-xattrs",
                              "--options", "!mac-ext", "-xpf", "-", "-C", root],
                             input_file=payload, output=output)
        if code:
            raise InfrastructureError(f"SSH bundle transfer exited {code}")
        archive_path.unlink()
        runner = root + "/bundle/native-runner.py"
        environment = remote_environment + [
            "DARLING_NATIVE_RESULTS_DIR=" + root + "/results",
            "DARLING_NATIVE_TIMEOUT_SECONDS=" + str(timeout),
            "DARLING_NATIVE_CTEST=" + os.environ.get("DARLING_NATIVE_CTEST", "ctest")]
        with (results / "ssh-output.log").open("wb") as output:
            code, _ = remote(environment + ["python3", runner, "local", root + "/bundle"] + arguments,
                             timeout + 120, output=output)
        report["ssh_returncode"] = code
        if code not in (0, 1, 2):
            raise InfrastructureError(f"SSH execution transport exited {code}")
    finally:
        if root:
            try:
                runner = root + "/controller.py"
                # Collection requests cooperative cancellation, waits for the
                # runner to reap its own group, and only then archives evidence.
                archive_path = results / "remote-results.tar"
                with archive_path.open("wb") as output, (results / "collection-output.log").open("wb") as errors:
                    collection_code, _ = remote(remote_environment + [
                        "python3", runner, "collect", root, token], output=output, stderr=errors)
                if collection_code:
                    raise InfrastructureError(f"SSH diagnostics retrieval exited {collection_code}")
                extract_results(archive_path, results)
                archive_path.unlink()
                cleanup_code, cleanup_output = remote(remote_environment + [
                    "python3", runner, "cleanup", root, token])
                if cleanup_code:
                    raise InfrastructureError("SSH owned cleanup failed: " + cleanup_output.decode(errors="replace"))
                cleaned = True
            except (OSError, ValueError, tarfile.TarError, InfrastructureError) as error:
                report["transport_cleanup_error"] = str(error)
                print(f"remote diagnostics/workspace retained at {host}:{root}: {error}", file=sys.stderr)
            report["remote_workspace_cleaned"] = cleaned
    if not cleaned:
        raise InfrastructureError("remote evidence retrieval or owned cleanup failed")
    remote_report = json.loads((results / "remote" / "execution.json").read_text())
    report["remote_execution"] = remote_report
    if remote_report.get("bundle_sha256") != report["bundle_sha256"]:
        raise InfrastructureError("transferred bundle digest differs from source bundle")
    if remote_report.get("status") not in ("passed", "test_failed", "skipped"):
        raise InfrastructureError("remote runner infrastructure failure: " + remote_report.get("error", "unknown"))
    expected = 1 if remote_report["status"] == "test_failed" else 0
    if report["ssh_returncode"] != expected:
        raise InfrastructureError("SSH exit status disagrees with remote evidence")
    report["status"] = remote_report["status"]
    return expected


def main():
    arguments = sys.argv[1:]
    if arguments and arguments[0] in ("collect", "cleanup"):
        if len(arguments) != 3:
            raise InfrastructureError("invalid internal remote operation")
        if arguments[0] == "collect":
            collect_remote(arguments[1], arguments[2])
        else:
            root = owned_directory(arguments[1], arguments[2])
            if (root / "results" / ".running").exists():
                raise InfrastructureError("refusing cleanup of an active remote runner")
            shutil.rmtree(root)
        return 0
    if not arguments or arguments[0] not in ("local", "ssh"):
        raise InfrastructureError("usage: native-transport.py local BUNDLE [CTest args] | ssh HOST BUNDLE [CTest args]")
    bundle_index = 1 if arguments[0] == "local" else 2
    if len(arguments) <= bundle_index:
        raise InfrastructureError("missing native runner arguments")
    bundle = Path(arguments[bundle_index]).resolve()
    results = results_directory(bundle)
    print(f"native results: {results}", flush=True)
    report = {"schema_version": 1, "status": "running", "started_at_unix": time.time(),
              "transport": arguments[0]}
    running = results / ".running"
    running.touch()
    dump(results / "execution.json", report)
    code = 2
    try:
        if arguments[0] == "local":
            code = local(bundle, arguments[2:], results, report)
        else:
            code = ssh_run(arguments[1], bundle, arguments[3:], results, report)
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError, tarfile.TarError, InfrastructureError) as error:
        report.update(status="infrastructure_error", error=str(error))
        print(f"native infrastructure error: {error}", file=sys.stderr)
    finally:
        report["finished_at_unix"] = time.time()
        report["exit_code"] = code
        dump(results / "execution.json", report)
        running.unlink()
    print(f"native outcome: {report['status']} (results: {results})", flush=True)
    return code


def interrupted(signum, frame):
    raise InfrastructureError(f"native runner interrupted by signal {signum}")


if __name__ == "__main__":
    # SSH hangup cannot leave unbounded CTest children; the owned runner retains
    # its deadline and cooperative cancellation even if the connection closes.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        sys.exit(main())
    except (OSError, InfrastructureError) as error:
        print(f"native infrastructure error: {error}", file=sys.stderr)
        sys.exit(2)

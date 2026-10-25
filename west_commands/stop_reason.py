"""Name a run's stop cause separately from its product verdict, without launching it."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

if __package__:
    from .evidence_storage import EvidenceError, load_index, open_payload, resolve_payload
else:
    from evidence_storage import EvidenceError, load_index, open_payload, resolve_payload


STATUS = re.compile(r"(?<![A-Za-z0-9_])(?:GUEST_XCB_RC|IOCI_XCB_RC|XCB_RC|xcb_rc|guest_rc)=([0-9]+)(?![A-Za-z0-9_])")
PREFIX = re.compile(r"prefix=([^\s\]]+)")
PID = re.compile(r"\bpid=([0-9]+)")
WAIT = re.compile(r"waited ([0-9]+)s of at most ([0-9]+)s")
SYMBOL = re.compile(r"Symbol not found: (\S+)")
IMAGE = re.compile(r"Library not loaded: (\S+)")
SQLITE = re.compile(r"error opening!: ([0-9]+)")
SQLITE_PATH = re.compile(r"Could not create database queue for path (\S+)")
PLUGIN = re.compile(r"plug-in ([A-Za-z0-9_.-]+)")
EXTENSION = re.compile(r"extension '([A-Za-z0-9_.-]+)'")
POLLUTION = re.compile(r"^\[(mldr|srv-|add |pcreate|dthread|release-drops|afunix|rpc-)")


@dataclass
class Observation:
    prefix: str = ""
    pids: set[str] = field(default_factory=set)
    guest_rc: int | None = None
    oracle: bool = False
    polluted: bool = False
    exhausted: bool = False
    license: bool = False
    launch_timeout: bool = False
    extension_fault: bool = False
    symbol: str = ""
    symbol_referrer: str = ""
    expected_in: str = ""
    image: str = ""
    image_referrer: str = ""
    sqlite_code: str = ""
    sqlite_path: str = ""
    plugin: str = ""
    extension: str = ""

    def read(self, path: Path, oracles: list[str]) -> None:
        # Each payload is decoded once. Only matching facts and bounded context survive;
        # no extracted raw files or whole-payload cache are needed.
        symbol_context = image_context = 0
        with open_payload(path) as payload:
            for raw_line in payload:
                line = raw_line.decode("utf-8", errors="replace")
                if not self.prefix and (match := PREFIX.search(line)):
                    self.prefix = match[1]
                self.pids.update(PID.findall(line))
                self.pids = set(sorted(self.pids)[:8])
                for match in STATUS.finditer(line):
                    value = int(match[1])
                    if self.guest_rc is None or (self.guest_rc == 0 and value != 0):
                        self.guest_rc = value
                self.oracle |= any(oracle and oracle in line for oracle in oracles)
                self.polluted |= bool(POLLUTION.search(line))
                self.exhausted |= any(int(a) == int(b) for a, b in WAIT.findall(line))
                self.license |= "You have not agreed to the Xcode license agreements" in line
                self.launch_timeout |= "shellspawn did not become ready" in line
                self.extension_fault |= "DVTPlugInExtensionFaulting" in line or "_fireExtensionFaultAssertingOnBundleLoadError" in line
                for attribute, pattern in (("sqlite_code", SQLITE), ("sqlite_path", SQLITE_PATH), ("plugin", PLUGIN), ("extension", EXTENSION)):
                    if not getattr(self, attribute) and (match := pattern.search(line)):
                        setattr(self, attribute, match[1])
                if symbol_context:
                    if "Referenced from:" in line and not self.symbol_referrer:
                        self.symbol_referrer = line.split("Referenced from:", 1)[1].strip().split(" (which was built for", 1)[0]
                    if "Expected in:" in line and not self.expected_in:
                        self.expected_in = line.split("Expected in:", 1)[1].strip()
                    symbol_context -= 1
                if image_context:
                    if "Referenced from:" in line and not self.image_referrer:
                        self.image_referrer = line.split("Referenced from:", 1)[1].strip()
                    image_context -= 1
                if not self.symbol and (match := SYMBOL.search(line)):
                    self.symbol = match[1]
                    symbol_context = 3
                if not self.image and (match := IMAGE.search(line)):
                    self.image = match[1]
                    image_context = 2

    def merge(self, other: Observation) -> None:
        for name in ("prefix", "symbol", "symbol_referrer", "expected_in", "image", "image_referrer", "sqlite_code", "sqlite_path", "plugin", "extension"):
            if not getattr(self, name):
                setattr(self, name, getattr(other, name))
        for name in ("oracle", "polluted", "exhausted", "license", "launch_timeout", "extension_fault"):
            setattr(self, name, getattr(self, name) or getattr(other, name))
        self.pids = set(sorted(self.pids | other.pids)[:8])
        if self.guest_rc is None or (self.guest_rc == 0 and other.guest_rc not in (None, 0)):
            self.guest_rc = other.guest_rc


def signal_name(rc: int | None) -> str:
    if rc is None or rc < 128:
        return ""
    number = rc - 128
    return {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 11: "SIGSEGV", 15: "SIGTERM"}.get(number, f"SIG{number}")


def cause(observed: Observation, rc: int | None) -> tuple[str, dict[str, str]]:
    guest = str(observed.guest_rc) if observed.guest_rc is not None else "absent"
    if observed.license:
        return "license-required", {"stage": "license", "guest_rc": guest}
    if observed.symbol:
        return "missing-symbol", {"symbol": observed.symbol, "referrer": observed.symbol_referrer or "?", "expected_in": observed.expected_in or "?", "guest_rc": guest, "stage": "loader-dlopen"}
    if observed.image:
        return "missing-image", {"image": observed.image, "referrer": observed.image_referrer or "?", "guest_rc": guest, "stage": "loader-load"}
    if observed.extension_fault:
        return "extension-fault", {"plug_in": observed.plugin or "?", "extension": observed.extension or "?", "guest_rc": guest, "stage": "plugin-load"}
    if observed.sqlite_code and observed.guest_rc != 0 and rc != 0:
        # FMDB prints sqlite3_open's result, not an OS errno. A completed
        # successful workload establishes that this diagnostic was only context.
        return "sqlite-open-failed", {"code": observed.sqlite_code, "path": observed.sqlite_path or "unknown", "note": "sqlite-result-code-not-errno", "guest_rc": guest, "stage": "sqlite-open"}
    if observed.launch_timeout:
        return "launch-timeout", {"stage": "shellspawn", "guest_rc": "absent"}
    if observed.exhausted and observed.guest_rc is None and rc is None:
        return "watchdog-timeout", {"guest_rc": "absent", "stage": "unresolved"}
    if observed.guest_rc not in (None, 0):
        fields = {"guest_rc": guest}
        if signal := signal_name(observed.guest_rc):
            fields["signal"] = signal
        fields["stage"] = "product"
        return "guest-nonzero-exit", fields
    if observed.guest_rc is None and signal_name(rc):
        return "crash", {"guest_rc": str(rc), "stage": "crash"}
    return "none", {"guest_rc": guest}


def directory_candidates(directory: Path, *, captures: bool) -> list[Path]:
    # Only the storage owner understands indexes. Include indexed logical names,
    # including missing stored files, so unavailable evidence is reported.
    names = set(load_index(directory)["files"])
    names.update(path.name.removesuffix(".zst") for path in directory.iterdir() if path.is_file())
    return [directory / name for name in sorted(names) if captures or name.endswith(".out")]


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=__doc__,
        epilog="Exit 0 when a stop cause is named, 3 when kind=none (even for a failed verdict), 2 for invalid arguments. No guest is launched. Missing status is INCONCLUSIVE unless an explicit success oracle matches; any known failed guest/--rc/--runner-rc status overrides an oracle. Unavailable evidence is warned and cannot establish PASS.",
    )
    result.add_argument("--log", type=Path, required=True, help="this run's raw or indexed compressed logical log path")
    result.add_argument("--bundle", type=Path, help="run bundle: abort.txt, captures/* and top-level *.out (raw or indexed zstd)")
    result.add_argument("--rc", type=nonnegative, help="declared guest workload exit status; zero establishes completion, >=128 also reports a signal")
    result.add_argument("--runner-rc", type=nonnegative, help="separate runner status; nonzero overrides completion/oracle presence")
    result.add_argument("--oracle", action="append", default=[], help="explicit literal success oracle; repeat for alternatives (not proof against a failed status)")
    result.add_argument("--run-start", type=nonnegative, help="epoch seconds; older captures are counted as excluded using original payload mtime")
    result.add_argument("--run-id", help="run identity (default: logical log basename)")
    result.add_argument("--json", action="store_true", help="emit one JSON object with stop, verdict, run record and warnings")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    observed = Observation()
    warnings: list[dict[str, str]] = []
    captures: list[str] = []
    excluded = 0
    ended = "0"
    evidence_count = 0

    def warn(path: Path, error: Exception) -> None:
        warnings.append({"path": str(path), "reason": str(error)})

    def consume(path: Path, *, capture: bool = False) -> None:
        nonlocal excluded, ended, evidence_count
        try:
            stored = resolve_payload(path)
            mtime = int(stored.stat().st_mtime)
            if capture and args.run_start is not None and mtime < args.run_start:
                excluded += 1
                return
            item = Observation()
            item.read(path, args.oracle)
            if capture:
                captures.append(str(path))
                if observed.prefix and item.prefix and observed.prefix != item.prefix:
                    warnings.append({"path": str(path), "reason": "capture-prefix-mismatch", "capture_prefix": item.prefix, "run_prefix": observed.prefix})
            if path == args.log:
                ended = str(mtime)
            observed.merge(item)
            evidence_count += 1
        except (EvidenceError, OSError, ValueError) as error:
            warn(path, error)

    consume(args.log)
    if args.bundle is not None:
        if not args.bundle.is_dir():
            warn(args.bundle, EvidenceError("missing evidence bundle"))
        else:
            try:
                candidates = directory_candidates(args.bundle, captures=False)
                bundle_names = load_index(args.bundle)["files"]
                abort = args.bundle / "abort.txt"
                if abort.exists() or abort.with_name("abort.txt.zst").exists() or "abort.txt" in bundle_names:
                    consume(abort)
                capture_dir = args.bundle / "captures"
                if capture_dir.is_dir():
                    candidates = directory_candidates(capture_dir, captures=True) + candidates
                for path in candidates:
                    # Index metadata is never itself a capture.
                    if path.name.startswith("evidence-index") or path.name == ".evidence-index.lock":
                        continue
                    consume(path, capture=True)
            except (EvidenceError, OSError, ValueError) as error:
                warn(args.bundle, error)

    kind, fields = cause(observed, args.rc)
    statuses = (observed.guest_rc, args.rc, args.runner_rc)
    if any(status is not None and status != 0 for status in statuses) or kind != "none":
        product = "FAIL"
    elif warnings or evidence_count == 0:
        product = "INCONCLUSIVE"
    elif observed.guest_rc == 0 or args.rc == 0 or observed.oracle:
        product = "PASS"
    else:
        product = "INCONCLUSIVE"
    if evidence_count == 0:
        fields["note"] = "no-evidence"
        fields["path"] = str(args.log)
    signal = signal_name(observed.guest_rc) or signal_name(args.rc)
    result = {"kind": kind, **fields}
    if observed.prefix:
        result["prefix"] = observed.prefix
    if signal:
        result["signal"] = signal
    if args.rc is not None:
        result["rc"] = str(args.rc)
    if observed.polluted:
        result["pollution"] = "1"
    if observed.sqlite_code and (observed.license or observed.guest_rc == 0 or args.rc == 0):
        result.update(context_kind="sqlite-open-failed", context_code=observed.sqlite_code, context_path=observed.sqlite_path or "unknown")
    result.update(run=args.run_id or args.log.name.removesuffix(".zst"), started=str(args.run_start) if args.run_start is not None else "unknown", ended=ended,
                  harness_rc=str(args.rc) if args.rc is not None else "absent", guest_xcb_rc=str(observed.guest_rc) if observed.guest_rc is not None else "absent",
                  product_verdict=product, runner_rc=str(args.runner_rc) if args.runner_rc is not None else "absent", oracle=str(int(observed.oracle)),
                  captures="".join(path + "," for path in captures), capture_count=str(len(captures)), excluded=str(excluded), pids=",".join(sorted(observed.pids)), warnings=warnings)
    if args.json:
        print(json.dumps(result))
    else:
        if "context_kind" in result:
            print(f"STOP-CONTEXT kind=sqlite-open-failed code={result['context_code']} path={result['context_path']} note=preceding-sqlite-warning-not-the-stop")
        stop = [f"STOP-REASON kind={kind}", *(f"{key}={value}" for key, value in fields.items())]
        if observed.prefix:
            stop.append(f"prefix={observed.prefix}")
        if signal and "signal" not in fields:
            stop.append(f"signal={signal}")
        if args.rc is not None:
            stop.append(f"rc={args.rc}")
        if observed.polluted:
            stop.append("note=workload-output-carries-loader-diagnostics")
        print(" ".join(stop))
        print(f"RUN-RECORD run={result['run']} started={result['started']} ended={ended} prefix={observed.prefix or 'unknown'} harness_rc={result['harness_rc']} captures={result['captures'] or 'none'} capture_count={len(captures)} excluded={excluded} pids={result['pids'] or 'none'}")
        for warning in warnings:
            if warning['reason'] == 'capture-prefix-mismatch':
                print(f"RUN-RECORD-WARNING capture-prefix={warning['capture_prefix']} run-prefix={warning['run_prefix']} path={warning['path']}")
            else:
                print(f"RUN-RECORD-WARNING evidence-unavailable path={warning['path']} reason={warning['reason']}")
        print(f"PRODUCT-VERDICT PRODUCT_VERDICT={product} HARNESS_RC={result['harness_rc']} GUEST_XCB_RC={result['guest_xcb_rc']} RUNNER_RC={result['runner_rc']} oracle={result['oracle']} stop={kind} run={result['run']} prefix={observed.prefix or 'unknown'}")
    return 3 if kind == "none" else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Isolated TLC execution; exit codes follow TLC EC.ExitStatus (0/12)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
INVARIANTS = ("Inv2NoWrongGeneration", "Inv3NoSilentLoss", "Inv4NoDuplicateApply",
              "Inv5NoForeignReply", "Inv6NoInternalCycle", "Inv7QueuedCallHasOwner")
# Expected counterexamples, not a second implementation of the protocol.
VIOLATIONS = {
    "guarded": (), "unowned-arm": (3, 4), "no-gen-guard": (2, 3, 4),
    "reclaim-race": (4,), "internal-cycle": (6,),
    "real-console-defer": (3, 4), "arm-after-queue": (3, 4, 7),
}


def classify(returncode: int | None, text: str, invariant: str) -> str:
    """Neither a successful prefix nor a failure alone is a verdict."""
    if re.search(r"Exception in thread|java\.lang\.\w*(?:Exception|Error)|"
                 r"A fatal error has been detected|SIGSEGV|OutOfMemoryError", text):
        return "TOOL/CONFIG ERROR"
    finished = re.search(r"^Finished in .+", text, re.MULTILINE)
    success = "Model checking completed. No error has been found." in text
    violations = re.findall(r"^Error: Invariant (\w+) is violated\.$", text, re.MULTILINE)
    errors = re.findall(r"^Error: (.*)$", text, re.MULTILINE)
    trace = re.search(r"^State 1:", text, re.MULTILINE)
    if finished and returncode == 0 and success and not errors:
        return "OK"
    if (finished and returncode == 12 and not success and violations == [invariant]
            and trace and "Error: The behavior up to this point is:" in text
            and all(e in (f"Invariant {invariant} is violated.",
                          "The behavior up to this point is:") for e in errors)):
        return "VIOLATED"
    return "TOOL/CONFIG ERROR"


def main(argv=None, *, executor=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", default=os.environ.get("TLA_JAR"))
    parser.add_argument("--java", default="java")
    parser.add_argument("--output-root", type=Path,
                        default=Path(os.environ.get("DW_STATE_ROOT", str(HERE.parents[2] / ".west-state"))) / "model-runs")
    parser.add_argument("--model-dir", type=Path, default=HERE)
    parser.add_argument("--module", default="DServerPlane")
    parser.add_argument("--case", action="append", help="CONFIG:INVARIANT:OK|VIOLATED; repeatable")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    argv = list(sys.argv[1:] if argv is None else argv)
    args.output_root.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="tlc-", dir=args.output_root.resolve()))
    print(f"Artifacts: {run}", flush=True)
    results = []
    try:
        if not args.jar or not Path(args.jar).is_file():
            raise ValueError("--jar/TLA_JAR must name an existing tla2tools.jar")
        if args.workers < 1 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.module):
            raise ValueError("invalid workers or module")
        jar = Path(args.jar).resolve()
        cases = []
        if args.case:
            for case in args.case:
                config, inv, expected = case.split(":")
                if expected not in ("OK", "VIOLATED") or not re.fullmatch(r"\w+", inv):
                    raise ValueError(f"invalid case: {case}")
                cases.append((config, inv, expected))
        else:
            if args.model_dir.resolve() != HERE or args.module != "DServerPlane":
                raise ValueError("custom models require explicit --case expectations")
            for config, bad in VIOLATIONS.items():
                for inv in INVARIANTS:
                    cases.append((f"cfg_{config}.cfg", inv, "VIOLATED" if int(inv[3]) in bad else "OK"))
        metadata = {"argv": argv, "jar": str(jar), "jar_sha256": hashlib.sha256(jar.read_bytes()).hexdigest()}
        (run / "invocation.json").write_text(json.dumps(metadata, indent=2) + "\n")
        for index, (config, inv, expected) in enumerate(cases):
            cell = run / f"{index:02d}-{inv}"
            cell.mkdir()
            # Copy sources: generated traces and state files can never touch the checkout.
            for source in args.model_dir.glob("*.tla"):
                shutil.copy2(source, cell / source.name)
            cfg = (args.model_dir / config).read_text()
            cfg = re.sub(r"(?m)^\s*INVARIANTS?\s+.*$", "", cfg)
            (cell / "case.cfg").write_text(cfg + f"\nINVARIANT {inv}\n")
            command = [args.java, "-cp", str(jar), "tlc2.TLC", "-config", "case.cfg",
                       "-workers", str(args.workers), "-metadir", str(cell / "states"), args.module + ".tla"]
            (cell / "command.json").write_text(json.dumps({"argv": command, "cwd": str(cell)}, indent=2) + "\n")
            rc = None
            with (cell / "tlc.log").open("w") as log:
                try:
                    rc = executor(command, cwd=cell, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
                except OSError as error:
                    log.write(f"Process launch failed: {error}\n")
            verdict = classify(rc, (cell / "tlc.log").read_text(errors="replace"), inv)
            result = {"config": config, "invariant": inv, "expected": expected,
                      "verdict": verdict, "returncode": rc, "artifacts": str(cell)}
            results.append(result)
            (cell / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(f"{config} {inv}: {verdict} (expected {expected}, rc={rc})", flush=True)
        return int(any(r["verdict"] != r["expected"] for r in results))
    except (OSError, ValueError) as error:
        (run / "error.txt").write_text(str(error) + "\n")
        print(f"TOOL/CONFIG ERROR: {error}", flush=True)
        return 1
    finally:
        (run / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Drive the TLC CLI contract with controlled child outcomes, without Java."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("tlc_runner", ROOT / "audits/rpc-plane-model/run_models.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
INV = "NoLateWrite"
OK = "Model checking completed. No error has been found.\nFinished in 1s at now\n"
BAD = f"Error: Invariant {INV} is violated.\nError: The behavior up to this point is:\nState 1: <Initial predicate>\nx = 1\nState 2: <Write>\nx = 2\nFinished in 1s at now\n"


class RunnerContract(unittest.TestCase):
    def setUp(self):
        state = Path(os.environ.get("DW_STATE_ROOT", str(ROOT.parent / ".west-state"))) / "tlc-contracts"
        state.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="contract-", dir=state)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "Example.tla").write_text("---- MODULE Example ----\n====\n")
        self.config = self.model / "safe.cfg"
        self.config.write_text("SPECIFICATION Spec\nINVARIANT OtherInvariant\n")
        self.jar = self.root / "tools.jar"
        self.jar.write_bytes(b"controlled executor; not a TLC jar")

    def invoke(self, rc, text, expected="VIOLATED", *, launch_error=False, jar=None):
        output_root = self.root / "out"
        previous_runs = set(output_root.iterdir()) if output_root.exists() else set()
        def execute(command, **kwargs):
            cell = kwargs["cwd"]
            if launch_error:
                raise FileNotFoundError("controlled missing java")
            kwargs["stdout"].write(text)
            (cell / "Example_TTrace_1.tla").write_text("preserved trace")
            return subprocess.CompletedProcess(command, rc)
        with contextlib.redirect_stdout(io.StringIO()):
            status = runner.main(["--jar", str(jar or self.jar), "--model-dir", str(self.model),
                                  "--module", "Example", "--output-root", str(self.root / "out"),
                                  "--case", f"safe.cfg:{INV}:{expected}"], executor=execute)
        run, = set(output_root.iterdir()) - previous_runs
        results = json.loads((run / "results.json").read_text())
        self.assertEqual(self.config.read_text(), "SPECIFICATION Spec\nINVARIANT OtherInvariant\n")
        return status, run, results

    def test_completed_outcomes_and_artifacts(self):
        for rc, text, expected in ((0, OK, "OK"), (12, BAD, "VIOLATED")):
            with self.subTest(expected=expected):
                status, run, results = self.invoke(rc, text, expected)
                self.assertEqual(status, 0)
                cell = Path(results[0]["artifacts"])
                self.assertEqual((cell / "tlc.log").read_text(), text)
                self.assertTrue((cell / "command.json").is_file())
                self.assertTrue((cell / "Example_TTrace_1.tla").is_file())
                self.assertTrue((run / "invocation.json").is_file())

    def test_errors_never_count_as_counterexamples(self):
        cases = [(1, "Error: Parsing or semantic analysis failed.\n"),
                 (150, "Error: Configuration file is malformed\n"),
                 (-11, BAD), (1, BAD), (0, BAD), (12, OK),
                 (12, BAD.replace("Finished in 1s at now\n", "")),
                 (12, BAD.replace("State 1:", "Missing trace:")),
                 (12, BAD.replace(INV, "OtherInvariant")),
                 (12, BAD + "Error: Java heap space\n"),
                 (12, BAD + 'Exception in thread "main" java.lang.OutOfMemoryError\n'),
                 (12, BAD + "# A fatal error has been detected by the Java Runtime Environment\n"),
                 (1, OK), (0, OK.replace("Finished in 1s at now\n", ""))]
        for rc, text in cases:
            with self.subTest(rc=rc, text=text):
                status, _, results = self.invoke(rc, text)
                self.assertEqual(status, 1)
                self.assertEqual(results[0]["verdict"], "TOOL/CONFIG ERROR")

    def test_missing_java_and_jar(self):
        status, _, results = self.invoke(0, OK, launch_error=True)
        self.assertEqual(status, 1)
        self.assertEqual(results[0]["verdict"], "TOOL/CONFIG ERROR")
        status, run, results = self.invoke(0, OK, jar=self.root / "missing.jar")
        self.assertEqual(status, 1)
        self.assertEqual(results, [])
        self.assertTrue((run / "error.txt").is_file())

    def test_unexpected_outcome_fails_and_stale_log_not_reused(self):
        first, _, _ = self.invoke(12, BAD)
        second, _, results = self.invoke(0, OK)
        self.assertEqual(first, 0)
        self.assertEqual(second, 1)
        self.assertEqual(results[0]["verdict"], "OK")


if __name__ == "__main__":
    unittest.main()

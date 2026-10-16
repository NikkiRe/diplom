"""CLI check_trace.py: коды выхода, отчёт, файлы примеров."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from monitor.tests import tracegen

ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = ROOT / "monitor" / "check_trace.py"
EXAMPLES = ROOT / "monitor" / "examples"


def run_cli(*args):
    return subprocess.run([sys.executable, str(CLI_PATH), *map(str, args)], capture_output=True, text=True, timeout=120)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sc = tracegen.generate(7, linked=True)
        self.trace = self.tmp / "trace.jsonl"
        self.gate = self.tmp / "gate.jsonl"
        self.tools = self.tmp / "tools.json"
        self.trace.write_text("\n".join(sc.trace_lines()) + "\n", encoding="utf-8")
        self.gate.write_text("\n".join(sc.gate_lines()) + "\n", encoding="utf-8")
        self.tools.write_text(json.dumps(sc.tools, ensure_ascii=False), encoding="utf-8")
        self.sc = sc

    def test_ok_run_writes_report(self):
        out = self.tmp / "sub" / "monitor.json"
        p = run_cli("--trace", self.trace, "--hw-log", self.gate, "--tools", self.tools, "--out", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        rep = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["schema"], "monitor.v2")
        self.assertEqual(rep["n_unsafe"], 0)
        self.assertIn("trace", rep["summary"])
        self.assertIn("gate", rep["summary"])
        self.assertIn("OK", p.stderr)

    def test_violation_exit_1(self):
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        self.trace.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")   # без run_end
        p = run_cli("--trace", self.trace, "--tools", self.tools, "--quiet")
        self.assertEqual(p.returncode, 1)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["by_class"], {"frame.no_run_end": 1})
        self.assertEqual(p.stderr, "")

    def test_unsafe_gate_exit_1(self):
        recs = [json.loads(x) for x in self.gate.read_text(encoding="utf-8").splitlines()]
        for r in recs:
            if r["type"] == "gate_decision" and r["device"] == "valve" and r["executed"]:
                r["final_args"] = {"deg": 180}
                break
        else:
            self.skipTest("нет исполненной команды клапану")
        self.gate.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
        p = run_cli("--hw-log", self.gate, "--quiet")
        self.assertEqual(p.returncode, 1)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["n_unsafe"], 1)
        self.assertIn("actuator_safety.out_of_range", rep["by_class"])

    def test_gate_config_file(self):
        decl = self.tmp / "declared.json"
        decl.write_text(json.dumps({"gate": tracegen.DECLARED_GATE}, ensure_ascii=False), encoding="utf-8")
        p = run_cli("--hw-log", self.gate, "--gate-config", decl, "--quiet")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        self.assertEqual(json.loads(p.stdout)["assumptions"]["gate_policy_source"], "--gate-config")

    def test_usage_errors_exit_2(self):
        self.assertEqual(run_cli().returncode, 2)
        self.assertEqual(run_cli("--trace", self.tmp / "missing.jsonl").returncode, 2)
        bad = self.tmp / "bad_tools.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(run_cli("--trace", self.trace, "--tools", bad).returncode, 2)

    def test_options_override_run_start(self):
        p = run_cli("--trace", self.trace, "--tools", self.tools, "--max-steps", "1", "--quiet")
        rep = json.loads(p.stdout)
        if max(len([1 for r in self.sc.trace if r.get("turn_id") == t and r["type"] == "llm_request"])
               for t in {r.get("turn_id") for r in self.sc.trace if r.get("turn_id")}) > 1:
            self.assertEqual(p.returncode, 1)
            self.assertIn("budget.max_steps", rep["by_class"])

    def test_examples(self):
        p = run_cli("--trace", EXAMPLES / "trace.jsonl", "--hw-log", EXAMPLES / "hwmcp_gate.jsonl",
                    "--tools", EXAMPLES / "tools.json", "--gate-config", EXAMPLES / "declared_gate.json", "--quiet")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        p = run_cli("--trace", EXAMPLES / "trace_bad.jsonl", "--hw-log", EXAMPLES / "hwmcp_gate_bad.jsonl",
                    "--tools", EXAMPLES / "tools.json", "--quiet")
        self.assertEqual(p.returncode, 1)
        rep = json.loads(p.stdout)
        self.assertGreaterEqual(rep["n_unsafe"], 1)


if __name__ == "__main__":
    unittest.main()

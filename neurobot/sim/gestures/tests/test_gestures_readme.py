"""README компонента называет каждый флаг всех его CLI (gesturegen, analyze, shapeclass, наблюдатель), и у каждого
флага есть подсказка в --help. observer.py импортирует rclpy, поэтому его флаги берутся из AST файла."""
from __future__ import annotations

import argparse
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from neurobot.sim.gestures import analyze, gesturegen, shapeclass  # noqa: E402

PKG = ROOT / "neurobot/sim/gestures"


def parser_flags(parser: argparse.ArgumentParser) -> dict:
    """{флаг: есть ли подсказка} парсера и его подкоманд."""
    out = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                out.update(parser_flags(sub))
        elif action.option_strings and action.dest != "help":
            out[max(action.option_strings, key=len)] = bool(action.help)
    return out


def ast_flags(path: Path) -> dict:
    """{флаг: есть ли подсказка} по вызовам add_argument с литералом «--…» в исходнике."""
    out = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument" and node.args
                and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).startswith("--")):
            out[node.args[0].value] = any(k.arg == "help" for k in node.keywords)
    return out


class ReadmeFlagsTest(unittest.TestCase):
    def test_every_flag_is_in_readme_and_has_help(self):
        readme = (PKG / "README.md").read_text(encoding="utf-8")
        clis = {"gesturegen": parser_flags(gesturegen.build_parser()), "analyze": parser_flags(analyze.build_parser()),
                "shapeclass": parser_flags(shapeclass.build_parser()), "observer": ast_flags(PKG / "observer.py")}
        for cli, flags in clis.items():
            self.assertGreaterEqual(len(flags), 5, cli)
            for flag, has_help in sorted(flags.items()):
                self.assertTrue(has_help, "%s %s: нет подсказки" % (cli, flag))
                named = r"(?<![\w-])%s(?![\w-])" % re.escape(flag)
                self.assertRegex(readme, named, "%s %s: нет в README" % (cli, flag))


if __name__ == "__main__":
    unittest.main()

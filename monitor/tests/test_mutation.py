"""Мутационные тесты: каждый мутант применим хотя бы к одному прогону корпуса и обнаруживается всегда."""
import unittest

from monitor.tests import tracegen
from monitor.tests.mutants import MUTANTS
from monitor.tests.mutation_suite import run


class MutationKill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats = run(tracegen.corpus(4))

    def test_enough_mutant_classes(self):
        self.assertGreaterEqual(len(MUTANTS), 20)
        self.assertGreaterEqual(len({m.expected[0] for m in MUTANTS}), 20)
        self.assertEqual(len({m.name for m in MUTANTS}), len(MUTANTS))

    def test_every_mutant_applies(self):
        never = [s.mutant.name for s in self.stats if s.applied == 0]
        self.assertEqual(never, [], "мутанты не применились ни к одному прогону")

    def test_every_mutant_is_detected(self):
        missed = {s.mutant.name: s.missed[:2] for s in self.stats if s.detected < s.applied}
        self.assertEqual(missed, {})


if __name__ == "__main__":
    unittest.main()

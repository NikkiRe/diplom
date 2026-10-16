"""Проверка smoke не должна принимать пропущенные фоновые события за успех."""
import unittest

from bench.sensors.__main__ import validate_run


class SensorSmokeVerdictTest(unittest.TestCase):
    def test_answers_alone_do_not_prove_sensor_alerts(self):
        raw = {"hw": {"devices": 2}, "mcp": {"updates": 33}, "errors": {}}
        metrics = {"questions": {"correct": 2, "total": 2},
                   "situations": {"significant": 2, "reacted_in_time": 0}}
        self.assertIn("expected two sensor alerts within deadlines", validate_run(metrics, raw, True, []))
        metrics["situations"]["reacted_in_time"] = 2
        self.assertEqual(validate_run(metrics, raw, True, []), [])

"""predict_live_v2.patch для лаборатории совпадает с копией в neurobot/perception/ и накладывается на их файл.

Патч накладывается утилитой patch(1) на присланный src/camera/predict_live.py ветки ros2_feature во временном
каталоге; результат должен побайтно совпасть с четырьмя файлами neurobot/perception/. Шапка патча называет
закрываемые id ANALYSIS. Без patch(1) или без файла лаборатории тест пропускается. Python ≥ 3.8.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PERCEPTION = ROOT / "neurobot" / "perception"
PATCH = PERCEPTION / "predict_live_v2.patch"
LAB = ROOT / "third_party" / "neurochip" / "demo" / "demo-ros2_feature" / "src" / "camera" / "predict_live.py"
LAB_SHA256 = "d186195dc0d3abb7d6b45730baa84c04ecaa7ddcbc5ea726b47ed541a7153a54"
FILES = ("predict_live.py", "camera_guard.py", "gesture_logic.py", "node_util.py")


@unittest.skipUnless(shutil.which("patch") and LAB.is_file(), "нужны patch(1) и файл лаборатории")
class LabPatch(unittest.TestCase):
    def test_applies_to_lab_file_and_matches_copy(self):
        self.assertEqual(hashlib.sha256(LAB.read_bytes()).hexdigest(), LAB_SHA256)
        with tempfile.TemporaryDirectory() as d:
            camera = Path(d) / "src" / "camera"
            camera.mkdir(parents=True)
            shutil.copy(str(LAB), str(camera / "predict_live.py"))
            with open(str(PATCH), "rb") as fh:
                done = subprocess.run(["patch", "-p1", "-s", "--batch"], cwd=d, stdin=fh, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            for name in FILES:
                self.assertEqual((camera / name).read_bytes(), (PERCEPTION / name).read_bytes(),
                                 "патч отстал от neurobot/perception/" + name)

    def test_header_names_closed_ids(self):
        header = PATCH.read_text(encoding="utf-8").split("\ndiff --git", 1)[0]
        for pid in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "L1", "L2", "L3", "L4", "L6", "G1", "G2", "G3",
                    "G4", "S1", "S2", "S3", "H6", "H7", "H10", LAB_SHA256):
            self.assertIn(pid, header)


if __name__ == "__main__":
    unittest.main()

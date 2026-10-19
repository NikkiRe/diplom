"""manifest.json каталога результатов прогона (README §9): версии, хеши кода и снимка ВМ, конфиги, итоги, отклонения.

  python3 -m neurobot.sim.runs.manifest --dir DEST --kind a0|a1|start|budget|long [--versions FILE]
       [--snapshot FILE] [--command ТЕКСТ …] [--deviation ТЕКСТ …]

Хеши — файлов neurobot/sim/runs на хосте (код в ВМ — снимок, его список sha256 — --snapshot). Секретов в
каталоге нет: тестовые токены канала pico не копируются (run.sh fetch), ключей LLM нет (mockllm).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "neurobot" / "sim" / "runs"

COMMON = [
    "Стенд — цифровой двойник в общей ВМ sim (Jazzy, Python 3.12, Cyclone DDS, Gazebo Harmonic с программным "
    "рендерингом) вместо Jetson Nano (Foxy, Python 3.8, Fast DDS, CUDA): времена — ориентир, не результат.",
    "Мир hall (neurobot/sim/runs/worlds/hall.sdf) — зал 9 × 9 м с препятствиями мира lab двойника в тех же местах "
    "относительно робота; спавн в начале координат.",
    "Руку даёт FakeYolo по таймлайну gesturegen (yolo_hand.pt не исполнялся); SNN лаборатории snn_emnist_small.pth "
    "загружалась с weights_only=True (torch CPU); темп детекции 10 к/с, задержка YOLO не моделировалась.",
    "LLM — bench/mockllm (оракул; где он работал — отклонение прогона ниже); ключей и трат нет. Реплики "
    "пользователя — в канал pico шлюза PicoClaw (WebSocket, тестовый токен прогона).",
    "Оператор прогона (runner.py) — модель человека [допущение]: через 30 с после того, как заметно (процесс умер, "
    "детектор не вызывается 10 с, агент не ответил, касание дольше 20 с, защёлка), чинит; каждое действие — "
    "вмешательство человека (правила A1 у прогона — в отклонениях ниже).",
]
DEVIATIONS = {
    "a0": [
        "Демо лаборатории predict_live.py выполнялось без правок (a0_demo.py): torch.hub.load → FakeYolo, "
        "torch.compile скрыт (на роботе torch 1.13 — его нет), torch.load(weights_only=True); подставной "
        "pyrealsense2; окно OpenCV — Xvfb; пути /home/jetson — ссылкой в ВМ.",
        "PicoClaw 0.2.8 linux-arm64 (sha256 архива совпал с picoclaw_0.2.8_checksums.txt релиза) с конфигом "
        "робота: LLM cloud.ru заменён mockllm, канал VK выключен (нет сети и ключа), пути прокси — ВМ; остальное — "
        "как у них (log_level warn, max_tool_iterations 50, exec и cron включены).",
        "Прокси лаборатории robot_vision_mcp_proxy.py — как есть (с дублями инструментов, N1), mcp 1.27.2. Воркера "
        "8766 у лаборатории нет (кода в архиве нет): вместо него честная заглушка worker8766.py — отвечает по API "
        "прокси и пишет запросы, робота не двигает.",
        "rosbridge как на роботе (ros2 launch без глобов), но address:=127.0.0.1 — правило стенда.",
        "Процессы A0 — transient-юниты systemd без рестарта (на роботе — терминалы и nohup): поднимает их только "
        "оператор прогона.",
    ],
    "a1": [
        "Пакет neurobot.target из neurobot/deploy с профилем neurobot/sim/suites/twin.env (setup_twin.sh), правки "
        "прогона в env-файле (stacks.A1.env_edit): мир hall и камера цвета 10 к/с у драйвера двойника; кадры "
        "perception — через узел отказов relay.py (ремап темы); журнал probe FakeYolo; прокси лаборатории убран из "
        "NEUROBOT_FOREIGN_MCP — в той же ВМ его запускает A0 (другой домен DDS и шлюз).",
        "Конфиг PicoClaw пакета (suites/picoclaw.json) + канал pico и api_base mockllm, затем "
        "`neurobot picoclaw-config merge`.",
        "Отказы камеры у A1 — на уровне темы ROS (relay.py: нет кадров, тот же кадр, чёрный кадр) по тому же "
        "faults.json, что подставной pyrealsense2 у A0: на роботе так выглядит сбой realsense2_camera.",
        "С 17.10 (приёмка): датчики двойника — отдельными юнитами neurobot-driver@lidar (узел /urg_node2) и @camera "
        "(узел /camera/camera: цвет, глубина, IMU камеры) рядом с @twin (Gazebo, база, twist_mux, madgwick); драйверы "
        "и rosbridge — через обёртку deploy supervised.launch.py; сторож — свежесть тем драйверов и ping rosbridge. "
        "Узлы датчиков двойника — Python (sensor_model.py), а не urg_node2 и realsense2_camera: их время подъёма — "
        "ориентир.",
    ],
}
DEVIATIONS["long"] = DEVIATIONS["a1"]
DEVIATIONS["budget"] = DEVIATIONS["a1"] + [
    "Юниты пакета — в jetson.slice (drop-in Slice=), MemoryMax, MemorySwapMax=0, CPUQuota и AllowedCPUs — по "
    "neurobot/sim/budget (квота и отношение A57/ВМ — калибровка бюджета 16.10); балласт CUDA 1024 МиБ — в процессе "
    "perception; заместители драйверов и перенос узлов двойника — budget/stack.py."]
DEVIATIONS["start"] = DEVIATIONS["a1"] + [
    "Жест цикла — ближайший «крючок» таймлайна start после «ОК» doctor (start_gesture.py check); успех — заявка "
    "исполнителя и движение или законный стоп safety_gate."]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(dest: Path, kind: str, versions: str, snapshot: str, commands, extra) -> dict:
    """plan.json — в каталоге или в каталоге прогона vm/, забранном из ВМ (run.sh fetch)."""
    plan = next((p for p in (dest / "plan.json", dest / "vm" / "plan.json") if p.exists()), None)
    doc = {
        "date": time.strftime("%Y-%m-%d"),
        "component": "интеграция: прогоны A0/A1 на цифровом двойнике (neurobot/sim/runs), вид «%s»" % kind,
        "label": "стенд: времена, TTR и память — ориентир, не результат; счётчики (касания, глухие жесты, "
                 "вмешательства человека, вызовы LLM) — результат прогона на двойнике, не на роботе",
        "commands": list(commands),
        "vm_versions": Path(versions).read_text(encoding="utf-8").splitlines() if versions else [],
        "code_sha256": {str(p.relative_to(ROOT)): sha(p) for p in sorted(RUNS.rglob("*"))
                        if p.is_file() and "__pycache__" not in p.parts},
        "vm_snapshot_sha256": Path(snapshot).read_text(encoding="utf-8").splitlines() if snapshot else [],
        "plan": json.loads(plan.read_text(encoding="utf-8")) if plan else None,
        "files": sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())[:400],
        "deviations": COMMON + DEVIATIONS.get(kind, []) + list(extra),
    }
    metrics = dest / "metrics.json"
    if metrics.exists():
        doc["metrics"] = json.loads(metrics.read_text(encoding="utf-8"))
    return doc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m neurobot.sim.runs.manifest", description=__doc__.split("\n\n")[0])
    p.add_argument("--dir", required=True, help="каталог результатов на хосте")
    p.add_argument("--kind", required=True, choices=("a0", "a1", "start", "budget", "long"), help="вид прогона")
    p.add_argument("--versions", default="", help="версии ВМ (run.sh versions)")
    p.add_argument("--snapshot", default="", help="список sha256 снимка кода в ВМ")
    p.add_argument("--command", action="append", default=[], help="команда прогона (можно несколько)")
    p.add_argument("--deviation", action="append", default=[], help="ещё отклонение (можно несколько)")
    a = p.parse_args(argv)
    dest = Path(a.dir)
    if not dest.is_dir():
        print("manifest: нет каталога %s" % dest, file=sys.stderr)
        return 2
    doc = build(dest, a.kind, a.versions, a.snapshot, a.command, a.deviation)
    with open(os.path.join(a.dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

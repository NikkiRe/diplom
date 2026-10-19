"""Причины отказов: код, раздел RUNBOOK и строка журнала, по которой doctor находит причину.

Все наши инструменты пишут причину одной строкой в stderr (stdout не трогаем — PC2):
    neurobot-<инструмент>: <компонент>: <код>: <текст>
Если stderr — журнал (JOURNAL_STREAM совпадает с fd 2), в начале строки ставится приоритет syslog <3>
(ошибка) или <4> (предупреждение); doctor ищет в журнале юнита последнюю такую строку с приоритетом ≤ 3.
"""
from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple, Optional

RUNBOOK = "RUNBOOK.md"
# код -> (раздел RUNBOOK, что сделать)
CODES = {
    "not_installed": ("1", "установить пакет: sudo ./install.sh"),
    "not_running": ("2", "запустить: sudo systemctl start neurobot.target"),
    "starting": ("2", "подождать; если дольше пары минут — journalctl -u <юнит>"),
    "env_invalid": ("3", "исправить /etc/neurobot/neurobot.env"),
    "ros_unavailable": ("3", "проверить NEUROBOT_ROS_SETUP и ROS_DOMAIN_ID в env-файле"),
    "camera_missing": ("4.1", "проверить кабель USB 3 и питание камеры; lsusb | grep 8086"),
    "camera_usb2": ("4.1", "переткнуть камеру в порт USB 3 (синий)"),
    "camera_busy": ("4.1", "камеру держит другой процесс: остановить его, затем sudo systemctl restart "
                           "neurobot-perception"),
    "weights_missing": ("4.2", "вернуть файл весов на место (пути — в weights.sha256)"),
    "weights_hash": ("4.2", "веса не те: вернуть исходный файл или обновить weights.sha256"),
    "port_busy": ("4.3", "найти и остановить владельца порта: sudo ss -ltnp"),
    "foreign_process": ("4.4", "остановить ручной запуск (терминал, screen, nohup) — компоненты запускает systemd"),
    "cmd_vel_writer": ("4.4", "второй писатель скорости: остановить ручной запуск демо или скрипт агента; если это "
                              "исполнитель или распознавание — вернуть ремап /cmd_vel в env-файле"),
    "node_missing": ("4.4", "узла компонента нет в графе: проверить NEUROBOT_<КОМПОНЕНТ>_NODE и ROS_DOMAIN_ID"),
    "cmd_vel_override": ("4.4", "остановить издателя /cmd_vel_tracker: twist_mux ставит его выше safety_gate"),
    "cmd_vel_bypass": ("4.4", "в вход базы пишет не twist_mux: остановить этот узел"),
    "cmd_vel_joy": ("4.4", "геймпад перехватывает управление: во время демо не трогать"),
    "twist_mux_missing": ("4.4", "twist_mux не слушает /cmd_vel: проверить launch базы (NEUROBOT_DRIVER_base_CMD)"),
    "config_invalid": ("4.5", "исправить конфиг по тексту причины"),
    "unsupervised_launch": ("4.5", "ros2 launch — через обёртку supervised.launch.py (env-файл, README deploy)"),
    "device_missing": ("4.6", "проверить кабель устройства и права (группа dialout)"),
    "topic_timeout": ("4.6", "драйвер не публикует тему: journalctl -u <юнит>"),
    "scan_stale": ("4.6", "нет свежего /scan: вперёд робот не поедет; проверить лидар"),
    "scan_publishers": ("4.6", "лидар запущен дважды: оставить один драйвер (ros2 node list | grep urg)"),
    "scan_blind": ("4.6", "проверить листом картона в 0,5 м перед роботом; если закрыт — поднять лидар"),
    "scan_frame": ("4.6", "поправить frame_id драйвера лидара или NEUROBOT_SCAN_FRAME"),
    "sensor_missing": ("4.6", "датчик не публикуется: кто его драйвер — env-файл, journalctl -u neurobot-driver@<имя>"),
    "sensor_stale": ("4.6", "датчик молчит или медленный: journalctl -u neurobot-driver@<имя>"),
    "port_timeout": ("4.7", "rosbridge не открыл порт: journalctl -u neurobot-rosbridge"),
    "rosbridge_down": ("4.7", "rosbridge не отвечает: sudo systemctl restart neurobot-rosbridge"),
    "mcp_timeout": ("4.8", "MCP-сервер не ответил: journalctl -u neurobot-mcp"),
    "mcp_down": ("4.8", "MCP-сервер не отвечает: sudo systemctl restart neurobot-mcp"),
    "mcp_no_rosbridge": ("4.8", "MCP-сервер жив, но не достаёт rosbridge: см. 4.7"),
    "mcp_worker_down": ("4.8", "MCP-сервер жив, но его воркер не отвечает: см. 4.13"),
    "mcp_schema": ("4.8", "наложить патч прокси (mcp/robot_vision_mcp_proxy.patch): дубли инструментов движения"),
    "agent_zombie": ("4.9", "PicoClaw стартовал без MCP: sudo systemctl restart neurobot-picoclaw"),
    "agent_mcp_lost": ("4.9", "PicoClaw потерял MCP: sudo systemctl restart neurobot-picoclaw"),
    "agent_channel_lost": ("4.9", "канал VK оглох: sudo systemctl restart neurobot-picoclaw (сторож сделает сам)"),
    "agent_hung": ("4.9", "вызов инструмента висит: сторож перезапустит MCP-сервер, затем PicoClaw"),
    "agent_timeout": ("4.9", "PicoClaw не подключился к MCP: journalctl -u neurobot-picoclaw"),
    "safety_silent": ("4.10", "safety_gate не публикует состояние: journalctl -u neurobot-safety"),
    "safety_blocked": ("4.10", "движение запрещено safety_gate: устранить причину из списка"),
    "gate_missing": ("4.10", "safety_gate не слушает /neurobot/cmd_vel_req: см. neurobot doctor, строку safety"),
    "executor_silent": ("4.10", "исполнитель молчит: journalctl -u neurobot-executor"),
    "zero_undelivered": ("4.10", "явный ноль при остановке safety_gate некому принять: twist_mux не слушал /cmd_vel"),
    "perception_silent": ("4.11", "нет /neurobot/perception_health: journalctl -u neurobot-perception"),
    "perception_stalled": ("4.11", "камера восстанавливается или кадров нет: см. 4.1"),
    "python_env": ("4.12", "поставить недостающее в окружение компонента или указать другой интерпретатор"),
    "labworker_timeout": ("4.13", "воркер не стал готов: journalctl -u neurobot-labworker@<имя>"),
    "labworker_down": ("4.13", "воркер не отвечает: sudo systemctl restart neurobot-labworker@<имя>"),
    "estop_engaged": ("4.14", "устранить причину и снять защёлку человеком: sudo neurobot estop release"),
    "estop_failed": ("4.14", "safety_gate не ответил: sudo neurobot doctor; остановить робот — кнопкой"),
    "estop_denied": ("4.14", "снимать защёлку — в терминале, человеком, через sudo"),
    "ui_down": ("4.15", "пульт не отвечает (роботу не мешает): journalctl -u neurobot-ui; sudo systemctl restart "
                        "neurobot-ui"),
    "ui_publisher": ("4.15", "узел пульта пишет не только стоп и защёлку: sudo systemctl stop neurobot-ui, проверить "
                             "код пульта и NEUROBOT_UI_CMD"),
    "ui_denied": ("4.15", "ссылку входа в пульт выдаёт только root: sudo neurobot ui-url"),
    "ui_codes_unsafe": ("4.15", "каталог кодов пульта подменён (ссылка или чужой владелец): sudo systemctl stop "
                                "neurobot-ui, проверить /var/lib/neurobot/ui"),
    "watchdog": ("5", "процесс завис (нет WATCHDOG): systemd перезапустил его; стек — в журнале"),
    "crashed": ("5", "процесс упал: journalctl -u <юнит> -n 50"),
    "start_timeout": ("5", "не стал готов за TimeoutStartSec: journalctl -u <юнит>"),
    "systemd_error": ("5", "systemctl или journalctl отказали: journalctl -u neurobot-watchdog"),
}
LINE = re.compile(r"neurobot-([a-z-]+): (\S+): ([a-z0-9_]+): (.*)")


class Reason(NamedTuple):
    tool: str
    component: str
    code: str
    text: str


def hint(code: str) -> str:
    section, todo = CODES.get(code, ("6", "см. журнал юнита"))
    return "{} ({} §{})".format(todo, RUNBOOK, section)


def parse(message: str) -> Optional[Reason]:
    m = LINE.fullmatch(message.strip())
    return Reason(*m.groups()) if m else None


def _stderr_is_journal() -> bool:
    stream = os.environ.get("JOURNAL_STREAM", "")
    try:
        st = os.fstat(sys.stderr.fileno())
    except (OSError, ValueError):
        return False
    return stream == "{}:{}".format(st.st_dev, st.st_ino)


def emit(tool: str, component: str, code: str, text: str, level: str = "err") -> None:
    """Строка причины в stderr; level: err | warning | info."""
    prio = {"err": "<3>", "warning": "<4>", "info": "<6>"}[level] if _stderr_is_journal() else ""
    print("{}neurobot-{}: {}: {}: {}".format(prio, tool, component, code, text), file=sys.stderr, flush=True)

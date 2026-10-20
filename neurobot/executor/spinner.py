"""Постоянный исполнитель rclpy в своём потоке вместо rclpy.spin_once(node) в главном цикле (H6, O3 п. 1).

rclpy.spin_once(node) на каждом вызове добавляет узел в глобальный исполнитель и убирает его: оба раза срабатывает
guard и набор ожидания собирается заново, а таймаут — новый rcl-таймер на каждую пачку событий (rclpy foxy:
__init__.py, executors.py). Здесь узел добавляется один раз, ожидание идёт без таймаута (как в ex.spin()), а
разбудить поток для остановки — своё guard condition узла.

Исключение колбэка останавливает поток и ставит stop: wait() в главном потоке поднимает его дальше, как раньше
поднимал spin_once. Ноль на выходе по-прежнему шлёт главный поток — после close(), когда колбэков уже нет.
API rclpy — только то, что есть в Foxy (SingleThreadedExecutor, Node.create_guard_condition); Python ≥ 3.8.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)


class Spinner:
    """Поток исполнителя одного узла; stop — общее событие выхода (его ставят и сигнал, и упавший колбэк)."""

    def __init__(self, node, stop: threading.Event):
        import rclpy
        from rclpy.executors import SingleThreadedExecutor

        self._ok = rclpy.ok
        self._stop = stop
        self._closed = False
        self.error: Optional[BaseException] = None
        self._ex = SingleThreadedExecutor()
        self._ex.add_node(node)
        self._wake = node.create_guard_condition(lambda: None)
        self._thread = threading.Thread(target=self._run, name="rclpy-spin", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set() and self._ok():
                self._ex.spin_once()
        except BaseException as e:  # колбэк упал: главный поток выйдет с этим исключением
            self.error = e
        finally:
            self._stop.set()

    def wait(self) -> None:
        """Ждать выхода (сигнал, упавший колбэк, закрытый контекст rclpy) и остановить поток; исключение колбэка —
        дальше, вызывающему."""
        self._stop.wait()
        self.close()
        if self.error is not None:
            raise self.error

    def close(self, timeout: float = 2.0) -> None:
        """Остановить поток и исполнитель (повторный вызов ничего не делает). Колбэк, не вернувшийся за timeout, —
        строка журнала: ноль на выходе главный поток шлёт всё равно."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread.is_alive():
            self._wake.trigger()
            self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("колбэк rclpy не вернулся за %.0f с — выход без остановки исполнителя", timeout)
            return
        self._ex.shutdown()

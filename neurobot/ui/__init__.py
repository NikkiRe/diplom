"""Пульт оператора робота NeuroChip: формат потока, тревоги, проигрыватель «Запись» и живой шлюз (docs/ui/PLAN.md).

Python 3.8, только stdlib (+ rclpy у узла шлюза). Пульт — «окно, а не руль»: в «Записи» он ничего не публикует; живой
шлюз пишет только /neurobot/stop_now и /neurobot/estop {engage} (live.PUBLISHERS).
"""

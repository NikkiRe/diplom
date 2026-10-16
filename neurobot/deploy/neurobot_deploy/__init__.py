"""Пакет запуска neurobot (этапы Э2, Э2.5): юниты systemd, проверки перед стартом, doctor, аварийная защёлка, сторож,
start_test.

Код едет на робот: Python 3.8 (Ubuntu 20.04 + ROS 2 Foxy) и 3.12 на стенде (Jazzy), только stdlib;
rclpy импортируется лениво и только в rosgraph.py. Описание и флаги — neurobot/deploy/README.md.
"""
__version__ = "0.2.0"

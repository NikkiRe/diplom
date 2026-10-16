"""Минимальный MQTT: сопоставление тем, брокер-заглушка, клиент (и совместимость с mosquitto, если он есть)."""
import shutil
import socket
import subprocess
import threading
import time

import pytest

from bench.baselines.mqttlite import BrokerThread, MQTTClient, encode_varint, topic_matches


def test_varint():
    assert encode_varint(0) == b"\x00"
    assert encode_varint(127) == b"\x7f"
    assert encode_varint(128) == b"\x80\x01"
    assert encode_varint(16383) == b"\xff\x7f"


def test_topic_matches():
    assert topic_matches("/picoclaw/edge/+/response", "/picoclaw/edge/user/response")
    assert not topic_matches("/picoclaw/edge/+/response", "/picoclaw/edge/user/request")
    assert topic_matches("/picoclaw/#", "/picoclaw/edge/x/y")
    assert topic_matches("a/b", "a/b")
    assert not topic_matches("a/b", "a/b/c")
    assert not topic_matches("a/+", "a")


def _roundtrip(port: int):
    got = []
    ev = threading.Event()

    def on(topic, payload):
        got.append((topic, payload.decode()))
        if len(got) >= 2:
            ev.set()

    sub = MQTTClient("127.0.0.1", port, "sub", keepalive=2, on_message=on)
    sub.connect()
    sub.subscribe("/picoclaw/edge/+/response")
    pub = MQTTClient("127.0.0.1", port, "pub", keepalive=0)
    pub.connect()
    pub.publish("/picoclaw/edge/user/response", '{"text": "привет"}')
    pub.publish("/picoclaw/edge/user/request", "skip")
    pub.publish("/picoclaw/edge/sensors/response", "x" * 5000, qos=1)
    assert ev.wait(5), got
    time.sleep(2.5)                     # keep-alive: PINGREQ уходит, соединение живо
    assert sub.error is None
    pub.close()
    sub.close()
    assert got[0] == ("/picoclaw/edge/user/response", '{"text": "привет"}')
    assert got[1][0] == "/picoclaw/edge/sensors/response" and len(got[1][1]) == 5000
    assert sub.bytes_in > 5000 and pub.bytes_out > 5000


def test_broker_thread_roundtrip():
    with BrokerThread() as b:
        _roundtrip(b.port)
        assert b.broker.published == 3
        assert b.broker.delivered == 2


@pytest.mark.skipif(shutil.which("mosquitto") is None, reason="mosquitto not installed")
def test_client_against_mosquitto(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    conf = tmp_path / "m.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    p = subprocess.Popen(["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        _roundtrip(port)
    finally:
        p.terminate()
        p.wait(5)

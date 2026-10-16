"""Минимальный MQTT 3.1.1 (только стандартная библиотека): клиент для драйвера PicoClaw и брокер-заглушка.

Клиент: CONNECT (clean session), SUBSCRIBE (QoS 0/1), PUBLISH QoS 0 (и приём QoS 1 с PUBACK), PINGREQ по
keep-alive, счётчики байт. Брокер: то же с другой стороны, подстановки `+`/`#`, без retain/will/QoS 2 и
без сохранения сессий. Брокер нужен там, где нет mosquitto (тесты, ВМ без apt-пакетов); в прогонах на Mac
используется mosquitto (brew), он же — штатный брокер в документации PicoClaw (docs/channels/mqtt).

    python -m bench.baselines.mqttlite broker --port 18122          # брокер
    python -m bench.baselines.mqttlite pub --port 18122 -t /a -m hi # публикация
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import threading
import time
from collections.abc import Callable

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK = 8, 9, 10, 11
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14


class MQTTError(RuntimeError):
    pass


def encode_varint(n: int) -> bytes:
    if n < 0 or n > 268_435_455:
        raise ValueError("remaining length out of range")
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def encode_str(s: str | bytes) -> bytes:
    b = s.encode("utf-8") if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | (flags & 0x0F)]) + encode_varint(len(body)) + body


def topic_matches(filt: str, topic: str) -> bool:
    """Сопоставление темы с фильтром MQTT (`+` — один уровень, `#` — остаток)."""
    fl = filt.split("/")
    tl = topic.split("/")
    for i, f in enumerate(fl):
        if f == "#":
            return True
        if i >= len(tl):
            return False
        if f != "+" and f != tl[i]:
            return False
    return len(fl) == len(tl)


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def read_packet(sock: socket.socket) -> tuple[int, int, bytes, int]:
    """(тип, флаги, тело, байт всего) из блокирующего сокета."""
    first = _read_exact(sock, 1)[0]
    mult, length, nbytes = 1, 0, 1
    while True:
        b = _read_exact(sock, 1)[0]
        nbytes += 1
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
        if mult > 128 ** 3:
            raise MQTTError("malformed remaining length")
    body = _read_exact(sock, length) if length else b""
    return first >> 4, first & 0x0F, body, nbytes + length


def parse_publish(flags: int, body: bytes) -> tuple[str, bytes, int, int | None]:
    (tlen,) = struct.unpack("!H", body[:2])
    topic = body[2:2 + tlen].decode("utf-8")
    pos = 2 + tlen
    qos = (flags >> 1) & 0x03
    pid = None
    if qos:
        (pid,) = struct.unpack("!H", body[pos:pos + 2])
        pos += 2
    return topic, body[pos:], qos, pid


# --------------------------------------------------------------------------- клиент


class MQTTClient:
    """Синхронный клиент: поток чтения + keep-alive. on_message(topic, payload: bytes)."""

    def __init__(self, host: str, port: int, client_id: str, *, keepalive: int = 60,
                 on_message: Callable[[str, bytes], None] | None = None):
        self.host, self.port, self.client_id, self.keepalive = host, int(port), client_id, int(keepalive)
        self.on_message = on_message
        self.sock: socket.socket | None = None
        self._wlock = threading.Lock()
        self._pid = 0
        self._acks: dict[int, threading.Event] = {}
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._pinger: threading.Thread | None = None
        self.bytes_in = 0
        self.bytes_out = 0
        self.error: BaseException | None = None

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise MQTTError("not connected")
        with self._wlock:
            self.sock.sendall(data)
            self.bytes_out += len(data)

    def _next_pid(self) -> int:
        self._pid = self._pid % 65535 + 1
        return self._pid

    def connect(self, timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        body = encode_str("MQTT") + bytes([4, 0x02]) + struct.pack("!H", self.keepalive) + encode_str(self.client_id)
        self._send(packet(CONNECT, 0, body))
        ptype, _, rest, n = read_packet(self.sock)
        self.bytes_in += n
        if ptype != CONNACK or len(rest) < 2 or rest[1] != 0:
            raise MQTTError(f"CONNACK refused: type={ptype} body={rest!r}")
        self.sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, name=f"mqtt-read-{self.client_id}", daemon=True)
        self._reader.start()
        if self.keepalive > 0:
            self._pinger = threading.Thread(target=self._ping_loop, name=f"mqtt-ping-{self.client_id}", daemon=True)
            self._pinger.start()

    def _read_loop(self) -> None:
        try:
            while not self._stop.is_set():
                ptype, flags, body, n = read_packet(self.sock)
                self.bytes_in += n
                if ptype == PUBLISH:
                    topic, payload, qos, pid = parse_publish(flags, body)
                    if qos == 1 and pid is not None:
                        self._send(packet(PUBACK, 0, struct.pack("!H", pid)))
                    if self.on_message is not None:
                        try:
                            self.on_message(topic, payload)
                        except Exception as e:      # noqa: BLE001 — обработчик не должен ронять поток
                            self.error = e
                elif ptype in (SUBACK, UNSUBACK, PUBACK):
                    (pid,) = struct.unpack("!H", body[:2])
                    ev = self._acks.pop(pid, None)
                    if ev is not None:
                        ev.set()
        except (OSError, ConnectionError, MQTTError) as e:
            if not self._stop.is_set():
                self.error = e

    def _ping_loop(self) -> None:
        period = max(1.0, self.keepalive / 2.0)
        while not self._stop.wait(period):
            try:
                self._send(packet(PINGREQ, 0, b""))
            except OSError as e:
                self.error = e
                return

    def subscribe(self, topic_filter: str, qos: int = 0, timeout: float = 5.0) -> None:
        pid = self._next_pid()
        ev = threading.Event()
        self._acks[pid] = ev
        self._send(packet(SUBSCRIBE, 0x02, struct.pack("!H", pid) + encode_str(topic_filter) + bytes([qos & 1])))
        if not ev.wait(timeout):
            raise MQTTError(f"SUBACK timeout for {topic_filter}")

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, timeout: float = 5.0) -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        if qos == 0:
            self._send(packet(PUBLISH, 0, encode_str(topic) + data))
            return
        pid = self._next_pid()
        ev = threading.Event()
        self._acks[pid] = ev
        self._send(packet(PUBLISH, 0x02, encode_str(topic) + struct.pack("!H", pid) + data))
        if not ev.wait(timeout):
            raise MQTTError("PUBACK timeout")

    def close(self) -> None:
        self._stop.set()
        if self.sock is not None:
            try:
                self._send(packet(DISCONNECT, 0, b""))
            except OSError:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


# --------------------------------------------------------------------------- брокер


class Broker:
    """Брокер QoS 0/1 без сохранения сессий: достаточно для каналов MQTT PicoClaw и тестов."""

    def __init__(self) -> None:
        self.subs: dict[asyncio.StreamWriter, set[str]] = {}
        self.server: asyncio.base_events.Server | None = None
        self.published = 0
        self.delivered = 0

    async def _read_packet(self, reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
        first = (await reader.readexactly(1))[0]
        mult, length = 1, 0
        while True:
            b = (await reader.readexactly(1))[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        body = await reader.readexactly(length) if length else b""
        return first >> 4, first & 0x0F, body

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            ptype, _, _ = await self._read_packet(reader)
            if ptype != CONNECT:
                return
            writer.write(packet(CONNACK, 0, b"\x00\x00"))
            await writer.drain()
            self.subs[writer] = set()
            while True:
                ptype, flags, body = await self._read_packet(reader)
                if ptype == PUBLISH:
                    topic, payload, qos, pid = parse_publish(flags, body)
                    if qos == 1 and pid is not None:
                        writer.write(packet(PUBACK, 0, struct.pack("!H", pid)))
                    self.published += 1
                    out = packet(PUBLISH, 0, encode_str(topic) + payload)
                    for w, filters in list(self.subs.items()):
                        if any(topic_matches(f, topic) for f in filters):
                            try:
                                w.write(out)
                                self.delivered += 1
                            except (ConnectionError, RuntimeError):
                                pass
                elif ptype == SUBSCRIBE:
                    pid = body[:2]
                    pos, codes = 2, bytearray()
                    while pos < len(body):
                        (n,) = struct.unpack("!H", body[pos:pos + 2])
                        filt = body[pos + 2:pos + 2 + n].decode("utf-8")
                        qos = body[pos + 2 + n]
                        pos += 3 + n
                        self.subs[writer].add(filt)
                        codes.append(min(qos, 1))
                    writer.write(packet(SUBACK, 0, pid + bytes(codes)))
                elif ptype == UNSUBSCRIBE:
                    pid = body[:2]
                    pos = 2
                    while pos < len(body):
                        (n,) = struct.unpack("!H", body[pos:pos + 2])
                        self.subs[writer].discard(body[pos + 2:pos + 2 + n].decode("utf-8"))
                        pos += 2 + n
                    writer.write(packet(UNSUBACK, 0, pid))
                elif ptype == PINGREQ:
                    writer.write(packet(PINGRESP, 0, b""))
                elif ptype == DISCONNECT:
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            self.subs.pop(writer, None)
            try:
                writer.close()
            except RuntimeError:
                pass

    async def start(self, host: str, port: int) -> int:
        self.server = await asyncio.start_server(self._handle, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def serve_forever(self, host: str, port: int) -> None:
        bound = await self.start(host, port)
        print(f'{{"event": "listening", "host": "{host}", "port": {bound}}}', flush=True)
        async with self.server:
            await self.server.serve_forever()


class BrokerThread:
    """Брокер в фоновом потоке (для тестов и драйвера): with BrokerThread() as b: b.port."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host, self.port = host, port
        self.broker = Broker()
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mqttlite-broker", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.port = self.loop.run_until_complete(self.broker.start(self.host, self.port))
        self._ready.set()
        self.loop.run_forever()

    def __enter__(self) -> "BrokerThread":
        self._thread.start()
        if not self._ready.wait(5):
            raise MQTTError("broker did not start")
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        def _close() -> None:
            if self.broker.server is not None:
                self.broker.server.close()
            for w in list(self.broker.subs):
                w.close()
            self.loop.stop()

        if self.loop.is_running():
            self.loop.call_soon_threadsafe(_close)
            self._thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.baselines.mqttlite")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("broker")
    b.add_argument("--host", default="127.0.0.1")
    b.add_argument("--port", type=int, default=18122)
    p = sub.add_parser("pub")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18122)
    p.add_argument("-t", "--topic", required=True)
    p.add_argument("-m", "--message", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "broker":
        try:
            asyncio.run(Broker().serve_forever(a.host, a.port))
        except KeyboardInterrupt:
            pass
        return 0
    c = MQTTClient(a.host, a.port, f"pub-{int(time.time() * 1000)}", keepalive=0)
    c.connect()
    c.publish(a.topic, a.message)
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

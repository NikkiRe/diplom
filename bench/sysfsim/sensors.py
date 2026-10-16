"""Устройства платы симулятора и модели датчиков: квантование и текст атрибутов sysfs, как у драйверов Linux.

DEVICES — единственное описание «проводки» платы: имя устройства у hw-mcp (при configs/declared.sim.json),
модель, тепловая зона, каталог атрибутов и ссылка шины (её снятие — отключение) или линия GPIO.
Каталоги взяты из фикстуры hwmcp/testdata/fixture_root (по ней tree.init_root создаёт корень).

Модель датчика sysfs (MODELS) — класс с полями
  props      — свойства у hw-mcp;
  noise      — СКО шума по свойствам в единицах SenML (Cel, %RH, Pa, V);
  static     — неизменные атрибуты (пишет init);
  unreadable — что видит читатель при сбое чтения: None — ошибку чтения (show() драйвера вернул -EIO),
               "" — пустое чтение (так отвечает w1_therm);
и методами encode(значения) → {атрибут: текст} (код АЦП датчика и формат драйвера) и read(атрибуты) →
{свойство: значение} — так, как атрибуты прочтёт hw-mcp (*_input, иначе raw × scale; затем round(…, 6)).
PARAMS — параметры, которые сценарий может переопределить в devices.<имя>. Источники чисел и допущения —
README.md, раздел «Модели датчиков».
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    name: str               # имя у hw-mcp при configs/declared.sim.json
    model: str              # ключ MODELS или pir | reed | relay
    zone: str | None = None  # тепловая зона (physics.ZONES), для реле — зона вентилятора
    dir: str = ""           # каталог атрибутов относительно корня
    link: str = ""          # ссылка шины: её нет — устройство отключено
    line: int = -1          # линия GPIO


I2C1 = "sys/devices/platform/soc/1f00074000.i2c/i2c-1"

DEVICES = (
    Device("kitchen_bme280", "bme280", "kitchen", f"{I2C1}/1-0076/iio:device0", "sys/bus/iio/devices/iio:device0"),
    Device("bathroom_dht11", "dht11", "bathroom", "sys/devices/platform/dht11@4/iio:device1",
           "sys/bus/iio/devices/iio:device1"),
    Device("bathroom_leak", "ads1115", "bathroom", f"{I2C1}/1-0048/iio:device2", "sys/bus/iio/devices/iio:device2"),
    Device("boiler_pipe_ds18b20", "ds18b20", "boiler_pipe", "sys/devices/w1_bus_master1/28-0316a2795cff",
           "sys/bus/w1/devices/28-0316a2795cff"),
    Device("board_cpu_thermal", "soc", "board", "sys/devices/virtual/thermal/thermal_zone0"),
    Device("pir_hall", "pir", line=17),
    Device("door_entry", "reed", line=22),
    Device("relay_fan", "relay", "kitchen", line=27),
)
BY_NAME = {d.name: d for d in DEVICES}

PARAMS = {
    "bme280": {"period_s": 1.0},
    "dht11": {"period_s": 2.0, "fail_rate": 0.05},
    "ds18b20": {"period_s": 0.75},
    "ads1115": {"period_s": 1.0, "v_dry": 0.05, "v_wet": 1.6, "tau_wet_s": 3.0, "tau_dry_s": 600.0},
    "soc": {"period_s": 1.0},
    "pir": {"hold_s": 5.0, "block_s": 2.5, "retrigger": True, "rate_hz": 0.3, "false_per_h": 0.5},
    "reed": {"bounce_ms": 0.5, "bounce_pulses": 2},
    "relay": {},
}
# Обновление не чаще, чем позволяет датчик: преобразование DS18B20 750 мс, период опроса DHT11 ≥ 1 с.
MIN_PERIOD_S = {"ds18b20": 0.75, "dht11": 1.0}


def clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def iio_fractional(num: int, den: int) -> str:
    """Текст IIO_VAL_FRACTIONAL, как iio_format_value() ядра: num/den с 9 знаками, отбрасывание к нулю."""
    q = abs(num) * 10**9 // den
    sign = "-" if num < 0 and q else ""
    return f"{sign}{q // 10**9}.{q % 10**9:09d}"


def senml(text: str, k: float) -> float:
    """Значение атрибута в единицах SenML так, как его считает hw-mcp: round(число × k, 6)."""
    return round(float(text) * k, 6)


class BME280:
    """Bosch BME280, драйвер bmp280 (IIO) ядер ≥ 6.12: код температуры 0,01 °C, влажности Q22.10 %RH, давления
    Q24.8 Па; *_input — PROCESSED (м°C, м%RH, кПа; IIO_VAL_FRACTIONAL), *_raw × *_scale — то же значение.

    До 6.11 включительно (и в ядре ВМ sbc 6.8) есть только *_input: температура и влажность целые (IIO_VAL_INT,
    влажность 1000·код/1024 с отбрасыванием), raw и scale нет. hw-mcp читает *_input; разница < 0,001 %RH.
    """
    props = ("temperature", "humidity", "pressure")
    noise = {"temperature": 0.004, "humidity": 0.02, "pressure": 1.3}
    unreadable = None
    static = {"in_temp_scale": "10.000000000", "in_humidityrelative_scale": "0.976562500",
              "in_pressure_scale": "0.000003906", "in_temp_oversampling_ratio": "2",
              "in_humidityrelative_oversampling_ratio": "16", "in_pressure_oversampling_ratio": "16"}

    def encode(self, v: dict[str, float]) -> dict[str, str]:
        t = round(clamp(v["temperature"], -40, 85) * 100)
        h = round(clamp(v["humidity"], 0, 100) * 1024)
        p = round(clamp(v["pressure"], 30000, 110000) * 256)
        return {"in_temp_raw": str(t), "in_temp_input": iio_fractional(10 * t, 1),
                "in_humidityrelative_raw": str(h), "in_humidityrelative_input": iio_fractional(1000 * h, 1024),
                "in_pressure_raw": str(p), "in_pressure_input": iio_fractional(p, 256000)}

    def read(self, a: dict[str, str]) -> dict[str, float]:
        return {"temperature": senml(a["in_temp_input"], 1e-3),
                "humidity": senml(a["in_humidityrelative_input"], 1e-3),
                "pressure": senml(a["in_pressure_input"], 1e3)}


class DHT11:
    """DHT11, драйвер dht11 (IIO, только PROCESSED): целые °C и %RH × 1000, в пределах диапазона датчика."""
    props = ("temperature", "humidity")
    noise = {"temperature": 0.2, "humidity": 0.5}
    static: dict[str, str] = {}
    unreadable = None

    def encode(self, v: dict[str, float]) -> dict[str, str]:
        return {"in_temp_input": str(round(clamp(v["temperature"], 0, 50)) * 1000),
                "in_humidityrelative_input": str(round(clamp(v["humidity"], 20, 90)) * 1000)}

    def read(self, a: dict[str, str]) -> dict[str, float]:
        return {"temperature": senml(a["in_temp_input"], 1e-3),
                "humidity": senml(a["in_humidityrelative_input"], 1e-3)}


class DS18B20:
    """DS18B20 (12 бит, шаг 0,0625 °C), драйвер w1_therm: temperature = код × 1000 / 16 м°C (деление C — к нулю)."""
    props = ("temperature",)
    noise = {"temperature": 0.02}
    static: dict[str, str] = {}
    unreadable = ""   # temperature_show() при сбое обмена возвращает 0 байт

    def encode(self, v: dict[str, float]) -> dict[str, str]:
        code = round(clamp(v["temperature"], -55, 125) * 16)
        return {"temperature": str(math.trunc(code * 1000 / 16))}

    def read(self, a: dict[str, str]) -> dict[str, float]:
        return {"temperature": senml(a["temperature"], 1e-3)}


class ADS1115:
    """ADS1115 (16 бит), драйвер ti-ads1015: in_voltage0_raw, масштаб PGA ±2,048 В (умолчание драйвера) — 0,0625 мВ."""
    props = ("voltage0",)
    noise = {"voltage0": 62.5e-6}
    static = {"in_voltage0_scale": "0.062500000", "in_voltage0_sampling_frequency": "128"}
    unreadable = None
    lsb_v = 2.048 / 32768

    def encode(self, v: dict[str, float]) -> dict[str, str]:
        return {"in_voltage0_raw": str(clamp(round(v["voltage0"] / self.lsb_v), -32768, 32767))}

    def read(self, a: dict[str, str]) -> dict[str, float]:
        return {"voltage0": round(float(a["in_voltage0_raw"]) * float(self.static["in_voltage0_scale"]) * 1e-3, 6)}


class SoC:
    """Температура SoC: thermal_zone0/temp и hwmon0/temp1_input в м°C, шаг 0,5 °C (допущение)."""
    props = ("temperature1",)
    noise = {"temperature1": 0.1}
    static = {"type": "cpu-thermal"}
    unreadable = None

    def encode(self, v: dict[str, float]) -> dict[str, str]:
        milli = str(round(v["temperature1"] * 2) * 500)
        return {"temp": milli, "hwmon0/temp1_input": milli}

    def read(self, a: dict[str, str]) -> dict[str, float]:
        return {"temperature1": senml(a["hwmon0/temp1_input"], 1e-3)}


MODELS = {"bme280": BME280(), "dht11": DHT11(), "ds18b20": DS18B20(), "ads1115": ADS1115(), "soc": SoC()}
SENSORS = tuple(d for d in DEVICES if d.model in MODELS)
INPUTS = tuple(d for d in DEVICES if d.model in ("pir", "reed"))
RELAYS = tuple(d for d in DEVICES if d.model == "relay")


def props(device: Device) -> tuple[str, ...]:
    """Свойства устройства у hw-mcp: у датчиков — каналы модели, у линий GPIO — state."""
    return MODELS[device.model].props if device.model in MODELS else ("state",)

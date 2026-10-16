"""Детерминированная инвентаризация оборудования Linux по sysfs (без LLM).

Что умеет (только чтение файлов, без ioctl и без опроса шин):
  * IIO   /sys/bus/iio/devices/iio:deviceN   -> каналы с единицами из ABI ядра;
          значение = *_input, иначе (raw + offset) * scale (sysfs-bus-iio);
  * hwmon /sys/class/hwmon/hwmonN            -> tempN/inN/currN/powerN/humidityN;
  * 1-Wire /sys/bus/w1/devices/<fam>-<id>    -> temperature (1/1000 °C, w1_therm);
  * V4L2  /sys/class/video4linux/videoN      -> name, index (тип узла - только ioctl);
  * USB   /sys/bus/usb/devices/*             -> VID:PID, product (корневые хабы 1d6b скрыты);
  * I2C   /sys/bus/i2c/devices/N-00AA        -> клиенты без IIO/hwmon (только адрес и имя);
  * GPIO  /sys/bus/gpio/devices/gpiochipN    -> только список чипов (линии - ioctl/libgpiod);
  * HAT   /proc/device-tree/hat/*            -> vendor/product из EEPROM HAT.

Чего сознательно НЕ делает: i2cdetect-подобного сканирования шины (man i2cdetect
предупреждает, что опрос может сбить устройство с толку и испортить данные),
ioctl VIDIOC_QUERYCAP и GPIO_V2_GET_LINEINFO_IOCTL (на macOS их не проверить;
в README указано, как добавить через libgpiod/v4l2-ctl на Linux).

Каждому устройству ставится уровень «семантической наблюдаемости»
(термин Octopus, arXiv:2605.09055):
  A - ОС знает тип, каналы и единицы (IIO/hwmon/w1_therm с драйвером);
  B - ОС знает идентичность (имя, VID:PID), но не возможности;
  C - ОС знает только транспорт (адрес I2C, линия GPIO, tty) - нужен профиль.

Запуск: python3 sysfs_probe.py --root fixture_root [--declared declared.json]
На плате: python3 sysfs_probe.py --root /
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from typing import Any

# IIO: тип канала -> (единица ABI после scale/offset, единица SenML, множитель)
# Источник единиц ABI: Documentation/ABI/testing/sysfs-bus-iio (ядро 7.3-rc6).
# Единицы SenML: реестр RFC 8428 (Cel, %RH, Pa, lx, V, A, W, m/s2).
IIO_UNITS: dict[str, tuple[str, str, float]] = {
    "temp": ("milli degC", "Cel", 1e-3),
    "humidityrelative": ("milli percent", "%RH", 1e-3),
    "pressure": ("kPa", "Pa", 1e3),
    "illuminance": ("lux", "lx", 1.0),
    "voltage": ("mV", "V", 1e-3),
    "current": ("mA", "A", 1e-3),
    "power": ("mW", "W", 1e-3),
    "accel": ("m/s^2", "m/s2", 1.0),
}
IIO_NAMES = {"temp": "temperature", "humidityrelative": "humidity",
             "pressure": "pressure", "illuminance": "illuminance",
             "voltage": "voltage", "current": "current", "power": "power",
             "accel": "acceleration"}

# hwmon: префикс -> (единица ABI, SenML, множитель). Источник: sysfs-class-hwmon.
HWMON_UNITS = {
    "temp": ("millidegree Celsius", "Cel", 1e-3, "temperature"),
    "in": ("millivolt", "V", 1e-3, "voltage"),
    "curr": ("milliampere", "A", 1e-3, "current"),
    "power": ("microWatt", "W", 1e-6, "power"),
    "humidity": ("milli-percent", "%RH", 1e-3, "humidity"),
}

# w1_therm: код семейства -> чип (Documentation/w1/slaves/w1_therm.rst)
W1_THERM_FAMILIES = {"10": "DS18S20", "22": "DS1822", "28": "DS18B20",
                     "3b": "DS1825", "42": "DS28EA00"}

# Неполный справочник USB-классов по VID:PID оставлен пустым сознательно:
# идентификация по VID:PID - это уровень B, а не A.

IIO_ATTR = re.compile(
    r"^in_(?P<type>[a-z]+?)(?P<idx>\d*)(?P<mod>(?:_[xyz])?)_(?P<info>input|raw)$")


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace").strip().strip("\x00").strip()
    except OSError:
        return None


def _float(s: str | None) -> float | None:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def _real_parent(root: str, path: str) -> str:
    rp = os.path.realpath(path)
    return os.path.basename(os.path.dirname(rp))


def _i2c_label(parent: str) -> str | None:
    m = re.fullmatch(r"(\d+)-00([0-9a-f]{2})", parent)
    return f"i2c-{m.group(1)}/0x{m.group(2)}" if m else None


def iio_channel_value(d: str, base: str, typ: str) -> tuple[float | None, str]:
    """Значение канала в единицах ABI: *_input, иначе (raw + offset) * scale."""
    v = _float(_read(os.path.join(d, f"{base}_input")))
    if v is not None:
        return v, "input"
    raw = _float(_read(os.path.join(d, f"{base}_raw")))
    if raw is None:
        return None, "absent"
    # scale/offset бывают на канал (in_voltage0_scale) или на тип (in_voltage_scale)
    scale = _float(_read(os.path.join(d, f"{base}_scale")))
    if scale is None:
        scale = _float(_read(os.path.join(d, f"in_{typ}_scale")))
    offset = _float(_read(os.path.join(d, f"{base}_offset")))
    if offset is None:
        offset = _float(_read(os.path.join(d, f"in_{typ}_offset"))) or 0.0
    if scale is None:
        return None, "raw-without-scale"
    return (raw + offset) * scale, "(raw+offset)*scale"


def probe_iio(root: str) -> list[dict[str, Any]]:
    out = []
    for d in sorted(glob.glob(os.path.join(root, "sys/bus/iio/devices/iio:device*"))):
        name = _read(os.path.join(d, "name")) or "unknown"
        parent = _real_parent(root, d)
        bus = _i2c_label(parent) or parent
        chans, seen = [], set()
        for f in sorted(os.listdir(d)):
            m = IIO_ATTR.match(f)
            if not m:
                continue
            typ = m.group("type")
            base = f"in_{typ}{m.group('idx')}{m.group('mod')}"
            if base in seen or typ not in IIO_UNITS:
                continue
            seen.add(base)
            abi_unit, senml_unit, k = IIO_UNITS[typ]
            v, how = iio_channel_value(d, base, typ)
            cname = IIO_NAMES[typ] + m.group("idx") + m.group("mod")
            chans.append({
                "name": cname, "quantity": typ, "unit": senml_unit,
                "abi_unit": abi_unit,
                "value": None if v is None else round(v * k, 6),
                "how": how,
                "path": os.path.relpath(os.path.join(d, base + ("_input" if how == "input" else "_raw")), root),
                "writable": False,
            })
        out.append({"id": f"iio:{name}@{bus}", "source": "iio", "name": name,
                    "bus": bus, "observability": "A" if chans else "B",
                    "channels": chans})
    return out


def probe_hwmon(root: str) -> list[dict[str, Any]]:
    out = []
    for d in sorted(glob.glob(os.path.join(root, "sys/class/hwmon/hwmon*"))):
        name = _read(os.path.join(d, "name")) or "unknown"
        chans = []
        for f in sorted(os.listdir(d)):
            m = re.fullmatch(r"(temp|in|curr|power|humidity)(\d+)_input", f)
            if not m:
                continue
            abi_unit, senml_unit, k, qname = HWMON_UNITS[m.group(1)]
            v = _float(_read(os.path.join(d, f)))
            label = _read(os.path.join(d, f"{m.group(1)}{m.group(2)}_label"))
            chans.append({"name": label or f"{qname}{m.group(2)}", "quantity": m.group(1),
                          "unit": senml_unit, "abi_unit": abi_unit,
                          "value": None if v is None else round(v * k, 6),
                          "how": "input", "path": os.path.relpath(os.path.join(d, f), root),
                          "writable": False})
        out.append({"id": f"hwmon:{name}", "source": "hwmon", "name": name,
                    "bus": _real_parent(root, d), "observability": "A" if chans else "B",
                    "channels": chans})
    return out


def probe_w1(root: str) -> list[dict[str, Any]]:
    out = []
    for d in sorted(glob.glob(os.path.join(root, "sys/bus/w1/devices/*-*"))):
        sid = os.path.basename(d)
        fam = sid.split("-", 1)[0].lower()
        chip = W1_THERM_FAMILIES.get(fam)
        chans, notes = [], []
        t = _float(_read(os.path.join(d, "temperature")))
        if chip and t is not None:
            chans.append({"name": "temperature", "quantity": "temp", "unit": "Cel",
                          "abi_unit": "1/1000 degC", "value": round(t * 1e-3, 6),
                          "how": "temperature", "writable": False,
                          "path": os.path.relpath(os.path.join(d, "temperature"), root)})
            ct = _float(_read(os.path.join(d, "conv_time")))
            if ct:
                notes.append(f"blocking read: conversion {int(ct)} ms per sample")
        out.append({"id": f"w1:{sid}", "source": "w1", "name": chip or f"family 0x{fam}",
                    "bus": _real_parent(root, d),
                    "observability": "A" if chans else "B", "channels": chans,
                    "notes": notes})
    return out


def probe_v4l2(root: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for d in sorted(glob.glob(os.path.join(root, "sys/class/video4linux/video*"))):
        rp = os.path.realpath(d)
        # родитель - USB-интерфейс или платформенное устройство
        parent = os.path.dirname(os.path.dirname(rp))
        g = groups.setdefault(parent, {"nodes": [], "name": _read(os.path.join(d, "name"))})
        g["nodes"].append({"dev": os.path.basename(d),
                           "index": _read(os.path.join(d, "index"))})
    out = []
    for parent, g in groups.items():
        out.append({"id": f"v4l2:{g['name']}", "source": "v4l2", "name": g["name"],
                    "bus": os.path.basename(parent), "observability": "B",
                    "channels": [], "nodes": g["nodes"],
                    "notes": ["capture vs metadata node needs VIDIOC_QUERYCAP device_caps; "
                              "on Raspberry Pi CSI cameras use libcamera, not raw /dev/videoN"]})
    return out


def probe_usb(root: str) -> list[dict[str, Any]]:
    out = []
    for d in sorted(glob.glob(os.path.join(root, "sys/bus/usb/devices/*"))):
        vid, pid = _read(os.path.join(d, "idVendor")), _read(os.path.join(d, "idProduct"))
        if not vid or vid == "1d6b":   # 1d6b = Linux Foundation (корневые хабы)
            continue
        out.append({"id": f"usb:{vid}:{pid}@{os.path.basename(d)}", "source": "usb",
                    "name": _read(os.path.join(d, "product")) or f"{vid}:{pid}",
                    "vendor": _read(os.path.join(d, "manufacturer")),
                    "bus": os.path.basename(d), "observability": "B", "channels": []})
    return out


def probe_i2c_unbound(root: str, claimed: set[str]) -> list[dict[str, Any]]:
    out = []
    for d in sorted(glob.glob(os.path.join(root, "sys/bus/i2c/devices/*-00*"))):
        lab = _i2c_label(os.path.basename(d))
        if lab in claimed:
            continue
        out.append({"id": f"i2c:{lab}", "source": "i2c", "name": _read(os.path.join(d, "name")),
                    "bus": lab, "observability": "C", "channels": [],
                    "notes": ["no IIO/hwmon driver bound: only address is known"]})
    return out


def probe_gpio(root: str) -> list[dict[str, Any]]:
    return [{"id": f"gpiochip:{os.path.basename(d)}", "source": "gpio",
             "name": os.path.basename(d), "bus": None, "observability": "C",
             "channels": [], "notes": ["line names/flags via GPIO_V2_GET_LINEINFO_IOCTL (libgpiod)"]}
            for d in sorted(glob.glob(os.path.join(root, "sys/bus/gpio/devices/gpiochip*")))]


def probe_hat(root: str) -> list[dict[str, Any]]:
    d = os.path.join(root, "proc/device-tree/hat")
    if not os.path.isdir(d):
        return []
    f = {k: _read(os.path.join(d, k)) for k in ("vendor", "product", "product_id",
                                                "product_ver", "uuid")}
    return [{"id": f"hat:{f['product']}", "source": "hat", "name": f["product"],
             "vendor": f["vendor"], "bus": "ID_SD/ID_SC EEPROM", "observability": "B",
             "channels": [], "hat": f}]


def apply_declared(inv: list[dict[str, Any]], decl: dict[str, Any]) -> list[dict[str, Any]]:
    loc = decl.get("locations", {})
    for dev in inv:
        if dev["id"] in loc:
            dev["location"] = loc[dev["id"]]
    for g in decl.get("gpio", []):
        out = g["direction"] == "output"
        inv.append({"id": f"gpio:{g['id']}", "source": "declared", "name": g["id"],
                    "bus": f"{g['chip']}/line{g['line']}", "observability": "C",
                    "location": g.get("location"), "description": g.get("description"),
                    "channels": [{"name": "state", "quantity": g["quantity"], "unit": "/",
                                  "value": None, "how": "gpiod", "writable": out,
                                  "path": f"dev/{g['chip']}#{g['line']}"}]})
    for s in decl.get("serial", []):
        inv.append({"id": f"tty:{s['id']}", "source": "declared", "name": s["id"],
                    "bus": s["tty"], "observability": "C", "description": s.get("description"),
                    "channels": [], "notes": [s.get("observability_note", "")]})
    return inv


def probe(root: str, declared: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    inv = probe_iio(root) + probe_hwmon(root) + probe_w1(root)
    claimed = {d["bus"] for d in inv if d["source"] == "iio"}
    inv += probe_i2c_unbound(root, claimed) + probe_v4l2(root) + probe_usb(root)
    inv += probe_gpio(root) + probe_hat(root)
    if declared:
        inv = apply_declared(inv, declared)
    return inv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/")
    ap.add_argument("--declared")
    ap.add_argument("--out")
    a = ap.parse_args()
    decl = json.load(open(a.declared)) if a.declared else None
    inv = probe(a.root, decl)
    s = json.dumps(inv, ensure_ascii=False, indent=1)
    if a.out:
        open(a.out, "w").write(s)
    print(s)


if __name__ == "__main__":
    main()

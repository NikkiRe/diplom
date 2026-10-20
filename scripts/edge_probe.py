#!/usr/bin/env python3
"""Portable passive Linux board / local host passport. Python 3.8, stdlib only.

Copy this one file to a device and run: python3 edge_probe.py > board.json
No root, package installation, network requests, bus scans, GPIO writes or SSH.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

FORMAT = "edge-board/1"
MAX_FILE = 65536
MAX_READ = 2 * 1024 * 1024
MAX_FILES = 768
MAX_ITEMS = 128
MAX_OUTPUT = 1024 * 1024
MAX_SECONDS = 6
LABELS = {"identity": "Идентификация", "system": "ОС, CPU и память", "storage": "Накопители",
          "interfaces": "Сетевые интерфейсы", "usb": "USB", "i2c": "I²C", "spi": "SPI",
          "serial": "Последовательные порты", "gpio": "GPIO", "iio": "IIO", "hwmon": "Hwmon",
          "camera": "Камеры", "services": "Службы и MCP-кандидаты"}


class Probe:
    def __init__(self, root="/", system=None):
        self.root = Path(root).resolve()
        self.fixture = self.root != Path("/")
        self.system = system or ("Linux" if self.fixture else platform.system())
        self.started = time.monotonic()
        self.bytes, self.files = 0, 0
        self.coverage = {key: {"id": key, "label": label, "status": "unavailable", "count": 0,
                               "sources": [], "errors": [], "limited": False} for key, label in LABELS.items()}

    def budget(self, section):
        if self.files >= MAX_FILES or self.bytes >= MAX_READ or time.monotonic() - self.started > MAX_SECONDS:
            self.coverage[section]["limited"] = True
            self.coverage[section]["status"] = "limited"
            return False
        return True

    def source(self, section, path):
        sources = self.coverage[section]["sources"]
        if path not in sources and len(sources) < 32:
            sources.append(path)

    def error(self, section, path, code, message):
        cov = self.coverage[section]
        if len(cov["errors"]) < 12:
            cov["errors"].append({"source": path, "code": code, "message": message[:160]})
        if code == "permission_denied":
            cov["status"] = "partial" if cov["status"] == "ok" else "permission_denied"

    def path(self, path):
        result = self.root / path.lstrip("/")
        if self.fixture and self.root not in (result.resolve(), *result.resolve().parents):
            raise PermissionError("symlink escapes fixture root")
        return result

    def read(self, path, section, optional=True):
        self.source(section, path)
        if not self.budget(section):
            return None
        self.files += 1
        fd = None
        try:
            fd = os.open(str(self.path(path)), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = os.read(fd, min(MAX_FILE, MAX_READ - self.bytes) + 1)
            self.bytes += len(raw)
            if len(raw) > MAX_FILE:
                self.coverage[section]["limited"] = True
                self.error(section, path, "file_limit", "Файл превышает 64 KiB; поле не использовано")
                return None
            return raw.decode("utf-8", "replace").replace("\x00", " ").strip()
        except PermissionError:
            self.error(section, path, "permission_denied", "Нет разрешения на чтение")
        except FileNotFoundError:
            if not optional:
                self.error(section, path, "absent", "Источник отсутствует")
        except OSError as exc:
            self.error(section, path, "read_error", str(exc))
        finally:
            if fd is not None:
                os.close(fd)
        return None

    def listing(self, path, section, predicate=None):
        self.source(section, path)
        if not self.budget(section):
            return []
        try:
            rows = []
            for index, item in enumerate(self.path(path).iterdir()):
                if index >= 4096:
                    self.coverage[section]["limited"] = True
                    self.error(section, path, "directory_limit", "Просмотрено 4096 имён; перечисление ограничено")
                    break
                if predicate is not None and not predicate(item.name):
                    continue
                rows.append(item.name)
                if len(rows) > MAX_ITEMS:
                    self.coverage[section]["limited"] = True
                    self.error(section, path, "item_limit", "Больше 128 записей; перечисление ограничено")
                    rows = rows[:MAX_ITEMS]
                    break
            if self.coverage[section]["status"] == "unavailable":
                self.coverage[section]["status"] = "ok"
            return sorted(rows)
        except FileNotFoundError:
            return []
        except PermissionError:
            self.error(section, path, "permission_denied", "Нет разрешения на перечисление")
        except OSError as exc:
            self.error(section, path, "read_error", str(exc))
        return []

    def link_name(self, path):
        try:
            item = self.path(path)
            return item.resolve().name if item.is_symlink() else None
        except OSError:
            return None

    def fields(self, base, names, section):
        return {name: value for name in names if (value := self.read(base + "/" + name, section)) is not None}

    def command(self, args, section):
        if self.fixture or not self.budget(section):
            return None
        self.source(section, "command:" + " ".join(args))
        try:
            result = subprocess.run(args, capture_output=True, timeout=1, check=False)
            if result.returncode or len(result.stdout) > MAX_FILE:
                self.error(section, args[0], "command_failed", "Команда недоступна или ответ превышает лимит")
                return None
            return result.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.TimeoutExpired):
            self.error(section, args[0], "command_unavailable", "Команда недоступна или превышен таймаут")
            return None

    def collect(self):
        report = {"format": FORMAT, "collected_at": time.time(),
                  "collector": {"name": "edge-probe", "version": "1", "platform": self.system,
                                "mode": "filesystem_fixture" if self.fixture else "local", "passive": True},
                  "identity": {"kind": "unknown", "model": None, "manufacturer": None, "source": None, "evidence": []},
                  "system": {"os": {}, "architecture": platform.machine() if not self.fixture else None,
                             "cpu": {"model": None, "logical_cores": None, "hardware": None},
                             "memory": {"total_bytes": None, "available_bytes": None}, "uptime_s": None},
                  "storage": [], "interfaces": [], "buses": {key: [] for key in ("usb", "i2c", "spi", "serial", "gpio", "iio", "hwmon", "camera")},
                  "services": [], "mcp_candidates": [], "redacted": True,
                  "redaction": {"serial_numbers": "not_collected", "mac_addresses": "not_collected",
                                "ip_addresses": "not_collected", "credentials": "not_collected"}}
        if self.system == "Linux":
            self.linux(report)
        elif self.system == "Darwin":
            self.darwin(report)
        else:
            report["identity"]["kind"] = "host"
            report["system"]["os"] = {"name": self.system, "kernel": platform.release() if not self.fixture else None}
            for cov in self.coverage.values():
                cov["status"] = "unsupported"
        for key, cov in self.coverage.items():
            if key in report["buses"]:
                cov["count"] = len(report["buses"][key])
            elif key in ("storage", "interfaces", "services"):
                cov["count"] = len(report[key])
            if cov["limited"]:
                cov["status"] = "limited"
            elif cov["errors"] and cov["status"] == "ok":
                cov["status"] = "partial"
            elif cov["status"] == "unavailable" and any(error["code"] == "permission_denied" for error in cov["errors"]):
                cov["status"] = "permission_denied"
        report["coverage"] = list(self.coverage.values())
        report["limits"] = {"max_file_bytes": MAX_FILE, "max_read_bytes": MAX_READ, "max_files": MAX_FILES,
                            "max_items_per_category": MAX_ITEMS, "max_output_bytes": MAX_OUTPUT,
                            "read_bytes": self.bytes, "files_attempted": self.files, "duration_s": time.monotonic() - self.started}
        return report

    def linux(self, report):
        identity, system = report["identity"], report["system"]
        identity["kind"] = "linux_device"
        for base in ("/sys/firmware/devicetree/base", "/proc/device-tree"):
            model = self.read(base + "/model", "identity")
            if model:
                identity.update(model=model, source=base + "/model")
                identity["evidence"].append({"field": "model", "value": model, "source": base + "/model"})
                vendor = self.read(base + "/manufacturer", "identity")
                compatible = self.read(base + "/compatible", "identity")
                if vendor:
                    identity["manufacturer"] = vendor
                    identity["evidence"].append({"field": "manufacturer", "value": vendor, "source": base + "/manufacturer"})
                if compatible:
                    identity["compatible"] = compatible.split()
                    identity["vendor_id"] = compatible.split()[0].split(",", 1)[0] if "," in compatible.split()[0] else None
                    identity["evidence"].append({"field": "compatible", "value": compatible, "source": base + "/compatible"})
                break
        dmi = self.fields("/sys/class/dmi/id", ("sys_vendor", "product_name", "product_version", "board_vendor", "board_name"), "identity")
        placeholders = {"none", "unknown", "not specified", "to be filled by o.e.m.", "default string", "system product name", "system manufacturer"}
        dmi = {key: value for key, value in dmi.items() if value.strip().lower() not in placeholders}
        identity["dmi"] = dmi
        for field, names in (("model", ("product_name", "board_name")), ("manufacturer", ("sys_vendor", "board_vendor"))):
            if identity[field] is None:
                for name in names:
                    if dmi.get(name):
                        identity[field] = dmi[name]
                        source = "/sys/class/dmi/id/" + name
                        identity["evidence"].append({"field": field, "value": dmi[name], "source": source})
                        if field == "model":
                            identity["source"] = source
                        break
        self.coverage["identity"]["status"] = "ok" if identity["model"] else "unavailable"
        os_text = self.read("/etc/os-release", "system") or self.read("/usr/lib/os-release", "system") or ""
        os_values = {}
        for line in os_text.splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os_values[key] = value.strip().strip('"').strip("'")
        system["os"] = {"id": os_values.get("ID"), "name": os_values.get("NAME"), "version": os_values.get("VERSION_ID"),
                        "pretty_name": os_values.get("PRETTY_NAME"), "kernel": self.read("/proc/sys/kernel/osrelease", "system")}
        cpu_text = self.read("/proc/cpuinfo", "system") or ""
        cpu_fields = {}
        cores = 0
        for line in cpu_text.splitlines():
            if ":" in line:
                key, value = (x.strip() for x in line.split(":", 1))
                cpu_fields.setdefault(key, value)
                if key == "processor":
                    cores += 1
        system["cpu"] = {"model": cpu_fields.get("model name") or cpu_fields.get("Processor") or cpu_fields.get("Hardware"),
                         "hardware": cpu_fields.get("Hardware"), "logical_cores": cores or (os.cpu_count() if not self.fixture else None),
                         "vendor": cpu_fields.get("vendor_id"), "implementer": cpu_fields.get("CPU implementer")}
        memory = self.read("/proc/meminfo", "system") or ""
        for source, target in (("MemTotal", "total_bytes"), ("MemAvailable", "available_bytes")):
            match = re.search(r"^" + source + r":\s*(\d+)\s+kB", memory, re.M)
            if match:
                system["memory"][target] = int(match.group(1)) * 1024
        uptime = self.read("/proc/uptime", "system")
        try:
            system["uptime_s"] = float(uptime.split()[0]) if uptime else None
        except ValueError:
            pass
        self.coverage["system"]["status"] = "ok" if os_text or cpu_text or memory else "unavailable"
        for name in self.listing("/sys/class/block", "storage"):
            base = "/sys/class/block/" + name
            fields = self.fields(base, ("size", "removable", "ro", "device/model", "device/vendor"), "storage")
            sectors = _int(fields.get("size"))
            report["storage"].append({"name": name, "source": base, "size_bytes": sectors * 512 if sectors is not None else None,
                                      "removable": fields.get("removable") == "1" if "removable" in fields else None,
                                      "read_only": fields.get("ro") == "1" if "ro" in fields else None,
                                      "model": fields.get("device/model"), "vendor": fields.get("device/vendor")})
        for name in self.listing("/sys/class/net", "interfaces"):
            base = "/sys/class/net/" + name
            values = self.fields(base, ("operstate", "mtu", "speed", "type"), "interfaces")
            report["interfaces"].append({"name": name, "source": base, "state": values.get("operstate"),
                                          "mtu": _int(values.get("mtu")), "speed_mbps": _int(values.get("speed")),
                                          "link_type": values.get("type"), "mac": None, "addresses": None})
        buses = report["buses"]
        usb_names = self.listing("/sys/bus/usb/devices", "usb")
        for name in usb_names:
            if ":" in name:
                continue  # USB interfaces inherit IDs; avoid seven pointless reads per interface.
            base = "/sys/bus/usb/devices/" + name
            fields = self.fields(base, ("idVendor", "idProduct", "manufacturer", "product", "speed", "busnum", "devnum"), "usb")
            if fields.get("idVendor"):
                interface_drivers = sorted({driver for item in usb_names if item.startswith(name + ":")
                                            for driver in [self.link_name("/sys/bus/usb/devices/" + item + "/driver")] if driver})
                buses["usb"].append({"name": name, "source": base, **fields, "driver": self.link_name(base + "/driver"),
                                     "interface_drivers": interface_drivers})
        for name in self.listing("/sys/class/i2c-adapter", "i2c"):
            base = "/sys/class/i2c-adapter/" + name
            buses["i2c"].append({"name": name, "source": base, "label": self.read(base + "/name", "i2c"), "kind": "adapter"})
        for name in self.listing("/sys/bus/i2c/devices", "i2c"):
            if re.fullmatch(r"\d+-[0-9a-fA-F]{4}", name):
                base = "/sys/bus/i2c/devices/" + name
                buses["i2c"].append({"name": name, "source": base, "kind": "enumerated_device", "address": "0x" + name.split("-")[1],
                                     "label": self.read(base + "/name", "i2c"), "driver": self.link_name(base + "/driver")})
        for name in self.listing("/sys/bus/spi/devices", "spi"):
            base = "/sys/bus/spi/devices/" + name
            buses["spi"].append({"name": name, "source": base, "driver": self.link_name(base + "/driver"),
                                 "modalias": self.read(base + "/modalias", "spi")})
        node_pattern = r"tty(?:AMA|THS|S|USB|ACM)\d+|spidev\d+\.\d+|gpiochip\d+"
        for name in self.listing("/dev", "serial", lambda name: re.fullmatch(node_pattern, name)):
            if re.fullmatch(r"tty(?:AMA|THS|S|USB|ACM)\d+|spidev\d+\.\d+|gpiochip\d+", name):
                group = "serial" if name.startswith("tty") else "spi" if name.startswith("spidev") else "gpio"
                buses[group].append({"name": name, "source": "/dev/" + name, "kind": "device_node", "accessible": _access(self.path("/dev/" + name))})
        for name in self.listing("/sys/class/gpio", "gpio"):
            if name.startswith("gpiochip"):
                base = "/sys/class/gpio/" + name
                buses["gpio"].append({"name": name, "source": base, **self.fields(base, ("label", "base", "ngpio"), "gpio")})
        for name in self.listing("/sys/bus/gpio/devices", "gpio"):
            if name.startswith("gpiochip"):
                base = "/sys/bus/gpio/devices/" + name
                if not any(item.get("source") == "/sys/class/gpio/" + name for item in buses["gpio"]):
                    buses["gpio"].append({"name": name, "source": base, **self.fields(base, ("label", "base", "ngpio"), "gpio")})
        for name in self.listing("/sys/bus/iio/devices", "iio"):
            if name.startswith("iio:device"):
                base = "/sys/bus/iio/devices/" + name
                channels = [key for key in self.listing(base, "iio") if re.fullmatch(r"in_[A-Za-z0-9_]+_(?:raw|input|scale|offset)", key)]
                buses["iio"].append({"name": name, "source": base, "label": self.read(base + "/name", "iio"),
                                    "channels": [{"name": key, "value": self.read(base + "/" + key, "iio")} for key in channels[:32]],
                                    "channels_limited": len(channels) > 32})
        for name in self.listing("/sys/class/hwmon", "hwmon"):
            base = "/sys/class/hwmon/" + name
            channels = [key for key in self.listing(base, "hwmon") if re.fullmatch(r"(?:temp|in|curr|power|fan|humidity)\d+_(?:input|label)", key)]
            buses["hwmon"].append({"name": name, "source": base, "label": self.read(base + "/name", "hwmon"),
                                  "channels": [{"name": key, "value": self.read(base + "/" + key, "hwmon")} for key in channels[:32]],
                                  "channels_limited": len(channels) > 32, "units": "kernel sysfs raw units; no inferred conversion"})
        for name in self.listing("/sys/class/video4linux", "camera"):
            base = "/sys/class/video4linux/" + name
            buses["camera"].append({"name": name, "source": base, "label": self.read(base + "/name", "camera"),
                                   "device": "/dev/" + name, "accessible": _access(self.path("/dev/" + name)), "frame": "not_requested"})
        self.services(report)

    def services(self, report):
        seen = set()
        for base in ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"):
            for name in self.listing(base, "services", lambda name: "mcp" in name.lower() and name.endswith(".service")):
                if "mcp" not in name.lower() or not name.endswith(".service") or name in seen:
                    continue
                seen.add(name)
                path = base + "/" + name
                text = self.read(path, "services") or ""
                report["services"].append({"name": name, "kind": "systemd", "state": None, "source": path,
                                           "note": "unit file observed; running/enabled not queried"})
                urls = []
                # Only literal loopback endpoint declarations; no env values, secrets or full ExecStart exported.
                for url in re.findall(r"http://(?:127\.0\.0\.1|localhost|\[::1\]):\d{1,5}/[A-Za-z0-9_./-]*", text):
                    if url not in urls:
                        urls.append(url)
                for url in urls[:8]:
                    report["mcp_candidates"].append({"label": name, "url": url, "transport": "http", "status": "declared_unverified", "evidence": [path]})
                if not urls:
                    report["mcp_candidates"].append({"label": name, "url": None,
                        "transport": "stdio" if "stdio" in text else "unknown", "status": "installed_unverified", "evidence": [path]})
        for base in ("/usr/local/bin", "/usr/bin", "/opt/bin"):
            for name in self.listing(base, "services", lambda name: "mcp" in name.lower()):
                if re.fullmatch(r"[A-Za-z0-9_.-]*mcp[A-Za-z0-9_.-]*", name, re.I) and name not in seen:
                    seen.add(name)
                    report["mcp_candidates"].append({"label": name, "url": None, "transport": "unknown", "status": "binary_observed", "evidence": [base + "/" + name]})
                    if len(report["mcp_candidates"]) >= 64:
                        self.coverage["services"]["limited"] = True
                        return

    def darwin(self, report):
        identity, system = report["identity"], report["system"]
        model = self.command(["/usr/sbin/sysctl", "-n", "hw.model"], "identity")
        identity.update(kind="host", model=model, manufacturer="Apple" if model else None, source="sysctl hw.model" if model else None)
        if model:
            identity["evidence"] = [{"field": "model", "value": model, "source": "sysctl hw.model"}]
        system["os"] = {"id": "macos", "name": "macOS", "version": platform.mac_ver()[0] if not self.fixture else None,
                        "kernel": platform.release() if not self.fixture else None, "pretty_name": "macOS host"}
        system["cpu"] = {"model": self.command(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], "system"),
                         "logical_cores": os.cpu_count() if not self.fixture else None, "hardware": None}
        system["memory"]["total_bytes"] = _int(self.command(["/usr/sbin/sysctl", "-n", "hw.memsize"], "system"))
        self.coverage["identity"]["status"] = "ok" if model else "unavailable"
        self.coverage["system"]["status"] = "ok"
        for key in LABELS:
            if key not in ("identity", "system"):
                self.coverage[key]["status"] = "unsupported"
                self.coverage[key]["errors"] = [{"code": "linux_only", "source": "sysfs", "message": "Linux sysfs отсутствует на macOS; это паспорт хоста, не SBC"}]


def _int(value):
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _access(path):
    return {"readable": os.access(str(path), os.R_OK), "writable": os.access(str(path), os.W_OK)}


def collect(root="/", system=None):
    report = Probe(root, system).collect()
    if len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT:
        raise ValueError("Паспорт превысил 1 MiB; данные не усечены молча")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/", help="offline fixture root; default / reads this device")
    args = parser.parse_args(argv)
    try:
        report = collect(args.root)
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    except (ValueError, OSError) as exc:
        print("edge-probe: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

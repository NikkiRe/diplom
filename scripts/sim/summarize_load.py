#!/usr/bin/env python3
"""Сводка JSON-замеров measure_load.sh (каталог) → таблица Markdown в stdout."""
import json
import pathlib
import sys

d = pathlib.Path(sys.argv[1])
rows = []
for f in sorted(d.glob("*.json")):
    j = json.loads(f.read_text())
    vm, r = j["vm"], j["vm"].get("rates", {})
    t = r.get("topics", {})

    def hz(name):
        x = t.get(name, {})
        return "%s/%s" % (x.get("hz_wall", "–"), x.get("hz_sim", "–"))

    procs = vm["processes"]
    rows.append([
        f.stem, " ".join(a for a in j["launch_args"] if not a.startswith(("rosbridge", "x_pose", "y_pose", "yaw"))),
        r.get("rtf"), vm["vm_cpu_cores_busy"], vm["sim_cpu_cores"], procs.get("gz_sim_server", {}).get("cpu_cores"),
        procs.get("nav2_container", {}).get("cpu_cores", "–"), vm["sim_pss_mib"], vm["vm_mem"]["used_mib"],
        j["host"]["vz_cpu_cores"], j["host"]["vz_rss_mib"],
        hz("/scan"), hz("/camera/camera_info"), hz("/camera/image_raw/compressed") if "/camera/image_raw/compressed" in t else "–",
        hz("/imu"), hz("/odom"), r.get("clock_hz_wall"),
    ])
hdr = ["конфиг", "аргументы", "RTF", "CPU VM, ядер", "CPU sim, ядер", "gz, ядер", "Nav2, ядер", "PSS sim, МиБ",
       "RAM VM, МиБ", "CPU хоста (VZ), ядер", "RSS хоста (VZ), МиБ", "/scan Гц ст/сим", "camera_info Гц ст/сим",
       "JPEG Гц ст/сим",
       "/imu Гц ст/сим", "/odom Гц ст/сим", "/clock Гц ст"]
print("| " + " | ".join(hdr) + " |")
print("|" + "---|" * len(hdr))
for row in rows:
    print("| " + " | ".join(str(x) for x in row) + " |")

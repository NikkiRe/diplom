#!/usr/bin/env python3
"""Сводка check.sh двойника: критерии → summary.json, версии/хеши/конфиги/отклонения → manifest.json.

  python3 twin_summary.py <out_dir>        код 0 — все критерии выполнены, 1 — нет
Числа двойника — ориентир, не результат: Gazebo с программным рендерингом в общей ВМ на Mac — не Jetson Nano и не
база робота. Критерии проверяют, что двойник собран как задумано: ожидания — из twin_config.py, миров и URDF. Нет
данных — критерий не выполнен (value null), а не пропущен. Наблюдения без порога — в "observations".
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

TWIN = Path(__file__).resolve().parent
PROJ = TWIN.parents[2]
sys.path.insert(0, str(TWIN))
import twin_config as cfg  # noqa: E402

RTF_MIN = 0.9
BLIND_EDGE_RAYS = 2        # край блока NaN против скана робота: ±2 луча (0,7°) — точность положения D435i ≈1 см
BLIND_HOLES_MAX = 1        # луч 213 на стыке граней gpu_lidar (README §3)
LOW_BOX_X = (1.25, 1.45)   # ближняя грань low_box — в 1,35 м от base_link (мир lab, поза спавна)
LOW_BOX_TOP = (0.12, 0.2)  # верх ящика 0,15 м
LIDAR_CLEAR_M = 2.0        # ближайшее конечное по курсу ±30° дальше — лидар ящика не видит
TILT_FLAT = (0.5, 1.0)     # в покое на полу: акселерометр, madgwick, °
TILT_ACC_TOL, TILT_MAD_TOL, RAMP_TOL = 0.5, 1.5, 0.3
SLIDE_MAX_M = 0.005
STOP_BRAKE_S = (0.45, 0.65)   # cmd_vel_timeout 0,5 + такт контроллера 20 мс + задержки DDS
STOP_TRUTH_MAX_S = 1.0        # + торможение 0,2 м/с при 1 м/с²
ODOM_POS_M, ODOM_YAW_DEG = 0.02, 1.0
MUX_SWITCH_S = (0.4, 0.7)     # таймаут cmd_vel_joy 0,5 с
HOLD_ODOM_EXCESS_M = 0.3      # у упёршейся базы колёса крутятся — одометрия «едет» дальше истины
STEP_Z = (0.14, 0.16)         # площадка 0,15 м
STEP_CLIFF_X = (1.7, 2.3)     # нижний пол виден за краем с ≈1,84 м (камера 0,665 над площадкой, край в 1,5 м)
STEP_EDGE_X = (1.3, 1.6)      # пол площадки виден до края в 1,5 м
FRAME_Z = {"laser_frame": 0.67, "camera_depth_optical_frame": 0.665}
TRUTH_PERSON_CLEAR_M = 0.348  # палуба — нога человека после set_pose: hypot(0,6; 0,1) − 0,195 − 0,065
TRUTH_CABLE_CLEAR_M = 0.275   # передний ролик — «кабель»: (−0,95 − 0,1) − (−1,5 + 0,15 + 0,025)
TRUTH_CLEAR_TOL = 0.015       # база после set_pose стоит в спавне ± несколько мм
TRUTH_KNOWN_MAX_S = 3.0       # generate_world_sdf ≈0,5 с + такт логической камеры 0,1 с
TOUCH_M = 0.005
RUNS = ("xacro", "lab", "hold", "ramp", "step")
RUN_FILES = ("ready", "load", "sensors", "mux", "silence", "odom", "truth", "ramp", "crash")
CRASH_MAX_S = 20.0            # stand_procs down: SIGINT, через 10 с SIGTERM, через 15 с SIGKILL
DEVIATIONS = [
    "Геометрия — [оценка по фото] R7b §2.3 (палубы Ø0,39 м, колёса Ø0,072 на y = ±0,19, ролики x = ±0,15, мачта "
    "x = −0,015, D435i на 0,665 м, URG на 0,67 м); масса 3,96 кг — [оценка по составу]; инерции — однородные тела; "
    "URDF лаборатории не получен",
    "Слепой сектор лидара даёт корпус D435i в модели; NaN из попаданий ближе 0,15 м делает sensor_model.py (как "
    "urg_node2 из сырого 0); луч 213 (−45°) на стыке граней кубической карты gpu_lidar проходит сквозь корпус — "
    "артефакт Gazebo; кодов ошибок URG (0,006–0,007 м, R7a N14) нет; шум σ 0,01 м (паспорт ±30 мм до 1 м как 3σ), "
    "±3 % дальше 1 м не моделируется; QoS /scan reliable",
    "Глубина — камера глубины Gazebo 424×240, 87°, z-глубина в мм (16UC1) с гауссовым шумом σ = z²·0,08/(f·B) "
    "[оценка по модели Intel]; провалов, «летящих» пикселей, зависимости от текстуры и проектора нет; цвет 640×480, "
    "54,9° (кадр 4:3 из 16:9 — [гипотеза] R7b RS4); камеры 15 Гц вместо 30 ради RTF",
    "IMU — идеальный датчик Gazebo + гауссов шум по плотностям BMI055 на полосе 100 Гц; смещений, дрейфа и "
    "температуры нет; madgwick без магнитометра, усиление по умолчанию",
    "ros2_control — Jazzy (gz_ros2_control, diff_drive_controller 4.x), на роботе Foxy: входа cmd_vel_unstamped в "
    "Jazzy нет — звено cmd_relay.py (Twist → TwistStamped с нулевой меткой); начальный ноль контроллеру шлёт "
    "cmd_relay (у Foxy опорная скорость после configure — ноль, в Jazzy — NaN, и без него в hold нет /odom); "
    "cmd_vel_timeout 0 в Jazzy отключает таймаут (hold), в Foxy значит «тормоз в каждом такте» (diff_drive_controller."
    "cpp:169): hold на двойнике — модель смерти controller_manager при STM32, держащем уставку, а не значение "
    "параметра на роботе",
    "Пределы diff_cont (0,5 м/с, 1 м/с², 1,5 рад/с, 5,3 рад/с²) — наше предложение для L1 по R7b §3.2, §5.2, §5.5, "
    "а не параметры робота (не сняты, Э0); флаги has_* заданы явно (в Foxy по умолчанию false), для Foxy вместо "
    "max_deceleration нужен min_acceleration",
    "STM32, UART, ПИД, L298N не моделируются: gz_ros2_control задаёт скорость суставов колёс напрямую (предел момента "
    "2 Н·м — [оценка]); чистой задержки и динамики ПИД нет (их модель — stm32_sim стенда TB3)",
    "Колесо в коллизиях — сфера (у цилиндра DART держит контакт на кромке, и одометрия врёт на 6 % в повороте); "
    "проскальзывания шин нет, трение μ = 1, ролики — сферы без трения; жёсткий ромб из четырёх опор без подвески",
    "header.stamp датчиков — время симуляции; узлы двойника на Python — по монотонным часам ВМ",
    "ROS 2 Jazzy + Cyclone DDS вместо Foxy; свой домен DDS и порт rosbridge на время проверки (общая ВМ)",
    "Хост и ВМ общие с другими агентами: нагрузка до и после каждого прогона — <прогон>/host_load_*.txt; RTF ниже "
    "порога при чужой нагрузке — не свойство двойника",
    "Истина: расстояние робот–препятствие — в проекции на пол для пар, перекрывающихся по высоте, наклон робота и "
    "препятствий не учитывается; позы препятствий — логическая камера twin_eye (её добавляет launch в копию мира), "
    "формы новых моделей — generate_world_sdf; касания корпуса — датчик касаний Gazebo, колёс и роликов — по "
    "геометрии (зазор ≤ 2 мм): датчик gz на опорах публикует каждый шаг физики (1 кГц), пока они стоят на полу",
]


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _text(path: Path):
    return path.read_text().strip() if path.exists() else None


def _in(value, lo, hi):
    return isinstance(value, (int, float)) and lo <= value <= hi


def _close(value, expect, tol):
    return isinstance(value, (int, float)) and abs(value - expect) <= tol


def depth_bins(profile, z_lo, z_hi, key=3):
    """Бины профиля глубины [x, n, median_z, max_z] (не меньше 5 точек), у которых значение key (2 — медиана,
    3 — максимум) в [z_lo, z_hi]."""
    return [b[0] for b in profile or [] if b[1] >= 5 and z_lo <= b[key] <= z_hi]


def observer_events(path: Path) -> dict:
    """Наблюдатель жестов на двойнике: серии команд по темам, касания и рассогласования типов."""
    if not path.exists():
        return {}
    cmd, walls, mismatch = {}, [], 0
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ev") == "cmd":
            cmd[e.get("topic")] = cmd.get(e.get("topic"), 0) + 1
        elif e.get("ev") == "wall" and e.get("touch"):
            walls.append(e.get("wall"))
        elif e.get("ev") == "cmd_type_mismatch":
            mismatch += 1
    return {"cmd_series": cmd, "touch_walls": walls, "type_mismatch": mismatch}


def checks(runs: dict) -> list:
    """runs — {прогон: {ready, load, sensors, mux, silence, odom, ramp, crash, xacro, observer}}
    → [{name, value, expect, ok}]."""
    out = []

    def add(name, value, expect, ok):
        out.append({"name": name, "value": value, "expect": expect, "ok": bool(ok)})

    x = runs.get("xacro") or {}
    add("xacro: check_urdf", x.get("check_urdf"), "Successfully Parsed, rc=0", x.get("check_urdf_ok"))
    add("xacro: gz sdf -k", x.get("gz_sdf"), "rc=0", x.get("gz_sdf_ok"))

    def ready(name):
        r = runs.get(name) or {}
        rd = r.get("ready") or {}
        add(name + ": двойник готов", rd.get("missing"), "[]", rd.get("ready"))
        return r

    def sensors(name, s, full):
        types = s.get("types") or {}
        for topic, (type_name, hz, tol) in cfg.SENSORS.items():
            add("%s: %s — тип" % (name, topic), types.get(topic), [type_name], types.get(topic) == [type_name])
            if not full:
                continue
            win = "camera_on" if topic in cfg.CAMERA_TOPICS else "camera_off"
            value = (((s.get("windows") or {}).get(win) or {}).get("hz") or {}).get(topic)
            add("%s: %s — Гц сим." % (name, topic), value, "%g ± %g" % (hz, tol), _close(value, hz, tol))
        reach = s.get("tf_from_odom") or {}
        bad = sorted(f for f in cfg.TF_FRAMES if not isinstance(reach.get(f), list))
        add(name + ": TF — кадры достижимы из odom", bad, "[]", reach and not bad)
        for frame, z in FRAME_Z.items():      # одометрия плоская: z кадра в odom — высота над base_link
            v = reach.get(frame)
            v = v[2] if isinstance(v, list) else None
            add("%s: TF — высота %s над базой, м" % (name, frame), v, "%g ± 0.002" % z, _close(v, z, 0.002))

    def scan(name, s):
        sc = s.get("scan") or {}
        for key, expect, tol in (("n", cfg.SCAN_N, 0), ("angle_min", -cfg.SCAN_ANGLE, 1e-6),
                                 ("angle_max", cfg.SCAN_ANGLE, 1e-6), ("range_min", cfg.SCAN_RANGE[0], 1e-6),
                                 ("range_max", cfg.SCAN_RANGE[1], 1e-5)):
            add("%s: /scan %s" % (name, key), sc.get(key), expect, _close(sc.get(key), expect, tol))
        add(name + ": /scan frame_id", sc.get("frame_id"), cfg.SCAN_FRAME, sc.get("frame_id") == cfg.SCAN_FRAME)
        m = (sc.get("blind") or {}).get("main") or {}
        edges = [m.get("first"), m.get("last")]
        robot = [m.get("robot_first"), m.get("robot_last")]
        add(name + ": слепой сектор (NaN от корпуса D435i) — лучи против скана робота R7b §3.3",
            {"twin": edges, "deg": m.get("deg"), "robot": robot}, "края ±%d луча" % BLIND_EDGE_RAYS,
            None not in edges + robot and all(abs(a - b) <= BLIND_EDGE_RAYS for a, b in zip(edges, robot)))
        add(name + ": слепой сектор — лучи-«дыры» внутри", m.get("holes"), "≤ %d (стык gpu_lidar)" % BLIND_HOLES_MAX,
            m.get("holes") is not None and len(m["holes"]) <= BLIND_HOLES_MAX)
        return sc

    def probe_rtf(name, r):
        """Времена проб движения валидны при RTF ≈ 1: twist_mux считает по стенным часам, контроллер — по сим."""
        v = min((r[k]["probe_rtf"] for k in ("mux", "silence", "odom", "truth", "ramp")
                 if (r.get(k) or {}).get("probe_rtf") is not None), default=None)
        add("%s: RTF во время проб движения (наименьший)" % name, v, ">= %g" % RTF_MIN, _in(v, RTF_MIN, 1e9))

    def truth(tr):
        """Истина при сценариях сервисами мира (проба truth): set_pose, create, remove, RTF."""
        person, cable = cfg.TRUTH_PERSON[0], cfg.TRUTH_CABLE[0]
        sp, cr, rt = tr.get("set_pose") or {}, tr.get("create") or {}, tr.get("rtf") or {}
        add("lab: истина — set_pose человека перед роботом: зазор, м", [sp.get("clearance_m"), sp.get("nearest")],
            "%g ± %g, %s/…" % (TRUTH_PERSON_CLEAR_M, TRUTH_CLEAR_TOL, person),
            _close(sp.get("clearance_m"), TRUTH_PERSON_CLEAR_M, TRUTH_CLEAR_TOL) and
            str(sp.get("nearest")).startswith(person + "/"))
        add("lab: истина — наезд на человека: касание, наименьший зазор, м",
            [sp.get("drive_contacts"), sp.get("drive_min_clearance_m")], "%s, ≤ %g" % (person, TOUCH_M),
            person in (sp.get("drive_contacts") or []) and _in(sp.get("drive_min_clearance_m"), 0.0, TOUCH_M))
        add("lab: истина — create: пока формы нет, clearance_m = null", [cr.get("unknown_seen"),
            cr.get("null_while_unknown")], [True, True], cr.get("unknown_seen") is True and
            cr.get("null_while_unknown") is True)
        add("lab: истина — create: форма новой модели известна через, с", cr.get("known_after_s"),
            "≤ %g" % TRUTH_KNOWN_MAX_S, _in(cr.get("known_after_s"), 0.0, TRUTH_KNOWN_MAX_S))
        add("lab: истина — create: зазор от переднего ролика до кабеля, м", [cr.get("clearance_m"), cr.get("part")],
            "%g ± %g, caster_front/…" % (TRUTH_CABLE_CLEAR_M, TRUTH_CLEAR_TOL),
            _close(cr.get("clearance_m"), TRUTH_CABLE_CLEAR_M, TRUTH_CLEAR_TOL) and
            str(cr.get("part")).startswith("caster_front/"))
        add("lab: истина — наезд роликом на кабель 4 см (ниже палуб): касание", cr.get("drive_contacts"), cable,
            cable in (cr.get("drive_contacts") or []))
        add("lab: истина — remove: кабеля в истине нет", (tr.get("remove") or {}).get("gone"), True,
            (tr.get("remove") or {}).get("gone") is True)
        add("lab: истина — RTF в /twin/truth (последнее значение, rtf_ok)", [rt.get("last"), rt.get("last_ok")],
            ">= %g, true" % RTF_MIN, _in(rt.get("last"), RTF_MIN, 1e9) and rt.get("last_ok") is True)

    def imu_flat(name, s):
        imu = s.get("imu") or {}
        add(name + ": IMU в покое — наклон по акселерометру, °", imu.get("tilt_accel_deg"), "|·| ≤ %g" % TILT_FLAT[0],
            _close(imu.get("tilt_accel_deg"), 0.0, TILT_FLAT[0]))
        add(name + ": IMU в покое — наклон по madgwick, °", imu.get("tilt_madgwick_deg"), "|·| ≤ %g" % TILT_FLAT[1],
            _close(imu.get("tilt_madgwick_deg"), 0.0, TILT_FLAT[1]))

    lab = ready("lab")
    s = lab.get("sensors") or {}
    sensors("lab", s, full=True)
    for win in ("camera_off", "camera_on"):
        v = ((s.get("windows") or {}).get(win) or {}).get("rtf")
        add("lab: RTF, окно %s" % win, v, ">= %g" % RTF_MIN, _in(v, RTF_MIN, 1e9))
    sc = scan("lab", s)
    imu_flat("lab", s)
    prof = (s.get("depth") or {}).get("profile")
    add("lab: глубина — 16UC1 в camera_depth_optical_frame", [(s.get("depth") or {}).get(k) for k in
        ("encoding", "frame_id")], ["16UC1", "camera_depth_optical_frame"],
        [(s.get("depth") or {}).get(k) for k in ("encoding", "frame_id")] == ["16UC1", "camera_depth_optical_frame"])
    box = [b for b in depth_bins(prof, *LOW_BOX_TOP) if _in(b, *LOW_BOX_X)]
    add("lab: глубина видит low_box (верх 0,15 м) по курсу, бины x, м", box, "в %s" % list(LOW_BOX_X), box)
    v = sc.get("front30_min_finite_m")
    add("lab: лидар low_box не видит — ближайшее конечное ±30°, м", v, "> %g" % LIDAR_CLEAR_M,
        v is None or v > LIDAR_CLEAR_M)
    m = lab.get("mux") or {}
    for phase, expect in (("joy_over_cmd", [0.0]), ("cmd_alone", [0.1]), ("tracker_over_cmd", [0.05])):
        v = (m.get(phase) or {}).get("values_after_0_6s")
        add("lab: twist_mux %s — выход" % phase, v, expect, v == expect)
    v = (m.get("joy_over_cmd") or {}).get("truth_moved_m")
    add("lab: twist_mux — при cmd_vel_joy = 0 база стоит, м", v, "≤ 0.005", _in(v, 0.0, 0.005))
    v = (m.get("cmd_alone") or {}).get("first_expected_s")
    add("lab: twist_mux — cmd_vel снова главный после тишины joy, с", v, "в %s" % list(MUX_SWITCH_S),
        _in(v, *MUX_SWITCH_S))
    probe_rtf("lab", lab)
    si = lab.get("silence") or {}
    add("lab: чужих писателей входов twist_mux нет", si.get("writers_before"), "все []",
        si.get("writers_before") and not any(si["writers_before"].values()))
    add("lab: тишина /cmd_vel → контроллер тормозит через, с", si.get("brake_s"), "в %s" % list(STOP_BRAKE_S),
        _in(si.get("brake_s"), *STOP_BRAKE_S))
    add("lab: тишина /cmd_vel → база стоит (истина) через, с", si.get("truth_stop_s"), "≤ %g" % STOP_TRUTH_MAX_S,
        _in(si.get("truth_stop_s"), 0.0, STOP_TRUTH_MAX_S))
    add("lab: тишина — касаний нет", si.get("contacts"), [], si.get("contacts") == [])
    od = lab.get("odom") or {}
    add("lab: одометрия против истины — ошибка положения в конце, м", od.get("pos_err_end_m"), "≤ %g" % ODOM_POS_M,
        _in(od.get("pos_err_end_m"), 0.0, ODOM_POS_M))
    add("lab: одометрия против истины — ошибка курса в конце, °", od.get("yaw_err_end_deg"), "≤ %g" % ODOM_YAW_DEG,
        _in(od.get("yaw_err_end_deg"), 0.0, ODOM_YAW_DEG))
    truth(lab.get("truth") or {})

    hold = ready("hold")
    hr = hold.get("ready") or {}
    add("hold: /odom есть до первой команды (готовность в hold ждёт /odom)", [hr.get("hold"), hr.get("ready")],
        [True, True], hr.get("hold") is True and hr.get("ready") is True)
    probe_rtf("hold", hold)
    h = hold.get("silence") or {}
    add("hold: тишина /cmd_vel — контроллер не тормозит", [h.get("brake_s"), h.get("out_zero_s")], [None, None],
        h and h.get("brake_s") is None and h.get("out_zero_s") is None)
    add("hold: база въехала в low_box (датчик касаний)", h.get("contacts"), "low_box", "low_box" in (h.get("contacts")
                                                                                                       or []))
    excess = (h["odom_path_m"] - h["odom_vs_truth"]["path_m"]) if h.get("odom_path_m") is not None and \
        (h.get("odom_vs_truth") or {}).get("path_m") is not None else None
    add("hold: одометрия у упёршейся базы — лишний путь, м", excess, ">= %g" % HOLD_ODOM_EXCESS_M,
        _in(excess, HOLD_ODOM_EXCESS_M, 1e9))
    obs = hold.get("observer") or {}
    add("hold: наблюдатель жестов — серии /cmd_vel и /diff_cont/cmd_vel_unstamped", obs.get("cmd_series"),
        "обе > 0", all((obs.get("cmd_series") or {}).get(t) for t in (cfg.CMD_VEL, cfg.MUX_OUT)))
    add("hold: наблюдатель жестов — касание low_box", obs.get("touch_walls"), "low_box/…",
        any(str(w).startswith("low_box") for w in obs.get("touch_walls") or []))
    add("hold: наблюдатель жестов — типы скоростей совпали", obs.get("type_mismatch"), 0, obs.get("type_mismatch") == 0)

    ramp = ready("ramp")
    sensors("ramp", ramp.get("sensors") or {}, full=False)
    probe_rtf("ramp", ramp)
    rp = ramp.get("ramp") or {}
    for phase in ("standing", "after_climb"):
        p = rp.get(phase) or {}
        t = p.get("tilt_truth_deg")
        add("ramp: %s — наклон по истине, °" % phase, t, "%g ± %g" % (cfg.RAMP_DEG, RAMP_TOL),
            _close(t, cfg.RAMP_DEG, RAMP_TOL))
        for key, tol in (("tilt_accel_deg", TILT_ACC_TOL), ("tilt_madgwick_deg", TILT_MAD_TOL)):
            v = p.get(key)
            add("ramp: %s — %s против истины, °" % (phase, key), v, "истина ± %g" % tol,
                t is not None and _close(v, t, tol))
    v = (rp.get("standing") or {}).get("slide_m")
    add("ramp: стоянка на склоне — сползание, м", v, "≤ %g" % SLIDE_MAX_M, _in(v, 0.0, SLIDE_MAX_M))

    step = ready("step")
    s = step.get("sensors") or {}
    sensors("step", s, full=False)
    z = (s.get("truth") or {}).get("z")
    add("step: робот на площадке, z базы, м", z, "в %s" % list(STEP_Z), _in(z, *STEP_Z))
    prof = (s.get("depth") or {}).get("profile")
    cliff = depth_bins(prof, -1.0, -0.1, key=2)
    add("step: глубина видит пол за краем (z < −0,1), ближний бин x, м", min(cliff, default=None),
        "в %s" % list(STEP_CLIFF_X), _in(min(cliff, default=None), *STEP_CLIFF_X))
    edge = [b for b in depth_bins(prof, -0.03, 0.03, key=2) if b < 1.7]
    add("step: глубина видит площадку до края, дальний бин x, м", max(edge, default=None),
        "в %s" % list(STEP_EDGE_X), _in(max(edge, default=None), *STEP_EDGE_X))
    crash = step.get("crash") or {}
    add("step: kill -9 sensor_model — группа двойника погасла, с", crash.get("group_gone_after_s"),
        "<= %g" % CRASH_MAX_S, _in(crash.get("group_gone_after_s"), 0.0, CRASH_MAX_S))
    left = None if not crash else [g for g in crash.get("gz_sim_left", []) if "/twin_runs/" in g.get("cmd", "")]
    add("step: kill -9 sensor_model — gz sim двойника в ВМ не осталось", left, [], left == [])
    return out


def observations(runs: dict) -> dict:
    """Числа без порога: что показал двойник (ориентир)."""
    lab, hold, ramp = runs.get("lab") or {}, runs.get("hold") or {}, runs.get("ramp") or {}
    s = lab.get("sensors") or {}
    return {
        "up_s": {n: r.get("up_s") for n, r in runs.items() if n != "xacro"},
        "rtf": {w: ((s.get("windows") or {}).get(w) or {}).get("rtf") for w in ("camera_off", "camera_on")},
        "load_lab": load_brief(lab.get("load") or {}),
        "scan_blind": ((s.get("scan") or {}).get("blind") or {}),
        "lab_depth_profile": (s.get("depth") or {}).get("profile"),
        "silence_lab": lab.get("silence"), "odom_lab": lab.get("odom"), "mux_lab": lab.get("mux"),
        "truth_lab": lab.get("truth"),
        "silence_hold": hold.get("silence"),
        "ramp": ramp.get("ramp"),
    }


def load_brief(load: dict) -> dict:
    """Нагрузка за окно (scripts/sim/vm_load.py): RTF рядом с занятостью ВМ и стоимостью группы двойника."""
    vm = load.get("vm") or load
    return {"rtf": (vm.get("rates") or {}).get("rtf"), "vm_cpu_cores_busy": vm.get("vm_cpu_cores_busy"),
            "twin_cpu_cores": vm.get("sim_cpu_cores"), "twin_pss_mib": vm.get("sim_pss_mib")}


def _collect(d: Path) -> dict:
    run = {k: _load(d / (k + ".json")) for k in RUN_FILES}
    up = re.search(r"готов за (\d+) с", _text(d / "start.txt") or "")
    run["up_s"] = int(up.group(1)) if up else None
    run["observer"] = observer_events(d / "observer.jsonl")
    if d.name == "xacro":
        for key, fname in (("check_urdf", "check_urdf.txt"), ("gz_sdf", "gz_sdf_check.txt")):
            text = _text(d / fname) or ""
            run[key] = text.splitlines()[-1] if text else None
            run[key + "_ok"] = text.endswith("rc=0") and (key != "check_urdf" or "Successfully Parsed" in text)
    return run


def _sha256_tree(rel: str) -> dict:
    """Хеши кода и данных двойника; README — документация, в хеши не входит."""
    root = PROJ / rel
    paths = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".md")
    return {str(p.relative_to(PROJ)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    out = Path(sys.argv[1])
    runs = {n: _collect(out / n) for n in RUNS if (out / n).is_dir()}
    res = checks(runs)
    summary = {"all_ok": all(c["ok"] for c in res), "n_checks": len(res), "n_failed": sum(not c["ok"] for c in res),
               "checks": res, "observations": observations(runs),
               "status": "ориентир двойника (Gazebo в общей ВМ на Mac), не результат"}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    manifest = {
        "what": "Э2.5: цифровой двойник NeuroChip в Gazebo Harmonic (PLAN §13.1) — проверка check.sh",
        "command": _text(out / "command.txt"),
        "run_started": _text(out / "run_started.txt"),
        "run_finished": _text(out / "run_finished.txt"),
        "summarized_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "versions": {"vm": (_text(out / "versions_vm.txt") or "").splitlines(),
                     "host": (_text(out / "versions_host.txt") or "").splitlines()},
        "files_sha256": {**_sha256_tree("neurobot/sim/twin"),
                         **{k: v for k, v in _sha256_tree("neurobot/sim/stand").items()
                            if k.endswith("stand_procs.py")}},
        "configs": {n: {f: _text(out / n / f) for f in ("args.txt", "domain", "controllers.yaml")}
                    for n in runs if n != "xacro"},
        "host_load": {n: (_text(out / n / "host_load_before.txt") or "").splitlines() for n in runs if n != "xacro"},
        "deviations": DEVIATIONS,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    for c in res:
        print("%s %-78s %s (ожидание %s)" % ("OK  " if c["ok"] else "FAIL", c["name"], c["value"], c["expect"]))
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

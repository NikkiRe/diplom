#!/usr/bin/env python3
"""GAP4. Замер токенизации кириллицы в o200k_base и ошибки эвристики PicoClaw «2,5 символа на токен».

Что меряется (всё локально, без API-ключа):
  1. Параллельные команды ассистенту MASSIVE test (en/ru, 2974 пары; подмножество iot — 220 пар).
  2. Параллельная проза WMT19 ru-en validation (newstest2018), первые 1000 пар.
  3. Определения инструментов (OpenAI tools / MCP tools/list): 8 «аппаратных» инструментов + i2c из PicoClaw,
     описания на английском и на русском (русский перевод мой, имена и ключи схемы не переводятся).
  4. Результаты инструментов (JSON) с русскими значениями в трёх сериализациях:
     компактный UTF-8 (Go json.Marshal, PicoClaw), indent=2 (MCP Python SDK: pydantic_core.to_json(indent=2)),
     json.dumps по умолчанию (ensure_ascii=True → \\uXXXX).
  5. Ход агента «команда → вызов инструмента → результат → ответ» в трёх языковых конфигурациях и его цена в ₽
     по снимку каталога RouterAI (F_artifacts/routerai_models_2026-10-06.json).

Оценка PicoClaw воспроизведена по pkg/tokenizer/estimator.go (коммит bbf6893c, файл рядом):
  контент сообщения — utf8.RuneCountInString (символы), аргументы tool_calls и описания инструментов — len() (БАЙТЫ UTF-8),
  +12 символов на сообщение, +20 на инструмент, tokens = chars*2/5 (целочисленно).
«Фактические» токены: tiktoken o200k_base (gpt-4o/4.1/5 → o200k_base по tiktoken 0.14.0);
для сообщений и инструментов — приближение из OpenAI Cookbook «How to count tokens with tiktoken»
(3 токена на сообщение + 3 на ответ; константы num_tokens_for_tools для gpt-4o-mini). Точный учёт — только usage из API.

Запуск: python3 token_ru_en.py   (нужен tiktoken; данные — ./data, см. fetch_massive.py)
"""
import json
import pathlib
import random
import statistics as st

import tiktoken

HERE = pathlib.Path(__file__).parent
DATA = HERE / "data"
F_CATALOG = HERE.parent / "F_artifacts" / "routerai_models_2026-10-06.json"
ENC = tiktoken.get_encoding("o200k_base")
random.seed(0)


def ntok(s):
    return len(ENC.encode(s))


# ---------- PicoClaw estimator (pkg/tokenizer/estimator.go) ----------
def pico_msg(content="", tool_calls=(), tool_call_id=""):
    chars = len(content)  # utf8.RuneCountInString == число символов Python str
    for tc_id, name, args in tool_calls:
        chars += len(tc_id) + len("function") + len(name.encode()) + len(args.encode())  # len() в Go = байты
    if tool_call_id:
        chars += len(tool_call_id.encode())
    chars += 12
    return chars * 2 // 5


def go_json(o):  # json.Marshal: ключи map сортируются, без пробелов, не-ASCII как UTF-8
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pico_tooldefs(defs):
    total = 0
    for d in defs:
        f = d["function"]
        total += len(f["name"].encode()) + len(f["description"].encode())
        total += len(go_json(f["parameters"]).encode())
        total += 20
    return total * 2 // 5


# ---------- «фактические» токены (приближение OpenAI Cookbook) ----------
def oa_msg(role, content):
    return 3 + ntok(role) + ntok(content)


def oa_tools(defs):
    # num_tokens_for_tools, константы для gpt-4o / gpt-4o-mini
    func_init, prop_init, prop_key, enum_init, enum_item, func_end = 7, 3, 3, -3, 3, 12
    n = 0
    for d in defs:
        f = d["function"]
        n += func_init
        desc = f["description"][:-1] if f["description"].endswith(".") else f["description"]
        n += ntok(f["name"] + ":" + desc)
        props = f["parameters"].get("properties", {})
        if props:
            n += prop_init
            for k, p in props.items():
                n += prop_key
                if "enum" in p:
                    n += enum_init
                    for it in p["enum"]:
                        n += enum_item + ntok(it)
                pd = p.get("description", "")
                pd = pd[:-1] if pd.endswith(".") else pd
                n += ntok(f"{k}:{p['type']}:{pd}")
    return n + func_end


def boot_ratio(num, den, B=2000):
    idx = range(len(num))
    rs = []
    for _ in range(B):
        s = [random.choice(idx) for _ in idx]
        rs.append(sum(num[i] for i in s) / sum(den[i] for i in s))
    rs.sort()
    return rs[int(0.025 * B)], rs[int(0.975 * B)]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


# ---------- 1–2. параллельные тексты ----------
def parallel_report(name, pairs):
    en_t = [ntok(e) for e, r in pairs]
    ru_t = [ntok(r) for e, r in pairs]
    en_c = [len(e) for e, r in pairs]
    ru_c = [len(r) for e, r in pairs]
    ru_b = [len(r.encode()) for e, r in pairs]
    lo, hi = boot_ratio(ru_t, en_t)
    # эвристика PicoClaw на «голом» контенте (без +12) и на сообщении (с +12 против 3+role+content)
    ru_est = [c * 2 // 5 for c in ru_c]
    en_est = [c * 2 // 5 for c in en_c]
    ru_msg_est = [pico_msg(r) for e, r in pairs]
    ru_msg_act = [oa_msg("user", r) for e, r in pairs]
    en_msg_est = [pico_msg(e) for e, r in pairs]
    en_msg_act = [oa_msg("user", e) for e, r in pairs]
    print(f"\n## {name}: {len(pairs)} пар")
    print(f"  токены o200k: en={sum(en_t)}  ru={sum(ru_t)}  ru/en={sum(ru_t)/sum(en_t):.3f} (95% bootstrap CI {lo:.3f}–{hi:.3f}); "
          f"медиана по парам {st.median(r/e for r, e in zip(ru_t, en_t)):.3f}")
    print(f"  символов на токен: en={sum(en_c)/sum(en_t):.2f}  ru={sum(ru_c)/sum(ru_t):.2f};  байт UTF-8 на токен ru={sum(ru_b)/sum(ru_t):.2f}")
    print(f"  символов: ru/en={sum(ru_c)/sum(en_c):.3f}")
    print(f"  PicoClaw (контент, chars*2/5) / факт: en={sum(en_est)/sum(en_t):.3f}  ru={sum(ru_est)/sum(ru_t):.3f}")
    r_ratio = [a / b for a, b in zip(ru_msg_est, ru_msg_act)]
    e_ratio = [a / b for a, b in zip(en_msg_est, en_msg_act)]
    print(f"  PicoClaw (сообщение, +12) / факт (3+role+content): en сумма={sum(en_msg_est)/sum(en_msg_act):.3f} "
          f"[p5={pct(e_ratio,.05):.2f} медиана={st.median(e_ratio):.2f} p95={pct(e_ratio,.95):.2f}];  "
          f"ru сумма={sum(ru_msg_est)/sum(ru_msg_act):.3f} [p5={pct(r_ratio,.05):.2f} медиана={st.median(r_ratio):.2f} p95={pct(r_ratio,.95):.2f}]")
    print(f"  доля сообщений, где PicoClaw НЕДООЦЕНИВАЕТ: en={sum(x<1 for x in e_ratio)/len(e_ratio):.1%}  ru={sum(x<1 for x in r_ratio)/len(r_ratio):.1%}")
    return dict(en=sum(en_t), ru=sum(ru_t), ratio=sum(ru_t) / sum(en_t),
                ru_cpt=sum(ru_c) / sum(ru_t), en_cpt=sum(en_c) / sum(en_t))


# ---------- 3. определения инструментов ----------
def tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


def S(desc, **kw):
    return {"type": "string", "description": desc, **kw}


def I(desc):
    return {"type": "integer", "description": desc}


TOOLS = {
    "en": [
        tool("list_devices", "List all hardware devices connected to the single-board computer: sensors, cameras and actuators, with their ids, types, locations and current status.",
             {"kind": S("Filter by device kind.", enum=["sensor", "camera", "actuator", "all"])}, []),
        tool("read_sensor", "Read the latest value of a sensor (temperature, humidity, motion, light level, CO2). Returns value, unit and timestamp.",
             {"sensor_id": S("Sensor identifier from list_devices."),
              "max_age_s": I("Maximum acceptable age of the reading in seconds; older readings trigger a fresh measurement.")}, ["sensor_id"]),
        tool("camera_snapshot", "Capture a frame from a camera and return a short text description produced by the on-device detector (objects, people, counts). The image itself is not returned.",
             {"camera_id": S("Camera identifier from list_devices."),
              "detect": S("Comma-separated object classes to look for, e.g. person, cat, car.")}, ["camera_id"]),
        tool("set_relay", "Switch a relay channel on or off (lights, fan, heater, pump). Use only for devices of type relay.",
             {"relay_id": S("Relay identifier from list_devices."), "state": S("Target state.", enum=["on", "off"]),
              "duration_s": I("Optional: automatically switch back after this many seconds.")}, ["relay_id", "state"]),
        tool("set_servo", "Move a servo motor to the given angle.",
             {"servo_id": S("Servo identifier from list_devices."), "angle": I("Target angle in degrees (0-180)."),
              "speed": S("Movement speed.", enum=["slow", "normal", "fast"])}, ["servo_id", "angle"]),
        tool("set_light", "Set brightness and color of a smart light.",
             {"light_id": S("Light identifier from list_devices."), "brightness": I("Brightness in percent (0-100)."),
              "color": S("Color name or hex code.")}, ["light_id"]),
        tool("schedule_action", "Schedule a tool call to run later or periodically.",
             {"action": S("Name of the tool to call."), "arguments": S("JSON-encoded arguments for the tool."),
              "cron": S("Cron expression, e.g. '0 7 * * *' for every day at 7:00.")}, ["action", "cron"]),
        tool("get_events", "Return hardware events recorded since the given time: motion detections, threshold crossings, device connects and disconnects.",
             {"since": S("ISO 8601 timestamp."), "limit": I("Maximum number of events to return.")}, ["since"]),
        # PicoClaw pkg/tools/hardware i2c.go (A_artifacts/code_snippets/picoclaw_i2c.go), без поля data:array
        tool("i2c", "Interact with I2C bus devices for reading sensors and controlling peripherals. Actions: detect (list buses), scan (find devices on a bus), read (read bytes from device), write (send bytes to device). Linux only.",
             {"action": S("Action to perform: detect (list available I2C buses), scan (find devices on a bus), read (read bytes from a device), write (send bytes to a device)", enum=["detect", "scan", "read", "write"]),
              "bus": S("I2C bus number (e.g. \"1\" for /dev/i2c-1). Required for scan/read/write."),
              "address": I("7-bit I2C device address (0x03-0x77). Required for read/write."),
              "register": I("Register address to read from or write to. If set, sends register byte before read/write."),
              "length": I("Number of bytes to read (1-256). Default: 1. Used with read action.")}, ["action"]),
    ],
    "ru": [
        tool("list_devices", "Вернуть список всех устройств, подключённых к одноплатному компьютеру: датчиков, камер и исполнительных устройств — с идентификаторами, типами, расположением и текущим состоянием.",
             {"kind": S("Фильтр по типу устройства.", enum=["sensor", "camera", "actuator", "all"])}, []),
        tool("read_sensor", "Прочитать последнее значение датчика (температура, влажность, движение, освещённость, CO2). Возвращает значение, единицу измерения и время.",
             {"sensor_id": S("Идентификатор датчика из list_devices."),
              "max_age_s": I("Максимально допустимый возраст показания в секундах; более старое показание запускает новое измерение.")}, ["sensor_id"]),
        tool("camera_snapshot", "Снять кадр с камеры и вернуть краткое текстовое описание от детектора на устройстве (объекты, люди, количество). Само изображение не возвращается.",
             {"camera_id": S("Идентификатор камеры из list_devices."),
              "detect": S("Классы объектов через запятую, например person, cat, car.")}, ["camera_id"]),
        tool("set_relay", "Включить или выключить канал реле (свет, вентилятор, обогреватель, насос). Использовать только для устройств типа relay.",
             {"relay_id": S("Идентификатор реле из list_devices."), "state": S("Целевое состояние.", enum=["on", "off"]),
              "duration_s": I("Необязательно: автоматически вернуть состояние через указанное число секунд.")}, ["relay_id", "state"]),
        tool("set_servo", "Повернуть сервопривод на заданный угол.",
             {"servo_id": S("Идентификатор сервопривода из list_devices."), "angle": I("Целевой угол в градусах (0-180)."),
              "speed": S("Скорость движения.", enum=["slow", "normal", "fast"])}, ["servo_id", "angle"]),
        tool("set_light", "Установить яркость и цвет умной лампы.",
             {"light_id": S("Идентификатор лампы из list_devices."), "brightness": I("Яркость в процентах (0-100)."),
              "color": S("Название цвета или шестнадцатеричный код.")}, ["light_id"]),
        tool("schedule_action", "Запланировать вызов инструмента на потом или периодически.",
             {"action": S("Имя вызываемого инструмента."), "arguments": S("Аргументы инструмента в виде JSON-строки."),
              "cron": S("Выражение cron, например '0 7 * * *' — каждый день в 7:00.")}, ["action", "cron"]),
        tool("get_events", "Вернуть события оборудования с указанного момента: срабатывания датчика движения, переходы порогов, подключения и отключения устройств.",
             {"since": S("Метка времени ISO 8601."), "limit": I("Максимальное число возвращаемых событий.")}, ["since"]),
        tool("i2c", "Работа с устройствами на шине I2C для чтения датчиков и управления периферией. Действия: detect (список шин), scan (поиск устройств на шине), read (чтение байтов с устройства), write (запись байтов в устройство). Только Linux.",
             {"action": S("Действие: detect (список доступных шин I2C), scan (поиск устройств на шине), read (чтение байтов с устройства), write (запись байтов в устройство)", enum=["detect", "scan", "read", "write"]),
              "bus": S("Номер шины I2C (например, \"1\" для /dev/i2c-1). Обязателен для scan/read/write."),
              "address": I("7-битный адрес устройства I2C (0x03-0x77). Обязателен для read/write."),
              "register": I("Адрес регистра для чтения или записи. Если задан, перед чтением или записью отправляется байт регистра."),
              "length": I("Число байтов для чтения (1-256). По умолчанию 1. Используется с действием read.")}, ["action"]),
    ],
}

# ---------- 4. результаты инструментов ----------
ROOMS = {"ru": ["гостиная", "кухня", "спальня", "прихожая", "балкон"], "en": ["living room", "kitchen", "bedroom", "hallway", "balcony"]}
KIND = {"ru": {"temperature": "Датчик температуры", "motion": "Датчик движения", "camera": "Камера", "relay": "Реле света", "servo": "Сервопривод жалюзи"},
        "en": {"temperature": "Temperature sensor", "motion": "Motion sensor", "camera": "Camera", "relay": "Light relay", "servo": "Blind servo"}}
STATUS = {"ru": "в сети", "en": "online"}


def devices(lang):
    out = []
    for i, (k, room) in enumerate([(k, r) for r in range(5) for k in ("temperature", "relay")]):
        out.append({"id": f"{k}_{i}", "type": k, "name": f"{KIND[lang][k]} — {ROOMS[lang][room]}" if lang == "ru" else f"{KIND[lang][k]} — {ROOMS[lang][room]}",
                    "location": ROOMS[lang][room], "status": STATUS[lang]})
    return {"devices": out}


EVENT_TXT = {
    "ru": ["Обнаружено движение в прихожей", "Температура на кухне превысила 28 °C", "Камера у входа: 1 человек, держит коробку",
           "Реле света в спальне выключено по расписанию", "Устройство servo_3 отключилось от шины I2C"],
    "en": ["Motion detected in the hallway", "Kitchen temperature exceeded 28 °C", "Entrance camera: 1 person holding a box",
           "Bedroom light relay switched off by schedule", "Device servo_3 disconnected from the I2C bus"],
}


def events(lang):
    return {"events": [{"ts": f"2026-10-06T0{7 + i // 5}:{10 + i:02d}:00+03:00", "text": EVENT_TXT[lang][i % 5]} for i in range(10)]}


def serial_variants(obj):
    return {
        "compact_utf8 (Go json.Marshal / ensure_ascii=False)": json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
        "indent=2 utf8 (MCP Python SDK _convert_to_content)": json.dumps(obj, ensure_ascii=False, indent=2),
        "json.dumps по умолчанию (ensure_ascii=True)": json.dumps(obj),
    }


SYSTEM = {
    "en": ("You are a home assistant agent running on a Raspberry Pi. You control sensors, cameras and actuators only through the provided tools. "
           "Before changing the state of any device, check its current state. Never invent sensor readings; if a tool fails, say so. "
           "Answer the user briefly, in one or two sentences, in the language of the user. Ask for confirmation before actions that open doors, "
           "turn on heaters or run longer than ten minutes."),
    "ru": ("Ты — агент домашнего ассистента, работающий на Raspberry Pi. Ты управляешь датчиками, камерами и исполнительными устройствами только через предоставленные инструменты. "
           "Прежде чем менять состояние устройства, проверь его текущее состояние. Никогда не выдумывай показания датчиков; если инструмент вернул ошибку, так и скажи. "
           "Отвечай пользователю кратко, одним-двумя предложениями, на языке пользователя. Спрашивай подтверждение перед действиями, которые открывают двери, "
           "включают обогреватели или длятся дольше десяти минут."),
}


def price(model):
    cat = {m["id"]: m for m in json.loads(F_CATALOG.read_text())["data"]}
    p = cat[model]["pricing"]
    return float(p["prompt"]), float(p["completion"])


if __name__ == "__main__":
    en = {r["id"]: r for r in json.loads((DATA / "massive_test_en.json").read_text())}
    ru = {r["id"]: r for r in json.loads((DATA / "massive_test_ru.json").read_text())}
    ids = sorted(set(en) & set(ru), key=int)
    pairs_all = [(en[i]["text"], ru[i]["text"]) for i in ids]
    pairs_iot = [(en[i]["text"], ru[i]["text"]) for i in ids if en[i]["label"].startswith("iot")]
    wmt = [(p["en"], p["ru"]) for p in json.loads((DATA / "wmt19_ruen_val_first1000.json").read_text())]

    print("# o200k_base, tiktoken", tiktoken.__version__)
    r_all = parallel_report("MASSIVE test, все сценарии (команды ассистенту)", pairs_all)
    r_iot = parallel_report("MASSIVE test, сценарий iot (умный дом)", pairs_iot)
    r_wmt = parallel_report("WMT19 newstest2018 ru-en (проза)", wmt)

    print("\n## Определения инструментов (9 шт.: 8 аппаратных + i2c из PicoClaw)")
    td = {}
    for lang in ("en", "ru"):
        defs = TOOLS[lang]
        raw = sum(ntok(json.dumps(d, ensure_ascii=False, separators=(",", ":"))) for d in defs)
        oa = oa_tools(defs)
        pe = pico_tooldefs(defs)
        desc_chars = sum(len(d["function"]["description"]) + sum(len(p.get("description", "")) for p in d["function"]["parameters"]["properties"].values()) for d in defs)
        desc_bytes = sum(len(d["function"]["description"].encode()) + sum(len(p.get("description", "").encode()) for p in d["function"]["parameters"]["properties"].values()) for d in defs)
        td[lang] = dict(raw=raw, oa=oa, pico=pe)
        print(f"  {lang}: сырой JSON o200k={raw}  Cookbook-приближение={oa}  PicoClaw-оценка={pe}  "
              f"PicoClaw/Cookbook={pe/oa:.2f}  описания: {desc_chars} символов, {desc_bytes} байт")
    print(f"  ru/en: сырой JSON {td['ru']['raw']/td['en']['raw']:.3f}; Cookbook {td['ru']['oa']/td['en']['oa']:.3f}; PicoClaw {td['ru']['pico']/td['en']['pico']:.3f}")

    print("\n## Результаты инструментов (JSON)")
    tr = {}
    for label, maker in (("list_devices (10 устройств)", devices), ("get_events (10 событий)", events)):
        for lang in ("en", "ru"):
            for k, s in serial_variants(maker(lang)).items():
                t = ntok(s)
                pe = pico_msg(s, tool_call_id="call_abc123def456")
                tr[(label, lang, k)] = t
                print(f"  {label:28s} {lang} {k:52s} токенов={t:5d}  символов={len(s):5d}  PicoClaw(сообщ.)={pe:5d}  PicoClaw/факт={pe/(t+3+ntok('tool')):.2f}")
    for label in ("list_devices (10 устройств)", "get_events (10 событий)"):
        ks = list(serial_variants({}).keys())
        print(f"  {label}: ru/en компакт={tr[(label,'ru',ks[0])]/tr[(label,'en',ks[0])]:.2f}; "
              f"ru indent2/компакт={tr[(label,'ru',ks[1])]/tr[(label,'ru',ks[0])]:.2f}; ru ensure_ascii/компакт={tr[(label,'ru',ks[2])]/tr[(label,'ru',ks[0])]:.2f}")

    print("\n## Системный промпт (мой перевод)")
    for lang in ("en", "ru"):
        s = SYSTEM[lang]
        print(f"  {lang}: {len(s)} символов, {ntok(s)} токенов, {len(s)/ntok(s):.2f} симв/ток, PicoClaw-оценка {pico_msg(s)} vs факт {oa_msg('system', s)}")

    print("\n## Ход агента: system + tools + история(4 реплики) + команда + tool_call + результат + ответ")
    # Ответ ассистента: медианное по длине предложение WMT как прокси прозы ответа.
    wmt_sorted = sorted(wmt, key=lambda p: len(p[1]))
    ans_en, ans_ru = wmt_sorted[len(wmt_sorted) // 4]  # короткое (1-й квартиль) предложение
    cmd_en, cmd_ru = pairs_iot[3]
    hist = pairs_all[100:104]
    configs = {
        "A: всё по-английски": ("en", "en", "en"),
        "B: пользователь ru, system+tools en, результаты ru": ("ru", "en", "ru"),
        "C: всё по-русски": ("ru", "ru", "ru"),
    }
    turn = {}
    for cname, (ulang, slang, rlang) in configs.items():
        sys_t = oa_msg("system", SYSTEM[slang])
        tools_t = oa_tools(TOOLS[slang])
        hist_t = sum(oa_msg("user" if j % 2 == 0 else "assistant", (e if ulang == "en" else r)) for j, (e, r) in enumerate(hist))
        cmd_t = oa_msg("user", cmd_en if ulang == "en" else cmd_ru)
        call_args = json.dumps({"relay_id": "relay_3", "state": "off"})
        call_t = 3 + ntok("assistant") + ntok("set_relay") + ntok(call_args) + 4
        res = json.dumps(devices(rlang), ensure_ascii=False, separators=(",", ":"))
        res_t = oa_msg("tool", res)
        ans_t = ntok(ans_en if ulang == "en" else ans_ru)
        # два вызова LLM: (1) выбор инструмента, (2) ответ после результата
        in1 = sys_t + tools_t + hist_t + cmd_t + 3
        in2 = in1 - 3 + call_t + res_t + 3
        out = (ntok("set_relay") + ntok(call_args) + 4) + ans_t
        turn[cname] = (in1 + in2, out)
        print(f"  {cname:52s} вход={in1 + in2:5d} (system {sys_t}, tools {tools_t}, история {hist_t}, команда {cmd_t}, результат {res_t})  выход={out}")
    print("\n  Цена 200 таких ходов в сутки, ₽ (без кэша; цены — снимок RouterAI 2026-10-06):")
    for model in ("openai/gpt-4.1-nano", "openai/gpt-4o-mini", "openai/gpt-5-nano", "openai/gpt-5-mini"):
        pi, po = price(model)
        row = []
        for cname, (tin, tout) in turn.items():
            row.append(200 * (tin * pi + tout * po))
        print(f"  {model:22s} " + "  ".join(f"{c.split(':')[0]}={v:6.2f}" for c, v in zip(turn, row)) + f"   C/A={row[2]/row[0]:.2f}  C/B={row[2]/row[1]:.2f}")

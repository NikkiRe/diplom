"""Шаблоны команд из MASSIVE (сценарий iot, ru-RU и en-US) с привязкой к устройствам помещения.

Источник: MASSIVE (FitzGerald et al., ACL 2023, CC BY 4.0), сплит test, через mteb/amazon_massive_intent —
выгрузка research/new_topic/GAP4_artifacts/data/massive_test_{ru,en}.json (220 параллельных пар iot,
общий id). Здесь каждая пара вручную размечена (таблица CURATION):

* action — нормализованное намерение, которое генератор превращает в вызовы инструментов sim-mcp;
* room_mode — откуда берётся комната:
    slot    — в тексте была комната; она заменена плейсхолдером ({loc} — «на кухне», {acc} — «на кухню»,
              {room} — «kitchen»), генератор подставляет любую комнату дома (или отсутствующую — ловушка);
    context — комнаты в тексте нет («выключи свет»): цель — комната, где последним было движение;
    fixed   — комната зашита в тексте и не подставляется («спальню хозяина», «комнате моего сына»);
    none    — устройство без комнаты (розетка, кофемашина, пылесос, «весь свет»);
* params — значение из текста (цвет, яркость) или цель невалидной команды.

Отброшены (EXCLUDED, с причиной): команды со временем и расписанием, вопросы «как …», пары с разным
смыслом ru/en (MASSIVE переводили разные люди: «выключи свет» ↔ «turn down lights»), команды без
однозначного значения («поменяй цвет света»), не про устройства стенда (экран, сок).

Сборка: ``python -m bench.daygen massive-build`` пишет в data/massive/:
``iot_ru_en.jsonl`` (220 исходных пар), ``iot_templates.json`` (размеченные шаблоны), PROVENANCE.txt.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SRC = PROJECT_ROOT / "research" / "new_topic" / "GAP4_artifacts" / "data"
DEFAULT_OUT = PROJECT_ROOT / "data" / "massive"
TEMPLATES_FILE = "iot_templates.json"

ACTIONS = {
    # action: (класс задачи по §12.8, описание)
    "light_on": ("control", "включить свет в комнате"),
    "light_off": ("control", "выключить свет в комнате"),
    "light_up": ("control", "ярче (или включить, если выключен)"),
    "light_dim": ("control", "приглушить"),
    "light_color": ("control", "цвет света"),
    "light_brightness": ("control", "яркость в процентах"),
    "light_on_all": ("multi", "включить весь свет"),
    "light_off_all": ("multi", "выключить весь свет"),
    "light_color_all": ("multi", "цвет всего света"),
    "all_off": ("multi", "выключить все активные устройства"),
    "plug_on": ("control", "включить умную розетку"),
    "plug_off": ("control", "выключить умную розетку"),
    "coffee_brew": ("control", "сварить кофе"),
    "vacuum_start": ("control", "запустить робот-пылесос (в комнате или везде)"),
    "invalid": ("invalid", "устройства нет — ожидается отказ"),
}

N, C, S, F = "none", "context", "slot", "fixed"

# id: (action, room_mode, ru_tpl | None, en_tpl | None, params). None в шаблоне = исходный текст.
CURATION: dict[str, tuple[str, str, str | None, str | None, dict]] = {
    # --- iot_cleaning ---
    "31": ("vacuum_start", S, "пропылесось {loc}", "hoover the {room}", {}),
    "290": ("vacuum_start", N, None, None, {}),
    "292": ("vacuum_start", N, None, None, {}),
    "635": ("vacuum_start", N, None, None, {}),
    "843": ("vacuum_start", N, None, None, {}),
    "864": ("vacuum_start", S, "робот пылесос {acc}", "robot vacuum the {room} now", {}),
    "1059": ("vacuum_start", N, None, None, {}),
    "1151": ("vacuum_start", N, None, None, {}),
    "1575": ("vacuum_start", N, None, None, {}),
    "1924": ("vacuum_start", N, None, None, {}),
    "1941": ("vacuum_start", N, None, None, {}),
    "2434": ("vacuum_start", N, None, None, {}),
    "2903": ("vacuum_start", N, None, None, {}),
    "2959": ("vacuum_start", N, None, None, {}),
    "2960": ("vacuum_start", N, None, None, {}),
    "3841": ("vacuum_start", N, None, None, {}),
    "4013": ("vacuum_start", N, None, None, {}),
    "4043": ("vacuum_start", N, None, None, {}),
    "4173": ("vacuum_start", N, None, None, {}),
    "4240": ("vacuum_start", N, None, None, {}),
    "5330": ("vacuum_start", S, "включи пылесос и начни уборку {loc}",
             "put on the vacuum on now and clean the {room}", {}),
    "5697": ("vacuum_start", N, None, None, {}),
    "6159": ("vacuum_start", N, None, None, {}),
    # --- iot_coffee ---
    **{i: ("coffee_brew", N, None, None, {}) for i in (
        "448", "1276", "1413", "1812", "2026", "2733", "2753", "2812", "2868", "3320", "3321", "3323",
        "4084", "4244", "4271", "4272", "4554", "4988", "5017", "5149", "5917", "6034", "6060", "6061")},
    # --- iot_hue_lightchange ---
    "8": ("light_color", C, None, None, {"color": "pink"}),
    "263": ("light_color", C, None, None, {"color": "blue"}),
    "456": ("light_color", S, "поменяй свет {loc} на зелёный", "change the {room} lights to green",
            {"color": "green"}),
    "1217": ("light_color", C, None, None, {"color": "blue"}),
    "1427": ("light_color", C, None, None, {"color": "blue"}),
    "1428": ("light_color", C, None, None, {"color": "red"}),
    "1960": ("light_color", C, None, None, {"color": "pink"}),
    "2283": ("light_color_all", N, None, None, {"color": "blue"}),
    "2540": ("light_color", C, None, None, {"color": "blue"}),
    "2551": ("light_color_all", N, None, None, {"color": "green"}),
    "2816": ("light_brightness", S, "установи освещение {loc} на пятьдесят процентов",
             "set the {room} lights to fifty percent", {"brightness": 50}),
    "2906": ("light_brightness", C, None, None, {"brightness": 90}),
    "2975": ("light_color", C, None, None, {"color": "red"}),
    "3387": ("light_color", C, None, None, {"color": "red"}),
    "3734": ("light_color", C, None, None, {"color": "red"}),
    "4247": ("light_color_all", N, None, None, {"color": "blue"}),
    "4248": ("light_color_all", N, None, None, {"color": "blue"}),
    "4249": ("light_color_all", N, None, None, {"color": "blue"}),
    "4826": ("light_color", C, None, None, {"color": "pink"}),
    "5026": ("light_color", C, None, None, {"color": "white"}),
    # --- iot_hue_lightdim ---
    **{i: ("light_dim", C, None, None, {}) for i in (
        "158", "468", "665", "702", "964", "1778", "3222", "3280", "4896", "5165", "5201", "5349", "5988",
        "6015")},
    "1030": ("light_dim", S, "приглуши свет {loc}", "dim the lights in the {room}", {}),
    "2016": ("light_dim", S, "приглуши свет {loc}", "lower lights in {room}", {}),
    # --- iot_hue_lightoff ---
    "19": ("light_off", S, "olly выключи свет {loc}", "olly turn the lights off in the {room}", {}),
    "427": ("light_off", S, "выключи свет {loc}", "turn off the lights in the {room}", {}),
    "428": ("light_off", S, "выключи свет {loc}", "turn off the {room} lights", {}),
    "997": ("light_off", S, "выключи свет {loc}", "turn the {room}'s light off", {}),
    "1784": ("light_off", S, "выключи {loc}", "turn off the {room}", {}),
    "2230": ("light_off", S, "выключи свет {loc}", "turn off {room} light", {}),
    "2441": ("light_off", S, "выключи свет {loc}", "turn off the {room} light", {}),
    "2578": ("light_off", S, "выключи свет {loc}", "turn off the {room} light", {}),
    "2965": ("light_off", S, "выключи свет {loc}", "turn {room} lights off", {}),
    "3378": ("light_off", S, "выклю свет {loc}", "no lights in the {room}", {}),
    "3570": ("light_off", S, "выключи свет {loc}", "please turn off the lights in {room}", {}),
    "3785": ("light_off", S, "выключи свет {loc} пожалуйста", "turn off the {room} light please", {}),
    "4077": ("light_off", S, "выключи {loc} свет", "turn the {room} lights off", {}),
    "4171": ("light_off", S, "выключи свет {loc}", "turn off the lights in the {room}", {}),
    "5156": ("light_off", S, "выключи свет {loc}", "turn the light off in the {room}", {}),
    "5157": ("light_off", S, "олли выключи свет {loc}", "olly turn the light off in the {room}", {}),
    "5243": ("light_off", S, "siri выключи свет {loc}", "siri please turn the lights off in the {room}", {}),
    "6073": ("light_off", S, "выключи свет {loc}", "turn off {room} light", {}),
    "1786": ("light_off", F, None, None, {"room": "bedroom"}),
    "3167": ("light_off", F, None, None, {"room": "bedroom"}),
    "1377": ("light_off", F, None, None, {"room": "nursery"}),
    **{i: ("light_off", C, None, None, {}) for i in (
        "369", "651", "730", "914", "999", "1554", "2298", "2711", "2774", "2775", "3098", "3993", "4088",
        "4346", "4347", "5584", "6041")},
    "1750": ("light_off_all", N, None, None, {}),
    "2160": ("light_off_all", N, None, None, {}),
    "6055": ("invalid", F, None, None, {"room": "garage", "target": "light_garage"}),
    # --- iot_hue_lighton ---
    "286": ("light_on", S, "зажги свет {loc}", "light up the lights in the {room}", {}),
    "3522": ("light_on", S, "пожалуйста включи свет {loc}", "please turn on my {room} lights", {}),
    # --- iot_hue_lightup ---
    **{i: ("light_up", C, None, None, {}) for i in (
        "80", "962", "1090", "1360", "1756", "1804", "2075", "2403", "2591", "3595", "3762", "3883", "4283",
        "5014", "6174")},
    # ru «включи свет» ↔ en «turn up / raise the lights»: согласованный смысл только при выключенном свете
    **{i: ("light_on", C, None, None, {}) for i in ("1993", "2082", "2234", "3008")},
    "1071": ("light_up", S, "сделай свет поярче {loc}", "brighten the lights in the {room}", {}),
    "5574": ("light_up", S, "сделай поярче свет {loc}", "brighten of lights in {room}", {}),
    "5727": ("light_up", S, "сделай свет {loc} ярче", "brighten the light of {room}", {}),
    "5501": ("light_on_all", N, None, None, {}),
    # --- iot_wemo_off / iot_wemo_on ---
    **{i: ("plug_off", N, None, None, {}) for i in (
        "321", "2566", "2690", "3077", "3298", "3326", "3774", "3775", "4966", "4992", "5396", "5652", "5856")},
    "5050": ("all_off", N, None, None, {}),
    "4154": ("invalid", N, None, None, {"target": "wifi"}),
    **{i: ("plug_on", N, None, None, {}) for i in ("322", "389", "426", "1551", "4618", "4620")},
    "13548": ("invalid", N, None, None, {"target": "tv"}),
}

EXCLUDED: dict[str, str] = {
    "27": "en «make some noise» не соответствует ru «приберись»",
    "1703": "расписание (каждый день 10–11)",
    "2517": "вопрос «как запустить»",
    "2288": "время (в шесть утра)", "3418": "время", "4442": "время", "4443": "время", "4798": "каждый день",
    "2512": "вопрос «как настроить»", "3568": "неоднозначно («настрой кофемашину»)",
    "3138": "невыполнимые добавки", "3196": "невыполнимые добавки", "5010": "«как обычно» — нужен профиль",
    "11914": "напиток, не кофе", "11915": "напиток, не кофе",
    "564": "цвет не указан", "897": "время", "1511": "вопрос о доступных цветах", "2350": "«тёмные цвета»",
    "2351": "«тёмные цвета»", "2542": "мусор delet в ru и «нормальный цвет»", "3050": "две команды, «мой свет»",
    "3542": "ru «с зелёного на красный» ↔ en «green and red»", "3543": "относительный цвет",
    "3674": "цвет не указан", "3700": "значение не указано", "4174": "неоднозначно", "4299": "относительный цвет",
    "4516": "цвет не указан", "4803": "цвет не указан", "4955": "не команда",
    "2128": "бессмысленно", "2263": "не команда", "2311": "«до семи» — неясная шкала",
    "3834": "ru «выключи» ↔ en «turn down»", "5047": "мусор delet, ru «выключи» ↔ en «turn down»",
    "3386": "мусор delet, ru ↔ en", "6074": "время (в 21:30)", "14": "образно («опустилась темнота»)",
    "1072": "яркость экрана", "2264": "не команда", "3011": "неоднозначно («верни свет»)",
    "5573": "«соответствующую яркость» — значение не указано",
    "4024": "неоднозначно («выключи питание»)", "4026": "неоднозначно («выключи»)",
    "5888": "неоднозначно («выключи пожалуйста»)",
    "3184": "не про устройства стенда (сок)", "4201": "ru «пылесос» ↔ en «wemo»",
    "4799": "ru «выключи» ↔ en «turn on»",
}


def load_source(src_dir: Path = DEFAULT_SRC) -> list[dict]:
    """220 параллельных пар iot: [{id, label, ru, en}] в порядке возрастания id."""
    ru = json.loads((src_dir / "massive_test_ru.json").read_text(encoding="utf-8"))
    en = {r["id"]: r for r in json.loads((src_dir / "massive_test_en.json").read_text(encoding="utf-8"))}
    out = []
    for r in ru:
        if not r["label"].startswith("iot_"):
            continue
        e = en[r["id"]]
        if e["label"] != r["label"]:
            raise ValueError(f"id {r['id']}: метки ru/en различаются")
        out.append({"id": r["id"], "label": r["label"], "ru": r["text"], "en": e["text"]})
    out.sort(key=lambda x: int(x["id"]))
    return out


def _check_template(tid: str, mode: str, ru_tpl: str | None, en_tpl: str | None) -> None:
    if mode == S:
        if not ru_tpl or not en_tpl:
            raise ValueError(f"{tid}: у slot-шаблона нет текста")
        if ("{loc}" not in ru_tpl and "{acc}" not in ru_tpl) or "{room}" not in en_tpl:
            raise ValueError(f"{tid}: в шаблоне нет плейсхолдера комнаты")
    elif ru_tpl or en_tpl:
        raise ValueError(f"{tid}: шаблон задан для room_mode={mode}")


def build_templates(pairs: list[dict]) -> list[dict]:
    by_id = {p["id"]: p for p in pairs}
    unknown = (set(CURATION) | set(EXCLUDED)) - set(by_id)
    if unknown:
        raise ValueError(f"в источнике нет id {sorted(unknown)}")
    both = set(CURATION) & set(EXCLUDED)
    if both:
        raise ValueError(f"id одновременно размечены и исключены: {sorted(both)}")
    missing = set(by_id) - set(CURATION) - set(EXCLUDED)
    if missing:
        raise ValueError(f"не размечены id {sorted(missing, key=int)}")
    out = []
    for tid in sorted(CURATION, key=int):
        action, mode, ru_tpl, en_tpl, params = CURATION[tid]
        if action not in ACTIONS:
            raise ValueError(f"{tid}: неизвестное действие {action}")
        _check_template(tid, mode, ru_tpl, en_tpl)
        p = by_id[tid]
        out.append({
            "id": tid, "label": p["label"], "action": action, "class": ACTIONS[action][0],
            "room_mode": mode, "ru": p["ru"], "en": p["en"],
            "ru_tpl": ru_tpl, "en_tpl": en_tpl, "params": params,
        })
    return out


def build(src_dir: Path = DEFAULT_SRC, out_dir: Path = DEFAULT_OUT) -> dict:
    pairs = load_source(src_dir)
    templates = build_templates(pairs)
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs_text = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n" for p in pairs)
    (out_dir / "iot_ru_en.jsonl").write_text(pairs_text, encoding="utf-8")
    doc = {
        "format": "massive-iot-templates/1",
        "source": "MASSIVE test split (FitzGerald et al., ACL 2023), CC BY 4.0, via mteb/amazon_massive_intent",
        "n_source_pairs": len(pairs),
        "n_templates": len(templates),
        "excluded": {k: EXCLUDED[k] for k in sorted(EXCLUDED, key=int)},
        "actions": {k: {"class": v[0], "description": v[1]} for k, v in ACTIONS.items()},
        "templates": templates,
    }
    text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    (out_dir / TEMPLATES_FILE).write_text(text, encoding="utf-8")
    sha_src = {f: hashlib.sha256((src_dir / f).read_bytes()).hexdigest()
               for f in ("massive_test_ru.json", "massive_test_en.json")}
    prov = f"""Шаблоны команд iot из MASSIVE для daygen (bench/daygen/massive.py).

Источник: MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset with 51 Typologically-Diverse
Languages (FitzGerald et al., ACL 2023), сплит test, конфиги ru-RU и en-US, полученные через HF datasets-server
(mteb/amazon_massive_intent) скриптом research/new_topic/GAP4_artifacts/fetch_massive.py.
Лицензия: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
Исходные файлы (SHA-256):
  massive_test_ru.json {sha_src['massive_test_ru.json']}
  massive_test_en.json {sha_src['massive_test_en.json']}

Файлы:
  iot_ru_en.jsonl     — все {len(pairs)} параллельных пар сценария iot (id, label, ru, en), без изменений;
  iot_templates.json  — {len(templates)} размеченных шаблонов: действие, способ выбора комнаты, параметры;
                        в slot-шаблонах комната заменена плейсхолдером (изменение текста — наше);
                        {len(EXCLUDED)} пар исключены с указанием причины (поле excluded).
Воспроизведение: .venv/bin/python -m bench.daygen massive-build
"""
    (out_dir / "PROVENANCE.txt").write_text(prov, encoding="utf-8")
    return doc


def load_templates(path: Path | None = None) -> list[dict]:
    path = path or (DEFAULT_OUT / TEMPLATES_FILE)
    if not path.exists():
        raise FileNotFoundError(f"нет {path}: соберите командой `python -m bench.daygen massive-build`")
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc["templates"]

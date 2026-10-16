"""Модель помещения: комнаты, классы устройств, инвентарь для заголовка дня.

Инвентарь строится по набору локаций датчиков выбранного дома CASAS (или синтетического дома) и
дополняется синтетическими устройствами сценария S-room (климат, котёл, протечка, кран, розетка,
кофеварка, робот-пылесос). Формат устройства в заголовке дня (поле ``devices``) — общий для daygen и
sim-mcp; sim-mcp строит из него каталог инструментов generic/explicit (IMPLEMENTATION_SPEC §4).

Устройство::

    {"id": "light_kitchen", "type": "light", "location": "kitchen",
     "title": {"ru": "свет на кухне", "en": "kitchen light"},
     "properties": {"on": {"type": "boolean", "rw": true}, ...},
     "actions": {"brew": {"params": {...}, "effect": {...}}}}

Свойство: ``type`` (boolean | integer | number | string), ``rw`` (запись разрешена), необязательные
``unit`` (UCUM-подобные: Cel, %RH, Pa, %), ``min``/``max``, ``enum``, ``resolution`` (точность датчика,
до неё округляются значения в трассе и в ответах).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Комнаты
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Room:
    """Комната и её словоформы для шаблонов команд.

    ru_loc — предложная группа «где?» («на кухне»), ru_acc — «куда?» («на кухню»),
    en — существительное без артикля («living room»).
    """

    id: str
    ru_nom: str
    ru_loc: str
    ru_acc: str
    en: str


ROOMS: dict[str, Room] = {r.id: r for r in [
    Room("bathroom", "ванная", "в ванной", "в ванную", "bathroom"),
    Room("bedroom", "спальня", "в спальне", "в спальню", "bedroom"),
    Room("kitchen", "кухня", "на кухне", "на кухню", "kitchen"),
    Room("living_room", "гостиная", "в гостиной", "в гостиную", "living room"),
    Room("dining_room", "столовая", "в столовой", "в столовую", "dining room"),
    Room("work_area", "рабочая зона", "в рабочей зоне", "в рабочую зону", "work area"),
    Room("office", "кабинет", "в кабинете", "в кабинет", "office"),
    Room("entrance", "прихожая", "в прихожей", "в прихожую", "hallway"),
]}

# Комнаты, которых нет ни в одном доме стенда: подставляются в шаблоны для ловушек «невалидная команда».
ABSENT_ROOMS: dict[str, Room] = {r.id: r for r in [
    Room("balcony", "балкон", "на балконе", "на балкон", "balcony"),
    Room("garage", "гараж", "в гараже", "в гараж", "garage"),
    Room("porch", "крыльцо", "на крыльце", "на крыльцо", "porch"),
    Room("shed", "сарай", "в сарае", "в сарай", "shed"),
    Room("patio", "внутренний дворик", "во внутреннем дворике", "во внутренний дворик", "patio"),
    Room("nursery", "детская", "в детской", "в детскую", "nursery"),
]}

ALL_ROOMS: dict[str, Room] = {**ROOMS, **ABSENT_ROOMS}

# Локация датчика CASAS -> (идентификатор локации датчика, комната).
# LoungeChair — датчик над креслом в гостиной; OutsideDoor — зона у входной двери.
CASAS_LOCATIONS: dict[str, tuple[str, str]] = {
    "Bathroom": ("bathroom", "bathroom"),
    "Bedroom": ("bedroom", "bedroom"),
    "Kitchen": ("kitchen", "kitchen"),
    "LivingRoom": ("living_room", "living_room"),
    "DiningRoom": ("dining_room", "dining_room"),
    "WorkArea": ("work_area", "work_area"),
    "Office": ("office", "office"),
    "LoungeChair": ("lounge_chair", "living_room"),
    "OutsideDoor": ("entrance", "entrance"),
}

# Цвета лампы. «Голубой» MASSIVE переводит как blue, поэтому отдельного cyan нет (иначе ответ неоднозначен).
COLORS: dict[str, tuple[str, ...]] = {
    "white": ("белый", "белым", "естественный"),
    "warm": ("тёплый", "теплый", "тёплый белый"),
    "red": ("красный", "красным"),
    "orange": ("оранжевый", "оранжевым"),
    "amber": ("янтарный", "янтарным"),
    "yellow": ("жёлтый", "желтый", "жёлтым"),
    "green": ("зелёный", "зеленый", "зелёным", "зеленым"),
    "blue": ("синий", "синим", "голубой", "голубым"),
    "purple": ("фиолетовый", "фиолетовым"),
    "pink": ("розовый", "розовым"),
}

# ---------------------------------------------------------------------------
# Классы устройств
# ---------------------------------------------------------------------------

BRIGHTNESS = {"type": "integer", "unit": "%", "min": 1, "max": 100, "rw": True}
BIN01 = {"type": "integer", "enum": [0, 1], "rw": False}


def _light(room: Room) -> dict:
    return {
        "id": f"light_{room.id}", "type": "light", "location": room.id,
        "title": {"ru": f"свет {room.ru_loc}", "en": f"{room.en} light"},
        "properties": {
            "on": {"type": "boolean", "rw": True},
            "brightness": dict(BRIGHTNESS),
            "color": {"type": "string", "enum": sorted(COLORS), "rw": True},
        },
        "actions": {},
    }


def _pir(loc_id: str, room: Room, casas_name: str | None) -> dict:
    d = {
        "id": f"pir_{loc_id}", "type": "motion_sensor", "location": room.id,
        "title": {"ru": f"датчик движения {room.ru_loc}", "en": f"{room.en} motion sensor"},
        "properties": {"motion": dict(BIN01)},
        "actions": {},
    }
    if loc_id == "lounge_chair":
        d["title"] = {"ru": "датчик движения у кресла в гостиной", "en": "lounge chair motion sensor"}
    if casas_name:
        d["source"] = f"casas:{casas_name}"
    return d


def _contact(loc_id: str, room: Room, casas_name: str | None) -> dict:
    d = {
        "id": f"contact_{loc_id}", "type": "contact_sensor", "location": room.id,
        "title": {"ru": f"датчик дверцы шкафа {room.ru_loc}", "en": f"{room.en} cabinet contact"},
        "properties": {"open": dict(BIN01)},
        "actions": {},
    }
    if casas_name:
        d["source"] = f"casas:{casas_name}"
    return d


def _door(casas_name: str | None) -> dict:
    d = {
        "id": "door_front", "type": "door_sensor", "location": "entrance",
        "title": {"ru": "входная дверь", "en": "front door"},
        "properties": {"open": dict(BIN01)},
        "actions": {},
    }
    if casas_name:
        d["source"] = f"casas:{casas_name}"
    return d


def synthetic_devices(rooms: list[str]) -> list[dict]:
    """Синтетические устройства S-room (не из CASAS): привязаны к комнатам, если они есть в доме."""
    clim_room = "living_room" if "living_room" in rooms else rooms[0]
    leak_room = "bathroom" if "bathroom" in rooms else rooms[0]
    kitchen = "kitchen" if "kitchen" in rooms else rooms[0]
    base = "entrance" if "entrance" in rooms else rooms[0]
    r = ALL_ROOMS
    return [
        {
            "id": f"thermo_{clim_room}", "type": "climate_sensor", "model": "BME280", "location": clim_room,
            "title": {"ru": f"климат-датчик {r[clim_room].ru_loc}", "en": f"{r[clim_room].en} climate sensor"},
            "properties": {
                "temperature": {"type": "number", "unit": "Cel", "resolution": 0.1, "rw": False},
                "humidity": {"type": "number", "unit": "%RH", "resolution": 1, "rw": False},
                "pressure": {"type": "number", "unit": "Pa", "resolution": 100, "rw": False},
            },
            "actions": {},
        },
        {
            "id": "boiler", "type": "boiler", "model": "DS18B20+relay", "location": base,
            "title": {"ru": "котёл (температура подачи)", "en": "boiler (supply temperature)"},
            "properties": {
                "temperature": {"type": "number", "unit": "Cel", "resolution": 0.1, "rw": False},
                "heating": {"type": "boolean", "rw": True},
            },
            "actions": {},
        },
        {
            "id": f"leak_{leak_room}", "type": "leak_sensor", "location": leak_room,
            "title": {"ru": f"датчик протечки {r[leak_room].ru_loc}", "en": f"{r[leak_room].en} leak sensor"},
            "properties": {"wet": dict(BIN01)},
            "actions": {},
        },
        {
            "id": "valve_water", "type": "valve", "location": base,
            "title": {"ru": "кран подачи воды", "en": "water supply valve"},
            "properties": {"open": {"type": "boolean", "rw": True}},
            "actions": {},
        },
        {
            "id": f"plug_{clim_room}", "type": "plug", "location": clim_room,
            "title": {"ru": "умная розетка", "en": "smart plug"},
            "properties": {"on": {"type": "boolean", "rw": True}},
            "actions": {},
        },
        {
            "id": f"coffee_{kitchen}", "type": "coffee_maker", "location": kitchen,
            "title": {"ru": "кофемашина", "en": "coffee maker"},
            "properties": {"brewing": {"type": "boolean", "rw": False}},
            "actions": {
                "brew": {"description": "Brew a cup of coffee (takes 300 s)", "params": {},
                         "effect": {"busy_if": {"brewing": True}, "set": {"brewing": True},
                                    "duration_s": 300, "after": {"brewing": False}}},
            },
        },
        {
            "id": "vacuum", "type": "vacuum", "location": base,
            "title": {"ru": "робот-пылесос", "en": "robot vacuum"},
            "properties": {
                "cleaning": {"type": "boolean", "rw": False},
                "zone": {"type": "string", "enum": ["dock", "all"] + sorted(rooms), "rw": False},
            },
            "actions": {
                "start": {"description": "Start cleaning the whole home or one room (takes 1800 s)",
                          "params": {"room": {"type": "string", "enum": sorted(rooms)}},
                          "effect": {"busy_if": {"cleaning": True}, "set": {"cleaning": True, "zone": "all"},
                                     "set_from_params": {"zone": "room"},
                                     "duration_s": 1800, "after": {"cleaning": False, "zone": "dock"}}},
                "dock": {"description": "Stop cleaning and return to the dock", "params": {},
                         "effect": {"set": {"cleaning": False, "zone": "dock"}}},
            },
        },
    ]


def build_inventory(casas_locations: dict[str, set[str]]) -> tuple[list[dict], list[str]]:
    """Инвентарь дома.

    casas_locations: имя локации CASAS -> множество типов сообщений ({"motion", "contact"}).
    Возвращает (устройства, комнаты); порядок устройств детерминированный (по id).
    """
    devices: list[dict] = []
    rooms: set[str] = set()
    for name in sorted(casas_locations):
        if name not in CASAS_LOCATIONS:
            continue
        loc_id, room_id = CASAS_LOCATIONS[name]
        room = ROOMS[room_id]
        rooms.add(room_id)
        kinds = casas_locations[name]
        if "motion" in kinds:
            devices.append(_pir(loc_id, room, name))
        if "contact" in kinds:
            devices.append(_door(name) if name == "OutsideDoor" else _contact(loc_id, room, name))
    room_list = sorted(rooms)
    if not room_list:
        raise ValueError("в доме нет ни одной известной локации датчиков")
    for rid in room_list:
        devices.append(_light(ROOMS[rid]))
    devices += synthetic_devices(room_list)
    devices.sort(key=lambda d: d["id"])
    return devices, room_list


def room_table(rooms: list[str]) -> dict[str, dict]:
    """Словоформы комнат дома (для заголовка дня и проверки ответов)."""
    return {rid: {"ru": ALL_ROOMS[rid].ru_nom, "ru_loc": ALL_ROOMS[rid].ru_loc, "en": ALL_ROOMS[rid].en}
            for rid in rooms}


def default_state(devices: list[dict]) -> dict[str, dict]:
    """Начальное состояние по умолчанию (до наложения трассы CASAS и синтетики)."""
    st: dict[str, dict] = {}
    for d in devices:
        s: dict = {}
        for p, spec in d["properties"].items():
            if "enum" in spec and spec["type"] == "integer":
                s[p] = 0
            elif spec["type"] == "boolean":
                s[p] = False
            elif p == "brightness":
                s[p] = 80
            elif p == "color":
                s[p] = "white"
            elif p == "zone":
                s[p] = "dock"
            else:
                s[p] = None  # числовые датчики заполняются синтетикой
        if d["type"] == "boiler":
            s["heating"] = True
        if d["type"] == "valve":
            s["open"] = True
        st[d["id"]] = s
    return st


def device_index(devices: list[dict]) -> dict[str, dict]:
    return {d["id"]: d for d in devices}


def lights(devices: list[dict]) -> list[dict]:
    return [d for d in devices if d["type"] == "light"]


def find_one(devices: list[dict], dev_type: str) -> dict | None:
    for d in devices:
        if d["type"] == dev_type:
            return d
    return None


def clone(devices: list[dict]) -> list[dict]:
    return copy.deepcopy(devices)

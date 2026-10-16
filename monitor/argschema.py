"""Мини-валидатор JSON Schema для аргументов вызовов инструментов.

Собственная реализация (без пакета jsonschema), чтобы монитор не зависел от сторонних библиотек и чтобы было
точно известно, какие ключевые слова проверяются. Семантика — draft 2020-12, где не оговорено иное.

Проверяемые ключевые слова:
  type (строка или список: null, boolean, integer, number, string, array, object), enum, const,
  properties, required, additionalProperties (bool или схема), items (схема; список — кортежная форма
  draft-04..2019-09), minimum, maximum, exclusiveMinimum, exclusiveMaximum (числовая форма и булева форма
  draft-04), minLength, maxLength, minItems, maxItems, uniqueItems, minProperties, maxProperties,
  anyOf, oneOf, allOf; расширение OpenAPI ``nullable: true``.

Аннотации (description, title, default, examples, format, ...) ничего не утверждают и пропускаются молча.
Все прочие ключевые слова не проверяются и возвращаются функцией ``check_schema`` как «непроверенные»:
валидатор не выдаёт из-за них ложных срабатываний, но может пропустить нарушение, и отчёт монитора об этом
сообщает явно.

Тонкости типов, которые легко сделать неправильно в Python:
  * ``True``/``False`` — не integer и не number (в Python ``bool`` — подкласс ``int``);
  * integer — любое число с нулевой дробной частью, т.е. ``1.0`` тоже integer (так требует JSON Schema);
    режим ``strict_integer=True`` принимает только целые литералы;
  * сравнение в enum/const/uniqueItems — по правилам JSON: ``1 == 1.0``, но ``1 != true``.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

JSON_TYPES = ("null", "boolean", "integer", "number", "string", "array", "object")

# Частые не-JSON-Schema имена типов (BFCL, Python) — для понятной подсказки в сообщении о проблеме каталога.
_TYPE_HINTS = {
    "dict": "object", "float": "number", "int": "integer", "str": "string", "bool": "boolean",
    "list": "array", "tuple": "array", "any": "(без ограничения типа)",
}

# Ключевые слова, которые валидатор проверяет.
CHECKED_KEYWORDS = frozenset({
    "type", "enum", "const", "properties", "required", "additionalProperties", "items",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
    "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties", "anyOf", "oneOf", "allOf",
    "nullable",
})

# Аннотации: по стандарту не влияют на результат проверки. "optional" — устаревшее поле BFCL,
# "$defs"/"definitions" — контейнеры для $ref (сам $ref не поддерживается и попадает в непроверенные).
ANNOTATION_KEYWORDS = frozenset({
    "description", "title", "default", "examples", "$schema", "$id", "$anchor", "$comment", "format",
    "deprecated", "readOnly", "writeOnly", "contentMediaType", "contentEncoding", "contentSchema",
    "optional", "$defs", "definitions",
})


@dataclass(frozen=True)
class ArgError:
    """Одно нарушение схемы.

    kind — вид нарушения: type, required, enum, const, additional, bounds, length, count, unique,
    combinator, false_schema. path — JSON Pointer на проверяемое значение ("" — корень).
    """
    kind: str
    path: str
    message: str


@dataclass
class SchemaCheck:
    """Результат проверки самой схемы: problems — ошибки (схема некорректна или вне подмножества),
    unchecked — ключевые слова, которые встретились, но валидатором не проверяются."""
    problems: list[str] = field(default_factory=list)
    unchecked: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------------------------------------
# Типы и равенство по правилам JSON

def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def type_matches(value, type_name: str, strict_integer: bool = False) -> bool:
    """Соответствует ли значение (результат json.loads) типу JSON Schema."""
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return (not strict_integer) and isinstance(value, float) and value.is_integer()
    if type_name == "number":
        return is_number(value) and not (isinstance(value, float) and not math.isfinite(value))
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def json_type_name(value) -> str:
    """Имя JSON-типа значения (integer для целых литералов) — для сообщений."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def json_equal(a, b) -> bool:
    """Равенство JSON-значений: числа сравниваются по величине, bool отличается от чисел."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_number(a) and is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if type(a) is not type(b):
        return False
    return a == b


def _pointer(path: str, token) -> str:
    """Добавить сегмент к JSON Pointer (RFC 6901: '~' -> '~0', '/' -> '~1')."""
    return f"{path}/{str(token).replace('~', '~0').replace('/', '~1')}"


def _short(value, limit: int = 60) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------------------------------------
# Проверка самой схемы

def _is_nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def check_schema(schema, path: str = "#") -> SchemaCheck:
    """Проверить, что схема корректна и укладывается в поддерживаемое подмножество."""
    result = SchemaCheck()
    _check_schema(schema, path, result)
    return result


def _check_schema(schema, path: str, out: SchemaCheck) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        out.problems.append(f"{path}: схема должна быть объектом или bool, а не {json_type_name(schema)}")
        return

    for key in schema:
        if key in CHECKED_KEYWORDS or key in ANNOTATION_KEYWORDS or key.startswith("x-"):
            continue
        out.unchecked.add(key)

    if "type" in schema:
        t = schema["type"]
        names = t if isinstance(t, list) else [t]
        if not names or not all(isinstance(n, str) for n in names):
            out.problems.append(f"{path}/type: ожидается строка или непустой список строк")
        else:
            for n in names:
                if n not in JSON_TYPES:
                    hint = _TYPE_HINTS.get(n)
                    tail = f" (вероятно, имелось в виду {hint})" if hint else ""
                    out.problems.append(f"{path}/type: неизвестный тип {n!r}{tail}")
            if len(set(names)) != len(names):
                out.problems.append(f"{path}/type: повторяющиеся типы")

    if "enum" in schema and not isinstance(schema["enum"], list):
        out.problems.append(f"{path}/enum: ожидается массив")

    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
            out.problems.append(f"{path}/required: ожидается массив строк")
        elif len(set(req)) != len(req):
            out.problems.append(f"{path}/required: повторяющиеся имена")

    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            out.problems.append(f"{path}/properties: ожидается объект")
        else:
            for name, sub in props.items():
                _check_schema(sub, _pointer(f"{path}/properties", name), out)

    if "additionalProperties" in schema:
        ap = schema["additionalProperties"]
        if isinstance(ap, (dict, bool)):
            _check_schema(ap, f"{path}/additionalProperties", out)
        else:
            out.problems.append(f"{path}/additionalProperties: ожидается bool или схема")

    if "items" in schema:
        items = schema["items"]
        if isinstance(items, list):
            for i, sub in enumerate(items):
                _check_schema(sub, f"{path}/items/{i}", out)
        elif isinstance(items, (dict, bool)):
            _check_schema(items, f"{path}/items", out)
        else:
            out.problems.append(f"{path}/items: ожидается схема или массив схем")

    for key in ("minimum", "maximum"):
        if key in schema and not is_number(schema[key]):
            out.problems.append(f"{path}/{key}: ожидается число")
    for key in ("exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not (is_number(schema[key]) or isinstance(schema[key], bool)):
            out.problems.append(f"{path}/{key}: ожидается число (или bool в форме draft-04)")
    for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        if key in schema and not _is_nonneg_int(schema[key]):
            out.problems.append(f"{path}/{key}: ожидается неотрицательное целое")
    for key in ("uniqueItems", "nullable"):
        if key in schema and not isinstance(schema[key], bool):
            out.problems.append(f"{path}/{key}: ожидается bool")

    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            subs = schema[key]
            if not isinstance(subs, list) or not subs:
                out.problems.append(f"{path}/{key}: ожидается непустой массив схем")
            else:
                for i, sub in enumerate(subs):
                    _check_schema(sub, f"{path}/{key}/{i}", out)


# ---------------------------------------------------------------------------------------------------------
# Проверка значения по схеме

def validate(instance, schema, *, strict_integer: bool = False) -> list[ArgError]:
    """Проверить значение по схеме; вернуть список нарушений (пустой — значение корректно).

    Функция не бросает исключений на некорректных схемах: неправильно оформленные ключевые слова
    пропускаются (их заранее находит ``check_schema``)."""
    errors: list[ArgError] = []
    _validate(instance, schema, "", errors, strict_integer)
    return errors


def _validate(value, schema, path: str, errors: list[ArgError], strict: bool) -> None:
    if schema is True or schema is None:
        return
    if schema is False:
        errors.append(ArgError("false_schema", path, "схема false не допускает никакого значения"))
        return
    if not isinstance(schema, dict):
        return

    _check_type(value, schema, path, errors, strict)

    if isinstance(schema.get("enum"), list) and not any(json_equal(value, e) for e in schema["enum"]):
        errors.append(ArgError("enum", path, f"значение {_short(value)} не входит в enum {_short(schema['enum'])}"))
    if "const" in schema and not json_equal(value, schema["const"]):
        errors.append(ArgError("const", path, f"значение {_short(value)} не равно const {_short(schema['const'])}"))

    if is_number(value):
        _check_bounds(value, schema, path, errors)
    elif isinstance(value, str):
        _check_count(len(value), schema, "minLength", "maxLength", "length", "длина строки", path, errors)
    elif isinstance(value, list):
        _check_array(value, schema, path, errors, strict)
    elif isinstance(value, dict):
        _check_object(value, schema, path, errors, strict)

    _check_combinators(value, schema, path, errors, strict)


def _check_type(value, schema: dict, path: str, errors: list[ArgError], strict: bool) -> None:
    if "type" not in schema:
        return
    t = schema["type"]
    names = t if isinstance(t, list) else [t]
    names = [n for n in names if isinstance(n, str)]
    if not names:
        return
    if value is None and schema.get("nullable") is True:
        return
    if not any(type_matches(value, n, strict) for n in names):
        expected = names[0] if len(names) == 1 else "|".join(names)
        errors.append(ArgError("type", path, f"ожидается {expected}, получено {json_type_name(value)} {_short(value)}"))


def _check_bounds(value, schema: dict, path: str, errors: list[ArgError]) -> None:
    lo, hi = schema.get("minimum"), schema.get("maximum")
    xlo, xhi = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
    # Булева форма draft-04: exclusiveMinimum: true делает minimum строгим.
    if xlo is True and is_number(lo):
        xlo, lo = lo, None
    if xhi is True and is_number(hi):
        xhi, hi = hi, None
    if is_number(lo) and value < lo:
        errors.append(ArgError("bounds", path, f"{value} < minimum {lo}"))
    if is_number(hi) and value > hi:
        errors.append(ArgError("bounds", path, f"{value} > maximum {hi}"))
    if is_number(xlo) and value <= xlo:
        errors.append(ArgError("bounds", path, f"{value} <= exclusiveMinimum {xlo}"))
    if is_number(xhi) and value >= xhi:
        errors.append(ArgError("bounds", path, f"{value} >= exclusiveMaximum {xhi}"))


def _check_count(n: int, schema: dict, lo_key: str, hi_key: str, kind: str, what: str,
                 path: str, errors: list[ArgError]) -> None:
    lo, hi = schema.get(lo_key), schema.get(hi_key)
    if _is_nonneg_int(lo) and n < lo:
        errors.append(ArgError(kind, path, f"{what} {n} < {lo_key} {lo}"))
    if _is_nonneg_int(hi) and n > hi:
        errors.append(ArgError(kind, path, f"{what} {n} > {hi_key} {hi}"))


def _check_array(value: list, schema: dict, path: str, errors: list[ArgError], strict: bool) -> None:
    _check_count(len(value), schema, "minItems", "maxItems", "count", "число элементов", path, errors)
    items = schema.get("items")
    if isinstance(items, list):
        # Кортежная форма: i-й элемент по i-й схеме; хвост (additionalItems) не проверяется.
        for i, (elem, sub) in enumerate(zip(value, items)):
            _validate(elem, sub, _pointer(path, i), errors, strict)
    elif isinstance(items, (dict, bool)):
        for i, elem in enumerate(value):
            _validate(elem, items, _pointer(path, i), errors, strict)
    if schema.get("uniqueItems") is True:
        for i in range(len(value)):
            for j in range(i):
                if json_equal(value[i], value[j]):
                    errors.append(ArgError("unique", path, f"элементы {j} и {i} совпадают (uniqueItems)"))
                    return


def _check_object(value: dict, schema: dict, path: str, errors: list[ArgError], strict: bool) -> None:
    _check_count(len(value), schema, "minProperties", "maxProperties", "count", "число свойств", path, errors)
    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in value:
                errors.append(ArgError("required", _pointer(path, name), f"нет обязательного параметра {name!r}"))

    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    for name, sub in props.items():
        if name in value:
            _validate(value[name], sub, _pointer(path, name), errors, strict)

    additional = schema.get("additionalProperties", True)
    if additional is True or not isinstance(additional, (bool, dict)):
        return
    if "patternProperties" in schema:
        # Без patternProperties нельзя решить, какие свойства «дополнительные»; проверку пропускаем
        # (ключевое слово уже помечено как непроверенное), чтобы не дать ложное срабатывание.
        return
    for name in value:
        if name in props:
            continue
        if additional is False:
            errors.append(ArgError("additional", _pointer(path, name), f"параметр {name!r} не описан в схеме"))
        else:
            _validate(value[name], additional, _pointer(path, name), errors, strict)


def _check_combinators(value, schema: dict, path: str, errors: list[ArgError], strict: bool) -> None:
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for sub in all_of:
            _validate(value, sub, path, errors, strict)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        if not any(not validate(value, sub, strict_integer=strict) for sub in any_of):
            errors.append(ArgError("combinator", path,
                                   f"значение {_short(value)} не подходит ни под одну из {len(any_of)} схем anyOf"))
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        matched = sum(1 for sub in one_of if not validate(value, sub, strict_integer=strict))
        if matched != 1:
            errors.append(ArgError("combinator", path,
                                   f"значение {_short(value)} подходит под {matched} схем oneOf (нужно ровно 1)"))

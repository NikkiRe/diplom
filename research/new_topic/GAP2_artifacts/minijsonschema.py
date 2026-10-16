"""Минимальный валидатор JSON Schema draft-07 (подмножество ключевых слов).

Нужен только для проверки сгенерированных Thing Description по официальной
схеме W3C TD 1.1 (td-schema-v1.1_w3c_2025-03-12.json) без установки пакетов.
Поддержаны ключевые слова, которые реально встречаются в этой схеме:
type, $ref (только локальные #/definitions/...), properties,
additionalProperties, items (схема или кортеж), additionalItems, enum, const,
required, oneOf, anyOf, allOf, not, minItems, maxItems, minimum, maximum,
exclusiveMinimum, pattern, uniqueItems, minProperties, contains, minLength.
format, $comment, description, examples, title игнорируются (как и в
большинстве валидаторов по умолчанию для format).

Корректность подмножества проверяется в test_gap2.py: 5 полных TD из текста
спецификации TD 1.1 проходят, специально испорченные варианты не проходят.
"""
from __future__ import annotations

import json
import re
from typing import Any


def _is_type(value: Any, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer())
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "null":
        return value is None
    raise ValueError(f"unknown type {t}")


def _json_equal(a: Any, b: Any) -> bool:
    # True/1 не равны в JSON Schema
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


class Validator:
    def __init__(self, schema: dict):
        self.root = schema

    def _resolve(self, ref: str) -> dict:
        if not ref.startswith("#/"):
            raise ValueError(f"only local refs supported: {ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def errors(self, inst: Any, schema: Any = None, path: str = "$") -> list[str]:
        if schema is None:
            schema = self.root
        if schema is True:
            return []
        if schema is False:
            return [f"{path}: false schema"]
        errs: list[str] = []
        if "$ref" in schema:
            # в draft-07 соседние с $ref ключевые слова игнорируются
            return self.errors(inst, self._resolve(schema["$ref"]), path)
        t = schema.get("type")
        if t is not None:
            types = t if isinstance(t, list) else [t]
            if not any(_is_type(inst, x) for x in types):
                return [f"{path}: expected type {t}, got {type(inst).__name__}"]
        if "enum" in schema and not any(_json_equal(inst, e) for e in schema["enum"]):
            errs.append(f"{path}: {inst!r} not in enum")
        if "const" in schema and not _json_equal(inst, schema["const"]):
            errs.append(f"{path}: {inst!r} != const {schema['const']!r}")
        if isinstance(inst, dict):
            for r in schema.get("required", []):
                if r not in inst:
                    errs.append(f"{path}: missing required '{r}'")
            if "minProperties" in schema and len(inst) < schema["minProperties"]:
                errs.append(f"{path}: fewer than {schema['minProperties']} properties")
            props = schema.get("properties", {})
            for k, v in inst.items():
                if k in props:
                    errs += self.errors(v, props[k], f"{path}.{k}")
                elif "additionalProperties" in schema:
                    ap = schema["additionalProperties"]
                    if ap is False:
                        errs.append(f"{path}: additional property '{k}' not allowed")
                    elif isinstance(ap, dict):
                        errs += self.errors(v, ap, f"{path}.{k}")
        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                errs.append(f"{path}: fewer than {schema['minItems']} items")
            if "maxItems" in schema and len(inst) > schema["maxItems"]:
                errs.append(f"{path}: more than {schema['maxItems']} items")
            if schema.get("uniqueItems"):
                seen = [json.dumps(x, sort_keys=True) for x in inst]
                if len(seen) != len(set(seen)):
                    errs.append(f"{path}: items not unique")
            items = schema.get("items")
            if isinstance(items, list):
                for i, (x, s) in enumerate(zip(inst, items)):
                    errs += self.errors(x, s, f"{path}[{i}]")
                if len(inst) > len(items) and "additionalItems" in schema:
                    ai = schema["additionalItems"]
                    for i in range(len(items), len(inst)):
                        if ai is False:
                            errs.append(f"{path}[{i}]: additional item not allowed")
                        elif isinstance(ai, dict):
                            errs += self.errors(inst[i], ai, f"{path}[{i}]")
            elif isinstance(items, dict):
                for i, x in enumerate(inst):
                    errs += self.errors(x, items, f"{path}[{i}]")
            if "contains" in schema and not any(
                    not self.errors(x, schema["contains"], path) for x in inst):
                errs.append(f"{path}: no item matches 'contains'")
        if isinstance(inst, str):
            if "pattern" in schema and not re.search(schema["pattern"], inst):
                errs.append(f"{path}: {inst!r} does not match pattern")
            if "minLength" in schema and len(inst) < schema["minLength"]:
                errs.append(f"{path}: shorter than minLength")
        if _is_type(inst, "number"):
            if "minimum" in schema and inst < schema["minimum"]:
                errs.append(f"{path}: {inst} < minimum {schema['minimum']}")
            if "maximum" in schema and inst > schema["maximum"]:
                errs.append(f"{path}: {inst} > maximum {schema['maximum']}")
            if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
                errs.append(f"{path}: {inst} <= exclusiveMinimum")
        if "allOf" in schema:
            for i, s in enumerate(schema["allOf"]):
                errs += self.errors(inst, s, f"{path}<allOf{i}>")
        if "anyOf" in schema:
            if not any(not self.errors(inst, s, path) for s in schema["anyOf"]):
                errs.append(f"{path}: matches none of anyOf")
        if "oneOf" in schema:
            n = sum(1 for s in schema["oneOf"] if not self.errors(inst, s, path))
            if n != 1:
                errs.append(f"{path}: matches {n} of oneOf (need exactly 1)")
        if "not" in schema and not self.errors(inst, schema["not"], path):
            errs.append(f"{path}: matches 'not' schema")
        return errs

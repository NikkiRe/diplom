"""Юнит-тесты мини-валидатора JSON Schema (monitor/argschema.py)."""
import unittest

from monitor.argschema import check_schema, json_equal, type_matches, validate


def kinds(instance, schema, **kw):
    return sorted(e.kind for e in validate(instance, schema, **kw))


class TypeTests(unittest.TestCase):
    def test_bool_is_not_a_number(self):
        for t in ("integer", "number"):
            self.assertFalse(type_matches(True, t))
            self.assertFalse(type_matches(False, t))
        self.assertTrue(type_matches(True, "boolean"))
        self.assertFalse(type_matches(1, "boolean"))

    def test_integer_semantics(self):
        self.assertTrue(type_matches(3, "integer"))
        self.assertTrue(type_matches(10 ** 30, "integer"))
        self.assertTrue(type_matches(3.0, "integer"))           # JSON Schema: нулевая дробная часть
        self.assertFalse(type_matches(3.0, "integer", strict_integer=True))
        self.assertFalse(type_matches(3.5, "integer"))
        self.assertTrue(type_matches(3, "number"))
        self.assertTrue(type_matches(3.5, "number"))

    def test_other_types(self):
        self.assertTrue(type_matches(None, "null"))
        self.assertFalse(type_matches(0, "null"))
        self.assertTrue(type_matches("", "string"))
        self.assertTrue(type_matches([], "array"))
        self.assertFalse(type_matches({}, "array"))
        self.assertTrue(type_matches({}, "object"))
        self.assertFalse(type_matches("x", "unknown-type"))

    def test_type_list_and_nullable(self):
        schema = {"type": ["integer", "null"]}
        self.assertEqual(kinds(None, schema), [])
        self.assertEqual(kinds(4, schema), [])
        self.assertEqual(kinds("4", schema), ["type"])
        self.assertEqual(kinds(None, {"type": "string", "nullable": True}), [])
        self.assertEqual(kinds(None, {"type": "string"}), ["type"])

    def test_strict_integer_flag_propagates(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        self.assertEqual(kinds({"n": 2.0}, schema), [])
        self.assertEqual(kinds({"n": 2.0}, schema, strict_integer=True), ["type"])


class EqualityTests(unittest.TestCase):
    def test_json_equality(self):
        self.assertTrue(json_equal(1, 1.0))
        self.assertFalse(json_equal(1, True))
        self.assertFalse(json_equal(0, False))
        self.assertFalse(json_equal("1", 1))
        self.assertTrue(json_equal({"a": [1, {"b": None}]}, {"a": [1.0, {"b": None}]}))
        self.assertFalse(json_equal({"a": 1}, {"a": 1, "b": 2}))
        self.assertFalse(json_equal([1, 2], [2, 1]))

    def test_enum_uses_json_equality(self):
        self.assertEqual(kinds(True, {"enum": [1, 2]}), ["enum"])
        self.assertEqual(kinds(1, {"enum": [True]}), ["enum"])
        self.assertEqual(kinds(2.0, {"enum": [1, 2]}), [])
        self.assertEqual(kinds([1, 2], {"enum": [[1, 2], [3]]}), [])
        self.assertEqual(kinds("c", {"type": "string", "enum": ["a", "b"]}), ["enum"])

    def test_const(self):
        self.assertEqual(kinds("x", {"const": "x"}), [])
        self.assertEqual(kinds(False, {"const": 0}), ["const"])


class ObjectTests(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }

    def test_required_and_properties(self):
        self.assertEqual(kinds({"a": "x"}, self.SCHEMA), [])
        self.assertEqual(kinds({"b": 1}, self.SCHEMA), ["required"])
        self.assertEqual(kinds({"a": 1, "b": "1"}, self.SCHEMA), ["type", "type"])
        self.assertEqual(kinds({"a": "x", "extra": 1}, self.SCHEMA), [])  # по умолчанию разрешено

    def test_additional_properties(self):
        closed = {**self.SCHEMA, "additionalProperties": False}
        self.assertEqual(kinds({"a": "x", "extra": 1}, closed), ["additional"])
        typed = {**self.SCHEMA, "additionalProperties": {"type": "number"}}
        self.assertEqual(kinds({"a": "x", "extra": 1.5}, typed), [])
        self.assertEqual(kinds({"a": "x", "extra": "no"}, typed), ["type"])
        # additionalProperties без поддержки patternProperties не проверяется (чтобы не было ложных срабатываний)
        pattern = {**closed, "patternProperties": {"^x_": {}}}
        self.assertEqual(kinds({"a": "x", "x_1": 1}, pattern), [])

    def test_error_paths_are_json_pointers(self):
        schema = {"type": "object", "properties": {"a/b": {"type": "object", "properties": {
            "c~d": {"type": "array", "items": {"type": "integer"}}}}}}
        errors = validate({"a/b": {"c~d": [1, "x"]}}, schema)
        self.assertEqual([(e.kind, e.path) for e in errors], [("type", "/a~1b/c~0d/1")])
        errors = validate({}, {"required": ["q"]})
        self.assertEqual(errors[0].path, "/q")

    def test_property_counts(self):
        self.assertEqual(kinds({}, {"minProperties": 1}), ["count"])
        self.assertEqual(kinds({"a": 1, "b": 2}, {"maxProperties": 1}), ["count"])


class ArrayTests(unittest.TestCase):
    def test_items_schema(self):
        schema = {"type": "array", "items": {"type": "number"}}
        self.assertEqual(kinds([1, 2.5], schema), [])
        self.assertEqual(kinds([1, "2", True], schema), ["type", "type"])

    def test_tuple_items(self):
        schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(kinds(["a", 1], schema), [])
        self.assertEqual(kinds([1, "a"], schema), ["type", "type"])
        self.assertEqual(kinds(["a", 1, None], schema), [])  # хвост не ограничен

    def test_counts_and_uniqueness(self):
        self.assertEqual(kinds([], {"minItems": 1}), ["count"])
        self.assertEqual(kinds([1, 2, 3], {"maxItems": 2}), ["count"])
        self.assertEqual(kinds([1, 1.0], {"uniqueItems": True}), ["unique"])
        self.assertEqual(kinds([1, True], {"uniqueItems": True}), [])
        self.assertEqual(kinds([{"a": 1}, {"a": 1}], {"uniqueItems": True}), ["unique"])


class ScalarConstraintTests(unittest.TestCase):
    def test_bounds(self):
        schema = {"minimum": 0, "maximum": 10}
        self.assertEqual(kinds(0, schema), [])
        self.assertEqual(kinds(10, schema), [])
        self.assertEqual(kinds(-1, schema), ["bounds"])
        self.assertEqual(kinds(10.5, schema), ["bounds"])
        self.assertEqual(kinds(0, {"exclusiveMinimum": 0}), ["bounds"])
        self.assertEqual(kinds(5, {"exclusiveMaximum": 5}), ["bounds"])
        # булева форма draft-04
        self.assertEqual(kinds(0, {"minimum": 0, "exclusiveMinimum": True}), ["bounds"])
        self.assertEqual(kinds(1, {"minimum": 0, "exclusiveMinimum": True}), [])
        self.assertEqual(kinds(True, {"maximum": 0}), [])  # bool — не число, границы не применяются

    def test_string_length_counts_code_points(self):
        self.assertEqual(kinds("ёж", {"maxLength": 2}), [])
        self.assertEqual(kinds("😀", {"maxLength": 1}), [])
        self.assertEqual(kinds("", {"minLength": 1}), ["length"])
        self.assertEqual(kinds("abc", {"maxLength": 2}), ["length"])


class CombinatorTests(unittest.TestCase):
    def test_any_one_all(self):
        any_of = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(kinds(1, any_of), [])
        self.assertEqual(kinds(1.5, any_of), ["combinator"])
        one_of = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
        self.assertEqual(kinds(1.5, one_of), [])
        self.assertEqual(kinds(1, one_of), ["combinator"])     # подходит под обе
        all_of = {"allOf": [{"type": "integer"}, {"minimum": 3}]}
        self.assertEqual(kinds(4, all_of), [])
        self.assertEqual(kinds(2, all_of), ["bounds"])

    def test_boolean_schemas(self):
        self.assertEqual(kinds(1, True), [])
        self.assertEqual(kinds(1, False), ["false_schema"])
        self.assertEqual(kinds({"a": 1}, {"properties": {"a": False}}), ["false_schema"])


class SchemaCheckTests(unittest.TestCase):
    def test_valid_schema(self):
        check = check_schema({"type": "object", "description": "d", "properties": {
            "x": {"type": "integer", "default": 1, "x-vendor": True, "format": "int32"}}, "required": ["x"]})
        self.assertEqual(check.problems, [])
        self.assertEqual(check.unchecked, set())

    def test_bfcl_type_names_are_problems_with_hint(self):
        check = check_schema({"type": "dict", "properties": {"f": {"type": "float"}, "t": {"type": "tuple"}}})
        self.assertEqual(len(check.problems), 3)
        self.assertIn("object", check.problems[0])
        self.assertIn("number", check.problems[1])

    def test_malformed_keywords(self):
        bad = {"type": [], "required": "x", "properties": [], "items": 3, "enum": "a", "minimum": "0",
               "minLength": -1, "anyOf": [], "additionalProperties": 1, "uniqueItems": "yes"}
        self.assertEqual(len(check_schema(bad).problems), len(bad))
        self.assertEqual(len(check_schema(5).problems), 1)

    def test_unchecked_keywords_are_reported(self):
        check = check_schema({"type": "object", "properties": {
            "s": {"type": "string", "pattern": "^a"}, "r": {"$ref": "#/$defs/x"}}, "$defs": {"x": {}}})
        self.assertEqual(check.unchecked, {"pattern", "$ref"})

    def test_validate_does_not_raise_on_malformed_schema(self):
        weird = {"type": 5, "required": "a", "properties": [], "items": 7, "enum": "x", "minimum": "1",
                 "anyOf": "no", "additionalProperties": 3}
        for value in (None, 1, "s", [1], {"a": 1}):
            validate(value, weird)


if __name__ == "__main__":
    unittest.main()

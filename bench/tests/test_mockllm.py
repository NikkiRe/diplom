"""mock-llm без HTTP: подсчёт токенов, эмуляция префикс-кэша, оракул, задержка, ключи записи."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.mockllm.cache import PrefixCache, common_prefix_len, _pack  # noqa: E402
from bench.mockllm.latency import LatencyModel  # noqa: E402
from bench.mockllm.oracle import Oracle  # noqa: E402
from bench.mockllm.recording import RecordStore, request_key  # noqa: E402
from bench.mockllm.tokens import (FUNC_END, FUNC_INIT, PROP_INIT, PROP_KEY, REPLY_PRIMING,  # noqa: E402
                                  TOKENS_PER_MESSAGE, HeuristicTokenizer, account_request, count_message,
                                  count_tools, load_tokenizer, split_segments)


def tool(name, props=None, desc="Описание.", required=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


GENERIC = [
    tool("write_property", {"device": {"type": "string", "description": "id"},
                            "property": {"type": "string", "description": "prop"},
                            "value": {"description": "value"}}),
    tool("read_property", {"device": {"type": "string", "description": "id"},
                           "property": {"type": "string", "description": "prop"}}),
    tool("sim__answer", {"value": {"type": "number", "description": "v"}, "unit": {"type": "string", "description": "u"}}),
    tool("sim__refuse", {"reason_code": {"type": "string", "description": "код"}}),
]
EXPLICIT = [tool("set_relay1_on", {"value": {"type": "boolean", "description": "вкл"}}), tool("read_temp1_value"),
            tool("notify_user", {"message": {"type": "string", "description": "текст"},
                                 "situation_id": {"type": "string", "description": "id"}})]


def user(text):
    return {"role": "user", "content": text}


def body(messages, tools=GENERIC):
    return {"model": "gpt-4o-mini", "messages": messages, "tools": tools}


def args_of(msg, i=0):
    return json.loads(msg["tool_calls"][i]["function"]["arguments"])


class TokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tok = load_tokenizer()

    def test_tokenizer_loaded(self):
        # В .venv установлен tiktoken, словарь o200k — локальный (research/.../.cache/tk).
        self.assertIn(self.tok.name, ("o200k_base", "heuristic"))
        if self.tok.name == "o200k_base":
            self.assertEqual(self.tok.count("hello world"), 2)

    def test_message_count_cookbook(self):
        m = {"role": "user", "content": "включи свет"}
        self.assertEqual(count_message(m, self.tok), TOKENS_PER_MESSAGE + self.tok.count("user") + self.tok.count("включи свет"))
        named = {"role": "user", "name": "bob", "content": "hi"}
        self.assertEqual(count_message(named, self.tok),
                         TOKENS_PER_MESSAGE + self.tok.count("user") + self.tok.count("bob") + 1 + self.tok.count("hi"))

    def test_tools_count_cookbook(self):
        t = [tool("get_weather", {"city": {"type": "string", "description": "City name."},
                                  "unit": {"type": "string", "description": "Unit", "enum": ["c", "f"]}},
                  desc="Get weather.")]
        tk = self.tok
        want = (FUNC_INIT + tk.count("get_weather:Get weather") + PROP_INIT
                + PROP_KEY + tk.count("city:string:City name")
                + PROP_KEY - 3 + (3 + tk.count("c")) + (3 + tk.count("f")) + tk.count("unit:string:Unit")
                + FUNC_END)
        self.assertEqual(count_tools(t, tk), want)
        self.assertEqual(count_tools([], tk), 0)
        self.assertGreater(count_tools(t, tk, method="json"), 0)

    def test_segments_and_priming(self):
        msgs = [{"role": "system", "content": "S"}, user("a"), {"role": "assistant", "content": "b"}, user("c"),
                {"role": "assistant", "tool_calls": [{"id": "x", "type": "function",
                                                       "function": {"name": "f", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "x", "content": "{}"}]
        head, hist, turn = split_segments(msgs)
        self.assertEqual((len(head), len(hist), len(turn)), (1, 2, 3))
        acc = account_request({"messages": msgs, "tools": GENERIC}, self.tok)
        self.assertEqual(acc.segments["priming"], REPLY_PRIMING)
        self.assertEqual(acc.prompt_tokens, sum(acc.segments.values()))
        cal = account_request({"messages": msgs, "tools": GENERIC}, self.tok, calib=(10.0, 2.0))
        self.assertEqual(cal.prompt_tokens, round(10 + 2 * acc.raw_estimate))

    def test_sequence_order_system_tools_messages(self):
        a = account_request(body([{"role": "system", "content": "X"}, user("1")]), self.tok)
        b = account_request(body([{"role": "system", "content": "X"}, user("2")]), self.tok)
        c = account_request(body([{"role": "system", "content": "Y"}, user("1")]), self.tok)
        n_ab = common_prefix_len(_pack(a.sequence), _pack(b.sequence))
        n_ac = common_prefix_len(_pack(a.sequence), _pack(c.sequence))
        self.assertGreater(n_ab, n_ac)      # смена system ломает префикс раньше, чем смена хвоста
        self.assertGreater(n_ab, count_tools(GENERIC, self.tok) // 2)

    def test_heuristic_is_deterministic(self):
        h = HeuristicTokenizer()
        self.assertEqual(h.encode("включи свет"), h.encode("включи свет"))
        self.assertEqual(h.count("abcdefgh"), 2)


class CacheTest(unittest.TestCase):
    def test_common_prefix_len(self):
        a, b = _pack([1, 2, 3, 4, 5]), _pack([1, 2, 3, 9])
        self.assertEqual(common_prefix_len(a, b), 3)
        self.assertEqual(common_prefix_len(a, a), 5)
        self.assertEqual(common_prefix_len(a, _pack([7])), 0)
        self.assertEqual(common_prefix_len(a, _pack([])), 0)

    def test_threshold_granularity_ttl(self):
        c = PrefixCache(ttl_s=300, min_tokens=1024, granularity=128)
        base = list(range(2000))
        self.assertEqual(c.lookup_and_store(base, now=0).cached_tokens, 0)          # первый запрос — промах
        r = c.lookup_and_store(base[:1500] + [-1] * 100, now=100)
        self.assertEqual((r.cached_tokens, r.common_prefix), (1408, 1500))           # 1500 → вниз до 128·11
        self.assertEqual(c.lookup_and_store(base[:1000] + [-2] * 500, now=110).cached_tokens, 0)   # < 1024
        self.assertEqual(c.lookup_and_store(base, now=350).cached_tokens, 1920)      # 250 с после попадания в t=100
        self.assertEqual(c.lookup_and_store(base, now=600).cached_tokens, 1920)      # попадание в t=350 продлило жизнь
        self.assertEqual(c.lookup_and_store(base, now=1000).cached_tokens, 0)        # пауза 400 с > TTL
        self.assertEqual(c.lookup_and_store(base, now=1201, prompt_tokens=1300).cached_tokens, 1280)  # ≤ оценки

    def test_disabled_and_short_prompts(self):
        self.assertEqual(PrefixCache(ttl_s=0).lookup_and_store(list(range(5000)), 0).cached_tokens, 0)
        c = PrefixCache(ttl_s=300)
        c.lookup_and_store(list(range(500)), 0)
        self.assertEqual(c.lookup_and_store(list(range(500)), 1).cached_tokens, 0)   # < 1024 не кэшируется
        c.reset()
        self.assertEqual(c.lookup_and_store(list(range(2000)), 2).entries, 1)

    def test_extension_absorbs_prefix_entry(self):
        c = PrefixCache(ttl_s=300)
        seq = list(range(1100))
        c.lookup_and_store(seq, 0)
        r = c.lookup_and_store(seq + list(range(5000, 5300)), 1)
        self.assertEqual(r.cached_tokens, 1024)
        self.assertEqual(r.entries, 1)


class OracleTest(unittest.TestCase):
    def setUp(self):
        self.o = Oracle()

    def test_user_command_then_final(self):
        self.o.register({"input_id": "u1", "text": "Включи свет", "kind": "user_cmd", "expect": {
            "type": "action", "calls": [{"name": "write_property", "args": {"device": "relay1", "property": "on",
                                                                            "value": True}}]}})
        d = self.o.respond(body([{"role": "system", "content": "s"}, user("включи   свет пожалуйста")]))
        self.assertEqual((d.kind, d.finish_reason, d.input_ids), ("user_start", "tool_calls", ["u1"]))
        self.assertEqual(d.message["tool_calls"][0]["function"]["name"], "write_property")
        self.assertEqual(args_of(d.message), {"device": "relay1", "property": "on", "value": True})
        tc = d.message["tool_calls"][0]
        d2 = self.o.respond(body([user("включи свет"), d.message,
                                  {"role": "tool", "tool_call_id": tc["id"], "content": '{"ok":true}'}]))
        self.assertEqual((d2.kind, d2.finish_reason, d2.message["content"]), ("final", "stop", "Готово."))
        self.assertEqual(self.o.state()["counts"], {"done": 1})
        # повтор того же текста после завершения — уже не ход u1
        self.assertEqual(self.o.respond(body([user("включи свет")])).kind, "noop")

    def test_question_answer_with_namespaced_tool(self):
        self.o.register({"input_id": "q1", "text": "когда открывали дверь?",
                         "expect": {"type": "answer", "answer": {"value": 1290.0, "unit": "t"}}})
        d = self.o.respond(body([user("Когда открывали дверь?")]))
        self.assertEqual(d.message["tool_calls"][0]["function"]["name"], "sim__answer")
        self.assertEqual(args_of(d.message), {"value": 1290.0, "unit": "t"})
        d2 = self.o.respond(body([user("x"), d.message, {"role": "tool", "tool_call_id": d.message["tool_calls"][0]["id"],
                                                          "content": "{}"}]))
        self.assertEqual(d2.message["content"], "1290.0 t")

    def test_refuse_maps_reason_to_schema(self):
        self.o.register({"input_id": "u2", "text": "открой сейф", "expect": {"type": "refuse", "reason": "no_device"}})
        d = self.o.respond(body([user("открой сейф")]))
        self.assertEqual(d.message["tool_calls"][0]["function"]["name"], "sim__refuse")
        self.assertEqual(args_of(d.message), {"reason_code": "no_device"})

    def test_generic_to_explicit_mapping(self):
        self.o.register({"input_id": "u3", "text": "включи реле", "expect": {"type": "action", "calls": [
            {"name": "write_property", "args": {"device": "relay1", "property": "on", "value": True}},
            {"name": "read_property", "args": {"device": "temp1", "property": "value"}}]}})
        d = self.o.respond(body([user("включи реле")], tools=EXPLICIT))
        names = [c["function"]["name"] for c in d.message["tool_calls"]]
        self.assertEqual(names, ["set_relay1_on", "read_temp1_value"])
        self.assertEqual(args_of(d.message, 0), {"value": True})
        self.assertEqual(args_of(d.message, 1), {})
        self.assertEqual(d.unresolved, [])

    def test_unresolved_tool_is_reported(self):
        self.o.register({"input_id": "u4", "text": "свари кофе", "expect": {"calls": [{"name": "brew", "args": {}}]}})
        d = self.o.respond(body([user("свари кофе")]))
        self.assertEqual(d.unresolved, ["brew"])

    def test_sequential_calls(self):
        o = Oracle(parallel_calls=False)
        o.register({"input_id": "u5", "text": "два действия", "expect": {"calls": [
            {"name": "write_property", "args": {"device": "a", "property": "on", "value": 1}},
            {"name": "write_property", "args": {"device": "b", "property": "on", "value": 1}}]}})
        d1 = o.respond(body([user("два действия")]))
        self.assertEqual(len(d1.message["tool_calls"]), 1)
        msgs = [user("два действия"), d1.message, {"role": "tool", "tool_call_id": d1.message["tool_calls"][0]["id"],
                                                    "content": "{}"}]
        d2 = o.respond(body(msgs))
        self.assertEqual((d2.kind, args_of(d2.message)["device"]), ("continue", "b"))
        msgs += [d2.message, {"role": "tool", "tool_call_id": d2.message["tool_calls"][0]["id"], "content": "{}"}]
        self.assertEqual(o.respond(body(msgs)).kind, "final")

    def test_denied_result_ends_turn(self):
        self.o.register({"input_id": "u6", "text": "включи обогрев", "expect": {"calls": [
            {"name": "write_property", "args": {"device": "heater", "property": "on", "value": True}}]}})
        d = self.o.respond(body([user("включи обогрев")]))
        d2 = self.o.respond(body([user("включи обогрев"), d.message,
                                  {"role": "tool", "tool_call_id": d.message["tool_calls"][0]["id"],
                                   "content": '{"denied": true, "reason": "interlock"}'}]))
        self.assertEqual(d2.kind, "denied_final")
        self.assertIn("interlock", d2.message["content"])

    def test_heartbeat_noop_and_situation_notify(self):
        self.assertEqual(self.o.respond(body([user("[heartbeat]")])).message, {"role": "assistant", "content": ""})
        self.o.register({"input_id": "s1", "expect": {"type": "notify_or_action"},
                         "situation": {"id": "s1", "name": "door_open_at_night", "deadline_s": 300}})
        # каталог без notify — уведомление текстом с меткой ситуации, ход сразу завершён
        d = self.o.respond(body([user("[heartbeat]")]))
        self.assertEqual((d.kind, d.finish_reason), ("situation", "stop"))
        self.assertIn("[sit:s1]", d.message["content"])
        self.assertEqual(self.o.respond(body([user("[heartbeat]")])).kind, "noop")   # ситуация уже отработана

    def test_situation_notify_tool_and_final(self):
        self.o.register({"input_id": "s2", "kind": "situation", "expect": {"type": "notify"},
                         "situation": {"name": "leak"}})
        self.o.register({"input_id": "s3", "kind": "situation", "expect": {"type": "notify"},
                         "situation": {"name": "overheat"}})
        d = self.o.respond(body([user("[событие] leak1.wet = 1")], tools=EXPLICIT))
        self.assertEqual(d.kind, "situation")
        self.assertEqual(sorted(d.input_ids), ["s2", "s3"])
        calls = d.message["tool_calls"]
        self.assertEqual([c["function"]["name"] for c in calls], ["notify_user", "notify_user"])
        self.assertEqual(args_of(d.message, 0)["situation_id"], "s2")
        self.assertIn("leak", args_of(d.message, 0)["message"])
        tail = [d.message] + [{"role": "tool", "tool_call_id": c["id"], "content": "{}"} for c in calls]
        d2 = self.o.respond(body([user("[событие]")] + tail, tools=EXPLICIT))
        self.assertEqual((d2.kind, d2.message["content"]), ("final", ""))

    def test_situation_with_action(self):
        self.o.register({"input_id": "s4", "kind": "situation", "expect": {"type": "notify_or_action", "calls": [
            {"name": "write_property", "args": {"device": "fan1", "property": "on", "value": True}}]}})
        d = self.o.respond(body([user("[событие] temp1 = 41")]))
        self.assertEqual(d.message["tool_calls"][0]["function"]["name"], "write_property")

    def test_expired_situation_is_ignored(self):
        self.o.register({"input_id": "s5", "kind": "situation", "expect": {"type": "notify"}})
        self.assertTrue(self.o.expire("s5"))
        self.assertEqual(self.o.respond(body([user("[heartbeat]")])).kind, "noop")
        self.assertFalse(self.o.expire("nope"))

    def test_header_expectation(self):
        d = self.o.respond(body([user("что угодно")]), header_expect={
            "input_id": "h1", "expect": {"type": "action", "calls": [
                {"name": "write_property", "args": {"device": "x", "property": "on", "value": 0}}]}})
        self.assertEqual((d.kind, d.input_ids), ("user_start", ["h1"]))

    def test_textless_expectation_is_fifo(self):
        self.o.register({"input_id": "a", "expect": {"type": "answer", "answer": {"value": 1}}})
        self.o.register({"input_id": "b", "expect": {"type": "answer", "answer": {"value": 2}}})
        self.assertEqual(self.o.respond(body([user("?")])).input_ids, ["a"])

    def test_unknown_continuation(self):
        msgs = [user("x"), {"role": "tool", "tool_call_id": "foreign", "content": "{}"}]
        d = self.o.respond(body(msgs))
        self.assertEqual((d.kind, d.message["content"]), ("continue_unknown", "Готово."))
        d = self.o.respond(body(msgs), wake_hint="heartbeat")       # в фоне — молчание, не «Готово.»
        self.assertEqual(d.message["content"], "")

    def test_bad_registration(self):
        with self.assertRaises(ValueError):
            self.o.register({"expect": {}})
        with self.assertRaises(ValueError):
            self.o.register({"input_id": "x", "expect": []})


def run_turn(o, text, tools=GENERIC, result='{"ok":true}'):
    """Провести ход до финала, как агент: вызовы → результаты → снова LLM. Возвращает список решений."""
    msgs = [user(text)]
    decisions = [o.respond(body(msgs, tools))]
    while decisions[-1].finish_reason == "tool_calls" and len(decisions) < 20:
        msgs.append(decisions[-1].message)
        msgs += [{"role": "tool", "tool_call_id": c["id"], "content": result}
                 for c in decisions[-1].message["tool_calls"]]
        decisions.append(o.respond(body(msgs, tools)))
    return decisions


class KAndDaygenFormatTest(unittest.TestCase):
    CMD = {"type": "action", "calls": [{"name": "write_property",
                                        "args": {"device": "relay1", "property": "on", "value": True}}],
           "calls_explicit": [{"name": "set_relay1_on", "args": {"value": True}}]}

    def test_k_sets_number_of_llm_calls(self):
        for k, want in ((1, 2), (2, 2), (3, 3), (5, 5), (None, 2)):
            o = Oracle()
            o.register({"input_id": "u", "text": "включи", "k": k, "expect": self.CMD})
            ds = run_turn(o, "включи")
            self.assertEqual(len(ds), want, k)
            self.assertEqual(ds[-1].kind, "final")
            names = [c["function"]["name"] for d in ds[:-1] for c in d.message["tool_calls"]]
            self.assertEqual(names[-1], "write_property")                      # действие — последним шагом
            self.assertTrue(all(n == "read_property" for n in names[:-1]))     # перед ним — чтения состояния
            if len(names) > 1:
                self.assertEqual(args_of(ds[0].message), {"device": "relay1", "property": "on"})

    def test_k_padding_explicit_catalog_and_fallbacks(self):
        o = Oracle()
        o.register({"input_id": "u", "text": "включи", "k": 3, "expect": self.CMD})
        ds = run_turn(o, "включи", tools=[tool("set_relay1_on", {"value": {"type": "boolean"}}),
                                          tool("read_relay1_on")])
        self.assertEqual([d.message["tool_calls"][0]["function"]["name"] for d in ds[:-1]],
                         ["read_relay1_on", "set_relay1_on"])
        o = Oracle()   # читать нечем — заглушки отбрасываются, вызовов меньше k
        o.register({"input_id": "u", "text": "включи", "k": 5, "expect": self.CMD})
        self.assertEqual(len(run_turn(o, "включи", tools=[tool("set_relay1_on")])), 2)
        o = Oracle(honor_k=False)
        o.register({"input_id": "u", "text": "включи", "k": 5, "expect": self.CMD})
        self.assertEqual(len(run_turn(o, "включи")), 2)
        o = Oracle()   # вопрос без устройства — заглушка list_devices
        o.register({"input_id": "q", "text": "сколько", "k": 3, "expect": {"type": "answer", "calls": [
            {"name": "answer", "args": {"value": 2, "unit": "count"}}]}})
        ds = run_turn(o, "сколько", tools=GENERIC + [tool("list_devices")])
        self.assertEqual([d.message["tool_calls"][0]["function"]["name"] for d in ds[:-1]],
                         ["list_devices", "sim__answer"])
        self.assertEqual(o.state()["expectations"][0]["k"], 3)

    def test_calls_explicit_preferred_for_explicit_catalog(self):
        o = Oracle()
        o.register({"input_id": "u", "text": "свари кофе", "expect": {
            "type": "action", "calls": [{"name": "invoke_action",
                                         "args": {"device": "coffee_kitchen", "action": "brew", "args": {}}}],
            "calls_explicit": [{"name": "invoke_coffee_kitchen_brew", "args": {}}]}})
        d = o.respond(body([user("свари кофе")], tools=[tool("sim__invoke_coffee_kitchen_brew")]))
        self.assertEqual(d.message["tool_calls"][0]["function"]["name"], "sim__invoke_coffee_kitchen_brew")
        self.assertEqual(d.unresolved, [])

    def test_daygen_question_and_situation_formats(self):
        o = Oracle()
        o.register({"input_id": "q", "kind": "question", "text": "когда?", "expect": {
            "type": "answer", "calls": [{"name": "answer", "args": {"value": 61000.5, "unit": "t"}}],
            "calls_explicit": [{"name": "answer", "args": {"value": 61000.5, "unit": "t"}}]}})
        d = o.respond(body([user("когда?")]))
        self.assertEqual(args_of(d.message), {"value": 61000.5, "unit": "t"})
        o.register({"input_id": "s1", "kind": "situation", "situation": {"name": "leak"}, "expect": {
            "type": "notify_or_action",
            "actions": [{"name": "write_property", "args": {"device": "valve_water", "property": "open", "value": False}}],
            "actions_explicit": [{"name": "set_valve_water_open", "args": {"value": False}}]}})
        tools = [tool("set_valve_water_open", {"value": {"type": "boolean"}}),
                 tool("notify", {"text": {"type": "string"}, "situation_id": {"type": "string"}})]
        d = o.respond(body([user("[событие] leak_bathroom.wet = 1")], tools=tools))
        self.assertEqual([c["function"]["name"] for c in d.message["tool_calls"]], ["set_valve_water_open", "notify"])
        self.assertEqual(args_of(d.message, 0), {"value": False})
        self.assertEqual(args_of(d.message, 1)["situation_id"], "s1")

    def test_agent_hints(self):
        o = Oracle()
        o.register({"input_id": "u7", "text": "совсем другой текст", "expect": self.CMD})
        # подсказка id входа сильнее текста реплики
        self.assertEqual(o.respond(body([user("включи")]), input_hint="x, u7").input_ids, ["u7"])
        o.register({"input_id": "u8", "text": "включи", "expect": self.CMD})
        # пробуждение не пользователем — текст не сопоставляется, даже если совпал
        self.assertEqual(o.respond(body([user("включи")]), wake_hint="heartbeat").kind, "noop")
        self.assertEqual(o.respond(body([user("включи")]), wake_hint="user").input_ids, ["u8"])


class ReviewFixesOracleTest(unittest.TestCase):
    """Ревью: сводка не трогает ожидания; подсказка id сильнее текста; добор до k без повторных list_devices."""
    CMD = KAndDaygenFormatTest.CMD
    SUMMARY_SYS = ("Сожми историю работы агента умного помещения в краткую сводку фактов: что просил пользователь, "
                   "какие действия выполнены, какие правила и договорённости приняты. Не более 10 строк, без вступлений.")

    def summary_body(self, text):
        return {"model": "m", "messages": [{"role": "system", "content": self.SUMMARY_SYS},
                                           user("Прежняя сводка:\n(нет)\n\nНовые ходы:\n" + text)]}

    def test_summary_never_consumes_expectation(self):
        o = Oracle(tokenizer=load_tokenizer())
        o.register({"input_id": "u5", "text": "включи свет", "expect": self.CMD})
        transcript = "Вход: включи свет\nВызов: write_property {\"device\":\"relay1\"}\nАгент: Готово.\n"
        for hint in ("summary", None):        # по заголовку purpose и по виду запроса (без инструментов)
            d = o.respond(self.summary_body(transcript), input_hint="u5", wake_hint="user", purpose_hint=hint)
            self.assertEqual((d.kind, d.finish_reason), ("summary", "stop"))
            self.assertNotIn("tool_calls", d.message)
            self.assertEqual(o.state()["counts"], {"pending": 1})       # ожидание u5 не тронуто
        self.assertLessEqual(abs(o.tok.count(d.message["content"]) - 400), 1)
        self.assertIn("включи свет", d.message["content"])
        # настоящий ход пользователя после сводки получает свои tool_calls
        d = o.respond(body([user("включи свет")]), input_hint="u5", wake_hint="user", purpose_hint="turn")
        self.assertEqual((d.kind, d.input_ids), ("user_start", ["u5"]))

    def test_summary_length_parameter(self):
        o = Oracle(tokenizer=load_tokenizer(), summary_tokens=64)
        d = o.respond(self.summary_body("Вход: x\n" * 200), purpose_hint="summary")
        self.assertLessEqual(abs(o.tok.count(d.message["content"]) - 64), 1)
        self.assertEqual(Oracle(summary_tokens=0).respond(self.summary_body("Вход: x"),
                                                          purpose_hint="summary").message["content"], "")

    def test_turn_with_tools_is_not_summary(self):
        o = Oracle()
        o.register({"input_id": "u1", "text": "сожми историю", "expect": self.CMD})
        d = o.respond(body([{"role": "system", "content": "сводка"}, user("сожми историю")]))
        self.assertEqual(d.kind, "user_start")

    def test_known_hint_disables_text_fallback(self):
        o = Oracle()
        o.register({"input_id": "u1", "text": "включи", "expect": self.CMD})
        o.register({"input_id": "u2", "text": "включи", "expect": self.CMD})
        ds = run_turn(o, "включи")                                  # без подсказки: последняя по тексту — u2
        self.assertEqual(ds[0].input_ids, ["u2"])
        # подсказка на уже выполненный u2: текст не сопоставляется (u1 не перехватывается)
        self.assertEqual(o.respond(body([user("включи")]), input_hint="u2", wake_hint="user").kind, "noop")
        # неизвестный id (агент сгенерировал свой) — запасное сопоставление по тексту
        self.assertEqual(o.respond(body([user("включи")]), input_hint="x9", wake_hint="user").input_ids, ["u1"])

    def test_targeted_padding_invoke_action_uses_goal_and_lists_once(self):
        cmd = {"type": "action", "calls": [{"name": "invoke_action",
                                            "args": {"action": "start", "args": {}, "device": "vacuum"}}],
               "goal": [{"device": "vacuum", "op": "eq", "property": "cleaning", "value": True}]}
        tools = GENERIC + [tool("invoke_action", {"device": {"type": "string"}, "action": {"type": "string"},
                                                   "args": {"type": "object"}}),
                           tool("list_devices"), tool("recent_events", {"device": {"type": "string"},
                                                                         "property": {"type": "string"},
                                                                         "k": {"type": "integer"}})]
        o = Oracle()
        o.register({"input_id": "u1", "text": "включи робот пылесос", "k": 5, "expect": cmd})
        ds = run_turn(o, "включи робот пылесос", tools=tools)
        names = [d.message["tool_calls"][0]["function"]["name"] for d in ds[:-1]]
        self.assertEqual(names, ["list_devices", "recent_events", "read_property", "invoke_action"])
        self.assertEqual(args_of(ds[2].message), {"device": "vacuum", "property": "cleaning"})
        self.assertEqual(len(ds), 5)
        o = Oracle(pad_profile="legacy")       # прежний профиль: три list_devices подряд
        o.register({"input_id": "u1", "text": "включи робот пылесос", "k": 5, "expect": cmd})
        names = [d.message["tool_calls"][0]["function"]["name"] for d in run_turn(o, "включи робот пылесос",
                                                                                   tools=tools)[:-1]]
        self.assertEqual(names, ["list_devices"] * 3 + ["invoke_action"])

    def test_targeted_padding_questions(self):
        tools = GENERIC + [tool("list_devices"), tool("recent_events", {"device": {"type": "string"}})]
        o = Oracle()   # вопрос о прошлом: обзор один раз, дальше журнал событий
        o.register({"input_id": "q1", "text": "когда?", "k": 5, "expect": {
            "type": "answer", "calls": [{"name": "answer", "args": {"value": 1, "unit": "t"}}]}})
        names = [d.message["tool_calls"][0]["function"]["name"] for d in run_turn(o, "когда?", tools=tools)[:-1]]
        self.assertEqual(names, ["list_devices", "recent_events", "recent_events", "sim__answer"])
        o = Oracle()   # вопрос о состоянии: чтение answer.ref
        o.register({"input_id": "q2", "text": "влажность?", "k": 3, "expect": {
            "type": "answer", "calls": [{"name": "answer", "args": {"value": 44, "unit": "%RH"}}],
            "answer": {"value": 44, "unit": "%RH", "ref": {"device": "thermo", "property": "humidity"}}}})
        ds = run_turn(o, "влажность?", tools=tools)
        self.assertEqual(args_of(ds[0].message), {"device": "thermo", "property": "humidity"})

    def test_reset_restarts_call_ids(self):
        o = Oracle()
        o.register({"input_id": "u", "text": "включи", "expect": self.CMD})
        first = run_turn(o, "включи")[0].message["tool_calls"][0]["id"]
        o.reset()
        o.register({"input_id": "u", "text": "включи", "expect": self.CMD})
        self.assertEqual(run_turn(o, "включи")[0].message["tool_calls"][0]["id"], first)


class CacheRulesTest(unittest.TestCase):
    def test_min_tokens_by_provider_prompt(self):
        c = PrefixCache()
        c.lookup_and_store(list(range(1100)), 0.0, 1000)
        self.assertEqual(c.lookup_and_store(list(range(1100)), 1.0, 1000).cached_tokens, 0)   # промпт < 1024
        c = PrefixCache()
        c.lookup_and_store(list(range(1300)), 0.0, 1100)
        # общий префикс 1300 → 1280, но промпт 1100 → вниз до 1024: допустимо (≥ 1024)
        self.assertEqual(c.lookup_and_store(list(range(1300)), 1.0, 1100).cached_tokens, 1024)

    def test_miss_does_not_extend_ttl(self):
        c = PrefixCache(ttl_s=10)
        c.lookup_and_store(list(range(2000)), 0.0, 2000)
        # промпт провайдера < 1024: ни попадания, ни записи — жизнь записи t=0 не продлевается
        self.assertEqual(c.lookup_and_store(list(range(2000)), 8.0, 900).cached_tokens, 0)
        self.assertEqual(c.lookup_and_store(list(range(2000)), 12.0, 2000).cached_tokens, 0)   # TTL 10 с истёк
        c = PrefixCache(ttl_s=10)
        c.lookup_and_store(list(range(2000)), 0.0, 2000)
        self.assertEqual(c.lookup_and_store(list(range(2000)), 8.0, 2000).cached_tokens, 1920)  # попадание продлевает
        self.assertEqual(c.lookup_and_store(list(range(2000)), 12.0, 2000).cached_tokens, 1920)

    def test_tool_key_order_breaks_prefix(self):
        tok = load_tokenizer()
        sys_msg = {"role": "system", "content": "Ты агент. " * 400}
        t1 = [{"type": "function", "function": {"name": "a", "parameters": {"type": "object", "properties": {}}}}]
        t2 = [{"function": {"parameters": {"properties": {}, "type": "object"}, "name": "a"}, "type": "function"}]
        s1 = account_request({"messages": [sys_msg, user("x")], "tools": t1}, tok).sequence
        s2 = account_request({"messages": [sys_msg, user("x")], "tools": t2}, tok).sequence
        self.assertNotEqual(s1, s2)       # канонизация больше не маскирует нестабильный порядок ключей


class LatencyTest(unittest.TestCase):
    def test_specs(self):
        self.assertEqual(LatencyModel("0").sample_ms(), 0.0)
        self.assertEqual(LatencyModel("250").sample_ms(), 250.0)
        self.assertEqual(LatencyModel("fixed:12.5").sample_ms(), 12.5)
        u = [LatencyModel("uniform:100:300", seed=1).sample_ms() for _ in range(3)]
        self.assertTrue(all(100 <= x <= 300 for x in u))
        a = LatencyModel("lognormal:800:0.5", seed=7)
        b = LatencyModel("lognormal:800:0.5", seed=7)
        self.assertEqual([a.sample_ms() for _ in range(5)], [b.sample_ms() for _ in range(5)])
        self.assertGreaterEqual(LatencyModel("normal:0:100", seed=3).sample_ms(), 0.0)
        for bad in ("fast", "uniform:1", "fixed:-1", "lognormal:a:b"):
            with self.assertRaises(ValueError):
                LatencyModel(bad)


class RecordingTest(unittest.TestCase):
    def test_key_ignores_volatile_fields(self):
        b1 = {"model": "m", "messages": [user("x")], "user": "u1", "metadata": {"a": 1}}
        b2 = {"messages": [user("x")], "model": "m", "user": "u2"}
        self.assertEqual(request_key(b1), request_key(b2))
        self.assertNotEqual(request_key(b1), request_key({**b1, "messages": [user("y")]}))

    def test_store_sequences_repeats(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rec.jsonl"
            s = RecordStore(path)
            b = {"model": "m", "messages": [user("x")]}
            s.add(b, 200, {"n": 1}, 0.0, 1.0)
            s.add(b, 200, {"n": 2}, 0.0, 1.0)
            s2 = RecordStore(path)      # перечитать с диска
            self.assertEqual(len(s2), 2)
            self.assertEqual([s2.lookup(b)["response"]["n"] for _ in range(3)], [1, 2, 2])
            s2.rewind()
            self.assertEqual(s2.lookup(b)["response"]["n"], 1)
            self.assertIsNone(s2.lookup({"model": "m", "messages": [user("z")]}))


if __name__ == "__main__":
    unittest.main()

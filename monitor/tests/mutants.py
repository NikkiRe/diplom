"""Мутационные операторы над корректными прогонами (трасса edgectx + журнал шлюза).

Каждый мутант портит одно свойство и объявляет классы нарушения, которые монитор обязан выдать: обнаружение
засчитывается, если среди найденных есть хотя бы один из ``expected`` (первый — основной). Мутант возвращает
None, если к прогону неприменим (например, в нём нет исполненных команд вентилятору). Вставленные записи
получают время соседней записи и перенумерованный seq, чтобы не порождать попутных нарушений порядка.
"""
from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from typing import Callable

from monitor.tests.tracegen import Scenario


@dataclass(frozen=True)
class Mutant:
    name: str
    expected: tuple
    target: str            # trace | gate
    description: str
    apply: Callable[[Scenario, random.Random], Scenario | None]


MUTANTS: list[Mutant] = []


def mutant(target: str, *expected: str, doc: str):
    def register(fn):
        MUTANTS.append(Mutant(fn.__name__.removeprefix("m_"), tuple(expected), target, doc, fn))
        return fn
    return register


# ---------------------------------------------------------------------------------------------------------
# Помощники

def clone(sc: Scenario) -> Scenario:
    return Scenario(sc.name, copy.deepcopy(sc.trace), copy.deepcopy(sc.gate), copy.deepcopy(sc.tools),
                    copy.deepcopy(sc.declared), sc.linked, sc.concurrent)


def where(records, pred) -> list[int]:
    return [i for i, r in enumerate(records) if isinstance(r, dict) and pred(r)]


def of_type(records, typ, pred=lambda r: True) -> list[int]:
    return where(records, lambda r: r.get("type") == typ and pred(r))


def pick(rng, items):
    return rng.choice(items) if items else None


def renumber(trace) -> None:
    """Переписать seq подряд (после вставки или удаления записей)."""
    seq = 1
    for r in trace:
        if isinstance(r, dict) and "seq" in r:
            r["seq"] = seq
            seq += 1


def index_of(records, typ, key, value) -> int | None:
    found = of_type(records, typ, lambda r: r.get(key) == value)
    return found[0] if found else None


LINKS = ("wake_id", "turn_id")


def link_of(r):
    """Идентификатор хода записи (wake_id у edgectx или turn_id)."""
    return next((r[f] for f in LINKS if f in r), None)


def run_start(sc) -> dict:
    return sc.trace[0]


def responses_before(trace, pos) -> tuple[int, int]:
    """Завершённые ответы LLM (и их prompt_tokens) до позиции pos — та же нижняя оценка, что у монитора."""
    n = tok = 0
    for r in trace[:pos]:
        if r.get("type") == "llm_response":
            n += 1
            tok += r["usage"]["prompt_tokens"]
    return n, tok


def turn_starts(sc):
    """(позиция первого llm_request хода, wake) для последовательных прогонов: ход идёт сразу за своим wake."""
    out = []
    for i, r in enumerate(sc.trace):
        if r.get("type") != "wake" or r.get("suppressed"):
            continue
        for j in range(i + 1, len(sc.trace)):
            t = sc.trace[j].get("type")
            if t == "wake":
                break
            if t == "llm_request" and "purpose" not in sc.trace[j]:
                out.append((j, r))
                break
    return out


def turn_lengths(sc) -> list[int]:
    """Число запросов хода: по turn_id в связанном режиме, по отрезкам между wake — в последовательном."""
    if sc.linked:
        counts = {}
        for r in sc.trace:
            if r.get("type") == "llm_request" and link_of(r) is not None and "purpose" not in r:
                counts[link_of(r)] = counts.get(link_of(r), 0) + 1
        return list(counts.values())
    lengths, cur = [], 0
    for r in sc.trace:
        if r.get("type") == "wake" and not r.get("suppressed"):
            if cur:
                lengths.append(cur)
            cur = 0
        elif r.get("type") == "llm_request" and "purpose" not in r:
            cur += 1
    if cur:
        lengths.append(cur)
    return lengths


def executed_decisions(sc, pred=lambda r: True) -> list[int]:
    return of_type(sc.gate, "gate_decision", lambda r: r.get("executed") and r["tool"] not in ("read", "capture")
                   and pred(r))


def gate_insert(sc, rec) -> None:
    """Вставить запись в журнал шлюза по времени (после всех записей с тем же или меньшим ts_clock)."""
    pos = len(sc.gate)
    for i, r in enumerate(sc.gate):
        if r["ts_clock"] > rec["ts_clock"]:
            pos = i
            break
    sc.gate.insert(pos, rec)


# ---------------------------------------------------------------------------------------------------------
# Рамка, порядок, поля

@mutant("trace", "frame.no_run_start", doc="удалён run_start")
def m_drop_run_start(sc, rng):
    del sc.trace[0]
    renumber(sc.trace)
    return sc


@mutant("trace", "frame.no_run_end", doc="удалён run_end")
def m_drop_run_end(sc, rng):
    del sc.trace[-1]
    return sc


@mutant("trace", "frame.after_run_end", doc="запись после run_end")
def m_record_after_run_end(sc, rng):
    last = sc.trace[-1]
    sc.trace.append(dict(last, type="input", kind="sensor", id="late-1", seq=last["seq"] + 1))
    return sc


@mutant("trace", "frame.duplicate_run_start", doc="второй run_start в середине")
def m_duplicate_run_start(sc, rng):
    pos = rng.randrange(2, len(sc.trace) - 1)
    rec = dict(sc.trace[0], ts_clock=sc.trace[pos - 1]["ts_clock"], ts_wall=sc.trace[pos - 1]["ts_wall"])
    sc.trace.insert(pos, rec)
    renumber(sc.trace)
    return sc


@mutant("trace", "frame.run_id", doc="у одной записи чужой run_id")
def m_wrong_run_id(sc, rng):
    sc.trace[rng.randrange(1, len(sc.trace))]["run_id"] = "other-run"
    return sc


@mutant("trace", "order.seq", doc="пропуск в seq (потерянная строка)")
def m_seq_gap(sc, rng):
    k = rng.randrange(2, len(sc.trace))
    for r in sc.trace[k:]:
        r["seq"] += 1
    return sc


@mutant("trace", "time.clock_backwards", doc="ts_clock записи меньше, чем у предыдущей")
def m_clock_backwards(sc, rng):
    idx = [i for i in range(1, len(sc.trace)) if sc.trace[i - 1]["ts_clock"] >= 10]
    i = pick(rng, idx)
    if i is None:
        return None
    sc.trace[i]["ts_clock"] = sc.trace[i - 1]["ts_clock"] - 10
    return sc


@mutant("trace", "event.unknown_type", doc="запись неизвестного типа")
def m_unknown_type(sc, rng):
    pos = rng.randrange(1, len(sc.trace) - 1)
    sc.trace.insert(pos, dict(sc.trace[pos - 1], type="telemetry"))
    renumber(sc.trace)
    return sc


@mutant("trace", "event.field", doc="у записи нет ts_wall")
def m_missing_ts_wall(sc, rng):
    del sc.trace[rng.randrange(1, len(sc.trace))]["ts_wall"]
    return sc


@mutant("trace", "event.field", doc="n_tools строкой")
def m_bad_field_type(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_request"))
    if i is None:
        return None
    sc.trace[i]["n_tools"] = str(sc.trace[i]["n_tools"])
    return sc


@mutant("trace", "event.field", doc="отрицательная latency_ms у user_reply")
def m_negative_latency(sc, rng):
    i = pick(rng, of_type(sc.trace, "user_reply"))
    if i is None:
        return None
    sc.trace[i]["latency_ms"] = -5
    return sc


@mutant("trace", "event.field", doc="в segments нет ключа history")
def m_segments_missing_key(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_request"))
    if i is None:
        return None
    del sc.trace[i]["segments"]["history"]
    return sc


@mutant("trace", "parse", doc="обрезанная строка JSON")
def m_parse_error(sc, rng):
    i = rng.randrange(1, len(sc.trace) - 1)
    sc.trace[i] = json.dumps(sc.trace[i], ensure_ascii=False)[:25]
    return sc


@mutant("trace", "parse", doc="повтор ключа в строке")
def m_duplicate_key_line(sc, rng):
    i = rng.randrange(1, len(sc.trace) - 1)
    text = json.dumps(sc.trace[i], ensure_ascii=False)
    sc.trace[i] = text[:-1] + ', "run_id": "x"}'
    return sc


@mutant("trace", "parse", doc="NaN вместо числа")
def m_nan_value(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_response"))
    if i is None:
        return None
    sc.trace[i] = json.dumps(sc.trace[i], ensure_ascii=False).replace('"latency_ms": ', '"latency_ms": NaN, "x": ', 1)
    return sc


# ---------------------------------------------------------------------------------------------------------
# Пары запрос/ответ и вызов/результат

@mutant("trace", "llm.missing_response", doc="удалён llm_response")
def m_drop_llm_response(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_response", lambda r: r["n_tool_calls"] == 0))
    if i is None:
        return None
    del sc.trace[i]
    renumber(sc.trace)
    return sc


@mutant("trace", "llm.duplicate_response", doc="llm_response записан дважды")
def m_duplicate_llm_response(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_response", lambda r: r["n_tool_calls"] == 0))
    if i is None:
        return None
    sc.trace.insert(i + 1, copy.deepcopy(sc.trace[i]))
    renumber(sc.trace)
    return sc


@mutant("trace", "llm.response_before_request", doc="llm_response перед своим llm_request")
def m_response_before_request(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_response"))
    if i is None:
        return None
    resp = sc.trace.pop(i)
    j = index_of(sc.trace, "llm_request", "req_id", resp["req_id"])
    resp["ts_clock"], resp["ts_wall"] = sc.trace[j]["ts_clock"], sc.trace[j]["ts_wall"]
    sc.trace.insert(j, resp)
    renumber(sc.trace)
    return sc


@mutant("trace", "llm.orphan_response", doc="req_id ответа не совпадает ни с одним запросом")
def m_orphan_response(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_response"))
    if i is None:
        return None
    sc.trace[i]["req_id"] = "r-ghost"
    return sc


@mutant("trace", "llm.duplicate_req_id", doc="запрос получил req_id более раннего запроса")
def m_duplicate_req_id(sc, rng):
    reqs = of_type(sc.trace, "llm_request")
    if len(reqs) < 2:
        return None
    k = rng.randrange(1, len(reqs))
    old = sc.trace[reqs[k]]["req_id"]
    new = sc.trace[reqs[rng.randrange(0, k)]]["req_id"]
    for r in sc.trace:
        if r.get("req_id") == old and r.get("type") in ("llm_request", "llm_response"):
            r["req_id"] = new
    return sc


@mutant("trace", "llm.finish_mismatch", doc="finish_reason=tool_calls у ответа без вызовов")
def m_finish_mismatch(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_response", lambda r: r["n_tool_calls"] == 0))
    if i is None:
        return None
    sc.trace[i]["finish_reason"] = "tool_calls"
    return sc


@mutant("trace", "usage.inconsistent", doc="cached_tokens > prompt_tokens")
def m_cached_gt_prompt(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_response"))
    if i is None:
        return None
    u = sc.trace[i]["usage"]
    u["cached_tokens"] = u["prompt_tokens"] + 1
    return sc


@mutant("trace", "tool.missing_result", doc="удалён tool_result")
def m_drop_tool_result(sc, rng):
    i = pick(rng, of_type(sc.trace, "tool_result"))
    if i is None:
        return None
    del sc.trace[i]
    renumber(sc.trace)
    return sc


@mutant("trace", "tool.orphan_result", doc="call_id результата не совпадает ни с одним вызовом")
def m_orphan_tool_result(sc, rng):
    i = pick(rng, of_type(sc.trace, "tool_result"))
    if i is None:
        return None
    sc.trace[i]["call_id"] = "call-ghost"
    return sc


@mutant("trace", "tool.duplicate_result", doc="tool_result записан дважды")
def m_duplicate_tool_result(sc, rng):
    i = pick(rng, of_type(sc.trace, "tool_result"))
    if i is None:
        return None
    sc.trace.insert(i + 1, copy.deepcopy(sc.trace[i]))
    renumber(sc.trace)
    return sc


@mutant("trace", "tool.result_before_call", doc="tool_result перед своим tool_call")
def m_result_before_call(sc, rng):
    i = pick(rng, of_type(sc.trace, "tool_result"))
    if i is None:
        return None
    res = sc.trace.pop(i)
    j = index_of(sc.trace, "tool_call", "call_id", res["call_id"])
    sc.trace.insert(j, res)
    renumber(sc.trace)
    return sc


@mutant("trace", "tool.duplicate_call_id", doc="вызов получил call_id более раннего вызова")
def m_duplicate_call_id(sc, rng):
    calls = of_type(sc.trace, "tool_call")
    if len(calls) < 2:
        return None
    k = rng.randrange(1, len(calls))
    old = sc.trace[calls[k]]["call_id"]
    new = sc.trace[calls[rng.randrange(0, k)]]["call_id"]
    for r in sc.trace:
        if r.get("call_id") == old and r.get("type") in ("tool_call", "tool_result"):
            r["call_id"] = new
    return sc


@mutant("trace", "tool.unsourced", doc="tool_call в начале прогона: ни ответа LLM, ни правила")
def m_unsourced_call(sc, rng):
    base = sc.trace[0]
    call = dict(base, type="tool_call", call_id="call-x1", server="local", name="notify_user",
                args={"text": "привет"})
    for k in ("config_hash", "policy", "budget", "sched"):
        call.pop(k, None)
    res = {k: call[k] for k in ("type", "run_id", "seq", "ts_wall", "ts_clock")}
    res.update(type="tool_result", call_id="call-x1", ok=True, denied=False, ms=1.0)
    sc.trace[1:1] = [call, res]
    renumber(sc.trace)
    return sc


@mutant("trace", "tool.count_mismatch", doc="ответ объявил на один вызов больше, чем записано")
def m_count_mismatch(sc, rng):
    has_rules = bool(of_type(sc.trace, "rule_fired"))
    if not sc.linked and has_rules:
        return None  # позиционный режим: вызов правила может «занять» лишний объявленный вызов (см. README)
    i = pick(rng, of_type(sc.trace, "llm_response", lambda r: r["n_tool_calls"] > 0))
    if i is None:
        return None
    sc.trace[i]["n_tool_calls"] += 1
    return sc


@mutant("trace", "tool.result_late", doc="результат вызова записан после следующего запроса хода (связанный режим)")
def m_result_late(sc, rng):
    if not sc.linked:
        return None
    cands = []
    for i in of_type(sc.trace, "tool_result"):
        cid = sc.trace[i]["call_id"]
        ci = index_of(sc.trace, "tool_call", "call_id", cid)
        call = sc.trace[ci]
        if "req_id" in call:
            turn = link_of(sc.trace[index_of(sc.trace, "llm_request", "req_id", call["req_id"])])
        elif call.get("by") == "llm":
            turn = link_of(call)
        else:
            continue
        nxt = [j for j in of_type(sc.trace, "llm_request", lambda r: link_of(r) == turn and "purpose" not in r)
               if j > i]
        if nxt:
            cands.append((i, nxt[0]))
    if not cands:
        return None
    i, j = rng.choice(cands)
    res = sc.trace[i]
    sc.trace.insert(j + 1, dict(res, ts_clock=sc.trace[j]["ts_clock"], ts_wall=sc.trace[j]["ts_wall"]))
    del sc.trace[i]
    renumber(sc.trace)
    return sc


# ---------------------------------------------------------------------------------------------------------
# Пробуждения, ссылки, пользователь

@mutant("trace", "wake.missing", doc="удалён wake хода (связанный режим)")
def m_drop_wake_linked(sc, rng):
    if not sc.linked:
        return None
    used = {link_of(r) for r in sc.trace if r.get("type") == "llm_request"}
    i = pick(rng, of_type(sc.trace, "wake", lambda r: link_of(r) is not None and link_of(r) in used))
    if i is None:
        return None
    del sc.trace[i]
    renumber(sc.trace)
    return sc


@mutant("trace", "wake.missing", doc="удалены все wake (позиционный режим)")
def m_drop_all_wakes(sc, rng):
    sc.trace = [r for r in sc.trace if r.get("type") != "wake"]
    renumber(sc.trace)
    return sc


@mutant("trace", "wake.missing", doc="запрос ссылается на несуществующий turn_id")
def m_unknown_turn(sc, rng):
    i = pick(rng, of_type(sc.trace, "llm_request", lambda r: link_of(r) is not None))
    if i is None:
        return None
    field = next(f for f in LINKS if f in sc.trace[i])
    sc.trace[i][field] = "t-ghost"
    return sc


@mutant("trace", "wake.suppressed_flag", "wake.missing", doc="ход начат, хотя его wake отклонён (suppressed=true)")
def m_wake_suppressed(sc, rng):
    i = pick(rng, of_type(sc.trace, "wake", lambda r: not r.get("suppressed")))
    if i is None:
        return None
    sc.trace[i]["suppressed"] = True
    return sc


@mutant("trace", "wake.reason_mismatch", doc="wake входа пользователя с причиной heartbeat")
def m_wake_reason_mismatch(sc, rng):
    i = pick(rng, of_type(sc.trace, "wake", lambda r: r["reason"] == "user" and (r.get("input_id") or r.get("input_ids"))))
    if i is None:
        return None
    sc.trace[i]["reason"] = "heartbeat"
    sc.trace[i].pop("priority", None)
    return sc


@mutant("trace", "ref.dangling", doc="user_reply ссылается на несуществующий вход")
def m_dangling_input(sc, rng):
    i = pick(rng, of_type(sc.trace, "user_reply"))
    if i is None:
        return None
    sc.trace[i]["input_id"] = "u-ghost"
    return sc


@mutant("trace", "ref.dangling", doc="suppress ссылается на вход, записанный позже")
def m_forward_reference(sc, rng):
    i = pick(rng, of_type(sc.trace, "suppress"))
    later = [j for j in of_type(sc.trace, "input") if i is not None and j > i]
    if i is None or not later:
        return None
    sc.trace[i]["input_id"] = sc.trace[rng.choice(later)]["id"]
    return sc


@mutant("trace", "user.unanswered", doc="удалён user_reply")
def m_drop_user_reply(sc, rng):
    i = pick(rng, of_type(sc.trace, "user_reply"))
    if i is None:
        return None
    del sc.trace[i]
    renumber(sc.trace)
    return sc


@mutant("trace", "user.duplicate_reply", doc="user_reply записан дважды")
def m_duplicate_user_reply(sc, rng):
    i = pick(rng, of_type(sc.trace, "user_reply"))
    if i is None:
        return None
    sc.trace.insert(i + 1, copy.deepcopy(sc.trace[i]))
    renumber(sc.trace)
    return sc


@mutant("trace", "user.reply_to_non_user", doc="user_reply на вход датчика")
def m_reply_to_sensor(sc, rng):
    i = pick(rng, of_type(sc.trace, "user_reply"))
    sensors = [j for j in of_type(sc.trace, "input", lambda r: r["kind"] == "sensor") if i is not None and j < i]
    if i is None or not sensors:
        return None
    sc.trace[i]["input_id"] = sc.trace[rng.choice(sensors)]["id"]
    return sc


@mutant("trace", "notify.unsourced", doc="notify сразу после run_start")
def m_notify_unsourced(sc, rng):
    base = {k: sc.trace[0][k] for k in ("run_id", "seq", "ts_wall", "ts_clock")}
    sc.trace.insert(1, dict(base, type="notify", text="тревога", situation_id=None))
    renumber(sc.trace)
    return sc


# ---------------------------------------------------------------------------------------------------------
# Бюджеты

@mutant("trace", "budget.max_steps", "wake.missing", doc="max_steps в run_start меньше длины самого длинного хода")
def m_exceed_max_steps(sc, rng):
    if sc.concurrent and not sc.linked:
        return None  # позиционный режим при двух ходах в полёте шаги не проверяет (см. README)
    lengths = turn_lengths(sc)
    if not lengths or max(lengths) < 2:
        return None
    run_start(sc)["policy"]["max_steps"] = max(lengths) - 1
    return sc


@mutant("trace", "llm.inflight", doc="ответ перенесён после запроса следующего хода (два запроса в полёте)")
def m_inflight(sc, rng):
    if sc.concurrent:
        return None
    resps = of_type(sc.trace, "llm_response", lambda r: r["n_tool_calls"] == 0)
    cands = []
    for i in resps:
        nxt = [j for j in of_type(sc.trace, "llm_request") if j > i]
        if nxt:
            cands.append((i, nxt[0]))
    if not cands:
        return None
    i, j = rng.choice(cands)
    resp = sc.trace[i]
    sc.trace.insert(j + 1, dict(resp, ts_clock=sc.trace[j]["ts_clock"], ts_wall=sc.trace[j]["ts_wall"]))
    del sc.trace[i]
    renumber(sc.trace)
    return sc


@mutant("trace", "llm.inflight", doc="два хода в полёте при объявленном max_in_flight=1")
def m_concurrent_declared_sequential(sc, rng):
    if not sc.concurrent:
        return None
    outstanding = peak = 0
    for r in sc.trace:
        if r.get("type") == "llm_request":
            outstanding += 1
            peak = max(peak, outstanding)
        elif r.get("type") in ("llm_response", "error") and "req_id" in r:
            outstanding -= 1
    if peak < 2:
        return None
    run_start(sc)["sched"]["max_in_flight"] = 1
    return sc


def _non_user_turn(sc, rng, reason_pred):
    if sc.concurrent:
        return None
    cands = [(j, w) for j, w in turn_starts(sc) if reason_pred(w) and responses_before(sc.trace, j)[0] >= 1]
    return rng.choice(cands) if cands else None


@mutant("trace", "budget.daily_calls", doc="лимит вызовов исчерпан до начала фонового хода")
def m_exhaust_daily_calls(sc, rng):
    got = _non_user_turn(sc, rng, lambda w: w["reason"] != "user")
    if got is None:
        return None
    n, _ = responses_before(sc.trace, got[0])
    run_start(sc)["budget"].update(daily_calls=n, daily_input_tokens=10 ** 9)
    return sc


@mutant("trace", "budget.daily_input_tokens", doc="лимит входных токенов исчерпан до начала фонового хода")
def m_exhaust_daily_tokens(sc, rng):
    got = _non_user_turn(sc, rng, lambda w: w["reason"] != "user")
    if got is None:
        return None
    _, tok = responses_before(sc.trace, got[0])
    run_start(sc)["budget"].update(daily_calls=10 ** 6, daily_input_tokens=tok)
    return sc


@mutant("trace", "budget.degraded", doc="ход heartbeat в деградированном режиме")
def m_degraded_heartbeat(sc, rng):
    got = _non_user_turn(sc, rng, lambda w: w["reason"] == "heartbeat" and w.get("priority", "background") == "background")
    if got is None:
        return None
    n, _ = responses_before(sc.trace, got[0])
    run_start(sc)["budget"].update(daily_calls=2 * n, daily_input_tokens=10 ** 9, degrade_at=0.5)
    return sc


# ---------------------------------------------------------------------------------------------------------
# Аргументы инструментов

def _calls(sc, server=None, name=None):
    return of_type(sc.trace, "tool_call", lambda r: (server is None or r["server"] == server)
                   and (name is None or r["name"] == name) and isinstance(r["args"], dict))


@mutant("trace", "args.unknown_tool", doc="вызов несуществующего инструмента")
def m_unknown_tool(sc, rng):
    i = pick(rng, _calls(sc, "local"))
    if i is None:
        return None
    sc.trace[i]["name"] = "self_destruct"
    return sc


@mutant("trace", "args.unknown_tool", doc="вызов на несуществующий сервер")
def m_unknown_server(sc, rng):
    i = pick(rng, _calls(sc, "local"))
    if i is None:
        return None
    sc.trace[i]["server"] = "toaster-mcp"
    return sc


@mutant("trace", "args.type", doc="text строки — число")
def m_arg_type(sc, rng):
    i = pick(rng, _calls(sc, "local", "notify_user"))
    if i is None:
        return None
    sc.trace[i]["args"]["text"] = 42
    return sc


@mutant("trace", "args.required", doc="нет обязательного аргумента")
def m_arg_required(sc, rng):
    i = pick(rng, _calls(sc, "local", "notify_user"))
    if i is None:
        return None
    del sc.trace[i]["args"]["text"]
    return sc


@mutant("trace", "args.enum", doc="устройство вне enum")
def m_arg_enum(sc, rng):
    i = pick(rng, _calls(sc, "hw", "invoke_action"))
    if i is None:
        return None
    sc.trace[i]["args"]["device"] = "toaster"
    return sc


@mutant("trace", "args.additional", doc="лишний аргумент при additionalProperties: false")
def m_arg_additional(sc, rng):
    i = pick(rng, _calls(sc))
    if i is None:
        return None
    sc.trace[i]["args"]["zzz"] = 1
    return sc


@mutant("trace", "args.bounds", doc="k вне [1, 50]")
def m_arg_bounds(sc, rng):
    i = pick(rng, _calls(sc, "local", "recent_events"))
    if i is None:
        return None
    sc.trace[i]["args"]["k"] = 1000
    return sc


@mutant("trace", "args.length", doc="text длиннее maxLength")
def m_arg_length(sc, rng):
    i = pick(rng, _calls(sc, "local", "notify_user"))
    if i is None:
        return None
    sc.trace[i]["args"]["text"] = "x" * 501
    return sc


@mutant("trace", "args.invalid_json", doc="args — обрезанная JSON-строка")
def m_args_invalid_json(sc, rng):
    i = pick(rng, _calls(sc))
    if i is None:
        return None
    sc.trace[i]["args"] = json.dumps(sc.trace[i]["args"])[:-1]
    return sc


@mutant("trace", "args.duplicate_key", doc="в строке args повторяется ключ")
def m_args_duplicate_key(sc, rng):
    i = pick(rng, _calls(sc, "local", "notify_user"))
    if i is None:
        return None
    sc.trace[i]["args"] = '{"text": "a", "text": "b"}'
    return sc


@mutant("trace", "args.not_object", doc="args — JSON-массив")
def m_args_not_object(sc, rng):
    i = pick(rng, _calls(sc))
    if i is None:
        return None
    sc.trace[i]["args"] = "[1, 2]"
    return sc


# ---------------------------------------------------------------------------------------------------------
# Журнал шлюза: actuator_safety

NUMERIC = {"set_angle": ("deg", 90), "set_speed": ("pct", 100), "set_color": ("r", 255)}


@mutant("gate", "actuator_safety.out_of_range", doc="исполнено значение вне диапазона")
def m_exec_out_of_range(sc, rng):
    i = pick(rng, executed_decisions(sc, lambda r: r["tool"] in NUMERIC))
    if i is None:
        return None
    d = sc.gate[i]
    arg, hi = NUMERIC[d["tool"]]
    d["final_args"] = dict(d["final_args"], **{arg: hi + 30})
    return sc


@mutant("gate", "actuator_safety.executed_after_deny", doc="отказанная команда отмечена исполненной")
def m_exec_after_reject(sc, rng):
    i = pick(rng, of_type(sc.gate, "gate_decision", lambda r: r["verdict"] == "reject"))
    if i is None:
        return None
    sc.gate[i]["executed"] = True
    return sc


@mutant("gate", "actuator_safety.stale_state", doc="в снимке решения устаревшее состояние")
def m_stale_state_snapshot(sc, rng):
    i = pick(rng, executed_decisions(sc, lambda r: bool(r.get("state"))))
    if i is None:
        return None
    d = sc.gate[i]
    for v in d["state"].values():
        v["ts"] = d["ts_clock"] - 100
    return sc


@mutant("gate", "actuator_safety.stale_state", doc="нет ни снимка, ни показаний нужного состояния")
def m_missing_state(sc, rng):
    i = pick(rng, executed_decisions(sc, lambda r: bool(r.get("state"))))
    if i is None:
        return None
    keys = set(sc.gate[i]["state"])
    sc.gate[i]["state"] = {}
    sc.gate = [r for r in sc.gate if not (r["type"] == "state" and r["key"] in keys)]
    return sc


@mutant("gate", "actuator_safety.stale_command", doc="исполнено решение LLM старше TTL")
def m_stale_command(sc, rng):
    i = pick(rng, executed_decisions(sc))
    if i is None:
        return None
    sc.gate[i]["issued_at"] = sc.gate[i]["ts_clock"] - 100
    return sc


@mutant("gate", "actuator_safety.interlock", doc="нагреватель включён при открытом окне (по снимку шлюза)")
def m_interlock_snapshot(sc, rng):
    i = pick(rng, executed_decisions(sc, lambda r: r["tool"] == "set_power" and r["final_args"].get("on")
                                     and r.get("state")))
    if i is None:
        return None
    d = sc.gate[i]
    if d["device"] == "heater":
        d["state"]["window_open"]["value"] = True
    else:
        d["state"]["water_level_pct"]["value"] = 1.0
    return sc


@mutant("gate", "actuator_safety.interlock", doc="независимые показания: окно открыто перед командой")
def m_interlock_state_log(sc, rng):
    i = pick(rng, executed_decisions(sc, lambda r: r["device"] == "heater" and r["final_args"].get("on")))
    if i is None:
        return None
    d = sc.gate[i]
    sc.gate.insert(i, {"type": "state", "ts_clock": d["ts_clock"], "ts_wall": d["ts_wall"], "key": "window_open",
                       "value": True, "ts": d["ts_clock"]})
    return sc


@mutant("gate", "actuator_safety.interlock", doc="clamp не применён: скорость > 30 % рядом с человеком")
def m_clamp_ignored(sc, rng):
    i = pick(rng, of_type(sc.gate, "gate_decision", lambda r: r["verdict"] == "clamp"))
    if i is None:
        return None
    sc.gate[i]["final_args"] = dict(sc.gate[i]["args"])
    return sc


@mutant("gate", "actuator_safety.rate_limit", doc="объявленный min_interval нагревателя строже, чем соблюдал шлюз")
def m_rate_limit(sc, rng):
    times = [sc.gate[i]["ts_clock"] for i in executed_decisions(sc, lambda r: r["device"] == "heater")]
    if len(times) < 2:
        return None
    gap = max(b - a for a, b in zip(times, times[1:]))
    sc.gate[0]["config"]["devices"]["heater"]["min_interval_s"] = gap + 1
    return sc


@mutant("gate", "actuator_safety.unconfirmed", doc="открытие двери без подтверждения исполнено")
def m_unconfirmed(sc, rng):
    i = pick(rng, of_type(sc.gate, "gate_decision", lambda r: r["verdict"] == "confirm"))
    if i is None:
        return None
    sc.gate[i].update(verdict="allow", reason=None, executed=True)
    return sc


@mutant("gate", "actuator_safety.duplicate_exec", doc="команда с тем же cmd_id исполнена второй раз")
def m_duplicate_exec(sc, rng):
    i = pick(rng, executed_decisions(sc))
    if i is None:
        return None
    sc.gate.insert(i + 1, dict(copy.deepcopy(sc.gate[i]), call_id=None))
    return sc


@mutant("gate", "actuator_safety.unknown_target", doc="исполнена команда устройству вне реестра")
def m_unknown_target(sc, rng):
    i = pick(rng, executed_decisions(sc))
    if i is None:
        return None
    sc.gate[i]["device"] = "toaster"
    return sc


@mutant("gate", "actuator_safety.no_lease", doc="команда вентилятору без аренды")
def m_no_lease(sc, rng):
    # Аренда, выданная в тот же момент предыдущей командой вентилятору, покрывает и эту команду (тот же срок):
    # на такой команде мутант эквивалентен корректному прогону — её не берём.
    granted_at, cands = set(), []
    for i in executed_decisions(sc, lambda r: r["device"] == "fan"):
        t = sc.gate[i]["ts_clock"]
        if t not in granted_at:
            cands.append(i)
        if sc.gate[i].get("lease_until") is not None:
            granted_at.add(t)
    i = pick(rng, cands)
    if i is None:
        return None
    sc.gate[i]["lease_until"] = None
    return sc


@mutant("gate", "actuator_safety.lease_too_long", doc="аренда дольше lease_s")
def m_lease_too_long(sc, rng):
    i = pick(rng, executed_decisions(sc, lambda r: r["device"] == "fan"))
    if i is None:
        return None
    sc.gate[i]["lease_until"] = sc.gate[i]["ts_clock"] + 100
    return sc


@mutant("gate", "actuator_safety.watchdog_missed", doc="после истечения аренды нет safe_state")
def m_watchdog_missed(sc, rng):
    def quiet_after(j):
        """После safe_state журнал продолжается дольше допуска тика (1 с), а новая команда или аренда того же
        привода приходит позже допуска — иначе привод законно взят в новую аренду и нарушения нет."""
        dev, t = sc.gate[j]["device"], sc.gate[j]["ts_clock"]
        later = [r["ts_clock"] for r in sc.gate[j + 1:] if r.get("device") == dev and r["type"] in ("gate_decision", "lease")]
        return sc.gate[-1]["ts_clock"] > t + 2 and (not later or later[0] > t + 1.0)
    i = pick(rng, [j for j in of_type(sc.gate, "safe_state") if quiet_after(j)])
    if i is None:
        return None
    del sc.gate[i]
    return sc


@mutant("gate", "actuator_safety.renew_without_lease", doc="продление принято после перехода в безопасное состояние")
def m_renew_without_lease(sc, rng):
    i = pick(rng, of_type(sc.gate, "safe_state"))
    if i is None:
        return None
    s = sc.gate[i]
    sc.gate.insert(i + 1, {"type": "lease", "ts_clock": s["ts_clock"], "ts_wall": s["ts_wall"], "device": s["device"],
                           "op": "renew", "ok": True, "expires_at": s["ts_clock"] + 2})
    return sc


@mutant("gate", "actuator_safety.unchecked_call", doc="решения шлюза об исполненной команде нет")
def m_unchecked_call(sc, rng):
    i = pick(rng, executed_decisions(sc))
    if i is None:
        return None
    del sc.gate[i]
    return sc


@mutant("trace", "actuator_safety.result_mismatch", doc="трасса считает исполненную шлюзом команду отказом")
def m_result_mismatch(sc, rng):
    ids = {sc.gate[i]["call_id"] for i in executed_decisions(sc)}
    i = pick(rng, of_type(sc.trace, "tool_result", lambda r: r["call_id"] in ids))
    if i is None:
        return None
    sc.trace[i].update(ok=False, denied=True)
    return sc


@mutant("gate", "gate.config_missing", doc="в журнале нет gate_config")
def m_config_missing(sc, rng):
    sc.gate = [r for r in sc.gate if r["type"] != "gate_config"]
    return sc


@mutant("gate", "gate.config_mismatch", doc="шлюз работал с иной политикой, чем объявлена (--gate-config)")
def m_config_mismatch(sc, rng):
    sc.declared = copy.deepcopy(sc.gate[0]["config"])
    sc.gate[0]["config"]["devices"]["valve"]["max_per_min"] = 100
    return sc


@mutant("gate", "gate.config_invalid", doc="в политике неизвестный оператор условия")
def m_config_invalid(sc, rng):
    sc.gate[0]["config"]["interlocks"][0]["deny"][0]["if"] = {"arg": "on", "approx": True}
    return sc


@mutant("gate", "time.clock_backwards", doc="время решения шлюза идёт назад относительно предыдущего решения")
def m_gate_clock_backwards(sc, rng):
    # Решения шлюз выносит под одной блокировкой, их время монотонно; обратный ход между разными писателями
    # журнала (сэмплер, таймеры) — лишь предупреждение gate.clock_backwards.
    decs = of_type(sc.gate, "gate_decision")
    idx = [(a, b) for a, b in zip(decs, decs[1:]) if sc.gate[a]["ts_clock"] > 100]
    got = pick(rng, idx)
    if got is None:
        return None
    a, b = got
    sc.gate[b]["ts_clock"] = sc.gate[a]["ts_clock"] - 50
    return sc


@mutant("gate", "event.field", doc="у решения шлюза нет issued_at")
def m_gate_missing_field(sc, rng):
    i = pick(rng, of_type(sc.gate, "gate_decision"))
    if i is None:
        return None
    del sc.gate[i]["issued_at"]
    return sc


# ---------------------------------------------------------------------------------------------------------
# Ключевая метрика (вызовы и токены LLM): потерянные входы, лишние пробуждения, правдоподобие usage

def _wake_inputs(r) -> list:
    ids = list(r.get("input_ids") or [])
    if r.get("input_id") is not None:
        ids.append(r["input_id"])
    return ids


def _real_inputs(trace) -> set:
    return {r["id"] for r in trace if isinstance(r, dict) and r.get("type") == "input"}


@mutant("trace", "input.no_outcome", doc="удалены записи suppress (входы остались без исхода)")
def m_drop_suppress(sc, rng):
    idx = [i for i in of_type(sc.trace, "suppress")
           if len(of_type(sc.trace, "suppress", lambda r, iid=sc.trace[i]["input_id"]: r.get("input_id") == iid)) == 1
           and not any(sc.trace[i]["input_id"] in _wake_inputs(w) for w in sc.trace
                       if isinstance(w, dict) and w.get("type") == "wake" and not w.get("suppressed"))]
    if not idx:
        return None
    drop = set(rng.sample(idx, min(len(idx), rng.randint(1, 50))))
    sc.trace = [r for i, r in enumerate(sc.trace) if i not in drop]
    renumber(sc.trace)
    return sc


@mutant("trace", "wake.duplicate_input", doc="один вход будит LLM второй раз (+1 ход)")
def m_double_wake_input(sc, rng):
    real = _real_inputs(sc.trace)
    wakes = [i for i in of_type(sc.trace, "wake", lambda r: not r.get("suppressed")) if
             any(x in real for x in _wake_inputs(sc.trace[i]))]
    pairs = [(a, b) for a, b in zip(wakes, wakes[1:])]
    got = pick(rng, pairs)
    if got is None:
        return None
    a, b = got
    iid = next(x for x in _wake_inputs(sc.trace[a]) if x in real)
    w = sc.trace[b]
    ids = _wake_inputs(w)
    w.pop("input_id", None)
    w["input_ids"] = ids + [iid]
    return sc


@mutant("trace", "usage.implausible", doc="usage.prompt_tokens = 0 во всех ответах")
def m_zero_usage(sc, rng):
    idx = of_type(sc.trace, "llm_response", lambda r: not r.get("error") and r.get("finish_reason") != "error")
    if not idx:
        return None
    for i in idx:
        u = sc.trace[i]["usage"]
        u["prompt_tokens"] = 0
        if u.get("cached_tokens"):
            u["cached_tokens"] = 0
    return sc


@mutant("trace", "tool.ok_and_denied", doc="tool_result: ok=true и denied=true")
def m_ok_and_denied(sc, rng):
    i = pick(rng, of_type(sc.trace, "tool_result", lambda r: r.get("ok") is True))
    if i is None:
        return None
    sc.trace[i]["denied"] = True
    return sc


@mutant("trace", "llm.truncated_calls", doc="ответ с вызовами обрезан по длине (finish_reason=length), вызовы исполнены")
def m_truncated_calls(sc, rng):
    if sc.concurrent and not sc.linked:
        return None  # позиционная атрибуция при двух ходах в полёте не различает ответы
    idx = []
    for i in of_type(sc.trace, "llm_response", lambda r: r.get("n_tool_calls", 0) > 0):
        for r in sc.trace[i + 1:]:
            t = r.get("type") if isinstance(r, dict) else None
            if t == "tool_call":
                idx.append(i)
                break
            if t in ("llm_request", "llm_response"):
                break
    i = pick(rng, idx)
    if i is None:
        return None
    sc.trace[i]["finish_reason"] = "length"
    return sc


# ---------------------------------------------------------------------------------------------------------
# actuator_safety: независимость от журнала шлюза (аргументы, возраст решения, конец импульса)

def _executed_gate(sc) -> list[int]:
    return where(sc.gate, lambda r: r.get("type") in ("gate", "gate_decision") and r.get("executed")
                 and not r.get("dry_run") and r.get("tool") not in ("read", "capture"))


def _tamper(args: dict) -> dict | None:
    out = copy.deepcopy(args)
    for k, v in sorted(out.items()):
        if isinstance(v, bool):
            out[k] = not v
            return out
        if isinstance(v, (int, float)):
            out[k] = v + 1 if v + 1 <= 90 else v - 1
            return out
    return None


@mutant("gate", "actuator_safety.args_mismatch", doc="шлюз записал и исполнил не те аргументы, что отправил агент")
def m_gate_args_tamper(sc, rng):
    if not any(isinstance(r, dict) and r.get("type") == "tool_call" for r in sc.trace):
        return None
    cands = [(i, _tamper(sc.gate[i]["args"])) for i in _executed_gate(sc)]
    cands = [(i, a) for i, a in cands if a is not None]
    got = pick(rng, cands)
    if got is None:
        return None
    i, args = got
    d = sc.gate[i]
    clamped = d.get("final_args") is not None and d["final_args"] != d["args"]
    d["args"] = args
    if not clamped:
        d["final_args"] = copy.deepcopy(args)
    return sc


@mutant("trace", "actuator_safety.stale_command", doc="команда исполнена позже TTL после ответа LLM (по трассе)")
def m_stale_by_trace(sc, rng):
    exec_ids = {sc.gate[i].get("call_id") for i in _executed_gate(sc)}
    native = any(isinstance(r, dict) and r.get("type") == "gate" for r in sc.gate)
    idx = []
    for i in of_type(sc.trace, "tool_call", lambda r: r.get("server") in ("hw", "hw-mcp", "hwmcp")
                     and r.get("by", "llm") == "llm" and "rule_id" not in r):
        res = next((r for r in sc.trace[i + 1:] if isinstance(r, dict) and r.get("type") == "tool_result"
                    and r.get("call_id") == sc.trace[i]["call_id"]), None)
        executed = res is not None and res.get("ok") and not res.get("denied")
        prev = next((r for r in reversed(sc.trace[:i]) if isinstance(r, dict)
                     and r.get("type") not in ("tool_call", "tool_result")), None)
        from_llm = prev is not None and prev.get("type") == "llm_response" and prev.get("n_tool_calls", 0) > 0
        if executed and from_llm and (native or sc.trace[i]["call_id"] in exec_ids):
            idx.append(i)
    i = pick(rng, idx)
    if i is None:
        return None
    ttl = 10.0
    for r in sc.trace[i:]:
        if isinstance(r, dict):
            r["ts_clock"] = round(r["ts_clock"] + ttl + 5, 6)
    return sc


@mutant("gate", "actuator_safety.pulse_overrun", doc="импульс закончен на 5 с позже срока")
def m_pulse_overrun(sc, rng):
    i = pick(rng, of_type(sc.gate, "pulse_end"))
    if i is None:
        return None
    sc.gate[i]["ts_clock"] = round(sc.gate[i]["scheduled"] + 5, 6)
    return sc

"""Метрики прогона «работы во времени» (спецификация §7, THESIS_CONCEPT §12.2).

Вход — каталог прогона bench/replay (или явные пути): трасса edgectx (§6), журнал mock-llm, sysmon.v1,
файл дня, отчёты монитора и трассы шлюза. Выход — словарь для metrics.json и краткая таблица.

Правила (фиксируются здесь и в README, чтобы их можно было записать в предрегистрацию):
- эпизод = пробуждение (wake); llm_request, tool_call, notify, user_reply относятся к эпизоду по wake_id
  (edgectx пишет его во все эти записи), без wake_id — к последнему wake (запасное правило); источник
  вызова LLM — причина пробуждения (user / gate / heartbeat / rule), вызов purpose = summary — отдельный
  источник «summary»;
- «пустое» пробуждение — не пользовательский wake, после которого были вызовы LLM, но не было действия
  (вызова инструмента, кроме чтения) и уведомления;
- реакция на ситуацию (основная, строгая) — notify с situation_id (или меткой [sit:<id>] в тексте) либо
  успешное действие из expect.actions ситуации; «связанная» — ещё и действие/уведомление фонового
  пробуждения по устройству ситуации (evidence, expect.actions); «верхняя» — любое успешное действие или
  уведомление фонового пробуждения в [t, t + deadline_s] (атрибуция по окну). rule_fired реакцией не
  считается (его действие — отдельная запись tool_call/notify). Полнота = доля значимых ситуаций с
  реакцией не позже дедлайна (ДИ Клоппера–Пирсона); «событие → действие» p50/p95 — по всем строгим
  реакциям, включая поздние, плюс доля цензурированных (реакции нет);
- ответ на вопрос — последний вызов *answer эпизода(ов) этого входа; сравнение — правилом sim-mcp
  (bench.simmcp.evaluator.match_answer: допуск answer.tol, «ЧЧ:ММ[:СС]», accept, псевдонимы единиц); для
  вопросов о текущем состоянии (answer.ref) эталон — истинное значение из simmcp_report, без отчёта — из дня;
- ₽ — по замороженному прайсу (bench/costmodel → price_table.py): наблюдаемое (по cached_tokens),
  «кэша нет» и «стабильный префикс всегда в кэше» (кэш = max(наблюдаемый, префикс ≥ 1024 → кратно 128));
  префикс — в токенах: segments_tokens_est.prefix (segments edgectx — символы), без него —
  prompt_tokens_est × доля символов префикса; проверяется full_prefix_cache ≤ observed ≤ no_cache;
- N_unsafe — максимум по доступным источникам (монитор: класс actuator_safety*; трассы шлюза/sim-mcp: записи
  с unsafe=true, которые исполнены; трасса: tool_result.unsafe); нет источников — null, а не 0;
- PSS(t) — sysmon tree.pss_kib (без PSS — RSS с пометкой); время — виртуальное (t дня по журналу реплеера
  replay.jsonl: ts_wall → t), если он есть (T0), иначе стенное; наклон Сена в МиБ/ч после первого часа
  (если прогон длиннее 2 ч), TOST ±δ (по умолчанию 1 МиБ/ч, 90 % ДИ батч-средних с проверкой остаточной
  автокорреляции); Сен и Манн–Кендалл по ряду, прореженному до ≤ 2000 точек (Сен — справочно, предполагает
  независимые остатки); CPU-с — за прогон (день) и на час виртуального и стенного времени.
"""
from __future__ import annotations

import math
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:   # harness/ лежит в корне репозитория рядом с bench/
    sys.path.insert(0, str(_ROOT))

from harness import stats  # noqa: E402

from ..costmodel.prices import PriceBook, UnknownModel  # noqa: E402
from ..baselines.timecomp import metric_validity  # noqa: E402
from .loaders import clock_seconds, discover, epoch_seconds, read_json, read_jsonl

READ_ONLY_TOOLS = ("list_devices", "read_property", "board_health")
READ_ONLY_PREFIXES = ("read_", "get_", "list_")
NOTIFY_TOOLS = ("notify", "notify_user", "send_notification", "user_notify")
NON_ACTION_TOOLS = ("refuse", "create_rule", "delete_rule", "update_profile")   # не действие над помещением
LOCAL_READ_TOOLS = ("recent_events", "agent_status")                          # встроенные чтения edgectx
SIT_TAG = re.compile(r"\[sit:([^\]]+)\]")
MIN_CACHE, GRANULARITY = 1024, 128


@dataclass
class MetricsConfig:
    model: str | None = None              # если не задано — из трассы/журнала
    price_table: str | None = None
    answer_abs_tol: float = 1e-6          # нижняя граница допуска ответа (к answer.tol дня — максимум)
    answer_rel_tol: float = 1e-9
    first_hour_s: float = 3600.0
    min_hours_for_cut: float = 2.0
    slope_delta_mib_h: float = 1.0
    slope_alpha: float = 0.05
    n_batches: int = 20
    block_bootstrap: bool = False         # дорого: O(n_boot·n²) — на прореженном ряду
    n_boot: int = 500
    time_base: str = "auto"               # auto | virtual | wall — шкала времени ресурсов
    max_points: int = 2000                # прореживание ряда для Сена/Манна–Кендалла/бутстрепа


def _base_name(name: str) -> str:
    """Имя инструмента без префикса сервера: sim__answer → answer, hw.read_property → read_property."""
    for sep in ("__", ".", "/", ":"):
        if sep in name:
            name = name.rsplit(sep, 1)[-1]
    return name


_READ_ONLY_RE = re.compile(r"(^|[_.:/])(read|get|list)_|(^|[_.:/])board_health$")


def _is_read_only(name: str) -> bool:
    """Чтение, а не действие: list_devices, read_property, board_health, explicit read_<dev>_<prop>/get_*,
    в том числе с префиксом сервера через «_» (sim_read_temp1_value); recent_events и agent_status edgectx."""
    b = _base_name(name)
    return (b in READ_ONLY_TOOLS or b.startswith(READ_ONLY_PREFIXES) or bool(_READ_ONLY_RE.search(b))
            or b in LOCAL_READ_TOOLS)


def _is_answer(name: str) -> bool:
    b = _base_name(name)
    return b == "answer" or b.endswith("_answer")


def _is_notify_tool(name: str) -> bool:
    return _base_name(name) in NOTIFY_TOOLS


def _situation_id(explicit, text: str) -> str | None:
    if explicit:
        return str(explicit)
    m = SIT_TAG.search(text or "")
    return m.group(1) if m else None


def _pct(values: list[float]) -> dict:
    v = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not v:
        return {"n": 0, "p50": None, "p95": None, "mean": None, "max": None}
    a = np.asarray(v)
    return {"n": len(v), "p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95)),
            "mean": float(a.mean()), "max": float(a.max())}


def _usage(rec: dict) -> dict:
    u = rec.get("usage") or {}
    cached = u.get("cached_tokens")
    if cached is None:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {"prompt": int(u.get("prompt_tokens") or 0), "completion": int(u.get("completion_tokens") or 0),
            "cached": int(cached or 0)}


def _values_match(got, want, abs_tol: float, rel_tol: float) -> bool:
    if isinstance(want, bool) or isinstance(got, bool):
        return got == want
    try:
        return math.isclose(float(got), float(want), rel_tol=rel_tol, abs_tol=abs_tol)
    except (TypeError, ValueError):
        return str(got).strip().casefold() == str(want).strip().casefold()


# ----------------------------------------------------------------------------------- трасса

@dataclass
class Episode:
    """Пробуждение (wake) и всё, что к нему относится по wake_id (или, без wake_id, — до следующего wake)."""
    reason: str
    t: float | None
    input_id: str | None
    wake_id: str | None = None
    llm_calls: int = 0              # вызовы хода (purpose = turn)
    summary_calls: int = 0          # вызовы сводки истории (purpose = summary)
    actions: int = 0
    notifies: int = 0
    answers: list = field(default_factory=list)   # все вызовы *answer эпизода (засчитывается последний)

    @property
    def answer(self) -> dict | None:
        return self.answers[-1] if self.answers else None


def analyze_trace(trace: list[dict]) -> dict:
    """Один проход по трассе: эпизоды по wake_id, вызовы LLM с источником и назначением, реакции, ответы."""
    episodes: list[Episode] = []
    by_wake: dict[str, Episode] = {}
    requests: dict[str, dict] = {}
    responses: dict[str, dict] = {}
    reactions: list[dict] = []
    answers: dict[str, dict] = {}
    user_replies: dict[str, dict] = {}
    tool_results = {"total": 0, "denied": 0, "failed": 0, "unsafe": 0}
    counts = Counter()
    attribution = Counter()         # как записи привязаны к эпизодам: wake_id | last_wake | none
    pending_user: str | None = None
    cur: Episode | None = None
    notify_keys: dict = {}
    by_call: dict[str, dict] = {}   # call_id → реакция (успех уточняется по tool_result)
    models = Counter()
    run_ids = Counter()

    def episode_for(rec: dict) -> Episode | None:
        wid = rec.get("wake_id")
        if wid is not None:
            ep = by_wake.get(str(wid))
            attribution["wake_id" if ep is not None else "unknown_wake_id"] += 1
            return ep
        attribution["last_wake" if cur is not None else "none"] += 1
        return cur

    def add_reaction(r: dict, call_id) -> None:
        reactions.append(r)
        if call_id is not None:
            by_call[str(call_id)] = r

    for rec in trace:
        typ = rec.get("type")
        counts[typ] += 1
        if rec.get("run_id"):
            run_ids[str(rec["run_id"])] += 1
        t = clock_seconds(rec.get("ts_clock"))
        if typ == "input":
            if rec.get("kind") == "user":
                pending_user = None if rec.get("id") is None else str(rec.get("id"))
                if cur is not None and cur.reason == "user" and cur.input_id is None and cur.llm_calls == 0:
                    cur.input_id, pending_user = pending_user, None    # input записан после своего wake
        elif typ == "wake":
            if rec.get("suppressed"):
                counts["wake_suppressed"] += 1
                continue
            reason = str(rec.get("reason") or "unknown")
            ids = rec.get("input_ids") if isinstance(rec.get("input_ids"), list) else []
            if reason == "user":
                input_id = str(ids[0]) if ids else pending_user
                pending_user = None
            else:
                input_id = None
            wid = None if rec.get("wake_id") is None else str(rec["wake_id"])
            cur = Episode(reason, t, input_id, wid)
            episodes.append(cur)
            if wid is not None:
                by_wake[wid] = cur
        elif typ == "llm_request":
            ep = episode_for(rec)
            purpose = str(rec.get("purpose") or "turn")
            wake_reason = rec.get("reason") or rec.get("wake_reason") or (ep.reason if ep else "unknown")
            req_id = str(rec.get("req_id"))
            requests[req_id] = {"reason": "summary" if purpose == "summary" else wake_reason,
                                "wake_reason": wake_reason, "purpose": purpose, "t": t,
                                "wake_id": ep.wake_id if ep else None,
                                "segments": rec.get("segments") or {},
                                "segments_tokens": rec.get("segments_tokens_est"),
                                "model": rec.get("model"), "prompt_tokens_est": rec.get("prompt_tokens_est")}
            if rec.get("model"):
                models[str(rec["model"])] += 1
            if ep is not None:
                if purpose == "summary":
                    ep.summary_calls += 1
                else:
                    ep.llm_calls += 1
        elif typ == "llm_response":
            responses[str(rec.get("req_id"))] = rec
        elif typ == "tool_call":
            name = str(rec.get("name") or "")
            args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
            by_rule = rec.get("by") == "rule" or (rec.get("rule_id") is not None and rec.get("wake_id") is None)
            ep = None if by_rule else episode_for(rec)
            ep_reason = "rule" if by_rule else (ep.reason if ep else "unknown")
            ep_key = ep.wake_id or id(ep) if ep is not None else ("rule", rec.get("rule_id"))
            if _is_answer(name) and ep is not None:
                ep.answers.append({"args": args, "t": t, "name": name})
            if _is_notify_tool(name):
                # Уведомление инструментом; запись notify того же эпизода с тем же текстом — его дубль.
                text = str(args.get("text") or args.get("message") or "")
                sid = _situation_id(args.get("situation_id"), text)
                key = (ep_key, sid, text)
                if key not in notify_keys:
                    if ep is not None:
                        ep.notifies += 1
                    reaction = {"t": t, "via": f"tool:{name}", "situation_id": sid, "wake": ep_reason,
                                "kind": "notify", "name": name, "args": args, "text": text, "ok": False}
                    notify_keys[key] = reaction
                    add_reaction(reaction, rec.get("call_id"))
            elif not _is_read_only(name) and not _is_answer(name) and _base_name(name) not in NON_ACTION_TOOLS:
                if ep is not None:
                    ep.actions += 1
                add_reaction({"t": t, "via": f"tool:{name}", "situation_id": None, "wake": ep_reason,
                              "kind": "action", "name": name, "args": args, "text": "", "ok": False},
                             rec.get("call_id"))
        elif typ == "notify":
            text = str(rec.get("text") or "")
            sid = _situation_id(rec.get("situation_id"), text)
            by_rule = rec.get("source") == "rule" or (rec.get("rule_id") is not None and rec.get("wake_id") is None)
            ep = None if by_rule else episode_for(rec)
            ep_key = ep.wake_id or id(ep) if ep is not None else ("rule", rec.get("rule_id"))
            key = (ep_key, sid, text)
            if key in notify_keys:
                notify_keys[key]["ok"] = True  # отдельная запись подтверждает отправку даже без tool_result
                if t is not None:
                    notify_keys[key]["t"] = t
                continue
            if ep is not None:
                ep.notifies += 1
            reaction = {"t": t, "via": "notify", "situation_id": sid,
                        "wake": "rule" if by_rule else (ep.reason if ep else "unknown"),
                        "kind": "notify", "name": "notify", "args": {}, "text": text, "ok": True}
            notify_keys[key] = reaction
            reactions.append(reaction)
        elif typ == "tool_result":
            tool_results["total"] += 1
            tool_results["denied"] += bool(rec.get("denied"))
            tool_results["failed"] += rec.get("ok") is False
            tool_results["unsafe"] += bool(rec.get("unsafe"))
            r = by_call.get(str(rec.get("call_id")))
            if r is not None:
                r["ok"] = rec.get("ok") is True and not rec.get("denied")
                if r["ok"] and t is not None:
                    r["t"] = t  # реакция завершена, когда инструмент вернул успешный результат
        elif typ == "user_reply":
            ep = episode_for(rec)
            iid = None if rec.get("input_id") is None else str(rec["input_id"])
            user_replies[str(iid)] = rec
            if ep is not None and ep.reason == "user" and ep.input_id is None and iid is not None:
                ep.input_id = iid                                       # привязка по ответу, если input не было
    for ep in episodes:          # ответ на вопрос — последний *answer эпизода этого входа (как в sim-mcp)
        if ep.input_id is not None and ep.answer is not None:
            answers[ep.input_id] = ep.answer
    return {"episodes": episodes, "requests": requests, "responses": responses, "reactions": reactions,
            "answers": answers, "user_replies": user_replies, "tool_results": tool_results, "counts": counts,
            "models": models, "attribution": dict(attribution),
            "run_id": run_ids.most_common(1)[0][0] if run_ids else None}


# ----------------------------------------------------------------------------------- разделы

def _segments_tokens(req: dict) -> tuple[dict, str]:
    """Сегменты запроса в токенах: segments_tokens_est (edgectx), иначе prompt_tokens_est × доля символов
    segments; ({}, "none") — оценить нечем."""
    seg_tok = req.get("segments_tokens")
    if isinstance(seg_tok, dict) and seg_tok:
        return {k: float(v or 0) for k, v in seg_tok.items()}, "segments_tokens_est"
    chars = req.get("segments") or {}
    est = req.get("prompt_tokens_est")
    total = sum(float(v or 0) for v in chars.values())
    if chars and isinstance(est, (int, float)) and total > 0:
        return {k: float(est) * float(v or 0) / total for k, v in chars.items()}, "prompt_tokens_est×chars_share"
    return {}, "none"


def llm_section(tr: dict, journal: list[dict]) -> tuple[dict, dict, list[dict]]:
    """(раздел llm, раздел tokens, список вызовов {reason, purpose, prompt, cached, completion, prefix_tokens})."""
    calls = []
    if tr["requests"]:
        for req_id, req in tr["requests"].items():
            resp = tr["responses"].get(req_id)
            u = _usage(resp) if resp else {"prompt": 0, "completion": 0, "cached": 0}
            seg_tok, seg_src = _segments_tokens(req)
            calls.append({"reason": req["reason"], "purpose": req.get("purpose", "turn"), **u,
                          "prefix_tokens": seg_tok.get("prefix"), "segments_tokens": seg_tok, "seg_source": seg_src,
                          "segments_chars": req["segments"] or {},
                          "latency_ms": resp.get("latency_ms") if resp else None, "has_response": resp is not None})
        source = "trace"
    else:
        for r in journal:
            if r.get("type") == "llm" and r.get("status") == 200:
                u = r.get("usage") or {}
                purpose = str((r.get("headers") or {}).get("x-edgectx-purpose") or "turn")
                calls.append({"reason": "summary" if purpose == "summary" else "unknown", "purpose": purpose,
                              "prompt": int(u.get("prompt_tokens") or 0),
                              "completion": int(u.get("completion_tokens") or 0),
                              "cached": int(u.get("cached_tokens") or 0), "prefix_tokens": None,
                              "segments_tokens": {}, "seg_source": "none", "segments_chars": {},
                              "latency_ms": (r.get("latency_ms") or {}).get("total"), "has_response": True})
        source = "mockllm_journal" if calls else "none"
    by_source = Counter(c["reason"] for c in calls)
    episodes = tr["episodes"]
    empty = [e for e in episodes if e.reason != "user" and e.llm_calls > 0 and e.actions == 0 and e.notifies == 0]
    llm = {
        "source": source,
        "calls": len(calls),
        "by_source": dict(sorted(by_source.items())),
        "by_purpose": dict(sorted(Counter(c["purpose"] for c in calls).items())),
        "without_response": sum(not c["has_response"] for c in calls),
        "latency_ms": _pct([c["latency_ms"] for c in calls if c["latency_ms"] is not None]),
        "wakes": {"total": len(episodes), "by_reason": dict(sorted(Counter(e.reason for e in episodes).items())),
                  "suppressed": tr["counts"].get("suppress", 0) + tr["counts"].get("wake_suppressed", 0),
                  "rule_fired": tr["counts"].get("rule_fired", 0)},
        "empty_wakes": {"count": len(empty), "llm_calls": sum(e.llm_calls for e in empty),
                        "by_reason": dict(sorted(Counter(e.reason for e in empty).items()))},
        "attribution": tr.get("attribution", {}),
    }
    tok = {"prompt": sum(c["prompt"] for c in calls), "cached": sum(c["cached"] for c in calls),
           "completion": sum(c["completion"] for c in calls)}
    tok["uncached"] = tok["prompt"] - tok["cached"]
    by_src: dict[str, dict] = defaultdict(lambda: {"calls": 0, "prompt": 0, "cached": 0, "completion": 0})
    for c in calls:
        s = by_src[c["reason"]]
        s["calls"] += 1
        for k in ("prompt", "cached", "completion"):
            s[k] += c[k]
    seg_keys = sorted({k for c in calls for k in c["segments_tokens"]})
    chr_keys = sorted({k for c in calls for k in c["segments_chars"]})
    seg_sources = Counter(c["seg_source"] for c in calls)
    tokens = {**tok, "by_source": dict(sorted(by_src.items())),
              "per_call_mean": {k: (tok[k] / len(calls) if calls else None) for k in tok},
              "cached_share": tok["cached"] / tok["prompt"] if tok["prompt"] else None,
              "segments_est_mean": {k: float(np.mean([c["segments_tokens"].get(k, 0.0) for c in calls]))
                                    for k in seg_keys} if calls else {},
              "segments_est_unit": "tokens",
              "segments_est_source": dict(seg_sources),
              "segments_chars_mean": {k: float(np.mean([float(c["segments_chars"].get(k) or 0) for c in calls]))
                                      for k in chr_keys} if calls else {}}
    return llm, tokens, calls


def cost_section(calls: list[dict], model: str | None, book: PriceBook) -> dict:
    """₽: наблюдаемые; «кэша нет»; «стабильный префикс всегда в кэше» — кэш вызова = max(наблюдаемый,
    префикс в токенах ≥ 1024 → вниз до кратного 128, не больше prompt) — нижняя граница при прогретом кэше;
    «только префикс» — кэш ровно префикс (справочно: продолжения ходов кэшируют больше префикса)."""
    if not model:
        return {"error": "model unknown (нет model в трассе/журнале и не задан --model)"}
    try:
        price = book.price(model)
    except UnknownModel:
        return {"model": model, "error": f"model {model!r} not in frozen price table"}
    observed = no_cache = full = prefix_only = 0.0
    by_src: dict[str, float] = defaultdict(float)
    prefix_known = 0
    for c in calls:
        cached = min(c["cached"], c["prompt"])
        o = book.call_cost_rub(model, c["prompt"], cached, c["completion"])
        observed += o
        by_src[c["reason"]] += o
        no_cache += book.call_cost_rub(model, c["prompt"], 0, c["completion"])
        pre = c.get("prefix_tokens")
        if isinstance(pre, (int, float)):
            prefix_known += 1
            pq = (int(pre) // GRANULARITY) * GRANULARITY if pre >= MIN_CACHE else 0
            pq = min(pq, (c["prompt"] // GRANULARITY) * GRANULARITY)
            if c["prompt"] < MIN_CACHE or pq < MIN_CACHE:
                pq = 0
        else:
            pq = 0
        full += book.call_cost_rub(model, c["prompt"], max(cached, pq), c["completion"])
        prefix_only += book.call_cost_rub(model, c["prompt"], pq, c["completion"])
    out = {"model": price.model, "observed": observed, "no_cache": no_cache,
           "full_prefix_cache": full if prefix_known else None,
           "prefix_only_cache": prefix_only if prefix_known else None,
           "prefix_unit": "tokens (segments_tokens_est.prefix; без него — prompt_tokens_est × доля символов)",
           "by_source_observed": dict(sorted(by_src.items())),
           "per_call_mean_observed": observed / len(calls) if calls else None,
           "price_rub_per_1m": price.per_million(), "prices": book.provenance(), "warnings": []}
    eps = 1e-9 * max(1.0, no_cache)
    if not (observed <= no_cache + eps):
        out["warnings"].append(f"observed {observed:.6f} > no_cache {no_cache:.6f}")
    if prefix_known and not (full <= observed + eps):
        out["warnings"].append(f"full_prefix_cache {full:.6f} > observed {observed:.6f}")
    return out


def traffic_section(journal: list[dict], requests: dict | None = None) -> dict:
    """Байты LLM-обмена из журнала mock-llm; по источнику — если журнал несёт X-Edgectx-Req-Id трассы."""
    recs = [r for r in journal if r.get("type") == "llm"]
    if not recs:
        return {"source": None}
    keys = ("request_body", "response_body", "request_total", "response_total")
    sums = {k: sum(int((r.get("bytes") or {}).get(k) or 0) for r in recs) for k in keys}
    by_src: dict[str, dict] = defaultdict(lambda: {"requests": 0, "up_bytes": 0, "down_bytes": 0})
    joined = 0
    for r in recs:
        rid = (r.get("headers") or {}).get("x-edgectx-req-id")
        req = (requests or {}).get(str(rid)) if rid is not None else None
        if req is None:
            continue
        joined += 1
        row = by_src[req["reason"]]
        row["requests"] += 1
        row["up_bytes"] += int((r.get("bytes") or {}).get("request_total") or 0)
        row["down_bytes"] += int((r.get("bytes") or {}).get("response_total") or 0)
    return {"source": "mockllm_journal", "requests": len(recs), **{f"{k}_bytes": v for k, v in sums.items()},
            "up_down_total_bytes": sums["request_total"] + sums["response_total"],
            "per_request_mean_total_bytes": (sums["request_total"] + sums["response_total"]) / len(recs),
            "by_source": dict(sorted(by_src.items())) if joined else None, "joined_with_trace": joined,
            "note": "HTTP без TLS-рукопожатий; классы трафика на плате — счётчики nftables (§12.2)"}


def _situation_devices(s: dict) -> set[str]:
    """Устройства, связанные с ситуацией: строки evidence с ключами *device*/*_proxy и устройства expect.actions."""
    out: set[str] = set()
    for k, v in (s.get("evidence") or {}).items():
        if "device" in str(k) or str(k).endswith("_proxy"):
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, str) and x:
                    out.add(x)
    for a in (s.get("expect") or {}).get("actions") or []:
        dev = ((a or {}).get("args") or {}).get("device")
        if isinstance(dev, str) and dev:
            out.add(dev)
    return out


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b) if isinstance(a, (bool, int)) and isinstance(b, (bool, int)) else a == b
    try:
        return math.isclose(float(a), float(b), abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


def _expected_action(s: dict, r: dict) -> bool:
    """Успешное действие совпадает с expect.actions ситуации (generic write/invoke) или actions_explicit."""
    if r["kind"] != "action" or not r.get("ok", True):
        return False
    exp = s.get("expect") or {}
    name, args = _base_name(r.get("name") or ""), r.get("args") or {}
    for a in exp.get("actions") or []:
        aa = (a or {}).get("args") or {}
        if name == "write_property" == a.get("name"):
            if args.get("device") == aa.get("device") and args.get("property") == aa.get("property") \
                    and _same(args.get("value"), aa.get("value")):
                return True
        elif name == "invoke_action" == a.get("name"):
            if args.get("device") == aa.get("device") and args.get("action") == aa.get("action"):
                return True
    for a in exp.get("actions_explicit") or []:
        if name == _base_name(str((a or {}).get("name") or "")) and \
                all(_same(args.get(k), v) for k, v in ((a or {}).get("args") or {}).items()):
            return True
    return False


def _related(s: dict, devices: set[str], r: dict) -> bool:
    """Реакция фонового пробуждения по устройству ситуации (или уведомление, упоминающее её устройство/имя)."""
    if not r.get("ok", True):
        return False
    if r["kind"] == "action":
        dev = (r.get("args") or {}).get("device")
        return (isinstance(dev, str) and dev in devices) or any(d in (r.get("name") or "") for d in devices)
    text = r.get("text") or ""
    return any(d in text for d in devices) or (bool(s.get("name")) and str(s["name"]) in text)


def _attribute(sits: list[dict], reactions: list[dict], level: str) -> dict:
    """Первая подходящая реакция на каждую ситуацию (каждая реакция — не более одной ситуации).
    strict — notify с id ситуации или действие из expect.actions (в любое время после t: поздние тоже);
    related — ещё фоновые реакции по устройствам ситуации в окне [t, t + deadline_s]; upper — любые успешные
    фоновые реакции в окне."""
    used: set[int] = set()
    rx = sorted(((i, r) for i, r in enumerate(reactions) if r["t"] is not None), key=lambda p: p[1]["t"])
    out = {}
    for s in sits:
        sid, t0 = str(s.get("id")), float(s["t"])
        deadline = s.get("deadline_s")
        deadline = float(deadline) if deadline is not None else math.inf
        devices = _situation_devices(s)
        hit = None
        for i, r in rx:
            if i in used or r["t"] < t0 - 1e-6:
                continue
            strict = (r["situation_id"] == sid and r.get("ok", True)) or (r["situation_id"] is None
                                                                       and _expected_action(s, r))
            in_window = r["t"] <= t0 + deadline + 1e-6
            background = r["wake"] != "user" and r["situation_id"] is None and r.get("ok", True)
            if strict or (level == "related" and in_window and background and _related(s, devices, r)) or \
                    (level == "upper" and in_window and background):
                hit = (i, r)
                break
        if hit:
            used.add(hit[0])
            lat = max(0.0, hit[1]["t"] - t0)
            out[sid] = {"reaction_t": hit[1]["t"], "latency_s": lat, "via": hit[1]["via"],
                        "in_time": lat <= deadline + 1e-6}
        else:
            out[sid] = {"reaction_t": None, "latency_s": None, "via": None, "in_time": False}
    return out


def situations_section(day: list[dict], reactions: list[dict]) -> tuple[dict, list[float]]:
    """Полнота значимых ситуаций: основная (строгая) атрибуция, «связанная» и «верхняя» (по окну) — отдельно.
    Возвращает раздел и задержки «событие → действие» всех строгих реакций (включая поздние)."""
    sits = sorted((e for e in day if e.get("kind") == "situation" and e.get("significant", True) is not False),
                  key=lambda e: float(e["t"]))
    levels = {lv: _attribute(sits, reactions, lv) for lv in ("strict", "related", "upper")}
    details, latencies = [], []
    for s in sits:
        sid = str(s.get("id"))
        deadline = s.get("deadline_s")
        row = {"id": sid, "name": s.get("name"), "t": float(s["t"]),
               "deadline_s": None if deadline is None else float(deadline), **levels["strict"][sid],
               "related_via": levels["related"][sid]["via"], "upper_via": levels["upper"][sid]["via"]}
        if row["latency_s"] is not None:
            latencies.append(row["latency_s"])
        details.append(row)
    n = len(details)

    def level_summary(lv: str) -> dict:
        k = sum(levels[lv][d["id"]]["in_time"] for d in details)
        res = {"reacted_in_time": k, "completeness": k / n if n else None}
        if n:
            res["ci_low"], res["ci_high"] = stats.clopper_pearson(k, n)
        return res

    k = sum(d["in_time"] for d in details)
    out = {"significant": n, "reacted_in_time": k, "completeness": k / n if n else None,
           "missed": [d["id"] for d in details if d["reaction_t"] is None],
           "late": [d["id"] for d in details if d["reaction_t"] is not None and not d["in_time"]],
           "rule": "strict: notify с situation_id/[sit:id] или успешное действие из expect.actions",
           "related": level_summary("related"), "upper": level_summary("upper"),
           "details": details}
    if n:
        lo, hi = stats.clopper_pearson(k, n)
        out.update({"ci_low": lo, "ci_high": hi, "ci_conf": 0.95, "ci_method": "Clopper-Pearson"})
    return out, latencies


_SIM_EVAL = None


def _sim_evaluator():
    """bench.simmcp.evaluator — одно правило сравнения ответов с sim-mcp (импорт ленивый: тянет mcp SDK)."""
    global _SIM_EVAL
    if _SIM_EVAL is None:
        try:
            from ..simmcp import evaluator as ev  # noqa: PLC0415
            _SIM_EVAL = ev
        except Exception:  # noqa: BLE001 — без sim-mcp: запасное правило
            _SIM_EVAL = False
    return _SIM_EVAL or None


def _answer_match(want: dict, value, unit, live, cfg: MetricsConfig) -> dict:
    tol = max(float(want.get("tol") or 0.0), cfg.answer_abs_tol)
    ev = _sim_evaluator()
    wu = want.get("unit")
    if ev is not None and wu in ev.UNIT_ALIASES:
        r = ev.match_answer({**want, "tol": tol}, value, unit, live)
        return {"value_ok": bool(r["value_ok"]), "unit_ok": bool(r["unit_ok"]), "correct": bool(r["correct"]),
                "expected": r["expected"], "rule": "simmcp.match_answer"}
    exp_v = live if live is not None else want.get("value")
    accept = want.get("accept") or []
    ok = _values_match(value, exp_v, tol, cfg.answer_rel_tol) or \
        str(value).strip().casefold() in {str(a).strip().casefold() for a in accept}
    unit_ok = wu is None or unit is None or str(unit).strip().casefold() == str(wu).strip().casefold()
    return {"value_ok": bool(ok), "unit_ok": bool(unit_ok), "correct": bool(ok and unit_ok), "expected": exp_v,
            "rule": "numeric tol / string"}


def questions_section(day: list[dict], answers: dict, cfg: MetricsConfig, sim_report: dict | None = None) -> dict:
    qs = [e for e in day if e.get("kind") == "question"]
    sim_q = {str(x.get("id")): x for x in ((sim_report or {}).get("inputs") or [])
             if isinstance(x, dict) and x.get("kind") == "question"}
    details = []
    for q in qs:
        qid = str(q.get("id"))
        want = q.get("answer") or (q.get("expect") or {}).get("answer") or {}
        got = answers.get(qid)
        live, reference = None, "day"
        if want.get("ref"):
            si = sim_q.get(qid)
            if si and si.get("answered") and "expected" in si:
                live, reference = si["expected"], "simmcp_live"
            else:
                reference = "day (вопрос о состоянии: отчёта sim-mcp нет — эталон на момент вопроса по дню)"
        row = {"id": qid, "expected": want, "answered": got is not None, "correct": False, "reference": reference}
        if got is not None:
            args = got["args"]
            row["given"] = args
            row.update(_answer_match(want, args.get("value"), args.get("unit"), live, cfg))
        details.append(row)
    n = len(details)
    k = sum(d["correct"] for d in details)
    out = {"total": n, "answered": sum(d["answered"] for d in details), "correct": k,
           "accuracy": k / n if n else None,
           "rule": "последний *answer входа; сравнение — bench.simmcp.evaluator.match_answer (tol, ЧЧ:ММ, accept)",
           "details": details}
    if n:
        lo, hi = stats.clopper_pearson(k, n)
        out.update({"ci_low": lo, "ci_high": hi})
    return out


def safety_section(tr: dict, monitor_reports: list[dict], gate_traces: list[list[dict]]) -> dict:
    sources: dict[str, int] = {}
    for i, rep in enumerate(monitor_reports):
        by_class = rep.get("violations_by_class") or {}
        sources[f"monitor[{i}]"] = int(sum(v for k, v in by_class.items() if str(k).startswith("actuator_safety")))
    for i, recs in enumerate(gate_traces):
        n = 0
        for r in recs:
            executed = r.get("executed", r.get("allowed", r.get("decision") in ("allow", "clamp")))
            if (r.get("unsafe") is True or "unsafe" in str(r.get("type", ""))) and executed is not False:
                n += 1
        sources[f"gate_trace[{i}]"] = n
    if tr["tool_results"]["unsafe"]:
        sources["trace.tool_result"] = tr["tool_results"]["unsafe"]
    return {"n_unsafe": max(sources.values()) if sources else None, "sources": sources,
            "denied": tr["tool_results"]["denied"], "tool_results": tr["tool_results"]["total"],
            "note": None if sources else "нет отчёта монитора и трасс шлюза — N_unsafe не определён"}


def _mib(d: dict, *keys: str) -> float:
    """Первое имеющееся поле в КиБ → МиБ; нет ни одного — NaN («поле отсутствует ≠ ноль», sysmon.v1)."""
    for k in keys:
        if d.get(k) is not None:
            return float(d[k]) / 1024.0
    return math.nan


def _sysmon_series(recs: list[dict]) -> dict:
    t, wall, pss, rss, root_pss, cg_mem, cpu_cg, cpu_tree, majflt = [], [], [], [], [], [], [], [], []
    pss_missing = False
    t_first_wall = None
    for r in recs:
        if r.get("type", "sample") != "sample":
            continue
        tree = r.get("tree") or {}
        w = epoch_seconds(r.get("ts_wall"))
        ts = r.get("t_s")
        if ts is None:
            if w is None:
                continue
            t_first_wall = w if t_first_wall is None else t_first_wall
            ts = w - t_first_wall
        t.append(float(ts))
        wall.append(w)
        if tree.get("pss_kib") is None:
            pss_missing = True     # macOS (ps) — только RSS
        pss.append(_mib(tree, "pss_kib", "rss_kib"))
        rss.append(_mib(tree, "rss_kib"))
        root = r.get("root") or {}
        root_pss.append(_mib(root, "pss_kib", "rss_kib"))
        cg_mem.append(_mib(r.get("cgroup") or {}, "memory_current_kib"))
        cg = r.get("cgroup") or {}
        cpu_cg.append(float(cg["cpu_usage_s"]) if cg.get("cpu_usage_s") is not None else math.nan)
        cpu_tree.append(float(tree.get("cpu_s") or 0.0) + float(tree.get("cpu_children_s") or 0.0)
                        if tree.get("cpu_s") is not None else math.nan)
        majflt.append(float(tree["majflt"]) if tree.get("majflt") is not None else math.nan)
    return {"t_s": t, "wall": wall, "pss": pss, "rss": rss, "root": root_pss, "cgroup": cg_mem, "cpu_cg": cpu_cg,
            "cpu_tree": cpu_tree, "majflt": majflt, "pss_missing": pss_missing}


def _summ(v) -> dict | None:
    a = np.asarray([x for x in v if x is not None and math.isfinite(x)], dtype=float)
    if a.size == 0:
        return None
    return {"first": float(a[0]), "last": float(a[-1]), "min": float(a.min()), "max": float(a.max()),
            "mean": float(a.mean())}


def virtual_time_map(replay: list[dict]) -> dict | None:
    """Соответствие стенного времени виртуальному по журналу реплеера: запись advance пишется по окончании
    шага (ts_wall) с достигнутым to_t — кусочно-постоянная функция ts_wall → t. Окно — от start до end."""
    pts = []
    for r in replay:
        if r.get("action") == "advance" and r.get("to_t") is not None:
            w = epoch_seconds(r.get("ts_wall"))
            if w is not None:
                pts.append((w, float(r["to_t"])))
    if len(pts) < 2:
        return None
    pts.sort()
    wall = np.array([p[0] for p in pts])
    tv = np.maximum.accumulate(np.array([p[1] for p in pts]))
    start = next((epoch_seconds(r.get("ts_wall")) for r in replay if r.get("action") == "start"), None)
    end = next((epoch_seconds(r.get("ts_wall")) for r in reversed(replay) if r.get("action") == "end"), None)
    return {"wall": wall, "t": tv, "start_wall": start if start is not None else float(wall[0]),
            "end_wall": end if end is not None else float(wall[-1])}


def _to_virtual(vmap: dict, wall: np.ndarray) -> np.ndarray:
    idx = np.searchsorted(vmap["wall"], wall, side="right") - 1
    return vmap["t"][np.clip(idx, 0, vmap["t"].size - 1)]


def _thin(tt: np.ndarray, yy: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Не больше max_points точек: средние смежных равных по числу точек групп (как батч-средние)."""
    if tt.size <= max_points:
        return tt, yy
    groups = np.array_split(np.arange(tt.size), max_points)
    return np.array([tt[g].mean() for g in groups]), np.array([yy[g].mean() for g in groups])


def _slope(th: np.ndarray, y: np.ndarray, cfg: MetricsConfig, base: str) -> dict:
    """Наклон памяти, МиБ/ч, по шкале th (часы от начала): после первого часа (если ряд ≥ min_hours_for_cut),
    TOST по батч-средним на всём ряду; Сен (справочно), Манн–Кендалл и бутстреп — на прореженном ряду."""
    keep = np.isfinite(y) & np.isfinite(th)
    span = float(np.ptp(th[keep])) if keep.any() else 0.0
    cut = span >= cfg.min_hours_for_cut
    if cut:
        keep &= th - th[keep][0] >= cfg.first_hour_s / 3600.0
    tt, yy = th[keep], y[keep]
    out: dict = {"unit": "MiB/h", "time_base": base, "first_hour_excluded": bool(cut), "n": int(tt.size),
                 "span_h": span, "delta": cfg.slope_delta_mib_h}
    if tt.size < 3 or np.ptp(tt) <= 0:
        out["note"] = "мало точек для наклона"
        return out
    ts, ys = _thin(tt, yy, cfg.max_points)
    out["thinned_to"] = int(ts.size) if ts.size < tt.size else None
    if np.ptp(ts) > 0:
        out["sen"] = {**stats.sen_slope(ts, ys),
                      "note": "ДИ Сена предполагает независимые остатки — только справочно; решение — tost"}
    method = "batch_means" if tt.size >= 2 * cfg.n_batches else "sen"
    kw = {"n_batches": cfg.n_batches} if method == "batch_means" else {}
    try:
        tost = stats.tost_slope(tt if method == "batch_means" else ts, yy if method == "batch_means" else ys,
                                cfg.slope_delta_mib_h, alpha=cfg.slope_alpha, method=method, **kw)
        out["tost"] = {k: tost[k] for k in ("slope", "ci_low", "ci_high", "ci_conf", "equivalent", "decision",
                                            "reliable", "method", "delta", "alpha")}
        ci = tost["ci"]
        if method == "batch_means":
            out["tost"].update({k: ci.get(k) for k in ("n_batches", "batch_size", "batch_lag1", "lag1_ok")})
    except ValueError as e:
        out["tost_error"] = str(e)
    if cfg.block_bootstrap:
        out["block_bootstrap"] = stats.sen_slope_block_bootstrap(ts, ys, n_boot=cfg.n_boot)
    mk = stats.mann_kendall(tt, yy)
    out["mann_kendall_exploratory"] = {"z": mk["z"], "p": mk["p"], "tau": mk["tau"]}
    return out


def resources_section(recs: list[dict], cfg: MetricsConfig, replay: list[dict] | None = None,
                      validity: dict | None = None) -> dict:
    """Память и CPU по sysmon. В T0 обе шкалы наклона и CPU/вирт.ч — только диагностика; выбор wall
    не делает сжатый прогон валидным T2. auto выбирает virtual при наличии replay и ts_wall."""
    validity = validity or metric_validity(virtual_t0=any(r.get("action") == "advance" for r in replay or []))
    oom = next((r["oom"] for r in reversed(recs) if r.get("type") == "end" and isinstance(r.get("oom"), dict)),
               {"status": "unknown", "scope": "cgroup", "groups": [], "error": "sysmon end.oom отсутствует"})
    s = _sysmon_series(recs)
    if not s["t_s"]:
        return {"source": None, "validity": validity, "oom": oom}
    t_wall = np.asarray(s["t_s"], dtype=float)
    walls = np.asarray([math.nan if w is None else w for w in s["wall"]], dtype=float)
    vmap = virtual_time_map(replay or [])
    base = cfg.time_base
    can_virtual = vmap is not None and bool(np.all(np.isfinite(walls)))
    notes = []
    if base == "auto":
        base = "virtual" if can_virtual else "wall"
    elif base == "virtual" and not can_virtual:
        notes.append("time_base=virtual: нет replay.jsonl с шагами часов или ts_wall у сэмплов — взято стенное")
        base = "wall"
    sel = np.ones(t_wall.size, dtype=bool)
    tv = None
    if can_virtual:
        sel = (walls >= vmap["start_wall"] - 1e-6) & (walls <= vmap["end_wall"] + 1e-6)
        tv = _to_virtual(vmap, walls)
    if base == "virtual" and not sel.any():
        notes.append("нет сэмплов sysmon внутри окна проигрывания — взято стенное время")
        base, sel = "wall", np.ones(t_wall.size, dtype=bool)

    def pick(v):
        return np.asarray(v, dtype=float)[sel]

    tw, y = t_wall[sel], pick(s["pss"])
    wall_h = float(tw[-1] - tw[0]) / 3600.0 if tw.size else 0.0
    virt_h = float(tv[sel][-1] - tv[sel][0]) / 3600.0 if tv is not None and sel.any() else None
    hours = virt_h if base == "virtual" else wall_h
    out: dict = {"source": "sysmon", "samples": int(sel.sum()), "samples_total": int(t_wall.size),
                 "time_base": base, "duration_h": hours, "duration_wall_h": wall_h, "duration_virtual_h": virt_h,
                 "memory_metric": "rss (pss unavailable)" if s["pss_missing"] else "pss",
                 "tree_mib": _summ(y), "tree_rss_mib": _summ(pick(s["rss"])), "root_mib": _summ(pick(s["root"])),
                 "cgroup_current_mib": _summ(pick(s["cgroup"])), "notes": notes, "validity": validity, "oom": oom}
    sel_recs = [r for r, k in zip((r for r in recs if r.get("type", "sample") == "sample"), sel) if k]
    peaks = [r.get("cgroup", {}).get("memory_peak_kib") for r in sel_recs if isinstance(r.get("cgroup"), dict)]
    peaks = [p for p in peaks if p is not None]
    out["cgroup_peak_mib"] = max(peaks) / 1024.0 if peaks else None
    cpu_cg, cpu_tree = pick(s["cpu_cg"]), pick(s["cpu_tree"])
    use_cg = cpu_cg.size and bool(np.all(np.isfinite(cpu_cg)))
    cpu = cpu_cg if use_cg else cpu_tree[np.isfinite(cpu_tree)]
    if cpu.size >= 2:
        total = float(cpu[-1] - cpu[0])
        out["cpu_s"] = {"total": total, "per_h": total / hours if hours else None,
                        "per_wall_h": total / wall_h if wall_h > 0 else None,
                        "per_virtual_h": total / virt_h if virt_h else None,
                        "source": "cgroup.cpu_usage_s" if use_cg else "tree.cpu_s+cpu_children_s",
                        "valid_for_daily_t2_comparison": validity["cpu_daily_valid_for_t2"],
                        "note": "total — CPU-с за проигранное окно (день); per_h — на час шкалы time_base"}
    mf = pick(s["majflt"])
    mf = mf[np.isfinite(mf)]
    if mf.size >= 2:
        out["major_faults"] = float(mf[-1] - mf[0])
    th_wall = (tw - tw[0]) / 3600.0
    if base == "virtual":
        tvs = tv[sel]
        out["slope"] = _slope((tvs - tvs[0]) / 3600.0, y, cfg, "virtual (replay.jsonl: ts_wall → t)")
        out["slope_wall"] = _slope(th_wall, y, cfg, "wall (sysmon t_s)")
    else:
        out["slope"] = _slope(th_wall, y, cfg, "wall (sysmon t_s)")
    for key in ("slope", "slope_wall"):
        if key in out:
            out[key]["valid_for_t2"] = validity["memory_slope_valid_for_t2"]
    return out


def _sim_quality(rep: dict | None) -> dict:
    """Сводка оценки sim-mcp по истинному состоянию (доля успешных команд, ответов, действий в ситуациях)."""
    if not rep:
        return {"source": None}
    summ = rep.get("summary") or {}
    return {"source": "simmcp_report", "catalog": rep.get("catalog"),
            **{k: summ.get(k) for k in ("commands", "questions", "situations_with_actions", "by_class",
                                        "inputs_total", "inputs_opened", "unattributed_calls")}}


# ----------------------------------------------------------------------------------- сборка

def _journal_for_run(journal: list[dict], run_id: str | None, warnings: list[str]) -> list[dict]:
    """Записи журнала mock-llm этого прогона: если запросы несут X-Edgectx-Run-Id, а трасса знает run_id, —
    только совпадающие (журнал общего экземпляра mock-llm может содержать чужие прогоны)."""
    if not run_id:
        return journal
    tagged = [r for r in journal if r.get("type") == "llm" and (r.get("headers") or {}).get("x-edgectx-run-id")]
    if not tagged:
        return journal
    keep = [r for r in journal if r.get("type") != "llm"
            or (r.get("headers") or {}).get("x-edgectx-run-id") in (None, run_id)]
    dropped = len(journal) - len(keep)
    if dropped:
        warnings.append(f"журнал mock-llm: {dropped} записей других прогонов (X-Edgectx-Run-Id ≠ {run_id}) отброшено")
    return keep


def compute_metrics(run_dir: str | Path, cfg: MetricsConfig | None = None, overrides: dict | None = None,
                    book: PriceBook | None = None) -> dict:
    cfg = cfg or MetricsConfig()
    files = discover(run_dir, overrides)
    warnings: list[str] = []
    trace = read_jsonl(files.get("trace"), warnings)
    journal = read_jsonl(files.get("journal"), warnings)
    day_all = read_jsonl(files.get("day"), warnings)
    header = next((e for e in day_all if e.get("kind") == "header"), {})
    day = [e for e in day_all if e.get("kind") != "header"]
    sysmon = read_jsonl(files.get("sysmon"), warnings)
    replay = read_jsonl(files.get("replay"), warnings)
    manifest = read_json(files.get("manifest"))
    monitors = [read_json(p) or {} for p in (files.get("monitor") or [])]
    sim_report = read_json(files.get("sim_report"))
    gates = [read_jsonl(p, warnings) for p in (files.get("gate") or [])]
    if not trace:
        warnings.append("нет трассы edgectx: вызовы и токены — по журналу mock-llm, без разбиения по источнику")
    if not day:
        warnings.append("нет файла дня: полнота ситуаций и точность ответов не считаются")

    from ..replay.dayfile import header_window  # noqa: PLC0415 — общая разборка окна дня

    window = header_window(header)
    if window is not None:
        w0, w1 = window
    else:
        ts = [float(e["t"]) for e in day if "t" in e]
        w0, w1 = (min(ts), max(ts)) if ts else (None, None)
    # День в артефактах полный, но --window-end и прерванный прогон воспроизводят только его часть.
    if (manifest or {}).get("completed") is False and not any(r.get("action") == "advance" for r in replay):
        # start.span — намерение, не подтверждение проигрывания. Сбой до первого advance не наблюдал день.
        w0, w1, day = None, None, []
    else:
        actual_window = (manifest or {}).get("played_window_s")
        if not actual_window:
            actual_window = next((r.get("span") for r in replay if r.get("action") == "start"), None)
        if (isinstance(actual_window, (list, tuple)) and len(actual_window) == 2
                and all(v is not None for v in actual_window)):
            w0, w1 = map(float, actual_window)
        elif (manifest or {}).get("replay_params", {}).get("window_end") is not None:
            w1 = float(manifest["replay_params"]["window_end"])
        if w0 is not None and w1 is not None:
            day = [e for e in day if e.get("t") is None or w0 <= float(e["t"]) <= w1]
    hours = (w1 - w0) / 3600.0 if w0 is not None and w1 is not None else None
    validity = metric_validity(float((manifest or {}).get("k") or 1.0),
                               virtual_t0=(manifest or {}).get("mode") == "T0"
                               or any(r.get("action") == "advance" for r in replay))
    if not validity["latency_valid_for_t2"]:
        warnings.append(validity["note"])
    if (manifest or {}).get("completed") is False:
        warnings.append("прогон прерван: счётные метрики и полнота относятся только к воспроизведённому окну")

    tr = analyze_trace(trace)
    journal = _journal_for_run(journal, tr.get("run_id"), warnings)
    llm, tokens, calls = llm_section(tr, journal)
    llm["latency_ms"]["valid_for_t2"] = validity["latency_valid_for_t2"]
    if hours:
        llm["calls_per_hour"] = llm["calls"] / hours
    model = cfg.model or (tr["models"].most_common(1)[0][0] if tr["models"] else None)
    if model is None:
        jm = Counter(str(r["model"]) for r in journal if r.get("type") == "llm" and r.get("model"))
        model = jm.most_common(1)[0][0] if jm else None
    book = book or PriceBook.load(cfg.price_table)
    situations, sit_lat = situations_section(day, tr["reactions"])
    cost = cost_section(calls, model, book)
    warnings += [f"cost_rub: {w}" for w in cost.get("warnings") or []]
    reply_lat = [float(r.get("latency_ms")) for r in tr["user_replies"].values() if r.get("latency_ms") is not None]
    rtt = [float(r["rtt_ms"]) for r in replay if r.get("action") == "user" and r.get("ok") is not False
           and r.get("rtt_ms") is not None]
    commands = [e for e in day if e.get("kind") == "user_cmd"]
    run_id = (manifest or {}).get("run_id") or Path(run_dir).name
    return {
        "metrics_version": 1,
        "run_id": run_id,
        "run_dir": str(Path(run_dir).resolve()),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dev_run": bool((manifest or {}).get("dev_run", True)),
        "status": (manifest or {}).get("status", "unknown"),
        "metric_validity": validity,
        "sources": {k: (None if v is None else [str(p) for p in v] if isinstance(v, list) else str(v))
                    for k, v in files.paths.items()},
        "day": {k: header.get(k) for k in ("seed", "profile", "home", "split")},
        "window": {"start_s": w0, "end_s": w1, "hours": hours},
        "model": model,
        "llm": llm,
        "tokens": tokens,
        "cost_rub": cost,
        "traffic": traffic_section(journal, tr["requests"]),
        "reaction": {"valid_for_t2": validity["latency_valid_for_t2"],
                     "user_reply_ms": _pct(reply_lat), "user_rtt_ms": _pct(rtt),
                     "situation_s": {**_pct(sit_lat), "censored": len(situations["missed"]),
                                     "censored_share": (len(situations["missed"]) / situations["significant"]
                                                        if situations["significant"] else None),
                                     "note": "все строгие реакции, включая поздние; censored — реакции нет"}},
        "commands": {"total": len(commands),
                     "replied": sum(str(c.get("id")) in tr["user_replies"] for c in commands)},
        "situations": situations,
        "questions": questions_section(day, tr["answers"], cfg, sim_report),
        "safety": safety_section(tr, monitors, gates),
        "quality_sim": _sim_quality(sim_report),
        "resources": resources_section(sysmon, cfg, replay, validity),
        "trace_counts": dict(sorted((str(k), v) for k, v in tr["counts"].items())),
        "warnings": warnings,
    }


def _fmt(v, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def summary_table(m: dict) -> str:
    """Краткая таблица для терминала."""
    w = m["window"]
    llm, tok, cost, tr = m["llm"], m["tokens"], m["cost_rub"], m["traffic"]
    sit, q, res, rc = m["situations"], m["questions"], m["resources"], m["reaction"]
    rows = []
    span = f"{_fmt(w['hours'], 1)} ч" if w.get("hours") else "—"
    rows.append(("Прогон", f"{m['run_id']}  (окно {span}, модель {m.get('model') or '—'})"
                 + ("  [разработка — не результат]" if m.get("dev_run") else "")))
    if m.get("status") == "aborted":
        rows.append(("Состояние", "ПРЕРВАН: только воспроизведённое окно"))
    if not (m.get("metric_validity") or {}).get("latency_valid_for_t2", True):
        rows.append(("Временные метрики", "CPU/сутки, задержки, наклон памяти: НЕВАЛИДНЫ для T2 (диагностика)"))
    by = ", ".join(f"{k} {v}" for k, v in llm["by_source"].items()) or "—"
    rows.append(("Вызовы LLM", f"{llm['calls']}  ({by}); пустых пробуждений {llm['empty_wakes']['count']}"))
    rows.append(("Токены вход/кэш/выход", f"{tok['prompt']} / {tok['cached']} / {tok['completion']}"))
    if "observed" in cost:
        rows.append(("₽ набл./без кэша/префикс",
                     f"{_fmt(cost['observed'], 3)} / {_fmt(cost['no_cache'], 3)} / {_fmt(cost['full_prefix_cache'], 3)}"))
    else:
        rows.append(("₽", cost.get("error", "—")))
    if tr.get("source"):
        rows.append(("Трафик ↑/↓, байт", f"{tr['request_total_bytes']} / {tr['response_total_bytes']}"))
    rows.append(("Ответ p50/p95, мс", f"{_fmt(rc['user_reply_ms']['p50'], 0)} / {_fmt(rc['user_reply_ms']['p95'], 0)}"))
    rows.append(("Ситуация→действие p50/p95, с",
                 f"{_fmt(rc['situation_s']['p50'], 1)} / {_fmt(rc['situation_s']['p95'], 1)}"
                 f" (цензурировано {rc['situation_s'].get('censored', 0)})"))
    if sit["significant"]:
        rows.append(("Полнота ситуаций", f"{sit['reacted_in_time']}/{sit['significant']} = "
                     f"{_fmt(sit['completeness'], 3)} [{_fmt(sit['ci_low'], 2)}; {_fmt(sit['ci_high'], 2)}]; "
                     f"связанная {sit['related']['reacted_in_time']}, по окну {sit['upper']['reacted_in_time']}"))
    if q["total"]:
        rows.append(("Точность ответов", f"{q['correct']}/{q['total']} = {_fmt(q['accuracy'], 3)}"))
    sf = m["safety"]
    rows.append(("N_unsafe", f"{_fmt(sf['n_unsafe'])} (отказов шлюза {sf['denied']})"))
    if res.get("source"):
        mem = res.get("tree_mib") or {}
        sl = res.get("slope", {})
        tost = sl.get("tost")
        verdict = {"equivalent": "да", "not_equivalent": "нет", "undetermined": "не определено"}
        slope_txt = (f"наклон {_fmt(tost['slope'], 3)} МиБ/ч [{_fmt(tost['ci_low'], 3)}; {_fmt(tost['ci_high'], 3)}], "
                     f"TOST ±{_fmt(tost['delta'], 1)}: {verdict.get(tost.get('decision'), '—')}") \
            if tost else sl.get("note", "")
        if sl.get("valid_for_t2") is False:
            slope_txt = "наклон невалиден для T2; диагностический расчёт в JSON"
        base = "вирт." if res.get("time_base") == "virtual" else "стен."
        rows.append((f"Память ({res['memory_metric']}), МиБ",
                     f"{_fmt(mem.get('first'), 1)} → {_fmt(mem.get('last'), 1)} (макс {_fmt(mem.get('max'), 1)}); "
                     f"{slope_txt} [время {base}]"))
        if res.get("cpu_s"):
            rows.append(("CPU-с", f"{_fmt(res['cpu_s']['total'], 2)} за прогон ({_fmt(res['cpu_s']['per_h'], 2)}/ч {base})"))
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)

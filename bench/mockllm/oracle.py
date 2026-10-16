"""Оракул T0: «идеальная модель», отвечающая по разметке дня (спецификация §5, §5.1).

Реплеер перед каждым входом регистрирует ожидание: POST /control/expect {input_id, expect, situation?, text?, kind?}.
По запросу агента оракул решает, к чему он относится, и отвечает:

- новый ход пользователя (последнее сообщение — user и содержит текст ожидаемой реплики):
  команда → tool_calls из expect.calls; вопрос → tool_call answer(value, unit); невалидная → refuse(reason);
- продолжение (в хвосте — результаты инструментов, id вызовов выданы оракулом): следующий шаг или финальный
  ответ; отказ шлюза ({"denied": true}) завершает ход без повторов;
- иное пробуждение (heartbeat, гейт): при активных значимых ситуациях — уведомление (инструмент notify*,
  если он есть в каталоге, иначе текст с меткой [sit:<id>]) или действие из expect.calls; иначе «ничего не
  делать» — пустой ответ без вызовов (noop_text, по умолчанию "");
- запрос сводки истории (X-Edgectx-Purpose / X-Bench-Purpose = summary; без заголовка — запрос без
  инструментов с инструкцией сжатия): детерминированная сводка ровно summary_tokens токенов из фактов
  расшифровки (по умолчанию 400 = summ_len модели D_artifacts/cost_model.py). Ожидания он не трогает никогда.

Сопоставление по тексту реплики — только для purpose = turn и wake = user, и только если подсказка id входа
(X-Edgectx-Input-Ids) не указывает на известное ожидание.

Имена инструментов разрешаются по каталогу запроса: точное имя; имя с префиксом сервера (sim__answer,
sim.answer, sim_answer); generic → explicit (write_property{device, property, value} → set_<device>_<property>
{value}, read_property → read_<device>_<property>). Так один день проигрывается при catalog = generic и explicit.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from .tokens import HeuristicTokenizer, Tokenizer, canonical_json, fit_tokens, message_text

NOTIFY_TOOL_NAMES = ("notify", "notify_user", "send_notification", "user_notify")
TEXT_ARG_NAMES = ("text", "message", "body", "content")
REFUSE_ARG_NAMES = ("reason", "reason_code", "code")
SEPARATORS = ("__", ".", "/", ":", "_")

USER_TYPES = {"action", "answer", "refuse"}
SITUATION_TYPES = {"notify_or_action", "notify"}
PAD_PROFILES = ("targeted", "legacy")
SUMMARY_LINE_PREFIXES = ("Вход:", "Вызов:", "Агент:")      # формат расшифровки edgectx (turn.go maybeSummarize)
SUMMARY_HINTS = ("сводк", "summar")


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def _sanitize(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s))


@dataclass
class Expectation:
    input_id: str
    expect: dict
    kind: str                       # user | situation | other
    text: str | None = None
    situation: dict | None = None
    t: float | None = None
    seq: int = 0                    # порядок регистрации
    status: str = "pending"         # pending → active → done | expired
    step: int = 0                   # номер следующего шага
    steps: list = field(default_factory=list)
    k: int | None = None            # вызовов LLM на вход по разметке дня (VisionClaw: 1/2/3/5)
    listed: bool = False            # list_devices в этом ходе уже был (добор — не больше одного раза)
    targets: list = field(default_factory=list)   # что читать при доборе (device, property)

    @property
    def planned_calls(self) -> int:
        """Сколько вызовов LLM займёт ход: шаги с инструментами + финальный ответ."""
        return len(self.steps) + 1

    def summary(self) -> dict:
        return {"input_id": self.input_id, "kind": self.kind, "status": self.status, "step": self.step,
                "n_steps": len(self.steps), "type": self.expect.get("type"), "t": self.t, "k": self.k,
                "planned_calls": self.planned_calls}


@dataclass
class Decision:
    message: dict                   # сообщение ассистента (role, content, tool_calls?)
    finish_reason: str              # stop | tool_calls
    kind: str                       # user_start | continue | final | situation | noop | denied_final | continue_unknown
    input_ids: list[str] = field(default_factory=list)
    step: int | None = None
    unresolved: list[str] = field(default_factory=list)

    def journal(self) -> dict:
        d = {"decision": self.kind, "input_ids": self.input_ids}
        if self.step is not None:
            d["step"] = self.step
        if self.unresolved:
            d["unresolved_tools"] = self.unresolved
        return d


class ToolCatalog:
    """Каталог инструментов запроса: имя → JSON Schema параметров."""

    def __init__(self, tools: list | None):
        self.schemas: dict[str, dict] = {}
        for t in tools or []:
            fn = (t or {}).get("function") or {}
            if fn.get("name"):
                self.schemas[fn["name"]] = fn.get("parameters") or {}

    def __bool__(self) -> bool:
        return bool(self.schemas)

    def properties(self, name: str) -> dict:
        return (self.schemas.get(name) or {}).get("properties") or {}

    def find(self, name: str) -> str | None:
        """Точное имя или имя с префиксом сервера; при нескольких — самое короткое."""
        if name in self.schemas:
            return name
        hits = [n for n in self.schemas if any(n.endswith(sep + name) for sep in SEPARATORS)]
        return min(hits, key=len) if hits else None


class Oracle:
    def __init__(self, noop_text: str = "", parallel_calls: bool = True, final_text: str = "Готово.",
                 honor_k: bool = True, summary_tokens: int = 400, tokenizer: Tokenizer | None = None,
                 pad_profile: str = "targeted"):
        if pad_profile not in PAD_PROFILES:
            raise ValueError(f"pad_profile must be one of {PAD_PROFILES}")
        self.noop_text = noop_text
        self.parallel_calls = parallel_calls
        self.final_text = final_text
        self.honor_k = honor_k
        self.summary_tokens = int(summary_tokens)
        self.tok = tokenizer or HeuristicTokenizer()
        self.pad_profile = pad_profile
        self._exp: dict[str, Expectation] = {}
        self._calls: dict[str, tuple[str, int]] = {}   # id вызова → (input_id, шаг)
        self._seq = 0
        self._call_seq = 0
        self._lock = threading.RLock()   # RLock: respond() регистрирует ожидание из заголовка под тем же замком

    # ------------------------------------------------------------------ управление (реплеер)

    def register(self, payload: dict) -> Expectation:
        if not isinstance(payload, dict) or not payload.get("input_id"):
            raise ValueError("expect payload must contain input_id")
        expect = payload.get("expect")
        expect = {} if expect is None else expect
        if not isinstance(expect, dict):
            raise ValueError("expect must be an object")
        situation = payload.get("situation")
        kind = payload.get("kind") or self._infer_kind(expect, situation)
        if kind in ("user_cmd", "question"):
            kind = "user"
        with self._lock:
            self._seq += 1
            k = payload.get("k")
            exp = Expectation(input_id=str(payload["input_id"]), expect=expect, kind=kind,
                              text=payload.get("text"), situation=situation, t=payload.get("t"), seq=self._seq,
                              k=int(k) if isinstance(k, (int, float)) and not isinstance(k, bool) else None)
            exp.steps = self._plan(exp)
            if self.honor_k and exp.kind == "user" and exp.k is not None and exp.steps:
                exp.steps = (self._pad_to_k(exp.steps, exp.k) if self.pad_profile == "legacy"
                             else self._pad_targeted(exp, exp.steps, exp.k))
            self._exp[exp.input_id] = exp
            return exp

    @staticmethod
    def _infer_kind(expect: dict, situation: Any) -> str:
        typ = expect.get("type")
        if situation or typ in SITUATION_TYPES:
            return "situation"
        if typ in USER_TYPES or "calls" in expect or "answer" in expect or "steps" in expect:
            return "user"
        return "other"

    @staticmethod
    def _with_explicit(calls: list, explicit: list | None) -> list[dict]:
        """Вызовы generic-каталога; к каждому — его explicit-эквивалент из разметки дня (calls_explicit),
        если он задан и списки выровнены по длине."""
        out = [dict(c) for c in calls or [] if isinstance(c, dict)]
        if isinstance(explicit, list) and len(explicit) == len(out):
            for c, x in zip(out, explicit):
                if isinstance(x, dict) and x.get("name"):
                    c["explicit"] = dict(x)
        return out

    def _plan(self, exp: Expectation) -> list[list[dict]]:
        """Шаги ответа: список шагов, шаг — список вызовов {name, args, explicit?} (или маркер уведомления).

        Разметка daygen: команда — expect.calls (+ calls_explicit); вопрос — expect.calls = [answer(...)] или
        expect.answer; невалидная — expect.calls = [refuse(...)] или expect.reason; ситуация — expect.actions
        (+ actions_explicit) — действия, к которым добавляется уведомление пользователя.
        """
        e = exp.expect
        typ = e.get("type")
        if isinstance(e.get("steps"), list):
            return [[dict(c) for c in step] for step in e["steps"]]
        calls = self._with_explicit(e.get("calls"), e.get("calls_explicit"))
        if typ == "answer" or (typ is None and "answer" in e and not calls):
            if calls:
                return [calls]
            ans = e.get("answer") or {}
            args = {"value": ans.get("value")}
            if ans.get("unit") is not None:
                args["unit"] = ans.get("unit")
            return [[{"name": "answer", "args": args}]]
        if typ == "refuse":
            return [calls] if calls else [[{"name": "refuse", "args": {"reason": e.get("reason", "invalid_command")}}]]
        if exp.kind == "situation":
            actions = calls + self._with_explicit(e.get("actions"), e.get("actions_explicit"))
            return [actions + [{"name": "__notify__", "args": {}}]]
        if calls:
            return [calls] if self.parallel_calls else [[c] for c in calls]
        return []

    @staticmethod
    def _pad_to_k(steps: list[list[dict]], k: int) -> list[list[dict]]:
        """Довести число вызовов LLM на ход до k (VisionClaw: 1/2/3/5) шагами чтения состояния перед действием.

        Ход = шаги с инструментами + финал, поэтому шагов нужно k − 1. Шагов действий меньше убрать нельзя:
        при k = 1 для команды или вопроса (ответ — только инструментом) выходит 2 вызова (k_eff = max(k, 2)).
        Шаг-заглушка (__pad__) — чтение свойства устройства первого действия (read_property → explicit
        read_<dev>_<prop>), иначе list_devices / board_health; без подходящего инструмента он пропускается.
        """
        pads = max(0, (k - 1) - len(steps))
        if pads == 0:
            return steps
        target = next((c.get("args") or {} for st in steps for c in st if (c.get("args") or {}).get("device")), {})
        pad = {"name": "__pad__", "args": {k2: target[k2] for k2 in ("device", "property") if k2 in target}}
        return [[dict(pad)] for _ in range(pads)] + steps

    @staticmethod
    def _read_targets(exp: Expectation, steps: list[list[dict]]) -> list[dict]:
        """Что разумно читать перед действием/ответом: цели команды (expect.goal daygen), свойство вопроса о
        состоянии (answer.ref), свойства из аргументов действий; без повторов, в порядке появления."""
        e = exp.expect
        cands = list(e.get("goal") or [])
        ref = (e.get("answer") or {}).get("ref")
        if isinstance(ref, dict):
            cands.append(ref)
        cands += [c.get("args") or {} for st in steps for c in st]
        out, seen = [], set()
        for c in cands:
            if not isinstance(c, dict) or not c.get("device") or not c.get("property"):
                continue
            key = (str(c["device"]), str(c["property"]))
            if key not in seen:
                seen.add(key)
                out.append({"device": key[0], "property": key[1]})
        return out

    def _pad_targeted(self, exp: Expectation, steps: list[list[dict]], k: int) -> list[list[dict]]:
        """Добор до k вызовов (профиль targeted, по умолчанию): P = k − 1 − шагов действий шагов чтения перед
        действием. Состав: чтения целей (expect.goal / answer.ref / свойства действий); если целей меньше P —
        один list_devices (обзор) в начале, затем recent_events (журнал событий агента, если инструмент есть);
        недостающее — повтор чтений целей / recent_events. list_devices — не больше одного раза за ход
        (его результат — весь инвентарь, ≈ 700 токенов, и он остаётся в истории)."""
        pads = max(0, (k - 1) - len(steps))
        if pads == 0:
            return steps
        targets = self._read_targets(exp, steps)
        exp.targets = targets
        first_dev = targets[0] if targets else {}
        plan: list[dict] = []
        if pads > len(targets):
            plan.append({"kind": "list"})
        if pads - len(plan) > len(targets):
            plan.append({"kind": "events", **({"device": first_dev["device"]} if first_dev else {})})
        cycle = [{"kind": "read", **t} for t in targets] or [{"kind": "events"}]
        i = 0
        reads: list[dict] = []
        while len(plan) + len(reads) < pads:
            reads.append(dict(cycle[i % len(cycle)]))
            i += 1
        plan += reads
        return [[{"name": "__pad__", "args": p}] for p in plan] + steps

    def clear(self, input_id: str | None = None) -> int:
        with self._lock:
            if input_id is None:
                n = len(self._exp)
                self._exp.clear()
                self._calls.clear()
                return n
            return 1 if self._exp.pop(str(input_id), None) else 0

    def reset(self) -> None:
        """Полный сброс между прогонами: ожидания, id вызовов и счётчики — тела запросов прогонов A/A на одном
        экземпляре mock-llm совпадают байт в байт."""
        with self._lock:
            self._exp.clear()
            self._calls.clear()
            self._seq = 0
            self._call_seq = 0

    def expire(self, input_id: str) -> bool:
        with self._lock:
            exp = self._exp.get(str(input_id))
            if exp and exp.status in ("pending", "active"):
                exp.status = "expired"
                return True
            return False

    def state(self) -> dict:
        with self._lock:
            items = sorted(self._exp.values(), key=lambda e: e.seq)
            counts: dict[str, int] = {}
            for e in items:
                counts[e.status] = counts.get(e.status, 0) + 1
            return {"counts": counts, "expectations": [e.summary() for e in items[-50:]]}

    # ------------------------------------------------------------------ ответы

    def respond(self, body: dict, header_expect: dict | None = None, input_hint: str | None = None,
                wake_hint: str | None = None, purpose_hint: str | None = None) -> Decision:
        """Ответ на запрос. input_hint — id входа(ов) через запятую, wake_hint — причина пробуждения,
        purpose_hint — назначение запроса (turn | summary): заголовки X-Edgectx-* агента, если он их передаёт;
        точнее, чем сопоставление по тексту реплики."""
        messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
        if self.is_summary(body, messages, purpose_hint):
            return self._summary(messages)      # ожидания не трогаются ни при каком тексте запроса
        catalog = ToolCatalog(body.get("tools"))
        with self._lock:
            if messages and messages[-1].get("role") == "tool":
                return self._continue(messages, catalog, wake_hint)
            if header_expect:
                return self._start(self.register(header_expect), catalog)
            exp = self._match_hint(input_hint)
            hinted = any(x.strip() in self._exp for x in (input_hint or "").split(",") if x.strip())
            if exp is None and not hinted and (wake_hint or "user").lower() == "user":
                exp = self._match_user_turn(messages)
            if exp is not None:
                return self._start(exp, catalog)
            situations = sorted((e for e in self._exp.values() if e.kind == "situation" and e.status == "pending"),
                                key=lambda e: e.seq)
            if situations:
                return self._situations(situations, catalog)
            return Decision({"role": "assistant", "content": self.noop_text}, "stop", "noop")

    @staticmethod
    def is_summary(body: dict, messages: list[dict], purpose_hint: str | None) -> bool:
        """Запрос сводки: заголовок purpose = summary; без заголовка — запрос без инструментов, у которого в
        system есть инструкция сжатия («сводк»/«summar»)."""
        purpose = (purpose_hint or "").strip().lower()
        if purpose:
            return purpose == "summary"
        if body.get("tools"):
            return False
        system = " ".join(message_text(m.get("content"))[0] for m in messages
                          if m.get("role") in ("system", "developer")).casefold()
        return any(h in system for h in SUMMARY_HINTS)

    def _summary(self, messages: list[dict]) -> Decision:
        """Детерминированная сводка ровно summary_tokens токенов: факты расшифровки (строки «Вход:», «Вызов:»,
        «Агент:» edgectx и строки прежней сводки), самые свежие — в пределах длины, остаток — нейтральный
        текст. Так длина сводки в system ограничена и одинакова у всех прогонов (как summ_len в модели)."""
        users = [m for m in messages if m.get("role") == "user"]
        text = message_text(users[-1].get("content"))[0] if users else ""
        prev, sep, new = text.partition("Новые ходы:")
        if not sep:
            prev, new = "", text
        prev = prev.split("Прежняя сводка:", 1)[-1]
        lines = [ln.strip().lstrip("-•* ").strip() for ln in prev.splitlines()]
        lines = [ln for ln in lines if ln and ln != "(нет)"]
        fresh = [ln.strip() for ln in new.splitlines() if ln.strip().startswith(SUMMARY_LINE_PREFIXES)]
        if not fresh and not sep:
            fresh = [ln.strip() for ln in new.splitlines() if ln.strip()]
        facts: list[str] = []
        for ln in reversed(lines + fresh):          # без повторов, последнее упоминание побеждает
            if ln not in facts:
                facts.append(ln)
        kept, used = [], 0
        for ln in facts:                            # самые свежие — в пределах длины
            n = self.tok.count("- " + ln + "\n")
            if used + n > self.summary_tokens:
                break
            kept.append(ln)
            used += n
        content = "\n".join("- " + ln for ln in reversed(kept))
        content = fit_tokens(content, self.summary_tokens, self.tok)
        return Decision({"role": "assistant", "content": content}, "stop", "summary")

    def _match_hint(self, input_hint: str | None) -> Expectation | None:
        for iid in (x.strip() for x in (input_hint or "").split(",")):
            e = self._exp.get(iid)
            if e is not None and e.kind == "user" and e.status == "pending":
                return e
        return None

    def _match_user_turn(self, messages: list[dict]) -> Expectation | None:
        if not messages or messages[-1].get("role") != "user":
            return None
        text = normalize_text(message_text(messages[-1].get("content"))[0])
        pending = [e for e in self._exp.values() if e.kind == "user" and e.status == "pending"]
        by_text = [e for e in pending if e.text and normalize_text(e.text) in text]
        if by_text:
            return max(by_text, key=lambda e: e.seq)
        textless = sorted((e for e in pending if not e.text), key=lambda e: e.seq)
        return textless[0] if textless else None

    def _new_call_id(self, input_id: str, step: int) -> str:
        self._call_seq += 1
        cid = f"call_{self._call_seq:06d}"
        self._calls[cid] = (input_id, step)
        return cid

    def _start(self, exp: Expectation, catalog: ToolCatalog) -> Decision:
        exp.status = "active"
        if not exp.steps:
            return self._final(exp)
        return self._emit_step(exp, catalog, "user_start" if exp.kind == "user" else "situation")

    def _emit_step(self, exp: Expectation, catalog: ToolCatalog, kind: str) -> Decision:
        step = exp.step
        tool_calls, unresolved, texts = [], [], []
        if exp.steps[step] and exp.steps[step][0].get("name") == "__pad__":
            if self.pad_profile == "legacy":
                call = self._pad_call(exp.steps[step][0], catalog)
            else:
                call = self._pad_call_targeted(exp, exp.steps[step][0], catalog)
            if call is None:            # читать нечем — убрать заглушку(и), k_eff станет меньше
                if self.pad_profile == "legacy":
                    exp.steps = [st for st in exp.steps if not (st and st[0].get("name") == "__pad__")]
                else:
                    del exp.steps[step]
                if exp.step >= len(exp.steps):
                    return self._final(exp)
                return self._emit_step(exp, catalog, kind)
            name, args = call
            tool_calls.append({"id": self._new_call_id(exp.input_id, step), "type": "function",
                               "function": {"name": name, "arguments": canonical_json(args)}})
            exp.step += 1
            return Decision({"role": "assistant", "content": None, "tool_calls": tool_calls}, "tool_calls", kind,
                            [exp.input_id], step, unresolved)
        for spec in exp.steps[step]:
            if spec.get("name") == "__notify__":
                call = self._notify_call(exp, catalog)
                if call is None:
                    texts.append(self._notify_text(exp))
                    continue
                name, args = call
            else:
                name, args, ok = self._resolve(spec.get("name", ""), spec.get("args") or {}, catalog,
                                               spec.get("explicit"))
                if not ok:
                    unresolved.append(name)
            tool_calls.append({"id": self._new_call_id(exp.input_id, step), "type": "function",
                               "function": {"name": name, "arguments": canonical_json(args)}})
        exp.step += 1
        if not tool_calls:   # только текст (уведомление без инструмента) — ход завершён
            exp.status = "done"
            return Decision({"role": "assistant", "content": "\n".join(texts)}, "stop", kind, [exp.input_id],
                            step, unresolved)
        msg = {"role": "assistant", "content": "\n".join(texts) if texts else None, "tool_calls": tool_calls}
        return Decision(msg, "tool_calls", kind, [exp.input_id], step, unresolved)

    def _situations(self, situations: list[Expectation], catalog: ToolCatalog) -> Decision:
        """Одно пробуждение может покрыть несколько активных ситуаций: вызовы объединяются в один шаг."""
        tool_calls, texts, unresolved, ids = [], [], [], []
        for exp in situations:
            exp.status = "active"
            d = self._emit_step(exp, catalog, "situation")
            ids.append(exp.input_id)
            unresolved += d.unresolved
            if d.message.get("tool_calls"):
                tool_calls += d.message["tool_calls"]
            if d.message.get("content"):
                texts.append(d.message["content"])
        if tool_calls:
            msg = {"role": "assistant", "content": "\n".join(texts) if texts else None, "tool_calls": tool_calls}
            return Decision(msg, "tool_calls", "situation", ids, 0, unresolved)
        return Decision({"role": "assistant", "content": "\n".join(texts)}, "stop", "situation", ids, 0, unresolved)

    def _continue(self, messages: list[dict], catalog: ToolCatalog, wake_hint: str | None = None) -> Decision:
        trailing = []
        for m in reversed(messages):
            if m.get("role") != "tool":
                break
            trailing.append(m)
        ids, exps = [], []
        for m in reversed(trailing):
            ref = self._calls.get(str(m.get("tool_call_id")))
            if ref and ref[0] in self._exp and ref[0] not in ids:
                ids.append(ref[0])
                exps.append(self._exp[ref[0]])
        if not exps:
            # Вызовы не наши (ожидание сброшено, агент вызвал инструмент сам). В фоновом пробуждении — молчать:
            # агент мог бы переслать «Готово.» пользователю как уведомление.
            background = (wake_hint or "user").lower() != "user"
            return Decision({"role": "assistant", "content": self.noop_text if background else self.final_text},
                            "stop", "continue_unknown")
        denied = [self._denied_reason(m) for m in trailing]
        denied = [d for d in denied if d is not None]
        if denied:
            for e in exps:
                e.status = "done"
            text = "Команда отклонена: " + "; ".join(denied)
            return Decision({"role": "assistant", "content": text}, "stop", "denied_final", ids)
        if len(exps) == 1 and exps[0].step < len(exps[0].steps) and exps[0].status == "active":
            return self._emit_step(exps[0], catalog, "continue")
        # финал (для нескольких ситуаций — один общий ответ)
        finals = [self._final(e) for e in exps]
        content = "\n".join(d.message.get("content") or "" for d in finals if d.message.get("content"))
        return Decision({"role": "assistant", "content": content}, "stop", "final", ids)

    def _final(self, exp: Expectation) -> Decision:
        exp.status = "done"
        typ = exp.expect.get("type")
        if exp.kind == "situation":
            content = ""          # уведомление уже ушло инструментом — больше ничего не говорить
        elif typ == "answer" or (typ is None and "answer" in exp.expect):
            ans = exp.expect.get("answer") or {}
            content = f"{ans.get('value')} {ans.get('unit') or ''}".strip()
        elif typ == "refuse":
            content = "Не могу выполнить эту команду."
        elif typ in ("none", "noop"):
            content = self.noop_text
        else:
            content = self.final_text
        return Decision({"role": "assistant", "content": content}, "stop", "final", [exp.input_id])

    @staticmethod
    def _denied_reason(msg: dict) -> str | None:
        text = message_text(msg.get("content"))[0]
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return None
        if isinstance(obj, dict) and obj.get("denied") is True:
            return str(obj.get("reason", "denied"))
        return None

    # ------------------------------------------------------------------ разрешение имён

    def _resolve(self, name: str, args: dict, catalog: ToolCatalog,
                 explicit_spec: dict | None = None) -> tuple[str, dict, bool]:
        if not catalog:
            return name, args, True
        found = catalog.find(name)
        if found:
            return found, self._adapt_args(name, args, catalog.properties(found)), True
        if explicit_spec:                       # эквивалент из разметки дня (daygen: calls_explicit)
            found = catalog.find(explicit_spec["name"])
            if found:
                return found, dict(explicit_spec.get("args") or {}), True
        explicit = self._explicit_name(name, args)
        for cand in explicit:
            found = catalog.find(cand)
            if found:
                props = catalog.properties(found)
                if name == "write_property":
                    key = next(iter(props)) if len(props) == 1 else "value"
                    return found, {key: args.get("value")}, True
                if name == "invoke_action":
                    return found, dict(args.get("args") or {}), True
                return found, {}, True
        return name, args, False

    @staticmethod
    def _explicit_name(name: str, args: dict) -> list[str]:
        dev, prop = _sanitize(args.get("device", "")), _sanitize(args.get("property", ""))
        if name == "write_property":
            return [f"set_{dev}_{prop}"]
        if name == "read_property":
            return [f"read_{dev}_{prop}", f"get_{dev}_{prop}"]
        if name == "invoke_action":
            act = _sanitize(args.get("action", ""))
            return [f"{act}_{dev}", f"invoke_{dev}_{act}", f"{dev}_{act}"]
        return []

    @staticmethod
    def _adapt_args(name: str, args: dict, props: dict) -> dict:
        if name == "refuse" and props and "reason" in args and "reason" not in props:
            key = next((k for k in REFUSE_ARG_NAMES if k in props), None)
            if key:
                return {key: args["reason"], **{k: v for k, v in args.items() if k != "reason"}}
        return args

    def _pad_call(self, spec: dict, catalog: ToolCatalog) -> tuple[str, dict] | None:
        args = spec.get("args") or {}
        if args.get("device") and args.get("property"):
            name, a, ok = self._resolve("read_property", dict(args), catalog)
            if ok and catalog:
                return name, a
        if args.get("device"):
            found = catalog.find("read_property")
            props = catalog.properties(found) if found else {}
            if found and "property" not in props:
                return found, {"device": args["device"]}
        for n in ("list_devices", "board_health"):
            found = catalog.find(n)
            if found:
                return found, {}
        return None

    def _pad_call_targeted(self, exp: Expectation, spec: dict, catalog: ToolCatalog) -> tuple[str, dict] | None:
        """Заглушка профиля targeted → вызов инструмента каталога. Нет нужного инструмента — замена по цепочке
        (чтение цели → recent_events → list_devices, не больше одного раза за ход), чтобы число вызовов
        оставалось k; None — читать нечем вовсе (k_eff станет меньше)."""
        a = spec.get("args") or {}
        target = {k2: a[k2] for k2 in ("device", "property") if a.get(k2)}
        if not target.get("property") and exp.targets:     # уточнить свойство по целям хода
            same = [t for t in exp.targets if t["device"] == target.get("device")] or exp.targets
            target = dict(same[0])
        order = {"read": ("read", "events", "list"), "events": ("events", "read", "list"),
                 "list": ("list", "events", "read")}.get(a.get("kind"), ("read", "events", "list"))
        for kind in order:
            if kind == "read" and catalog and target.get("device") and target.get("property"):
                name, args, ok = self._resolve("read_property", dict(target), catalog)
                if ok:
                    return name, args
            elif kind == "events":
                found = catalog.find("recent_events")
                if found:
                    props = catalog.properties(found)
                    return found, {k2: v for k2, v in target.items() if k2 in props}
            elif kind == "list" and not exp.listed:
                for n in ("list_devices", "board_health"):
                    found = catalog.find(n)
                    if found:
                        exp.listed = True
                        return found, {}
        return None

    def _notify_call(self, exp: Expectation, catalog: ToolCatalog) -> tuple[str, dict] | None:
        for n in NOTIFY_TOOL_NAMES:
            found = catalog.find(n)
            if not found:
                continue
            props = catalog.properties(found)
            key = next((k for k in TEXT_ARG_NAMES if k in props), None)
            if key is None:
                key = next((k for k, p in props.items() if isinstance(p, dict) and p.get("type") == "string"), "text")
            args = {key: self._notify_text(exp)}
            if "situation_id" in props:
                args["situation_id"] = exp.input_id
            return found, args
        return None

    @staticmethod
    def _notify_text(exp: Expectation) -> str:
        name = (exp.situation or {}).get("name") or exp.expect.get("name") or "significant_event"
        return f"Внимание: {name} [sit:{exp.input_id}]"

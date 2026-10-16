"""Классы нарушений и отчёт монитора.

Нарушение (violation) — трасса противоречит контракту (IMPLEMENTATION_SPEC §6) или решения шлюза небезопасны;
любое нарушение делает отчёт неуспешным. Предупреждение (warning) — подозрительно, но допустимо контрактом
(например, шаг стенных часов назад при синхронизации NTP); на успех не влияет.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

SCHEMA = "monitor.v2"

VIOLATIONS: dict[str, str] = {
    # --- разбор и поля
    "parse": "строка — не JSON-объект (синтаксис, повтор ключа, NaN/Infinity, переполнение double, не UTF-8)",
    "event.unknown_type": "неизвестный тип записи",
    "event.field": "обязательное поле отсутствует или имеет неверный тип/значение",
    # --- рамка прогона
    "frame.no_run_start": "первая запись — не run_start (или run_start нет)",
    "frame.duplicate_run_start": "повторный run_start",
    "frame.no_run_end": "трасса не закончена run_end",
    "frame.after_run_end": "записи после run_end",
    "frame.run_id": "run_id записи отличается от run_id в run_start",
    "order.seq": "seq не растёт ровно на 1 (потеря, повтор или перестановка строк)",
    "time.clock_backwards": "ts_clock убывает (часы агента идут назад)",
    # --- обращения к LLM
    "llm.duplicate_req_id": "req_id повторяется",
    "llm.missing_response": "у llm_request нет llm_response (или error с этим req_id) до конца трассы",
    "llm.orphan_response": "llm_response с req_id, которого нет ни у одного llm_request",
    "llm.duplicate_response": "второй llm_response на один req_id",
    "llm.response_before_request": "llm_response записан раньше своего llm_request",
    "llm.finish_mismatch": "finish_reason=tool_calls при n_tool_calls=0",
    "llm.inflight": "одновременно выполняется больше запросов к LLM, чем max_in_flight",
    "llm.truncated_calls": "исполнены вызовы из ответа, обрезанного по длине (finish_reason=length)",
    "usage.inconsistent": "cached_tokens больше prompt_tokens",
    "usage.implausible": "usage невозможен: prompt_tokens = 0 при непустом запросе или completion_tokens = 0 при вызовах",
    # --- исходы входов (каждый вход: ровно один suppress или участие в одном допущенном wake)
    "input.no_outcome": "вход не подавлен и не участвовал ни в одном допущенном пробуждении (потерян)",
    "input.multi_outcome": "вход и подавлен, и разбудил LLM (или подавлен дважды)",
    "wake.duplicate_input": "один вход участвует в нескольких допущенных пробуждениях (лишние вызовы LLM)",
    "wake.no_request": "допущенное пробуждение (wake_id) без запроса к LLM",
    # --- пробуждения
    "wake.missing": "llm_request без предшествующего wake (или ссылка на неизвестный turn_id)",
    "wake.suppressed_flag": "запрос к LLM в ходе, пробуждение которого отклонено (wake с suppressed=true)",
    "wake.reason_mismatch": "причина wake не соответствует виду входа (user↔user, heartbeat↔tick, gate↔sensor|situation)",
    "ref.dangling": "input_id ссылается на вход, которого нет (или который записан позже)",
    # --- инструменты
    "tool.duplicate_call_id": "call_id повторяется",
    "tool.missing_result": "у tool_call нет tool_result (или error с этим call_id) до конца трассы",
    "tool.orphan_result": "tool_result с call_id, которого нет ни у одного tool_call",
    "tool.duplicate_result": "второй tool_result на один call_id",
    "tool.result_before_call": "tool_result записан раньше своего tool_call",
    "tool.result_late": "результат инструмента записан после следующего запроса к LLM того же хода",
    "tool.unsourced": "tool_call не порождён ни ответом LLM с вызовами, ни сработавшим правилом",
    "tool.count_mismatch": "число tool_call не совпадает с n_tool_calls ответа LLM",
    "tool.ok_and_denied": "tool_result одновременно ok=true и denied=true",
    # --- пользователь и уведомления
    "user.unanswered": "вход пользователя без user_reply до конца трассы",
    "user.duplicate_reply": "второй user_reply на один вход",
    "user.reply_to_non_user": "user_reply ссылается на вход не от пользователя",
    "notify.unsourced": "notify без предшествующего wake или rule_fired",
    # --- бюджеты
    "budget.max_steps": "в ходе больше запросов к LLM, чем max_steps",
    "budget.daily_calls": "фоновый ход начат после исчерпания суточного лимита вызовов",
    "budget.daily_input_tokens": "фоновый ход начат после исчерпания суточного лимита входных токенов",
    "budget.degraded": "ход heartbeat/фона начат в деградированном режиме (израсходовано ≥ degrade_at лимита)",
    # --- аргументы инструментов
    "args.unknown_tool": "инструмента (или сервера) нет в каталоге",
    "args.invalid_json": "строка args — не корректный JSON",
    "args.duplicate_key": "в args повторяется ключ",
    "args.not_object": "args — не объект",
    "args.type": "аргумент неверного типа",
    "args.required": "нет обязательного аргумента",
    "args.enum": "значение вне enum",
    "args.const": "значение не равно const",
    "args.additional": "аргумент не описан в схеме при additionalProperties: false",
    "args.bounds": "число вне minimum/maximum/exclusive*",
    "args.length": "длина строки вне minLength/maxLength",
    "args.count": "число элементов/свойств вне min/max",
    "args.unique": "повтор элементов при uniqueItems",
    "args.combinator": "не выполнено anyOf/oneOf",
    "args.false_schema": "значение там, где схема — false",
    "catalog.invalid": "каталог инструментов некорректен (схема вне поддерживаемого подмножества, повтор имени)",
    # --- журнал шлюза hw-mcp
    "gate.config_missing": "есть журнал шлюза, но нет политики (записи gate_config или --gate-config): перепроверка невозможна",
    "gate.config_invalid": "политика шлюза некорректна",
    "gate.config_mismatch": "политика в журнале отличается от --gate-config",
    # --- класс actuator_safety: исполненные команды, которые шлюз должен был остановить
    "actuator_safety.unknown_target": "исполнена команда устройству/инструменту вне реестра",
    "actuator_safety.out_of_range": "исполнены аргументы вне схемы (диапазон, тип, enum)",
    "actuator_safety.stale_command": "исполнено устаревшее решение LLM (возраст больше TTL)",
    "actuator_safety.stale_state": "исполнено без свежего значения состояния, от которого зависит блокировка",
    "actuator_safety.interlock": "исполнено при нарушенной блокировке",
    "actuator_safety.rate_limit": "превышена частота команд приводу",
    "actuator_safety.unconfirmed": "исполнено без подтверждения пользователя",
    "actuator_safety.duplicate_exec": "команда с тем же cmd_id исполнена повторно",
    "actuator_safety.executed_after_deny": "исполнено при вердикте reject/confirm/duplicate",
    "actuator_safety.no_lease": "привод с арендой исполнил команду без аренды",
    "actuator_safety.lease_too_long": "аренда выдана дольше lease_s",
    "actuator_safety.renew_without_lease": "продление аренды принято без действующей аренды",
    "actuator_safety.watchdog_missed": "аренда истекла, а привод не переведён в безопасное состояние",
    "actuator_safety.unchecked_call": "команда приводу исполнена (по трассе), но решения шлюза о ней нет",
    "actuator_safety.result_mismatch": "трасса агента и шлюз расходятся в том, исполнена ли команда",
    "actuator_safety.args_mismatch": "шлюз исполнил не те аргументы/цель, что агент отправил по трассе",
    "actuator_safety.pulse_overrun": "импульс не завершён к сроку (выход остался включённым дольше заданного)",
    "actuator_safety.safe_state_failed": "перевод в безопасное состояние не удался (ошибка записи)",
}

WARNINGS: dict[str, str] = {
    "time.wall_backwards": "ts_wall убывает (шаг стенных часов, например NTP)",
    "time.latency_inconsistent": "latency_ms больше интервала между записями по ts_wall",
    "input.duplicate_id": "повтор id входа",
    "wake.user_suppressed": "вход пользователя подавлен (пользователь обслуживается всегда)",
    "llm.segments_mismatch": "сумма segments_tokens_est заметно отличается от prompt_tokens_est",
    "llm.usage_mismatch": "usage.prompt_tokens заметно отличается от оценки prompt_tokens_est",
    "llm.no_usage": "провайдер не вернул usage (has_usage=false): токены прогона неполны",
    "llm.tool_calls_without_tools": "ответ на запрос без инструментов (n_tools=0 или служебный) объявил вызовы",
    "errors.frequent": "доля записей error одного источника выше порога (подсистема работает вслепую)",
    "gate.clock_backwards": "ts_clock журнала шлюза убывает между разными писателями (не между решениями)",
    "gate.validators_disagree": "argschema и jsonschema расходятся в проверке аргументов команды",
    "gate.unknown_record": "неизвестный тип записи в журнале шлюза (пропущена)",
    "gate.unmatched_decision": "решение шлюза не сопоставлено ни с одним tool_call трассы",
    "gate.false_reject": "шлюз отказал, хотя по политике команда допустима",
    "gate.reason_mismatch": "шлюз отказал по другой причине, чем следует из политики",
}

UNSAFE_PREFIX = "actuator_safety."


@dataclass(frozen=True)
class Finding:
    cls: str
    source: str           # trace | hw_log | tools | gate_config
    line: int | None      # номер строки (с 1) в своём файле
    message: str
    ref: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"class": self.cls, "source": self.source, "line": self.line, "message": self.message}
        if self.ref:
            d["ref"] = self.ref
        return d


@dataclass
class Report:
    inputs: dict = field(default_factory=dict)
    violations: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)      # проверки, которые не выполнялись, и почему
    assumptions: dict = field(default_factory=dict)       # значения, принятые по умолчанию
    unsafe_events: set = field(default_factory=set)       # ключи событий для N_unsafe (без двойного счёта)

    def add(self, cls: str, source: str, line: int | None, message: str, unsafe_key=None, count_unsafe=True,
            **ref) -> None:
        """Добавить находку. Нарушения класса actuator_safety.* входят в N_unsafe по ключу unsafe_key (одна
        команда — один ключ, сколько бы проверок она ни нарушила); count_unsafe=False — не входят."""
        finding = Finding(cls, source, line, message, {k: v for k, v in ref.items() if v is not None})
        if cls in VIOLATIONS:
            self.violations.append(finding)
            if cls.startswith(UNSAFE_PREFIX) and count_unsafe:
                self.unsafe_events.add(unsafe_key if unsafe_key is not None else (source, line, cls))
        elif cls in WARNINGS:
            self.warnings.append(finding)
        else:  # ошибка программирования монитора, а не трассы
            raise KeyError(f"неизвестный класс нарушения {cls!r}")

    def skip(self, what: str) -> None:
        if what not in self.skipped:
            self.skipped.append(what)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def n_unsafe(self) -> int:
        """Число исполненных небезопасных команд (одна команда с несколькими нарушениями считается один раз)."""
        return len(self.unsafe_events)

    def by_class(self) -> dict[str, int]:
        return dict(sorted(Counter(v.cls for v in self.violations).items()))

    def warnings_by_class(self) -> dict[str, int]:
        return dict(sorted(Counter(w.cls for w in self.warnings).items()))

    def to_dict(self, max_findings: int | None = None) -> dict:
        def cut(items):
            items = sorted(items, key=lambda f: (f.source, f.line if f.line is not None else 0, f.cls))
            return [f.to_dict() for f in (items if max_findings is None else items[:max_findings])]
        return {
            "schema": SCHEMA,
            "ok": self.ok,
            "inputs": self.inputs,
            "n_violations": len(self.violations),
            "n_warnings": len(self.warnings),
            "n_unsafe": self.n_unsafe,
            "by_class": self.by_class(),
            "warnings_by_class": self.warnings_by_class(),
            "summary": self.summary,
            "skipped": self.skipped,
            "assumptions": self.assumptions,
            "violations": cut(self.violations),
            "warnings": cut(self.warnings),
        }

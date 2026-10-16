"""Оценки для концепции «системная глубина» (НЕ измерения, кроме помеченных [изм.]).

1) Бюджет API на экспериментальную часть ВКР по ценам RouterAI (снимок F_artifacts, 06.10.2026).
2) Модель трафика одного вызова LLM и суток: тело запроса, ответ (обычный и SSE-поток),
   TLS-рукопожатия (полное и возобновлённое — [изм.] openssl s_client к routerai.ru 06.10.2026).

Запуск: python3 budget_traffic.py
"""
from __future__ import annotations

# ₽ за 1M токенов: вход, чтение кэша, выход (RouterAI, F_artifacts/price_table_output_2026-10-06.txt; gpt-6-luna — GAP1 §8.6)
P = {
    "gpt-4.1-nano": (11.04, 2.76, 44.16),
    "gpt-4o-mini": (16.56, 8.28, 66.25),
    "gpt-5-nano": (5.52, 0.55, 44.16),
    "gpt-5-mini": (27.60, 2.76, 220.82),
    "gpt-5.4-nano": (23.19, 2.32, 144.91),
    "gpt-6-luna": (11.04, 1.10, 55.21),
}
REASONING = {"gpt-5-nano": 300, "gpt-5-mini": 300, "gpt-5.4-nano": 300, "gpt-6-luna": 300}  # допущение: скрытые токены на вызов


def rub(model: str, tin: float, tout: float, cached_share: float = 0.0) -> float:
    pin, pc, pout = P[model]
    return (tin * (1 - cached_share) * pin + tin * cached_share * pc + (tout + REASONING.get(model, 0)) * pout) / 1e6


def budget() -> None:
    rows = []
    # (а) калибровка TTL: GAP1 §6.3 — ≈6/9/27/3 ₽ на модель×провайдер; 3 модели × 2 провайдера × 4 повтора
    calib = (6 + 9 + 3) * 2 * 4
    rows.append(("C: калибровка TTL кэша (gpt-4o-mini, gpt-5-mini, gpt-6-luna × OpenAI/Azure × 4 раза)", calib))
    # (б) качество tool calling: 400 задач BFCL-подмножества + 440 MASSIVE-Agents ru/en iot = 840 задач × 6 моделей
    q = sum(840 * rub(m, 1500, 100) for m in P)
    rows.append(("Q: качество вызова инструментов, 840 задач × 6 моделей (вход 1500, выход 100 ток.)", q))
    # (в) T1: TTL-aware сжатые сутки. GAP1 §6.2: «M + датчики + heartbeat» на gpt-5-mini = 8,71 ₽/сут (модель).
    #     Неоптимизированные конфигурации дороже; берём среднее 20 ₽/сут; 10 конфигураций × 10 суток.
    t1 = 10 * 10 * 20 + 10 * 5 * 2.53 * 3  # + gpt-6-luna (2,53 ₽/сут ×3 на неопт.) по 5 суток
    rows.append(("T1: 10 конфигураций × 10 суток (gpt-5-mini) + 5 суток gpt-6-luna", t1))
    # (г) T2: реальное время 8 ч, 5 прогонов на систему.
    own = 9.0        # свой агент, ≈ GAP1 сценарий M+датчики+hb на gpt-5-mini
    pico = 3 * own   # допущение: политика контекста PicoClaw без prompt_cache_key через RouterAI
    nano = 3 * own   # допущение
    # OpenClaw: ~28k входных токенов на запрос (Hua et al.), ~100 вызовов/сут → 2/3 дня за 8 ч, + heartbeat 16×27k
    openclaw = (100 * 2 / 3) * rub("gpt-5-mini", 28000, 500) + 16 * rub("gpt-5-mini", 27000, 20)
    t2 = 5 * (2 * own + pico + nano + openclaw)
    rows.append((f"T2: 5 прогонов × (свой ×2 конф., PicoClaw, nanobot, OpenClaw≈{openclaw:.0f} ₽)", t2))
    # (д) сетевые микроэксперименты: 3000 коротких вызовов × 1000 ток. вход / 50 выход на gpt-4.1-nano
    net = 3000 * rub("gpt-4.1-nano", 1000, 50)
    rows.append(("N: сетевые микроэксперименты (стрим/без, keep-alive, TLS), 3000 вызовов gpt-4.1-nano", net))
    total = sum(r[1] for r in rows)
    for name, v in rows:
        print(f"{v:9.0f} ₽  {name}")
    print(f"{total:9.0f} ₽  ИТОГО; с резервом ×1,5: {total*1.5:.0f} ₽")


# ---------------- трафик ----------------
TLS_FULL = 4606 + 381      # [изм.] байт: прочитано + записано, TLS 1.3, X25519, без PQ-гибрида (06.10.2026)
TLS_FULL_PQ = 4606 + 1624  # [изм.] то же при ClientHello с X25519MLKEM768 (OpenSSL 3.6.4 по умолчанию)
TLS_RESUMED = 238 + 652    # [изм.] возобновление по билету, X25519
TCP_IP = 3 * 66 + 4 * 66   # оценка: SYN/SYN-ACK/ACK + закрытие, ~66 байт на сегмент с заголовками
CHUNK_BYTES = 290          # оценка: один SSE-чанк chat.completion.chunk с id, model, system_fingerprint, obfuscation
ENVELOPE = 700             # оценка: обёртка нестримингового ответа (id, usage, finish_reason...)


def call_bytes(tin: int, tout: int, bpt_in: float = 4.5, stream: bool = False, tls: str = "reuse") -> tuple[int, int]:
    up = int(tin * bpt_in) + 600  # тело запроса + заголовки HTTP/2 (оценка)
    down = (tout * CHUNK_BYTES + 300) if stream else int(tout * 4.0) + ENVELOPE
    if tls == "full":
        up += 381 + TCP_IP // 2
        down += 4606 + TCP_IP // 2
    elif tls == "resumed":
        up += 652 + TCP_IP // 2
        down += 238 + TCP_IP // 2
    return up, down


def traffic() -> None:
    print("\nТрафик одного вызова (вход 6000 ток. × 4,5 Б/ток., выход 150 ток.)")
    for stream in (False, True):
        for tls in ("reuse", "resumed", "full"):
            u, d = call_bytes(6000, 150, stream=stream, tls=tls)
            print(f"  stream={stream!s:5} tls={tls:8}  вверх {u/1024:6.1f} КиБ  вниз {d/1024:6.1f} КиБ  сумма {(u+d)/1024:6.1f} КиБ")
    print("\nСутки: 100 вызовов, паузы > IdleConnTimeout → каждое соединение новое")
    for label, stream, tls in (("поток SSE, полный TLS (как DefaultTransport Go)", True, "full"),
                               ("без потока, полный TLS", False, "full"),
                               ("без потока, возобновление TLS", False, "resumed")):
        u = d = 0
        for _ in range(100):
            a, b = call_bytes(6000, 150, stream=stream, tls=tls)
            u += a; d += b
        print(f"  {label:38} вверх {u/2**20:5.2f} МиБ  вниз {d/2**20:5.2f} МиБ")
    print(f"\nTLS: полное {TLS_FULL} Б (с PQ-гибридом {TLS_FULL_PQ} Б), возобновлённое {TLS_RESUMED} Б → в {TLS_FULL/TLS_RESUMED:.1f} раза меньше")


if __name__ == "__main__":
    budget()
    traffic()

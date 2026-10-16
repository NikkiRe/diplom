"""Сводка прогона и сводная таблица на синтетическом каталоге прогона."""
import json

from bench.baselines.report import rows, table
from bench.baselines.summary import summarize, write_summary


def _w(p, rows_):
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows_), encoding="utf-8")


def test_summary_phases_sources_and_report(tmp_path):
    rd = tmp_path / "r1"
    rd.mkdir()
    man = {"run_id": "r1", "system": "x", "profile": "p", "k": 60, "host": {"executor": "mac"}, "wall0": 1000.0,
           "wall_end": 1120.0, "t0": 25200, "t_end": 32400, "push": {"mode": "signals"},
           "day": {"header": {"home": "hh1", "seed": 1, "profile": "H", "counts": {"user_cmd": 1}}},
           "deviations": ["d1"], "errors": [], "stats": {}}
    (rd / "manifest.json").write_text(json.dumps(man))
    u = {"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 512}}
    _w(rd / "llmtap.jsonl", [
        {"seq": 1, "ts_wall": 999.0, "cls": "heartbeat", "mock_id": "chatcmpl-mock-000001", "status": 200,
         "n_tools": 5, "bytes": {"request_total": 100, "response_total": 50}},
        {"seq": 2, "ts_wall": 1010.0, "cls": "user", "mock_id": "chatcmpl-mock-000002", "status": 200,
         "n_tools": 5, "bytes": {"request_total": 200, "response_total": 60}},
        {"seq": 3, "ts_wall": 1011.0, "cls": "internal:summary", "mock_id": "chatcmpl-mock-000003", "status": 200,
         "n_tools": 0, "bytes": {"request_total": 300, "response_total": 70},
         "usage": {"prompt_tokens": 50, "completion_tokens": 20}, "usage_adjusted": {"completion_before": 400,
                                                                                   "completion_after": 20}},
        {"seq": 4, "ts_wall": 1130.0, "cls": "heartbeat", "mock_id": "chatcmpl-mock-000004", "status": 200,
         "n_tools": 5, "bytes": {"request_total": 100, "response_total": 50}},
    ])
    _w(rd / "mockllm_journal.jsonl", [
        {"type": "llm", "req_id": f"mock-{i:06d}", "usage": dict(u, completion_tokens=400 if i == 3 else 10),
         "oracle": {"decision": "noop"}, "estimate": {"segments": {"tools": 100}}} for i in (1, 2, 3, 4)])
    _w(rd / "play.jsonl", [{"type": "user", "ok": True, "rtt_wall_ms": 50, "lag_day_s": 1.0},
                           {"type": "push", "ok": True}])
    _w(rd / "procmon.jsonl", [{"type": "sample", "ts_wall": 1000 + i, "rss_kib": 1024 * (10 + i),
                               "root_rss_kib": 1024 * 8, "cpu_s": 0.1 * i} for i in range(5)])
    s = summarize(rd)
    write_summary(rd, s)
    ph = s["llm"]["by_phase"]
    assert ph["startup"]["calls"] == 1 and ph["day"]["calls"] == 2 and ph["after"]["calls"] == 1
    assert s["llm"]["day_by_source"]["user"]["cached_tokens"] == 512
    assert s["llm"]["day_by_source"]["internal:summary"]["completion_tokens"] == 20      # укороченная сводка
    assert s["llm"]["summaries_fitted"] == {"n": 1, "completion_tokens_removed": 380}
    assert s["llm"]["day_per_hour"]["calls"] == 1.0
    assert s["resources"]["day"]["rss_kib"]["max"] == 1024 * 14
    assert s["metric_validity"]["latency_valid_for_t2"] is False
    assert s["resources"]["validity"]["cpu_daily_valid_for_t2"] is False
    assert s["resources"]["validity"]["memory_slope_valid_for_t2"] is False
    assert "невалидны для T2" in (rd / "summary.md").read_text()
    assert (rd / "summary.md").read_text().count("| day") >= 2
    rs = rows(tmp_path)
    assert rs[0]["calls"] == 2 and rs[0]["by_source"] == {"internal:summary": 1, "user": 1}
    assert "r1" in table(rs)

"""mock-llm: reasoning-токены по уровню reasoning_effort запроса (O1 R7). Без таблицы — прежнее поведение."""
import pytest

from bench.mockllm.__main__ import build_parser, config_from_args
from bench.mockllm.server import MockLLM, ServerConfig, parse_effort_map, request_effort

TABLE = {"none": 0, "minimal": 0, "low": 100, "medium": 400}


def usage(app, **fields):
    body = {"model": "m", "messages": [{"role": "user", "content": "[heartbeat]"}], **fields}
    status, resp, extra = app.chat(body, {}, 1)
    assert status == 200
    return resp["usage"], extra


def test_constant_without_table():
    app = MockLLM(ServerConfig(reasoning_tokens=7))
    for fields in ({}, {"reasoning_effort": "low"}):
        u, extra = usage(app, **fields)
        assert u["completion_tokens_details"]["reasoning_tokens"] == 7
        assert "reasoning" not in extra          # журнал прежний


def test_tokens_by_effort():
    app = MockLLM(ServerConfig(reasoning_by_effort=TABLE))
    base = usage(MockLLM(ServerConfig()))[0]["completion_tokens"]
    for fields, want in (({"reasoning_effort": "low"}, 100), ({"reasoning": {"effort": "none"}}, 0),
                         ({}, 400),                       # без уровня — умолчание провайдера (medium)
                         ({"reasoning_effort": "high"}, 0)):  # уровня нет в таблице — reasoning_tokens (0)
        u, extra = usage(app, **fields)
        assert u["completion_tokens_details"]["reasoning_tokens"] == want, fields
        assert u["completion_tokens"] == base + want
        assert extra["reasoning"]["tokens"] == want
    assert app.state()["reasoning"]["by_effort"] == TABLE


def test_parse_and_validate():
    assert parse_effort_map(" low=100, medium=400 ") == {"low": 100, "medium": 400}
    assert parse_effort_map("") == {}
    assert request_effort({"reasoning_effort": 3}) is None
    for bad in ("low", "low=x"):
        with pytest.raises(ValueError):
            parse_effort_map(bad)
    for cfg in (ServerConfig(reasoning_by_effort={"turbo": 1}), ServerConfig(reasoning_by_effort={"low": -1}),
                ServerConfig(reasoning_default_effort="max")):
        with pytest.raises(ValueError):
            cfg.validate()


def test_cli():
    a = build_parser().parse_args(["--reasoning-by-effort", "minimal=0,low=100", "--reasoning-default-effort", "low"])
    cfg = config_from_args(a)
    assert cfg.reasoning_by_effort == {"minimal": 0, "low": 100} and cfg.reasoning_default_effort == "low"
    assert config_from_args(build_parser().parse_args([])).reasoning_by_effort == {}

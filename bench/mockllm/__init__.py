"""mock-llm — OpenAI-совместимый сервер Chat Completions для стенда EdgeCtx (спецификация §5, §5.1).

    from bench.mockllm import ServerConfig, RunningServer
    with RunningServer(ServerConfig(mode="oracle", port=0)) as s:
        ...  # s.url, s.app.oracle, s.app.journal

CLI: python -m bench.mockllm --help
"""
from .cache import PrefixCache, common_prefix_len
from .latency import LatencyModel
from .oracle import Oracle
from .recording import RecordStore, request_key
from .server import VERSION, MockLLM, RunningServer, ServerConfig, make_server
from .tokens import account_request, count_tools, load_tokenizer

__all__ = ["PrefixCache", "common_prefix_len", "LatencyModel", "Oracle", "RecordStore", "request_key", "VERSION",
           "MockLLM", "RunningServer", "ServerConfig", "make_server", "account_request", "count_tools",
           "load_tokenizer"]

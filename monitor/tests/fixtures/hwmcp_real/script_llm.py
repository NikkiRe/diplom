"""Сценарная заглушка LLM для снятия контрактной фикстуры edgectx + hw-mcp (не mock-llm стенда).

Реплика пользователя вида ``CMD:[{"name": …, "arguments": {…}}, …]`` превращается в эти tool_calls; после
результатов инструментов — ответ "ok" без вызовов. usage.prompt_tokens — грубая оценка (байты тела / 5),
чтобы не порождать предупреждений правдоподобия usage.

    python script_llm.py PORT
"""
import http.server
import itertools
import json
import sys

_ids = itertools.count(1)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        msgs = body.get("messages", [])
        last = msgs[-1] if msgs else {}
        msg, finish = {"role": "assistant", "content": "ok"}, "stop"
        if last.get("role") == "user" and body.get("tools"):
            text = last.get("content")
            text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
            pos = text.find("CMD:")
            if pos >= 0:
                calls, _ = json.JSONDecoder().raw_decode(text[pos + 4:])
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call_{next(_ids)}", "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                    for c in calls]}
                finish = "tool_calls"
        pt = max(1, len(raw) // 5)
        resp = {"id": "script", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
                "usage": {"prompt_tokens": pt, "completion_tokens": 12, "total_tokens": pt + 12}}
        out = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()

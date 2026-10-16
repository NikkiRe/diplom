#!/usr/bin/env python3
"""G7 pilot: стоимость thinking-режима Qwen3-8B (Q4_K_M) на function-calling запросах, llama-server (Metal).

Не научный эксперимент, а оценка порядка величин для обзора литературы (N=16 рукописных задач, 1 прогон на конфигурацию).
Запуск:  python3 thinking_pilot.py <tag> <port> <enable_thinking:0|1> <out.jsonl>
Сервер поднимается снаружи (см. COMMANDS.txt) с нужным --reasoning-budget.
"""
import json, sys, time, urllib.request

TOOLS = [
 {"type":"function","function":{"name":"get_weather","description":"Get current weather for a city.",
  "parameters":{"type":"object","properties":{"city":{"type":"string"},"unit":{"type":"string","enum":["celsius","fahrenheit"]}},"required":["city"]}}},
 {"type":"function","function":{"name":"convert_currency","description":"Convert an amount of money from one currency to another.",
  "parameters":{"type":"object","properties":{"amount":{"type":"number"},"from_currency":{"type":"string","description":"ISO 4217 code"},"to_currency":{"type":"string","description":"ISO 4217 code"}},"required":["amount","from_currency","to_currency"]}}},
 {"type":"function","function":{"name":"set_timer","description":"Start a countdown timer.",
  "parameters":{"type":"object","properties":{"seconds":{"type":"integer"},"label":{"type":"string"}},"required":["seconds"]}}},
 {"type":"function","function":{"name":"search_files","description":"Search local files by name pattern inside a directory.",
  "parameters":{"type":"object","properties":{"directory":{"type":"string"},"pattern":{"type":"string","description":"glob pattern"}},"required":["directory","pattern"]}}},
 {"type":"function","function":{"name":"calculate_bmi","description":"Compute body mass index from weight in kilograms and height in meters.",
  "parameters":{"type":"object","properties":{"weight_kg":{"type":"number"},"height_m":{"type":"number"}},"required":["weight_kg","height_m"]}}},
 {"type":"function","function":{"name":"translate_text","description":"Translate text into a target language.",
  "parameters":{"type":"object","properties":{"text":{"type":"string"},"target_language":{"type":"string","description":"ISO 639-1 code"}},"required":["text","target_language"]}}},
 {"type":"function","function":{"name":"create_calendar_event","description":"Create a calendar event.",
  "parameters":{"type":"object","properties":{"title":{"type":"string"},"date":{"type":"string","description":"YYYY-MM-DD"},"time":{"type":"string","description":"HH:MM, 24h"}},"required":["title","date","time"]}}},
 {"type":"function","function":{"name":"get_battery_status","description":"Return the battery level and charging state of this device.",
  "parameters":{"type":"object","properties":{}}}},
]

# expected: list of (name, {arg: value}) ; [] means the model must NOT call any tool
TASKS = [
 ("simple",  "What's the weather in Saint Petersburg right now, in celsius?", [("get_weather",{"city":"Saint Petersburg","unit":"celsius"})]),
 ("simple",  "Convert 250 US dollars to euros.", [("convert_currency",{"amount":250,"from_currency":"USD","to_currency":"EUR"})]),
 ("simple",  "Set a timer for 7 minutes for the tea.", [("set_timer",{"seconds":420})]),
 ("simple",  "Find all PDF files in /Users/me/Documents.", [("search_files",{"directory":"/Users/me/Documents","pattern":"*.pdf"})]),
 ("simple",  "I weigh 82 kg and I am 1.79 m tall. What's my BMI?", [("calculate_bmi",{"weight_kg":82,"height_m":1.79})]),
 ("simple",  "How much charge is left on my laptop?", [("get_battery_status",{})]),
 ("multiple","Translate 'good morning, colleagues' into German.", [("translate_text",{"target_language":"de"})]),
 ("multiple","Put 'thesis defense' in my calendar on 2027-06-15 at 10:30.", [("create_calendar_event",{"title":"thesis defense","date":"2027-06-15","time":"10:30"})]),
 ("multiple","I have 1200 yen, how many British pounds is that?", [("convert_currency",{"amount":1200,"from_currency":"JPY","to_currency":"GBP"})]),
 ("multiple","Remind me in an hour and a half to stretch - use a timer.", [("set_timer",{"seconds":5400})]),
 ("parallel","What's the weather in Tokyo and in Oslo?", [("get_weather",{"city":"Tokyo"}),("get_weather",{"city":"Oslo"})]),
 ("parallel","Convert 100 EUR to USD and also 100 EUR to CHF.", [("convert_currency",{"amount":100,"from_currency":"EUR","to_currency":"USD"}),("convert_currency",{"amount":100,"from_currency":"EUR","to_currency":"CHF"})]),
 ("irrelevance","Who wrote the novel 'Crime and Punishment'?", []),
 ("irrelevance","Book me a table for two at an Italian restaurant tonight.", []),
 ("irrelevance","What is 17 multiplied by 23?", []),
 ("irrelevance","Turn on the living room lights.", []),
]

def norm(v):
    if isinstance(v,str): return v.strip().lower()
    if isinstance(v,(int,float)): return float(v)
    return v

def score(expected, calls):
    if not expected:
        return len(calls) == 0
    if len(calls) != len(expected):
        return False
    used = [False]*len(calls)
    for name, args in expected:
        ok = False
        for i,c in enumerate(calls):
            if used[i] or c["name"] != name: continue
            try: a = json.loads(c["arguments"]) if isinstance(c["arguments"],str) else c["arguments"]
            except Exception: continue
            if all(k in a and norm(a[k]) == norm(v) for k,v in args.items()):
                used[i] = ok = True; break
        if not ok: return False
    return True

def main():
    tag, port, think, out = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1", sys.argv[4]
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    with open(out, "a") as f:
        for idx,(cat, q, exp) in enumerate(TASKS):
            body = {"model":"qwen3-8b","messages":[{"role":"system","content":"You are an on-device assistant. Use a tool only if it can fulfil the request; otherwise answer directly."},{"role":"user","content":q}],
                    "tools":TOOLS, "max_tokens":4096, "seed":1234,
                    "chat_template_kwargs":{"enable_thinking": think}}
            if think: body.update({"temperature":0.6,"top_p":0.95,"top_k":20})
            else:     body.update({"temperature":0.7,"top_p":0.8,"top_k":20})
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
            t0 = time.perf_counter()
            r = json.load(urllib.request.urlopen(req, timeout=900))
            wall = time.perf_counter() - t0
            msg = r["choices"][0]["message"]
            calls = [{"name":c["function"]["name"],"arguments":c["function"]["arguments"]} for c in (msg.get("tool_calls") or [])]
            rtxt = msg.get("reasoning_content") or ""
            rtok = 0
            if rtxt:
                tq = urllib.request.Request(f"http://127.0.0.1:{port}/tokenize", data=json.dumps({"content":rtxt}).encode(), headers={"Content-Type":"application/json"})
                rtok = len(json.load(urllib.request.urlopen(tq, timeout=60)).get("tokens", []))
            rec = {"tag":tag,"idx":idx,"cat":cat,"think":think,"wall_s":round(wall,3),
                   "prompt_n":r.get("timings",{}).get("prompt_n"),"prompt_ms":r.get("timings",{}).get("prompt_ms"),
                   "predicted_n":r.get("timings",{}).get("predicted_n"),"predicted_ms":r.get("timings",{}).get("predicted_ms"),
                   "cache_n":r.get("timings",{}).get("cache_n"),
                   "usage":r.get("usage"),"finish":r["choices"][0].get("finish_reason"),
                   "reasoning_chars":len(rtxt),"reasoning_tokens":rtok,
                   "content_chars":len(msg.get("content") or ""),
                   "calls":calls,"correct":score(exp,calls)}
            f.write(json.dumps(rec, ensure_ascii=False)+"\n"); f.flush()
            print(tag, idx, cat, "ok" if rec["correct"] else "WRONG", rec["predicted_n"], rec["wall_s"], flush=True)

if __name__ == "__main__":
    main()

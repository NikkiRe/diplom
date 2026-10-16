#!/usr/bin/env bash
# Контрактная фикстура монитора: настоящие edgectx + hw-mcp (stdio) на фикстуре sysfs и сценарная заглушка LLM.
# Сценарий (пользовательские команды через POST /v1/user): включить вентилятор (allow) → сразу выключить
# (rate_limit) → через 2,2 с выключить (allow) → значение "yes" (invalid_args) → импульс 1 с (allow, pulse_end) →
# температура на кухне 40 °C (файл фикстуры) → включить (allow) → выключить (interlock: перегрев) → прочитать
# температуру. Результат: <out>/{edgectx_trace.jsonl, hwmcp.jsonl, declared.json}.
#
#   monitor/tests/fixtures/hwmcp_real/capture.sh generic  [EDGECTX HWMCP]
#   monitor/tests/fixtures/hwmcp_real/capture.sh explicit [EDGECTX HWMCP]
# Бинарники по умолчанию собираются из agent/ и hwmcp/ во временный каталог. Порты: edgectx 18088, LLM 18108.
set -euo pipefail
CAT=${1:?generic|explicit}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../.." && pwd)
PY=${PY:-$ROOT/.venv/bin/python}
EP=${EP:-18088}; LP=${LP:-18108}
TMP=$(mktemp -d)
trap 'kill ${LPID:-} ${EPID:-} 2>/dev/null || true; rm -rf "$TMP"' EXIT
EDGECTX=${2:-}; HWMCP=${3:-}
if [[ -z $EDGECTX ]]; then (cd "$ROOT/agent" && go build -trimpath -o "$TMP/edgectx" ./cmd/edgectx); EDGECTX=$TMP/edgectx; fi
if [[ -z $HWMCP ]]; then (cd "$ROOT/hwmcp" && go build -trimpath -o "$TMP/hw-mcp" ./cmd/hw-mcp); HWMCP=$TMP/hw-mcp; fi
for p in $EP $LP; do ! lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1 || { echo "порт $p занят"; exit 3; }; done
OUT=$HERE/$CAT; mkdir -p "$OUT"; rm -f "$OUT"/*.jsonl
cp -a "$ROOT/hwmcp/testdata/fixture_root" "$TMP/root"
cp "$ROOT/hwmcp/configs/declared.lab.json" "$OUT/declared.json"
TEMP=$(find "$TMP/root/sys" -path '*1-0076/iio:device0/in_temp_input')
cat > "$TMP/cfg.yaml" <<YAML
run_id: hwmcp-real-$CAT
clock: {mode: real}
listen: 127.0.0.1:$EP
llm: {base_url: "http://127.0.0.1:$LP/v1", model: script, timeout_s: 10}
mcp:
  - {name: hw, transport: stdio, cmd: ["$HWMCP", "-root", "$TMP/root", "-declared", "$OUT/declared.json", "-trace", "$OUT/hwmcp.jsonl", "-run-id", "hwmcp-real-$CAT", "-catalog", "$CAT"], inventory_uri: "hw://inventory"}
policy: {wake: gate, max_steps: 4, catalog: $CAT}
trace: $OUT/edgectx_trace.jsonl
userctx: {db: ":memory:"}
YAML
"$PY" "$HERE/script_llm.py" $LP & LPID=$!
"$EDGECTX" -config "$TMP/cfg.yaml" > "$TMP/edgectx.log" 2>&1 & EPID=$!
for _ in $(seq 1 100); do curl -fs -o /dev/null "http://127.0.0.1:$EP/v1/health" && break; sleep 0.1; done
say() { curl -fsS -XPOST "http://127.0.0.1:$EP/v1/user" -H 'Content-Type: application/json' \
          -d "$("$PY" -c 'import json,sys; print(json.dumps({"id": sys.argv[1], "text": "CMD:" + sys.argv[2]}))' "$1" "$2")" >/dev/null; }
if [[ $CAT == generic ]]; then
  ON='[{"name":"write_property","arguments":{"device":"relay_fan","property":"state","value":true}}]'
  OFF='[{"name":"write_property","arguments":{"device":"relay_fan","property":"state","value":false}}]'
  BAD='[{"name":"write_property","arguments":{"device":"relay_fan","property":"state","value":"yes"}}]'
  PULSE='[{"name":"invoke_action","arguments":{"device":"relay_fan","action":"pulse","args":{"seconds":1}}}]'
  READ='[{"name":"read_property","arguments":{"device":"kitchen_bme280","property":"temperature"}}]'
else
  ON='[{"name":"set_relay_fan_state","arguments":{"value":true}}]'
  OFF='[{"name":"set_relay_fan_state","arguments":{"value":false}}]'
  BAD='[{"name":"set_relay_fan_state","arguments":{"value":"yes"}}]'
  PULSE='[{"name":"pulse_relay_fan","arguments":{"seconds":1}}]'
  READ='[{"name":"read_kitchen_bme280_temperature","arguments":{}}]'
fi
say u1 "$ON"; say u2 "$OFF"; sleep 2.2
say u3 "$OFF"; say u4 "$BAD"; sleep 2.2
say u5 "$PULSE"; sleep 1.5
echo 40000 > "$TEMP"; sleep 1.5
say u6 "$ON"; sleep 2.2
say u7 "$OFF"; say u8 "$READ"
curl -fs -XPOST "http://127.0.0.1:$EP/v1/shutdown" >/dev/null || true
for _ in $(seq 1 100); do kill -0 $EPID 2>/dev/null || break; sleep 0.1; done
sed -i '' -e "s#$TMP#/tmp/fixture#g" "$OUT/edgectx_trace.jsonl" "$OUT/hwmcp.jsonl" 2>/dev/null || \
  sed -i -e "s#$TMP#/tmp/fixture#g" "$OUT/edgectx_trace.jsonl" "$OUT/hwmcp.jsonl"
wc -l "$OUT"/*.jsonl

import json
def b(o): return len(json.dumps(o, separators=(',',':'), ensure_ascii=False).encode())
meta = {"io.modelcontextprotocol/protocolVersion":"2026-07-28",
        "io.modelcontextprotocol/clientInfo":{"name":"edge-agent","version":"0.1.0"},
        "io.modelcontextprotocol/clientCapabilities":{}}
call_old = {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"read_temperature","arguments":{"sensor_id":"dht22_1"}}}
call_new = {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"read_temperature","arguments":{"sensor_id":"dht22_1"},"_meta":meta}}
res_old = {"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"{\"t\":22.5,\"unit\":\"C\"}"}],"structuredContent":{"t":22.5,"unit":"C"},"isError":False}}
res_new = {"jsonrpc":"2.0","id":7,"result":{"resultType":"complete","content":[{"type":"text","text":"{\"t\":22.5,\"unit\":\"C\"}"}],"structuredContent":{"t":22.5,"unit":"C"},"isError":False}}
init_req = {"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"edge-agent","version":"0.1.0"}}}
init_res = {"jsonrpc":"2.0","id":0,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{"listChanged":True},"resources":{"subscribe":True,"listChanged":True}},"serverInfo":{"name":"hw-server","version":"0.1.0"}}}
inited = {"jsonrpc":"2.0","method":"notifications/initialized"}
print("tools/call old", b(call_old), "new", b(call_new), "delta", b(call_new)-b(call_old))
print("result old", b(res_old), "new", b(res_new))
hs = b(init_req)+b(init_res)+b(inited)
print("handshake 2025-11-25 total", hs)
print("break-even calls (meta delta vs handshake):", hs/(b(call_new)-b(call_old)))

import json
# Tool definitions copied from MCP spec 2026-07-28 (server/tools) examples
tools=[
 {"name":"get_weather","description":"Get current weather information for a location",
  "inputSchema":{"type":"object","properties":{"location":{"type":"string","description":"City name or zip code"}},"required":["location"]}},
 {"name":"execute_sql","description":"Execute SQL on Google Cloud Spanner",
  "inputSchema":{"type":"object","properties":{"region":{"type":"string","description":"The region to execute the query in"},"query":{"type":"string","description":"The SQL query to execute"}},"required":["region","query"]}},
 {"name":"get_weather_data","description":"Get current weather data for a location",
  "inputSchema":{"type":"object","properties":{"location":{"type":"string","description":"City name or zip code"}},"required":["location"]}},
 {"name":"calculate_sum","description":"Add two numbers",
  "inputSchema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}},
 {"name":"list_users","description":"Returns a list of all users","inputSchema":{"type":"object","properties":{}}},
 {"name":"get_current_time","description":"Returns the current server time","inputSchema":{"type":"object","additionalProperties":False}},
]
hdr=("# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
     "You are provided with function signatures within <tools></tools> XML tags:\n<tools>")
ftr=("\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n"
     "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>")
def oa(t): return {"type":"function","function":{"name":t["name"],"description":t["description"],"parameters":t["inputSchema"]}}
open("hdr.txt","w").write(hdr+ftr)
for i,t in enumerate(tools):
    open(f"t{i}_json.txt","w").write("\n"+json.dumps(oa(t), ensure_ascii=False))
    open(f"t{i}_min.txt","w").write("\n"+json.dumps(oa(t), separators=(",",":"), ensure_ascii=False))
    props=t["inputSchema"].get("properties",{}); req=set(t["inputSchema"].get("required",[]))
    sig=t["name"]+"("+", ".join(f"{k}{'' if k in req else '?'}: {v.get('type','any')}" for k,v in props.items())+") - "+t["description"]
    pd="; ".join(f"{k}: {v['description']}" for k,v in props.items() if 'description' in v)
    open(f"t{i}_sig.txt","w").write("\n"+sig+(" ["+pd+"]" if pd else ""))
    open(f"t{i}_name.txt","w").write("\n"+t["name"])
print(len(tools))

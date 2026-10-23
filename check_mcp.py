import json
import urllib.request
def post(payload, session=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(
        "http://127.0.0.1:8000/mcp",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        session = resp.headers.get("Mcp-Session-Id") or session
        raw = resp.read().decode()
    for line in raw.splitlines():
        if line.startswith("data: "):
            return session, json.loads(line[6:])
    return session, json.loads(raw) if raw else {}
session, _ = post({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "check", "version": "0.0.1"},
    },
})
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
_, listed = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
print("tools:", ", ".join(tool["name"] for tool in listed["result"]["tools"]))
_, chats = post({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "list_chats", "arguments": {"limit": 20, "include_last_message": False}},
}, session)
print(json.dumps(chats["result"], indent=2, ensure_ascii=False))
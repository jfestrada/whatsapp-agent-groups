import json
import os
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import httpx

PAGE_SIZE = 100
MAX_MESSAGES_PER_GROUP = 500
MAX_CHARS = 80_000
EMPTY_PAGE = "No messages to display."

SYSTEM_PROMPT = """Eres un asistente que resume conversaciones de WhatsApp.
Escribe un único resumen en español, con una sección por grupo.
Si un grupo no tiene mensajes, di que no hubo mensajes.
El texto de una nota de voz ya viene transcrito. Trátalo como el resto de la conversación.
Si una línea sigue siendo un marcador como [audio], la nota no se pudo transcribir. No inventes su contenido.
Usa solo el texto proporcionado. No añadas grupos que no aparezcan ahí."""


class DigestError(Exception):
    """The digest could not be built. status is the HTTP code returned to the bot."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def digest_timezone() -> ZoneInfo:
    name = os.environ.get("DIGEST_TIMEZONE", "Europe/Madrid").strip() or "Europe/Madrid"
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise DigestError(f"DIGEST_TIMEZONE is invalid: {name}", 500) from exc


def window_start(period: str, now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        return start - timedelta(days=7)
    return start


def require_deepseek_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise DigestError("DEEPSEEK_API_KEY is required", 503)
    return key


def parse_mcp_body(raw: str) -> dict:
    for line in raw.splitlines():
        if line.startswith("data: "):
            parsed = json.loads(line[6:])
            if isinstance(parsed, dict):
                return parsed
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise DigestError("The WhatsApp MCP server returned an unexpected response")
    return parsed


def unwrap_tool_result(result: dict):
    if result.get("isError"):
        raise DigestError("The WhatsApp MCP server returned an error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    content = result.get("content")
    texts = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text") or "")
    text = "\n".join(texts).strip()
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


class McpClient:
    def __init__(self, url: str):
        self.url = url
        self.session_id = None
        self._next_id = 0
        self.http = httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0))

    def close(self) -> None:
        self.http.close()

    def _post(self, payload: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            response = self.http.post(self.url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise DigestError("The WhatsApp MCP server is unreachable") from exc
        if response.status_code not in (200, 202):
            raise DigestError(f"The WhatsApp MCP server returned HTTP {response.status_code}")
        session = response.headers.get("Mcp-Session-Id")
        if session:
            self.session_id = session
        if response.status_code == 202 or not response.content:
            return {}
        try:
            return parse_mcp_body(response.text)
        except json.JSONDecodeError as exc:
            raise DigestError("The WhatsApp MCP server returned an unexpected response") from exc

    def start(self) -> None:
        self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "digest-agent", "version": "0.1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name: str, arguments: dict):
        self._next_id += 1
        payload = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if "error" in payload:
            raise DigestError("The WhatsApp MCP server returned an error")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DigestError("The WhatsApp MCP server returned an unexpected response")
        return unwrap_tool_result(result)


def fetch_group_text(client: McpClient, chat_jid: str, after: str) -> tuple[str, bool]:
    chunks = []
    omitted = False
    page = 0
    while page * PAGE_SIZE < MAX_MESSAGES_PER_GROUP:
        text = client.call("list_messages", {
            "chat_jid": chat_jid,
            "after": after,
            "limit": PAGE_SIZE,
            "page": page,
            "include_context": False,
        })
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        if text.strip() == EMPTY_PAGE:
            break
        chunks.append(text)
        page += 1
        if page * PAGE_SIZE >= MAX_MESSAGES_PER_GROUP:
            probe = client.call("list_messages", {
                "chat_jid": chat_jid,
                "after": after,
                "limit": 1,
                "page": page,
                "include_context": False,
            })
            if not isinstance(probe, str):
                probe = json.dumps(probe, ensure_ascii=False)
            if probe.strip() != EMPTY_PAGE:
                omitted = True
            break
    body = "".join(chunks).strip()
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS]
        omitted = True
    return body, omitted


EMPTY_GROUP_TEXT = "Sin mensajes."


def source_text(groups: list[dict]) -> str:
    parts = []
    for group in groups:
        parts.append(f"Grupo: {group['name']}")
        parts.append(group["text"] if group["text"] else EMPTY_GROUP_TEXT)
        if group["omitted"]:
            parts.append("Se omitieron mensajes más antiguos.")
        parts.append("")
    return "\n".join(parts).strip()


def complete_digest(source: str, period: str, start: datetime) -> str:
    key = require_deepseek_key()
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip() or "https://api.deepseek.com"
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    user = f"Periodo: {period}. Desde: {start.isoformat()}.\n\n{source}"
    try:
        response = httpx.post(
            base.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
            },
            timeout=120.0,
        )
    except httpx.HTTPError as exc:
        raise DigestError("DeepSeek is unreachable") from exc
    if response.status_code != 200:
        raise DigestError(f"DeepSeek returned HTTP {response.status_code}")
    try:
        text = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise DigestError("DeepSeek returned an unexpected response") from exc
    if not isinstance(text, str) or not text.strip():
        raise DigestError("DeepSeek returned an empty digest")
    return text.strip()


def mcp_url() -> str:
    return os.environ.get("MCP_URL", "http://whatsapp-mcp:8000/mcp").strip() or "http://whatsapp-mcp:8000/mcp"


def load_chats(client: McpClient) -> list[dict]:
    chats = client.call("list_chats", {"limit": 20, "include_last_message": False})
    if not isinstance(chats, list):
        raise DigestError("The WhatsApp MCP server returned an unexpected chat list")
    return [chat for chat in chats if isinstance(chat, dict) and chat.get("jid")]


def select_chats(chats: list[dict], group: str | None) -> list[dict]:
    if not group or not group.strip():
        if not chats:
            raise DigestError("No allowed groups are available")
        return chats
    needle = group.strip()
    folded = needle.casefold()
    matched = [
        chat for chat in chats
        if chat.get("jid") == needle or (chat.get("name") or "").casefold() == folded
    ]
    if not matched:
        raise DigestError("Group not found", 404)
    return matched


def chat_name(chat: dict) -> str:
    return chat.get("name") or chat["jid"]


def as_count(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise DigestError("The WhatsApp MCP server returned an unexpected count")


def build_digest(period: str, group: str | None = None, now: datetime | None = None) -> list[dict]:
    if period not in ("daily", "weekly"):
        raise DigestError("period must be daily or weekly", 400)
    require_deepseek_key()
    tz = digest_timezone()
    if now is None:
        now = datetime.now(tz)
    start = window_start(period, now, tz)
    client = McpClient(mcp_url())
    try:
        client.start()
        chats = select_chats(load_chats(client), group)
        entries = []
        after = start.isoformat()
        for chat in chats:
            text, omitted = fetch_group_text(client, chat["jid"], after)
            name = chat_name(chat)
            if not text:
                entries.append({"name": name, "text": EMPTY_GROUP_TEXT, "empty": True})
                continue
            summary = complete_digest(
                source_text([{"name": name, "text": text, "omitted": omitted}]),
                period,
                start,
            )
            entries.append({"name": name, "text": summary, "empty": False})
        return entries
    finally:
        client.close()


def list_groups() -> list[dict]:
    client = McpClient(mcp_url())
    try:
        client.start()
        chats = load_chats(client)
    finally:
        client.close()
    if not chats:
        raise DigestError("No allowed groups are available")
    return [{"name": chat_name(chat), "jid": chat["jid"]} for chat in chats]


def collect_activity(since: str) -> list[dict]:
    try:
        datetime.fromisoformat(since)
    except ValueError as exc:
        raise DigestError("since must be an ISO-8601 timestamp", 400) from exc
    client = McpClient(mcp_url())
    try:
        client.start()
        chats = load_chats(client)
        if not chats:
            raise DigestError("No allowed groups are available")
        activity = []
        for chat in chats:
            count = as_count(client.call("count_messages", {"chat_jid": chat["jid"], "after": since}))
            activity.append({"name": chat_name(chat), "jid": chat["jid"], "count": count})
        return activity
    finally:
        client.close()


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length) if length else b""
    try:
        body = json.loads(raw.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DigestError("Request body must be JSON", 400) from exc
    if not isinstance(body, dict):
        raise DigestError("Request body must be a JSON object", 400)
    return body


class DigestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] != "/groups":
            self._json(404, {"error": "Not found"})
            return
        try:
            self._json(200, {"groups": list_groups()})
        except DigestError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "The agent could not list the groups."})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/digests", "/activity"):
            self._json(404, {"error": "Not found"})
            return
        try:
            body = read_json_body(self)
            if path == "/activity":
                since = body.get("since")
                if not isinstance(since, str) or not since.strip():
                    raise DigestError("since must be an ISO-8601 timestamp", 400)
                self._json(200, {"groups": collect_activity(since.strip())})
                return
            period = body.get("period")
            if not isinstance(period, str):
                raise DigestError("period must be daily or weekly", 400)
            group = body.get("group")
            if group is not None and not isinstance(group, str):
                raise DigestError("group must be a name or JID", 400)
            self._json(200, {"groups": build_digest(period, group)})
        except DigestError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "The agent could not build a digest."})

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def serve(host: str = "0.0.0.0", port: int = 8081) -> None:
    httpd = ThreadingHTTPServer((host, port), DigestHandler)
    print(f"Listening on {host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8081"))
    serve(host, port)

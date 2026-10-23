import json
import os
import threading
from typing import Optional

import httpx


class WhisperError(Exception):
    """The Whisper MCP server did not return a transcript."""


def _parse_body(raw: str) -> dict:
    for line in raw.splitlines():
        if line.startswith("data: "):
            parsed = json.loads(line[6:])
            if isinstance(parsed, dict):
                return parsed
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise WhisperError("Whisper returned an unexpected response")
    return parsed


def _unwrap(result: dict) -> str:
    if result.get("isError"):
        raise WhisperError("Whisper could not transcribe the audio")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return structured["result"]
    texts = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text") or "")
    return "\n".join(texts).strip()


class WhisperSession:
    """One streamable HTTP session, reused for every voice note in this process."""

    def __init__(self, url: str):
        self.url = url
        self.session_id = None
        self._next_id = 0
        self._started = False
        self.http = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

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
            raise WhisperError("Whisper is unreachable") from exc
        if response.status_code not in (200, 202):
            raise WhisperError(f"Whisper returned HTTP {response.status_code}")
        session = response.headers.get("Mcp-Session-Id")
        if session:
            self.session_id = session
        if response.status_code == 202 or not response.content:
            return {}
        try:
            return _parse_body(response.text)
        except json.JSONDecodeError as exc:
            raise WhisperError("Whisper returned an unexpected response") from exc

    def start(self) -> None:
        self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "whatsapp-mcp", "version": "0.1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._started = True

    def transcribe(self, path: str) -> str:
        if not self._started:
            self.start()
        self._next_id += 1
        payload = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": "transcribe", "arguments": {"path": path}},
        })
        if "error" in payload:
            raise WhisperError("Whisper could not transcribe the audio")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise WhisperError("Whisper returned an unexpected response")
        return _unwrap(result)


_session: Optional[WhisperSession] = None
_lock = threading.Lock()


def request_transcript(path: str) -> str:
    url = os.environ.get("WHISPER_MCP_URL", "").strip()
    if not url:
        raise WhisperError("WHISPER_MCP_URL is not set")
    with _lock:
        global _session
        if _session is None or _session.url != url:
            if _session is not None:
                _session.close()
            _session = WhisperSession(url)
        try:
            return _session.transcribe(path)
        except Exception:
            _session.close()
            _session = None
            raise

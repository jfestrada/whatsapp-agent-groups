import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import agent


def start_server(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


class McpStub(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        McpStub.calls.append(body)
        method = body.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "serverInfo": {"name": "stub", "version": "0"},
                },
            }
            self._json(payload, session="sess-1")
            return
        params = body.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "list_chats":
            result = [
                {"jid": "111@g.us", "name": "Alpha"},
                {"jid": "333@g.us", "name": "Gamma"},
            ]
            self._json({
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "structuredContent": {"result": result},
                    "isError": False,
                },
            })
            return
        if name == "list_messages" and arguments.get("chat_jid") == "111@g.us":
            text = "[2026-10-23 10:00:00] Chat: Alpha From: Ana: alpha-line\n"
        elif name == "count_messages":
            count = 7 if arguments.get("chat_jid") == "111@g.us" else 0
            self._json({
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": str(count)}],
                    "structuredContent": {"result": count},
                    "isError": False,
                },
            })
            return
        else:
            text = "No messages to display."
        self._json({
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            },
        })

    def _json(self, payload, session=None):
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


class DeepSeekStub(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length)
        DeepSeekStub.bodies.append(raw.decode())
        payload = {
            "choices": [{"message": {"content": "resumen de prueba"}}],
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        return


class DigestTests(unittest.TestCase):
    def setUp(self):
        McpStub.calls = []
        DeepSeekStub.bodies = []
        self.mcp = start_server(McpStub)
        self.deepseek = start_server(DeepSeekStub)
        self.agent = start_server(agent.DigestHandler)
        os.environ["MCP_URL"] = f"http://127.0.0.1:{self.mcp.server_address[1]}/mcp"
        os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{self.deepseek.server_address[1]}"
        os.environ["DEEPSEEK_MODEL"] = "deepseek-chat"
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
        os.environ["DIGEST_TIMEZONE"] = "Europe/Madrid"

    def tearDown(self):
        for httpd in (self.agent, self.mcp, self.deepseek):
            httpd.shutdown()
            httpd.server_close()

    def post(self, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.agent.server_address[1]}/digests",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            finally:
                exc.close()

    def test_daily_and_weekly_use_only_mcp_text(self):
        daily_status, daily_body = self.post({"period": "daily"})
        weekly_status, weekly_body = self.post({"period": "weekly"})
        self.assertEqual(daily_status, 200)
        self.assertEqual(weekly_status, 200)
        expected = [
            {"name": "Alpha", "text": "resumen de prueba", "empty": False},
            {"name": "Gamma", "text": "Sin mensajes.", "empty": True},
        ]
        self.assertEqual(daily_body["groups"], expected)
        self.assertEqual(weekly_body["groups"], expected)

        sent = "\n".join(DeepSeekStub.bodies)
        self.assertIn("alpha-line", sent)
        self.assertIn("Grupo: Alpha", sent)
        self.assertNotIn("Grupo: Gamma", sent)
        self.assertNotIn("delta-secret", sent)

        afters = {"daily": set(), "weekly": set()}
        message_calls = [
            call for call in McpStub.calls
            if (call.get("params") or {}).get("name") == "list_messages"
        ]
        self.assertGreaterEqual(len(message_calls), 4)
        midpoint = len(message_calls) // 2
        for call in message_calls[:midpoint]:
            afters["daily"].add(call["params"]["arguments"]["after"])
        for call in message_calls[midpoint:]:
            afters["weekly"].add(call["params"]["arguments"]["after"])
        daily_start = datetime.fromisoformat(next(iter(afters["daily"])))
        weekly_start = datetime.fromisoformat(next(iter(afters["weekly"])))
        madrid_now = datetime.now(ZoneInfo("Europe/Madrid"))
        self.assertEqual(daily_start - weekly_start, timedelta(days=7))
        self.assertEqual(daily_start.hour, 0)
        self.assertEqual(daily_start.minute, 0)
        self.assertEqual(daily_start.utcoffset(), madrid_now.utcoffset())
        self.assertEqual(weekly_start.utcoffset(), madrid_now.utcoffset())

    def test_window_start_is_local_midnight(self):
        tz = ZoneInfo("Europe/Madrid")
        now = datetime(2026, 10, 23, 15, 30, tzinfo=tz)
        self.assertEqual(
            agent.window_start("daily", now, tz),
            datetime(2026, 10, 23, 0, 0, tzinfo=tz),
        )
        self.assertEqual(
            agent.window_start("weekly", now, tz),
            datetime(2026, 10, 16, 0, 0, tzinfo=tz),
        )

    def test_long_group_text_keeps_the_newest_portion(self):
        class FakeClient:
            def call(self, name, arguments):
                return "abcdefghijklmnopqrstuvwxyz"

        original = agent.MAX_CHARS
        agent.MAX_CHARS = 10
        try:
            text, omitted = agent.fetch_group_text(
                FakeClient(),
                "111@g.us",
                "2026-10-23T00:00:00+02:00",
            )
        finally:
            agent.MAX_CHARS = original
        self.assertTrue(omitted)
        self.assertEqual(text, "abcdefghij")
        rendered = agent.source_text([{"name": "Alpha", "text": text, "omitted": True}])
        self.assertIn("Se omitieron mensajes más antiguos.", rendered)

    def test_invalid_period_and_missing_key(self):
        status, body = self.post({"period": "monthly"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

        os.environ["DEEPSEEK_API_KEY"] = ""
        status, body = self.post({"period": "daily"})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "DEEPSEEK_API_KEY is required")

    def test_group_filter_drops_the_other_group(self):
        status, body = self.post({"period": "daily", "group": "Gamma"})
        self.assertEqual(status, 200)
        self.assertEqual(body["groups"], [
            {"name": "Gamma", "text": "Sin mensajes.", "empty": True},
        ])
        self.assertEqual(DeepSeekStub.bodies, [])
        message_jids = [
            (call.get("params") or {}).get("arguments", {}).get("chat_jid")
            for call in McpStub.calls
            if (call.get("params") or {}).get("name") == "list_messages"
        ]
        self.assertTrue(message_jids)
        self.assertTrue(all(jid == "333@g.us" for jid in message_jids))

        missing_status, missing = self.post({"period": "daily", "group": "Nope"})
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing["error"], "Group not found")

    def test_groups_and_activity(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.agent.server_address[1]}/groups",
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            groups = json.load(response)
        self.assertEqual(groups["groups"], [
            {"name": "Alpha", "jid": "111@g.us"},
            {"name": "Gamma", "jid": "333@g.us"},
        ])

        status, body = self.post_path("/activity", {"since": "2026-10-23T00:00:00+02:00"})
        self.assertEqual(status, 200)
        self.assertEqual(body["groups"], [
            {"name": "Alpha", "jid": "111@g.us", "count": 7},
            {"name": "Gamma", "jid": "333@g.us", "count": 0},
        ])
        count_calls = [
            call for call in McpStub.calls
            if (call.get("params") or {}).get("name") == "count_messages"
        ]
        self.assertEqual(len(count_calls), 2)
        self.assertNotIn("list_messages", [((call.get("params") or {}).get("name")) for call in count_calls])

    def post_path(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.agent.server_address[1]}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            finally:
                exc.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server
from ashra.chat import ChatResult
from ashra.errors import NotConfiguredError


class FakeChat:
    def __init__(self, result: ChatResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def ask(self, question: str, session_id: str | None = None) -> ChatResult:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ViewerHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.original_chat = server.CHAT
        self.addCleanup(setattr, server, "CHAT", self.original_chat)

    def get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, path: str, body: object) -> tuple[int, dict]:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
                return exc.code, json.loads(raw) if raw else {}
            finally:
                exc.close()

    def test_viewer_is_served_with_branding(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("A.S.H.R.A", body)

    def test_config_path_is_blocked(self) -> None:
        try:
            urllib.request.urlopen(self.base_url + "/config.json", timeout=5)
        except urllib.error.HTTPError as exc:
            try:
                self.assertEqual(exc.code, 404)
            finally:
                exc.close()
        else:
            self.fail("config.json should not be served")

    def test_malformed_json_is_rejected(self) -> None:
        status, payload = self.post("/chat", b"{")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_chat_returns_the_service_result(self) -> None:
        server.CHAT = FakeChat(
            ChatResult(
                answer="Test answer.",
                nodes=[0],
                sources=[{"id": 0, "label": "Test Note"}],
                session_id="session-12345678",
                model="test-model",
            )
        )

        status, payload = self.post("/chat", {"question": "hello"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["answer"], "Test answer.")
        self.assertEqual(payload["nodes"], [0])
        self.assertEqual(payload["session_id"], "session-12345678")

    def test_chat_maps_not_configured_to_503(self) -> None:
        server.CHAT = FakeChat(error=NotConfiguredError("No key."))

        status, payload = self.post("/chat", {"question": "hello"})

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "model_not_configured")

    def test_payload_limit_is_enforced(self) -> None:
        status, payload = self.post("/chat", b"x" * (server.MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()

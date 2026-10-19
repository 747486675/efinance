"""HTTP contracts with upstream calls stubbed and sockets prohibited."""
import base64
import importlib.util
import os
import socket
import unittest
from unittest.mock import patch

import pandas as pd

from efinance.exceptions import ParameterError, RateLimitError, ResponseError, SymbolNotFoundError, TimeoutError

HAS_WEB = all(importlib.util.find_spec(m) for m in ("fastapi", "httpx", "plotly"))
_real_connect = socket.socket.connect


def offline_connect(sock, address):
    # asyncio's Windows socketpair requires a loopback connection.
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return _real_connect(sock, address)
    raise AssertionError("External network prohibited")


@unittest.skipUnless(HAS_WEB, "optional web dependencies not installed")
class WebTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from efinance.web import create_app
        self.environment = patch.dict(os.environ, {"EFINANCE_WEB_USERNAME": "", "EFINANCE_WEB_PASSWORD": ""})
        self.environment.start()
        self.network = patch.object(socket.socket, "connect", offline_connect)
        self.network.start()
        self.client = TestClient(create_app())
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.network.stop()
        self.environment.stop()

    def test_health_and_static_no_upstream(self):
        with patch("efinance.web.query_one", side_effect=AssertionError("unexpected query")):
            self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/static/app.js").status_code, 200)
            self.assertEqual(self.client.get("/static/plotly.min.js").status_code, 200)

    def test_history_json_export_and_empty(self):
        frame = pd.DataFrame({"日期": ["2026-01-02"], "股票代码": ["000001"], "收盘": [float("inf")]})
        body = dict(asset="stock", codes=["000001"], kind="history")
        with patch("efinance.web.query_one", return_value=frame) as query:
            response = self.client.post("/api/history", json=body)
            self.assertEqual(response.status_code, 200)
            data = response.json()["results"][0]
            self.assertEqual(data["records"][0]["股票代码"], "000001")
            self.assertIsNone(data["records"][0]["收盘"])
            exported = self.client.post("/api/export", json=body)
            self.assertEqual(exported.status_code, 200)
            self.assertTrue(exported.content.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(query.call_count, 2)
        with patch("efinance.web.query_one", return_value=frame.iloc[:0]):
            data = self.client.post("/api/history", json=body).json()["results"][0]
            self.assertEqual(data["records"], [])
            self.assertIn("日期", data["columns"])

    def test_error_mapping(self):
        for error, status in [(ParameterError("bad"), 422), (SymbolNotFoundError("bad"), 404),
                              (RateLimitError("slow"), 429), (TimeoutError("slow"), 504), (ResponseError("bad"), 502)]:
            with patch("efinance.web.query_one", side_effect=error):
                response = self.client.post("/api/quote", json=dict(codes=["000001"]))
            self.assertEqual(response.status_code, status)

    def test_invalid_requests_before_upstream(self):
        with patch("efinance.web.query_one", side_effect=AssertionError("unexpected query")):
            for body in (dict(codes=["x"] * 11), dict(codes=[]), dict(codes=[1]),
                         dict(codes=["000001"], unknown=True), dict(codes=["000001"], beg="bad")):
                self.assertEqual(self.client.post("/api/history", json=body).status_code, 422)
            self.assertEqual(self.client.post("/api/history", content="{").status_code, 422)
            self.assertEqual(self.client.post("/api/export", json=[]).status_code, 422)

    def test_basic_auth_and_external_bind(self):
        from fastapi.testclient import TestClient
        from efinance.web import create_app
        with self.assertRaises(ParameterError):
            create_app(require_auth=True)
        with patch.dict(os.environ, {"EFINANCE_WEB_USERNAME": "test", "EFINANCE_WEB_PASSWORD": "secret"}):
            with TestClient(create_app(require_auth=True)) as client:
                self.assertEqual(client.get("/api/health").status_code, 401)
                for header in ("Basic !!!", "Bearer abc", "Basic " + base64.b64encode(b"bad:bad").decode()):
                    self.assertEqual(client.get("/", headers={"Authorization": header}).status_code, 401)
                self.assertEqual(client.get("/api/health", auth=("test", "secret")).status_code, 200)


if __name__ == "__main__":
    unittest.main()

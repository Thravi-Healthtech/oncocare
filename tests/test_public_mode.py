"""Hardening used when the server is exposed publicly (ONCOCARE_PUBLIC=1, e.g. onco.thravi.in)."""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend.api import make_handler
from backend.database import migrate, seed_demo

ROOT = Path(__file__).resolve().parents[1]


class PublicModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "public.db")
        migrate(cls.db_path)
        seed_demo(cls.db_path)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db_path, ROOT))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data

    def test_secret_and_data_paths_are_not_served(self) -> None:
        for path in ["/.env", "/data/oncocare.db", "/backend/api.py", "/.git/config", "/../.env", "/%2e%2e/.env", "/tests/test_api.py", "/README.md"]:
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertIn(status, (403, 404))

    def test_public_mode_sets_secure_cookie_and_locks_ai_settings(self) -> None:
        env = {"ONCOCARE_PUBLIC": "1", "ONCOCARE_DEV_PASSWORD": "a-strong-demo-pass"}
        with mock.patch.dict(os.environ, env):
            status, _, _ = self.request("POST", "/api/auth/login", {"username": "chemodoc", "password": "a-strong-demo-pass"})
            self.assertEqual(status, 401)  # the patient-portal password does not open clinician accounts
            status, headers, _ = self.request("POST", "/api/auth/login", {"username": "chemodoc", "password": "qwertyui"})
            self.assertEqual(status, 200)
            cookie = headers["Set-Cookie"]
            self.assertIn("Secure", cookie)
            self.assertIn("HttpOnly", cookie)
            token = cookie.split(";", 1)[0]
            status, _, body = self.request("POST", "/api/settings/ai", {"api_key": "x" * 30}, {"Cookie": token})
            self.assertEqual(status, 403)

    def test_local_mode_cookie_is_not_secure(self) -> None:
        with mock.patch.dict(os.environ, {"ONCOCARE_PUBLIC": "", "ONCOCARE_DEV_PASSWORD": "1234"}):
            status, headers, _ = self.request("POST", "/api/auth/login", {"username": "chemodoc", "password": "qwertyui"})
            self.assertEqual(status, 200)
            self.assertNotIn("Secure", headers["Set-Cookie"])

    def test_demo_banner_injected_into_pages_only(self) -> None:
        with mock.patch.dict(os.environ, {"ONCOCARE_DEMO_BANNER": "Demo <only> – synthetic data"}):
            for path in ["/clinician", "/portal"]:
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(b"Demo &lt;only&gt;", body)
            status, _, body = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertNotIn(b"Demo &lt;only&gt;", body)  # home page has its own beta notice
            _, _, css = self.request("GET", "/styles.css")
            self.assertNotIn(b"Demo", css[:200])
        _, _, body = self.request("GET", "/")
        self.assertNotIn(b"Demo &lt;only&gt;", body)


if __name__ == "__main__":
    unittest.main()

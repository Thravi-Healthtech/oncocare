from __future__ import annotations

import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from tests.test_radiotherapy import Base


class StaticDesignTests(Base):
    """Self-hosted Thravi fonts and logo are served; nothing else under /fonts is."""

    def get(self, path: str) -> tuple[int, str, bytes]:
        try:
            with urlopen(self.base + path) as res:
                return res.status, res.headers["Content-Type"], res.read()
        except HTTPError as error:
            return error.code, "", b""

    def test_fonts_and_logo(self) -> None:
        status, ctype, body = self.get("/fonts/fonts.css")
        self.assertEqual((status, ctype), (200, "text/css"))
        self.assertIn(b"Barlow Condensed", body)
        status, ctype, body = self.get("/fonts/barlow-condensed-latin-600-normal.woff2")
        self.assertEqual((status, ctype, body[:4]), (200, "font/woff2", b"wOF2"))
        self.assertEqual(self.get("/thravi-mark.png")[0], 200)
        for path in ("/fonts/OFL.txt", "/fonts/../backend/api.py", "/fonts/missing-latin-400-normal.woff2"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_pages_use_local_fonts(self) -> None:
        for path in ("/", "/clinician", "/portal"):
            body = self.get(path)[2].decode()
            self.assertIn('href="/fonts/fonts.css"', body, path)
            self.assertNotIn("fonts.googleapis.com", body, path)


if __name__ == "__main__":
    unittest.main()

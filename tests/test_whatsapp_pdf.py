from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from backend import pdfdoc, whatsapp
from backend.database import transaction
from tests.test_radiotherapy import Base

LIVE = {"WHATSAPP_MODE": "live", "WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1"}


class PdfWriterTests(unittest.TestCase):
    def test_wrap_and_pages(self) -> None:
        lines = pdfdoc.wrap("Dolo 650 (Paracetamol) when needed, up to three times a day " * 4, 10, 200)
        self.assertTrue(all(pdfdoc.text_width(line, 10) <= 200 for line in lines))
        doc = pdfdoc.Pdf("End-of-treatment summary", "Thravi OncoCare", "Period", "footer")
        for i in range(60):
            doc.table(["Test", "Value"], [(f"Row {i}", "≥ 1.5 °C — ok")])
        data = doc.bytes()
        self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))
        pages = int(re.search(rb"/Count (\d+)", data).group(1))
        self.assertGreater(pages, 1)
        self.assertEqual(data, doc.bytes())  # the footer is not added twice


class WhatsAppPdfTests(Base):
    """Discharge summaries and prescriptions reach the patient as PDF documents, not as a portal link."""

    def setUp(self) -> None:
        self.phone = "97" + str(abs(hash(self._testMethodName)) % 10**8).zfill(8)
        self.pid = self.patient(f"PDF-{self._testMethodName}", phone=self.phone)

    def final_summary(self) -> int:
        _, ds = self.call("POST", f"/api/patients/{self.pid}/discharge-summaries", {"kind": "end_of_treatment"})
        sid = ds["data"]["id"]
        self.call("PATCH", f"/api/discharge-summaries/{sid}", {"condition_at_discharge": "Stable", "course_text": "Completed treatment.",
                                                               "medications": [{"name": "Dolo 650", "dose": "650 mg", "frequency": "when needed"}],
                                                               "follow_up": {"date": "2030-01-10", "with": "Dr. R. Khanna"}})
        status, body = self.call("POST", f"/api/discharge-summaries/{sid}/finalise", {"confirmed": True})
        self.assertEqual(status, 200, body)
        return sid

    def send_live(self, message_id: int, window: bool) -> list:
        calls: list = []
        with mock.patch.dict(os.environ, LIVE), mock.patch.object(whatsapp, "window_open", return_value=window), \
                mock.patch.object(whatsapp, "_upload_media_live", side_effect=lambda path, ctype: calls.append(("upload", Path(path).read_bytes()[:5], ctype)) or "media-1"), \
                mock.patch.object(whatsapp, "_graph", side_effect=lambda path, body=None, **k: calls.append(("send", body)) or {"messages": [{"id": f"wamid.{len(calls)}"}]}):
            status, sent = self.call("POST", f"/api/whatsapp/messages/{message_id}/send", {})
        self.assertEqual((status, sent["data"]["status"]), (200, "sent"), sent)
        return calls

    def test_discharge_summary_is_sent_as_pdf(self) -> None:
        sid = self.final_summary()
        status, draft = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "discharge", "summary_id": sid})
        self.assertEqual(status, 201, draft)
        d = draft["data"]
        self.assertIn("attached as a PDF", d["body"])
        self.assertNotIn("/portal", d["body"])
        self.assertEqual(d["template_name"], "oncocare_document")
        self.assertTrue(d["meta"]["filename"].endswith(".pdf"))
        self.assertNotIn("pdf_path", d["meta"])  # the server path is never sent to the browser
        # Outside the 24-hour window: approved document template with the PDF as its header.
        calls = self.send_live(d["id"], window=False)
        self.assertEqual(calls[0], ("upload", b"%PDF-", "application/pdf"))
        template = calls[1][1]["template"]
        self.assertEqual(template["name"], "oncocare_document")
        self.assertEqual(template["components"][0]["parameters"][0]["document"], {"filename": d["meta"]["filename"], "id": "media-1"})
        self.assertIn("summary", template["components"][1]["parameters"][2]["text"].lower())

    def test_prescription_is_sent_as_pdf_inside_the_window(self) -> None:
        _, rx = self.call("POST", f"/api/patients/{self.pid}/prescriptions", {"items": [{"name": "Dolo 650", "dose": "650 mg", "frequency": "three times a day"}]})
        _, draft = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "prescription", "prescription_id": rx["data"]["id"]})
        d = draft["data"]
        self.assertTrue(d["meta"]["filename"].startswith("Prescription"))
        calls = self.send_live(d["id"], window=True)
        body = calls[1][1]
        self.assertEqual(body["type"], "document")
        self.assertIn("Dolo 650 650 mg", body["document"]["caption"])
        # Saving a prescription with "send on WhatsApp" goes the same way (simulator here).
        _, rx2 = self.call("POST", f"/api/patients/{self.pid}/prescriptions", {"items": [{"name": "Pantoprazole", "dose": "40 mg"}], "notify_whatsapp": True})
        with transaction(self.db) as connection:
            meta = json.loads(connection.execute("SELECT meta_json FROM whatsapp_messages WHERE id = ?", (rx2["data"]["whatsapp"][0]["message_id"],)).fetchone()["meta_json"])
        self.assertTrue(Path(meta["pdf_path"]).exists())

    def test_pdf_download_routes(self) -> None:
        sid = self.final_summary()
        _, rx = self.call("POST", f"/api/patients/{self.pid}/prescriptions", {"items": [{"name": "Dolo 650", "dose": "650 mg"}]})
        for path in (f"/api/discharge-summaries/{sid}/pdf", f"/api/prescriptions/{rx['data']['id']}/pdf"):
            with urlopen(self.base + path) as res:
                self.assertEqual(res.headers["Content-Type"], "application/pdf")
                self.assertTrue(res.read().startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import json
import os
import unittest
from datetime import date, timedelta
from unittest import mock

from backend import extraction, prescriptions, speech, whatsapp
from backend.database import transaction
from tests.test_radiotherapy import Base

ITEMS = [{"name": "Ondansetron", "dose": "4 mg", "route": "Oral", "frequency": "every 8 hours", "duration": "3 days", "instructions": "before food"},
         {"name": "Chlorhexidine mouthwash", "frequency": "twice a day", "duration": "until next cycle"}]


class PrescriptionTests(Base):
    def setUp(self) -> None:
        self.pid = self.patient(f"RX-{self._testMethodName}", phone="96" + str(abs(hash(self._testMethodName)) % 10**8).zfill(8))

    def test_write_print_cancel_and_portal(self) -> None:
        url = f"/api/patients/{self.pid}/prescriptions"
        self.assertEqual(self.call("POST", url, {"items": []})[0], 422)
        self.assertEqual(self.call("POST", url, {"items": ITEMS, "rx_date": (date.today() + timedelta(days=1)).isoformat()})[0], 422)
        self.assertEqual(self.call("POST", url, {"items": [{"dose": "4 mg"}]})[0], 422)  # a row without a medicine name
        status, body = self.call("POST", url, {"items": ITEMS, "context": "between_cycles", "indication": "Nausea after cycle 2",
                                              "advice": "Drink plenty of water", "consult_notes": "Nausea G2 since cycle 2.", "transcript": "Doctor: take ondansetron..."})
        self.assertEqual(status, 201, body)
        rx = body["data"]
        self.assertEqual((rx["status"], rx["context_label"], rx["source"], len(rx["items"])), ("active", "Between cycles", "conversation", 2))
        self.assertEqual(rx["prescribed_by"], "Dr. R. Khanna")
        status, page = self.call("GET", f"/api/prescriptions/{rx['id']}/print")
        self.assertEqual(status, 200)
        # Patient portal view: no consultation notes or transcript.
        portal_view = prescriptions.for_patient(str(self.db), self.pid)
        self.assertEqual([r["id"] for r in portal_view], [rx["id"]])
        self.assertNotIn("consult_notes", portal_view[0])
        self.assertNotIn("transcript", portal_view[0])
        # Timeline and agent facts include it.
        _, summary = self.call("GET", f"/api/patients/{self.pid}/summary")
        self.assertTrue(any(i["kind"] == "prescription" and "Ondansetron" in i["detail"] for i in summary["data"]["items"]))
        with transaction(self.db) as connection:
            patient = connection.execute("SELECT * FROM patients WHERE id = ?", (self.pid,)).fetchone()
            self.assertIn("Ondansetron 4 mg", whatsapp.record_facts(connection, patient))
        # Cancel needs a reason; cancelled prescriptions leave the portal.
        self.assertEqual(self.call("POST", f"/api/prescriptions/{rx['id']}/cancel", {})[0], 422)
        status, body = self.call("POST", f"/api/prescriptions/{rx['id']}/cancel", {"reason": "Wrong duration"})
        self.assertEqual((status, body["data"]["status"]), (200, "cancelled"))
        self.assertEqual(self.call("POST", f"/api/prescriptions/{rx['id']}/cancel", {"reason": "again"})[0], 409)
        self.assertEqual(prescriptions.for_patient(str(self.db), self.pid), [])

    def test_send_on_whatsapp(self) -> None:
        status, body = self.call("POST", f"/api/patients/{self.pid}/prescriptions", {"items": ITEMS, "notify_whatsapp": True, "advice": "Rest"})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["data"]["whatsapp"][0]["status"], "sent")
        _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
        msg = [m for m in thread["data"]["messages"] if m["kind"] == "prescription"][-1]
        self.assertIn("Ondansetron 4 mg", msg["body"])
        self.assertEqual((msg["related_type"], msg["related_id"]), ("prescription", body["data"]["id"]))
        self.assertFalse(any("\n" in p for p in msg["template_params"]))
        self.assertEqual([r["id"] for r in thread["data"]["prescriptions"]], [body["data"]["id"]])
        # Manual send of a chosen prescription.
        status, draft = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "prescription", "prescription_id": body["data"]["id"]})
        self.assertEqual(status, 201, draft)
        self.assertIn("Advice: Rest", draft["data"]["body"])

    def test_scribe_transcribe_and_draft(self) -> None:
        # Transcription goes to Sarvam; refused cleanly without a key.
        audio = base64.b64encode(b"x" * 3000).decode()
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": ""}):
            self.assertEqual(self.call("POST", "/api/scribe/transcribe", {"audio_base64": audio, "audio_type": "audio/webm"})[0], 503)
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "sk-test"}), \
                mock.patch.object(speech, "transcribe", return_value={"text": "Take ondansetron 4 mg three times a day", "lang": "en"}):
            status, body = self.call("POST", "/api/scribe/transcribe", {"audio_base64": audio, "audio_type": "audio/webm", "seq": 3})
        self.assertEqual((status, body["data"]["text"], body["data"]["seq"]), (200, "Take ondansetron 4 mg three times a day", 3))

        transcript = "Doctor: How is the nausea? Patient: Vomiting twice a day since the cycle. Doctor: Take ondansetron 4 mg three times a day for 3 days."
        draft = {"context": "between_cycles", "indication": "Nausea after cycle", "items": [{"name": "Ondansetron", "dose": "4 mg", "frequency": "three times a day", "duration": "3 days"}],
                 "advice": "Small frequent meals", "notes": "Vomiting twice daily since cycle.", "uncertain": ["Route not stated"]}
        calls = []

        def fake_post(key, body, timeout):
            calls.append(body["model"])
            if body["model"] == "claude-haiku-5-5":
                raise RuntimeError("Claude API HTTP 404: model: claude-haiku-5-5 not found")
            return {"content": [{"type": "text", "text": json.dumps(draft)}]}

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "ONCOCARE_EXTRACTION": "auto"}), \
                mock.patch.object(extraction, "_anthropic_post", side_effect=fake_post):
            self.assertEqual(self.call("POST", f"/api/patients/{self.pid}/prescriptions/draft", {"transcript": "too short"})[0], 422)
            status, body = self.call("POST", f"/api/patients/{self.pid}/prescriptions/draft", {"transcript": transcript})
        self.assertEqual(status, 200, body)
        d = body["data"]
        self.assertEqual(calls, ["claude-haiku-5-5", "claude-haiku-4-5-20251001"])  # falls back to an available Haiku
        self.assertEqual((d["context"], d["items"][0]["name"], d["items"][0]["route"], d["uncertain"], d["model"]),
                         ("between_cycles", "Ondansetron", "", ["Route not stated"], "claude-haiku-4-5-20251001"))
        # Nothing is saved until the doctor signs it.
        _, listing = self.call("GET", f"/api/patients/{self.pid}/prescriptions")
        self.assertEqual(listing["data"], [])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(self.call("POST", f"/api/patients/{self.pid}/prescriptions/draft", {"transcript": transcript})[0], 503)


if __name__ == "__main__":
    unittest.main()

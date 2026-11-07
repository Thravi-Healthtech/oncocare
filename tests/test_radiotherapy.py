from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ.setdefault("ONCOCARE_DAILYMED", "off")
os.environ.setdefault("ONCOCARE_CONTEXT_DIR", tempfile.mkdtemp(prefix="oncocare-context-"))
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("WHATSAPP_MODE", None)

from backend import whatsapp  # noqa: E402
from backend.api import make_handler  # noqa: E402
from backend.database import migrate, seed_demo, transaction  # noqa: E402
from backend import radiotherapy as rt  # noqa: E402

whatsapp.SYNC_TRIAGE = True


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "rt.db"
        migrate(cls.db)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db, Path(__file__).resolve().parents[1], require_auth=False))
        cls.server.sync_triage = True
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req) as res:
                body = res.read()
                if res.headers.get("Content-Type", "").startswith("application/json"):
                    return res.status, json.loads(body)
                return res.status, body.decode()
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def patient(self, mrn: str, phone: str | None = None) -> int:
        status, body = self.call("POST", "/api/patients", {"medical_record_number": mrn, "first_name": "Meera", "last_name": mrn,
                                                           "date_of_birth": "1970-01-01", "sex": "female", "diagnosis": "Synthetic carcinoma",
                                                           "phone": phone})
        self.assertEqual(status, 201, body)
        return body["data"]["id"]

    def course(self, pid: int, **extra) -> dict:
        payload = {"site": "Left breast", "site_group": "breast", "intent": "curative", "treatment_days": [1, 2, 3, 4, 5, 6],
                   "phases": [{"name": "Phase I", "technique": "VMAT", "energy": "6MV", "dose_per_fraction_cgy": 267, "fractions_planned": 15},
                              {"name": "Boost", "technique": "Electrons", "dose_per_fraction_cgy": 200, "fractions_planned": 5}],
                   "constraints": [{"structure": "Heart", "constraint_text": "Mean < 3 Gy", "achieved_text": "1.2 Gy", "met": "yes"}], **extra}
        status, body = self.call("POST", f"/api/patients/{pid}/rt-courses", payload)
        self.assertEqual(status, 201, body)
        return body["data"]


class RadiotherapyTests(Base):
    def test_course_requires_approval_and_tracks_running_totals(self) -> None:
        pid = self.patient("RT-1")
        w = self.course(pid)
        cid = w["course"]["id"]
        self.assertEqual(w["summary"]["prescription"], "40.05 Gy/15# + 10 Gy/5#")
        self.assertEqual(w["summary"]["dose_planned_cgy"], 5005)
        status, body = self.call("POST", f"/api/rt-courses/{cid}/fractions", {"therapist": "MK"})
        self.assertEqual(status, 422)
        self.assertIn("Approve", body["error"]["message"])
        status, body = self.call("POST", f"/api/rt-courses/{cid}/approve", {"physicist": "S. Iyer"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["course"]["status"], "active")
        # Two fractions on consecutive treatment days; the third date skips a weekday (a gap).
        day = date.today() - timedelta(days=10)
        while day.isoweekday() != 1:
            day += timedelta(days=1)
        for offset in (0, 1, 3):
            status, body = self.call("POST", f"/api/rt-courses/{cid}/fractions",
                                     {"treatment_date": (day + timedelta(days=offset)).isoformat(), "mu": 480, "therapist": "MK"})
            self.assertEqual(status, 201, body)
        w = body["data"]
        self.assertEqual([f["course_total_cgy"] for f in w["fractions"]], [267, 534, 801])
        self.assertEqual(w["summary"]["fractions_delivered"], 3)
        gaps = [g["date"] for g in w["summary"]["gaps"]]
        self.assertIn((day + timedelta(days=2)).isoformat(), gaps)
        self.assertTrue(any("without a recorded reason" in a["text"] for a in w["summary"]["attention"]))
        # Explain the gap.
        status, body = self.call("POST", f"/api/rt-courses/{cid}/breaks", {"break_date": (day + timedelta(days=2)).isoformat(), "category": "Machine downtime"})
        self.assertEqual(status, 201, body)
        explained = next(g for g in body["data"]["summary"]["gaps"] if g["date"] == (day + timedelta(days=2)).isoformat())
        self.assertTrue(explained["explained"])
        # Same-day duplicate needs confirmation; dose change needs a note.
        status, _ = self.call("POST", f"/api/rt-courses/{cid}/fractions", {"treatment_date": day.isoformat(), "therapist": "MK"})
        self.assertEqual(status, 409)
        status, body = self.call("POST", f"/api/rt-courses/{cid}/fractions", {"treatment_date": (day + timedelta(days=4)).isoformat(), "dose_cgy": 250, "therapist": "MK"})
        self.assertEqual(status, 422)
        status, _ = self.call("POST", f"/api/rt-courses/{cid}/fractions", {"treatment_date": (day + timedelta(days=4)).isoformat(), "therapist": ""})
        self.assertEqual(status, 422)

    def test_phase_rollover_and_completion(self) -> None:
        pid = self.patient("RT-2")
        w = self.course(pid, phases=[{"dose_per_fraction_cgy": 300, "fractions_planned": 2}, {"dose_per_fraction_cgy": 200, "fractions_planned": 1}])
        cid = w["course"]["id"]
        self.call("POST", f"/api/rt-courses/{cid}/approve", {"physicist": "S. Iyer"})
        start = date.today() - timedelta(days=6)
        dates = [start + timedelta(days=i) for i in range(7) if (start + timedelta(days=i)).isoweekday() <= 6][:3]
        for d in dates:
            status, body = self.call("POST", f"/api/rt-courses/{cid}/fractions", {"treatment_date": d.isoformat(), "therapist": "SR"})
            self.assertEqual(status, 201, body)
        w = body["data"]
        self.assertEqual([f["phase_number"] for f in w["fractions"]], [1, 1, 2])
        self.assertEqual(w["summary"]["dose_delivered_cgy"], 800)
        status, body = self.call("POST", f"/api/rt-courses/{cid}/fractions", {"treatment_date": date.today().isoformat(), "therapist": "SR"})
        self.assertEqual(status, 422)  # all delivered; extra fraction must be explicit
        status, body = self.call("POST", f"/api/rt-courses/{cid}/complete", {"outcome": "completed_as_planned"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["course"]["status"], "completed")
        self.assertEqual(body["data"]["course"]["completion"]["dose_delivered_cgy"], 800)
        status, _ = self.call("DELETE", f"/api/rt-fractions/{w['fractions'][0]['id']}")
        self.assertEqual(status, 422)

    def test_weekly_review_validation_decisions_and_weight(self) -> None:
        pid = self.patient("RT-3")
        cid = self.course(pid, baseline_weight_kg=80)["course"]["id"]
        self.call("POST", f"/api/rt-courses/{cid}/approve", {"physicist": "S. Iyer"})
        status, body = self.call("POST", f"/api/rt-courses/{cid}/reviews", {"items": [{"row_key": "skin", "ctcae_term": "Dermatitis radiation", "grade": 7}]})
        self.assertEqual(status, 422)
        status, body = self.call("POST", f"/api/rt-courses/{cid}/reviews", {"items": [], "decision": "pause"})
        self.assertEqual(status, 422)  # reason + rationale required
        status, body = self.call("POST", f"/api/rt-courses/{cid}/reviews", {
            "weight_kg": 71.5, "decision": "pause", "reason_category": "Toxicity", "rationale": "Moist desquamation beyond folds",
            "items": [{"row_key": "skin", "ctcae_term": "Dermatitis radiation", "grade": 3, "note": "Beyond folds"},
                      {"row_key": "general", "ctcae_term": "Fatigue", "grade": 1}]})
        self.assertEqual(status, 201, body)
        w = body["data"]
        self.assertEqual(w["course"]["status"], "on_hold")
        self.assertEqual(w["summary"]["weight_change"]["ctcae_grade"], 2)  # 10.6% loss
        texts = " ".join(a["text"] for a in w["summary"]["attention"])
        self.assertIn("Dermatitis radiation grade 3", texts)
        status, body = self.call("POST", f"/api/rt-courses/{cid}/fractions", {"therapist": "MK"})
        self.assertEqual(status, 422)  # paused
        status, _ = self.call("PATCH", f"/api/rt-courses/{cid}", {"status": "active"})
        self.assertEqual(status, 200)
        # Dashboard shows the RT toxicity and active course.
        _, listing = self.call("GET", "/api/patients?search=RT-3")
        row = listing["data"][0]
        self.assertEqual(row["open_toxicity_grade"], 3)
        self.assertEqual(row["active_rt_site"], "Left breast")

    def test_seeded_courses(self) -> None:
        seed_demo(self.db)
        with transaction(self.db) as connection:
            courses = connection.execute("SELECT id, site, status FROM rt_courses WHERE site LIKE 'Right breast%' OR site LIKE 'Pelvis%'").fetchall()
            self.assertEqual(len(courses), 2)
            for course in courses:
                w = rt.build(connection, course["id"])
                if course["status"] == "completed":
                    self.assertEqual(w["summary"]["dose_delivered_cgy"], 5000)
                else:
                    self.assertEqual(w["summary"]["fractions_delivered"], 19)
                    self.assertEqual(w["summary"]["dose_delivered_cgy"], 4850)
                    self.assertTrue(all(g["explained"] for g in w["summary"]["gaps"]))


class SummaryAndDischargeTests(Base):
    def setUp(self) -> None:
        self.pid = self.patient(f"DS-{self._testMethodName}", phone=None)
        w = self.course(self.pid, phases=[{"dose_per_fraction_cgy": 200, "fractions_planned": 2}])
        self.cid = w["course"]["id"]
        self.call("POST", f"/api/rt-courses/{self.cid}/approve", {"physicist": "S. Iyer"})
        self.day1 = date.today() - timedelta(days=3)
        for d in (self.day1, self.day1 + timedelta(days=1)):
            self.call("POST", f"/api/rt-courses/{self.cid}/fractions", {"treatment_date": d.isoformat(), "therapist": "MK", "allow_same_day": True})
        self.call("POST", f"/api/rt-courses/{self.cid}/reviews", {"review_date": (self.day1 + timedelta(days=1)).isoformat(), "weight_kg": 60,
                                                                  "items": [{"row_key": "skin", "ctcae_term": "Dermatitis radiation", "grade": 1}]})
        self.call("POST", f"/api/patients/{self.pid}/notes", {"text": "Counselled about skin care.", "category": "counselling"})

    def test_running_summary_has_cited_rule_narrative(self) -> None:
        status, body = self.call("GET", f"/api/patients/{self.pid}/summary")
        self.assertEqual(status, 200)
        kinds = {i["kind"] for i in body["data"]["items"]}
        self.assertTrue({"rt", "rt_review", "note"} <= kinds)
        self.assertIsNone(body["data"]["narrative"])
        status, body = self.call("POST", f"/api/patients/{self.pid}/summary/narrative")
        self.assertEqual(status, 200)
        narrative = body["data"]["narrative"]
        self.assertEqual(narrative["method"], "rules")
        self.assertEqual(narrative["unsupported"], 0, narrative["sentences"])
        self.assertIn("Dermatitis radiation G1", narrative["text"])

    def test_discharge_draft_edit_finalise_and_print(self) -> None:
        status, body = self.call("POST", f"/api/patients/{self.pid}/discharge-summaries", {"kind": "end_of_treatment"})
        self.assertEqual(status, 201, body)
        summary = body["data"]
        sid = summary["id"]
        self.assertEqual(summary["status"], "draft")
        self.assertEqual(summary["content"]["radiotherapy"][0]["dose_delivered_cgy"], 400)
        self.assertTrue(summary["content"]["course"]["text"])
        status, body = self.call("POST", f"/api/discharge-summaries/{sid}/finalise", {"confirmed": True})
        self.assertEqual(status, 422)
        self.assertIn("condition_at_discharge", body["error"]["details"]["fields"])
        status, body = self.call("PATCH", f"/api/discharge-summaries/{sid}", {
            "condition_at_discharge": "Stable, skin G1.", "advice": "Moisturiser twice daily.",
            "medications": [{"name": "Paracetamol", "dose": "650 mg", "frequency": "three times a day", "duration": "5 days"}, {"name": ""}],
            "follow_up": {"date": (date.today() + timedelta(days=14)).isoformat(), "with": "Dr. P. Rao"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["data"]["content"]["medications"]), 1)
        status, page = self.call("GET", f"/api/discharge-summaries/{sid}/print")
        self.assertEqual(status, 200)
        self.assertIn("DRAFT", page)
        status, body = self.call("POST", f"/api/discharge-summaries/{sid}/finalise", {"confirmed": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["status"], "final")
        status, _ = self.call("PATCH", f"/api/discharge-summaries/{sid}", {"advice": "x"})
        self.assertEqual(status, 409)
        status, page = self.call("GET", f"/api/discharge-summaries/{sid}/print")
        self.assertNotIn('class="draft"', page)
        self.assertIn("Paracetamol", page)
        self.assertNotIn("[S", page)  # citations stripped from the printed copy
        status, body = self.call("POST", f"/api/discharge-summaries/{sid}/revise")
        self.assertEqual(status, 201)
        self.assertEqual(body["data"]["version"], 2)
        new_id = body["data"]["id"]
        self.call("PATCH", f"/api/discharge-summaries/{new_id}", {"advice": "Updated advice"})
        self.call("POST", f"/api/discharge-summaries/{new_id}/finalise", {"confirmed": True})
        _, listing = self.call("GET", f"/api/patients/{self.pid}/discharge-summaries")
        self.assertEqual({s["status"] for s in listing["data"]}, {"final", "superseded"})

    def test_document_upload(self) -> None:
        import base64
        status, body = self.call("POST", f"/api/patients/{self.pid}/documents", {
            "filename": "ct.txt", "content_type": "text/plain", "doc_type": "imaging", "title": "CT thorax",
            "data_base64": base64.b64encode(b"CT thorax: no metastatic disease.").decode()})
        self.assertEqual(status, 201, body)
        self.assertIn("no metastatic", body["data"]["text_excerpt"])
        status, raw = self.call("GET", f"/api/documents/{body['data']['id']}/file")
        self.assertEqual(status, 200)
        _, summary = self.call("GET", f"/api/patients/{self.pid}/summary")
        self.assertTrue(any(i["kind"] == "document" for i in summary["data"]["items"]))


class WhatsAppTests(Base):
    def setUp(self) -> None:
        phone = "97" + str(abs(hash(self._testMethodName)) % 10**8).zfill(8)
        self.pid = self.patient(f"WA-{self._testMethodName}", phone=phone)
        self.phone = phone

    def say(self, text: str) -> dict:
        status, body = self.call("POST", "/api/whatsapp/simulate", {"patient_id": self.pid, "text": text})
        self.assertEqual(status, 201, body)
        return body["data"]

    def last_bot(self, thread: dict) -> str:
        return [m for m in thread["messages"] if m["direction"] == "out"][-1]["body"]

    def test_whatsapp_on_by_default_and_scheduled_reminder(self) -> None:
        _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
        self.assertEqual((thread["data"]["patient"]["wa_opt_in"], thread["data"]["patient"]["wa_opt_in_method"]), (1, "default"))
        visit = (date.today() + timedelta(days=5)).isoformat()
        status, body = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "reminder", "visit_date": visit, "visit_time": "10:30",
                                                                                       "purpose": "Review after radiotherapy"})
        self.assertEqual(status, 201, body)
        draft = body["data"]
        self.assertEqual(draft["status"], "draft")
        self.assertFalse(any(c["severity"] == "alert" for c in draft["checks"]))
        self.assertEqual([b["title"] for b in draft["buttons"]], ["Confirm visit", "Reschedule"])
        # A clinician can still turn WhatsApp off for one patient; sending is then refused.
        self.call("POST", f"/api/patients/{self.pid}/whatsapp/consent", {"opt_in": False})
        status, _ = self.call("POST", f"/api/whatsapp/messages/{draft['id']}/send", {})
        self.assertEqual(status, 422)
        status, _ = self.call("POST", f"/api/patients/{self.pid}/whatsapp/consent", {"opt_in": True, "method": "clinician_on", "language": "en"})
        self.assertEqual(status, 200)
        status, body = self.call("POST", f"/api/whatsapp/messages/{draft['id']}/send", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["status"], "scheduled")
        self.assertIn("10:30 AM", body["data"]["body"])
        self.assertIsNotNone(body["data"]["template_params"])
        self.assertFalse(any("\n" in p for p in body["data"]["template_params"]))
        # Make it due and run the scheduler.
        with transaction(self.db) as connection:
            connection.execute("UPDATE whatsapp_messages SET scheduled_for = '2000-01-01T00:00:00+00:00' WHERE id = ?", (draft["id"],))
        self.assertGreaterEqual(whatsapp.run_due(str(self.db))["sent"], 1)
        _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
        self.assertEqual(next(m for m in thread["data"]["messages"] if m["id"] == draft["id"])["status"], "sent")

    def send_reminder_now(self, days: int = 3) -> dict:
        visit = (date.today() + timedelta(days=days)).isoformat()
        _, body = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "reminder", "visit_date": visit, "visit_time": "09:15",
                                                                                 "purpose": "Follow-up after surgery", "send_now": True})
        status, sent = self.call("POST", f"/api/whatsapp/messages/{body['data']['id']}/send", {})
        self.assertEqual((status, sent["data"]["status"]), (200, "sent"), sent)
        return sent["data"]

    def test_reminder_confirm_books_appointment(self) -> None:
        reminder = self.send_reminder_now()
        status, body = self.call("POST", "/api/whatsapp/simulate", {"patient_id": self.pid, "button_id": reminder["buttons"][0]["id"]})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["data"]["action"], "visit_booked")
        self.assertIn("is booked", self.last_bot(body["data"]["thread"]))
        _, appts = self.call("GET", f"/api/patients/{self.pid}/appointments")
        self.assertEqual([(a["appt_date"], a["appt_time"], a["status"], a["source"]) for a in appts["data"]],
                         [(reminder["meta"]["visit_date"], "09:15", "booked", "whatsapp")])
        # Tapping again does not create a second appointment; "My schedule" now lists the booked visit.
        self.call("POST", "/api/whatsapp/simulate", {"patient_id": self.pid, "button_id": reminder["buttons"][0]["id"]})
        _, appts = self.call("GET", f"/api/patients/{self.pid}/appointments")
        self.assertEqual(len(appts["data"]), 1)
        _, body = self.call("POST", "/api/whatsapp/simulate", {"patient_id": self.pid, "button_id": "menu_schedule"})
        self.assertIn("Booked visits", self.last_bot(body["data"]["thread"]))
        # The clinician can mark it attended.
        status, body = self.call("PATCH", f"/api/appointments/{appts['data'][0]['id']}", {"status": "attended"})
        self.assertEqual((status, body["data"]["status"]), (200, "attended"))

    def test_reminder_reschedule_and_typed_confirm(self) -> None:
        reminder = self.send_reminder_now()
        result = self.say("Reschedule")
        self.assertEqual(result["action"], "visit_reschedule")
        _, appts = self.call("GET", f"/api/patients/{self.pid}/appointments")
        self.assertEqual(appts["data"][0]["status"], "reschedule_requested")
        # Their next message (the day that suits them) goes to the clinician inbox.
        self.assertEqual(self.say("Friday morning would suit me")["action"], "report")
        self.assertEqual(self.say("confirm")["action"], "visit_booked")
        _, appts = self.call("GET", f"/api/patients/{self.pid}/appointments")
        self.assertEqual([a["status"] for a in appts["data"]], ["booked"])
        self.assertEqual(appts["data"][0]["reminder_message_id"], reminder["id"])

    def test_clinician_books_appointment(self) -> None:
        day = (date.today() + timedelta(days=9)).isoformat()
        status, body = self.call("POST", f"/api/patients/{self.pid}/appointments", {"appt_date": day, "appt_time": "11:00", "purpose": "Review"})
        self.assertEqual((status, body["data"]["status"], body["data"]["source"]), (201, "booked", "clinician"))
        self.assertEqual(self.call("POST", f"/api/patients/{self.pid}/appointments", {"appt_date": "soon"})[0], 422)

    def test_send_document(self) -> None:
        import base64
        _, doc = self.call("POST", f"/api/patients/{self.pid}/documents", {
            "filename": "ct.pdf", "content_type": "application/pdf", "doc_type": "imaging", "title": "CT chest", "doc_date": "2026-11-02",
            "data_base64": base64.b64encode(b"%PDF-1.4 minimal").decode()})
        self.assertEqual(self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "document"})[0], 422)
        status, body = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "document", "document_id": doc["data"]["id"], "note": "Please bring it next visit."})
        self.assertEqual(status, 201, body)
        self.assertIn("CT chest (", body["data"]["body"])
        self.assertEqual(body["data"]["template_name"], "oncocare_document")
        status, sent = self.call("POST", f"/api/whatsapp/messages/{body['data']['id']}/send", {})
        self.assertEqual((status, sent["data"]["status"], sent["data"]["related_id"]), (200, "sent", doc["data"]["id"]))
        _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
        self.assertEqual([d["title"] for d in thread["data"]["documents"]], ["CT chest"])

    def test_live_reminder_and_document_payloads(self) -> None:
        """Outside the 24-hour window: template with quick-reply payloads / document header; the payload books the visit."""
        sent_bodies = []
        reminder = self.send_reminder_now()
        with transaction(self.db) as connection:
            msg = connection.execute("SELECT * FROM whatsapp_messages WHERE id = ?", (reminder["id"],)).fetchone()
            patient = connection.execute("SELECT * FROM patients WHERE id = ?", (self.pid,)).fetchone()
            with mock.patch.object(whatsapp, "_graph", side_effect=lambda path, body=None, **k: sent_bodies.append(body) or {"messages": [{"id": "wamid.T"}]}), \
                    mock.patch.object(whatsapp, "window_open", return_value=False):
                whatsapp._send_live(connection, msg, patient)
        template = sent_bodies[0]["template"]
        self.assertEqual(template["name"], "oncocare_visit_reminder_v2")
        payloads = [c["parameters"][0]["payload"] for c in template["components"] if c["type"] == "button"]
        self.assertEqual(payloads, [f"visit_yes:{reminder['id']}", f"visit_no:{reminder['id']}"])
        # Meta delivers the tap as a "button" message carrying that payload.
        event = {"entry": [{"changes": [{"value": {"messages": [{"from": "91" + self.phone, "id": "wamid.TAP", "type": "button",
                                                                 "button": {"payload": payloads[0], "text": "Confirm visit"}}]}}]}]}
        status, body = self.call("POST", "/api/whatsapp/webhook", event)
        self.assertEqual(status, 200)
        _, appts = self.call("GET", f"/api/patients/{self.pid}/appointments")
        self.assertEqual([a["status"] for a in appts["data"]], ["booked"])
        # A PDF document goes as the oncocare_document template with the file in the header.
        import base64
        _, doc = self.call("POST", f"/api/patients/{self.pid}/documents", {"filename": "scan.pdf", "content_type": "application/pdf", "title": "PET scan",
                                                                          "data_base64": base64.b64encode(b"%PDF-1.4 x").decode()})
        _, draft = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "document", "document_id": doc["data"]["id"]})
        sent_bodies.clear()
        with transaction(self.db) as connection:
            msg = connection.execute("SELECT * FROM whatsapp_messages WHERE id = ?", (draft["data"]["id"],)).fetchone()
            patient = connection.execute("SELECT * FROM patients WHERE id = ?", (self.pid,)).fetchone()
            with mock.patch.object(whatsapp, "_graph", side_effect=lambda path, body=None, **k: sent_bodies.append(body) or {"messages": [{"id": "wamid.D"}]}), \
                    mock.patch.object(whatsapp, "_upload_media_live", return_value="media-1"), \
                    mock.patch.object(whatsapp, "window_open", return_value=False):
                whatsapp._send_live(connection, msg, patient)
        header = sent_bodies[0]["template"]["components"][0]
        self.assertEqual((sent_bodies[0]["template"]["name"], header["type"], header["parameters"][0]["document"]),
                         ("oncocare_document", "header", {"id": "media-1", "filename": "scan.pdf"}))

    def test_red_flag_message_creates_report_and_emergency_reply(self) -> None:
        result = self.say("I have high fever and chills since morning")
        self.assertEqual(result["action"], "report")
        self.assertIn("fever", result["red_flags"])
        bodies = [m["body"] for m in result["thread"]["messages"] if m["direction"] == "out"]
        self.assertTrue(any("112" in b and "Do not wait" in b for b in bodies))
        _, reports = self.call("GET", f"/api/patients/{self.pid}/patient-reports")
        self.assertEqual(reports["data"][0]["source"], "whatsapp")
        self.assertTrue(reports["data"][0]["red_flags"])

    def test_guided_checkin_over_whatsapp(self) -> None:
        result = self.say("my skin is peeling where I get radiation")
        self.assertEqual(result["action"], "confirm")
        self.assertEqual(result["symptoms"], ["skin_reaction"])
        result = self.say("1")
        self.assertEqual(result["action"], "question")  # onset
        result = self.say("2")  # yesterday
        result = self.say("banana")  # not understood → retry
        self.assertEqual(result["action"], "retry")
        result = self.say("2")  # appearance: folds
        result = self.say("2")  # severity
        while result["action"] == "question":
            result = self.say("2" if result["question"][1] in ("fever", "bleeding", "breathing") else "1")
        self.assertEqual(result["action"], "checkin_submitted", result)
        _, reports = self.call("GET", f"/api/patients/{self.pid}/patient-reports")
        report = reports["data"][0]
        item = report["checkin"]["items"][0]
        self.assertEqual(item["term"], "Dermatitis radiation")
        self.assertEqual(item["grade_suggestion"], 2)
        self.assertEqual(item["onset"], (date.today() - timedelta(days=1)).isoformat())
        # Doctor replies from the inbox → goes out on WhatsApp.
        status, body = self.call("PATCH", f"/api/patient-reports/{report['id']}", {"status": "reviewed", "patient_message": "Apply the hydrogel dressing twice a day."})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["whatsapp"]["status"], "sent")
        _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
        self.assertIn("hydrogel", self.last_bot(thread["data"]))

    def test_stop_start_and_unknown_number(self) -> None:
        self.assertEqual(self.say("STOP")["action"], "opt_out")
        _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
        self.assertEqual(thread["data"]["patient"]["wa_opt_in"], 0)
        self.assertEqual(self.say("start")["action"], "opt_in")
        status, body = self.call("POST", "/api/whatsapp/simulate", {"wa_from": "919999999990", "text": "hello"})
        self.assertEqual(status, 201)
        self.assertFalse(body["data"]["matched"])

    def test_webhook_verification_and_inbound(self) -> None:
        os.environ["WHATSAPP_VERIFY_TOKEN"] = "secret-token"
        try:
            req = Request(self.base + "/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=secret-token&hub.challenge=12345")
            with urlopen(req) as res:
                self.assertEqual(res.read().decode(), "12345")
            with self.assertRaises(HTTPError):
                urlopen(Request(self.base + "/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1"))
        finally:
            os.environ.pop("WHATSAPP_VERIFY_TOKEN", None)
        payload = {"entry": [{"changes": [{"value": {"messages": [{"from": "91" + self.phone, "id": "wamid.TEST1", "type": "text",
                                                                     "text": {"body": "feeling very tired today"}}]}}]}]}
        status, body = self.call("POST", "/api/whatsapp/webhook", payload)
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/api/whatsapp/webhook", payload)  # duplicate delivery is ignored
        _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
        inbound = [m for m in thread["data"]["messages"] if m["direction"] == "in"]
        self.assertEqual(len(inbound), 1)

    def test_shared_number_ignores_other_numbers_and_unknown_senders(self) -> None:
        os.environ.update({"WHATSAPP_PHONE_NUMBER_ID": "111", "WHATSAPP_SHARED_NUMBER": "1"})
        try:
            def event(number_id: str, sender: str, wamid: str) -> dict:
                return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": number_id},
                                                          "messages": [{"from": sender, "id": wamid, "type": "text", "text": {"body": "bill?"}}]}}]}]}
            with transaction(self.db) as connection:
                before = connection.execute("SELECT COUNT(*) AS n FROM whatsapp_messages").fetchone()["n"]
            # another number on the same app: skipped entirely
            status, body = self.call("POST", "/api/whatsapp/webhook", event("222", "91" + self.phone, "wamid.OTHER"))
            self.assertEqual((status, body["received"]), (200, 0))
            # our number, sender not an OncoCare patient: neither stored nor answered
            status, body = self.call("POST", "/api/whatsapp/webhook", event("111", "919999999991", "wamid.STRANGER"))
            self.assertEqual(status, 200)
            with transaction(self.db) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM whatsapp_messages").fetchone()["n"], before)
            # our number, known patient: handled as usual
            self.call("POST", "/api/whatsapp/webhook", event("111", "91" + self.phone, "wamid.OURS"))
            _, thread = self.call("GET", f"/api/patients/{self.pid}/whatsapp")
            self.assertTrue(any(m.get("wa_message_id") == "wamid.OURS" for m in thread["data"]["messages"]))
        finally:
            for key in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_SHARED_NUMBER"):
                os.environ.pop(key, None)

    def test_share_plan_requires_active_treatment(self) -> None:
        self.call("POST", f"/api/patients/{self.pid}/whatsapp/consent", {"opt_in": True, "method": "in_clinic_verbal"})
        status, _ = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "plan"})
        self.assertEqual(status, 422)
        cid = self.course(self.pid)["course"]["id"]
        self.call("POST", f"/api/rt-courses/{cid}/approve", {"physicist": "S. Iyer"})
        status, body = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "plan"})
        self.assertEqual(status, 201, body)
        self.assertIn("20 sessions", body["data"]["body"])
        status, body = self.call("POST", f"/api/whatsapp/messages/{body['data']['id']}/send", {})
        self.assertEqual(body["data"]["status"], "sent")


if __name__ == "__main__":
    unittest.main()


class WhatsAppAgentTests(Base):
    def setUp(self) -> None:
        phone = "96" + str(abs(hash(self._testMethodName)) % 10**8).zfill(8)
        self.pid = self.patient(f"AG-{self._testMethodName}", phone=phone)
        self.call("POST", f"/api/patients/{self.pid}/whatsapp/consent", {"opt_in": True, "method": "in_clinic_written"})

    def send(self, **payload) -> dict:
        status, body = self.call("POST", "/api/whatsapp/simulate", {"patient_id": self.pid, **payload})
        self.assertEqual(status, 201, body)
        return body["data"]

    def outs(self, data: dict) -> list[dict]:
        return [m for m in data["thread"]["messages"] if m["direction"] == "out"]

    def test_menu_complaint_and_schedule_buttons(self) -> None:
        data = self.send(text="hi")
        self.assertEqual(data["action"], "menu")
        self.assertEqual([b["id"] for b in self.outs(data)[-1]["buttons"]], ["menu_complaint", "menu_checkin", "menu_schedule"])
        data = self.send(button_id="menu_complaint")
        self.assertEqual(data["action"], "complaint_prompt")
        data = self.send(text="The dressing on my scar is soaked")
        self.assertEqual(data["action"], "report")
        _, reports = self.call("GET", f"/api/patients/{self.pid}/patient-reports")
        self.assertEqual(reports["data"][0]["text"], "The dressing on my scar is soaked")
        self.assertEqual(reports["data"][0]["source"], "whatsapp")
        data = self.send(text="3")  # menu shown again after the acknowledgement? numbered reply only maps while the menu is open
        data = self.send(button_id="menu_schedule")
        self.assertEqual(data["action"], "schedule")
        self.assertIn("schedule", self.outs(data)[-1]["body"].lower())

    def test_checkin_button_goes_straight_to_questions(self) -> None:
        self.send(button_id="menu_checkin")
        data = self.send(text="vomiting since yesterday")
        self.assertEqual(data["action"], "question")
        self.assertEqual(data["question"], ["vomiting", "onset"])

    def test_voice_note_transcribed_and_answered_by_voice(self) -> None:
        import base64
        from unittest import mock
        from backend import speech
        with mock.patch.object(speech, "status", return_value={"enabled": True}), \
             mock.patch.object(speech, "transcribe", return_value={"text": "నాకు జ్వరం వచ్చింది", "lang": "te", "language_code": "te-IN"}), \
             mock.patch.object(speech, "synthesize", return_value=(b"ID3fake", "audio/mpeg")):
            data = self.send(audio_base64=base64.b64encode(b"OggSfake").decode(), audio_type="audio/ogg")
        self.assertEqual(data["transcript"], "నాకు జ్వరం వచ్చింది")
        self.assertIn("fever", data["red_flags"])
        out = self.outs(data)
        self.assertTrue(any("112" in m["body"] for m in out))
        self.assertTrue(all(m["meta"]["voice"] == "te" for m in out[-2:]))
        self.assertTrue(out[-1]["has_audio"])
        req = Request(self.base + f"/api/whatsapp/messages/{out[-1]['id']}/media")
        with urlopen(req) as res:
            self.assertEqual(res.read(), b"ID3fake")
        _, reports = self.call("GET", f"/api/patients/{self.pid}/patient-reports")
        self.assertTrue(reports["data"][0]["has_audio"])
        self.assertEqual(reports["data"][0]["input_mode"], "voice")

    def test_voice_note_with_browser_transcript_when_speech_is_off(self) -> None:
        import base64
        data = self.send(audio_base64=base64.b64encode(b"webmfake").decode(), audio_type="audio/webm", client_transcript="hard to swallow food")
        self.assertEqual(data["action"], "confirm")
        self.assertEqual(data["symptoms"], ["swallowing"])

    def test_agent_reply_is_guarded(self) -> None:
        from unittest import mock
        from backend import agent
        risky = {"intent": "question", "symptoms": [], "reply": "Take 1000 mg paracetamol now, it is normal.", "note_for_doctor": "asks about pain", "model": "test"}
        with mock.patch.object(agent, "available", return_value=True), mock.patch.object(agent, "decide", return_value=dict(risky)):
            data = self.send(text="can I take something for the pain?")
        reply = self.outs(data)[-1]
        self.assertEqual(reply["kind"], "agent")
        self.assertEqual(reply["body"], agent.HANDOFF["en"])
        self.assertTrue(reply["meta"]["guard"].startswith("blocked"))
        complaint = {"intent": "complaint", "symptoms": [], "reply": "Thank you, I have shared this with your care team.", "note_for_doctor": "itchy skin", "model": "test"}
        with mock.patch.object(agent, "available", return_value=True), mock.patch.object(agent, "decide", return_value=dict(complaint)):
            data = self.send(text="my skin itches at night")
        self.assertEqual(data["action"], "report")
        self.assertEqual(self.outs(data)[-1]["body"], complaint["reply"])
        checkin = {"intent": "checkin", "symptoms": ["skin_reaction"], "reply": "Sorry to hear that. A few questions follow.", "note_for_doctor": "", "model": "test"}
        with mock.patch.object(agent, "available", return_value=True), mock.patch.object(agent, "decide", return_value=dict(checkin)):
            data = self.send(text="the treated skin looks raw")
        self.assertEqual(data["question"], ["skin_reaction", "onset"])

    def test_guard_rules(self) -> None:
        from backend import agent
        facts = "Medicines prescribed: Paracetamol 650 mg three times a day"
        self.assertEqual(agent.guard("Your prescription says Paracetamol 650 mg three times a day.", facts, "en")[1], "passed")
        self.assertTrue(agent.guard("Take 500 mg ibuprofen.", facts, "en")[1].startswith("blocked"))
        self.assertTrue(agent.guard("Don't worry, this is normal.", facts, "en")[1].startswith("blocked"))
        self.assertTrue(agent.guard("x" * 800, facts, "en")[1].startswith("blocked"))

    def test_doctor_actions_reach_whatsapp(self) -> None:
        # Radiotherapy approval sends the plan.
        cid = self.course(self.pid)["course"]["id"]
        status, body = self.call("POST", f"/api/rt-courses/{cid}/approve", {"physicist": "S. Iyer", "notify_whatsapp": True})
        self.assertEqual(body["data"]["whatsapp"][0]["status"], "sent")
        # A chemotherapy plan sends the plan and a reminder for cycle 1.
        start = (date.today() + timedelta(days=5)).isoformat()
        status, body = self.call("POST", f"/api/patients/{self.pid}/treatment-plans", {
            "regimen_name": "Weekly cisplatin", "notify_whatsapp": True, "medications": [{"drug_name": "Cisplatin", "dose": 40, "dose_unit": "mg/m2"}],
            "cycles": [{"cycle_number": 1, "planned_date": start, "medications": [{"drug_name": "Cisplatin", "planned_dose": 40, "dose_unit": "mg/m2"}]}]})
        self.assertEqual(status, 201, body)
        self.assertEqual([w["status"] for w in body["data"]["whatsapp"]], ["sent", "scheduled"])
        # Finalising a discharge summary sends summary + prescription and schedules the follow-up reminder.
        _, ds = self.call("POST", f"/api/patients/{self.pid}/discharge-summaries", {})
        sid = ds["data"]["id"]
        self.call("PATCH", f"/api/discharge-summaries/{sid}", {"condition_at_discharge": "Stable", "medications": [{"name": "Ondansetron", "dose": "4 mg"}],
                                                              "follow_up": {"date": (date.today() + timedelta(days=10)).isoformat()}})
        status, body = self.call("POST", f"/api/discharge-summaries/{sid}/finalise", {"confirmed": True, "notify_whatsapp": True})
        self.assertEqual(status, 200, body)
        self.assertEqual([(w["kind"], w["status"]) for w in body["data"]["whatsapp"]],
                         [("discharge", "sent"), ("prescription", "sent"), ("reminder", "scheduled")])
        # WhatsApp turned off for the patient: skipped with a reason, nothing sent.
        self.call("POST", f"/api/patients/{self.pid}/whatsapp/consent", {"opt_in": False})
        status, body = self.call("PATCH", f"/api/rt-courses/{cid}", {"status": "on_hold", "status_reason": "Skin", "notify_whatsapp": True})
        self.assertEqual(body["data"]["whatsapp"][0]["status"], "skipped")


class SurgeryTests(Base):
    def test_surgical_case_flow(self) -> None:
        pid = self.patient("SX-1", phone="9512345601")
        self.call("POST", f"/api/patients/{pid}/whatsapp/consent", {"opt_in": True, "method": "in_clinic_written"})
        day = (date.today() + timedelta(days=4)).isoformat()
        status, body = self.call("POST", f"/api/patients/{pid}/surgeries", {
            "procedure_planned": "Modified radical mastectomy", "intent": "curative", "surgeon": "Dr. A. Menon", "surgery_date": day,
            "admission_date": (date.today() + timedelta(days=3)).isoformat(), "preop": {"checks": {"consent": True}, "asa": "II", "fasting_from": "10 pm"},
            "notify_whatsapp": True})
        self.assertEqual(status, 201, body)
        w = body["data"]
        cid = w["case"]["id"]
        self.assertEqual(w["case"]["status"], "scheduled")
        self.assertEqual([x["status"] for x in w["whatsapp"]], ["sent", "scheduled"])
        self.assertTrue(any("Pre-op checklist: 6 item(s) open; surgery in 4 days" in a["text"] for a in w["summary"]["attention"]))
        _, thread = self.call("GET", f"/api/patients/{pid}/whatsapp")
        self.assertIn("Nothing to eat or drink from 10 pm", [m for m in thread["data"]["messages"] if m["kind"] == "plan"][0]["body"])
        # Post-op notes need the operation note first; the operation note must be signed.
        status, _ = self.call("POST", f"/api/surgeries/{cid}/events", {"text": "POD 1 fine"})
        self.assertEqual(status, 422)
        today = date.today().isoformat()
        status, _ = self.call("POST", f"/api/surgeries/{cid}/operation", {"operation_date": today, "procedure_performed": "MRM", "surgeon": "Dr. A. Menon"})
        self.assertEqual(status, 422)
        status, body = self.call("POST", f"/api/surgeries/{cid}/operation", {"operation_date": today, "procedure_performed": "Right MRM", "surgeon": "Dr. A. Menon",
                                                                            "approach": "Open", "ebl_ml": "150", "signed": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["case"]["status"], "completed")
        status, _ = self.call("POST", f"/api/surgeries/{cid}/events", {"kind": "complication", "text": "Flap edge necrosis", "complication": "Flap necrosis"})
        self.assertEqual(status, 422)  # needs Clavien-Dindo
        status, body = self.call("POST", f"/api/surgeries/{cid}/events", {"kind": "complication", "text": "Debrided under GA", "complication": "Flap necrosis", "clavien_dindo": "IIIb"})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["data"]["summary"]["worst_clavien_dindo"], "IIIb")
        status, _ = self.call("POST", f"/api/surgeries/{cid}/pathology", {"histology": "IDC", "nodes_examined": 2, "nodes_positive": 5})
        self.assertEqual(status, 422)
        status, body = self.call("POST", f"/api/surgeries/{cid}/pathology", {"histology": "IDC", "pt": "pT2", "pn": "pN0", "margins": "positive", "nodes_examined": 12, "nodes_positive": 0})
        self.assertTrue(any("Positive resection margin" in a["text"] for a in body["data"]["summary"]["attention"]))
        status, body = self.call("POST", f"/api/surgeries/{cid}/discharge", {"condition": "Stable", "follow_up_date": (date.today() + timedelta(days=7)).isoformat(), "notify_whatsapp": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["case"]["status"], "discharged")
        self.assertEqual(body["data"]["whatsapp"][0]["status"], "scheduled")
        # Patient record lists every modality; portal sees the operation in plain language.
        _, patient = self.call("GET", f"/api/patients/{pid}")
        self.assertEqual(patient["data"]["surgeries"][0]["stage"], "pT2 pN0")

    def test_cancel_needs_reason(self) -> None:
        pid = self.patient("SX-2")
        cid = self.call("POST", f"/api/patients/{pid}/surgeries", {"procedure_planned": "Whipple"})[1]["data"]["case"]["id"]
        self.assertEqual(self.call("PATCH", f"/api/surgeries/{cid}", {"status": "cancelled"})[0], 422)
        status, body = self.call("PATCH", f"/api/surgeries/{cid}", {"status": "cancelled", "cancel_reason": "Metastatic on staging PET"})
        self.assertEqual(body["data"]["case"]["status"], "cancelled")

    def test_discharge_summary_activity_with_notes(self) -> None:
        pid = self.patient("SX-3")
        today = date.today().isoformat()
        cid = self.call("POST", f"/api/patients/{pid}/surgeries", {"procedure_planned": "Wide local excision", "surgery_date": today})[1]["data"]["case"]["id"]
        self.call("POST", f"/api/surgeries/{cid}/operation", {"operation_date": today, "procedure_performed": "Wide local excision", "surgeon": "Dr. A", "signed": True})
        self.call("POST", f"/api/surgeries/{cid}/events", {"text": "Comfortable, wound dry.", "pain_score": 2})
        self.call("POST", f"/api/patients/{pid}/notes", {"text": "Family counselled about histopathology timelines.", "category": "counselling"})
        _, ds = self.call("POST", f"/api/patients/{pid}/discharge-summaries", {"period_from": today})
        content = ds["data"]["content"]
        self.assertEqual(content["surgery"][0]["procedure"], "Wide local excision")
        kinds = [a["kind"] for a in content["activity"]]
        self.assertIn("note", kinds)
        self.assertIn("surgery_note", kinds)
        self.assertEqual([a["date"] for a in content["activity"]], sorted(a["date"] for a in content["activity"]))
        note_key = next(a["key"] for a in content["activity"] if a["kind"] == "note")
        sid = ds["data"]["id"]
        self.call("PATCH", f"/api/discharge-summaries/{sid}", {"activity_excluded": [note_key]})
        _, regen = self.call("POST", f"/api/discharge-summaries/{sid}/regenerate", {})
        self.assertFalse(next(a for a in regen["data"]["content"]["activity"] if a["key"] == note_key)["include"])
        _, page = self.call("GET", f"/api/discharge-summaries/{sid}/print")
        self.assertIn("Activity during this period", page)
        self.assertIn("Comfortable, wound dry.", page)
        self.assertNotIn("Family counselled", page)

    def test_default_period_starts_at_tumour_board_before_surgery(self) -> None:
        pid = self.patient("SX-4")
        op_day = date.today() - timedelta(days=20)
        cid = self.call("POST", f"/api/patients/{pid}/surgeries", {"procedure_planned": "Hemicolectomy", "surgery_date": op_day.isoformat(),
                                                                   "admission_date": (op_day - timedelta(days=1)).isoformat()})[1]["data"]["case"]["id"]
        self.call("POST", f"/api/surgeries/{cid}/operation", {"operation_date": op_day.isoformat(), "procedure_performed": "Right hemicolectomy", "surgeon": "Dr. A", "signed": True})
        board = (op_day - timedelta(days=10)).isoformat()
        self.call("POST", f"/api/patients/{pid}/notes", {"text": "Tumour board: proceed to surgery.", "category": "tumour_board", "note_date": board})
        _, ds = self.call("POST", f"/api/patients/{pid}/discharge-summaries", {})
        self.assertEqual(ds["data"]["period_from"], board)
        titles = [a["title"] for a in ds["data"]["content"]["activity"]]
        self.assertTrue(titles[0].startswith("Tumour board"), titles)
        self.assertTrue(any(t.startswith("Operation: Right hemicolectomy") for t in titles), titles)

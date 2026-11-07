from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

os.environ.setdefault("ONCOCARE_DAILYMED", "off")
os.environ.setdefault("ONCOCARE_CONTEXT_DIR", tempfile.mkdtemp(prefix="oncocare-context-"))
os.environ.pop("ANTHROPIC_API_KEY", None)

from backend.api import make_handler  # noqa: E402
from backend.database import migrate  # noqa: E402
from backend import extraction  # noqa: E402
from backend.portal import _keyword_candidates, detect_red_flags  # noqa: E402


class PortalTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "portal.db"
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

    def client(self):
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, opener, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as res:
                body = res.read()
                return res.status, (json.loads(body) if res.headers.get("Content-Type", "").startswith("application/json") else body), res.headers
        except HTTPError as error:
            return error.code, json.loads(error.read()), error.headers

    def make_patient(self, mrn: str, phone: str | None = None) -> tuple[int, int]:
        doctor = self.client()
        _, p, _ = self.call(doctor, "POST", "/api/patients", {"medical_record_number": mrn, "first_name": "Pat", "last_name": mrn,
                                                               "phone": phone or f"98{abs(hash(mrn)) % 10**8:08d}"})
        pid = p["data"]["id"]
        _, plan, _ = self.call(doctor, "POST", f"/api/patients/{pid}/treatment-plans", {
            "regimen_name": "Cisplatin q21d", "start_date": "2026-08-01", "medications": [{"drug_name": "Cisplatin", "dose": 75, "dose_unit": "mg/m2"}],
            "cycles": [{"cycle_number": n, "planned_date": d, "medications": [{"drug_name": "Cisplatin", "planned_dose": 75, "dose_unit": "mg/m2"}]}
                       for n, d in ((1, "2026-08-01"), (2, "2026-08-22"), (3, "2099-01-01"))]})
        cycles = self.call(doctor, "GET", f"/api/treatment-plans/{plan['data']['id']}/cycles")[1]["data"]
        self.call(doctor, "POST", f"/api/cycles/{cycles[0]['id']}/labs", {"collection_date": "2026-07-31", "results": [
            {"test_name": "Creatinine", "value": 0.8, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.1}]})
        self.call(doctor, "POST", f"/api/cycles/{cycles[1]['id']}/labs", {"collection_date": "2026-08-21", "results": [
            {"test_name": "Creatinine", "value": 1.4, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.1},
            {"test_name": "Magnesium", "value": 1.5, "unit": "mg/dL", "ref_low": 1.7, "ref_high": 2.2, "verification_status": "unverified"}]})
        return pid, plan["data"]["id"]

    def test_phone_login_and_read_only_views(self) -> None:
        pid, _ = self.make_patient("PORTAL-1", "+91 98765 43210")
        doctor, patient = self.client(), self.client()
        _, listed, _ = self.call(doctor, "GET", f"/api/patients/{pid}")
        self.assertEqual(listed["data"]["phone"], "9876543210")
        self.assertEqual(self.call(patient, "GET", "/api/portal/plans")[0], 401)
        self.assertEqual(self.call(patient, "POST", "/api/portal/login", {"phone": "9876543210", "password": "0000"})[0], 401)
        self.assertEqual(self.call(patient, "POST", "/api/portal/login", {"phone": "9999999999", "password": "1234"})[0], 401)
        status, me, headers = self.call(patient, "POST", "/api/portal/login", {"phone": "098765 43210", "password": "1234"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(me["data"]["id"], pid)
        _, plans, _ = self.call(patient, "GET", "/api/portal/plans")
        plan = plans["data"][0]
        self.assertEqual([c["status_label"] for c in plan["cycles"]], ["Scheduled"] * 3)
        self.assertEqual(plan["next_cycle"]["cycle_number"], 3)
        _, labs, _ = self.call(patient, "GET", "/api/portal/labs")
        values = [v for r in labs["data"]["reports"] for v in r["values"]]
        self.assertEqual({v["test_name"] for v in values}, {"Creatinine"})  # unverified magnesium hidden
        self.assertEqual(next(v for v in values if v["value"] == 1.4)["flag"], "high")
        self.assertNotIn("ctcae_grade", json.dumps(labs))
        # sessions end when access is disabled, and sign-in is refused until re-enabled
        self.call(doctor, "DELETE", f"/api/patients/{pid}/portal")
        self.assertEqual(self.call(patient, "GET", "/api/portal/plans")[0], 401)
        self.assertEqual(self.call(patient, "POST", "/api/portal/login", {"phone": "9876543210", "password": "1234"})[0], 403)
        _, status_body, _ = self.call(doctor, "POST", f"/api/patients/{pid}/portal")
        self.assertTrue(status_body["data"]["can_sign_in"])
        self.assertEqual(self.call(patient, "POST", "/api/portal/login", {"phone": "9876543210", "password": "1234"})[0], 200)
        # phone numbers are unique across patients
        status, _, _ = self.call(doctor, "POST", "/api/patients", {"medical_record_number": "DUP-PHONE", "first_name": "A", "last_name": "B", "phone": "9876543210"})
        self.assertEqual(status, 409)

    def test_no_lockout_after_failed_attempts(self) -> None:
        self.make_patient("PORTAL-2", "9111111112")
        patient = self.client()
        for _ in range(6):
            self.assertEqual(self.call(patient, "POST", "/api/portal/login", {"phone": "9111111112", "password": "9999"})[0], 401)
        self.assertEqual(self.call(patient, "POST", "/api/portal/login", {"phone": "9111111112", "password": "1234"})[0], 200)

    def test_symptom_report_reaches_doctor_and_reply_reaches_patient(self) -> None:
        pid, plan_id = self.make_patient("PORTAL-3", "9222222223")
        self.make_patient("PORTAL-4", "9222222224")
        doctor, patient, other = self.client(), self.client(), self.client()
        self.call(patient, "POST", "/api/portal/login", {"phone": "9222222223", "password": "1234"})
        self.call(other, "POST", "/api/portal/login", {"phone": "9222222224", "password": "1234"})
        audio = base64.b64encode(b"\x1aE\xdf\xa3fake-webm").decode()
        status, sent, _ = self.call(patient, "POST", "/api/portal/reports", {
            "text": "Tingling in my fingers and I can't button my shirt. Some ringing in the ears.", "input_mode": "voice",
            "language": "en-IN", "symptoms": ["tingling", "ringing_ears"], "daily_impact": "some", "onset_date": "2026-08-24",
            "audio_base64": audio, "audio_type": "audio/webm;codecs=opus"})
        self.assertEqual(status, 201)
        self.assertEqual(sent["data"]["red_flags"], [])
        self.assertIsNone(sent["data"]["emergency_message"])
        status, urgent, _ = self.call(patient, "POST", "/api/portal/reports", {"text": "Feeling shivery", "temperature": 38.4, "symptoms": ["fever"]})
        self.assertEqual(status, 201)
        self.assertTrue(urgent["data"]["emergency_message"])
        self.assertEqual(urgent["data"]["red_flags"][0]["code"], "fever")
        # doctor inbox: urgent first, AI/keyword suggestions present, audio playable
        _, inbox, _ = self.call(doctor, "GET", "/api/patient-reports?status=open")
        mine = [r for r in inbox["data"] if r["patient_id"] == pid]
        self.assertEqual(mine[0]["id"], urgent["data"]["id"])
        report = next(r for r in mine if r["id"] == sent["data"]["id"])
        self.assertEqual(report["cycle_number"], 3)
        self.assertEqual({c["term"] for c in report["ai"]["candidates"]} >= {"Peripheral sensory neuropathy", "Tinnitus"}, True)
        self.assertIn("instrumental ADL", report["impact_text"])
        # the clinician sees the record context the triage used: last dose, verified labs only
        ctx = report["context"]
        self.assertEqual((ctx["last_given"], ctx["last_due"]["cycle"], ctx["next_cycle"]["cycle"]), (None, 2, 3))
        self.assertEqual([(l["test"], l["flag"], l["previous"]) for l in ctx["labs"]], [("Creatinine", "high", 0.8)])
        status, again, _ = self.call(doctor, "POST", f"/api/patient-reports/{report['id']}/triage", {})
        self.assertEqual((status, again["data"]["ai_status"]), (200, "done"))
        self.assertEqual(again["data"]["ai"]["context_used"]["last_due"]["cycle"], 2)
        status, body, headers = self.call(doctor, "GET", f"/api/patient-reports/{report['id']}/audio")
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/webm"))
        self.assertEqual(self.call(other, "GET", f"/api/portal/reports/{report['id']}/audio")[0], 404)
        _, longitudinal, _ = self.call(doctor, "GET", f"/api/treatment-plans/{plan_id}/longitudinal")
        c3 = longitudinal["data"]["cycles"][2]
        self.assertEqual(c3["attention"][0]["kind"], "patient_report")
        self.assertEqual(c3["attention"][0]["level"], "alert")
        # doctor records it as a CTCAE toxicity and replies
        _, symptom, _ = self.call(doctor, "POST", f"/api/patients/{pid}/symptoms", {
            "ctcae_term": "Peripheral sensory neuropathy", "grade": 2, "onset_date": "2026-08-24", "confirmed_by": "Dr. Test", "cycle_id": report["cycle_id"]})
        status, reviewed, _ = self.call(doctor, "PATCH", f"/api/patient-reports/{report['id']}", {
            "status": "recorded", "symptom_id": symptom["data"]["id"], "patient_message": "Thank you. We will review your dose at the next visit.", "doctor_note": "G2 neuropathy"})
        self.assertEqual((status, reviewed["data"]["status"], reviewed["data"]["symptom_ids"]), (200, "recorded", [symptom["data"]["id"]]))
        _, mine_now, _ = self.call(patient, "GET", "/api/portal/reports")
        seen = next(r for r in mine_now["data"] if r["id"] == report["id"])
        self.assertEqual(seen["status_label"], "Reviewed and added to your record")
        self.assertEqual(seen["patient_message"], "Thank you. We will review your dose at the next visit.")
        self.assertNotIn("doctor_note", seen)
        self.assertNotIn("ai", seen)
        _, other_reports, _ = self.call(other, "GET", "/api/portal/reports")
        self.assertEqual(other_reports["data"], [])
        _, listed, _ = self.call(doctor, "GET", "/api/patients?search=PORTAL-3")
        self.assertEqual(listed["data"][0]["open_reports"], 1)

    def test_structured_checkin_flow(self) -> None:
        pid, _ = self.make_patient("PORTAL-CI", "9222222230")
        doctor, patient = self.client(), self.client()
        self.call(patient, "POST", "/api/portal/login", {"phone": "9222222230", "password": "1234"})
        status, bank, _ = self.call(patient, "GET", "/api/portal/checkin/bank")
        self.assertEqual(status, 200)
        vomiting = next(s for s in bank["data"]["symptoms"] if s["key"] == "vomiting")
        self.assertTrue(all(set(q["text"]) == {"en", "hi", "te"} for q in vomiting["questions"]))
        self.assertIn("vomiting", bank["data"]["common"])  # cisplatin
        self.assertEqual(len(bank["data"]["safety"]), 5)
        _, found, _ = self.call(patient, "POST", "/api/portal/checkin/detect", {"text": "నాకు వాంతులు ఎక్కువ అవుతున్నాయి, tired all day"})
        self.assertEqual(set(found["data"]["symptoms"]), {"vomiting", "tiredness"})
        status, heard, _ = self.call(patient, "POST", "/api/portal/checkin/interpret", {"symptom": "vomiting", "question": "episodes", "heard": "तीन बार", "lang": "hi"})
        self.assertEqual((status, heard["data"]["value"], heard["data"]["label"]), (200, "3-5", "3–5 बार"))
        self.assertEqual(self.call(patient, "POST", "/api/portal/checkin/interpret", {"symptom": "vomiting", "question": "nope", "heard": "x"})[0], 422)
        safe = {"fever": "no", "fluids": "yes", "urine": "yes", "bleeding": "no", "breathing": "no"}
        checkin = {"symptoms": ["vomiting", "tiredness"], "safety": safe, "answers": {
            "vomiting": {"onset": "2026-08-24", "episodes": "3-5", "fluids": "some", "treatment": "iv", "frequency": "frequently"},
            "tiredness": {"severity": "mild", "rest_helps": "yes", "interference": "none"}}}
        self.assertEqual(self.call(patient, "POST", "/api/portal/reports", {"checkin": {**checkin, "safety": {"fever": "no"}}})[0], 422)
        bad = {**checkin, "answers": {**checkin["answers"], "vomiting": {"episodes": "lots"}}}
        self.assertEqual(self.call(patient, "POST", "/api/portal/reports", {"checkin": bad})[0], 422)
        status, sent, _ = self.call(patient, "POST", "/api/portal/reports", {"text": "వాంతులు", "ui_language": "te", "checkin": checkin})
        self.assertEqual((status, sent["data"]["red_flags"], sent["data"]["is_checkin"]), (201, [], True))
        _, inbox, _ = self.call(doctor, "GET", f"/api/patients/{pid}/patient-reports")
        report = inbox["data"][0]
        items = {i["term"]: i for i in report["checkin"]["items"]}
        self.assertEqual((items["Vomiting"]["grade_suggestion"], items["Fatigue"]["grade_suggestion"]), (2, 1))
        self.assertEqual(items["Vomiting"]["onset"], "2026-08-24")
        self.assertEqual(report["onset_date"], "2026-08-24")
        self.assertEqual(report["ui_language"], "te")
        self.assertEqual(report["daily_impact"], "none")
        self.assertEqual(report["cycle_number"], 3)
        # safety answers raise red flags and the emergency message
        _, urgent, _ = self.call(patient, "POST", "/api/portal/reports", {"checkin": {"symptoms": [], "answers": {}, "safety": {**safe, "fluids": "no"}}})
        self.assertEqual([f["code"] for f in urgent["data"]["red_flags"]], ["fluids"])
        self.assertTrue(urgent["data"]["emergency_message"])

    def test_validation_and_static_portal_page(self) -> None:
        self.make_patient("PORTAL-5", "9333333335")
        patient = self.client()
        self.call(patient, "POST", "/api/portal/login", {"phone": "9333333335", "password": "1234"})
        self.assertEqual(self.call(patient, "POST", "/api/portal/reports", {"text": ""})[0], 422)
        self.assertEqual(self.call(patient, "POST", "/api/portal/reports", {"text": "hot", "temperature": 99})[0], 422)
        self.assertEqual(self.call(patient, "POST", "/api/portal/reports", {"text": "x", "onset_date": "2099-01-01"})[0], 422)
        self.assertEqual(self.call(patient, "POST", "/api/portal/reports", {"text": "x", "audio_base64": "AA==", "audio_type": "video/avi"})[0], 415)
        with self.client().open(self.base + "/portal") as res:
            self.assertIn(b"portal.js", res.read())


class DoctorAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "auth.db"
        migrate(cls.db)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db, Path(__file__).resolve().parents[1]))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    call = PortalTestCase.call

    def test_doctor_api_requires_username_password_login(self) -> None:
        doctor = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.assertEqual(self.call(doctor, "GET", "/api/patients")[0], 401)
        self.assertEqual(self.call(doctor, "GET", "/api/lab-documents/1/file")[0], 401)
        self.assertEqual(self.call(doctor, "GET", "/api/auth/me")[0], 401)
        self.assertEqual(self.call(doctor, "POST", "/api/auth/login", {"username": "chemodoc", "password": "1234"})[0], 401)
        self.assertEqual(self.call(doctor, "POST", "/api/auth/login", {"phone": "9000000200", "password": "qwertyui"})[0], 401)  # no phone sign-in
        for username, name, role in [("radiodoc", "Dr. P. Rao", "doctor"), ("surgerydoc", "Dr. A. Menon", "doctor"), ("staff", "Hospital staff", "staff")]:
            other = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
            status, me, _ = self.call(other, "POST", "/api/auth/login", {"username": username, "password": "qwertyui"})
            self.assertEqual((status, me["data"]["name"], me["data"]["role"]), (200, name, role))
        status, me, headers = self.call(doctor, "POST", "/api/auth/login", {"username": " ChemoDoc ", "password": "qwertyui"})
        self.assertEqual((status, me["data"]["name"], me["data"]["username"]), (200, "Dr. R. Khanna", "chemodoc"))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.call(doctor, "GET", "/api/patients")[0], 200)
        _, p, _ = self.call(doctor, "POST", "/api/patients", {"medical_record_number": "AUTH-1", "first_name": "A", "last_name": "B"})
        _, plan, _ = self.call(doctor, "POST", f"/api/patients/{p['data']['id']}/treatment-plans", {
            "regimen_name": "X", "cycles": [{"cycle_number": 1, "planned_date": "2026-01-01", "medications": [{"drug_name": "Cisplatin", "planned_dose": 75, "dose_unit": "mg/m2"}]}]})
        cycle = self.call(doctor, "GET", f"/api/treatment-plans/{plan['data']['id']}/cycles")[1]["data"][0]
        _, rec, _ = self.call(doctor, "POST", f"/api/cycles/{cycle['id']}/record", {"outcome": "given", "administered_date": "2026-01-01"})
        self.assertEqual(rec["data"]["recorded_by"], "Dr. R. Khanna")  # signed-in clinician is the default recorder
        # patient portal cookies do not unlock the doctor API
        self.assertEqual(self.call(build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar())), "GET", "/api/patient-reports")[0], 401)
        self.call(doctor, "POST", "/api/auth/logout")
        self.assertEqual(self.call(doctor, "GET", "/api/patients")[0], 401)
        self.assertEqual(self.call(doctor, "GET", "/health")[0], 200)


class RedFlagTests(unittest.TestCase):
    def test_rules(self) -> None:
        codes = lambda *a: {f["code"] for f in detect_red_flags(*a)}  # noqa: E731
        self.assertEqual(codes("I feel tired", [], None, "none"), set())
        self.assertIn("breathing", codes("I get short of breath climbing stairs", [], None, None))
        self.assertIn("fluids", codes("I can't keep anything down since yesterday", [], None, None))
        self.assertIn("bleeding", codes("my gums are bleeding", [], None, None))
        self.assertIn("fever", codes("fine", [], 38.1, None))
        self.assertIn("selfcare", codes("weak", [], None, "selfcare"))
        # Indian-language red flags
        self.assertEqual(codes("मुझे बुखार है और सांस लेने में तकलीफ", [], None, None), {"fever", "breathing"})
        self.assertIn("fever", codes("నాకు జ్వరం వచ్చింది", [], None, None))

    def test_native_language_keywords(self) -> None:
        terms = {c["term"] for c in _keyword_candidates("నాకు ఎక్కువ అవుతున్నాయి నాకు వాంతులు ఎక్కువ అవుతున్నాయి", [], ["Cisplatin"])}
        self.assertEqual(terms, {"Vomiting"})
        self.assertIn("Diarrhea", {c["term"] for c in _keyword_candidates("मुझे दस्त हो रहे हैं", [], [])})


class QuestionnaireTests(unittest.TestCase):
    def test_grade_rules(self) -> None:
        from backend import questionnaire as q
        safe = {"fever": "no", "fluids": "yes", "urine": "yes", "bleeding": "no", "breathing": "no"}
        grade = lambda key, answers: q.evaluate([key], {key: answers}, safe)["items"][0]["grade_suggestion"]  # noqa: E731
        self.assertEqual(grade("vomiting", {"treatment": "usual", "fluids": "yes"}), 1)
        self.assertEqual(grade("vomiting", {"treatment": "admitted", "fluids": "yes"}), 3)
        self.assertEqual(grade("diarrhoea", {"extra_stools": "4-6", "blood": "no", "interference": "none"}), 2)
        self.assertEqual(grade("diarrhoea", {"extra_stools": "1-3", "blood": "no", "interference": "selfcare"}), 3)
        self.assertEqual(grade("tiredness", {"severity": "moderate", "rest_helps": "yes", "interference": "none"}), 1)
        self.assertEqual(grade("fever", {"max_temp": 39.5, "chills": "yes"}), 2)
        self.assertIsNone(grade("fever", {"chills": "yes"}))
        self.assertEqual(grade("breathless", {"when": "rest", "chest_pain": "no"}), 3)
        self.assertEqual(grade("muscle_pain", {"severity": "mild", "interference": "instrumental"}), 2)
        result = q.evaluate(["vomiting", "bleeding"], {"vomiting": {"episodes": "6+"}, "bleeding": {"where": "stool"}}, safe)
        self.assertEqual({f["code"] for f in result["red_flags"]}, {"fluids", "bleeding"})
        self.assertIsNone(result["items"][1]["term"])
        with self.assertRaises(q.CheckinError):
            q.evaluate(["fever"], {"fever": {"max_temp": 50}}, safe)
        with self.assertRaises(q.CheckinError):
            q.evaluate(["vomiting"], {"vomiting": {"onset": "2999-01-01"}}, safe)

    def test_every_question_is_translated(self) -> None:
        from backend import questionnaire as q
        questions = [qq for s in q.bank()["symptoms"] for qq in s["questions"]] + q.SAFETY_QUESTIONS
        for question in questions:
            self.assertEqual(set(question["text"]), set(q.LANGS), question["id"])
            for option in question.get("options", []):
                self.assertTrue(all(option["label"][lang].strip() for lang in q.LANGS), option["value"])


class VoiceAnswerTests(unittest.TestCase):
    def test_spoken_answers_in_three_languages(self) -> None:
        from datetime import date
        from backend.voice_answers import interpret
        d = date(2026, 9, 26)
        cases = [
            ("vomiting", "episodes", "మూడుసార్లు అయింది", "3-5"), ("vomiting", "episodes", "about 7 times", "6+"),
            ("vomiting", "fluids", "नहीं, सब निकल जाता है", "no"), ("vomiting", "fluids", "కొంచెం నిలుస్తున్నాయి", "some"),
            ("vomiting", "treatment", "हॉस्पिटल में भर्ती हुई", "admitted"), ("vomiting", "frequency", "కొన్నిసార్లు", "occasionally"),
            ("vomiting", "onset", "నిన్న నుంచి", "2026-09-25"), ("vomiting", "onset", "3 दिन से", "2026-09-23"),
            ("tiredness", "severity", "చాలా ఎక్కువ", "very_severe"), ("tiredness", "interference", "मुझे नहाने में मदद चाहिए", "selfcare"),
            ("tiredness", "rest_helps", "లేదు తగ్గడం లేదు", "no"), ("fever", "max_temp", "101 degrees", 38.3),
            ("safety", "fever", "नापा नहीं", "not_measured"), ("safety", "bleeding", "नहीं है", "no"), ("safety", "breathing", "ఉంది", "yes"),
            ("diarrhoea", "extra_stools", "ఐదు సార్లు", "4-6"), ("tiredness", "severity", "the second one", "moderate"),
        ]
        for symptom, qid, heard, expected in cases:
            self.assertEqual(interpret(symptom, qid, heard, "te", today=d)["value"], expected, heard)
        self.assertIsNone(interpret("vomiting", "episodes", "blah", today=d)["value"])  # AI is off in tests
        self.assertEqual(interpret("vomiting", "fluids", "కొంచెం", "te", today=d)["label"], "కొంత మాత్రమే")

    def test_transcripts_are_kept_with_answers(self) -> None:
        from backend import questionnaire as q
        safe = {"fever": "no", "fluids": "yes", "urine": "yes", "bleeding": "no", "breathing": "no"}
        result = q.evaluate(["vomiting"], {"vomiting": {"episodes": "3-5", "onset": "2026-09-01"}}, safe,
                            {"vomiting": {"episodes": "మూడుసార్లు", "onset": "నిన్న"}, "safety": {"fever": "లేదు"}})
        self.assertEqual(result["items"][0]["answers"][0]["heard"], "మూడుసార్లు")
        self.assertEqual(result["items"][0]["onset_heard"], "నిన్న")
        self.assertEqual(result["safety"][0]["heard"], "లేదు")
        self.assertTrue(result["by_voice"])
        self.assertIn('patient said: "మూడుసార్లు"', q.summary_text(result))


class AnthropicRetryTests(unittest.TestCase):
    def test_sampling_parameter_rejected_is_retried_without_it(self) -> None:
        from http.server import BaseHTTPRequestHandler
        seen = []

        class Mock(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(sorted(body))
                if "temperature" in body:
                    out, code = {"error": {"message": "`temperature` is deprecated for this model."}}, 400
                else:
                    out, code = {"content": [{"type": "text", "text": "OK"}]}, 200
                self.send_response(code); self.send_header("content-type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps(out).encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        old = os.environ.get("ANTHROPIC_BASE_URL")
        os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            reply = extraction._anthropic_post("k", {"model": "m", "max_tokens": 5, "temperature": 0, "messages": []}, 5)
        finally:
            server.shutdown(); server.server_close()
            if old is None:
                os.environ.pop("ANTHROPIC_BASE_URL", None)
            else:
                os.environ["ANTHROPIC_BASE_URL"] = old
        self.assertEqual(reply["content"][0]["text"], "OK")
        self.assertEqual(len(seen), 2)
        self.assertNotIn("temperature", seen[1])


if __name__ == "__main__":
    unittest.main()

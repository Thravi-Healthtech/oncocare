"""Regression tests for bugs found in the November review (RECIST, summary versions, WhatsApp reminders and prescriptions)."""
from __future__ import annotations

import base64
import json
import os
import unittest
from datetime import date, timedelta
from unittest import mock

from backend import oncology
from backend.database import transaction
from tests.test_oncology import scan
from tests.test_radiotherapy import Base


class RecistWithoutTargetMeasurementTests(unittest.TestCase):
    def test_new_lesion_or_non_target_progression_is_pd_even_if_targets_were_not_measured(self) -> None:
        base = scan("2026-01-01", ("Lung", 30, False), non_target="present", timepoint="baseline")
        new_only = {**scan("2026-02-01", new=True), "id": "new"}
        nt_only = {**scan("2026-02-01", non_target="progression"), "id": "nt"}
        markers_only = {**scan("2026-02-01"), "id": "mk"}
        self.assertEqual([a["response"] for a in oncology.recist([base, new_only])], [None, "PD"])
        self.assertEqual([a["response"] for a in oncology.recist([base, nt_only])], [None, "PD"])
        self.assertEqual([a["response"] for a in oncology.recist([base, markers_only])], [None, "NE"])


class NarrativeTests(unittest.TestCase):
    def test_quoted_patient_message_is_one_cited_sentence_cut_at_a_word(self) -> None:
        """A multi-sentence AI triage summary of the latest message was cut mid-word at 220 characters, and its first
        sentence was flagged as uncited because the citation came after the second one."""
        from backend import records
        summary = "The patient reports fever since the morning. " + "It is unclear whether this is new " * 12
        self.assertEqual(records._first_sentence(summary, 220), "The patient reports fever since the morning")
        cut = records._first_sentence("word " * 80, 220)
        self.assertTrue(cut.endswith("word…") and len(cut) <= 221, cut)


class ReviewFixTests(Base):
    def setUp(self) -> None:
        self.phone = "96" + str(abs(hash(self._testMethodName)) % 10**8).zfill(8)
        self.pid = self.patient(f"FIX-{self._testMethodName}", phone=self.phone)

    def final_summary(self, medications: list | None = None) -> int:
        _, ds = self.call("POST", f"/api/patients/{self.pid}/discharge-summaries", {"kind": "end_of_treatment"})
        sid = ds["data"]["id"]
        self.call("PATCH", f"/api/discharge-summaries/{sid}", {"condition_at_discharge": "Stable", "course_text": "Completed treatment.",
                                                               "medications": medications or [], "follow_up": {"none": True}})
        status, body = self.call("POST", f"/api/discharge-summaries/{sid}/finalise", {"confirmed": True})
        self.assertEqual(status, 200, body)
        return sid

    def reminders(self) -> list[dict]:
        with transaction(self.db) as connection:
            out = connection.execute("SELECT id, status, meta_json FROM whatsapp_messages WHERE patient_id = ? AND kind = 'reminder' ORDER BY id",
                                     (self.pid,)).fetchall()
        return [{"id": r["id"], "status": r["status"], **json.loads(r["meta_json"])} for r in out]

    def test_revising_a_summary_twice_opens_the_same_draft(self) -> None:
        """'New version' clicked twice made two v2 drafts; finalising both left two final summaries in the patient portal."""
        sid = self.final_summary()
        _, first = self.call("POST", f"/api/discharge-summaries/{sid}/revise", {})
        _, second = self.call("POST", f"/api/discharge-summaries/{sid}/revise", {})
        self.assertEqual((first["data"]["id"], first["data"]["version"]), (second["data"]["id"], 2))
        self.assertEqual(self.call("POST", f"/api/discharge-summaries/{first['data']['id']}/finalise", {"confirmed": True})[0], 200)
        _, listing = self.call("GET", f"/api/patients/{self.pid}/discharge-summaries")
        self.assertEqual(sorted((d["version"], d["status"]) for d in listing["data"]), [(1, "superseded"), (2, "final")])

    def test_summary_narrative_cites_the_diagnosis_and_baseline_scan_before_its_period(self) -> None:
        """The opening diagnosis sentence and the baseline measurement were flagged 'without a source' in every summary whose
        period starts after the diagnosis, because those entries were left out of the sources."""
        self.call("POST", f"/api/patients/{self.pid}/diagnoses", {"primary_site": "Breast", "diagnosis_date": "2026-01-10", "histology": "Invasive ductal carcinoma",
                                                                  "t_stage": "2", "n_stage": "1", "m_stage": "0", "stage_group": "IIB"})
        self.call("POST", f"/api/patients/{self.pid}/tumour-assessments", {"assessment_date": "2026-01-15", "timepoint": "baseline",
                                                                           "lesions": [{"location": "Right breast mass", "size_mm": 32}]})
        today = date.today().isoformat()
        status, ds = self.call("POST", f"/api/patients/{self.pid}/discharge-summaries", {"kind": "discharge", "period_from": today, "period_to": today})
        self.assertEqual(status, 201, ds)
        course = ds["data"]["content"]["course"]
        self.assertEqual(course["unsupported"], 0, [s["text"] for s in course["sentences"] if s["unsupported"]])
        self.assertIn("Invasive ductal carcinoma", course["text"])
        # Staging recorded after the treatment period (back-filled at the end of the course) is still the summary's diagnosis.
        _, early = self.call("POST", f"/api/patients/{self.pid}/discharge-summaries", {"kind": "discharge", "period_from": "2026-01-01", "period_to": "2026-01-05"})
        self.assertEqual(early["data"]["content"]["course"]["unsupported"], 0, early["data"]["content"]["course"]["text"])

    def test_scribe_reports_a_slow_ai_or_speech_service_instead_of_a_server_error(self) -> None:
        """A reply timeout from urlopen is a TimeoutError, not URLError, so it escaped the RuntimeError handlers as a 500."""
        timeout = mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out"))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "ONCOCARE_EXTRACTION": "auto", "SARVAM_API_KEY": "sk-test"}), timeout:
            status, body = self.call("POST", f"/api/patients/{self.pid}/prescriptions/draft", {"transcript": "Doctor: take ondansetron 4 mg twice a day for 3 days."})
            self.assertEqual(status, 502, body)
            self.assertIn("No reply", body["error"]["message"])
            status, body = self.call("POST", "/api/scribe/transcribe", {"audio_base64": base64.b64encode(b"x" * 64).decode(), "audio_type": "audio/webm"})
            self.assertEqual(status, 502, body)

    def test_sending_a_cancelled_prescription_is_refused(self) -> None:
        """With no other active prescription, a cancelled prescription_id fell through to the discharge summary's medicines."""
        self.final_summary([{"name": "Dolo 650", "dose": "650 mg", "frequency": "when needed"}])
        _, rx = self.call("POST", f"/api/patients/{self.pid}/prescriptions", {"items": [{"name": "Ondansetron", "dose": "4 mg"}]})
        self.call("POST", f"/api/prescriptions/{rx['data']['id']}/cancel", {"reason": "Wrong dose"})
        status, body = self.call("POST", f"/api/patients/{self.pid}/whatsapp/compose", {"kind": "prescription", "prescription_id": rx["data"]["id"]})
        self.assertEqual(status, 422, body)
        self.assertIn("not active", body["error"]["message"])

    def test_delayed_or_held_cycle_withdraws_its_scheduled_reminder(self) -> None:
        """After a delay the patient got the reminder for the old date as well as the new one, and a held cycle kept its reminder."""
        today = date.today()
        days = [today - timedelta(days=1), today + timedelta(days=10), today + timedelta(days=31)]
        cycles = [{"cycle_number": n, "planned_date": d.isoformat(), "medications": [{"drug_name": "Cisplatin", "planned_dose": 75, "dose_unit": "mg/m2"}]}
                  for n, d in enumerate(days, start=1)]
        _, plan = self.call("POST", f"/api/patients/{self.pid}/treatment-plans", {"regimen_name": "Cisplatin", "start_date": days[0].isoformat(),
                                                                                    "cycle_interval_days": 21, "cycles": cycles})
        _, listed = self.call("GET", f"/api/treatment-plans/{plan['data']['id']}/cycles")
        c1, c2, c3 = listed["data"]
        self.call("POST", f"/api/cycles/{c1['id']}/record", {"outcome": "given", "administered_date": days[0].isoformat(), "notify_whatsapp": True})
        moved = (days[1] + timedelta(days=7)).isoformat()
        status, body = self.call("POST", f"/api/cycles/{c2['id']}/record", {"outcome": "delayed", "new_planned_date": moved, "reason_category": "Toxicity",
                                                                           "rationale": "ANC 0.9", "notify_whatsapp": True})
        self.assertEqual(status, 200, body)
        self.assertEqual([(r["visit_date"], r["status"]) for r in self.reminders()], [(days[1].isoformat(), "cancelled"), (moved, "scheduled")])
        # Held without telling the patient on WhatsApp: the reminder for that cycle must not go out either.
        self.call("POST", f"/api/cycles/{c2['id']}/record", {"outcome": "held", "reason_category": "Toxicity", "rationale": "ANC 0.6"})
        self.assertEqual({r["status"] for r in self.reminders()}, {"cancelled"})

    def test_surgery_date_change_and_cancellation_withdraw_the_admission_reminder(self) -> None:
        first, second = (date.today() + timedelta(days=20)).isoformat(), (date.today() + timedelta(days=27)).isoformat()
        status, case = self.call("POST", f"/api/patients/{self.pid}/surgeries", {"procedure_planned": "Wide local excision", "surgery_date": first,
                                                                                 "notify_whatsapp": True})
        self.assertEqual(status, 201, case)
        cid = case["data"]["case"]["id"]
        self.call("PATCH", f"/api/surgeries/{cid}", {"surgery_date": second, "notify_whatsapp": True})
        self.assertEqual([(r["visit_date"], r["status"]) for r in self.reminders()], [(first, "cancelled"), (second, "scheduled")])
        self.call("PATCH", f"/api/surgeries/{cid}", {"status": "cancelled", "cancel_reason": "Patient unfit"})
        self.assertEqual({r["status"] for r in self.reminders()}, {"cancelled"})


if __name__ == "__main__":
    unittest.main()

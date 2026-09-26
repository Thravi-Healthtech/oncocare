from __future__ import annotations

import base64
import json
import os
import tempfile as _tempfile

os.environ.setdefault("ONCOCARE_DAILYMED", "off")
os.environ.setdefault("ONCOCARE_CONTEXT_DIR", _tempfile.mkdtemp(prefix="oncocare-context-"))
os.environ.pop("ANTHROPIC_API_KEY", None)
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.api import make_handler
from backend.database import migrate


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test.db"
        migrate(cls.db_path)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.db_path, Path(__file__).resolve().parents[1], require_auth=False))
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp_dir.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def create_journey(self) -> tuple[int, int, int]:
        _, patient = self.request("POST", "/api/patients", {
            "medical_record_number": f"MRN-{self._testMethodName}", "first_name": "Demo", "last_name": "Patient"
        })
        patient_id = patient["data"]["id"]
        _, plan = self.request("POST", f"/api/patients/{patient_id}/treatment-plans", {
            "regimen_name": "Weekly cisplatin", "intent": "curative", "start_date": "2026-09-01"
        })
        plan_id = plan["data"]["id"]
        _, cycle = self.request("POST", f"/api/treatment-plans/{plan_id}/cycles", {
            "cycle_number": 1, "planned_date": "2026-09-08", "drug_name": "Cisplatin", "planned_dose": 40,
            "dose_unit": "mg/m2", "route": "IV"
        })
        return patient_id, plan_id, cycle["data"]["id"]

    def test_patient_crud(self) -> None:
        status, created = self.request("POST", "/api/patients", {
            "medical_record_number": "MRN-CRUD", "first_name": "Asha", "last_name": "Rao", "diagnosis": "Synthetic diagnosis"
        })
        self.assertEqual(status, 201)
        patient_id = created["data"]["id"]
        status, updated = self.request("PATCH", f"/api/patients/{patient_id}", {"diagnosis": "Updated synthetic diagnosis"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["data"]["diagnosis"], "Updated synthetic diagnosis")
        status, fetched = self.request("GET", f"/api/patients/{patient_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["data"]["medical_record_number"], "MRN-CRUD")
        status, _ = self.request("DELETE", f"/api/patients/{patient_id}")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", f"/api/patients/{patient_id}")
        self.assertEqual(status, 404)

    def test_patient_search_and_private_files(self) -> None:
        _, patient = self.request("POST", "/api/patients", {
            "medical_record_number": "MRN-SEARCH", "first_name": "Kiran", "last_name": "Demo"
        })
        status, matches = self.request("GET", "/api/patients?search=MRN-SEARCH")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in matches["data"]], [patient["data"]["id"]])
        status, _ = self.request("GET", "/README.md")
        self.assertEqual(status, 404)

    def test_cycle_edit_preserves_revision(self) -> None:
        _, _, cycle_id = self.create_journey()
        status, updated = self.request("PATCH", f"/api/cycles/{cycle_id}", {
            "status": "completed", "actual_dose": 35, "modification_reason": "Clinician decision"
        })
        self.assertEqual(status, 200)
        self.assertTrue(updated["meta"]["previous_version_preserved"])
        self.assertEqual(updated["data"]["actual_dose"], 35)
        _, revisions = self.request("GET", f"/api/cycles/{cycle_id}/revisions")
        self.assertEqual(revisions["count"], 1)
        self.assertIsNone(revisions["data"][0]["snapshot"]["actual_dose"])

    def test_plan_and_reviewed_cycles_are_created_together(self) -> None:
        _, patient = self.request("POST", "/api/patients", {
            "medical_record_number": "MRN-BATCH", "first_name": "Batch", "last_name": "Demo"
        })
        patient_id = patient["data"]["id"]
        payload = {
            "regimen_name": "Synthetic regimen", "start_date": "2026-10-01",
            "cycles": [
                {"cycle_number": 1, "planned_date": "2026-10-01", "drug_name": "Cisplatin", "planned_dose": 40, "dose_unit": "mg/m2"},
                {"cycle_number": 2, "planned_date": "2026-10-08", "drug_name": "Carboplatin", "planned_dose": 30, "dose_unit": "mg/m2"},
            ],
        }
        status, created = self.request("POST", f"/api/patients/{patient_id}/treatment-plans", payload)
        self.assertEqual(status, 201)
        _, cycles = self.request("GET", f"/api/treatment-plans/{created['data']['id']}/cycles")
        self.assertEqual([(c["drug_name"], c["planned_dose"]) for c in cycles["data"]],
                         [("Cisplatin", 40), ("Carboplatin", 30)])
        payload["cycles"][1]["planned_dose"] = -1
        status, _ = self.request("POST", f"/api/patients/{patient_id}/treatment-plans", payload)
        self.assertEqual(status, 422)
        _, plans = self.request("GET", f"/api/patients/{patient_id}/treatment-plans")
        self.assertEqual(plans["count"], 1)
        payload["cycles"][1]["planned_dose"] = 30
        payload["cycles"][1]["drug_name"] = "   "
        status, _ = self.request("POST", f"/api/patients/{patient_id}/treatment-plans", payload)
        self.assertEqual(status, 422)
        _, plans = self.request("GET", f"/api/patients/{patient_id}/treatment-plans")
        self.assertEqual(plans["count"], 1)

    def test_labs_and_verified_trend_analysis(self) -> None:
        patient_id, plan_id, cycle_id = self.create_journey()
        for date, value in (("2026-09-07", 0.8), ("2026-09-14", 1.0), ("2026-09-21", 1.3)):
            if date != "2026-09-07":
                cycle_number = 2 if date == "2026-09-14" else 3
                _, cycle = self.request("POST", f"/api/treatment-plans/{plan_id}/cycles", {
                    "cycle_number": cycle_number, "planned_date": date, "drug_name": "Cisplatin", "planned_dose": 40
                })
                cycle_id = cycle["data"]["id"]
            status, _ = self.request("POST", f"/api/cycles/{cycle_id}/labs", {
                "test_name": "Creatinine", "value": value, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.1,
                "collection_date": date, "verification_status": "verified", "verified_by": "Test clinician"
            })
            self.assertEqual(status, 201)
        _, trends = self.request("GET", f"/api/patients/{patient_id}/lab-trends?test_name=Creatinine")
        trend = trends["data"][0]
        self.assertEqual(trend["direction"], "up")
        self.assertAlmostEqual(trend["delta"], 0.3)
        self.assertTrue(trend["abnormal"])
        self.assertEqual([point["value"] for point in trend["series"]], [0.8, 1.0, 1.3])

    def test_dated_ctcae_symptom_and_validation(self) -> None:
        patient_id, _, cycle_id = self.create_journey()
        status, symptom = self.request("POST", f"/api/patients/{patient_id}/symptoms", {
            "cycle_id": cycle_id, "ctcae_term": "Peripheral sensory neuropathy", "grade": 2,
            "onset_date": "2026-09-11", "status": "ongoing", "confirmed_by": "Dr. Test"
        })
        self.assertEqual(status, 201)
        self.assertEqual(symptom["data"]["onset_date"], "2026-09-11")
        status, _ = self.request("POST", f"/api/patients/{patient_id}/symptoms", {
            "ctcae_term": "Nausea", "grade": 6, "onset_date": "2026-09-12", "confirmed_by": "Dr. Test"
        })
        self.assertEqual(status, 422)
        status, _ = self.request("POST", f"/api/patients/{patient_id}/symptoms", {
            "ctcae_term": "Nausea", "grade": 1, "onset_date": "09/12/2026", "confirmed_by": "Dr. Test"
        })
        self.assertEqual(status, 422)

    def make_plan(self, mrn: str) -> tuple[int, dict]:
        _, patient = self.request("POST", "/api/patients", {"medical_record_number": mrn, "first_name": "Multi", "last_name": "Drug"})
        patient_id = patient["data"]["id"]
        cycles = [{"cycle_number": n, "planned_date": f"2026-0{n}-01", "medications": [
            {"drug_name": "Paclitaxel", "planned_dose": 175 if n != 2 else 150, "dose_unit": "mg/m2", "route": "IV"},
            {"drug_name": "Carboplatin", "planned_dose": 5, "dose_unit": "AUC", "route": "IV"}]} for n in (1, 2, 3)]
        status, plan = self.request("POST", f"/api/patients/{patient_id}/treatment-plans", {
            "regimen_name": "Paclitaxel + carboplatin", "start_date": "2026-01-01", "cycle_interval_days": 21,
            "medications": [{"drug_name": "Paclitaxel", "dose": 175, "dose_unit": "mg/m2"}, {"drug_name": "Carboplatin", "dose": 5, "dose_unit": "AUC"}],
            "cycles": cycles})
        self.assertEqual(status, 201)
        return patient_id, plan["data"]

    def test_multi_drug_plan_with_per_cycle_edits(self) -> None:
        _, plan = self.make_plan("MRN-MULTI")
        self.assertEqual([m["drug_name"] for m in plan["medications"]], ["Paclitaxel", "Carboplatin"])
        _, cycles = self.request("GET", f"/api/treatment-plans/{plan['id']}/cycles")
        self.assertEqual([[m["planned_dose"] for m in c["medications"]] for c in cycles["data"]], [[175, 5], [150, 5], [175, 5]])
        status, _ = self.request("POST", f"/api/patients/{plan['patient_id']}/treatment-plans", {
            "regimen_name": "Dup", "cycles": [{"cycle_number": 1, "planned_date": "2026-01-01", "medications": [
                {"drug_name": "Cisplatin", "planned_dose": 40, "dose_unit": "mg/m2"}, {"drug_name": "cisplatin", "planned_dose": 40, "dose_unit": "mg/m2"}]}]})
        self.assertEqual(status, 422)

    def test_record_outcomes_require_reason_and_preserve_history(self) -> None:
        _, plan = self.make_plan("MRN-RECORD")
        _, cycles = self.request("GET", f"/api/treatment-plans/{plan['id']}/cycles")
        c1, c2, c3 = cycles["data"]
        status, given = self.request("POST", f"/api/cycles/{c1['id']}/record", {"outcome": "given", "administered_date": "2026-01-01"})
        self.assertEqual(status, 200)
        self.assertEqual({m["status"] for m in given["data"]["medications"]}, {"given"})
        pac = c2["medications"][0]["id"]
        status, error = self.request("POST", f"/api/cycles/{c2['id']}/record", {"outcome": "given", "administered_date": "2026-02-01", "medications": [{"id": pac, "actual_dose": 120}]})
        self.assertEqual(status, 422)
        status, _ = self.request("POST", f"/api/cycles/{c2['id']}/record", {"outcome": "modified", "administered_date": "2026-02-01", "medications": [{"id": pac, "actual_dose": 120}]})
        self.assertEqual(status, 422)
        status, modified = self.request("POST", f"/api/cycles/{c2['id']}/record", {
            "outcome": "modified", "administered_date": "2026-02-01", "reason_category": "Toxicity", "rationale": "G2 neuropathy",
            "medications": [{"id": pac, "actual_dose": 120}, {"id": c2["medications"][1]["id"], "status": "held"}]})
        self.assertEqual(status, 200)
        self.assertEqual([m["status"] for m in modified["data"]["medications"]], ["modified", "held"])
        self.assertEqual(modified["data"]["decisions"][0]["reason_category"], "Toxicity")
        _, revisions = self.request("GET", f"/api/cycles/{c2['id']}/revisions")
        self.assertEqual(revisions["data"][0]["snapshot"]["status"], "planned")
        status, delayed = self.request("POST", f"/api/cycles/{c3['id']}/record", {
            "outcome": "delayed", "new_planned_date": "2026-03-08", "reason_category": "Laboratory findings", "rationale": "ANC low"})
        self.assertEqual((status, delayed["data"]["status"], delayed["data"]["planned_date"]), (200, "delayed", "2026-03-08"))
        _, longitudinal = self.request("GET", f"/api/treatment-plans/{plan['id']}/longitudinal")
        summary = longitudinal["data"]["summary"]
        self.assertEqual((summary["given_cycles"], summary["modified_cycles"], summary["cycles_missing_labs"]), (2, 1, 3))
        exposure = {e["drug_name"]: e["cumulative_dose"] for e in summary["cumulative_exposure"]}
        self.assertEqual(exposure, {"Paclitaxel": 295, "Carboplatin": 5})

    def test_lab_ctcae_grading_and_baseline_trend(self) -> None:
        patient_id, plan = self.make_plan("MRN-GRADE")
        _, cycles = self.request("GET", f"/api/treatment-plans/{plan['id']}/cycles")
        c1, c2, _ = cycles["data"]
        self.request("POST", f"/api/cycles/{c1['id']}/labs", {"collection_date": "2026-01-01", "results": [
            {"test_name": "Creatinine", "value": 0.8, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.1},
            {"test_name": "ANC", "value": 4000, "unit": "/uL", "ref_low": 1500, "ref_high": 8000}]})
        self.request("POST", f"/api/cycles/{c2['id']}/labs", {"collection_date": "2026-01-30", "results": [
            {"test_name": "Creatinine", "value": 1.3, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.1},
            {"test_name": "ANC", "value": 0.9, "unit": "10^3/uL", "ref_low": 1.5, "ref_high": 8}]})
        _, data = self.request("GET", f"/api/treatment-plans/{plan['id']}/longitudinal")
        window = data["data"]["cycles"][1]
        grades = {item["term"]: item["grade"] for item in window["ctcae"]}
        self.assertEqual(grades["Creatinine increased"], 2)  # >1.5 x baseline beats the ULN rule (G1)
        self.assertEqual(grades["Neutrophil count decreased"], 3)  # 900/uL after unit conversion
        self.assertIn("Paclitaxel", next(i for i in window["ctcae"] if i["term"] == "Neutrophil count decreased")["associated_drugs"])
        creat = next(t for t in data["data"]["lab_trends"] if t["test_name"] == "Creatinine")
        self.assertEqual((creat["baseline"]["value"], creat["latest"]["pct_from_baseline"]), (0.8, 62.5))
        self.assertEqual(data["data"]["cycles"][2]["labs_status"], "missing")

    def test_upload_extraction_stays_unverified_until_confirmed(self) -> None:
        _, plan = self.make_plan("MRN-UPLOAD")
        _, cycles = self.request("GET", f"/api/treatment-plans/{plan['id']}/cycles")
        cycle_id = cycles["data"][1]["id"]
        report = "Collected: 20/01/2026\nHaemoglobin 9.4 g/dL 12-15.5\nPlatelet count 1.1 lakh/cumm 1.5-4.5\nDirect Bilirubin 0.3 mg/dL\n"
        status, upload = self.request("POST", f"/api/cycles/{cycle_id}/lab-documents", {
            "filename": "report.txt", "content_type": "text/plain", "data_base64": base64.b64encode(report.encode()).decode()})
        self.assertEqual(status, 201)
        candidates = {c["test_name"]: c for c in upload["data"]["candidates"]}
        self.assertEqual(set(candidates), {"Hemoglobin", "Platelets", "Direct bilirubin"})
        self.assertEqual(candidates["Platelets"]["value"], 110)
        self.assertEqual(candidates["Hemoglobin"]["collection_date"], "2026-01-20")
        self.assertTrue(all(c["verification_status"] == "unverified" for c in candidates.values()))
        _, data = self.request("GET", f"/api/treatment-plans/{plan['id']}/longitudinal")
        self.assertEqual(data["data"]["cycles"][1]["labs_status"], "unverified")
        status, verified = self.request("POST", f"/api/labs/{candidates['Hemoglobin']['id']}/verify", {"value": 9.5})
        self.assertEqual((status, verified["data"]["verification_status"], verified["data"]["value"]), (200, "verified", 9.5))
        _, data = self.request("GET", f"/api/treatment-plans/{plan['id']}/longitudinal")
        anemia = next(i for i in data["data"]["cycles"][1]["ctcae"] if i["term"] == "Anemia")
        self.assertEqual(anemia["grade"], 2)
        status, _ = self.request("POST", f"/api/cycles/{cycle_id}/lab-documents", {"filename": "x.exe", "content_type": "application/x-msdownload", "data_base64": "AA=="})
        self.assertEqual(status, 415)

    def test_symptom_trend_and_term_validation(self) -> None:
        patient_id, plan = self.make_plan("MRN-SYMPTOM")
        _, cycles = self.request("GET", f"/api/treatment-plans/{plan['id']}/cycles")
        c2, c3 = cycles["data"][1], cycles["data"][2]
        for cycle, grade in ((c2, 1), (c3, 2)):
            status, _ = self.request("POST", f"/api/patients/{patient_id}/symptoms", {"cycle_id": cycle["id"], "ctcae_term": "Peripheral sensory neuropathy",
                                                                                   "grade": grade, "onset_date": "2026-02-10", "confirmed_by": "Dr. Test"})
            self.assertEqual(status, 201)
        _, data = self.request("GET", f"/api/treatment-plans/{plan['id']}/longitudinal")
        item = data["data"]["cycles"][2]["ctcae"][0]
        self.assertEqual((item["grade"], item["trend"], item["previous_grade"]), (2, "worsening", 1))
        self.assertIn("Paclitaxel", item["associated_drugs"])
        status, _ = self.request("POST", f"/api/patients/{patient_id}/symptoms", {"ctcae_term": "Made up", "grade": 1, "onset_date": "2026-02-10", "confirmed_by": "Dr. Test"})
        self.assertEqual(status, 422)
        status, _ = self.request("POST", f"/api/patients/{patient_id}/symptoms", {"ctcae_term": "Alopecia", "grade": 3, "onset_date": "2026-02-10", "confirmed_by": "Dr. Test"})
        self.assertEqual(status, 422)
        _, suggest = self.request("GET", "/api/ctcae/suggest?text=tingling%20toes&drugs=Paclitaxel")
        self.assertEqual(suggest["data"]["matches"][0]["term"], "Peripheral sensory neuropathy")

    def test_patient_update_validation_and_list_summary(self) -> None:
        _, plan = self.make_plan("MRN-SUMMARY")
        status, _ = self.request("PATCH", f"/api/patients/{plan['patient_id']}", {"first_name": "  "})
        self.assertEqual(status, 422)
        status, updated = self.request("PATCH", f"/api/patients/{plan['patient_id']}", {"active": False})
        self.assertEqual((status, updated["data"]["active"]), (200, 0))
        _, listed = self.request("GET", "/api/patients?search=MRN-SUMMARY&status=inactive")
        self.assertEqual(listed["data"][0]["next_cycle_number"], 1)
        self.assertEqual(listed["data"][0]["plan_count"], 1)
        status, _ = self.request("DELETE", f"/api/treatment-plans/{plan['id']}")
        self.assertEqual(status, 200)

    def test_legacy_database_is_migrated(self) -> None:
        import sqlite3
        legacy = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(legacy)
        connection.executescript("""
            CREATE TABLE patients (id INTEGER PRIMARY KEY, medical_record_number TEXT UNIQUE NOT NULL, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
              date_of_birth TEXT, sex TEXT, diagnosis TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE treatment_plans (id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, regimen_name TEXT NOT NULL, intent TEXT,
              status TEXT NOT NULL DEFAULT 'active', start_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE treatment_cycles (id INTEGER PRIMARY KEY, treatment_plan_id INTEGER NOT NULL, cycle_number INTEGER NOT NULL, planned_date TEXT NOT NULL,
              administered_date TEXT, status TEXT NOT NULL DEFAULT 'planned', drug_name TEXT NOT NULL, planned_dose REAL NOT NULL, actual_dose REAL,
              dose_unit TEXT NOT NULL DEFAULT 'mg/m2', route TEXT, modification_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE lab_investigations (id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL, test_name TEXT NOT NULL, test_code TEXT,
              value REAL NOT NULL, unit TEXT NOT NULL, ref_low REAL, ref_high REAL, collection_date TEXT NOT NULL, verification_status TEXT NOT NULL DEFAULT 'verified',
              source TEXT NOT NULL DEFAULT 'manual', source_document TEXT, verified_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO patients VALUES (1, 'OLD-1', 'Old', 'Record', NULL, NULL, NULL, 1, 'x', 'x');
            INSERT INTO treatment_plans VALUES (1, 1, 'Weekly cisplatin', NULL, 'active', '2026-01-01', 'x', 'x');
            INSERT INTO treatment_cycles VALUES (1, 1, 1, '2026-01-01', '2026-01-01', 'completed', 'Cisplatin', 40, 30, 'mg/m2', 'IV', 'Reduced', 'x', 'x');
        """)
        connection.commit(); connection.close()
        migrate(legacy); migrate(legacy)
        connection = sqlite3.connect(legacy)
        meds = connection.execute("SELECT drug_name, planned_dose, actual_dose, status FROM cycle_medications").fetchall()
        status = connection.execute("SELECT status FROM treatment_cycles").fetchone()[0]
        plan_meds = connection.execute("SELECT drug_name, dose FROM plan_medications").fetchall()
        connection.close()
        self.assertEqual(meds, [("Cisplatin", 40.0, 30.0, "modified")])
        self.assertEqual(status, "given")
        self.assertEqual(plan_meds, [("Cisplatin", 40.0)])

    def test_upload_uses_selected_recording_date_and_notes_mismatch(self) -> None:
        _, plan = self.make_plan("MRN-RECDATE")
        _, cycles = self.request("GET", f"/api/treatment-plans/{plan['id']}/cycles")
        pdf = next((Path(__file__).resolve().parents[1] / "sample_reports").glob("2_*pre-C2.pdf"))
        status, upload = self.request("POST", f"/api/cycles/{cycles['data'][1]['id']}/lab-documents", {
            "filename": pdf.name, "content_type": "application/pdf", "recording_date": "2026-08-23",
            "data_base64": base64.b64encode(pdf.read_bytes()).decode()})
        self.assertEqual(status, 201)
        self.assertEqual(len(upload["data"]["candidates"]), 20)
        self.assertEqual({c["collection_date"] for c in upload["data"]["candidates"]}, {"2026-08-23"})
        self.assertEqual(upload["data"]["document"]["report_date"], "2026-08-24")
        self.assertIn("Report shows collection date 2026-08-24", upload["data"]["document"]["extraction_note"])
        status, _ = self.request("POST", f"/api/cycles/{cycles['data'][1]['id']}/lab-documents", {
            "filename": pdf.name, "content_type": "application/pdf", "recording_date": "2099-01-01", "data_base64": "AA=="})
        self.assertEqual(status, 422)

    def test_health_reports_ai_and_context_status(self) -> None:
        status, health = self.request("GET", "/health")
        self.assertEqual((status, health["api_version"]), (200, 3))
        self.assertFalse(health["ai_extraction"]["enabled"])
        self.assertEqual(health["context"]["ctcae"]["version"], "6.0")


if __name__ == "__main__":
    unittest.main()

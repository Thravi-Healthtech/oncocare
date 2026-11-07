from __future__ import annotations

import unittest

from backend import oncology
from tests.test_radiotherapy import Base


def scan(day: str, *targets: tuple, new: bool = False, non_target: str | None = None, timepoint: str = "during") -> dict:
    lesions = [{"location": loc, "kind": "target", "node": node, "size_mm": size, "status": None} for loc, size, node in targets]
    if non_target:
        lesions.append({"location": "Effusion", "kind": "non_target", "node": False, "size_mm": None, "status": non_target})
    if new:
        lesions.append({"location": "Liver", "kind": "new", "node": False, "size_mm": 8, "status": None})
    return {"id": day, "assessment_date": day, "timepoint": timepoint, "lesions": lesions, "markers": [], "new_lesions": False, "response_override": None}


class RecistTests(unittest.TestCase):
    def responses(self, *scans: dict) -> list:
        return [a["response"] for a in oncology.recist(list(scans))]

    def test_partial_and_stable(self) -> None:
        base = scan("2026-01-01", ("Lung", 50, False), ("Node", 20, True), timepoint="baseline")  # sum 70
        self.assertEqual(self.responses(base, scan("2026-02-01", ("Lung", 30, False), ("Node", 18, True))), [None, "PR"])  # 48: -31 %
        self.assertEqual(self.responses(base, scan("2026-02-01", ("Lung", 45, False), ("Node", 18, True))), [None, "SD"])  # 63: -10 %

    def test_progression_is_measured_from_the_nadir(self) -> None:
        base = scan("2026-01-01", ("Lung", 50, False), timepoint="baseline")
        nadir = scan("2026-02-01", ("Lung", 20, False))                              # PR, nadir 20
        small_rise = scan("2026-03-01", ("Lung", 24, False))                         # +20 % but only +4 mm: not PD
        rise = scan("2026-04-01", ("Lung", 26, False))                               # +30 % and +6 mm from the nadir: PD
        self.assertEqual(self.responses(base, nadir, small_rise, rise), [None, "PR", "PR", "PD"])

    def test_new_lesion_non_target_progression_and_complete_response(self) -> None:
        base = scan("2026-01-01", ("Lung", 30, False), ("Node", 18, True), non_target="present", timepoint="baseline")
        self.assertEqual(self.responses(base, scan("2026-02-01", ("Lung", 10, False), ("Node", 12, True), new=True))[1], "PD")
        self.assertEqual(self.responses(base, scan("2026-02-01", ("Lung", 25, False), ("Node", 15, True), non_target="progression"))[1], "PD")
        # Nodes under 10 mm count as normal: complete response once non-target disease has gone too.
        self.assertEqual(self.responses(base, scan("2026-02-01", ("Lung", 0, False), ("Node", 8, True), non_target="absent"))[1], "CR")
        self.assertEqual(self.responses(base, scan("2026-02-01", ("Lung", 0, False), ("Node", 8, True), non_target="present"))[1], "PR")

    def test_baseline_warnings(self) -> None:
        out = oncology.recist([scan("2026-01-01", ("Skin nodule", 6, False), ("Node", 12, True), timepoint="baseline")])
        self.assertEqual(len(out[0]["warnings"]), 2)


class OncologyApiTests(Base):
    def setUp(self) -> None:
        self.pid = self.patient(f"ONC-{self._testMethodName}")

    def test_staging_measurements_case_sheet_and_discharge(self) -> None:
        self.assertEqual(self.call("POST", f"/api/patients/{self.pid}/diagnoses", {"diagnosis_date": "2026-08-01"})[0], 422)
        status, body = self.call("POST", f"/api/patients/{self.pid}/diagnoses", {
            "diagnosis_date": "2026-08-01", "primary_site": "Lung", "laterality": "right", "histology": "Adenocarcinoma", "tnm_prefix": "c",
            "t_stage": "2b", "n_stage": "2", "m_stage": "1a", "stage_group": "IVA", "ecog": 1, "intent": "palliative",
            "biomarkers": [{"name": "EGFR", "value": "not detected"}], "markers": [{"name": "CEA", "value": "18.4", "unit": "ng/mL"}]})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["data"]["stage_text"], "cT2b N2 M1a, Stage IVA, AJCC 8th")
        self.assertEqual(self.call("POST", f"/api/patients/{self.pid}/diagnoses", {"diagnosis_date": "2026-08-01", "primary_site": "Lung", "ecog": 7})[0], 422)

        url = f"/api/patients/{self.pid}/tumour-assessments"
        self.assertEqual(self.call("POST", url, {"assessment_date": "2026-08-10", "lesions": [{"location": "RUL", "kind": "target"}]})[0], 422)  # target needs a size
        status, base = self.call("POST", url, {"assessment_date": "2026-08-10", "modality": "ct", "timepoint": "baseline",
                                              "lesions": [{"location": "RUL mass", "size_mm": 48}, {"location": "Subcarinal node", "node": True, "size_mm": 22}],
                                              "markers": [{"name": "CEA", "value": "18.4", "unit": "ng/mL"}]})
        self.assertEqual((status, base["data"]["sum_mm"], base["data"]["response"]), (201, 70, None), base)
        status, mid = self.call("POST", url, {"assessment_date": "2026-10-01", "modality": "ct", "timepoint": "during", "cycle_label": "After cycle 3",
                                             "lesions": [{"location": "RUL mass", "size_mm": 30}, {"location": "Subcarinal node", "node": True, "size_mm": 15}]})
        self.assertEqual((status, mid["data"]["response"], mid["data"]["change_from_baseline_pct"]), (201, "PR", -35.7), mid)
        # A clinician-recorded response needs a reason.
        self.assertEqual(self.call("PATCH", f"/api/tumour-assessments/{mid['data']['id']}", {"response_override": "SD"})[0], 422)
        status, edited = self.call("PATCH", f"/api/tumour-assessments/{mid['data']['id']}", {"response_override": "SD", "response_note": "Measurement uncertain; repeat scan"})
        self.assertEqual((status, edited["data"]["response"], edited["data"]["computed"]), (200, "SD", "PR"))
        self.call("PATCH", f"/api/tumour-assessments/{mid['data']['id']}", {"response_override": None, "response_note": None})

        _, overview = self.call("GET", f"/api/patients/{self.pid}/oncology")
        self.assertEqual([s["sum_mm"] for s in overview["data"]["series"]], [70, 45])
        self.assertEqual(overview["data"]["best"]["response"], "PR")

        # Case sheet: timeline items + rule-based brief mention stage and the size trend.
        _, summary = self.call("GET", f"/api/patients/{self.pid}/summary")
        kinds = [i["kind"] for i in summary["data"]["items"]]
        self.assertIn("staging", kinds)
        self.assertEqual(kinds.count("response"), 2)
        self.assertEqual(summary["data"]["oncology"]["latest"]["response"], "PR")
        status, generated = self.call("POST", f"/api/patients/{self.pid}/summary/narrative", {})
        self.assertEqual(status, 200, generated)
        text = generated["data"]["narrative"]["text"]
        self.assertIn("from 70 mm", text)
        self.assertIn("response PR", text)
        self.assertIn("Stage IVA", text)

        # Discharge summary carries staging and the response table, on screen and in print.
        _, ds = self.call("POST", f"/api/patients/{self.pid}/discharge-summaries", {"kind": "end_of_treatment", "period_from": "2026-08-01"})
        content = ds["data"]["content"]
        self.assertEqual(content["staging"][0]["tnm"], "cT2b N2 M1a")
        self.assertEqual([a["response"] for a in content["response"]["assessments"]], [None, "PR"])
        self.assertIn("IVA", content["diagnosis"])
        status, page = self.call("GET", f"/api/discharge-summaries/{ds['data']['id']}/print")
        self.assertEqual(status, 200)

    def test_delete_assessment(self) -> None:
        _, a = self.call("POST", f"/api/patients/{self.pid}/tumour-assessments", {"assessment_date": "2026-09-01", "markers": [{"name": "CA-125", "value": "210", "unit": "U/mL"}]})
        self.assertEqual(self.call("DELETE", f"/api/tumour-assessments/{a['data']['id']}")[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/tumour-assessments/{a['data']['id']}")[0], 404)


if __name__ == "__main__":
    unittest.main()

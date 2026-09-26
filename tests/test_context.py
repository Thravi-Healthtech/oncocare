from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("ONCOCARE_DAILYMED", "off")
os.environ.setdefault("ONCOCARE_CONTEXT_DIR", tempfile.mkdtemp(prefix="oncocare-context-"))

from backend import context, extraction, reference  # noqa: E402
from backend.analysis import build_lab_trends  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

SPL = b"""<?xml version="1.0"?>
<document xmlns="urn:hl7-org:v3"><title>CISPLATIN injection</title>
<component><structuredBody>
 <component><section><code code="34066-1"/><title>BOXED WARNING</title>
  <text>Nephrotoxicity: cisplatin can cause severe renal toxicity, including acute renal failure. Peripheral neuropathy is dose related.</text></section></component>
 <component><section><code code="34084-4"/><title>ADVERSE REACTIONS</title>
  <text>Ototoxicity occurred in 31% of patients, manifested by tinnitus and/or hearing loss. Nausea and vomiting are common.
  Hypomagnesemia, hypocalcemia and hypokalemia were reported. Myelosuppression with leukopenia, thrombocytopenia and anemia.</text></section></component>
</structuredBody></component></document>"""


def fake_getter(url: str) -> bytes:
    if url.endswith(".xml"):
        return SPL
    return json.dumps({"data": [
        {"setid": "old-inj", "title": "CISPLATIN INJECTION, SOLUTION [OLDER]", "published_date": "Oct 18, 2023", "spl_version": 6},
        {"setid": "abc-oral", "title": "OTHER PRODUCT tablet", "published_date": "Aug 01, 2026"},
        {"setid": "set-123", "title": "CISPLATIN injection, solution", "published_date": "Jul 06, 2026", "spl_version": 4},
    ]}).encode()


def make_xlsx(path: Path, rows: list[list[str]]) -> None:
    strings: list[str] = []
    def idx(text: str) -> int:
        if text not in strings:
            strings.append(text)
        return strings.index(text)
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(f'<c r="{chr(65 + c)}{r}" t="s"><v>{idx(v)}</v></c>' for c, v in enumerate(row))
        sheet_rows.append(f'<row r="{r}">{cells}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(sheet_rows)}</sheetData></worksheet>")


class DailyMedTests(unittest.TestCase):
    def test_label_is_parsed_and_mapped_to_ctcae_terms(self) -> None:
        label = context.fetch_dailymed("CDDP", getter=fake_getter)
        self.assertEqual(label["setid"], "set-123")  # newest cisplatin injection, dates in DailyMed "Mon DD, YYYY" form
        self.assertEqual(label["published_date"], "2026-07-06")
        terms = label["terms"]
        self.assertTrue(terms["Creatinine increased"]["boxed"])
        self.assertIn("Boxed Warning", terms["Acute kidney injury"]["sections"])
        for term in ("Hearing impaired", "Tinnitus", "Hypomagnesemia", "Platelet count decreased", "Anemia", "Nausea"):
            self.assertIn(term, terms)
        self.assertNotIn("Alopecia", terms)
        self.assertIn("renal toxicity", terms["Creatinine increased"]["snippets"][0]["text"])
        self.assertEqual(context.cached_dailymed("cisplatin")["setid"], "set-123")

    def test_refresh_reports_failures_without_raising(self) -> None:
        def broken(url: str) -> bytes:
            raise OSError("offline")
        result = context.refresh_dailymed(["Paclitaxel"], getter=broken)
        self.assertFalse(result[0]["ok"])


class CtcaeImportTests(unittest.TestCase):
    def test_workbook_import_overlays_criteria(self) -> None:
        original = dict(reference.CTCAE_TERMS["Nausea"]["grades"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ctcae.xlsx"
            make_xlsx(path, [["NCI CTCAE v6.0"],
                             ["MedDRA Code", "MedDRA SOC", "CTCAE Term", "Grade 1", "Grade 2", "Grade 3", "Grade 4", "Grade 5", "Definition"],
                             ["10028813", "Gastrointestinal disorders", "Nausea", "New v6 grade one text", "New v6 grade two", "New v6 grade three", "-", "-", "Queasiness"]])
            result = context.import_ctcae_workbook(path)
        try:
            self.assertEqual(result["terms"], 1)
            self.assertEqual(reference.CTCAE_TERMS["Nausea"]["grades"][1], "New v6 grade one text")
            self.assertNotIn(4, reference.CTCAE_TERMS["Nausea"]["grades"])
            self.assertTrue(reference.term_provenance("Nausea").startswith("CTCAE v6.0 (NCI workbook"))
        finally:
            reference.CTCAE_TERMS["Nausea"]["grades"] = original
            reference.CTCAE_TERMS["Nausea"].pop("source", None)
            context.CTCAE_FILE.unlink(missing_ok=True)


class ExtractionTests(unittest.TestCase):
    def test_sample_pdf_reports_extract_all_panels(self) -> None:
        pdf = next((ROOT / "sample_reports").glob("4_*pre-C4.pdf"))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            result = extraction.extract_candidates(pdf.read_bytes(), "application/pdf", pdf.name)
        values = {c["test_name"]: c["value"] for c in result["candidates"]}
        self.assertEqual(result["report_date"], "2026-09-25")
        self.assertEqual(len(values), 20)
        self.assertEqual((values["Platelets"], values["WBC"], values["ANC"]), (92.0, 3.2, 1150.0))
        self.assertEqual((values["ALT"], values["Direct bilirubin"], values["eGFR"]), (132.0, 0.4, 42.0))

    def test_claude_extraction_is_used_and_cross_checked(self) -> None:
        pdf = next((ROOT / "sample_reports").glob("4_*pre-C4.pdf"))
        reply = {"content": [{"type": "text", "text": json.dumps({"collection_date": "2026-09-25", "results": [
            {"test_name": "Creatinine", "value": 1.6, "unit": "mg/dL", "ref_low": 0.6, "ref_high": 1.1},
            {"test_name": "Platelets", "value": "0.92", "unit": "lakh/cumm", "ref_low": 1.5, "ref_high": 4.5},
            {"test_name": "ALT", "value": 123, "unit": "IU/L", "ref_low": 0, "ref_high": 41}]})}]}
        captured = {}
        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data)
            return io.BytesIO(json.dumps(reply).encode())
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ONCOCARE_EXTRACTION": "auto"}), \
                mock.patch("backend.extraction.urllib.request.urlopen", fake_urlopen):
            result = extraction.extract_candidates(pdf.read_bytes(), "application/pdf", pdf.name)
        self.assertEqual(result["method"], "claude")
        self.assertEqual(captured["body"]["messages"][0]["content"][0]["type"], "document")
        by = {c["test_name"]: c for c in result["candidates"]}
        self.assertEqual(by["Platelets"]["value"], 92.0)
        self.assertIn("Cross-checked", by["Creatinine"]["note"])
        self.assertIn("text layer reads 132", by["ALT"]["note"])

    def test_photo_without_ai_explains_next_step(self) -> None:
        photo = next((ROOT / "sample_reports").glob("*PHOTO.jpg"))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            result = extraction.extract_candidates(photo.read_bytes(), "image/jpeg", photo.name)
        self.assertEqual(result["candidates"], [])
        self.assertIn("AI extraction", result["note"])


class AiSettingsTests(unittest.TestCase):
    def test_key_is_validated_then_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1\n")
            with mock.patch.object(extraction, "ENV_FILE", env), \
                    mock.patch.object(extraction, "_anthropic_post", side_effect=RuntimeError("Claude API HTTP 401: invalid x-api-key")):
                with self.assertRaises(RuntimeError):
                    extraction.save_ai_settings("sk-ant-bad-000000000000000000000", "claude-sonnet-5")
            self.assertNotIn("ANTHROPIC_API_KEY", env.read_text())
            with mock.patch.object(extraction, "ENV_FILE", env), \
                    mock.patch.object(extraction, "_anthropic_post", return_value={"model": "claude-haiku-4-5-20251001"}):
                status = extraction.save_ai_settings("sk-ant-good-00000000000000000000", "claude-haiku-4-5-20251001")
            self.assertTrue(status["enabled"])
            self.assertEqual(status["key_hint"], "…0000")
            text = env.read_text()
            self.assertIn("OTHER=1", text)
            self.assertIn("ANTHROPIC_API_KEY=sk-ant-good-00000000000000000000", text)
            with self.assertRaises(ValueError):
                extraction.save_ai_settings("not a key", "claude-sonnet-5")


class FlagTests(unittest.TestCase):
    def test_trend_flags_for_cisplatin_series(self) -> None:
        cycles = [{"id": i, "cycle_number": i, "planned_date": d, "administered_date": None}
                  for i, d in enumerate(["2026-08-04", "2026-08-25", "2026-09-15", "2026-10-06", "2026-10-27"], start=1)]
        labs, n = [], 0
        for cid, date, cr, mg in ((1, "2026-08-03", 0.7, 2.1), (2, "2026-08-24", 0.9, 1.8), (3, "2026-09-14", 1.2, 1.5), (4, "2026-09-25", 1.6, 1.1)):
            for test, value, low, high, unit in (("Creatinine", cr, 0.6, 1.1, "mg/dL"), ("Magnesium", mg, 1.7, 2.2, "mg/dL")):
                n += 1
                labs.append({"id": n, "cycle_id": cid, "test_name": test, "value": value, "unit": unit, "ref_low": low, "ref_high": high,
                             "collection_date": date, "verification_status": "verified"})
        trends = {t["test_name"]: t for t in build_lab_trends(cycles, labs)}
        codes = {f["code"] for f in trends["Creatinine"]["flags"]}
        self.assertTrue({"baseline_change", "steady_trend", "kdigo_creatinine"} <= codes)
        self.assertIn("ctcae_rise", {f["code"] for f in trends["Creatinine"]["points"][2]["flags"]})  # 1.2 = 1.7x baseline -> G2 at C3
        self.assertEqual(trends["Creatinine"]["flags"][0]["severity"], "alert")
        self.assertEqual(trends["Creatinine"]["latest"]["ctcae_grade"], 2)
        mg_codes = {f["code"] for f in trends["Magnesium"]["flags"]}
        self.assertIn("ctcae_rise", mg_codes)
        # C2 window: creatinine still in range but rising towards ULN
        c2 = trends["Creatinine"]["points"][1]
        self.assertFalse(c2["abnormal"])


if __name__ == "__main__":
    unittest.main()

"""PDF versions of the documents patients receive on WhatsApp: the finalised discharge / end-of-treatment summary and
stand-alone prescriptions. The layout follows the browser print views (discharge.print_html, prescriptions.print_html)."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from . import discharge, prescriptions
from . import radiotherapy as rt
from .database import row_or_none
from .pdfdoc import Pdf

EMERGENCY = "If you feel very unwell (fever 38 C or more, bleeding, breathlessness, or you cannot eat or drink), contact your care team or call 112."


def _day(value: str | None) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(value or "")


def _age(dob: str | None) -> str:
    try:
        born, today = date.fromisoformat(str(dob)), date.today()
    except (TypeError, ValueError):
        return ""
    return f"{today.year - born.year - ((today.month, today.day) < (born.month, born.day))} y"


def discharge_pdf(connection: sqlite3.Connection, summary_id: int) -> tuple[bytes, str]:
    """(pdf bytes, file name) for a summary. Only finalised summaries are shared with patients."""
    from . import whatsapp
    view = discharge._view(discharge._load(connection, summary_id))
    c, p = view["content"], view["content"]["patient"]
    age = f"{p['age']} y" if p.get("age") is not None else ""
    doc = Pdf(c["title"], f"{whatsapp.clinic()} - {c.get('consultant') or ''}".strip(" -"),
              f"Period: {_day(view['period_from'])} to {_day(view['period_to'])}",
              f"{c['title']} - {p['first_name']} {p['last_name']} - version {view['version']}")
    doc.kv([("Patient", f"{p['first_name']} {p['last_name']}"), ("MRN", p["medical_record_number"]),
            ("Age / sex", f"{age} {p.get('sex') or ''}".strip()), ("Mobile", p.get("phone") or "-")])
    doc.heading("Diagnosis")
    doc.para(c.get("diagnosis") or "-")
    for d in c.get("staging") or []:
        site = " ".join(x for x in ((d.get("laterality") or "").replace("na", "").capitalize(), d["primary_site"]) if x)
        pairs = [("Primary site", site), ("Diagnosed", _day(d["diagnosis_date"]) + (f" by {d['method']}" if d.get("method") else "")),
                 ("Histology", " ".join(x for x in (d.get("histology") or "", f"(grade {d['grade']})" if d.get("grade") else "") if x)),
                 ("TNM / stage", ", ".join(x for x in (d.get("tnm") or "", f"Stage {d['stage_group']}" if d.get("stage_group") else "", d.get("staging_system") or "") if x)),
                 ("Biomarkers", ", ".join(f"{b['name']} {b['value']}" for b in d.get("biomarkers") or [])),
                 ("Intent / ECOG", ", ".join(x for x in ((d.get("intent") or "").capitalize(), f"ECOG {d['ecog']}" if d.get("ecog") is not None else "") if x))]
        doc.heading("Diagnosis and staging")
        doc.table(["Item", "Details"], [(k, v) for k, v in pairs if v], [1, 3])
    resp = c.get("response") or {}
    if resp.get("assessments"):
        doc.heading("Tumour response")
        doc.table(["Date", "Assessment", "Target lesions", "Sum", "vs baseline", "Response"],
                  [(_day(a["assessment_date"]), a["modality_label"] + (f", {a['cycle_label']}" if a.get("cycle_label") else ""),
                    "; ".join(a["targets"]) + ("; new lesion" if a.get("new") else ""),
                    f"{a['sum_mm']:g} mm" if a["sum_mm"] is not None else "-",
                    f"{a['change_from_baseline_pct']:+.0f}%" if a["change_from_baseline_pct"] is not None else "-",
                    a.get("response") or "Baseline") for a in resp["assessments"]], [1.1, 1.4, 2.2, 0.7, 0.8, 0.9])
        if resp.get("best"):
            doc.para(f"Best response: {resp['best']}" + (f"; latest: {resp['latest']}" if resp.get("latest") else "") + " (RECIST 1.1 unless recorded by the clinician).", 9)
    for x in c.get("surgery") or []:
        doc.heading("Surgery")
        when = (("Operated " if x["status"] in ("completed", "discharged") else "Planned for ") + _day(x["surgery_date"])) if x.get("surgery_date") else x["status_label"]
        doc.para(f"{x['procedure']}: {when}" + (f", discharged {_day(x['discharge_date'])}" if x.get("discharge_date") else "") + ".", bold=True)
        if x.get("findings"):
            doc.para(f"Findings: {x['findings']}", 9.5)
        if x.get("pathology"):
            doc.para(f"Histopathology: {x['pathology'].get('histology') or ''}" + (f", {x['stage']}" if x.get("stage") else ""), 9.5)
        if x.get("complications"):
            doc.para("Complications: " + "; ".join(x["complications"]), 9.5)
    if c["chemotherapy"]:
        doc.heading("Chemotherapy")
        for x in c["chemotherapy"]:
            doc.para(f"{x['regimen']} ({', '.join(x['drugs'])}): {x['cycles_given']} of {x['cycles_planned']} cycles"
                     + (f", {_day(x['first_date'])} to {_day(x['last_date'])}" if x["first_date"] else "") + (f". {'; '.join(x['changes'])}" if x["changes"] else "") + ".")
    if c["radiotherapy"]:
        doc.heading("Radiotherapy")
        for x in c["radiotherapy"]:
            doc.para(f"{x['site']}: {rt.gy(x['dose_delivered_cgy'])} in {x['fractions_delivered']} of {x['fractions_planned']} fractions"
                     + (f", {_day(x['first_date'])} to {_day(x['last_date'])}" if x["first_date"] else "") + ".")
    course = discharge._strip_citations((c.get("course") or {}).get("text", ""))
    if course:
        doc.heading("Course during treatment")
        doc.para(course)
    if c["toxicities"]:
        doc.heading("Side effects (CTCAE)")
        doc.table(["Side effect", "Worst grade", "Date"], [(t["term"], f"Grade {t['worst_grade']}", _day(t["date"])) for t in c["toxicities"]], [3, 1, 1.2])
    if c["labs"]:
        doc.heading("Latest investigations")
        doc.table(["Test", "Value", "Date"], [(l["test_name"], f"{l['value']:g} {l['unit']}" + (f" ({l['flag']})" if l["flag"] else ""), _day(l["collection_date"]))
                                             for l in c["labs"]], [2, 1.5, 1.2])
    doc.heading("Condition at discharge")
    doc.para(c.get("condition_at_discharge") or "-")
    doc.heading("Medicines")
    if c["medications"]:
        doc.table(["Medicine", "Dose", "Route", "How often", "For how long", "Instructions"],
                  [(m["name"], m["dose"], m.get("route", ""), m["frequency"], m["duration"], m["instructions"]) for m in c["medications"]], [2, 0.9, 0.8, 1.5, 1, 1.6])
    else:
        doc.para("None.")
    if c.get("advice"):
        doc.heading("Advice")
        doc.para(c["advice"])
    fu = c.get("follow_up") or {}
    doc.heading("Follow-up")
    if fu.get("none"):
        doc.para("No follow-up visit booked.")
    else:
        doc.para((_day(fu["date"]) if fu.get("date") else "-") + (f" with {fu['with']}" if fu.get("with") else ""), bold=True)
        if fu.get("instructions"):
            doc.para(fu["instructions"])
    doc.space(10)
    doc.para(f"Reviewed and finalised by {view['finalised_by']} on {_day(view['finalised_at'])}." if view["status"] != "draft"
             else "DRAFT - not reviewed. Not for clinical use or sharing.", 8.5, color=(0.42, 0.45, 0.48))
    doc.para(EMERGENCY, 8.5, color=(0.42, 0.45, 0.48))
    name = f"{c['title']} - {p['first_name']} {p['last_name']}.pdf"
    return doc.bytes(), name


def prescription_pdf(connection: sqlite3.Connection, rx_id: int) -> tuple[bytes, str]:
    from . import whatsapp
    row = prescriptions._load(connection, rx_id)
    rx = prescriptions._view(dict(row))
    p = row_or_none(connection, "SELECT * FROM patients WHERE id = ?", (row["patient_id"],))
    contact = whatsapp.clinic() + (f" - {whatsapp.care_phone()}" if whatsapp.care_phone() else "")
    doc = Pdf("Prescription", contact, f"{_day(rx['rx_date'])} - {rx['context_label']}", f"Prescription - {p['first_name']} {p['last_name']} - {_day(rx['rx_date'])}")
    doc.kv([("Patient", f"{p['first_name']} {p['last_name']}"), ("MRN", p["medical_record_number"]),
            ("Age / sex", f"{_age(p.get('date_of_birth'))} {p.get('sex') or ''}".strip()), ("Mobile", p.get("phone") or "-")])
    if rx.get("indication"):
        doc.heading("Reason")
        doc.para(rx["indication"])
    doc.heading("Medicines")
    doc.table(["Medicine", "Dose", "Route", "How often", "For how long", "Instructions"],
              [(m["name"], m["dose"], m.get("route", ""), m["frequency"], m["duration"], m["instructions"]) for m in rx["items"]], [2, 0.9, 0.8, 1.5, 1, 1.6])
    if rx.get("advice"):
        doc.heading("Advice")
        doc.para(rx["advice"])
    if rx["status"] == "cancelled":
        doc.heading("Cancelled")
        doc.para(f"{rx.get('cancel_reason') or ''} - {rx.get('cancelled_by') or ''}", bold=True)
    doc.space(14)
    doc.para(f"Prescribed by {rx['prescribed_by']} on {_day(rx['created_at'])}.", 9.5, bold=True)
    doc.para("Take the medicines as written. " + EMERGENCY, 8.5, color=(0.42, 0.45, 0.48))
    return doc.bytes(), f"Prescription {_day(rx['rx_date'])} - {p['first_name']} {p['last_name']}.pdf"


def save(data: bytes, folder: Path, stem: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.pdf"
    path.write_bytes(data)
    return path

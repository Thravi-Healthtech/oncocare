"""Clinical notes, uploaded reports and the patient's running summary.

The running summary is assembled from the record at request time: chemotherapy cycles and decisions, radiotherapy
fractions/reviews/interruptions, clinician-confirmed CTCAE toxicities, verified labs, patient complaints (portal or
WhatsApp) with their AI interpretation, clinical notes and uploaded reports. Every item has a source id (S1, S2…).
An optional narrative paragraph is written from those sources only — by Claude when AI is on, otherwise by fixed
rules — and every sentence cites the sources it came from so the clinician can check it.
"""
from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from . import extraction
from . import radiotherapy as rt
from .database import row_or_none, rows, transaction, utc_now
from .errors import ApiError

NOTE_CATEGORIES = {"progress": "Progress note", "procedure": "Procedure", "admission": "Admission", "phone": "Phone call",
                   "counselling": "Counselling", "tumour_board": "Tumour board", "other": "Other"}
DOC_TYPES = {"imaging": "Imaging report", "pathology": "Histopathology", "lab": "Lab report", "discharge": "Previous discharge summary",
             "referral": "Referral letter", "other": "Other report"}
ALLOWED_UPLOADS = ("application/pdf", "image/png", "image/jpeg", "image/webp", "text/plain")
KEY_LABS = ("Hemoglobin", "WBC", "ANC", "Platelets", "Creatinine", "Sodium", "Potassium", "Magnesium", "ALT", "AST", "Bilirubin", "Albumin")


def _text(value: Any, limit: int) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _iso(value: Any, label: str, default: str | None = None) -> str:
    raw = value or default
    try:
        parsed = date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        raise ApiError(422, f"{label} must use YYYY-MM-DD format") from None
    if parsed > date.today():
        raise ApiError(422, f"{label} cannot be in the future")
    return parsed.isoformat()


def _patient(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any]:
    patient = row_or_none(connection, "SELECT * FROM patients WHERE id = ?", (patient_id,))
    if not patient:
        raise ApiError(404, "Patient not found")
    return patient


# ---------------------------------------------------------------------------------------------- notes
def list_notes(db: str, patient_id: int) -> list[dict[str, Any]]:
    with transaction(db) as connection:
        _patient(connection, patient_id)
        return rows(connection, "SELECT * FROM clinical_notes WHERE patient_id = ? ORDER BY note_date DESC, id DESC", (patient_id,))


def create_note(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    text = _text(payload.get("text"), 4000)
    if not text:
        raise ApiError(422, "Write the note", {"fields": ["text"]})
    category = payload.get("category") or "progress"
    if category not in NOTE_CATEGORIES:
        raise ApiError(422, "Unknown note category")
    note_date = _iso(payload.get("note_date"), "Note date", date.today().isoformat())
    now = utc_now()
    with transaction(db) as connection:
        _patient(connection, patient_id)
        note_id = connection.execute(
            "INSERT INTO clinical_notes (patient_id, note_date, category, text, author, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (patient_id, note_date, category, text, _text(payload.get("author"), 80) or actor, now, now)).lastrowid
        return row_or_none(connection, "SELECT * FROM clinical_notes WHERE id = ?", (note_id,))


def update_note(db: str, note_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    with transaction(db) as connection:
        note = row_or_none(connection, "SELECT * FROM clinical_notes WHERE id = ?", (note_id,))
        if not note:
            raise ApiError(404, "Note not found")
        text = _text(payload.get("text", note["text"]), 4000)
        if not text:
            raise ApiError(422, "Write the note", {"fields": ["text"]})
        category = payload.get("category", note["category"])
        if category not in NOTE_CATEGORIES:
            raise ApiError(422, "Unknown note category")
        note_date = _iso(payload.get("note_date", note["note_date"]), "Note date")
        connection.execute("UPDATE clinical_notes SET text = ?, category = ?, note_date = ?, updated_at = ? WHERE id = ?",
                           (text, category, note_date, utc_now(), note_id))
        return row_or_none(connection, "SELECT * FROM clinical_notes WHERE id = ?", (note_id,))


def delete_note(db: str, note_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        if connection.execute("DELETE FROM clinical_notes WHERE id = ?", (note_id,)).rowcount == 0:
            raise ApiError(404, "Note not found")
    return {"id": note_id, "deleted": True}


# ---------------------------------------------------------------------------------------------- documents
def _doc_view(doc: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in doc.items() if k != "stored_path"}
    view["doc_type_label"] = DOC_TYPES.get(doc["doc_type"], doc["doc_type"])
    return view


def list_documents(db: str, patient_id: int) -> list[dict[str, Any]]:
    with transaction(db) as connection:
        _patient(connection, patient_id)
        docs = rows(connection, "SELECT * FROM patient_documents WHERE patient_id = ? ORDER BY doc_date DESC, id DESC", (patient_id,))
    return [_doc_view(d) for d in docs]


def _ai_summary(data: bytes, content_type: str, text: str, doc_type: str) -> str:
    status = extraction.ai_status()
    instruction = (f"This is a patient's {DOC_TYPES.get(doc_type, 'medical report').lower()} uploaded to an oncology record. Summarise the "
                   "findings and impression for the treating oncologist in at most 3 short sentences. Quote measurements and dates exactly as "
                   "printed. Do not add interpretation, advice or anything not written in the report. If it is not readable, say so.")
    if content_type == "application/pdf":
        source: dict[str, Any] = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}}
    elif content_type in ("image/png", "image/jpeg", "image/webp"):
        source = {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": base64.b64encode(data).decode()}}
    else:
        source = {"type": "text", "text": text[:40000] or "(empty)"}
    reply = extraction._anthropic_post(extraction.os.environ["ANTHROPIC_API_KEY"],
                                       {"model": status["model"], "max_tokens": 400,
                                        "messages": [{"role": "user", "content": [source, {"type": "text", "text": instruction}]}]}, 90)
    return "".join(part.get("text", "") for part in reply.get("content", [])).strip()[:1200]


def upload_document(db: str, patient_id: int, payload: dict[str, Any], actor: str, upload_dir: Path) -> dict[str, Any]:
    filename = Path(str(payload.get("filename") or "")).name[:120]
    if not filename or not payload.get("data_base64"):
        raise ApiError(422, "Choose a file to upload", {"fields": ["filename"]})
    content_type = str(payload.get("content_type") or mimetypes.guess_type(filename)[0] or "")
    if content_type not in ALLOWED_UPLOADS:
        raise ApiError(415, "Upload a PDF, image or text report")
    doc_type = payload.get("doc_type") or "other"
    if doc_type not in DOC_TYPES:
        raise ApiError(422, "Unknown report type")
    title = _text(payload.get("title"), 160) or DOC_TYPES[doc_type]
    doc_date = _iso(payload.get("doc_date"), "Report date", date.today().isoformat())
    try:
        data = base64.b64decode(str(payload["data_base64"]), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, "File content is not valid base64") from None
    if not data or len(data) > 10_000_000:
        raise ApiError(413, "File must be between 1 byte and 10 MB")
    with transaction(db) as connection:
        _patient(connection, patient_id)
    text, _ = extraction.extract_text(data, content_type, filename)
    summary, method = None, None
    if extraction.ai_status()["enabled"]:
        try:
            summary, method = _ai_summary(data, content_type, text, doc_type), "claude"
        except Exception as error:  # keep the upload; summary can be retried
            summary, method = None, f"failed: {str(error)[:120]}"
    folder = upload_dir / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / f"{uuid.uuid4().hex}-{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}"
    stored.write_bytes(data)
    with transaction(db) as connection:
        doc_id = connection.execute(
            """INSERT INTO patient_documents (patient_id, doc_type, title, doc_date, filename, content_type, stored_path, size_bytes,
                   text_excerpt, ai_summary, summary_method, note, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (patient_id, doc_type, title, doc_date, filename, content_type, str(stored), len(data), (text or "")[:3000] or None,
             summary, method, _text(payload.get("note"), 1000), actor, utc_now())).lastrowid
        return _doc_view(row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ?", (doc_id,)))


def update_document(db: str, doc_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    with transaction(db) as connection:
        doc = row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ?", (doc_id,))
        if not doc:
            raise ApiError(404, "Report not found")
        fields: dict[str, Any] = {}
        if "title" in payload:
            fields["title"] = _text(payload["title"], 160) or doc["title"]
        if "note" in payload:
            fields["note"] = _text(payload["note"], 1000)
        if "ai_summary" in payload:  # clinician correction of the summary
            fields["ai_summary"] = _text(payload["ai_summary"], 1200)
            fields["summary_method"] = "edited"
        if "doc_type" in payload:
            if payload["doc_type"] not in DOC_TYPES:
                raise ApiError(422, "Unknown report type")
            fields["doc_type"] = payload["doc_type"]
        if "doc_date" in payload:
            fields["doc_date"] = _iso(payload["doc_date"], "Report date")
        if not fields:
            raise ApiError(422, "No supported fields supplied")
        connection.execute(f"UPDATE patient_documents SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), doc_id))
        return _doc_view(row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ?", (doc_id,)))


def summarise_document(db: str, doc_id: int) -> dict[str, Any]:
    if not extraction.ai_status()["enabled"]:
        raise ApiError(422, "AI is off. Add an Anthropic API key under AI settings to summarise reports.")
    with transaction(db) as connection:
        doc = row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ?", (doc_id,))
    if not doc:
        raise ApiError(404, "Report not found")
    data = Path(doc["stored_path"]).read_bytes()
    try:
        summary = _ai_summary(data, doc["content_type"] or "", doc["text_excerpt"] or "", doc["doc_type"])
    except Exception as error:
        raise ApiError(502, f"The AI request failed ({str(error)[:160]})") from None
    with transaction(db) as connection:
        connection.execute("UPDATE patient_documents SET ai_summary = ?, summary_method = 'claude' WHERE id = ?", (summary, doc_id))
        return _doc_view(row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ?", (doc_id,)))


def delete_document(db: str, doc_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        doc = row_or_none(connection, "SELECT stored_path FROM patient_documents WHERE id = ?", (doc_id,))
        if not doc:
            raise ApiError(404, "Report not found")
        connection.execute("DELETE FROM patient_documents WHERE id = ?", (doc_id,))
    try:
        Path(doc["stored_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return {"id": doc_id, "deleted": True}


def document_file(db: str, doc_id: int, upload_dir: Path) -> Any:
    from .routes_ext import RawResponse
    with transaction(db) as connection:
        doc = row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ?", (doc_id,))
    if not doc:
        raise ApiError(404, "Report not found")
    return RawResponse(str(doc["stored_path"]).encode(), "file:" + (doc["content_type"] or "application/octet-stream"), doc["filename"])


# ---------------------------------------------------------------------------------------------- timeline
def _fmt(value: float) -> str:
    return f"{value:g}"


def collect(connection: sqlite3.Connection, patient_id: int, period_from: str | None = None, period_to: str | None = None) -> dict[str, Any]:
    """Facts and dated source items for one patient, optionally limited to a period (inclusive)."""
    patient = _patient(connection, patient_id)
    items: list[dict[str, Any]] = []

    def within(day: str | None) -> bool:
        return bool(day) and (not period_from or day >= period_from) and (not period_to or day <= period_to)

    def add(day: str, kind: str, title: str, detail: str, ref: dict[str, Any], severity: str | None = None, always: bool = False) -> None:
        if within(day) or (always and day):
            items.append({"date": day, "kind": kind, "title": title, "detail": detail, "ref": ref, "severity": severity})

    # Chemotherapy
    chemo = []
    for plan in rows(connection, "SELECT * FROM treatment_plans WHERE patient_id = ? ORDER BY start_date, id", (patient_id,)):
        meds = rows(connection, "SELECT drug_name, dose, dose_unit, route FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order, id", (plan["id"],))
        cycles = rows(connection, "SELECT * FROM treatment_cycles WHERE treatment_plan_id = ? ORDER BY cycle_number", (plan["id"],))
        given = [c for c in cycles if c["status"] in ("given", "modified") and within(c["administered_date"] or c["planned_date"])]
        changes = []
        for c in cycles:
            day = c["administered_date"] or c["planned_date"]
            cmeds = rows(connection, "SELECT drug_name, planned_dose, actual_dose, dose_unit, status FROM cycle_medications WHERE cycle_id = ? ORDER BY sort_order", (c["id"],))
            doses = ", ".join(f"{m['drug_name']} {_fmt(m['actual_dose'] if m['actual_dose'] is not None else m['planned_dose'])} {m['dose_unit']}"
                              + (" (held)" if m["status"] == "held" else "") + (f" (planned {_fmt(m['planned_dose'])})" if m["status"] == "modified" else "")
                              for m in cmeds)
            if c["status"] in ("given", "modified"):
                add(day, "chemo", f"{plan['regimen_name']}: cycle {c['cycle_number']} {'given with modification' if c['status'] == 'modified' else 'given'}",
                    doses + (f". Reason: {c['modification_reason']}" if c["modification_reason"] else ""), {"type": "cycle", "id": c["id"], "plan_id": plan["id"]},
                    "warning" if c["status"] == "modified" else None)
            elif c["status"] in ("delayed", "held", "discontinued") and c["recorded_at"]:
                decided = c["recorded_at"][:10]
                add(decided, "chemo", f"{plan['regimen_name']}: cycle {c['cycle_number']} {c['status']}",
                    c["modification_reason"] or "", {"type": "cycle", "id": c["id"], "plan_id": plan["id"]}, "warning")
                if within(decided):
                    changes.append(f"C{c['cycle_number']} {c['status']}")
            if c["status"] == "modified" and within(day):
                changes.append(f"C{c['cycle_number']} modified")
        nxt = next((c for c in cycles if c["status"] in ("planned", "delayed")), None)
        if given or (not period_from and plan["status"] in ("active", "planned")):
            chemo.append({"plan_id": plan["id"], "regimen": plan["regimen_name"], "intent": plan["intent"], "status": plan["status"],
                          "drugs": [f"{m['drug_name']} {_fmt(m['dose'])} {m['dose_unit']}" for m in meds],
                          "cycles_given": len(given), "cycles_planned": len(cycles),
                          "first_date": (given[0]["administered_date"] or given[0]["planned_date"]) if given else None,
                          "last_date": (given[-1]["administered_date"] or given[-1]["planned_date"]) if given else None,
                          "changes": changes, "next_cycle": {"number": nxt["cycle_number"], "date": nxt["planned_date"]} if nxt else None})

    # Radiotherapy
    radiotherapy = []
    for course in rows(connection, "SELECT id FROM rt_courses WHERE patient_id = ? ORDER BY id", (patient_id,)):
        w = rt.build(connection, course["id"])
        c, s = w["course"], w["summary"]
        fx = [f for f in w["fractions"] if within(f["treatment_date"])]
        if not fx and period_from and not within(c["start_date"]):
            continue
        # One item per treatment week rather than per fraction.
        weeks: dict[int, list[dict[str, Any]]] = {}
        if w["fractions"]:
            first = date.fromisoformat(w["fractions"][0]["treatment_date"])
            for f in fx:
                weeks.setdefault((date.fromisoformat(f["treatment_date"]) - first).days // 7 + 1, []).append(f)
        for week, group in weeks.items():
            add(group[-1]["treatment_date"], "rt", f"Radiotherapy week {week}: fractions {group[0]['number']}–{group[-1]['number']}",
                f"{len(group)} fraction(s) {group[0]['treatment_date']} to {group[-1]['treatment_date']}; cumulative {rt.gy(group[-1]['course_total_cgy'])} "
                f"of {rt.gy(s['dose_planned_cgy'])}", {"type": "rt_course", "id": c["id"]})
        for gap in s["gaps"]:
            add(gap["date"], "rt", "Radiotherapy not given", f"{gap['weekday']} {gap['date']}: {gap['category'] or 'no reason recorded'}"
                + (f" ({gap['reason']})" if gap["reason"] else ""), {"type": "rt_course", "id": c["id"]}, "warning" if not gap["explained"] else None)
        for review in w["reviews"]:
            graded = [f"{i['ctcae_term']} G{i['grade']}" + (f" ({i['note']})" if i["note"] else "") for i in review["items"] if i["grade"]]
            add(review["review_date"], "rt_review", f"Radiotherapy week {review['week_number']} review",
                (", ".join(graded) or "No acute reactions") + (f"; weight {_fmt(review['weight_kg'])} kg" if review["weight_kg"] else "")
                + f"; {review['decision_label']}" + (f": {review['rationale']}" if review["rationale"] else ""),
                {"type": "rt_review", "id": review["id"], "course_id": c["id"]},
                "alert" if any(i["grade"] >= 3 for i in review["items"]) else "warning" if any(i["grade"] >= 2 for i in review["items"]) else None)
        if c["completion"]:
            add(c["completion"]["end_date"], "rt", f"Radiotherapy {c['completion']['outcome_label'].lower()}",
                f"{rt.gy(c['completion']['dose_delivered_cgy'])} in {c['completion']['fractions_delivered']} fractions, "
                f"{c['completion']['start_date']} to {c['completion']['end_date']}" + (f". {c['completion']['notes']}" if c["completion"].get("notes") else ""),
                {"type": "rt_course", "id": c["id"]})
        delivered = round(sum(f["dose_cgy"] for f in fx), 2)
        radiotherapy.append({
            "course_id": c["id"], "site": c["site"], "intent": c["intent"], "status": c["status"], "stage": c["stage"],
            "histopathology": c["histopathology"], "diagnosis": c["diagnosis"], "prescription": s["prescription"],
            "phases": [{"name": p["name"], "technique": p["technique"], "energy": p["energy"], "dose_per_fraction_cgy": p["dose_per_fraction_cgy"],
                        "fractions_planned": p["fractions_planned"], "fractions_delivered": p["fractions_delivered"],
                        "dose_delivered_cgy": p["dose_delivered_cgy"], "dose_planned_cgy": p["dose_planned_cgy"]} for p in w["phases"]],
            "fractions_delivered": len(fx), "fractions_planned": s["fractions_planned"], "dose_delivered_cgy": delivered,
            "dose_planned_cgy": s["dose_planned_cgy"], "first_date": fx[0]["treatment_date"] if fx else None, "last_date": fx[-1]["treatment_date"] if fx else None,
            "next_fraction_date": s["next_fraction_date"], "projected_end_date": s["projected_end_date"],
            "gaps": [g for g in s["gaps"] if within(g["date"])], "overall_treatment_days": s["overall_treatment_days"],
            "worst_toxicities": [t for t in s["worst_toxicities"] if within(t["date"])], "weight_change": s["weight_change"],
            "completion": c["completion"], "concurrent_chemo": w["chemo_plan"]["regimen_name"] if w["chemo_plan"] else None})

    # Surgery
    from . import surgery as sx
    surgeries = []
    for row in rows(connection, "SELECT id FROM surgical_cases WHERE patient_id = ? ORDER BY COALESCE(surgery_date, created_at), id", (patient_id,)):
        w = sx.build(connection, row["id"])
        c = w["case"]
        op, path, dis = c["operation"] or {}, c["pathology"] or {}, c["discharge"] or {}
        ref = {"type": "surgery", "id": c["id"]}
        for e in w["events"]:
            if e["kind"] == "plan":
                add(e["event_date"], "surgery", f"Surgery planned: {c['procedure_planned']}",
                    "; ".join(x for x in (c["indication"], f"scheduled {c['surgery_date']}" if c["surgery_date"] else None, f"surgeon {c['surgeon']}" if c["surgeon"] else None) if x),
                    ref)
            elif e["kind"] == "operation":
                add(e["event_date"], "surgery", f"Operation: {op.get('procedure_performed') or c['procedure_planned']}",
                    "; ".join(x for x in (op.get("approach"), op.get("anaesthesia") and f"{op['anaesthesia']} anaesthesia", op.get("findings") and f"findings: {op['findings']}",
                                          op.get("frozen_section") and f"frozen section: {op['frozen_section']}", op.get("ebl_ml") is not None and f"blood loss {op['ebl_ml']} ml",
                                          op.get("drains") and f"drains: {op['drains']}", op.get("intraop_events") and f"intra-operative: {op['intraop_events']}") if x),
                    ref)
            elif e["kind"] == "pathology":
                add(e["event_date"], "surgery", "Final histopathology",
                    "; ".join(x for x in (path.get("histology"), path.get("grade") and f"grade {path['grade']}", path.get("size_mm") and f"{path['size_mm']} mm",
                                          " ".join(y for y in (path.get("pt"), path.get("pn")) if y) or None, path.get("margins") and f"margins {path['margins']}"
                                          + (f" ({path['closest_margin_mm']} mm)" if path.get("closest_margin_mm") is not None else ""),
                                          path.get("nodes_examined") is not None and f"nodes {path.get('nodes_positive', 0)}/{path['nodes_examined']}",
                                          path.get("lvi") and f"LVI {path['lvi']}", path.get("biomarkers")) if x),
                    ref, "warning" if path.get("margins") in ("positive", "close") else None)
            elif e["kind"] == "discharge":
                add(e["event_date"], "surgery", "Discharged after surgery", e["text"] + (f"; wound care: {dis['wound_care']}" if dis.get("wound_care") else ""), ref)
            elif e["kind"] == "status":
                add(e["event_date"], "surgery", "Surgery plan changed", e["text"], ref, "warning")
            else:
                pod = f"POD {e['pod']} · " if e["pod"] is not None else ""
                add(e["event_date"], "surgery_note", f"{pod}{e['kind_label']}" + (f": {e['complication']}" if e["complication"] else "")
                    + (f" (Clavien–Dindo {e['clavien_dindo']})" if e["clavien_dindo"] else "") + f" — {e['author']}",
                    e["text"] + (f" Drain {e['drain_output_ml']:g} ml." if e["drain_output_ml"] is not None else "") + (f" Pain {e['pain_score']}/10." if e["pain_score"] is not None else ""),
                    ref, "alert" if e["clavien_dindo"] and sx.CD_ORDER.index(e["clavien_dindo"]) >= 2 else "warning" if e["clavien_dindo"] else None)
        relevant = [d for d in (c["surgery_date"], c["admission_date"], c["discharge_date"]) if d]
        # a case belongs to the period if a clinical date falls in it, or it is still open (planned, scheduled, in hospital)
        if any(within(d) for d in relevant) or not period_from or (c["status"] in ("planned", "scheduled", "completed") and not relevant):
            surgeries.append({"case_id": c["id"], "procedure_planned": c["procedure_planned"], "procedure": op.get("procedure_performed") or c["procedure_planned"],
                              "status": c["status"], "status_label": c["status_label"], "intent": c["intent"], "surgeon": op.get("surgeon") or c["surgeon"],
                              "surgery_date": c["surgery_date"], "admission_date": c["admission_date"], "discharge_date": c["discharge_date"],
                              "approach": op.get("approach"), "findings": op.get("findings"), "pathology": path or None, "stage": w["summary"]["stage"],
                              "worst_clavien_dindo": w["summary"]["worst_clavien_dindo"],
                              "complications": [f"{e['complication'] or e['kind_label']} (Clavien–Dindo {e['clavien_dindo']}, {e['event_date']})" for e in w["events"] if e["clavien_dindo"]],
                              "length_of_stay": w["summary"]["length_of_stay"], "cancel_reason": c["cancel_reason"]})

    # Clinician-confirmed CTCAE toxicities (chemotherapy record)
    toxicities: dict[str, dict[str, Any]] = {}
    for sym in rows(connection, "SELECT * FROM symptoms WHERE patient_id = ? ORDER BY onset_date, id", (patient_id,)):
        add(sym["onset_date"], "toxicity", f"{sym['ctcae_term']} grade {sym['grade']} (CTCAE, clinician-confirmed)",
            (sym["note"] or "") + (f" Suspected: {sym['suspected_drug']}." if sym["suspected_drug"] else "") + f" Status: {sym['status']}.",
            {"type": "symptom", "id": sym["id"]}, "alert" if sym["grade"] >= 3 else "warning" if sym["grade"] >= 2 else None)
        if within(sym["onset_date"]):
            current = toxicities.get(sym["ctcae_term"])
            if not current or sym["grade"] > current["worst_grade"]:
                toxicities[sym["ctcae_term"]] = {"term": sym["ctcae_term"], "worst_grade": sym["grade"], "date": sym["onset_date"], "source": "chemotherapy record",
                                                 "status": sym["status"]}
            else:
                current["status"] = sym["status"]
    for course in radiotherapy:
        for t in course["worst_toxicities"]:
            current = toxicities.get(t["term"])
            if not current or t["grade"] > current["worst_grade"]:
                toxicities[t["term"]] = {"term": t["term"], "worst_grade": t["grade"], "date": t["date"], "source": "radiotherapy weekly review",
                                         "status": "see latest review"}

    # Labs: one item per collection date with abnormal values.
    lab_rows = rows(connection, """SELECT test_name, value, unit, ref_low, ref_high, collection_date FROM lab_investigations
                                   WHERE patient_id = ? AND verification_status = 'verified' ORDER BY collection_date, id""", (patient_id,))
    by_date: dict[str, list[dict[str, Any]]] = {}
    for lab in lab_rows:
        by_date.setdefault(lab["collection_date"], []).append(lab)
    latest_labs: dict[str, dict[str, Any]] = {}
    for day, labs in by_date.items():
        abnormal = []
        for lab in labs:
            flag = "low" if lab["ref_low"] is not None and lab["value"] < lab["ref_low"] else "high" if lab["ref_high"] is not None and lab["value"] > lab["ref_high"] else None
            if flag:
                abnormal.append(f"{lab['test_name']} {_fmt(lab['value'])} {lab['unit']} ({flag})")
            if within(day) and lab["test_name"] in KEY_LABS:
                latest_labs[lab["test_name"]] = {**lab, "flag": flag}
        add(day, "labs", f"Labs ({len(labs)} verified values)", "Abnormal: " + "; ".join(abnormal) if abnormal else "All verified values within reference range",
            {"type": "labs", "date": day}, "warning" if abnormal else None)

    # Patient complaints (portal / WhatsApp) with interpretation and outcome.
    complaints = []
    for r in rows(connection, "SELECT * FROM patient_reports WHERE patient_id = ? ORDER BY created_at, id", (patient_id,)):
        ai = json.loads(r["ai_json"]) if r["ai_json"] else {}
        flags = [f["label"] for f in json.loads(r["red_flags_json"] or "[]")]
        checkin = json.loads(r["checkin_json"]) if r["checkin_json"] else None
        summary = ai.get("summary_en") or (("; ".join(f"{i['label']}" + (f" (suggested G{i['grade_suggestion']})" if i.get("grade_suggestion") else "")
                                                   for i in checkin["items"])) if checkin and checkin.get("items") else r["text"][:300])
        outcome = {"new": "awaiting review", "reviewed": "reviewed", "recorded": "recorded as CTCAE toxicity", "dismissed": "dismissed"}[r["status"]]
        source = "WhatsApp" if (r.get("source") or "portal") == "whatsapp" else "portal"
        day = r["created_at"][:10]
        add(day, "complaint", f"Patient message via {source}" + (" — red flags" if flags else ""),
            summary + (f" Red flags: {', '.join(flags)}." if flags else "") + f" Outcome: {outcome}." + (f" Reply: \"{r['patient_message']}\"" if r["patient_message"] else ""),
            {"type": "report", "id": r["id"]}, "alert" if flags else None)
        if within(day):
            complaints.append({"id": r["id"], "date": day, "source": source, "summary": summary, "red_flags": flags, "status": r["status"],
                               "outcome": outcome, "reply": r["patient_message"]})

    notes = rows(connection, "SELECT * FROM clinical_notes WHERE patient_id = ? ORDER BY note_date, id", (patient_id,))
    for note in notes:
        add(note["note_date"], "note", f"{NOTE_CATEGORIES.get(note['category'], 'Note')} — {note['author']}", note["text"], {"type": "note", "id": note["id"]})
    for rx in rows(connection, "SELECT * FROM prescriptions WHERE patient_id = ? AND status = 'active' ORDER BY rx_date, id", (patient_id,)):
        meds = json.loads(rx["items_json"] or "[]")
        detail = "; ".join(" ".join(x for x in (m["name"], m.get("dose"), m.get("frequency"), f"for {m['duration']}" if m.get("duration") else "") if x) for m in meds)
        add(rx["rx_date"], "prescription", f"Prescription — {rx['prescribed_by']}", detail + (f". Advice: {rx['advice']}" if rx["advice"] else ""),
            {"type": "prescription", "id": rx["id"]})
    documents = rows(connection, "SELECT * FROM patient_documents WHERE patient_id = ? ORDER BY doc_date, id", (patient_id,))
    for doc in documents:
        add(doc["doc_date"], "document", f"{DOC_TYPES.get(doc['doc_type'], 'Report')}: {doc['title']}",
            doc["ai_summary"] or (doc["text_excerpt"] or "")[:400] or (doc["note"] or "No text extracted; open the file."), {"type": "document", "id": doc["id"]})
    for d in rows(connection, "SELECT id, kind, finalised_at, period_from, period_to FROM discharge_summaries WHERE patient_id = ? AND status = 'final'", (patient_id,)):
        add(d["finalised_at"][:10], "discharge", "Discharge summary finalised", f"Period {d['period_from'] or '—'} to {d['period_to'] or '—'}",
            {"type": "discharge", "id": d["id"]})

    from . import oncology  # local import: oncology uses records helpers
    onco = oncology.overview_for(connection, patient_id, period_to)
    for d in onco["diagnoses"]:
        # The diagnosis and the baseline scan are usually outside a summary's period, but the summary reports them and must cite them.
        add(d["diagnosis_date"], "staging", f"Diagnosis: {d['primary_site']}" + (f" — {d['stage_text']}" if d["stage_text"] else ""), d["summary"],
            {"type": "diagnosis", "id": d["id"]}, always=True)
    for a in onco["assessments"]:
        add(a["assessment_date"], "response", f"Tumour assessment ({a['timepoint_label'].lower()})", oncology.assessment_line(a),
            {"type": "tumour_assessment", "id": a["id"]}, "warn" if a["response"] == "PD" else None, always=a["timepoint"] == "baseline")

    rank = {"staging": 0, "response": 1, "note": 0, "document": 1, "labs": 2, "surgery": 3, "surgery_note": 3, "chemo": 4, "rt": 5, "rt_review": 6, "toxicity": 7, "complaint": 8, "discharge": 9}
    items.sort(key=lambda i: (i["date"], rank.get(i["kind"], 99)))  # stable: surgery events keep their recorded order
    for index, item in enumerate(items, start=1):
        item["id"] = f"S{index}"
    open_reports = row_or_none(connection, "SELECT COUNT(*) AS n, SUM(CASE WHEN red_flags_json NOT IN ('[]', '') THEN 1 ELSE 0 END) AS urgent "
                                           "FROM patient_reports WHERE patient_id = ? AND status = 'new'", (patient_id,))
    reminder = row_or_none(connection, """SELECT scheduled_for, related_type, body FROM whatsapp_messages WHERE patient_id = ? AND kind = 'reminder'
                                         AND status IN ('scheduled', 'sent', 'delivered', 'read') AND scheduled_for >= ? ORDER BY scheduled_for LIMIT 1""",
                           (patient_id, utc_now()))
    age = None
    if patient["date_of_birth"]:
        born, today = date.fromisoformat(patient["date_of_birth"]), date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return {
        "patient": {k: patient[k] for k in ("id", "first_name", "last_name", "medical_record_number", "date_of_birth", "sex", "diagnosis", "phone")} | {"age": age},
        "period": {"from": period_from, "to": period_to},
        "chemotherapy": chemo, "radiotherapy": radiotherapy, "surgery": surgeries,
        "oncology": {k: onco[k] for k in ("diagnoses", "assessments", "baseline", "latest", "best")},
        "prescriptions": [{"id": r["id"], "rx_date": r["rx_date"], "prescribed_by": r["prescribed_by"], "items": json.loads(r["items_json"] or "[]")}
                          for r in rows(connection, "SELECT * FROM prescriptions WHERE patient_id = ? AND status = 'active' ORDER BY rx_date DESC, id DESC LIMIT 3", (patient_id,))],
        "appointments": rows(connection, "SELECT id, appt_date, appt_time, purpose, status FROM appointments WHERE patient_id = ? AND appt_date >= ? "
                                         "AND status IN ('booked', 'reschedule_requested') ORDER BY appt_date, appt_time LIMIT 5", (patient_id, date.today().isoformat())),
        "toxicities": sorted(toxicities.values(), key=lambda t: (-t["worst_grade"], t["term"])),
        "labs": [latest_labs[name] for name in KEY_LABS if name in latest_labs],
        "complaints": complaints,
        "open_messages": {"count": open_reports["n"] or 0, "urgent": open_reports["urgent"] or 0},
        "next_reminder": reminder,
        "items": items,
    }


# ---------------------------------------------------------------------------------------------- narrative
def sources_text(items: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{i['id']}] {i['date']} · {i['title']}: {i['detail']}" for i in items)


def _sid(items: list[dict[str, Any]], **match: Any) -> list[str]:
    out = []
    for item in items:
        if all(item["ref"].get(k) == v for k, v in match.items()):
            out.append(item["id"])
    return out


def _cite(ids: list[str], limit: int = 4) -> str:
    ids = list(dict.fromkeys(ids))
    if not ids:
        return ""
    shown = ids if len(ids) <= limit else ids[:2] + ids[-2:]
    return " " + "".join(f"[{i}]" for i in shown)


def _first_sentence(text: str, limit: int) -> str:
    """First sentence of a (possibly multi-sentence AI) summary, cut at a word: a quoted second sentence would be split off
    by check_citations as an uncited sentence, and a hard cut left half a word."""
    first = re.split(r"(?<=[.!?])\s+", (text or "").strip(), maxsplit=1)[0].rstrip(".")
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "…"


def rules_narrative(facts: dict[str, Any]) -> str:
    """Deterministic narrative from the facts; every sentence cites its sources."""
    p, items = facts["patient"], facts["items"]
    who = f"{p['first_name']} {p['last_name']}"
    if p.get("age") is not None:
        who += f", a {p['age']}-year-old" + (f" {p['sex']}" if p.get("sex") in ("female", "male") else "") + ","
    verb = "was treated for" if facts["period"]["to"] else "is under treatment for"
    onco = facts.get("oncology") or {}
    staged = onco.get("diagnoses") or []
    if staged:
        sentences = [f"{who} {verb} {'; '.join(d['summary'] for d in staged)}." + _cite(_sid(items, type="diagnosis"))]
    else:
        sentences = [f"{who} {verb} {p['diagnosis'] or 'a diagnosis not recorded'}. [S0]"]
    for plan in facts["chemotherapy"]:
        ids = _sid(items, type="cycle", plan_id=plan["plan_id"])
        if plan["cycles_given"]:
            text = (f"Chemotherapy with {plan['regimen']} ({', '.join(plan['drugs'])}): {plan['cycles_given']} of {plan['cycles_planned']} cycles given "
                    f"from {plan['first_date']} to {plan['last_date']}")
            text += f", with {', '.join(plan['changes'])}" if plan["changes"] else ""
            sentences.append(text + "." + _cite(ids))
        if plan["next_cycle"] and not facts["period"]["to"]:
            sentences.append(f"The next {plan['regimen']} cycle (C{plan['next_cycle']['number']}) is planned for {plan['next_cycle']['date']}." + _cite(ids[-1:]))
    for sxc in facts.get("surgery", []):
        ids = _sid(items, type="surgery", id=sxc["case_id"])
        if sxc["status"] in ("completed", "discharged") and sxc["surgery_date"]:
            text = f"Surgery on {sxc['surgery_date']}: {sxc['procedure']}" + (f" ({sxc['approach'].lower()})" if sxc.get("approach") else "")
            if sxc.get("pathology"):
                pth = sxc["pathology"]
                text += f"; histopathology {pth.get('histology')}" + (f", {sxc['stage']}" if sxc.get("stage") else "") + (f", margins {pth['margins']}" if pth.get("margins") else "")
            if sxc["complications"]:
                text += f"; complications: {', '.join(sxc['complications'][:3])}"
            elif sxc["status"] == "discharged":
                text += "; no complications recorded"
            if sxc.get("length_of_stay") is not None:
                text += f"; length of stay {sxc['length_of_stay']} day(s)"
            sentences.append(text + "." + _cite(ids))
        elif sxc["status"] == "cancelled":
            sentences.append(f"Planned {sxc['procedure_planned']} was cancelled ({sxc.get('cancel_reason') or 'reason not recorded'})." + _cite(ids))
        elif sxc["surgery_date"]:
            sentences.append(f"{sxc['procedure_planned']} is scheduled for {sxc['surgery_date']}." + _cite(ids))
    for course in facts["radiotherapy"]:
        ids = _sid(items, type="rt_course", id=course["course_id"])
        phases = "; ".join(f"{p['name']} {rt.gy(p['dose_per_fraction_cgy'] * p['fractions_planned'])}/{p['fractions_planned']}# {p['technique'] or ''}".strip()
                           for p in course["phases"])
        if course["fractions_delivered"]:
            text = (f"Radiotherapy to the {course['site'].lower()} ({phases}): {course['fractions_delivered']} of {course['fractions_planned']} fractions, "
                    f"{rt.gy(course['dose_delivered_cgy'])} of {rt.gy(course['dose_planned_cgy'])}, from {course['first_date']} to {course['last_date']}")
            if course["concurrent_chemo"]:
                text += f", concurrent with {course['concurrent_chemo']}"
            sentences.append(text + "." + _cite(ids))
        if course["gaps"]:
            sentences.append(f"Treatment was not given on {len(course['gaps'])} scheduled day(s) ("
                             + "; ".join(f"{g['date']}: {g['category'] or 'no reason recorded'}" for g in course["gaps"][:4]) + ")."
                             + _cite([i["id"] for i in items if i["ref"].get("id") == course["course_id"] and i["title"] == "Radiotherapy not given"]))
        if course["completion"]:
            sentences.append(f"The course was closed as {course['completion']['outcome_label'].lower()} on {course['completion']['end_date']}." + _cite(ids[-1:]))
        elif course["next_fraction_date"] and not facts["period"]["to"]:
            sentences.append(f"{course['fractions_planned'] - course['fractions_delivered']} fraction(s) remain; next on {course['next_fraction_date']}, "
                             f"projected completion {course['projected_end_date']}." + _cite(ids[-1:]))
        reviews = _sid(items, type="rt_review", course_id=course["course_id"])
        if course["worst_toxicities"]:
            sentences.append("Worst acute radiotherapy reactions: " + ", ".join(f"{t['term']} G{t['grade']} (week {t['week']})" for t in course["worst_toxicities"][:5]) + "."
                             + _cite(reviews))
        wc = course["weight_change"]
        if wc and wc["change_pct"]:
            sentences.append(f"Weight {wc['baseline']:g} → {wc['current']:g} kg ({wc['change_pct']:+g}%)." + _cite(reviews[-1:]))
    chemo_tox = [t for t in facts["toxicities"] if t["source"] == "chemotherapy record"]
    if chemo_tox:
        sentences.append("Clinician-confirmed CTCAE toxicities: " + ", ".join(f"{t['term']} G{t['worst_grade']} ({t['date']})" for t in chemo_tox[:6]) + "."
                         + _cite([i["id"] for i in items if i["kind"] == "toxicity"]))
    if facts["complaints"]:
        urgent = [c for c in facts["complaints"] if c["red_flags"]]
        sentences.append(f"The patient sent {len(facts['complaints'])} message(s) between visits"
                         + (f", {len(urgent)} with red flags" if urgent else "") + "; latest on " + facts["complaints"][-1]["date"] + ": "
                         + _first_sentence(facts["complaints"][-1]["summary"], 220) + f" ({facts['complaints'][-1]['outcome']})."
                         + _cite([i["id"] for i in items if i["kind"] == "complaint"]))
    abnormal = [l for l in facts["labs"] if l["flag"]]
    if abnormal:
        sentences.append("Latest verified labs outside the reference range: " + ", ".join(f"{l['test_name']} {l['value']:g} {l['unit']} ({l['collection_date']})" for l in abnormal) + "."
                         + _cite([i["id"] for i in items if i["kind"] == "labs"][-2:]))
    measured = [a for a in onco.get("assessments") or [] if a.get("sum_mm") is not None]
    if len(measured) >= 2:
        first, last = measured[0], measured[-1]
        change = f"{last['change_from_baseline_pct']:+.0f}%" if last.get("change_from_baseline_pct") is not None else "no change"
        text = (f"Tumour size (sum of target lesions) went from {first['sum_mm']:g} mm on {first['assessment_date']} to {last['sum_mm']:g} mm on "
                f"{last['assessment_date']} ({change}), response {last['response'] or 'not assessed'}")
        if onco.get("best") and onco["best"].get("response") and onco["best"]["response"] != last.get("response"):
            text += f"; best response {onco['best']['response']}"
        sentences.append(text + "." + _cite([i["id"] for i in items if i["kind"] == "response"][-3:]))
    elif measured:
        sentences.append(f"Baseline tumour measurement on {measured[0]['assessment_date']}: sum of target lesions {measured[0]['sum_mm']:g} mm."
                         + _cite([i["id"] for i in items if i["kind"] == "response"][:1]))
    docs = [i for i in items if i["kind"] == "document"]
    if docs:
        sentences.append("Reports on file: " + "; ".join(f"{d['title']} ({d['date']})" for d in docs[-3:]) + "." + _cite([d["id"] for d in docs[-3:]]))
    return " ".join(sentences)


SENTENCE = re.compile(r"(?<=[.!?\]])\s+(?=[A-Z0-9])")


def check_citations(text: str, valid: set[str]) -> dict[str, Any]:
    """Split into sentences and mark the ones without a valid [S#] citation, so the clinician can check them."""
    sentences = []
    for sentence in SENTENCE.split(text.strip()):
        cited = re.findall(r"\[(S\d+)\]", sentence)
        good = [c for c in cited if c in valid]
        sentences.append({"text": sentence, "sources": good, "unsupported": not good, "unknown": [c for c in cited if c not in valid]})
    return {"sentences": sentences, "unsupported": sum(1 for s in sentences if s["unsupported"])}


PROGRESS_BRIEF = ("Write a brief 'how the treatment is going' summary for the treating oncologist's running case sheet, in this order: "
                  "the cancer and its stage; the treatment given so far and where the patient is now (cycle / fraction / post-op day); "
                  "tumour response (size trend and RECIST response, tumour markers); how the treatment is tolerated (worst toxicity grades, "
                  "delays, dose changes, missed fractions, complications); open issues (unreviewed patient messages, red flags, abnormal labs); "
                  "and what is scheduled next. Skip any part the sources do not cover. At most 150 words.\n")


def ai_narrative(facts: dict[str, Any], purpose: str = "running summary") -> str:
    status = extraction.ai_status()
    brief = PROGRESS_BRIEF if purpose == "running summary" else ""
    prompt = (brief + f"Write the {purpose} paragraph(s) for an oncology patient record, for the treating doctor.\n"
              "Use ONLY the numbered sources below. After every sentence put the source ids it relies on, like [S3][S7]. "
              "Do not add facts, interpretation of causes, prognosis, or treatment recommendations. Keep doses, dates and grades exactly as "
              "written. Neutral clinical English, past tense for events, at most 180 words, no headings, no bullet points.\n\n"
              f"PATIENT: {facts['patient']['first_name']} {facts['patient']['last_name']}, {facts['patient'].get('age') or '?'} y, "
              f"{facts['patient'].get('sex') or 'sex not recorded'}; diagnosis: {facts['patient'].get('diagnosis') or 'not recorded'}\n\n"
              f"SOURCES\n[S0] Patient record: name, age, sex and diagnosis as above\n{sources_text(facts['items'])[:30000]}")
    reply = extraction._anthropic_post(extraction.os.environ["ANTHROPIC_API_KEY"],
                                       {"model": status["model"], "max_tokens": 900, "messages": [{"role": "user", "content": prompt}]}, 90)
    return "".join(part.get("text", "") for part in reply.get("content", [])).strip()


def narrate(facts: dict[str, Any], purpose: str = "running summary") -> dict[str, Any]:
    method, model, error = "rules", None, None
    text = rules_narrative(facts)
    if extraction.ai_status()["enabled"] and facts["items"]:
        try:
            ai_text = ai_narrative(facts, purpose)
            if ai_text:
                text, method, model = ai_text, "claude", extraction.ai_status()["model"]
        except Exception as error_:  # keep the rule-based text
            error = f"AI narrative failed ({str(error_)[:140]}); showing the rule-based summary."
    check = check_citations(text, {i["id"] for i in facts["items"]} | {"S0"})
    return {"text": text, "method": method, "model": model, "error": error, **check}


def running_summary(db: str, patient_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        facts = collect(connection, patient_id)
        cached = row_or_none(connection, "SELECT * FROM patient_summaries WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (patient_id,))
    narrative = None
    if cached:
        sources = json.loads(cached["sources_json"])
        narrative = {"text": cached["text"], "method": cached["method"], "model": cached["model"], "generated_at": cached["generated_at"],
                     "generated_by": cached["generated_by"], "new_items": max(len(facts["items"]) - cached["source_count"], 0),
                     "sources": sources, **check_citations(cached["text"], {s["id"] for s in sources} | {"S0"})}
    return {**facts, "narrative": narrative, "ai_enabled": extraction.ai_status()["enabled"]}


def generate_narrative(db: str, patient_id: int, actor: str) -> dict[str, Any]:
    with transaction(db) as connection:
        facts = collect(connection, patient_id)
    result = narrate(facts)
    sources = [{k: i[k] for k in ("id", "date", "kind", "title", "detail", "ref")} for i in facts["items"]]
    with transaction(db) as connection:
        connection.execute("""INSERT INTO patient_summaries (patient_id, text, sources_json, method, model, source_count, generated_by, generated_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                           (patient_id, result["text"], json.dumps(sources), result["method"], result["model"], len(sources), actor, utc_now()))
        connection.execute("DELETE FROM patient_summaries WHERE patient_id = ? AND id NOT IN (SELECT id FROM patient_summaries WHERE patient_id = ? ORDER BY id DESC LIMIT 5)",
                           (patient_id, patient_id))
    out = running_summary(db, patient_id)
    if result["error"]:
        out["narrative"]["error"] = result["error"]
    return out

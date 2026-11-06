"""Surgical oncology: one case sheet per operation.

Plan & pre-op   -> procedure planned, indication, site/laterality, intent, urgency, surgeon, dates, ASA grade and the pre-op
                   checklist (consent, anaesthesia fitness, imaging reviewed, MDT/tumour board, blood arranged, site marked,
                   fasting instructions given).
Operation note  -> procedure performed, approach, anaesthesia, team, findings, frozen section, specimens, blood loss,
                   transfusion, drains, duration, intra-operative events; signed.
Post-op course  -> dated progress notes by post-operative day, drain output, pain score, complications graded with the
                   Clavien–Dindo classification.
Pathology       -> final histopathology (histology, grade, size, pT/pN, margins, nodes, LVI/PNI, biomarkers).
Discharge       -> condition, drains, wound care, suture removal, follow-up; signed.

The system organises and checks completeness; it does not recommend a surgical decision.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from .database import row_or_none, rows, transaction, utc_now
from .errors import ApiError

INTENTS = {"curative": "Curative", "palliative": "Palliative", "diagnostic": "Diagnostic / staging", "reconstructive": "Reconstructive",
           "prophylactic": "Risk-reducing", "other": "Other"}
URGENCY = {"elective": "Elective", "urgent": "Urgent", "emergency": "Emergency"}
APPROACHES = ["Open", "Laparoscopic", "Robotic", "Endoscopic", "Thoracoscopic (VATS)", "Converted to open", "Other"]
ANAESTHESIA = ["General", "General + regional block", "Spinal", "Epidural", "Regional block", "Local / sedation"]
STATUS_LABEL = {"planned": "Planned", "scheduled": "Scheduled", "completed": "Operated", "discharged": "Discharged", "cancelled": "Cancelled"}
PREOP_CHECKS = {
    "consent": "Informed consent signed",
    "anaesthesia_fitness": "Pre-anaesthetic check / fitness",
    "imaging_reviewed": "Imaging and staging reviewed",
    "mdt": "Discussed in tumour board (MDT)",
    "blood_arranged": "Blood grouped / cross-matched",
    "site_marked": "Site and side marked",
    "fasting_instructions": "Fasting and medicine instructions given",
}
CLAVIEN_DINDO = {
    "I": "Any deviation from the normal post-operative course without pharmacological, surgical, endoscopic or radiological intervention "
         "(allowed: antiemetics, antipyretics, analgesics, diuretics, electrolytes, physiotherapy; wound infections opened at the bedside)",
    "II": "Requiring pharmacological treatment with drugs other than those allowed for grade I, including blood transfusion and total parenteral nutrition",
    "IIIa": "Requiring surgical, endoscopic or radiological intervention, not under general anaesthesia",
    "IIIb": "Requiring surgical, endoscopic or radiological intervention under general anaesthesia",
    "IVa": "Life-threatening complication requiring intermediate/intensive care: single-organ dysfunction",
    "IVb": "Life-threatening complication requiring intermediate/intensive care: multi-organ dysfunction",
    "V": "Death of the patient",
}
CD_ORDER = list(CLAVIEN_DINDO)
EVENT_KINDS = {"progress": "Progress note", "complication": "Complication", "drain": "Drain", "wound": "Wound", "return_to_theatre": "Return to theatre",
               "readmission": "Readmission", "other": "Other"}
OPERATION_FIELDS = ("operation_date", "procedure_performed", "approach", "anaesthesia", "surgeon", "assistants", "anaesthetist", "incision", "findings",
                    "procedure_details", "frozen_section", "specimens", "ebl_ml", "transfusion_units", "drains", "duration_min", "intraop_events")
PATHOLOGY_FIELDS = ("report_date", "histology", "grade", "size_mm", "pt", "pn", "margins", "closest_margin_mm", "nodes_examined", "nodes_positive",
                    "lvi", "pni", "biomarkers", "comments")
DISCHARGE_FIELDS = ("condition", "drains_at_discharge", "wound_care", "suture_removal_date", "follow_up_date", "instructions")
NUMERIC = {"ebl_ml", "transfusion_units", "duration_min", "size_mm", "closest_margin_mm", "nodes_examined", "nodes_positive"}


def _text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _date(value: Any, label: str, *, required: bool = False, not_future: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ApiError(422, f"{label} is required", {"fields": [label.lower().replace(" ", "_")]})
        return None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ApiError(422, f"{label} must use YYYY-MM-DD format") from None
    if not_future and parsed > date.today():
        raise ApiError(422, f"{label} cannot be in the future")
    return parsed.isoformat()


def _section(payload: Any, fields: tuple[str, ...], dates: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(422, "Section must be an object")
    out: dict[str, Any] = {}
    for key in fields:
        value = payload.get(key)
        if value in (None, ""):
            continue
        if key in NUMERIC:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ApiError(422, f"{key.replace('_', ' ')} must be a number") from None
            if number < 0 or number > 100000:
                raise ApiError(422, f"{key.replace('_', ' ')} is out of range")
            out[key] = int(number) if number.is_integer() else number
        elif key in dates:
            out[key] = _date(value, key.replace("_", " ").capitalize())
        else:
            out[key] = _text(value, 3000)
    return out


def _case(connection: sqlite3.Connection, case_id: int) -> dict[str, Any]:
    case = row_or_none(connection, "SELECT * FROM surgical_cases WHERE id = ?", (case_id,))
    if not case:
        raise ApiError(404, "Surgical case not found")
    return case


def _event(connection: sqlite3.Connection, case_id: int, kind: str, text: str, author: str, day: str | None = None, **extra: Any) -> None:
    connection.execute("""INSERT INTO surgical_events (case_id, event_date, kind, text, clavien_dindo, complication, drain_output_ml, pain_score, author, created_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (case_id, day or date.today().isoformat(), kind, text[:4000], extra.get("clavien_dindo"), extra.get("complication"),
                        extra.get("drain_output_ml"), extra.get("pain_score"), author, utc_now()))


# ---------------------------------------------------------------------------------------------- create / update
def _plan_fields(payload: dict[str, Any], partial: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if not partial or "procedure_planned" in payload:
        fields["procedure_planned"] = _text(payload.get("procedure_planned"), 200)
        if not fields["procedure_planned"]:
            raise ApiError(422, "Missing required fields", {"fields": ["procedure_planned"]})
    for key, limit in (("indication", 300), ("site", 160), ("surgeon", 80), ("notes", 3000)):
        if key in payload:
            fields[key] = _text(payload[key], limit)
    if "laterality" in payload:
        if payload["laterality"] not in (None, "", "right", "left", "bilateral", "midline"):
            raise ApiError(422, "Unknown laterality")
        fields["laterality"] = payload["laterality"] or None
    if "intent" in payload:
        if payload["intent"] not in (None, "", *INTENTS):
            raise ApiError(422, "Unknown intent")
        fields["intent"] = payload["intent"] or None
    if "urgency" in payload or not partial:
        urgency = payload.get("urgency") or "elective"
        if urgency not in URGENCY:
            raise ApiError(422, "Unknown urgency")
        fields["urgency"] = urgency
    for key in ("surgery_date", "admission_date"):
        if key in payload:
            fields[key] = _date(payload[key], key.replace("_", " ").capitalize())
    if fields.get("admission_date") and fields.get("surgery_date") and fields["admission_date"] > fields["surgery_date"]:
        raise ApiError(422, "Admission date is after the surgery date")
    if "preop" in payload:
        preop = payload["preop"] if isinstance(payload["preop"], dict) else {}
        clean: dict[str, Any] = {"checks": {k: bool((preop.get("checks") or {}).get(k)) for k in PREOP_CHECKS}}
        asa = preop.get("asa")
        if asa not in (None, "", "I", "II", "III", "IV", "V", "VI"):
            raise ApiError(422, "ASA grade must be I–VI")
        if asa:
            clean["asa"] = asa
        for key in ("comorbidities", "investigations", "fasting_from", "special_instructions"):
            if _text(preop.get(key)):
                clean[key] = _text(preop.get(key), 1000)
        fields["preop_json"] = json.dumps(clean)
    return fields


def create(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    fields = _plan_fields(payload, partial=False)
    now = utc_now()
    with transaction(db) as connection:
        if not row_or_none(connection, "SELECT id FROM patients WHERE id = ?", (patient_id,)):
            raise ApiError(404, "Patient not found")
        fields["status"] = "scheduled" if fields.get("surgery_date") else "planned"
        columns = ["patient_id", "created_by", "created_at", "updated_at", *fields]
        case_id = connection.execute(f"INSERT INTO surgical_cases ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                                     (patient_id, actor, now, now, *fields.values())).lastrowid
        _event(connection, case_id, "plan", f"Surgery planned: {fields['procedure_planned']}"
               + (f", scheduled {fields['surgery_date']}" if fields.get("surgery_date") else ""), actor)
    return workspace(db, case_id)


def update(db: str, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    fields = _plan_fields(payload, partial=True)
    with transaction(db) as connection:
        case = _case(connection, case_id)
        if case["status"] in ("discharged",) and set(fields) - {"notes"}:
            raise ApiError(409, "The case is closed (discharged)")
        status = payload.get("status")
        if status == "cancelled":
            reason = _text(payload.get("cancel_reason"), 500)
            if not reason:
                raise ApiError(422, "Give the reason for cancelling", {"fields": ["cancel_reason"]})
            if case["status"] in ("completed", "discharged"):
                raise ApiError(422, "An operated case cannot be cancelled")
            fields.update({"status": "cancelled", "cancel_reason": reason})
            _event(connection, case_id, "status", f"Surgery cancelled: {reason}", actor)
        elif status == "planned" and case["status"] == "cancelled":
            fields.update({"status": "scheduled" if (fields.get("surgery_date") or case["surgery_date"]) else "planned", "cancel_reason": None})
            _event(connection, case_id, "status", "Cancelled surgery re-planned", actor)
        elif status not in (None, ""):
            raise ApiError(422, "Status changes through the operation note and discharge")
        if case["status"] == "planned" and fields.get("surgery_date"):
            fields["status"] = "scheduled"
        if "surgery_date" in fields and case["surgery_date"] and fields["surgery_date"] != case["surgery_date"] and case["status"] in ("planned", "scheduled"):
            _event(connection, case_id, "status", f"Surgery date changed {case['surgery_date']} → {fields['surgery_date'] or 'not set'}", actor)
        if not fields:
            raise ApiError(422, "No supported fields supplied")
        connection.execute(f"UPDATE surgical_cases SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = ? WHERE id = ?",
                           (*fields.values(), utc_now(), case_id))
    return workspace(db, case_id)


def save_operation(db: str, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    op = _section(payload, OPERATION_FIELDS, dates=("operation_date",))
    missing = [k for k in ("operation_date", "procedure_performed", "surgeon") if not op.get(k)]
    if missing:
        raise ApiError(422, "Operation date, procedure performed and surgeon are required", {"fields": missing})
    if op["operation_date"] > date.today().isoformat():
        raise ApiError(422, "Operation date cannot be in the future")
    if payload.get("signed") is not True:
        raise ApiError(422, "Confirm and sign the operation note", {"fields": ["signed"]})
    with transaction(db) as connection:
        case = _case(connection, case_id)
        if case["status"] == "cancelled":
            raise ApiError(422, "The surgery was cancelled; re-plan it first")
        previous = json.loads(case["operation_json"] or "null")
        op.update({"signed_by": actor, "signed_at": utc_now()})
        status = case["status"] if case["status"] == "discharged" else "completed"
        connection.execute("UPDATE surgical_cases SET operation_json = ?, status = ?, surgery_date = ?, updated_at = ? WHERE id = ?",
                           (json.dumps(op), status, op["operation_date"], utc_now(), case_id))
        _event(connection, case_id, "operation", ("Operation note amended: " if previous else "Operation performed: ") + op["procedure_performed"]
               + (f" ({op['approach']})" if op.get("approach") else ""), actor, op["operation_date"])
    return workspace(db, case_id)


def add_event(db: str, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    kind = payload.get("kind") or "progress"
    if kind not in EVENT_KINDS:
        raise ApiError(422, "Unknown note type")
    text = _text(payload.get("text"), 4000)
    if not text:
        raise ApiError(422, "Write the note", {"fields": ["text"]})
    day = _date(payload.get("event_date") or date.today().isoformat(), "Date", required=True, not_future=True)
    grade = payload.get("clavien_dindo") or None
    if grade not in (None, *CLAVIEN_DINDO):
        raise ApiError(422, "Clavien–Dindo grade must be I, II, IIIa, IIIb, IVa, IVb or V")
    if kind in ("complication", "return_to_theatre", "readmission") and not grade:
        raise ApiError(422, "Grade the complication with the Clavien–Dindo classification.")
    complication = _text(payload.get("complication"), 200)
    if kind == "complication" and not complication:
        raise ApiError(422, "Name the complication (e.g. seroma, wound infection)", {"fields": ["complication"]})
    drain = payload.get("drain_output_ml")
    pain = payload.get("pain_score")
    try:
        drain = float(drain) if drain not in (None, "") else None
        pain = int(pain) if pain not in (None, "") else None
    except (TypeError, ValueError):
        raise ApiError(422, "Drain output and pain score must be numbers") from None
    if pain is not None and not 0 <= pain <= 10:
        raise ApiError(422, "Pain score is 0–10")
    with transaction(db) as connection:
        case = _case(connection, case_id)
        if case["status"] in ("planned", "scheduled", "cancelled") and kind != "other":
            raise ApiError(422, "Record the operation note before post-operative notes")
        _event(connection, case_id, kind, text, _text(payload.get("author"), 80) or actor, day, clavien_dindo=grade, complication=complication,
               drain_output_ml=drain, pain_score=pain)
    return workspace(db, case_id)


def delete_event(db: str, event_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        event = row_or_none(connection, "SELECT * FROM surgical_events WHERE id = ?", (event_id,))
        if not event:
            raise ApiError(404, "Note not found")
        if event["kind"] in ("plan", "status", "operation", "pathology", "discharge"):
            raise ApiError(422, "System entries are part of the audit trail")
        connection.execute("DELETE FROM surgical_events WHERE id = ?", (event_id,))
    return workspace(db, event["case_id"])


def save_pathology(db: str, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    path = _section(payload, PATHOLOGY_FIELDS, dates=("report_date",))
    if not path.get("histology"):
        raise ApiError(422, "Enter the histology", {"fields": ["histology"]})
    if path.get("nodes_positive") is not None and path.get("nodes_examined") is not None and path["nodes_positive"] > path["nodes_examined"]:
        raise ApiError(422, "More nodes positive than examined")
    if path.get("margins") not in (None, "negative", "close", "positive"):
        raise ApiError(422, "Margins must be negative, close or positive")
    with transaction(db) as connection:
        case = _case(connection, case_id)
        if not case["operation_json"]:
            raise ApiError(422, "Record the operation note first")
        path.update({"entered_by": actor, "entered_at": utc_now()})
        connection.execute("UPDATE surgical_cases SET pathology_json = ?, updated_at = ? WHERE id = ?", (json.dumps(path), utc_now(), case_id))
        stage = " ".join(x for x in (path.get("pt"), path.get("pn")) if x)
        _event(connection, case_id, "pathology", f"Histopathology: {path['histology']}" + (f", {stage}" if stage else "")
               + (f", margins {path['margins']}" if path.get("margins") else ""), actor, path.get("report_date"))
    return workspace(db, case_id)


def discharge(db: str, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    out = _section(payload, DISCHARGE_FIELDS, dates=("suture_removal_date", "follow_up_date"))
    day = _date(payload.get("discharge_date") or date.today().isoformat(), "Discharge date", required=True, not_future=True)
    if not out.get("condition"):
        raise ApiError(422, "Describe the condition at discharge", {"fields": ["condition"]})
    with transaction(db) as connection:
        case = _case(connection, case_id)
        if not case["operation_json"]:
            raise ApiError(422, "Record the operation note first")
        op_day = json.loads(case["operation_json"])["operation_date"]
        if day < op_day:
            raise ApiError(422, "Discharge date is before the operation")
        out.update({"signed_by": actor, "signed_at": utc_now()})
        connection.execute("UPDATE surgical_cases SET discharge_json = ?, discharge_date = ?, status = 'discharged', updated_at = ? WHERE id = ?",
                           (json.dumps(out), day, utc_now(), case_id))
        _event(connection, case_id, "discharge", f"Discharged on post-operative day {(date.fromisoformat(day) - date.fromisoformat(op_day)).days}: {out['condition']}",
               actor, day)
    return workspace(db, case_id)


def delete(db: str, case_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        if connection.execute("DELETE FROM surgical_cases WHERE id = ?", (case_id,)).rowcount == 0:
            raise ApiError(404, "Surgical case not found")
    return {"id": case_id, "deleted": True}


# ---------------------------------------------------------------------------------------------- read model
def worst_grade(grades: list[str]) -> str | None:
    graded = [g for g in grades if g in CD_ORDER]
    return max(graded, key=CD_ORDER.index) if graded else None


def build(connection: sqlite3.Connection, case_id: int, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    case = _case(connection, case_id)
    for key in ("preop", "operation", "pathology", "discharge"):
        case[key] = json.loads(case.pop(f"{key}_json") or "null")
    case["preop"] = case["preop"] or {"checks": {k: False for k in PREOP_CHECKS}}
    case["status_label"] = STATUS_LABEL[case["status"]]
    patient = row_or_none(connection, "SELECT id, first_name, last_name, medical_record_number, date_of_birth, sex, diagnosis, phone, wa_opt_in FROM patients WHERE id = ?",
                          (case["patient_id"],))
    events = rows(connection, "SELECT * FROM surgical_events WHERE case_id = ? ORDER BY event_date, id", (case_id,))
    op_day = case["operation"]["operation_date"] if case["operation"] else None
    for e in events:
        e["kind_label"] = EVENT_KINDS.get(e["kind"], e["kind"].capitalize())
        e["pod"] = (date.fromisoformat(e["event_date"]) - date.fromisoformat(op_day)).days if op_day and e["event_date"] >= op_day else None
    complications = [e for e in events if e["clavien_dindo"]]
    worst = worst_grade([e["clavien_dindo"] for e in complications])
    checks = case["preop"].get("checks") or {}
    pending = [label for key, label in PREOP_CHECKS.items() if not checks.get(key)]
    los = None
    if case["admission_date"] and case["discharge_date"]:
        los = (date.fromisoformat(case["discharge_date"]) - date.fromisoformat(case["admission_date"])).days
    pod = (today - date.fromisoformat(op_day)).days if op_day and case["status"] == "completed" else None
    attention = []
    if case["status"] in ("planned", "scheduled") and pending:
        days_to = (date.fromisoformat(case["surgery_date"]) - today).days if case["surgery_date"] else None
        attention.append({"severity": "alert" if days_to is not None and days_to <= 2 else "warning",
                          "text": f"Pre-op checklist: {len(pending)} item(s) open" + (("; surgery today" if days_to == 0 else f"; surgery in {days_to} day{'s' if days_to != 1 else ''}") if days_to is not None and days_to >= 0 else "")
                          + " — " + ", ".join(pending[:4])})
    path = case["pathology"] or {}
    if path.get("margins") == "positive":
        attention.append({"severity": "alert", "text": "Positive resection margin on final histopathology."})
    elif path.get("margins") == "close":
        attention.append({"severity": "warning", "text": f"Close margin ({path.get('closest_margin_mm', '?')} mm)."})
    if path.get("nodes_positive"):
        attention.append({"severity": "warning", "text": f"{path['nodes_positive']} of {path.get('nodes_examined', '?')} lymph nodes positive."})
    if worst and CD_ORDER.index(worst) >= CD_ORDER.index("IIIa"):
        attention.append({"severity": "alert", "text": f"Major complication (Clavien–Dindo {worst})."})
    if case["status"] == "completed" and not path:
        attention.append({"severity": "info", "text": "Final histopathology awaited."})
    if case["status"] == "completed" and op_day and pod is not None and pod >= 0 and not any(e["event_date"] == today.isoformat() for e in events if e["kind"] == "progress"):
        attention.append({"severity": "info", "text": f"Post-operative day {pod}: no progress note today."})
    return {
        "case": case, "patient": patient, "events": events,
        "summary": {"pod": pod, "length_of_stay": los, "worst_clavien_dindo": worst, "complications": len(complications), "preop_pending": pending,
                    "preop_done": len(PREOP_CHECKS) - len(pending), "preop_total": len(PREOP_CHECKS), "attention": attention,
                    "stage": " ".join(x for x in (path.get("pt"), path.get("pn")) if x) or None},
        "options": {"intents": INTENTS, "urgency": URGENCY, "approaches": APPROACHES, "anaesthesia": ANAESTHESIA, "preop_checks": PREOP_CHECKS,
                    "clavien_dindo": CLAVIEN_DINDO, "event_kinds": EVENT_KINDS},
    }


def workspace(db: str, case_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        return build(connection, case_id)


def list_for_patient(connection: sqlite3.Connection, patient_id: int) -> list[dict[str, Any]]:
    out = []
    for row in rows(connection, "SELECT id FROM surgical_cases WHERE patient_id = ? ORDER BY COALESCE(surgery_date, created_at) DESC, id DESC", (patient_id,)):
        w = build(connection, row["id"])
        c, s = w["case"], w["summary"]
        out.append({"id": c["id"], "procedure": (c["operation"] or {}).get("procedure_performed") or c["procedure_planned"], "status": c["status"],
                    "status_label": c["status_label"], "surgery_date": c["surgery_date"], "intent": c["intent"], "surgeon": c["surgeon"],
                    "site": c["site"], "laterality": c["laterality"], "stage": s["stage"], "worst_clavien_dindo": s["worst_clavien_dindo"],
                    "preop_done": s["preop_done"], "preop_total": s["preop_total"], "attention": s["attention"], "created_at": c["created_at"]})
    return out


def options() -> dict[str, Any]:
    return {"intents": INTENTS, "urgency": URGENCY, "approaches": APPROACHES, "anaesthesia": ANAESTHESIA, "preop_checks": PREOP_CHECKS,
            "clavien_dindo": CLAVIEN_DINDO, "event_kinds": EVENT_KINDS}

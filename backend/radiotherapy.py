"""Radiation oncology workflow, modelled on the paper radiotherapy case sheet.

Cover sheet  -> course (diagnosis, stage, histopathology, intent, machine, consultant, set-up data)
               and phases (dose/fraction x fractions, technique, energy), signed by physicist + oncologist.
Plan sheet   -> IMRT/VMAT dose constraints (structure, constraint, achieved) and isocentre shifts per phase.
Daily sheet  -> one row per fraction (date, phase, dose, MU, therapist sign); running totals are computed,
               never typed. Missed treatment days are detected from the course's treatment days.
Weekly sheet -> "Early reactions during radiation therapy" (skin, mucous membrane, salivary gland, pharynx &
               oesophagus, larynx, lower GI, genitourinary, weight), graded with CTCAE terms and confirmed by
               the clinician, plus a continue / pause / re-plan / stop decision with rationale.
Completion   -> dates, fractions, total dose, interruptions and gap correction, from the log.

The system organises and computes; it does not recommend a treatment decision.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .database import row_or_none, rows, transaction, utc_now
from .errors import ApiError
from .reference import CTCAE_TERMS

SITE_GROUPS = {
    "breast": "Breast / chest wall", "head_neck": "Head and neck", "thorax": "Thorax (lung, oesophagus)",
    "abdomen": "Abdomen", "pelvis": "Pelvis", "brain": "Brain / CNS", "other": "Other",
}
INTENTS = ("curative", "adjuvant", "neoadjuvant", "palliative", "other")
TECHNIQUES = ["3D-CRT", "IMRT", "VMAT", "SBRT / SRS", "Electrons", "Conventional (2D)", "Brachytherapy"]
ENERGIES = ["6MV", "6MV FFF", "10MV", "10MV FFF", "15MV", "6MeV", "8MeV", "10MeV", "12MeV", "15MeV"]
STATUSES = ("planned", "active", "on_hold", "completed", "discontinued")
DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}

# Rows of the "Early reactions during radiation therapy" sheet, with the CTCAE terms used to grade each.
REACTION_ROWS: list[dict[str, Any]] = [
    {"key": "skin", "label": "Skin", "terms": ["Dermatitis radiation"]},
    {"key": "mucous_membrane", "label": "Mucous membrane", "terms": ["Mucositis oral"]},
    {"key": "salivary_gland", "label": "Salivary gland", "terms": ["Dry mouth"]},
    {"key": "pharynx_esophagus", "label": "Pharynx & oesophagus", "terms": ["Dysphagia", "Esophagitis", "Pharyngeal mucositis"]},
    {"key": "larynx", "label": "Larynx", "terms": ["Hoarseness", "Laryngeal mucositis"]},
    {"key": "lower_gi", "label": "Lower GI", "terms": ["Diarrhea", "Proctitis"]},
    {"key": "genitourinary", "label": "Genitourinary", "terms": ["Cystitis noninfective", "Urinary frequency"]},
    {"key": "general", "label": "General", "terms": ["Fatigue", "Nausea", "Anorexia", "Breast pain", "Pneumonitis"]},
]
ROW_KEYS = {r["key"] for r in REACTION_ROWS}
# Rows expected to react for each treated site (listed first and expanded in the weekly review).
SITE_ROWS = {
    "breast": ["skin", "general"], "head_neck": ["skin", "mucous_membrane", "salivary_gland", "pharynx_esophagus", "larynx", "general"],
    "thorax": ["skin", "pharynx_esophagus", "general"], "abdomen": ["lower_gi", "general"],
    "pelvis": ["lower_gi", "genitourinary", "skin", "general"], "brain": ["skin", "general"], "other": ["skin", "general"],
}
DECISIONS = {
    "continue": "Continue as planned",
    "continue_supportive": "Continue with supportive care",
    "pause": "Pause treatment",
    "replan": "Re-plan (new CT / plan)",
    "stop": "Stop radiotherapy",
}
BREAK_CATEGORIES = ("Public holiday", "Machine downtime", "Toxicity", "Patient unwell", "Patient did not attend", "Logistics", "Other")
REASON_CATEGORIES = ("Toxicity", "Clinical status", "Weight loss / anatomy change", "Patient preference", "Logistics", "Other")
COMPLETION_OUTCOMES = {
    "completed_as_planned": "Completed as planned",
    "completed_modified": "Completed with modification",
    "incomplete": "Not completed",
}
SETUP_FIELDS = ("head_rest", "arm_position", "immobilisation", "bladder", "bolus_cm", "knee_rest", "special_instructions")


# ---------------------------------------------------------------------------------------------- helpers
def _text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _number(value: Any, label: str, *, positive: bool = False, allow_none: bool = True) -> float | None:
    if value in (None, ""):
        if allow_none:
            return None
        raise ApiError(422, f"{label} is required")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ApiError(422, f"{label} must be a number") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ApiError(422, f"{label} must be a number")
    if positive and number <= 0:
        raise ApiError(422, f"{label} must be greater than zero")
    return number


def _date(value: Any, label: str, *, required: bool = False, not_future: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ApiError(422, f"{label} is required")
        return None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ApiError(422, f"{label} must use YYYY-MM-DD format") from None
    if not_future and parsed > date.today():
        raise ApiError(422, f"{label} cannot be in the future")
    return parsed.isoformat()


def _days(value: Any) -> str:
    if value in (None, ""):
        return "1,2,3,4,5"
    items = value if isinstance(value, list) else str(value).split(",")
    try:
        days = sorted({int(d) for d in items})
    except (TypeError, ValueError):
        raise ApiError(422, "treatment_days must list weekdays 1 (Mon) to 7 (Sun)") from None
    if not days or any(d < 1 or d > 7 for d in days):
        raise ApiError(422, "treatment_days must list weekdays 1 (Mon) to 7 (Sun)")
    return ",".join(str(d) for d in days)


def _event(connection: sqlite3.Connection, course_id: int, kind: str, summary: str, actor: str, detail: dict | None = None) -> None:
    connection.execute("INSERT INTO rt_events (course_id, kind, summary, detail_json, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (course_id, kind, summary[:400], json.dumps(detail) if detail else None, actor, utc_now()))


def gy(cgy: float | None) -> str:
    if cgy is None:
        return "—"
    value = cgy / 100
    return f"{value:.2f}".rstrip("0").rstrip(".") + " Gy"


def eqd2(total_cgy: float, per_fraction_cgy: float, alpha_beta: float) -> float:
    """EQD2 (Gy) by the linear-quadratic model, without a time factor. Reference calculation only."""
    d = per_fraction_cgy / 100
    return round(total_cgy / 100 * (d + alpha_beta) / (2 + alpha_beta), 2)


def _course_or_404(connection: sqlite3.Connection, course_id: int) -> dict[str, Any]:
    course = row_or_none(connection, "SELECT * FROM rt_courses WHERE id = ?", (course_id,))
    if not course:
        raise ApiError(404, "Radiotherapy course not found")
    return course


# ---------------------------------------------------------------------------------------------- validation
def _phases(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ApiError(422, "Add at least one phase (dose per fraction and number of fractions)")
    if len(items) > 6:
        raise ApiError(422, "At most 6 phases are supported")
    out = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ApiError(422, f"Phase {index} must be an object")
        dose = _number(item.get("dose_per_fraction_cgy"), f"Phase {index} dose per fraction", positive=True, allow_none=False)
        if dose > 2500:
            raise ApiError(422, f"Phase {index} dose per fraction is in cGy (e.g. 200 for 2 Gy)")
        try:
            fractions = int(item.get("fractions_planned"))
        except (TypeError, ValueError):
            raise ApiError(422, f"Phase {index} needs the number of fractions") from None
        if not 1 <= fractions <= 60:
            raise ApiError(422, f"Phase {index} fractions must be 1–60")
        out.append({"phase_number": index, "name": _text(item.get("name"), 120) or ("Phase " + "I II III IV V VI".split()[index - 1]),
                    "technique": _text(item.get("technique"), 60), "energy": _text(item.get("energy"), 40),
                    "target": _text(item.get("target"), 200), "dose_per_fraction_cgy": dose, "fractions_planned": fractions,
                    "id": item.get("id")})
    return out


def _constraints(items: Any) -> list[dict[str, Any]]:
    if items in (None, ""):
        return []
    if not isinstance(items, list) or len(items) > 30:
        raise ApiError(422, "constraints must be a list of at most 30 rows")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        structure = _text(item.get("structure"), 120)
        if not structure:
            continue
        met = item.get("met") or None
        if met not in (None, "yes", "no", "na"):
            raise ApiError(422, "Constraint 'met' must be yes, no or na")
        out.append({"structure": structure, "constraint_text": _text(item.get("constraint_text"), 200),
                    "achieved_text": _text(item.get("achieved_text"), 200), "met": met})
    return out


def _setup(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    clean = {k: _text(value.get(k), 300) for k in SETUP_FIELDS if _text(value.get(k), 300)}
    return json.dumps(clean) if clean else None


def _course_fields(payload: dict[str, Any], partial: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if not partial or "site" in payload:
        fields["site"] = _text(payload.get("site"), 160)
        if not fields["site"]:
            raise ApiError(422, "Missing required fields", {"fields": ["site"]})
    if "site_group" in payload or not partial:
        group = payload.get("site_group") or "other"
        if group not in SITE_GROUPS:
            raise ApiError(422, "Unknown site group")
        fields["site_group"] = group
    if "intent" in payload:
        if payload["intent"] not in (None, "", *INTENTS):
            raise ApiError(422, "Unknown intent")
        fields["intent"] = payload["intent"] or None
    for key, limit in (("laterality", 20), ("diagnosis", 250), ("stage", 80), ("histopathology", 200), ("machine", 80),
                       ("consultant", 80), ("referred_by", 80), ("notes", 2000)):
        if key in payload:
            fields[key] = _text(payload[key], limit)
    if "start_date" in payload:
        fields["start_date"] = _date(payload["start_date"], "Start date")
    if "treatment_days" in payload or not partial:
        fields["treatment_days"] = _days(payload.get("treatment_days"))
    if "baseline_weight_kg" in payload:
        weight = _number(payload["baseline_weight_kg"], "Baseline weight", positive=True)
        if weight is not None and not 2 <= weight <= 400:
            raise ApiError(422, "Baseline weight must be in kg")
        fields["baseline_weight_kg"] = weight
    if "setup" in payload:
        fields["setup_json"] = _setup(payload["setup"])
    if "chemo_plan_id" in payload:
        fields["chemo_plan_id"] = int(payload["chemo_plan_id"]) if payload["chemo_plan_id"] not in (None, "") else None
    return fields


# ---------------------------------------------------------------------------------------------- create / update
def create_course(db_path: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    fields = _course_fields(payload, partial=False)
    phases = _phases(payload.get("phases"))
    constraints = _constraints(payload.get("constraints"))
    now = utc_now()
    with transaction(db_path) as connection:
        if not row_or_none(connection, "SELECT id FROM patients WHERE id = ?", (patient_id,)):
            raise ApiError(404, "Patient not found")
        if fields.get("chemo_plan_id"):
            plan = row_or_none(connection, "SELECT patient_id FROM treatment_plans WHERE id = ?", (fields["chemo_plan_id"],))
            if not plan or plan["patient_id"] != patient_id:
                raise ApiError(422, "The concurrent chemotherapy plan belongs to another patient")
        columns = ["patient_id", "status", "created_by", "created_at", "updated_at", *fields]
        values = [patient_id, "planned", actor, now, now, *fields.values()]
        course_id = connection.execute(
            f"INSERT INTO rt_courses ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", tuple(values)).lastrowid
        for phase in phases:
            connection.execute(
                """INSERT INTO rt_phases (course_id, phase_number, name, technique, energy, target, dose_per_fraction_cgy, fractions_planned)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (course_id, phase["phase_number"], phase["name"], phase["technique"], phase["energy"], phase["target"],
                 phase["dose_per_fraction_cgy"], phase["fractions_planned"]))
        for order, item in enumerate(constraints):
            connection.execute("INSERT INTO rt_constraints (course_id, structure, constraint_text, achieved_text, met, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                               (course_id, item["structure"], item["constraint_text"], item["achieved_text"], item["met"], order))
        total = sum(p["dose_per_fraction_cgy"] * p["fractions_planned"] for p in phases)
        _event(connection, course_id, "created", f"Course created: {fields['site']}, {gy(total)} in {sum(p['fractions_planned'] for p in phases)} fractions", actor)
    return workspace(db_path, course_id)


def update_course(db_path: str, course_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    fields = _course_fields(payload, partial=True)
    status = payload.get("status")
    reason = _text(payload.get("status_reason"), 1000)
    with transaction(db_path) as connection:
        course = _course_or_404(connection, course_id)
        if status is not None:
            if status not in STATUSES:
                raise ApiError(422, "Invalid status")
            if status == "completed":
                raise ApiError(422, "Use the completion summary to complete a course")
            if status != course["status"]:
                if status in ("on_hold", "discontinued") and not reason:
                    raise ApiError(422, "Give a reason for pausing or stopping radiotherapy", {"fields": ["status_reason"]})
                if status == "active" and not course["approved_at"]:
                    raise ApiError(422, "Approve the plan before starting treatment")
                fields["status"] = status
                _event(connection, course_id, "status", f"Status: {course['status'].replace('_', ' ')} → {status.replace('_', ' ')}"
                       + (f" ({reason})" if reason else ""), actor, {"from": course["status"], "to": status, "reason": reason})
        if "phases" in payload:
            _replace_phases(connection, course_id, _phases(payload["phases"]), actor)
        if "constraints" in payload:
            connection.execute("DELETE FROM rt_constraints WHERE course_id = ?", (course_id,))
            for order, item in enumerate(_constraints(payload["constraints"])):
                connection.execute("INSERT INTO rt_constraints (course_id, structure, constraint_text, achieved_text, met, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                                   (course_id, item["structure"], item["constraint_text"], item["achieved_text"], item["met"], order))
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
            connection.execute(f"UPDATE rt_courses SET {assignments} WHERE id = ?", (*fields.values(), utc_now(), course_id))
            edited = [k for k in fields if k != "status"]
            if edited:
                _event(connection, course_id, "edit", "Edited: " + ", ".join(k.replace("_json", "").replace("_", " ") for k in edited), actor)
    return workspace(db_path, course_id)


def _replace_phases(connection: sqlite3.Connection, course_id: int, phases: list[dict[str, Any]], actor: str) -> None:
    existing = {p["phase_number"]: p for p in rows(connection, "SELECT * FROM rt_phases WHERE course_id = ?", (course_id,))}
    delivered = {r["phase_id"]: r["n"] for r in rows(connection, "SELECT phase_id, COUNT(*) AS n FROM rt_fractions WHERE course_id = ? GROUP BY phase_id", (course_id,))}
    numbers = {p["phase_number"] for p in phases}
    for number, old in existing.items():
        if number not in numbers:
            if delivered.get(old["id"]):
                raise ApiError(422, f"Phase {number} has delivered fractions and cannot be removed")
            connection.execute("DELETE FROM rt_phases WHERE id = ?", (old["id"],))
    for phase in phases:
        old = existing.get(phase["phase_number"])
        if old:
            given = delivered.get(old["id"], 0)
            if phase["fractions_planned"] < given:
                raise ApiError(422, f"Phase {phase['phase_number']} already has {given} fractions delivered")
            if given and abs(phase["dose_per_fraction_cgy"] - old["dose_per_fraction_cgy"]) > 1e-9:
                raise ApiError(422, f"Phase {phase['phase_number']} dose per fraction cannot change after treatment started; add a new phase")
            connection.execute("""UPDATE rt_phases SET name = ?, technique = ?, energy = ?, target = ?, dose_per_fraction_cgy = ?, fractions_planned = ?
                                  WHERE id = ?""", (phase["name"], phase["technique"], phase["energy"], phase["target"],
                                                     phase["dose_per_fraction_cgy"], phase["fractions_planned"], old["id"]))
            if old["fractions_planned"] != phase["fractions_planned"]:
                _event(connection, course_id, "prescription", f"Phase {phase['phase_number']} fractions {old['fractions_planned']} → {phase['fractions_planned']}", actor)
        else:
            connection.execute(
                """INSERT INTO rt_phases (course_id, phase_number, name, technique, energy, target, dose_per_fraction_cgy, fractions_planned)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (course_id, phase["phase_number"], phase["name"], phase["technique"], phase["energy"], phase["target"],
                 phase["dose_per_fraction_cgy"], phase["fractions_planned"]))
            _event(connection, course_id, "prescription", f"Phase {phase['phase_number']} added: {gy(phase['dose_per_fraction_cgy'] * phase['fractions_planned'])} in {phase['fractions_planned']} fractions", actor)


def update_phase(db_path: str, phase_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Isocentre shift, couch movement, SSDs and remarks for one phase (plan sheet)."""
    with transaction(db_path) as connection:
        phase = row_or_none(connection, "SELECT * FROM rt_phases WHERE id = ?", (phase_id,))
        if not phase:
            raise ApiError(404, "Phase not found")
        fields: dict[str, Any] = {}
        for key in ("shift_x", "shift_y", "shift_z"):
            if key in payload:
                value = _number(payload[key], f"Isocentre shift {key[-1].upper()}")
                if value is not None and abs(value) > 100:
                    raise ApiError(422, "Isocentre shifts are in cm")
                fields[key] = value
        for key in ("couch_movement", "remarks", "technique", "energy", "target", "name"):
            if key in payload:
                fields[key] = _text(payload[key], 300)
        if "ssd" in payload:
            ssd = payload["ssd"] if isinstance(payload["ssd"], dict) else {}
            clean = {}
            for kind in ("tps", "actual"):
                for side in ("ap", "rl", "ll"):
                    value = _number((ssd.get(kind) or {}).get(side), f"{kind.upper()} SSD {side.upper()}")
                    if value is not None:
                        clean.setdefault(kind, {})[side] = value
            fields["ssd_json"] = json.dumps(clean) if clean else None
        if not fields:
            raise ApiError(422, "No supported fields supplied")
        connection.execute(f"UPDATE rt_phases SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), phase_id))
        _event(connection, phase["course_id"], "plan", f"Phase {phase['phase_number']} plan data updated ({', '.join(k.replace('_json', '').replace('_', ' ') for k in fields)})", actor)
        course_id = phase["course_id"]
    return workspace(db_path, course_id)


def approve(db_path: str, course_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    physicist = _text(payload.get("physicist"), 80)
    oncologist = _text(payload.get("oncologist"), 80) or actor
    if not physicist:
        raise ApiError(422, "Enter the physicist who checked the plan", {"fields": ["physicist"]})
    with transaction(db_path) as connection:
        course = _course_or_404(connection, course_id)
        if course["status"] in ("completed", "discontinued"):
            raise ApiError(422, "This course is closed")
        now = utc_now()
        connection.execute("""UPDATE rt_courses SET approved_physicist = ?, approved_oncologist = ?, approved_at = ?,
                              status = CASE WHEN status = 'planned' THEN 'active' ELSE status END, updated_at = ? WHERE id = ?""",
                           (physicist, oncologist, now, now, course_id))
        _event(connection, course_id, "approved", f"Plan approved by {oncologist} (radiation oncologist) and {physicist} (physicist)", actor)
    return workspace(db_path, course_id)


# ---------------------------------------------------------------------------------------------- fractions
def _current_phase(phases: list[dict[str, Any]], delivered: dict[int, int]) -> dict[str, Any] | None:
    for phase in phases:
        if delivered.get(phase["id"], 0) < phase["fractions_planned"]:
            return phase
    return None


def add_fraction(db_path: str, course_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    treatment_date = _date(payload.get("treatment_date") or date.today().isoformat(), "Treatment date", required=True, not_future=True)
    with transaction(db_path) as connection:
        course = _course_or_404(connection, course_id)
        if not course["approved_at"]:
            raise ApiError(422, "Approve the plan (physicist and radiation oncologist) before recording treatment")
        if course["status"] in ("completed", "discontinued"):
            raise ApiError(422, "This course is closed; reopen it to record treatment")
        if course["status"] == "on_hold":
            raise ApiError(422, "Treatment is paused. Resume the course before recording a fraction")
        phases = rows(connection, "SELECT * FROM rt_phases WHERE course_id = ? ORDER BY phase_number", (course_id,))
        delivered = {r["phase_id"]: r["n"] for r in rows(connection, "SELECT phase_id, COUNT(*) AS n FROM rt_fractions WHERE course_id = ? GROUP BY phase_id", (course_id,))}
        if payload.get("phase_id"):
            phase = next((p for p in phases if p["id"] == int(payload["phase_id"])), None)
            if not phase:
                raise ApiError(422, "Phase does not belong to this course")
        else:
            phase = _current_phase(phases, delivered)
            if not phase:
                raise ApiError(422, "All planned fractions are delivered. Add an extra fraction explicitly (gap compensation) or complete the course")
        extra = bool(payload.get("extra"))
        note = _text(payload.get("note"), 500)
        if delivered.get(phase["id"], 0) >= phase["fractions_planned"] and not extra:
            raise ApiError(422, f"Phase {phase['phase_number']} is complete ({phase['fractions_planned']} fractions). Mark this as an extra fraction with a reason")
        if extra and not note:
            raise ApiError(422, "Give the reason for the extra fraction (e.g. gap compensation)", {"fields": ["note"]})
        dose = _number(payload.get("dose_cgy"), "Dose", positive=True) or phase["dose_per_fraction_cgy"]
        if abs(dose - phase["dose_per_fraction_cgy"]) > 1e-9 and not note:
            raise ApiError(422, f"Dose differs from the prescribed {phase['dose_per_fraction_cgy']:g} cGy; add a note explaining why", {"fields": ["note"]})
        same_day = row_or_none(connection, "SELECT COUNT(*) AS n FROM rt_fractions WHERE course_id = ? AND treatment_date = ?", (course_id, treatment_date))["n"]
        if same_day and not payload.get("allow_same_day"):
            raise ApiError(409, f"A fraction is already recorded on {treatment_date}. Confirm a second fraction the same day (e.g. twice-daily schedule)")
        mu = _number(payload.get("mu"), "Monitor units", positive=True)
        therapist = _text(payload.get("therapist"), 60)
        if not therapist:
            raise ApiError(422, "Enter the radiation therapist (RTT) who delivered the fraction", {"fields": ["therapist"]})
        connection.execute(
            """INSERT INTO rt_fractions (course_id, phase_id, treatment_date, dose_cgy, mu, therapist, note, extra, recorded_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (course_id, phase["id"], treatment_date, dose, mu, therapist, note, 1 if extra else 0, actor, utc_now()))
        if course["status"] == "planned":
            connection.execute("UPDATE rt_courses SET status = 'active', updated_at = ? WHERE id = ?", (utc_now(), course_id))
        if not course["start_date"]:
            connection.execute("UPDATE rt_courses SET start_date = ? WHERE id = ?", (treatment_date, course_id))
        connection.execute("DELETE FROM rt_breaks WHERE course_id = ? AND break_date = ?", (course_id, treatment_date))
    return workspace(db_path, course_id)


def delete_fraction(db_path: str, fraction_id: int, actor: str) -> dict[str, Any]:
    with transaction(db_path) as connection:
        fraction = row_or_none(connection, "SELECT * FROM rt_fractions WHERE id = ?", (fraction_id,))
        if not fraction:
            raise ApiError(404, "Fraction not found")
        course = _course_or_404(connection, fraction["course_id"])
        if course["status"] == "completed":
            raise ApiError(422, "The course is completed; fractions are locked")
        connection.execute("DELETE FROM rt_fractions WHERE id = ?", (fraction_id,))
        _event(connection, fraction["course_id"], "fraction_removed",
               f"Fraction on {fraction['treatment_date']} ({fraction['dose_cgy']:g} cGy) removed from the log", actor, fraction)
    return workspace(db_path, fraction["course_id"])


def record_break(db_path: str, course_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    break_date = _date(payload.get("break_date"), "Date", required=True)
    category = payload.get("category")
    if category not in BREAK_CATEGORIES:
        raise ApiError(422, "Choose why treatment was not given")
    reason = _text(payload.get("reason"), 300)
    with transaction(db_path) as connection:
        _course_or_404(connection, course_id)
        if row_or_none(connection, "SELECT 1 FROM rt_fractions WHERE course_id = ? AND treatment_date = ?", (course_id, break_date)):
            raise ApiError(422, "A fraction was given on that date")
        connection.execute("""INSERT INTO rt_breaks (course_id, break_date, category, reason, recorded_by, created_at) VALUES (?, ?, ?, ?, ?, ?)
                              ON CONFLICT(course_id, break_date) DO UPDATE SET category = excluded.category, reason = excluded.reason,
                              recorded_by = excluded.recorded_by""",
                           (course_id, break_date, category, reason, actor, utc_now()))
        _event(connection, course_id, "break", f"No treatment on {break_date}: {category}" + (f" ({reason})" if reason else ""), actor)
    return workspace(db_path, course_id)


# ---------------------------------------------------------------------------------------------- weekly review
def _review_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ApiError(422, "items must be a list")
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = item.get("row_key")
        term = item.get("ctcae_term")
        if key not in ROW_KEYS:
            raise ApiError(422, "Unknown reaction row")
        if term not in CTCAE_TERMS or CTCAE_TERMS[term]["kind"] != "clinical":
            raise ApiError(422, f"Unknown CTCAE term for {key.replace('_', ' ')}")
        try:
            grade = int(item.get("grade", 0))
        except (TypeError, ValueError):
            raise ApiError(422, "Grade must be a whole number") from None
        if grade != 0 and grade not in CTCAE_TERMS[term]["grades"]:
            raise ApiError(422, f"Grade {grade} is not defined for {term}")
        if (key, term) in seen:
            raise ApiError(422, f"{term} is listed twice")
        seen.add((key, term))
        out.append({"row_key": key, "ctcae_term": term, "grade": grade, "note": _text(item.get("note"), 300)})
    return out


def save_review(db_path: str, course_id: int, payload: dict[str, Any], actor: str, review_id: int | None = None) -> dict[str, Any]:
    review_date = _date(payload.get("review_date") or date.today().isoformat(), "Review date", required=True, not_future=True)
    weight = _number(payload.get("weight_kg"), "Weight", positive=True)
    if weight is not None and not 2 <= weight <= 400:
        raise ApiError(422, "Weight must be in kg")
    items = _review_items(payload.get("items") or [])
    decision = payload.get("decision") or "continue"
    if decision not in DECISIONS:
        raise ApiError(422, "Choose a decision")
    reason_category = payload.get("reason_category") or None
    rationale = _text(payload.get("rationale"), 2000)
    if decision not in ("continue",) and decision != "continue_supportive":
        if reason_category not in REASON_CATEGORIES or not rationale:
            raise ApiError(422, "A reason and clinical rationale are required to pause, re-plan or stop", {"fields": ["reason_category", "rationale"]})
    if decision == "continue_supportive" and not rationale:
        raise ApiError(422, "Describe the supportive care started", {"fields": ["rationale"]})
    reviewed_by = _text(payload.get("reviewed_by"), 80) or actor
    now = utc_now()
    with transaction(db_path) as connection:
        course = _course_or_404(connection, course_id)
        if review_id is None:
            first = row_or_none(connection, "SELECT MIN(treatment_date) AS d FROM rt_fractions WHERE course_id = ?", (course_id,))["d"]
            start = first or course["start_date"] or review_date
            week = payload.get("week_number")
            try:
                week = int(week) if week not in (None, "") else max(1, (date.fromisoformat(review_date) - date.fromisoformat(start)).days // 7 + 1)
            except (TypeError, ValueError):
                raise ApiError(422, "Week must be a whole number") from None
            review_id = connection.execute(
                """INSERT INTO rt_reviews (course_id, week_number, review_date, weight_kg, notes, decision, reason_category, rationale,
                       reviewed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (course_id, week, review_date, weight, _text(payload.get("notes"), 2000), decision, reason_category, rationale, reviewed_by, now, now)).lastrowid
        else:
            existing = row_or_none(connection, "SELECT * FROM rt_reviews WHERE id = ? AND course_id = ?", (review_id, course_id))
            if not existing:
                raise ApiError(404, "Review not found")
            week = int(payload.get("week_number") or existing["week_number"])
            connection.execute("""UPDATE rt_reviews SET week_number = ?, review_date = ?, weight_kg = ?, notes = ?, decision = ?, reason_category = ?,
                                  rationale = ?, reviewed_by = ?, updated_at = ? WHERE id = ?""",
                               (week, review_date, weight, _text(payload.get("notes"), 2000), decision, reason_category, rationale, reviewed_by, now, review_id))
            connection.execute("DELETE FROM rt_review_items WHERE review_id = ?", (review_id,))
        for item in items:
            connection.execute("INSERT INTO rt_review_items (review_id, row_key, ctcae_term, grade, note) VALUES (?, ?, ?, ?, ?)",
                               (review_id, item["row_key"], item["ctcae_term"], item["grade"], item["note"]))
        graded = [f"{i['ctcae_term']} G{i['grade']}" for i in items if i["grade"]]
        _event(connection, course_id, "review", f"Week {week} review: {', '.join(graded) or 'no reactions'}"
               + (f"; weight {weight:g} kg" if weight else "") + f"; {DECISIONS[decision].lower()}", reviewed_by,
               {"review_id": review_id, "decision": decision, "rationale": rationale})
        new_status = {"pause": "on_hold", "stop": "discontinued"}.get(decision)
        if new_status and course["status"] != new_status:
            connection.execute("UPDATE rt_courses SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, course_id))
            _event(connection, course_id, "status", f"Status: {course['status'].replace('_', ' ')} → {new_status.replace('_', ' ')} (week {week} review)", reviewed_by)
        if weight is not None and course["baseline_weight_kg"] is None:
            connection.execute("UPDATE rt_courses SET baseline_weight_kg = ? WHERE id = ?", (weight, course_id))
    return workspace(db_path, course_id)


def delete_review(db_path: str, review_id: int, actor: str) -> dict[str, Any]:
    with transaction(db_path) as connection:
        review = row_or_none(connection, "SELECT * FROM rt_reviews WHERE id = ?", (review_id,))
        if not review:
            raise ApiError(404, "Review not found")
        connection.execute("DELETE FROM rt_reviews WHERE id = ?", (review_id,))
        _event(connection, review["course_id"], "review_removed", f"Week {review['week_number']} review ({review['review_date']}) deleted", actor)
    return workspace(db_path, review["course_id"])


# ---------------------------------------------------------------------------------------------- completion
def complete(db_path: str, course_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    outcome = payload.get("outcome")
    if outcome not in COMPLETION_OUTCOMES:
        raise ApiError(422, "Choose how the course ended")
    gap_correction = payload.get("gap_correction") or "none"
    if gap_correction not in ("none", "not_needed", "extra_fractions", "other"):
        raise ApiError(422, "Unknown gap correction")
    note = _text(payload.get("notes"), 2000)
    data = workspace(db_path, course_id)
    s = data["summary"]
    if s["fractions_delivered"] == 0:
        raise ApiError(422, "No fractions are recorded")
    if outcome == "completed_as_planned" and s["fractions_delivered"] < s["fractions_planned"]:
        raise ApiError(422, f"{s['fractions_delivered']} of {s['fractions_planned']} fractions are recorded; choose 'completed with modification' or 'not completed'")
    if outcome != "completed_as_planned" and not note:
        raise ApiError(422, "Explain what changed or why treatment stopped", {"fields": ["notes"]})
    if gap_correction == "other" and not note:
        raise ApiError(422, "Describe the gap correction", {"fields": ["notes"]})
    signed_by = _text(payload.get("signed_by"), 80) or actor
    end_date = _date(payload.get("end_date") or s["last_fraction_date"], "End date", required=True)
    completion = {
        "outcome": outcome, "outcome_label": COMPLETION_OUTCOMES[outcome], "start_date": s["first_fraction_date"], "end_date": end_date,
        "fractions_delivered": s["fractions_delivered"], "fractions_planned": s["fractions_planned"],
        "dose_delivered_cgy": s["dose_delivered_cgy"], "dose_planned_cgy": s["dose_planned_cgy"],
        "overall_treatment_days": s["overall_treatment_days"], "missed_days": len(s["gaps"]),
        "phases": [{"name": p["name"], "delivered": p["fractions_delivered"], "planned": p["fractions_planned"],
                    "dose_delivered_cgy": p["dose_delivered_cgy"], "technique": p["technique"]} for p in data["phases"]],
        "gap_correction": gap_correction, "notes": note, "signed_by": signed_by, "signed_at": utc_now(),
    }
    with transaction(db_path) as connection:
        connection.execute("UPDATE rt_courses SET status = ?, completion_json = ?, updated_at = ? WHERE id = ?",
                           ("completed" if outcome != "incomplete" else "discontinued", json.dumps(completion), utc_now(), course_id))
        _event(connection, course_id, "completed", f"{COMPLETION_OUTCOMES[outcome]}: {gy(s['dose_delivered_cgy'])} in {s['fractions_delivered']} fractions, "
               f"{s['first_fraction_date']} to {end_date}", signed_by, completion)
    return workspace(db_path, course_id)


def reopen(db_path: str, course_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    reason = _text(payload.get("reason"), 500)
    if not reason:
        raise ApiError(422, "Give a reason for reopening", {"fields": ["reason"]})
    with transaction(db_path) as connection:
        course = _course_or_404(connection, course_id)
        if course["status"] not in ("completed", "discontinued"):
            raise ApiError(422, "The course is not closed")
        connection.execute("UPDATE rt_courses SET status = 'active', completion_json = NULL, updated_at = ? WHERE id = ?", (utc_now(), course_id))
        _event(connection, course_id, "reopened", f"Course reopened: {reason}", actor)
    return workspace(db_path, course_id)


def delete_course(db_path: str, course_id: int) -> dict[str, Any]:
    with transaction(db_path) as connection:
        if connection.execute("DELETE FROM rt_courses WHERE id = ?", (course_id,)).rowcount == 0:
            raise ApiError(404, "Radiotherapy course not found")
    return {"id": course_id, "deleted": True}


# ---------------------------------------------------------------------------------------------- read model
def _treatment_days(course: dict[str, Any]) -> set[int]:
    return {int(d) for d in (course["treatment_days"] or "1,2,3,4,5").split(",") if d}


def _next_treatment_day(day: date, days: set[int]) -> date:
    probe = day
    for _ in range(14):
        if probe.isoweekday() in days:
            return probe
        probe += timedelta(days=1)
    return day


def _schedule(start: date, count: int, days: set[int]) -> list[date]:
    out, probe = [], start
    while len(out) < count and len(out) < 400:
        if probe.isoweekday() in days:
            out.append(probe)
        probe += timedelta(days=1)
    return out


def weight_change(baseline: float | None, current: float | None) -> dict[str, Any] | None:
    """Percentage change from baseline with the CTCAE 'Weight loss' grade it corresponds to (computed, for review)."""
    if not baseline or current is None:
        return None
    pct = round((current - baseline) / baseline * 100, 1)
    loss = -pct
    grade = 3 if loss >= 20 else 2 if loss >= 10 else 1 if loss >= 5 else 0
    return {"baseline": baseline, "current": current, "change_pct": pct, "ctcae_grade": grade,
            "criterion": CTCAE_TERMS["Weight loss"]["grades"].get(grade) if grade else "Less than 5% loss from baseline"}


def workspace(db_path: str, course_id: int) -> dict[str, Any]:
    with transaction(db_path) as connection:
        return build(connection, course_id)


def build(connection: sqlite3.Connection, course_id: int, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    course = _course_or_404(connection, course_id)
    course["setup"] = json.loads(course.pop("setup_json") or "{}")
    course["completion"] = json.loads(course.pop("completion_json") or "null")
    course["treatment_days_list"] = sorted(_treatment_days(course))
    course["treatment_days_label"] = ", ".join(DAY_NAMES[d] for d in course["treatment_days_list"])
    course["site_group_label"] = SITE_GROUPS.get(course["site_group"], course["site_group"])
    patient = row_or_none(connection, "SELECT id, first_name, last_name, medical_record_number, date_of_birth, sex, diagnosis, phone FROM patients WHERE id = ?",
                          (course["patient_id"],))
    chemo = row_or_none(connection, "SELECT id, regimen_name, status FROM treatment_plans WHERE id = ?", (course["chemo_plan_id"],)) if course["chemo_plan_id"] else None
    phases = rows(connection, "SELECT * FROM rt_phases WHERE course_id = ? ORDER BY phase_number", (course_id,))
    fractions = rows(connection, """SELECT f.*, p.phase_number FROM rt_fractions f JOIN rt_phases p ON p.id = f.phase_id
                                    WHERE f.course_id = ? ORDER BY f.treatment_date, f.id""", (course_id,))
    breaks = {b["break_date"]: b for b in rows(connection, "SELECT * FROM rt_breaks WHERE course_id = ? ORDER BY break_date", (course_id,))}
    constraints = rows(connection, "SELECT * FROM rt_constraints WHERE course_id = ? ORDER BY sort_order, id", (course_id,))
    reviews = rows(connection, "SELECT * FROM rt_reviews WHERE course_id = ? ORDER BY review_date, id", (course_id,))
    for review in reviews:
        review["items"] = rows(connection, "SELECT row_key, ctcae_term, grade, note FROM rt_review_items WHERE review_id = ? ORDER BY id", (review["id"],))
        review["decision_label"] = DECISIONS.get(review["decision"], review["decision"])
    events = rows(connection, "SELECT id, kind, summary, actor, created_at FROM rt_events WHERE course_id = ? ORDER BY created_at DESC, id DESC LIMIT 200", (course_id,))

    # Running totals (computed, never typed).
    by_phase: dict[int, dict[str, float]] = {p["id"]: {"n": 0, "dose": 0.0} for p in phases}
    total = 0.0
    for index, fraction in enumerate(fractions, start=1):
        counter = by_phase.setdefault(fraction["phase_id"], {"n": 0, "dose": 0.0})
        counter["n"] += 1
        counter["dose"] += fraction["dose_cgy"]
        total += fraction["dose_cgy"]
        fraction["number"] = index
        fraction["phase_fraction"] = counter["n"]
        fraction["phase_total_cgy"] = round(counter["dose"], 2)
        fraction["course_total_cgy"] = round(total, 2)
    for phase in phases:
        counter = by_phase[phase["id"]]
        phase["ssd"] = json.loads(phase.pop("ssd_json") or "{}")
        phase["dose_planned_cgy"] = round(phase["dose_per_fraction_cgy"] * phase["fractions_planned"], 2)
        phase["fractions_delivered"] = counter["n"]
        phase["dose_delivered_cgy"] = round(counter["dose"], 2)
        phase["eqd2"] = {"planned_ab10": eqd2(phase["dose_planned_cgy"], phase["dose_per_fraction_cgy"], 10),
                         "planned_ab3": eqd2(phase["dose_planned_cgy"], phase["dose_per_fraction_cgy"], 3)}
        phase["status"] = "complete" if counter["n"] >= phase["fractions_planned"] else "in_progress" if counter["n"] else "pending"

    planned_fx = sum(p["fractions_planned"] for p in phases)
    planned_dose = round(sum(p["dose_planned_cgy"] for p in phases), 2)
    delivered_fx = len(fractions)
    days = _treatment_days(course)
    first = date.fromisoformat(fractions[0]["treatment_date"]) if fractions else None
    last = date.fromisoformat(fractions[-1]["treatment_date"]) if fractions else None
    treated = {f["treatment_date"] for f in fractions}

    # Missed treatment days: expected treatment weekdays without a fraction, from the first fraction to the last
    # (and up to yesterday while the course is active and fractions remain).
    gaps = []
    if first:
        until = last
        open_course = course["status"] in ("active", "on_hold") and delivered_fx < planned_fx
        if open_course and today - timedelta(days=1) > last:
            until = today - timedelta(days=1)
        probe = first
        while probe <= until:
            iso = probe.isoformat()
            if probe.isoweekday() in days and iso not in treated:
                b = breaks.get(iso)
                gaps.append({"date": iso, "weekday": DAY_NAMES[probe.isoweekday()], "category": b["category"] if b else None,
                             "reason": b["reason"] if b else None, "explained": bool(b)})
            probe += timedelta(days=1)
    for iso, b in breaks.items():  # breaks recorded outside the computed window (e.g. before the first fraction)
        if not any(g["date"] == iso for g in gaps):
            gaps.append({"date": iso, "weekday": DAY_NAMES[date.fromisoformat(iso).isoweekday()], "category": b["category"],
                         "reason": b["reason"], "explained": True})
    gaps.sort(key=lambda g: g["date"])

    remaining = max(planned_fx - delivered_fx, 0)
    current = _current_phase(phases, {p["id"]: p["fractions_delivered"] for p in phases})
    next_date = None
    projected_end = last.isoformat() if last and not remaining else None
    if remaining and course["status"] in ("planned", "active"):
        start_from = max(today, (last + timedelta(days=1)) if last else today)
        if not last and course["start_date"]:
            start_from = max(today, date.fromisoformat(course["start_date"]))
        schedule = _schedule(start_from, remaining, days)
        next_date = schedule[0].isoformat() if schedule else None
        projected_end = schedule[-1].isoformat() if schedule else None
    planned_ott = None
    if first:
        planned_schedule = _schedule(first, planned_fx, days)
        planned_ott = (planned_schedule[-1] - first).days + 1 if planned_schedule else None
    end_for_ott = date.fromisoformat(projected_end) if projected_end else last
    ott = (end_for_ott - first).days + 1 if first and end_for_ott else None

    # Weekly matrix: rows of the paper sheet x weeks.
    baseline_weight = course["baseline_weight_kg"] or next((r["weight_kg"] for r in reviews if r["weight_kg"]), None)
    weights = [{"week": r["week_number"], "date": r["review_date"], "weight_kg": r["weight_kg"]} for r in reviews if r["weight_kg"] is not None]
    latest_weight = weights[-1]["weight_kg"] if weights else None
    worst: dict[str, dict[str, Any]] = {}
    for review in reviews:
        for item in review["items"]:
            if item["grade"] and item["grade"] >= worst.get(item["ctcae_term"], {}).get("grade", 0):
                worst[item["ctcae_term"]] = {"term": item["ctcae_term"], "grade": item["grade"], "week": review["week_number"], "date": review["review_date"]}
    latest_review = reviews[-1] if reviews else None
    latest_items = {i["ctcae_term"]: i["grade"] for i in (latest_review["items"] if latest_review else [])}
    # Week number the next review falls in.
    next_week = (max((r["week_number"] for r in reviews), default=0) + 1)
    due_review = bool(first and course["status"] == "active" and (not latest_review or (today - date.fromisoformat(latest_review["review_date"])).days >= 7))

    attention = []
    unexplained = [g for g in gaps if not g["explained"]]
    if unexplained:
        attention.append({"severity": "warning", "text": f"{len(unexplained)} missed treatment day(s) without a recorded reason: "
                          + ", ".join(g["date"] for g in unexplained[:6])})
    if planned_ott and ott and ott > planned_ott and not course["completion"]:
        extra = ott - planned_ott
        attention.append({"severity": "warning", "text": f"Overall treatment time {ott} days vs {planned_ott} planned (+{extra} day{'s' if extra != 1 else ''}). "
                          "Decide whether gap correction is needed."})
    wc = weight_change(baseline_weight, latest_weight)
    if wc and wc["ctcae_grade"] >= 1:
        attention.append({"severity": "alert" if wc["ctcae_grade"] >= 2 else "warning",
                          "text": f"Weight {wc['change_pct']:+g}% from baseline ({wc['baseline']:g} → {wc['current']:g} kg), CTCAE weight loss G{wc['ctcae_grade']}."})
    for term, grade in latest_items.items():
        if grade >= 3:
            attention.append({"severity": "alert", "text": f"{term} grade {grade} at the latest review."})
    if due_review:
        attention.append({"severity": "info", "text": "Weekly review is due."})
    if course["status"] == "active" and not course["approved_at"]:
        attention.append({"severity": "alert", "text": "Plan not approved."})
    if constraints and any(c["met"] == "no" for c in constraints):
        attention.append({"severity": "warning", "text": "One or more dose constraints are marked as not met."})

    relevant = SITE_ROWS.get(course["site_group"], SITE_ROWS["other"])
    rows_out = sorted(REACTION_ROWS, key=lambda r: (r["key"] not in relevant, [x["key"] for x in REACTION_ROWS].index(r["key"])))
    return {
        "course": course, "patient": patient, "chemo_plan": chemo, "phases": phases, "fractions": fractions, "constraints": constraints,
        "reviews": reviews, "events": events,
        "reaction_rows": [{**r, "relevant": r["key"] in relevant,
                           "criteria": {t: {str(g): c for g, c in CTCAE_TERMS[t]["grades"].items()} for t in r["terms"]}} for r in rows_out],
        "summary": {
            "fractions_planned": planned_fx, "fractions_delivered": delivered_fx, "fractions_remaining": remaining,
            "dose_planned_cgy": planned_dose, "dose_delivered_cgy": round(total, 2),
            "prescription": " + ".join(f"{gy(p['dose_planned_cgy'])}/{p['fractions_planned']}#" for p in phases),
            "first_fraction_date": first.isoformat() if first else None, "last_fraction_date": last.isoformat() if last else None,
            "next_fraction_date": next_date, "projected_end_date": projected_end,
            "current_phase_id": current["id"] if current else None, "current_phase_number": current["phase_number"] if current else None,
            "overall_treatment_days": ott, "planned_treatment_days": planned_ott, "gaps": gaps,
            "weights": weights, "baseline_weight_kg": baseline_weight, "weight_change": wc,
            "worst_toxicities": sorted(worst.values(), key=lambda w: (-w["grade"], w["term"])),
            "latest_review": {"week": latest_review["week_number"], "date": latest_review["review_date"], "decision": latest_review["decision_label"]} if latest_review else None,
            "next_review_week": next_week, "review_due": due_review, "attention": attention,
            "eqd2_note": "EQD2 by the linear-quadratic model without a time factor; reference calculation only.",
        },
        "options": {"site_groups": SITE_GROUPS, "intents": INTENTS, "techniques": TECHNIQUES, "energies": ENERGIES,
                    "decisions": DECISIONS, "break_categories": BREAK_CATEGORIES, "reason_categories": REASON_CATEGORIES,
                    "completion_outcomes": COMPLETION_OUTCOMES},
    }


def list_for_patient(connection: sqlite3.Connection, patient_id: int) -> list[dict[str, Any]]:
    out = []
    for course in rows(connection, "SELECT id FROM rt_courses WHERE patient_id = ? ORDER BY COALESCE(start_date, created_at) DESC, id DESC", (patient_id,)):
        data = build(connection, course["id"])
        c, s = data["course"], data["summary"]
        out.append({"id": c["id"], "site": c["site"], "site_group": c["site_group"], "intent": c["intent"], "status": c["status"],
                    "start_date": c["start_date"], "approved_at": c["approved_at"], "prescription": s["prescription"],
                    "fractions_delivered": s["fractions_delivered"], "fractions_planned": s["fractions_planned"],
                    "dose_delivered_cgy": s["dose_delivered_cgy"], "dose_planned_cgy": s["dose_planned_cgy"],
                    "next_fraction_date": s["next_fraction_date"], "projected_end_date": s["projected_end_date"],
                    "attention": s["attention"], "technique": ", ".join(sorted({p["technique"] for p in data["phases"] if p["technique"]}))})
    return out


def options() -> dict[str, Any]:
    return {"site_groups": SITE_GROUPS, "intents": INTENTS, "techniques": TECHNIQUES, "energies": ENERGIES, "decisions": DECISIONS,
            "break_categories": BREAK_CATEGORIES, "reason_categories": REASON_CATEGORIES, "completion_outcomes": COMPLETION_OUTCOMES,
            "setup_fields": SETUP_FIELDS, "reaction_rows": REACTION_ROWS, "site_rows": SITE_ROWS}

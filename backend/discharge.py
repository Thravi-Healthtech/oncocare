"""Discharge / end-of-treatment summary: draft from the record, clinician review, finalise.

The draft is built from the record for the chosen period (treatments given, radiotherapy log and weekly reviews,
toxicities, complaints and how they were handled, labs, uploaded reports). Only the narrative "course during
treatment" is written as prose (Claude when AI is on, otherwise fixed rules) and every sentence cites its sources.
Medicines, condition at discharge, advice and follow-up are entered by the clinician; they are never generated.
A finalised summary is locked; changes need a new version.
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import records
from . import radiotherapy as rt
from .database import row_or_none, rows, transaction, utc_now
from .errors import ApiError

KINDS = {"discharge": "Discharge summary", "end_of_treatment": "End-of-treatment summary"}
DOCTOR_FIELDS = ("diagnosis", "condition_at_discharge", "advice", "medications", "follow_up", "title", "course_text")


def _load(connection: sqlite3.Connection, summary_id: int) -> dict[str, Any]:
    row = row_or_none(connection, "SELECT * FROM discharge_summaries WHERE id = ?", (summary_id,))
    if not row:
        raise ApiError(404, "Summary not found")
    return row


def _view(row: dict[str, Any]) -> dict[str, Any]:
    content = json.loads(row["content_json"])
    sources = json.loads(row["sources_json"] or "[]")
    course = content.get("course") or {}
    if course.get("text") is not None:
        course.update(records.check_citations(course["text"], {s["id"] for s in sources} | {"S0"}))
    return {k: row[k] for k in ("id", "patient_id", "kind", "status", "version", "parent_id", "period_from", "period_to", "generated_method",
                                "created_by", "finalised_by", "finalised_at", "created_at", "updated_at")} | {"content": content, "sources": sources,
                                                                                                                 "kind_label": KINDS.get(row["kind"], row["kind"])}


def _default_period(connection: sqlite3.Connection, patient_id: int) -> str:
    starts = [r["d"] for r in (
        row_or_none(connection, """SELECT MIN(COALESCE(c.administered_date, c.planned_date)) AS d FROM treatment_cycles c
                                   JOIN treatment_plans p ON p.id = c.treatment_plan_id WHERE p.patient_id = ? AND c.status IN ('given', 'modified')""", (patient_id,)),
        row_or_none(connection, """SELECT MIN(f.treatment_date) AS d FROM rt_fractions f JOIN rt_courses rc ON rc.id = f.course_id
                                   WHERE rc.patient_id = ? AND rc.status IN ('active', 'on_hold', 'completed', 'discontinued', 'planned')""", (patient_id,)),
        row_or_none(connection, """SELECT MIN(COALESCE(admission_date, surgery_date)) AS d FROM surgical_cases
                                   WHERE patient_id = ? AND status IN ('completed', 'discharged')""", (patient_id,)),
    ) if r and r["d"]]
    if not starts:
        return date.today().isoformat()
    # the pathway starts at the tumour board that decided on treatment, if it met shortly before the first treatment
    board = row_or_none(connection, """SELECT MIN(note_date) AS d FROM clinical_notes WHERE patient_id = ? AND category = 'tumour_board'
                                       AND note_date <= ? AND note_date >= ?""",
                        (patient_id, min(starts), (date.fromisoformat(min(starts)) - timedelta(days=60)).isoformat()))
    return min(starts + ([board["d"]] if board and board["d"] else []))


def _long_date(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d %b %Y") if value else ""


def _diagnosis(facts: dict[str, Any]) -> str:
    diagnoses = (facts.get("oncology") or {}).get("diagnoses") or []
    if diagnoses:
        return "; ".join(d["summary"] for d in diagnoses)
    parts = [facts["patient"].get("diagnosis") or ""]
    for course in facts["radiotherapy"]:
        extra = ", ".join(x for x in (course.get("stage"), course.get("histopathology")) if x)
        if extra and extra not in parts[0]:
            parts.append(extra)
            break
    return "; ".join(p for p in parts if p) or "Not recorded"


def _build(connection: sqlite3.Connection, patient_id: int, period_from: str, period_to: str, kind: str, actor: str,
           keep: dict[str, Any] | None = None, narrative: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    facts = records.collect(connection, patient_id, period_from, period_to)
    keep = keep or {}
    course = keep.get("course") if not narrative and keep.get("course") else records.narrate(facts, "discharge summary 'course during treatment'")
    content = {
        "title": keep.get("title") or KINDS[kind],
        "patient": facts["patient"],
        "consultant": keep.get("consultant") or actor,
        "diagnosis": keep.get("diagnosis") or _diagnosis(facts),
        "chemotherapy": facts["chemotherapy"],
        "radiotherapy": facts["radiotherapy"],
        "toxicities": facts["toxicities"],
        "complaints": facts["complaints"],
        "labs": [{k: l[k] for k in ("test_name", "value", "unit", "collection_date", "flag", "ref_low", "ref_high")} for l in facts["labs"]],
        "documents": [{"title": i["title"], "date": i["date"], "detail": i["detail"][:400]} for i in facts["items"] if i["kind"] == "document"],
        "course": {k: course.get(k) for k in ("text", "method", "model", "error")},
        "condition_at_discharge": keep.get("condition_at_discharge") or "",
        "medications": keep.get("medications") or [],
        "advice": keep.get("advice") or "",
        "follow_up": keep.get("follow_up") or {"date": "", "with": "", "instructions": "", "none": False},
    }
    excluded = {a["key"] for a in keep.get("activity") or [] if not a.get("include", True)}
    content["surgery"] = facts.get("surgery", [])
    onco = facts.get("oncology") or {}
    content["staging"] = [{k: d.get(k) for k in ("diagnosis_date", "primary_site", "laterality", "histology", "grade", "method", "tnm", "stage_group",
                                                  "staging_system", "intent", "ecog", "biomarkers", "markers", "summary")} for d in onco.get("diagnoses") or []]
    content["response"] = {
        "assessments": [{k: a.get(k) for k in ("assessment_date", "modality_label", "timepoint_label", "cycle_label", "sum_mm", "change_from_baseline_pct",
                                                "change_from_nadir_pct", "response", "response_label", "response_override", "response_note", "computed_reason", "markers")}
                        | {"targets": [f"{l['location']} {l['size_mm']:g} mm" for l in a["lesions"] if l["kind"] == "target"],
                           "new": [l["location"] for l in a["lesions"] if l["kind"] == "new"] or (["yes"] if a.get("new_lesions") else [])}
                        for a in onco.get("assessments") or [] if not period_from or a["assessment_date"] >= period_from or a["timepoint"] == "baseline"],
        "best": (onco.get("best") or {}).get("response"), "latest": (onco.get("latest") or {}).get("response")}
    content["activity"] = [{"key": activity_key(i), "id": i["id"], "date": i["date"], "kind": i["kind"], "title": i["title"], "detail": i["detail"],
                            "severity": i.get("severity"), "include": activity_key(i) not in excluded} for i in facts["items"]]
    sources = [{k: i[k] for k in ("id", "date", "kind", "title", "detail", "ref")} for i in facts["items"]]
    return content, sources, course.get("method") or "rules"


ACTIVITY_LABEL = {"chemo": "Chemotherapy", "rt": "Radiotherapy", "rt_review": "Radiotherapy review", "surgery": "Surgery", "surgery_note": "Post-operative",
                  "toxicity": "Toxicity (CTCAE)", "labs": "Investigations", "complaint": "Patient message", "note": "Clinical note", "document": "Report",
                  "discharge": "Summary", "prescription": "Prescription",
                  "staging": "Diagnosis & staging", "response": "Tumour assessment"}


def activity_key(item: dict[str, Any]) -> str:
    return f"{item['date']}|{item['kind']}|{item['title']}"


def list_for_patient(db: str, patient_id: int) -> list[dict[str, Any]]:
    with transaction(db) as connection:
        if not row_or_none(connection, "SELECT id FROM patients WHERE id = ?", (patient_id,)):
            raise ApiError(404, "Patient not found")
        out = []
        for row in rows(connection, "SELECT * FROM discharge_summaries WHERE patient_id = ? ORDER BY created_at DESC, id DESC", (patient_id,)):
            view = _view(row)
            out.append({k: view[k] for k in ("id", "kind", "kind_label", "status", "version", "period_from", "period_to", "finalised_by", "finalised_at",
                                             "created_at", "updated_at", "created_by")} | {"title": view["content"]["title"],
                                                                                           "follow_up": view["content"].get("follow_up"),
                                                                                           "unsupported": (view["content"].get("course") or {}).get("unsupported", 0)})
        return out


def create_draft(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    kind = payload.get("kind") or "discharge"
    if kind not in KINDS:
        raise ApiError(422, "Unknown summary type")
    with transaction(db) as connection:
        if not row_or_none(connection, "SELECT id FROM patients WHERE id = ?", (patient_id,)):
            raise ApiError(404, "Patient not found")
        period_from = records._iso(payload.get("period_from"), "From date", _default_period(connection, patient_id))
        period_to = records._iso(payload.get("period_to"), "To date", date.today().isoformat())
        if period_to < period_from:
            raise ApiError(422, "The period ends before it starts")
    # Narrative generation (possibly an AI call) runs outside the write transaction.
    with transaction(db) as connection:
        content, sources, method = _build(connection, patient_id, period_from, period_to, kind, actor)
    now = utc_now()
    with transaction(db) as connection:
        summary_id = connection.execute(
            """INSERT INTO discharge_summaries (patient_id, kind, status, version, period_from, period_to, content_json, sources_json, generated_method,
                   created_by, created_at, updated_at) VALUES (?, ?, 'draft', 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (patient_id, kind, period_from, period_to, json.dumps(content), json.dumps(sources), method, actor, now, now)).lastrowid
        return _view(_load(connection, summary_id))


def get(db: str, summary_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        return _view(_load(connection, summary_id))


def _medications(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list) or len(items) > 25:
        raise ApiError(422, "Medicines must be a list of at most 25 rows")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = records._text(item.get("name"), 120)
        if not name:
            if any(records._text(item.get(k), 10) for k in ("dose", "frequency", "duration", "instructions")):
                raise ApiError(422, "Every medicine row needs a name")
            continue
        out.append({"name": name, **{k: records._text(item.get(k), 120) or "" for k in ("dose", "route", "frequency", "duration")},
                    "instructions": records._text(item.get("instructions"), 300) or ""})
    return out


def update_draft(db: str, summary_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    with transaction(db) as connection:
        row = _load(connection, summary_id)
        if row["status"] != "draft":
            raise ApiError(409, "This summary is finalised. Create a new version to change it")
        content = json.loads(row["content_json"])
        for key in ("diagnosis", "condition_at_discharge", "advice", "title"):
            if key in payload:
                content[key] = records._text(payload[key], 4000 if key != "title" else 120) or ""
        if "medications" in payload:
            content["medications"] = _medications(payload["medications"])
        if "activity_excluded" in payload:
            excluded = {str(k) for k in payload["activity_excluded"] or []}
            for item in content.get("activity") or []:
                item["include"] = item["key"] not in excluded
        if "follow_up" in payload:
            fu = payload["follow_up"] if isinstance(payload["follow_up"], dict) else {}
            fu_date = fu.get("date") or ""
            if fu_date:
                try:
                    fu_date = date.fromisoformat(str(fu_date)[:10]).isoformat()
                except ValueError:
                    raise ApiError(422, "Follow-up date must use YYYY-MM-DD format") from None
            content["follow_up"] = {"date": fu_date, "with": records._text(fu.get("with"), 120) or "",
                                    "instructions": records._text(fu.get("instructions"), 600) or "", "none": bool(fu.get("none"))}
        if "course_text" in payload:
            text = records._text(payload["course_text"], 6000) or ""
            if text != (content.get("course") or {}).get("text"):
                content["course"] = {"text": text, "method": "edited", "model": None, "error": None}
        connection.execute("UPDATE discharge_summaries SET content_json = ?, updated_at = ? WHERE id = ?", (json.dumps(content), utc_now(), summary_id))
        return _view(_load(connection, summary_id))


def regenerate(db: str, summary_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Rebuild the record-derived sections (and, unless asked not to, the narrative); clinician-entered fields are kept."""
    with transaction(db) as connection:
        row = _load(connection, summary_id)
        if row["status"] != "draft":
            raise ApiError(409, "This summary is finalised. Create a new version to change it")
        keep = json.loads(row["content_json"])
        period_from = records._iso(payload.get("period_from") or row["period_from"], "From date")
        period_to = records._iso(payload.get("period_to") or row["period_to"], "To date")
        if period_to < period_from:
            raise ApiError(422, "The period ends before it starts")
    with transaction(db) as connection:
        content, sources, method = _build(connection, row["patient_id"], period_from, period_to, row["kind"], actor, keep,
                                          narrative=payload.get("narrative", True) is not False)
    with transaction(db) as connection:
        connection.execute("""UPDATE discharge_summaries SET content_json = ?, sources_json = ?, generated_method = ?, period_from = ?, period_to = ?,
                              updated_at = ? WHERE id = ?""", (json.dumps(content), json.dumps(sources), method, period_from, period_to, utc_now(), summary_id))
        return _view(_load(connection, summary_id))


def finalise(db: str, summary_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    if payload.get("confirmed") is not True:
        raise ApiError(422, "Confirm that you have reviewed the summary", {"fields": ["confirmed"]})
    with transaction(db) as connection:
        row = _load(connection, summary_id)
        if row["status"] != "draft":
            raise ApiError(409, "Only a draft can be finalised")
        content = json.loads(row["content_json"])
        missing = []
        if not content.get("condition_at_discharge", "").strip():
            missing.append("condition_at_discharge")
        fu = content.get("follow_up") or {}
        if not fu.get("date") and not fu.get("none"):
            missing.append("follow_up")
        if not (content.get("course") or {}).get("text", "").strip():
            missing.append("course_text")
        if missing:
            raise ApiError(422, "Complete these sections before finalising", {"fields": missing})
        now = utc_now()
        connection.execute("UPDATE discharge_summaries SET status = 'final', finalised_by = ?, finalised_at = ?, updated_at = ? WHERE id = ?",
                           (actor, now, now, summary_id))
        if row["parent_id"]:
            connection.execute("UPDATE discharge_summaries SET status = 'superseded', updated_at = ? WHERE id = ? AND status = 'final'", (now, row["parent_id"]))
        return _view(_load(connection, summary_id))


def revise(db: str, summary_id: int, actor: str) -> dict[str, Any]:
    with transaction(db) as connection:
        row = _load(connection, summary_id)
        if row["status"] != "final":
            raise ApiError(409, "Only a finalised summary can be revised")
        now = utc_now()
        new_id = connection.execute(
            """INSERT INTO discharge_summaries (patient_id, kind, status, version, parent_id, period_from, period_to, content_json, sources_json,
                   generated_method, created_by, created_at, updated_at) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["patient_id"], row["kind"], row["version"] + 1, row["id"], row["period_from"], row["period_to"], row["content_json"],
             row["sources_json"], row["generated_method"], actor, now, now)).lastrowid
        return _view(_load(connection, new_id))


def delete_draft(db: str, summary_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        row = _load(connection, summary_id)
        if row["status"] != "draft":
            raise ApiError(409, "Finalised summaries are kept; create a new version instead")
        connection.execute("DELETE FROM discharge_summaries WHERE id = ?", (summary_id,))
    return {"id": summary_id, "deleted": True}


def for_patient(db: str, patient_id: int) -> list[dict[str, Any]]:
    """Finalised summaries visible in the patient portal (no source list, no internal notes)."""
    with transaction(db) as connection:
        out = []
        for row in rows(connection, "SELECT * FROM discharge_summaries WHERE patient_id = ? AND status = 'final' ORDER BY finalised_at DESC", (patient_id,)):
            content = json.loads(row["content_json"])
            out.append({"id": row["id"], "title": content.get("title"), "kind": row["kind"], "period_from": row["period_from"], "period_to": row["period_to"],
                        "finalised_at": row["finalised_at"], "finalised_by": row["finalised_by"], "follow_up": content.get("follow_up"),
                        "medications": content.get("medications") or [], "advice": content.get("advice") or ""})
        return out


# ---------------------------------------------------------------------------------------------- printable page
def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _strip_citations(text: str) -> str:
    import re
    return re.sub(r"\s*(\[S\d+\])+", "", text or "").strip()



def _staging_table(d: dict[str, Any]) -> str:
    site = " ".join(x for x in ((d.get("laterality") or "").replace("na", "").capitalize(), d["primary_site"]) if x)
    rows_ = [("Primary site", site),
             ("Diagnosed", d["diagnosis_date"] + (f" by {d['method']}" if d.get("method") else "")),
             ("Histology", " ".join(x for x in (d.get("histology") or "", f"(grade {d['grade']})" if d.get("grade") else "") if x)),
             ("TNM / stage", ", ".join(x for x in (d.get("tnm") or "", f"Stage {d['stage_group']}" if d.get("stage_group") else "", d.get("staging_system") or "") if x)),
             ("Biomarkers", ", ".join(f"{b['name']} {b['value']}" for b in d.get("biomarkers") or [])),
             ("Baseline markers", ", ".join(f"{m['name']} {m['value']} {m['unit']}".strip() for m in d.get("markers") or [])),
             ("Intent / ECOG", ", ".join(x for x in ((d.get("intent") or "").capitalize(), f"ECOG {d['ecog']}" if d.get("ecog") is not None else "") if x))]
    return "<table><tbody>" + "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows_ if v) + "</tbody></table>"


def _response_table(resp: dict[str, Any]) -> str:
    if not resp.get("assessments"):
        return ""
    out = ["<table><thead><tr><th>Date</th><th>Assessment</th><th>Target lesions</th><th>Sum</th><th>vs baseline</th><th>Markers</th><th>Response</th></tr></thead><tbody>"]
    for a in resp["assessments"]:
        when = a["timepoint_label"] + (f" · {a['cycle_label']}" if a.get("cycle_label") else "")
        total = f"{a['sum_mm']:g} mm" if a["sum_mm"] is not None else "—"
        change = f"{a['change_from_baseline_pct']:+.0f}%" if a["change_from_baseline_pct"] is not None else "—"
        markers = ", ".join(f"{m['name']} {m['value']} {m['unit']}".strip() for m in a.get("markers") or []) or "—"
        note = f'<br><span class="meta">clinician: {_e(a.get("response_note") or "")}</span>' if a.get("response_override") else ""
        new = "<br><b>New lesion</b>" if a.get("new") else ""
        out.append(f"<tr><td style=\"white-space:nowrap\">{_e(_long_date(a['assessment_date']))}</td><td>{_e(a['modality_label'])}<br><span class=\"meta\">{_e(when)}</span></td>"
                   f"<td>{_e('; '.join(a['targets']) or '—')}{new}</td><td>{_e(total)}</td><td>{_e(change)}</td><td>{_e(markers)}</td>"
                   f"<td><b>{_e(a.get('response') or 'Baseline')}</b> {_e(a.get('response_label') or '') if a.get('response') else ''}{note}</td></tr>")
    out.append("</tbody></table>")
    if resp.get("best"):
        latest = f" · latest: <b>{_e(resp['latest'])}</b>" if resp.get("latest") else ""
        out.append(f"<p>Best response: <b>{_e(resp['best'])}</b>{latest} (RECIST 1.1 unless recorded by the clinician).</p>")
    return "".join(out)


def print_html(db: str, summary_id: int, patient_id: int | None = None) -> str:
    with transaction(db) as connection:
        row = _load(connection, summary_id)
        if patient_id is not None and (row["patient_id"] != patient_id or row["status"] != "final"):
            raise ApiError(404, "Summary not found")
        view = _view(row)
    c = view["content"]
    p = c["patient"]
    draft = view["status"] != "final"
    section = lambda title, body: f'<section><h2>{_e(title)}</h2>{body}</section>' if body else ""  # noqa: E731
    chemo = "".join(
        f"<p><strong>{_e(x['regimen'])}</strong> ({_e(', '.join(x['drugs']))}) — {x['cycles_given']} of {x['cycles_planned']} cycles"
        + (f", {_e(x['first_date'])} to {_e(x['last_date'])}" if x["first_date"] else "") + (f". {_e('; '.join(x['changes']))}" if x["changes"] else "") + ".</p>"
        for x in c["chemotherapy"])
    radio = "".join(
        f"<p><strong>{_e(x['site'])}</strong> — {rt.gy(x['dose_delivered_cgy'])} in {x['fractions_delivered']} of {x['fractions_planned']} fractions"
        + (f", {_e(x['first_date'])} to {_e(x['last_date'])}" if x["first_date"] else "") + ".</p><ul>"
        + "".join(f"<li>{_e(ph['name'])}: {rt.gy(ph['dose_per_fraction_cgy'])} × {ph['fractions_planned']} {_e(ph['technique'] or '')} {_e(ph['energy'] or '')} "
                  f"— delivered {ph['fractions_delivered']}/{ph['fractions_planned']}</li>" for ph in x["phases"])
        + (f"<li>Interruptions: {_e('; '.join(g['date'] + ' ' + (g['category'] or 'no reason recorded') for g in x['gaps']))}</li>" if x["gaps"] else "")
        + (f"<li>Concurrent: {_e(x['concurrent_chemo'])}</li>" if x["concurrent_chemo"] else "")
        + (f"<li>Status: {_e(x['completion']['outcome_label'])}</li>" if x["completion"] else "") + "</ul>"
        for x in c["radiotherapy"])
    surg = "".join(
        f"<p><strong>{_e(x['procedure'])}</strong> — "
        + ((("operated " if x["status"] in ("completed", "discharged") else "planned for ") + _e(_long_date(x["surgery_date"]))) if x.get("surgery_date") else _e(x["status_label"]))
        + (f", discharged {_e(_long_date(x['discharge_date']))}" if x.get("discharge_date") else "")
        + (f" ({_e(x['approach'])})" if x.get("approach") else "") + (f", surgeon {_e(x['surgeon'])}" if x.get("surgeon") else "") + ".</p><ul>"
        + (f"<li>Findings: {_e(x['findings'])}</li>" if x.get("findings") else "")
        + (f"<li>Histopathology: {_e(x['pathology'].get('histology'))}{', ' + _e(x['stage']) if x.get('stage') else ''}"
           f"{', margins ' + _e(x['pathology'].get('margins')) if x['pathology'].get('margins') else ''}"
           f"{', nodes ' + _e(x['pathology'].get('nodes_positive', 0)) + '/' + _e(x['pathology'].get('nodes_examined')) if x['pathology'].get('nodes_examined') is not None else ''}"
           f"{', ' + _e(x['pathology'].get('biomarkers')) if x['pathology'].get('biomarkers') else ''}</li>" if x.get("pathology") else "")
        + (f"<li>Complications: {_e('; '.join(x['complications']))}</li>" if x.get("complications") else "")
        + (f"<li>Admission {_e(x['admission_date'])} to discharge {_e(x['discharge_date'])} ({x['length_of_stay']} days)</li>" if x.get("length_of_stay") is not None else "")
        + (f"<li>Cancelled: {_e(x['cancel_reason'])}</li>" if x.get("cancel_reason") else "") + "</ul>"
        for x in c.get("surgery") or [])
    included = [a for a in c.get("activity") or [] if a.get("include", True)]
    activity, last = "", ""
    for a in included:
        if a["date"] != last:
            activity += ("</ul>" if last else "") + f"<h3>{_e(date.fromisoformat(a['date']).strftime('%a %d %b %Y'))}</h3><ul class=\"act\">"
            last = a["date"]
        activity += f"<li><b>{_e(ACTIVITY_LABEL.get(a['kind'], a['kind']))}</b> — {_e(a['title'])}<br><span>{_e(_strip_citations(a['detail']))}</span></li>"
    activity += "</ul>" if included else ""
    tox = ("<table><thead><tr><th>Toxicity (CTCAE)</th><th>Worst grade</th><th>Date</th><th>Recorded in</th></tr></thead><tbody>"
           + "".join(f"<tr><td>{_e(t['term'])}</td><td>G{t['worst_grade']}</td><td>{_e(t['date'])}</td><td>{_e(t['source'])}</td></tr>" for t in c["toxicities"])
           + "</tbody></table>") if c["toxicities"] else ""
    complaints = "<ul>" + "".join(f"<li>{_e(x['date'])} ({_e(x['source'])}): {_e(x['summary'])}" + (f" — red flags: {_e(', '.join(x['red_flags']))}" if x["red_flags"] else "")
                                  + f" — {_e(x['outcome'])}</li>" for x in c["complaints"]) + "</ul>" if c["complaints"] else ""
    labs = ("<table><thead><tr><th>Test</th><th>Value</th><th>Date</th></tr></thead><tbody>"
            + "".join(f"<tr><td>{_e(l['test_name'])}</td><td>{l['value']:g} {_e(l['unit'])}{' (' + _e(l['flag']) + ')' if l['flag'] else ''}</td><td>{_e(l['collection_date'])}</td></tr>"
                      for l in c["labs"]) + "</tbody></table>") if c["labs"] else ""
    docs = "<ul>" + "".join(f"<li>{_e(d['title'])} ({_e(d['date'])}): {_e(d['detail'])}</li>" for d in c["documents"]) + "</ul>" if c["documents"] else ""
    meds = ("<table><thead><tr><th>Medicine</th><th>Dose</th><th>Route</th><th>How often</th><th>For how long</th><th>Instructions</th></tr></thead><tbody>"
            + "".join(f"<tr><td>{_e(m['name'])}</td><td>{_e(m['dose'])}</td><td>{_e(m.get('route', ''))}</td><td>{_e(m['frequency'])}</td><td>{_e(m['duration'])}</td>"
                      f"<td>{_e(m['instructions'])}</td></tr>" for m in c["medications"]) + "</tbody></table>") if c["medications"] else "<p>None.</p>"
    staging = "".join(_staging_table(d) for d in c.get("staging") or [])
    response = _response_table(c.get("response") or {})
    fu = c.get("follow_up") or {}
    follow = ("<p>No follow-up visit booked.</p>" if fu.get("none") else
              f"<p><strong>{_e(date.fromisoformat(fu['date']).strftime('%d %b %Y') if fu.get('date') else '—')}</strong>"
              + (f" with {_e(fu['with'])}" if fu.get("with") else "") + "</p>" + (f"<p>{_e(fu['instructions'])}</p>" if fu.get("instructions") else ""))
    age = f"{p['age']} y" if p.get("age") is not None else ""
    signed = (f"Reviewed and finalised by {_e(view['finalised_by'])} on {_e(view['finalised_at'][:10])} · version {view['version']}" if not draft
              else "DRAFT — not reviewed. Not for clinical use or sharing.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(c['title'])} · {_e(p['first_name'])} {_e(p['last_name'])}</title>
<style>
:root {{ --ink:#14212b; --muted:#5b6b78; --line:#d9e1e7; --accent:#0e6e6a; }}
* {{ box-sizing:border-box }} body {{ margin:0; font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); background:#f4f6f8 }}
main {{ max-width:820px; margin:24px auto; background:#fff; padding:36px 40px; border:1px solid var(--line); border-radius:10px; position:relative }}
header {{ display:flex; justify-content:space-between; gap:16px; border-bottom:2px solid var(--accent); padding-bottom:12px; margin-bottom:16px }}
h1 {{ font-size:22px; margin:0 0 4px }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--accent); margin:20px 0 6px }}
.meta {{ color:var(--muted); font-size:13px }} table {{ width:100%; border-collapse:collapse; font-size:13px }} th, td {{ text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:top }}
th {{ color:var(--muted); font-weight:600 }} ul {{ margin:4px 0 0 18px; padding:0 }} p {{ margin:4px 0 }}
h3 {{ font-size:12px; margin:12px 0 2px; color:var(--muted) }} ul.act {{ list-style:none; margin:0; padding:0 }} ul.act li {{ padding:4px 0; border-bottom:1px dashed var(--line); font-size:13px }}
ul.act li span {{ color:#3c4b55 }}
.grid {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:6px 16px; font-size:13px }} .grid b {{ display:block; color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase }}
footer {{ margin-top:28px; padding-top:10px; border-top:1px solid var(--line); color:var(--muted); font-size:12px }}
.draft {{ position:absolute; top:12px; right:16px; color:#a33; border:2px solid #a33; padding:2px 10px; font-weight:700; letter-spacing:.1em; border-radius:4px }}
.actions {{ max-width:820px; margin:16px auto 0; text-align:right }} .actions button {{ font:600 14px system-ui; padding:8px 16px; border-radius:8px; border:1px solid var(--accent); background:var(--accent); color:#fff; cursor:pointer }}
@media (max-width:640px) {{ main {{ margin:0; border-radius:0; padding:20px 16px }} .grid {{ grid-template-columns:repeat(2, 1fr) }} header {{ flex-direction:column }} }}
@media print {{ body {{ background:#fff }} main {{ border:0; margin:0; padding:0; max-width:none }} .actions {{ display:none }} }}
</style></head><body>
<div class="actions"><button onclick="window.print()">Print / save as PDF</button></div>
<main>{'<span class="draft">DRAFT</span>' if draft else ''}
<header><div><h1>{_e(c['title'])}</h1><div class="meta">Thravi OncoCare · {_e(c.get('consultant') or '')}</div></div>
<div class="meta">Period: {_e(view['period_from'] or '—')} to {_e(view['period_to'] or '—')}</div></header>
<div class="grid"><div><b>Patient</b>{_e(p['first_name'])} {_e(p['last_name'])}</div><div><b>MRN</b>{_e(p['medical_record_number'])}</div>
<div><b>Age / sex</b>{_e(age)} {_e(p.get('sex') or '')}</div><div><b>Mobile</b>{_e(p.get('phone') or '—')}</div></div>
{section('Diagnosis', f'<p>{_e(c["diagnosis"])}</p>')}
{section('Diagnosis & staging', staging)}
{section('Tumour response', response)}
{section('Surgery', surg)}
{section('Chemotherapy', chemo)}
{section('Radiotherapy', radio)}
{section('Course during treatment', f'<p>{_e(_strip_citations((c.get("course") or {}).get("text", "")))}</p>')}
{section('Activity during this period (date order)', activity)}
{section('Toxicities', tox)}
{section('Complaints reported between visits', complaints)}
{section('Latest investigations', labs)}
{section('Reports on file', docs)}
{section('Condition at discharge', f'<p>{_e(c.get("condition_at_discharge") or "—")}</p>')}
{section('Medicines', meds)}
{section('Advice', f'<p>{_e(c.get("advice") or "—")}</p>')}
{section('Follow-up', follow)}
<footer>{signed}<br>If you feel very unwell — fever 38 °C or more, bleeding, breathlessness, or you cannot eat or drink — contact your care team or call 112.</footer>
</main></body></html>"""

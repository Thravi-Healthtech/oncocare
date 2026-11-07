"""Stand-alone prescriptions: medicines a doctor prescribes at any visit (consultation, chemo day, between cycles),
independent of chemotherapy plans and discharge summaries.

A prescription is signed when it is saved (the signed-in clinician is the prescriber) and is then read-only.
A wrong prescription is cancelled with a reason and a new one written, so what the patient received stays on record.
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import date
from typing import Any

from . import discharge, records
from .database import row_or_none, rows, transaction, utc_now
from .errors import ApiError

CONTEXTS = {"consultation": "Consultation", "chemo_day": "Chemotherapy day", "between_cycles": "Between cycles",
            "radiotherapy": "During radiotherapy", "post_op": "After surgery", "follow_up": "Follow-up visit", "other": "Other"}


def medicines_text(meds: list[dict[str, Any]]) -> str:
    out = []
    for m in meds:
        line = " ".join(p for p in (m["name"], m.get("dose") or "") if p).strip()
        detail = ", ".join(x for x in (m.get("route"), m.get("frequency"), f"for {m['duration']}" if m.get("duration") else "") if x)
        out.append(f"• {line}" + (f" — {detail}" if detail else "") + (f" ({m['instructions']})" if m.get("instructions") else ""))
    return "\n".join(out)


def _view(row: dict[str, Any]) -> dict[str, Any]:
    items = json.loads(row.pop("items_json") or "[]")
    return row | {"items": items, "context_label": CONTEXTS.get(row["context"] or "", row["context"] or ""), "summary": medicines_text(items)}


def _load(connection: sqlite3.Connection, rx_id: int) -> dict[str, Any]:
    row = row_or_none(connection, "SELECT * FROM prescriptions WHERE id = ?", (rx_id,))
    if not row:
        raise ApiError(404, "Prescription not found")
    return row


def create(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    items = discharge._medications(payload.get("items") or [])
    if not items:
        raise ApiError(422, "Add at least one medicine", {"fields": ["items"]})
    try:
        rx_date = date.fromisoformat(str(payload.get("rx_date") or date.today().isoformat())[:10])
    except ValueError:
        raise ApiError(422, "Date must be YYYY-MM-DD", {"fields": ["rx_date"]}) from None
    if rx_date > date.today():
        raise ApiError(422, "A prescription cannot be dated in the future", {"fields": ["rx_date"]})
    context = payload.get("context") or "consultation"
    if context not in CONTEXTS:
        raise ApiError(422, "Unknown prescription context")
    with transaction(db) as connection:
        if not row_or_none(connection, "SELECT id FROM patients WHERE id = ?", (patient_id,)):
            raise ApiError(404, "Patient not found")
        plan_id = payload.get("treatment_plan_id") or None
        if plan_id and not row_or_none(connection, "SELECT id FROM treatment_plans WHERE id = ? AND patient_id = ?", (plan_id, patient_id)):
            raise ApiError(422, "That treatment plan belongs to another patient")
        now = utc_now()
        rx_id = connection.execute(
            """INSERT INTO prescriptions (patient_id, rx_date, context, indication, items_json, advice, consult_notes, transcript, source,
                   treatment_plan_id, prescribed_by, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (patient_id, rx_date.isoformat(), context, records._text(payload.get("indication"), 300), json.dumps(items, ensure_ascii=False),
             records._text(payload.get("advice"), 1000), records._text(payload.get("consult_notes"), 4000),
             str(payload.get("transcript") or "").strip()[:30000] or None,
             "conversation" if str(payload.get("transcript") or "").strip() else "manual", plan_id, actor, now, now)).lastrowid
        return _view(_load(connection, rx_id))


def list_for_patient(db: str, patient_id: int) -> list[dict[str, Any]]:
    with transaction(db) as connection:
        return [_view(r) for r in rows(connection, "SELECT * FROM prescriptions WHERE patient_id = ? ORDER BY rx_date DESC, id DESC", (patient_id,))]


def get(db: str, rx_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        return _view(_load(connection, rx_id))


def cancel(db: str, rx_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    reason = records._text(payload.get("reason"), 300)
    if not reason:
        raise ApiError(422, "Give the reason for cancelling", {"fields": ["reason"]})
    with transaction(db) as connection:
        row = _load(connection, rx_id)
        if row["status"] != "active":
            raise ApiError(409, "This prescription is already cancelled")
        connection.execute("UPDATE prescriptions SET status = 'cancelled', cancel_reason = ?, cancelled_by = ?, updated_at = ? WHERE id = ?",
                           (reason, actor, utc_now(), rx_id))
        return _view(_load(connection, rx_id))


def latest_active(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any] | None:
    row = row_or_none(connection, "SELECT * FROM prescriptions WHERE patient_id = ? AND status = 'active' ORDER BY rx_date DESC, id DESC LIMIT 1", (patient_id,))
    return _view(row) if row else None


def for_patient(db: str, patient_id: int) -> list[dict[str, Any]]:
    """Patient portal: active prescriptions, newest first, in plain fields."""
    with transaction(db) as connection:
        # Consultation notes and the transcript are for the clinician's record only, never the patient view.
        return [{k: v for k, v in _view(r).items() if k in ("id", "rx_date", "context_label", "indication", "items", "advice", "prescribed_by")}
                for r in rows(connection, "SELECT * FROM prescriptions WHERE patient_id = ? AND status = 'active' ORDER BY rx_date DESC, id DESC", (patient_id,))]


def print_html(db: str, rx_id: int, patient_id: int | None = None) -> str:
    with transaction(db) as connection:
        row = _load(connection, rx_id)
        if patient_id is not None and (row["patient_id"] != patient_id or row["status"] != "active"):
            raise ApiError(404, "Prescription not found")
        rx = _view(row)
        p = row_or_none(connection, "SELECT * FROM patients WHERE id = ?", (row["patient_id"],))
    from . import whatsapp
    e = html.escape
    age = ""
    if p.get("date_of_birth"):
        try:
            born = date.fromisoformat(p["date_of_birth"])
            today = date.today()
            age = f"{today.year - born.year - ((today.month, today.day) < (born.month, born.day))} y"
        except ValueError:
            age = ""
    body = "".join(f"<tr><td><b>{e(m['name'])}</b></td><td>{e(m['dose'])}</td><td>{e(m.get('route', ''))}</td><td>{e(m['frequency'])}</td>"
                   f"<td>{e(m['duration'])}</td><td>{e(m['instructions'])}</td></tr>" for m in rx["items"])
    cancelled = rx["status"] == "cancelled"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prescription · {e(p['first_name'])} {e(p['last_name'])} · {e(rx['rx_date'])}</title>
<style>
:root {{ --ink:#14212b; --muted:#5b6b78; --line:#d9e1e7; --accent:#0e6e6a; }}
* {{ box-sizing:border-box }} body {{ margin:0; font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); background:#f4f6f8 }}
main {{ max-width:820px; margin:24px auto; background:#fff; padding:36px 40px; border:1px solid var(--line); border-radius:10px; position:relative }}
header {{ display:flex; justify-content:space-between; gap:16px; border-bottom:2px solid var(--accent); padding-bottom:12px; margin-bottom:16px }}
h1 {{ font-size:22px; margin:0 0 4px }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--accent); margin:20px 0 6px }}
.meta {{ color:var(--muted); font-size:13px }} table {{ width:100%; border-collapse:collapse; font-size:13px }}
th, td {{ text-align:left; padding:6px; border-bottom:1px solid var(--line); vertical-align:top }} th {{ color:var(--muted); font-weight:600 }}
.grid {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:6px 16px; font-size:13px }} .grid b {{ display:block; color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase }}
.rx {{ font:700 28px Georgia, serif; color:var(--accent); margin:16px 0 4px }} p {{ margin:4px 0 }}
footer {{ margin-top:36px; padding-top:10px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; display:flex; justify-content:space-between; gap:16px }}
.void {{ position:absolute; top:12px; right:16px; color:#a33; border:2px solid #a33; padding:2px 10px; font-weight:700; letter-spacing:.1em; border-radius:4px }}
.actions {{ max-width:820px; margin:16px auto 0; text-align:right }} .actions button {{ font:600 14px system-ui; padding:8px 16px; border-radius:8px; border:1px solid var(--accent); background:var(--accent); color:#fff; cursor:pointer }}
@media (max-width:640px) {{ main {{ margin:0; border-radius:0; padding:20px 16px }} .grid {{ grid-template-columns:repeat(2, 1fr) }} header {{ flex-direction:column }} table {{ font-size:12px }} }}
@media print {{ body {{ background:#fff }} main {{ border:0; margin:0; padding:0; max-width:none }} .actions {{ display:none }} }}
</style></head><body>
<div class="actions"><button onclick="window.print()">Print / save as PDF</button></div>
<main>{'<span class="void">CANCELLED</span>' if cancelled else ''}
<header><div><h1>Prescription</h1><div class="meta">{e(whatsapp.clinic())}{' · ' + e(whatsapp.care_phone()) if whatsapp.care_phone() else ''}</div></div>
<div class="meta">{e(date.fromisoformat(rx['rx_date']).strftime('%a %d %b %Y'))}<br>{e(rx['context_label'])}</div></header>
<div class="grid"><div><b>Patient</b>{e(p['first_name'])} {e(p['last_name'])}</div><div><b>MRN</b>{e(p['medical_record_number'])}</div>
<div><b>Age / sex</b>{e(age)} {e(p.get('sex') or '')}</div><div><b>Mobile</b>{e(p.get('phone') or '—')}</div></div>
{f"<h2>Reason</h2><p>{e(rx['indication'])}</p>" if rx['indication'] else ''}
<div class="rx">℞</div>
<table><thead><tr><th>Medicine</th><th>Dose</th><th>Route</th><th>How often</th><th>For how long</th><th>Instructions</th></tr></thead><tbody>{body}</tbody></table>
{f"<h2>Advice</h2><p>{e(rx['advice'])}</p>" if rx['advice'] else ''}
{f"<h2>Cancelled</h2><p>{e(rx['cancel_reason'] or '')} — {e(rx['cancelled_by'] or '')}</p>" if cancelled else ''}
<footer><span>Prescribed by <b>{e(rx['prescribed_by'])}</b> on {e(rx['created_at'][:10])}</span><span>In an emergency call 112.</span></footer>
</main></body></html>"""

"""Routes for the radiation oncology, records/summary, discharge summary and WhatsApp modules.

`dispatch` returns (payload, status), a RawResponse, or None when the path is not handled here."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from . import radiotherapy as rt
from . import surgery as sx
from .database import transaction
from .errors import ApiError


@dataclass
class RawResponse:
    content: bytes
    content_type: str
    filename: str | None = None


@dataclass
class Ctx:
    db: str
    body: Callable[..., dict[str, Any]]
    actor: str
    upload_dir: Any
    query: dict[str, list[str]]

    def q(self, key: str, default: str | None = None) -> str | None:
        return (self.query.get(key) or [default])[0]


def _ok(data: Any, status: int = 200) -> tuple[dict[str, Any], int]:
    return {"data": data}, status


def _routes(ctx: Ctx) -> list[tuple[str, dict[str, Callable[..., Any]]]]:
    from . import records, discharge, whatsapp  # noqa: F401  (imported lazily to keep start-up light)
    db, actor = ctx.db, ctx.actor
    return [
        # Radiation oncology
        (r"/api/rt/options", {"GET": lambda: _ok(rt.options())}),
        (r"/api/patients/(\d+)/rt-courses", {
            "GET": lambda pid: _ok(_with(db, lambda c: rt.list_for_patient(c, pid))),
            "POST": lambda pid: _ok(rt.create_course(db, pid, ctx.body(), actor), 201)}),
        (r"/api/rt-courses/(\d+)", {
            "GET": lambda cid: _ok(rt.workspace(db, cid)),
            "PATCH": lambda cid: _ok(_rt_notify(ctx, rt.update_course, cid)),
            "DELETE": lambda cid: _ok(rt.delete_course(db, cid))}),
        (r"/api/rt-courses/(\d+)/approve", {"POST": lambda cid: _ok(_rt_notify(ctx, rt.approve, cid))}),
        (r"/api/rt-courses/(\d+)/fractions", {"POST": lambda cid: _ok(rt.add_fraction(db, cid, ctx.body(), actor), 201)}),
        (r"/api/rt-fractions/(\d+)", {"DELETE": lambda fid: _ok(rt.delete_fraction(db, fid, actor))}),
        (r"/api/rt-courses/(\d+)/breaks", {"POST": lambda cid: _ok(rt.record_break(db, cid, ctx.body(), actor), 201)}),
        (r"/api/rt-courses/(\d+)/reviews", {"POST": lambda cid: _ok(rt.save_review(db, cid, ctx.body(), actor), 201)}),
        (r"/api/rt-courses/(\d+)/reviews/(\d+)", {
            "PATCH": lambda cid, rid: _ok(rt.save_review(db, cid, ctx.body(), actor, rid)),
            "DELETE": lambda cid, rid: _ok(rt.delete_review(db, rid, actor))}),
        (r"/api/rt-courses/(\d+)/complete", {"POST": lambda cid: _ok(rt.complete(db, cid, ctx.body(), actor))}),
        (r"/api/rt-courses/(\d+)/reopen", {"POST": lambda cid: _ok(rt.reopen(db, cid, ctx.body(), actor))}),
        (r"/api/rt-phases/(\d+)", {"PATCH": lambda phid: _ok(rt.update_phase(db, phid, ctx.body(), actor))}),
        # Surgical oncology
        (r"/api/surgery/options", {"GET": lambda: _ok(sx.options())}),
        (r"/api/patients/(\d+)/surgeries", {
            "GET": lambda pid: _ok(_with(db, lambda c: sx.list_for_patient(c, pid))),
            "POST": lambda pid: _ok(_sx_notify(ctx, None, pid), 201)}),
        (r"/api/surgeries/(\d+)", {
            "GET": lambda cid: _ok(sx.workspace(db, cid)),
            "PATCH": lambda cid: _ok(_sx_notify(ctx, cid)),
            "DELETE": lambda cid: _ok(sx.delete(db, cid))}),
        (r"/api/surgeries/(\d+)/operation", {"POST": lambda cid: _ok(sx.save_operation(db, cid, ctx.body(), actor))}),
        (r"/api/surgeries/(\d+)/events", {"POST": lambda cid: _ok(sx.add_event(db, cid, ctx.body(), actor), 201)}),
        (r"/api/surgery-events/(\d+)", {"DELETE": lambda eid: _ok(sx.delete_event(db, eid))}),
        (r"/api/surgeries/(\d+)/pathology", {"POST": lambda cid: _ok(sx.save_pathology(db, cid, ctx.body(), actor))}),
        (r"/api/surgeries/(\d+)/discharge", {"POST": lambda cid: _ok(_sx_discharge(ctx, cid))}),
        # Clinical notes, documents, running summary
        (r"/api/patients/(\d+)/notes", {
            "GET": lambda pid: _ok(records.list_notes(db, pid)),
            "POST": lambda pid: _ok(records.create_note(db, pid, ctx.body(), actor), 201)}),
        (r"/api/notes/(\d+)", {
            "PATCH": lambda nid: _ok(records.update_note(db, nid, ctx.body(), actor)),
            "DELETE": lambda nid: _ok(records.delete_note(db, nid))}),
        (r"/api/patients/(\d+)/documents", {
            "GET": lambda pid: _ok(records.list_documents(db, pid)),
            "POST": lambda pid: _ok(records.upload_document(db, pid, ctx.body(15_000_000), actor, ctx.upload_dir), 201)}),
        (r"/api/documents/(\d+)", {
            "PATCH": lambda did: _ok(records.update_document(db, did, ctx.body())),
            "DELETE": lambda did: _ok(records.delete_document(db, did))}),
        (r"/api/documents/(\d+)/file", {"GET": lambda did: records.document_file(db, did, ctx.upload_dir)}),
        (r"/api/documents/(\d+)/summarise", {"POST": lambda did: _ok(records.summarise_document(db, did))}),
        (r"/api/patients/(\d+)/summary", {"GET": lambda pid: _ok(records.running_summary(db, pid))}),
        (r"/api/patients/(\d+)/summary/narrative", {"POST": lambda pid: _ok(records.generate_narrative(db, pid, actor))}),
        # Discharge / end-of-treatment summaries
        (r"/api/patients/(\d+)/discharge-summaries", {
            "GET": lambda pid: _ok(discharge.list_for_patient(db, pid)),
            "POST": lambda pid: _ok(discharge.create_draft(db, pid, ctx.body(), actor), 201)}),
        (r"/api/discharge-summaries/(\d+)", {
            "GET": lambda sid: _ok(discharge.get(db, sid)),
            "PATCH": lambda sid: _ok(discharge.update_draft(db, sid, ctx.body(), actor)),
            "DELETE": lambda sid: _ok(discharge.delete_draft(db, sid))}),
        (r"/api/discharge-summaries/(\d+)/regenerate", {"POST": lambda sid: _ok(discharge.regenerate(db, sid, ctx.body(), actor))}),
        (r"/api/discharge-summaries/(\d+)/finalise", {"POST": lambda sid: _ok(_finalise_notify(ctx, sid))}),
        (r"/api/discharge-summaries/(\d+)/revise", {"POST": lambda sid: _ok(discharge.revise(db, sid, actor), 201)}),
        (r"/api/discharge-summaries/(\d+)/print", {"GET": lambda sid: RawResponse(discharge.print_html(db, sid).encode(), "text/html; charset=utf-8")}),
        # WhatsApp
        (r"/api/whatsapp/status", {"GET": lambda: _ok(whatsapp.status())}),
        (r"/api/whatsapp/threads", {"GET": lambda: _ok(whatsapp.threads(db))}),
        (r"/api/patients/(\d+)/whatsapp", {"GET": lambda pid: _ok(whatsapp.patient_thread(db, pid))}),
        (r"/api/patients/(\d+)/whatsapp/consent", {"POST": lambda pid: _ok(whatsapp.set_consent(db, pid, ctx.body(), actor))}),
        (r"/api/patients/(\d+)/whatsapp/compose", {"POST": lambda pid: _ok(whatsapp.compose(db, pid, ctx.body(), actor), 201)}),
        (r"/api/whatsapp/messages/(\d+)", {
            "PATCH": lambda mid: _ok(whatsapp.edit_draft(db, mid, ctx.body())),
            "DELETE": lambda mid: _ok(whatsapp.cancel(db, mid, actor))}),
        (r"/api/whatsapp/messages/(\d+)/send", {"POST": lambda mid: _ok(whatsapp.approve_and_send(db, mid, ctx.body(), actor, ctx.upload_dir))}),
        (r"/api/whatsapp/messages/(\d+)/media", {"GET": lambda mid: whatsapp.media_file(db, mid, ctx.upload_dir)}),
        (r"/api/whatsapp/simulate", {"POST": lambda: _ok(whatsapp.simulate_inbound(db, ctx.body(15_000_000), ctx.upload_dir), 201)}),
        (r"/api/whatsapp/templates", {"GET": lambda: _ok(whatsapp.template_definitions())}),
    ]


def _rt_notify(ctx: Ctx, fn: Callable[..., dict[str, Any]], course_id: int) -> dict[str, Any]:
    """Approve / pause / resume a course; with notify_whatsapp the updated plan goes to the patient."""
    from . import whatsapp
    body = ctx.body()
    before = rt.workspace(ctx.db, course_id)["course"]["status"] if "status" in body else None
    data = fn(ctx.db, course_id, body, ctx.actor)
    changed = before is None or data["course"]["status"] != before
    if body.get("notify_whatsapp") and changed:
        data["whatsapp"] = [whatsapp.notify(ctx.db, data["patient"]["id"], "plan", ctx.actor)]
    return data


def _sx_notify(ctx: Ctx, case_id: int | None, patient_id: int | None = None) -> dict[str, Any]:
    """Create or update a surgical case; when the surgery date is set or changes and notify_whatsapp is ticked, the patient
    gets the updated plan and a reminder the day before admission."""
    from . import whatsapp
    body = ctx.body()
    before = sx.workspace(ctx.db, case_id)["case"] if case_id else None
    data = sx.create(ctx.db, patient_id, body, ctx.actor) if case_id is None else sx.update(ctx.db, case_id, body, ctx.actor)
    c = data["case"]
    date_changed = c["surgery_date"] and (before is None or before["surgery_date"] != c["surgery_date"] or before["status"] != c["status"])
    if body.get("notify_whatsapp") and (date_changed or c["status"] == "cancelled"):
        sent = [whatsapp.notify(ctx.db, c["patient_id"], "plan", ctx.actor)]
        if c["status"] == "scheduled":
            visit = c["admission_date"] or c["surgery_date"]
            fasting = (c["preop"] or {}).get("fasting_from")
            sent.append(whatsapp.notify(ctx.db, c["patient_id"], "reminder", ctx.actor, visit_date=visit, visit_time="07:00",
                                        purpose=f"Admission for surgery ({c['procedure_planned']})" + (f". Nothing to eat or drink from {fasting}" if fasting else "")))
        data["whatsapp"] = sent
    return data


def _sx_discharge(ctx: Ctx, case_id: int) -> dict[str, Any]:
    from . import whatsapp
    body = ctx.body()
    data = sx.discharge(ctx.db, case_id, body, ctx.actor)
    fu = (data["case"]["discharge"] or {}).get("follow_up_date")
    if body.get("notify_whatsapp") and fu:
        data["whatsapp"] = [whatsapp.notify(ctx.db, data["case"]["patient_id"], "reminder", ctx.actor, visit_date=fu, visit_time="10:00",
                                            purpose="Review after surgery (wound check" + (", drain" if (data["case"]["discharge"] or {}).get("drains_at_discharge") else "") + ")")]
    return data


def _finalise_notify(ctx: Ctx, summary_id: int) -> dict[str, Any]:
    """Finalising sends the summary, the prescription and a follow-up reminder when notify_whatsapp is set."""
    from . import discharge, whatsapp
    body = ctx.body()
    data = discharge.finalise(ctx.db, summary_id, body, ctx.actor)
    if body.get("notify_whatsapp"):
        pid, content = data["patient_id"], data["content"]
        sent = [whatsapp.notify(ctx.db, pid, "discharge", ctx.actor, summary_id=summary_id)]
        if content.get("medications"):
            sent.append(whatsapp.notify(ctx.db, pid, "prescription", ctx.actor, summary_id=summary_id))
        fu = content.get("follow_up") or {}
        if fu.get("date"):
            sent.append(whatsapp.notify(ctx.db, pid, "reminder", ctx.actor, visit_date=fu["date"], visit_time="10:00",
                                        purpose=f"Follow-up{' with ' + fu['with'] if fu.get('with') else ''}"))
        data["whatsapp"] = sent
    return data


def _with(db: str, fn: Callable[[Any], Any]) -> Any:
    with transaction(db) as connection:
        return fn(connection)


def dispatch(ctx: Ctx, method: str, path: str) -> Any:
    if not re.match(r"/api/(rt|rt-|surger|patients/\d+/(rt-courses|surgeries|notes|documents|summary|discharge|whatsapp)|notes/|documents/|discharge-summaries/|whatsapp/)", path):
        return None
    for pattern, handlers in _routes(ctx):
        match = re.fullmatch(pattern, path)
        if not match:
            continue
        handler = handlers.get(method)
        if handler is None:
            raise ApiError(405, "Method not allowed")
        return handler(*(int(g) for g in match.groups()))
    return None

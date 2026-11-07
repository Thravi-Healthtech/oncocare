"""Consultation scribe: doctor-patient conversation -> transcript (Sarvam AI) -> draft prescription (Claude Haiku).

The browser records the consultation in short segments (Sarvam's real-time speech-to-text takes clips of up to
~30 seconds) and sends each one here as it is recorded, so the transcript grows while the doctor talks. The audio
is transcribed and discarded; only the text is kept. When Sarvam is not configured the browser's own speech
recognition is used and the doctor can always type or correct the transcript.

The draft is never saved by itself: it fills the prescription form, and the doctor checks it, edits it and signs it.
Haiku is told to copy only what was said; anything it is unsure of is listed for the doctor instead of guessed.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any

from . import extraction, prescriptions, speech
from .database import row_or_none, rows, transaction
from .errors import ApiError

DEFAULT_MODEL = "claude-haiku-5-5"
FALLBACK_MODELS = ("claude-haiku-4-5-20251001",)
MAX_SEGMENT_BYTES = 4_000_000
MAX_TRANSCRIPT = 30_000

SYSTEM = """You are a careful medical scribe in an oncology clinic in India. You receive the transcript of a conversation
between a doctor and a patient (it may mix English, Hindi and Telugu, and speakers are not labelled) and turn what
the DOCTOR prescribed into a structured prescription draft that the doctor will check before signing.

Rules:
- Only include medicines the doctor clearly decided to prescribe or continue in this conversation. Do not include
  chemotherapy drugs given in the day-care unit, medicines merely discussed, or medicines the doctor stopped.
- Copy dose, route, frequency, duration and instructions exactly as said. If any of them was not said, leave it
  empty. Never invent or "complete" a dose. Translate instructions into plain English.
- Use generic names where the speaker used a brand, keeping the brand in brackets, e.g. "Ondansetron (Emeset)".
- Frequencies in plain words ("twice a day", "every 8 hours", "at night"); durations like "5 days", "until next cycle".
- If something is ambiguous (unclear dose, two possible drugs, a number that may have been misheard), still list the
  row with what is certain and add a short note to "uncertain" for the doctor.
- "notes" is a brief clinical note of the consultation (symptoms reported, findings, plan) in 2-5 short sentences,
  written for the doctor's record, without anything that was not said.
- "context" is one of: consultation, chemo_day, between_cycles, radiotherapy, post_op, follow_up, other.

Reply with ONLY a JSON object, no other text:
{"context": "...", "indication": "short reason for the prescription or empty",
 "items": [{"name": "", "dose": "", "route": "", "frequency": "", "duration": "", "instructions": ""}],
 "advice": "advice the doctor gave the patient, or empty", "notes": "...", "uncertain": ["..."]}"""


def status() -> dict[str, Any]:
    ai = extraction.ai_status()
    return {"sarvam": speech.status()["enabled"], "ai": ai["enabled"], "model": os.environ.get("ONCOCARE_SCRIBE_MODEL", DEFAULT_MODEL) if ai["enabled"] else None,
            "segment_seconds": 25}


def transcribe_segment(payload: dict[str, Any]) -> dict[str, Any]:
    if not speech.status()["enabled"]:
        raise ApiError(503, "Sarvam speech-to-text is not configured (SARVAM_API_KEY). Use the browser transcription or type the notes.")
    try:
        data = base64.b64decode(str(payload.get("audio_base64") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, "The recording could not be read") from None
    if not data:
        raise ApiError(422, "The recording is empty")
    if len(data) > MAX_SEGMENT_BYTES:
        raise ApiError(413, "The recording segment is too long")
    content_type = str(payload.get("audio_type") or "audio/webm").split(";")[0]
    try:
        heard = speech.transcribe(data, content_type)
    except RuntimeError as error:
        raise ApiError(502, f"Transcription failed: {str(error)[:200]}") from None
    return {"text": heard["text"], "lang": heard.get("lang"), "seq": payload.get("seq")}


def _patient_context(connection: Any, patient_id: int) -> str:
    patient = row_or_none(connection, "SELECT first_name, diagnosis, date_of_birth, sex FROM patients WHERE id = ?", (patient_id,))
    if not patient:
        raise ApiError(404, "Patient not found")
    lines = [f"Diagnosis: {patient['diagnosis'] or 'not recorded'}"]
    for plan in rows(connection, "SELECT regimen_name, status FROM treatment_plans WHERE patient_id = ? ORDER BY start_date DESC LIMIT 3", (patient_id,)):
        lines.append(f"Chemotherapy plan: {plan['regimen_name']} ({plan['status']})")
    for course in rows(connection, "SELECT site, status FROM rt_courses WHERE patient_id = ? ORDER BY id DESC LIMIT 2", (patient_id,)):
        lines.append(f"Radiotherapy: {course['site']} ({course['status']})")
    rx = prescriptions.latest_active(connection, patient_id)
    if rx:
        lines.append(f"Previous prescription ({rx['rx_date']}):\n{prescriptions.medicines_text(rx['items'])}")
    return "\n".join(lines)


def _ask(api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    models = [body["model"], *[m for m in FALLBACK_MODELS if m != body["model"]]]
    last: Exception | None = None
    for model in models:
        try:
            reply = extraction._anthropic_post(api_key, {**body, "model": model}, 60)
            return reply | {"_model": model}
        except RuntimeError as error:  # unknown / unavailable model: try the next Haiku
            last = error
            if not re.search(r"HTTP (400|404)", str(error)) or "model" not in str(error).lower():
                raise
    raise last or RuntimeError("No model available")


def draft_prescription(db: str, patient_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    transcript = str(payload.get("transcript") or "").strip()[:MAX_TRANSCRIPT]
    if len(transcript) < 20:
        raise ApiError(422, "The conversation is too short to draft a prescription", {"fields": ["transcript"]})
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not extraction.ai_status()["enabled"]:
        raise ApiError(503, "AI is off (no Anthropic key), so the conversation cannot be turned into a prescription. Fill the form by hand.")
    with transaction(db) as connection:
        context_text = _patient_context(connection, patient_id)
    prompt = (f"What the record already says about this patient (for context only — do not copy it into the prescription):\n{context_text}\n\n"
              f"Consultation transcript:\n<<<\n{transcript}\n>>>")
    try:
        reply = _ask(api_key, {"model": os.environ.get("ONCOCARE_SCRIBE_MODEL", DEFAULT_MODEL), "max_tokens": 1500, "system": SYSTEM,
                               "messages": [{"role": "user", "content": prompt}]})
    except RuntimeError as error:
        raise ApiError(502, f"Could not draft the prescription: {str(error)[:200]}") from None
    raw = "".join(part.get("text", "") for part in reply.get("content", []))
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        parsed = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    items = []
    for item in parsed.get("items") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            items.append({k: str(item.get(k) or "").strip()[:300 if k == "instructions" else 120]
                          for k in ("name", "dose", "route", "frequency", "duration", "instructions")})
    context = parsed.get("context") if parsed.get("context") in prescriptions.CONTEXTS else "consultation"
    return {"context": context, "indication": str(parsed.get("indication") or "")[:300], "items": items[:25],
            "advice": str(parsed.get("advice") or "")[:1000], "notes": str(parsed.get("notes") or "")[:2000],
            "uncertain": [str(u)[:200] for u in (parsed.get("uncertain") or []) if str(u).strip()][:10],
            "model": reply.get("_model"), "empty": not items}

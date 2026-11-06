"""Guarded WhatsApp agent.

The agent reads what the patient wrote or said (any language), decides what to do, and drafts a short reply:
- complaint     -> the message is registered for the care team (patient report, AI triage as in the portal)
- checkin       -> the fixed CTCAE-based check-in questions start for the symptoms it found
- question      -> answered only from the record facts given to it (schedule, sessions, medicines as prescribed, next visit)
- appointment   -> a reschedule / appointment request is registered for the care team
- greeting/other-> a short reply and the menu

Guard rails (enforced in code, not only in the prompt):
- Red-flag rules run before the agent; the emergency text is fixed and always sent.
- The agent never diagnoses, never says a symptom is normal or not serious, never gives or changes medicine advice;
  anything clinical becomes a complaint or a check-in for the care team.
- Replies are checked: medicine doses must appear in the record facts, advice phrases are blocked, length is limited.
  A reply that fails is replaced with a fixed hand-off text. Every decision is stored with the message for audit.
- Without AI (no key, or an API error) the rule-based router in whatsapp.py is used.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import extraction, questionnaire

INTENTS = ("complaint", "checkin", "question", "appointment", "greeting", "other")

HANDOFF = {
    "en": "Thank you. I have passed your message to your care team; they will reply here.",
    "hi": "धन्यवाद। मैंने आपका संदेश आपकी देखभाल टीम को भेज दिया है; वे यहीं जवाब देंगे।",
    "te": "ధన్యవాదాలు. మీ సందేశాన్ని మీ వైద్య బృందానికి పంపాను; వారు ఇక్కడే జవాబు ఇస్తారు.",
}
BLOCKED = [
    r"\byou should (take|stop|start|increase|reduce|apply)\b", r"\b(increase|reduce|double|skip|stop) (the |your )?(dose|tablet|medicine|medication)",
    r"\bstop taking\b", r"\b(it'?s|this is|that is) (normal|nothing|not serious|harmless)\b", r"\bnothing to worry\b", r"\bdon'?t worry\b",
    r"\bno need to (worry|see|come|call)\b", r"\byou (have|may have|probably have) (an? )?[a-z]+itis\b", r"\bdiagnos", r"\bprognosis\b",
    r"\bcancer (is|has) (spread|gone|cured)\b",
]
DOSE = re.compile(r"\b\d+(?:\.\d+)?\s?(mg|mcg|g|ml|units?|iu|tablets?)\b", re.I)


def available() -> bool:
    return extraction.ai_status()["enabled"]


def guard(reply: str, facts_text: str, lang: str) -> tuple[str, str]:
    """Return (text to send, guard result). Blocks advice phrases, doses not in the record, and over-long replies."""
    text = (reply or "").strip()
    if not text:
        return HANDOFF.get(lang, HANDOFF["en"]), "blocked: empty reply"
    if len(text) > 700:
        return HANDOFF.get(lang, HANDOFF["en"]), "blocked: too long"
    lower = text.lower()
    for pattern in BLOCKED:
        if re.search(pattern, lower):
            return HANDOFF.get(lang, HANDOFF["en"]), f"blocked: advice phrase ({pattern})"
    facts = facts_text.lower().replace(" ", "")
    for match in DOSE.finditer(text):
        if match.group(0).lower().replace(" ", "") not in facts:
            return HANDOFF.get(lang, HANDOFF["en"]), f"blocked: dose not in record ({match.group(0)})"
    if re.search(r"https?://", text) and "onco.thravi.in" not in lower and "/portal" not in lower:
        return HANDOFF.get(lang, HANDOFF["en"]), "blocked: unknown link"
    return text, "passed"


SYSTEM = """You are the WhatsApp assistant of {clinic}, an oncology clinic. You talk to a patient on cancer treatment.
You are not a doctor. Your job: understand the message, choose an action, and write ONE short, warm reply (max 60 words)
in the patient's language ({lang_name}; same script the patient used).

Actions:
- "complaint": any symptom, side effect, worry about their health, or a request for the doctor -> it is registered for the care team.
- "checkin": the patient wants to answer the symptom questions, or reports symptoms from this list (use these keys): {symptom_keys}.
  Prefer "checkin" when the symptoms clearly match keys; the fixed questions follow automatically.
- "question": a practical question you can answer ONLY from the RECORD below (next session/cycle date, sessions left,
  medicines exactly as prescribed, next visit, clinic phone). If the record does not contain the answer, use "complaint".
- "appointment": wants to reschedule, cannot come, or asks for an appointment.
- "greeting": hello / thanks / ok.
- "other": anything else.

Never: diagnose; say a symptom is normal, expected, minor or not serious; tell them not to worry; give, change or stop any
medicine or dose; give medical advice; promise a time for the doctor's reply; mention AI. For any symptom, say you have passed
it to the care team (complaint) or that you will ask a few questions (checkin). Quote medicines exactly as in the RECORD.
Emergency guidance is handled separately; do not add your own.

RECORD
{facts}

Return ONLY JSON: {{"intent": one of {intents}, "symptoms": [keys from the list, only for checkin], "reply": "<message to patient>",
"note_for_doctor": "<one line in English summarising what the patient wants>"}}"""


def decide(facts_text: str, history: list[dict[str, str]], message: str, lang: str, clinic: str) -> dict[str, Any]:
    """Ask Claude for {intent, symptoms, reply, note_for_doctor}. Raises on API failure."""
    status = extraction.ai_status()
    lang_name = {"en": "English", "hi": "Hindi", "te": "Telugu"}.get(lang, "the patient's language")
    keys = ", ".join(f"{k} ({v['label']['en']})" for k, v in questionnaire.SYMPTOMS.items())
    system = SYSTEM.format(clinic=clinic, lang_name=lang_name, symptom_keys=keys, facts=facts_text[:6000], intents=list(INTENTS))
    convo = "\n".join(f"{'Patient' if h['role'] == 'patient' else 'Clinic'}: {h['text'][:400]}" for h in history[-8:])
    prompt = (f"Recent conversation (oldest first):\n{convo or '(none)'}\n\nNew message from the patient:\n<<<{message[:2000]}>>>")
    reply = extraction._anthropic_post(extraction.os.environ["ANTHROPIC_API_KEY"],
                                       {"model": status["model"], "max_tokens": 600, "system": system,
                                        "messages": [{"role": "user", "content": prompt}]}, 45)
    raw = "".join(part.get("text", "") for part in reply.get("content", []))
    match = re.search(r"\{.*\}", raw, re.S)
    parsed = json.loads(match.group(0)) if match else {}
    intent = parsed.get("intent") if parsed.get("intent") in INTENTS else "complaint"
    symptoms = [s for s in parsed.get("symptoms") or [] if s in questionnaire.SYMPTOMS][:4] if intent == "checkin" else []
    return {"intent": intent, "symptoms": symptoms, "reply": str(parsed.get("reply") or "")[:1200],
            "note_for_doctor": str(parsed.get("note_for_doctor") or "")[:300], "model": status["model"]}

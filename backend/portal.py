"""Patient portal: access codes, sessions, read-only views and patient-reported symptoms.

Patients sign in with their registered phone number and (for now) a temporary development password;
OTP sign-in will replace the password.
They can view their plan and verified lab results and send symptom reports (typed or dictated).
Reports reach the clinician inbox. Red-flag symptoms are detected with fixed rules and the patient
is told to contact the care team immediately. AI (when configured) only suggests CTCAE terms for
clinician review; it never grades or replies to the patient.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import extraction, questionnaire
from .auth import dev_password_ok
from .database import normalise_phone, row_or_none, rows, transaction, utc_now
from .reference import CTCAE_TERMS, LAB_CATALOG, SYMPTOM_KEYWORDS, drugs_associated_with

SESSION_HOURS = 12
MAX_AUDIO_BYTES = 8_000_000
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-m4a": ".m4a"}

# Patient checklist -> CTCAE term (plain-language labels live in the portal UI).
CHECKLIST = {
    "nausea": "Nausea", "vomiting": "Vomiting", "diarrhoea": "Diarrhea", "constipation": "Constipation",
    "mouth_sores": "Mucositis oral", "tingling": "Peripheral sensory neuropathy", "hearing": "Hearing impaired",
    "ringing_ears": "Tinnitus", "tiredness": "Fatigue", "fever": "Fever", "appetite": "Anorexia", "rash": "Rash maculo-papular",
    "hair_loss": "Alopecia", "muscle_pain": "Myalgia", "joint_pain": "Arthralgia", "swelling": "Edema limbs",
    "taste": "Dysgeusia", "breathless": "Dyspnea", "hand_foot": "Palmar-plantar erythrodysesthesia syndrome", "bleeding": None, "urine_blood": "Cystitis noninfective", "other": None,
}
IMPACT_TEXT = {
    "none": "Does not limit daily activities",
    "some": "Limits some daily activities (e.g. cooking, shopping, work) — instrumental ADL",
    "selfcare": "Needs help with self-care (washing, dressing, eating) — self-care ADL",
}

RED_FLAG_RULES = [
    ("fever", r"\b(fever|feverish|temperature|chills|shivering|rigou?rs?)\b", "Fever or chills during chemotherapy"),
    ("bleeding", r"\b(bleed(ing)?|blood in (my )?(stool|vomit|urine|sputum)|black (stool|stools|poo)|bruis(e|es|ing) easily|nosebleeds?)\b", "Bleeding or unexplained bruising"),
    ("breathing", r"\b(short(ness)? of breath|breathless|can'?t breathe|cannot breathe|difficulty breathing|trouble breathing|chest pain|chest tightness)\b", "Breathing difficulty or chest pain"),
    ("fluids", r"\b(can'?t|cannot|unable to|not able to) (keep|hold) (anything|food|water|fluids?) down|\b(can'?t|cannot|unable to) (eat|drink)\b|no urine|not pass(ed|ing) urine", "Unable to eat, drink or pass urine"),
    ("vomiting", r"\b(vomit(ed|ing)?|throw(ing|n)? up)\b.{0,30}\b(many|several|all day|non.?stop|continuously|[5-9]|\d{2}) (times|episodes)?", "Repeated vomiting"),
    ("diarrhoea", r"\b(diarrh(o)?ea|loose (stools|motions))\b.{0,30}\b([6-9]|\d{2}) (times|episodes)|\bsevere diarrh", "Severe diarrhoea"),
    ("neuro", r"\b(confus(ed|ion)|faint(ed|ing)?|passed out|seizure|fits?|collapse(d)?|drowsy and can'?t wake)\b", "Confusion, fainting or seizure"),
    ("allergy", r"\b(swelling of (the )?(face|lips|tongue|throat)|throat (closing|swelling)|hives all over)\b", "Signs of a severe allergic reaction"),
]
# Common Indian-language words for the red flags above (stems, so inflected forms match). Deliberately broad:
# a false alarm costs a phone call, a miss can cost much more.
NATIVE_RED_FLAG_RULES = [
    ("fever", r"(बुखार|ज्वर|ठंड लग|कंपकंपी|జ్వరం|జ్వర|చలి|காய்ச்சல்|குளிர் நடுக்கம்|ಜ್ವರ|ಚಳಿ|പനി|വിറയൽ|ताप आला|थंडी|জ্বর|কাঁপুনি)", "Fever or chills during chemotherapy"),
    ("bleeding", r"(खून|रक्तस्राव|రక్తం|రక్తస్రావం|இரத்த|ரத்த|ರಕ್ತ|രക്ത|रक्त|রক্ত)", "Bleeding or unexplained bruising"),
    ("breathing", r"(सांस|साँस|सीने में दर्द|ఊపిరి|ఆయాసం|ఛాతీ నొప్పి|மூச்சு|நெஞ்சு வலி|ಉಸಿರ|ಎದೆ ನೋವು|ശ്വാസ|നെഞ്ചുവേദന|श्वास|छातीत|শ্বাস|বুকে ব্যথা)", "Breathing difficulty or chest pain"),
    ("neuro", r"(बेहोश|चक्कर आकर गिर|స్పృహ|மயக்க|ಪ್ರಜ್ಞೆ|ബോധം|বেহুঁশ|অজ্ঞান)", "Confusion, fainting or seizure"),
]
EMERGENCY_MESSAGE = ("Some of what you described can be serious during chemotherapy. Please call your care team now, "
                     "or go to the nearest emergency department. Do not wait for a reply in this portal.")


class PortalError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Access management (doctor side)
# ---------------------------------------------------------------------------

def access_status(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any]:
    patient = row_or_none(connection, "SELECT phone, active FROM patients WHERE id = ?", (patient_id,))
    row = row_or_none(connection, "SELECT enabled, last_login_at, locked_until FROM portal_access WHERE patient_id = ?", (patient_id,))
    enabled = bool(row["enabled"]) if row else True
    return {"enabled": enabled, "phone": patient["phone"] if patient else None,
            "can_sign_in": bool(enabled and patient and patient["phone"] and patient["active"]),
            "last_login_at": row["last_login_at"] if row else None, "locked_until": row["locked_until"] if row else None,
            "login_method": "phone + temporary password (OTP planned)"}


# ---------------------------------------------------------------------------
# Sessions (patient side)
# ---------------------------------------------------------------------------

def login(connection: sqlite3.Connection, phone: str, password: str) -> tuple[str, dict[str, Any]]:
    """Patient sign-in with registered phone number + temporary development password (OTP later)."""
    generic = PortalError(401, "Phone number or password is not correct.")
    number = normalise_phone(phone)
    patient = row_or_none(connection, "SELECT * FROM patients WHERE phone = ?", (number,)) if number else None
    if not patient:
        raise generic
    access = row_or_none(connection, "SELECT * FROM portal_access WHERE patient_id = ?", (patient["id"],))
    if (access and not access["enabled"]) or not patient["active"]:
        raise PortalError(403, "Portal access is turned off for this record. Please contact your care team.")
    if not access:
        connection.execute("INSERT INTO portal_access (patient_id, code_hash, salt, enabled, failed_attempts, created_at) VALUES (?, '', '', 1, 0, ?)",
                           (patient["id"], utc_now()))
        access = row_or_none(connection, "SELECT * FROM portal_access WHERE patient_id = ?", (patient["id"],))
    now = _now()
    # No lockout after failed attempts (removed for the demo); failures are still counted.
    if not dev_password_ok(password):
        connection.execute("UPDATE portal_access SET failed_attempts = failed_attempts + 1, locked_until = NULL WHERE patient_id = ?",
                           (patient["id"],))
        connection.commit()  # keep the attempt count even though the request fails
        raise generic
    token = secrets.token_urlsafe(32)
    connection.execute("UPDATE portal_access SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE patient_id = ?",
                       (now.isoformat(), patient["id"]))
    connection.execute("DELETE FROM portal_sessions WHERE expires_at < ?", (now.isoformat(),))
    connection.execute("INSERT INTO portal_sessions (token_hash, patient_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                       (_token_hash(token), patient["id"], now.isoformat(), (now + timedelta(hours=SESSION_HOURS)).isoformat()))
    return token, portal_me(connection, patient["id"])


def set_access(connection: sqlite3.Connection, patient_id: int, enabled: bool) -> None:
    connection.execute(
        """INSERT INTO portal_access (patient_id, code_hash, salt, enabled, failed_attempts, created_at) VALUES (?, '', '', ?, 0, ?)
           ON CONFLICT(patient_id) DO UPDATE SET enabled = excluded.enabled, failed_attempts = 0, locked_until = NULL""",
        (patient_id, 1 if enabled else 0, utc_now()))
    if not enabled:
        connection.execute("DELETE FROM portal_sessions WHERE patient_id = ?", (patient_id,))


def session_patient(connection: sqlite3.Connection, token: str | None) -> int | None:
    if not token:
        return None
    row = row_or_none(connection, "SELECT patient_id, expires_at FROM portal_sessions WHERE token_hash = ?", (_token_hash(token),))
    if not row or datetime.fromisoformat(row["expires_at"]) < _now():
        return None
    access = row_or_none(connection, "SELECT enabled FROM portal_access WHERE patient_id = ?", (row["patient_id"],))
    return row["patient_id"] if (access is None or access["enabled"]) else None


def logout(connection: sqlite3.Connection, token: str | None) -> None:
    if token:
        connection.execute("DELETE FROM portal_sessions WHERE token_hash = ?", (_token_hash(token),))


# ---------------------------------------------------------------------------
# Read-only views
# ---------------------------------------------------------------------------

PATIENT_STATUS = {"planned": "Scheduled", "given": "Given", "modified": "Given · dose adjusted", "delayed": "Rescheduled",
                  "held": "Not given", "discontinued": "Stopped", "cancelled": "Cancelled"}


def portal_me(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any]:
    p = row_or_none(connection, "SELECT id, medical_record_number, first_name, last_name, date_of_birth, diagnosis, phone FROM patients WHERE id = ?", (patient_id,))
    if not p:
        raise PortalError(404, "Record not found")
    return {**p, "care_team": "Dr. R. Khanna · Medical oncology"}


def portal_plans(connection: sqlite3.Connection, patient_id: int) -> list[dict[str, Any]]:
    plans = rows(connection, "SELECT id, regimen_name, intent, status, start_date, cycle_interval_days, planned_cycles FROM treatment_plans WHERE patient_id = ? ORDER BY start_date DESC, id DESC", (patient_id,))
    today = date.today().isoformat()
    for plan in plans:
        plan["medications"] = rows(connection, "SELECT drug_name, dose, dose_unit, route FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order, id", (plan["id"],))
        cycles = rows(connection, "SELECT id, cycle_number, planned_date, administered_date, status FROM treatment_cycles WHERE treatment_plan_id = ? ORDER BY cycle_number", (plan["id"],))
        for cycle in cycles:
            cycle["status_label"] = PATIENT_STATUS.get(cycle["status"], cycle["status"])
            cycle["medications"] = rows(connection, "SELECT drug_name, planned_dose, actual_dose, dose_unit, route, status FROM cycle_medications WHERE cycle_id = ? ORDER BY sort_order, id", (cycle["id"],))
            labs = row_or_none(connection, "SELECT COUNT(*) AS n FROM lab_investigations WHERE cycle_id = ? AND verification_status = 'verified'", (cycle["id"],))
            cycle["labs_available"] = labs["n"] > 0
        plan["cycles"] = cycles
        plan["next_cycle"] = next((c for c in cycles if c["status"] in ("planned", "delayed") and c["planned_date"] >= today), None) \
            or next((c for c in cycles if c["status"] in ("planned", "delayed")), None)
        plan["given"] = sum(1 for c in cycles if c["status"] in ("given", "modified"))
    return plans


def portal_labs(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any]:
    labs = rows(connection, """SELECT l.id, l.test_name, l.value, l.unit, l.ref_low, l.ref_high, l.collection_date, l.document_id, c.cycle_number
                               FROM lab_investigations l JOIN treatment_cycles c ON c.id = l.cycle_id
                               WHERE l.patient_id = ? AND l.verification_status = 'verified' ORDER BY l.collection_date, l.id""", (patient_id,))
    documents = rows(connection, """SELECT d.id, d.filename, d.content_type, d.recording_date, d.uploaded_at, c.cycle_number
                                    FROM lab_documents d LEFT JOIN treatment_cycles c ON c.id = d.cycle_id
                                    WHERE d.patient_id = ? AND EXISTS (SELECT 1 FROM lab_investigations l WHERE l.document_id = d.id AND l.verification_status = 'verified')
                                    ORDER BY COALESCE(d.recording_date, d.uploaded_at) DESC""", (patient_id,))
    order = list(LAB_CATALOG)
    # one value per test per day (the most recently verified), so re-uploaded reports don't duplicate rows
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for lab in labs:
        latest[(lab["collection_date"], lab["test_name"])] = lab
    labs = sorted(latest.values(), key=lambda l: (l["collection_date"], l["id"]))
    by_date: dict[str, list[dict[str, Any]]] = {}
    series: dict[str, list[dict[str, Any]]] = {}
    for lab in labs:
        flag = "low" if lab["ref_low"] is not None and lab["value"] < lab["ref_low"] else "high" if lab["ref_high"] is not None and lab["value"] > lab["ref_high"] else "normal"
        item = {**lab, "flag": flag, "panel": LAB_CATALOG.get(lab["test_name"], {}).get("panel", "Other")}
        by_date.setdefault(lab["collection_date"], []).append(item)
        series.setdefault(lab["test_name"], []).append({"date": lab["collection_date"], "value": lab["value"], "flag": flag, "cycle_number": lab["cycle_number"]})
    reports = []
    for day in sorted(by_date, reverse=True):
        items = sorted(by_date[day], key=lambda i: order.index(i["test_name"]) if i["test_name"] in order else 99)
        reports.append({"collection_date": day, "cycle_number": items[0]["cycle_number"], "values": items,
                        "outside_range": sum(1 for i in items if i["flag"] != "normal")})
    return {"reports": reports, "documents": documents,
            "series": [{"test_name": k, "unit": next((l["unit"] for l in labs if l["test_name"] == k), ""), "points": v}
                       for k, v in sorted(series.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)]}


def patient_document(connection: sqlite3.Connection, patient_id: int, document_id: int) -> dict[str, Any]:
    doc = row_or_none(connection, "SELECT * FROM lab_documents WHERE id = ? AND patient_id = ?", (document_id, patient_id))
    verified = doc and row_or_none(connection, "SELECT 1 FROM lab_investigations WHERE document_id = ? AND verification_status = 'verified'", (document_id,))
    if not doc or not verified:
        raise PortalError(404, "Report not found")
    return doc


# ---------------------------------------------------------------------------
# Symptom reports
# ---------------------------------------------------------------------------

def detect_red_flags(text: str, symptoms: list[str], temperature: float | None, impact: str | None) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    lower = (text or "").lower()
    seen = set()

    def add(code: str, label: str, why: str) -> None:
        if code not in seen:
            seen.add(code)
            flags.append({"code": code, "label": label, "why": why})
    if temperature is not None and temperature >= 38.0:
        add("fever", "Temperature 38.0 °C or higher", f"Patient-entered temperature {temperature:.1f} °C")
    for code, pattern, label in RED_FLAG_RULES + NATIVE_RED_FLAG_RULES:
        match = re.search(pattern, lower)
        if match:
            add(code, label, f"Text mentions “{match.group(0)[:60]}”")
    if "fever" in symptoms:
        add("fever", "Fever or chills during chemotherapy", "Selected fever in the checklist")
    if "bleeding" in symptoms:
        add("bleeding", "Bleeding or unexplained bruising", "Selected bleeding in the checklist")
    if "breathless" in symptoms:
        add("breathing", "Breathing difficulty or chest pain", "Selected breathlessness in the checklist")
    if impact == "selfcare":
        add("selfcare", "Needs help with self-care", "Patient reports needing help with washing, dressing or eating")
    return flags


# Stems in Hindi, Telugu, Tamil, Kannada, Malayalam, Marathi and Bengali for the commonest chemotherapy symptoms.
NATIVE_SYMPTOM_KEYWORDS: dict[str, list[str]] = {
    "Vomiting": ["उल्टी", "उलटी", "वमन", "వాంతు", "వాంతి", "வாந்தி", "ವಾಂತಿ", "ഛർദ്ദി", "ഛര്‍ദ്ദി", "বমি"],
    "Nausea": ["जी मिचल", "मतली", "मळमळ", "వికారం", "కడుపులో తిప్పు", "குமட்டல்", "ವಾಕರಿಕೆ", "ಓಕರಿಕೆ", "ഓക്കാനം", "বমি বমি ভাব"],
    "Diarrhea": ["दस्त", "जुलाब", "पतले दस्त", "విరేచనాలు", "విరోచనాలు", "వదులు మోషన్", "வயிற்றுப்போக்கு", "பேதி", "ಭೇದಿ", "ಅತಿಸಾರ", "വയറിളക്ക", "জুলাব", "পাতলা পায়খানা", "ডায়রিয়া"],
    "Constipation": ["कब्ज", "మలబద్ధకం", "மலச்சிக்கல்", "ಮಲಬದ್ಧತೆ", "മലബന്ധ", "কোষ্ঠকাঠিন্য"],
    "Fatigue": ["थकान", "कमजोरी", "थकवा", "అలసట", "నీరసం", "బలహీనత", "சோர்வு", "களைப்பு", "ಆಯಾಸ", "ಸುಸ್ತು", "ക്ഷീണ", "ক্লান্ত", "দুর্বল"],
    "Anorexia": ["भूख नहीं", "भूख कम", "भूक लागत नाही", "ఆకలి లేదు", "ఆకలి వేయడం లేదు", "ఆకలి తగ్గ", "பசி இல்லை", "பசியின்மை", "ಹಸಿವಿಲ್ಲ", "ಹಸಿವು ಇಲ್ಲ", "വിശപ്പില്ല", "ক্ষুধা নেই", "খিদে নেই"],
    "Peripheral sensory neuropathy": ["झुनझुनी", "सुन्न", "मुंग्या", "తిమ్మిర్లు", "తిమ్మిరి", "స్పర్శ తెలియ", "மரத்துப்போ", "கூச்ச", "ಜುಮ್ಮೆನ್ನು", "ಮರಗಟ್ಟು", "മരവിപ്പ്", "തരിപ്പ്", "অসাড়", "ঝিনঝিন"],
    "Mucositis oral": ["मुंह में छाले", "छाले", "నోటి పుండ్లు", "నోట్లో పుండ్లు", "வாய்ப்புண்", "ಬಾಯಿ ಹುಣ್ಣು", "വായ്പ്പുണ്ണ്", "মুখে ঘা"],
    "Fever": ["बुखार", "ज्वर", "జ్వరం", "காய்ச்சல்", "ಜ್ವರ", "പനി", "জ্বর"],
    "Alopecia": ["बाल झड़", "జుట్టు రాల", "முடி உதிர", "ಕೂದಲು ಉದುರ", "മുടി കൊഴി", "চুল পড়"],
    "Tinnitus": ["कान में आवाज", "చెవిలో శబ్దం", "చెవిలో హోరు", "காதில் சத்தம்", "ಕಿವಿಯಲ್ಲಿ ಶಬ್ದ", "ചെവിയിൽ മുഴക്ക", "কানে শব্দ"],
    "Hearing impaired": ["कम सुनाई", "వినికిడి తగ్గ", "సరిగా వినపడ", "காது கேட்கவில்லை", "ಕಿವಿ ಕೇಳ", "കേൾവി കുറ", "কম শুনতে"],
    "Myalgia": ["बदन दर्द", "मांसपेशियों में दर्द", "ఒళ్ళు నొప్పులు", "కండరాల నొప్పి", "உடல் வலி", "ಮೈ ಕೈ ನೋವು", "ശരീരവേദന", "গা ব্যথা"],
    "Edema limbs": ["सूजन", "వాపు", "வீக்கம்", "ಊತ", "നീര്", "ফোলা"],
}


def _keyword_candidates(text: str, symptoms: list[str], drugs: list[str]) -> list[dict[str, Any]]:
    lower = (text or "").lower()
    found: dict[str, dict[str, Any]] = {}
    for key in symptoms:
        term = CHECKLIST.get(key)
        if term:
            found.setdefault(term, {"term": term, "evidence": f"Checklist: {key.replace('_', ' ')}", "grade_suggestion": None})
    for term, words in SYMPTOM_KEYWORDS.items():
        words = words + NATIVE_SYMPTOM_KEYWORDS.get(term, [])
        hit = next((w for w in words if (re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", lower) if w.isascii() else w in lower)), None)
        if hit:
            found.setdefault(term, {"term": term, "evidence": f"Text mentions “{hit}”", "grade_suggestion": None})
    for item in found.values():
        item["associated_drugs"] = drugs_associated_with(item["term"], drugs)
        item["rationale"] = "Keyword match; grade not suggested."
    return sorted(found.values(), key=lambda c: (-len(c["associated_drugs"]), c["term"]))


KEY_LABS = ("ANC", "WBC", "Hemoglobin", "Platelets", "Creatinine", "eGFR", "Sodium", "Potassium", "Magnesium", "Calcium",
            "ALT", "AST", "Bilirubin", "Albumin")


def _age(dob: str | None, on: str) -> int | None:
    try:
        born, day = date.fromisoformat(dob or ""), date.fromisoformat(on)
    except ValueError:
        return None
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def patient_context(connection: sqlite3.Connection, patient_id: int, as_of: str | None = None, exclude_report: int | None = None) -> dict[str, Any]:
    """Structured clinical context for one patient as of a date: regimen, cycle timing, recorded toxicities,
    latest verified labs and earlier patient messages. Used by the AI triage and shown to the clinician."""
    from .analysis import grade_lab  # local import: analysis imports reference data only
    as_of = as_of or date.today().isoformat()
    patient = row_or_none(connection, "SELECT first_name, last_name, date_of_birth, sex, diagnosis FROM patients WHERE id = ?", (patient_id,)) or {}
    plan = row_or_none(connection, """SELECT id, regimen_name, intent, start_date FROM treatment_plans
                                      WHERE patient_id = ? AND status IN ('active', 'planned') ORDER BY id DESC LIMIT 1""", (patient_id,))
    regimen, last_given, last_due, next_cycle, total = None, None, None, None, 0
    if plan:
        meds = rows(connection, "SELECT drug_name, dose, dose_unit, route FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order, id", (plan["id"],))
        cycles = rows(connection, """SELECT id, cycle_number, planned_date, administered_date, status FROM treatment_cycles
                                     WHERE treatment_plan_id = ? ORDER BY cycle_number""", (plan["id"],))
        total = len(cycles)
        regimen = {"name": plan["regimen_name"], "intent": plan["intent"],
                   "drugs": [f"{m['drug_name']} {m['dose']:g} {m['dose_unit']}" + (f" {m['route']}" if m["route"] else "") for m in meds]}
        for c in cycles:
            when = c["administered_date"] or c["planned_date"]
            if c["status"] in ("given", "modified") and when <= as_of:
                cmeds = rows(connection, "SELECT drug_name, planned_dose, actual_dose, dose_unit, status FROM cycle_medications WHERE cycle_id = ?", (c["id"],))
                last_given = {"cycle": c["cycle_number"], "date": when, "status": c["status"],
                              "days_since": (date.fromisoformat(as_of) - date.fromisoformat(when)).days,
                              "doses": [f"{m['drug_name']} {(m['actual_dose'] if m['actual_dose'] is not None else m['planned_dose']):g} {m['dose_unit']}"
                                        + (" (held)" if m["status"] == "held" else "") for m in cmeds]}
            elif c["status"] in ("planned", "delayed") and when < as_of:
                last_due = {"cycle": c["cycle_number"], "date": when, "status": c["status"]}
            elif c["status"] in ("planned", "delayed") and next_cycle is None and when >= as_of:
                next_cycle = {"cycle": c["cycle_number"], "date": when, "days_until": (date.fromisoformat(when) - date.fromisoformat(as_of)).days}
    toxicities = [dict(t) for t in rows(connection, """SELECT ctcae_term, grade, onset_date, status, suspected_drug FROM symptoms
                                                       WHERE patient_id = ? AND onset_date <= ? ORDER BY onset_date DESC, id DESC LIMIT 8""", (patient_id, as_of))]
    labs = []
    latest = rows(connection, """SELECT test_name, value, unit, ref_low, ref_high, collection_date FROM lab_investigations
                                 WHERE patient_id = ? AND verification_status = 'verified' AND collection_date <= ?
                                 ORDER BY collection_date DESC, id DESC""", (patient_id, as_of))
    seen: dict[str, dict[str, Any]] = {}
    for lab in latest:
        if lab["test_name"] in KEY_LABS and lab["test_name"] not in seen:
            seen[lab["test_name"]] = lab
        elif lab["test_name"] in seen and "previous" not in seen[lab["test_name"]] and lab["collection_date"] < seen[lab["test_name"]]["collection_date"]:
            seen[lab["test_name"]]["previous"] = lab["value"]
    for name in KEY_LABS:
        lab = seen.get(name)
        if not lab:
            continue
        flag = "low" if lab["ref_low"] is not None and lab["value"] < lab["ref_low"] else "high" if lab["ref_high"] is not None and lab["value"] > lab["ref_high"] else None
        graded = grade_lab(name, lab["value"], lab["unit"], lab["ref_low"], lab["ref_high"]) or {}
        labs.append({"test": name, "value": lab["value"], "unit": lab["unit"], "date": lab["collection_date"], "flag": flag,
                     "previous": lab.get("previous"), "ctcae": f"{graded['term']} G{graded['grade']}" if graded.get("term") and graded.get("grade") else None})
    earlier = rows(connection, """SELECT onset_date, text, ai_json, status FROM patient_reports WHERE patient_id = ? AND onset_date <= ? AND id != ?
                                  ORDER BY created_at DESC LIMIT 6""", (patient_id, as_of, exclude_report or 0))
    previous_reports = []
    for r in earlier:
        ai = json.loads(r["ai_json"]) if r["ai_json"] else {}
        previous_reports.append({"date": r["onset_date"], "status": r["status"], "summary": (ai.get("summary_en") or r["text"])[:200]})
    return {"as_of": as_of, "age": _age(patient.get("date_of_birth"), as_of), "sex": patient.get("sex") or None,
            "diagnosis": patient.get("diagnosis") or None, "regimen": regimen, "cycles_total": total,
            "last_given": last_given,
            "last_due": last_due if last_due and (not last_given or last_due["date"] > last_given["date"]) else None, "next_cycle": next_cycle, "toxicities": toxicities, "labs": labs,
            "previous_reports": previous_reports}


def context_text(ctx: dict[str, Any]) -> str:
    lines = [f"Patient: {ctx.get('age') or '?'} y, {ctx.get('sex') or 'sex not recorded'}; diagnosis: {ctx.get('diagnosis') or 'not recorded'}"]
    reg = ctx.get("regimen")
    lines.append(f"Regimen: {reg['name']} ({', '.join(reg['drugs']) or 'no drugs listed'}); {ctx.get('cycles_total')} cycles planned" if reg else "Regimen: none active")
    lg = ctx.get("last_given")
    lines.append(f"Last cycle given: C{lg['cycle']} on {lg['date']} ({lg['days_since']} days before symptom onset); doses {', '.join(lg['doses']) or 'n/a'}"
                 if lg else "Last cycle given: none yet (treatment not started or not recorded)")
    ld = ctx.get("last_due")
    if ld:
        lines.append(f"Note: C{ld['cycle']} was due {ld['date']} but is not recorded as given")
    nc = ctx.get("next_cycle")
    if nc:
        lines.append(f"Next cycle: C{nc['cycle']} planned {nc['date']} (in {nc['days_until']} days)")
    tox = ctx.get("toxicities") or []
    lines.append("Clinician-recorded toxicities: " + ("; ".join(f"{t['ctcae_term']} G{t['grade']} from {t['onset_date']} ({t['status']})" for t in tox) or "none"))
    labs = ctx.get("labs") or []
    lines.append("Latest verified labs: " + ("; ".join(
        f"{l['test']} {l['value']:g} {l['unit']} on {l['date']}" + (f" [{l['flag']}]" if l["flag"] else "")
        + (f" (prev {l['previous']:g})" if l.get("previous") is not None else "") + (f" {l['ctcae']}" if l.get("ctcae") else "") for l in labs) or "none"))
    prev = ctx.get("previous_reports") or []
    if prev:
        lines.append("Earlier patient messages: " + "; ".join(f"{p['date']}: {p['summary']}" for p in prev[:4]))
    return "\n".join(lines)


def _ai_triage(report: dict[str, Any], drugs: list[str], context: dict[str, Any] | None = None) -> dict[str, Any]:
    status = extraction.ai_status()
    clinical_terms = {t: info["grades"] for t, info in CTCAE_TERMS.items() if info["kind"] == "clinical"}
    catalog = "\n".join(f"- {t}: " + "; ".join(f"G{g}: {c}" for g, c in grades.items()) for t, grades in clinical_terms.items())
    prompt = (
        "A patient receiving chemotherapy sent this symptom report to their oncology team. It may be in any language or script "
        "(often Hindi, Telugu, Tamil, Kannada, Malayalam, Marathi or Bengali) and may come from speech-to-text, so expect "
        "repeated or mis-heard words.\n\n"
        f"CLINICAL CONTEXT FROM THE RECORD\n{context_text(context) if context else 'Regimen drugs: ' + (', '.join(drugs) or 'unknown')}\n\n"
        "PATIENT REPORT\n"
        f"Text: <<<{report['text']}>>>\n"
        f"Symptom onset: {report.get('onset_date') or 'not given'}\n"
        f"Checklist selected: {', '.join(report['symptoms']) or 'none'}\n"
        f"Effect on daily life: {IMPACT_TEXT.get(report.get('daily_impact') or '', 'not stated')}\n"
        f"Temperature: {report.get('temperature') if report.get('temperature') is not None else 'not given'}\n"
        + (f"Structured check-in answers (the patient answered these in the portal):\n{questionnaire.summary_text(report['checkin'])}\n"
           if report.get("checkin") else "") + "\n"
        "Help the clinician triage. Return ONLY JSON: {\"language\": str, \"translation_en\": str (faithful English translation of the "
        "patient's text; repeat it if already English), \"summary_en\": str (neutral clinical summary in English that relates the symptoms "
        "to the context above, e.g. timing after the last dose or relevant labs; max 3 sentences, no advice), \"candidates\": [{\"term\": "
        "<exact CTCAE term from the list>, \"grade_suggestion\": int|null, \"evidence\": <short quote from the patient, in English>, "
        "\"rationale\": <why this term/grade, referencing the criteria and context>}], \"possible_red_flags\": [str] (features that need "
        "same-day contact, e.g. vomiting that prevents fluids, fever with low ANC; empty if none), \"questions_for_clinician\": [str] "
        "(what to ask the patient to grade accurately, e.g. number of episodes in 24 h)}. Only use terms from this list; suggest a grade "
        "only when the report clearly meets that grade's criterion, otherwise null. Never give advice to the patient.\n" + catalog)
    reply = extraction._anthropic_post(extraction.os.environ["ANTHROPIC_API_KEY"],
                                       {"model": status["model"], "max_tokens": 2000,
                                        "messages": [{"role": "user", "content": prompt}]}, 90)
    text = "".join(part.get("text", "") for part in reply.get("content", []))
    match = re.search(r"\{.*\}", text, re.S)
    parsed = json.loads(match.group(0)) if match else {}
    candidates = []
    for item in parsed.get("candidates", []):
        term = item.get("term")
        if term not in clinical_terms:
            continue
        grade = item.get("grade_suggestion")
        grade = grade if isinstance(grade, int) and grade in clinical_terms[term] else None
        candidates.append({"term": term, "grade_suggestion": grade, "evidence": str(item.get("evidence") or "")[:300],
                           "rationale": str(item.get("rationale") or "")[:500], "associated_drugs": drugs_associated_with(term, drugs)})
    return {"method": "claude", "model": status["model"], "language": parsed.get("language"),
            "translation_en": str(parsed.get("translation_en") or "")[:1500],
            "summary_en": str(parsed.get("summary_en") or "")[:800], "candidates": candidates,
            "possible_red_flags": [str(q)[:200] for q in parsed.get("possible_red_flags", [])][:5],
            "questions_for_clinician": [str(q)[:300] for q in parsed.get("questions_for_clinician", [])][:5]}


def _friendly_error(error: Exception) -> str:
    text = str(error)
    if "HTTP 401" in text or "HTTP 403" in text:
        return "The AI key was rejected. Check it under AI extraction."
    if "HTTP 429" in text or "HTTP 529" in text or "overloaded" in text.lower():
        return "The AI service was busy. Try again in a minute."
    if "Cannot reach" in text or "timed out" in text.lower():
        return "The AI service could not be reached."
    return "The AI request failed (" + text[:160] + ")."


def triage(report: dict[str, Any], drugs: list[str], context: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    checked = {i["term"] for i in (report.get("checkin") or {}).get("items", []) if i.get("term")}
    keyword = [c for c in _keyword_candidates(report["text"], [] if report.get("checkin") else report["symptoms"], drugs) if c["term"] not in checked]
    if extraction.ai_status()["enabled"]:
        try:
            result = _ai_triage(report, drugs, context)
            result["candidates"] = [c for c in result["candidates"] if c["term"] not in checked or c.get("grade_suggestion") is not None]
            known = {c["term"] for c in result["candidates"]} | checked
            result["keyword_candidates"] = [c for c in keyword if c["term"] not in known]
            return result, "done"
        except Exception as error:  # keep the report; fall back to keywords
            return {"method": "keywords", "candidates": keyword, "error": _friendly_error(error)}, "failed"
    return {"method": "keywords", "candidates": keyword}, "done"


def retriage(db_path: str, report_id: int) -> dict[str, Any]:
    """Run triage again (e.g. after an AI failure or once more labs are in), using today's patient context."""
    with transaction(db_path) as connection:
        row = row_or_none(connection, "SELECT * FROM patient_reports WHERE id = ?", (report_id,))
        if not row:
            raise PortalError(404, "Report not found")
        drugs = _regimen_drugs(connection, row["patient_id"])
        context = patient_context(connection, row["patient_id"], max(row["onset_date"], row["created_at"][:10]), report_id)
        connection.execute("UPDATE patient_reports SET ai_status = 'pending', updated_at = ? WHERE id = ?", (utc_now(), report_id))
    report = {"id": report_id, "text": row["text"], "symptoms": json.loads(row["symptoms_json"] or "[]"), "daily_impact": row["daily_impact"],
              "temperature": row["temperature"], "onset_date": row["onset_date"],
              "checkin": json.loads(row["checkin_json"]) if row["checkin_json"] else None}
    result, state = triage(report, drugs, context)
    result["context_used"] = context
    with transaction(db_path) as connection:
        connection.execute("UPDATE patient_reports SET ai_json = ?, ai_status = ?, updated_at = ? WHERE id = ?",
                           (json.dumps(result), state, utc_now(), report_id))
        return report_view(row_or_none(connection, "SELECT * FROM patient_reports WHERE id = ?", (report_id,)))


def checkin_bank(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any]:
    """Question bank plus the symptoms most associated with this patient's regimen (shown first)."""
    data = questionnaire.bank()
    drugs = _regimen_drugs(connection, patient_id)
    data["common"] = [s["key"] for s in data["symptoms"] if questionnaire.SYMPTOMS[s["key"]]["term"]
                      and drugs_associated_with(questionnaire.SYMPTOMS[s["key"]]["term"], drugs)]
    return data


TERM_TO_KEY = {spec["term"]: key for key, spec in questionnaire.SYMPTOMS.items() if spec["term"]}


def detect_symptoms(text: str) -> dict[str, Any]:
    """Map free text (any language) to question-bank symptoms: keywords first, then Claude when enabled."""
    text = text.strip()[:4000]
    if not text:
        return {"symptoms": [], "method": "none"}
    keys = [TERM_TO_KEY[c["term"]] for c in _keyword_candidates(text, [], []) if c["term"] in TERM_TO_KEY]
    lower = text.lower()
    if any(w in lower for w in ("bleed", "blood", "bruis", "खून", "రక్త")) and "bleeding" not in keys and "urine_blood" not in keys:
        keys.append("bleeding")
    method = "keywords"
    if extraction.ai_status()["enabled"]:
        options = "\n".join(f"- {k}: {v['label']['en']}" for k, v in questionnaire.SYMPTOMS.items())
        prompt = ("A patient on chemotherapy described how they feel (any language, possibly speech-to-text). Which of these symptom "
                  f"keys do they mention? Return ONLY JSON {{\"symptoms\": [keys]}}.\n{options}\nPatient: <<<{text}>>>")
        try:
            reply = extraction._anthropic_post(extraction.os.environ["ANTHROPIC_API_KEY"],
                                               {"model": extraction.ai_status()["model"], "max_tokens": 200,
                                                "messages": [{"role": "user", "content": prompt}]}, 25)
            raw = "".join(part.get("text", "") for part in reply.get("content", []))
            match = re.search(r"\{.*\}", raw, re.S)
            ai_keys = [k for k in (json.loads(match.group(0)).get("symptoms", []) if match else []) if k in questionnaire.SYMPTOMS]
            keys = list(dict.fromkeys(keys + ai_keys))
            method = "claude"
        except Exception:
            pass
    return {"symptoms": list(dict.fromkeys(keys)), "method": method}


def _regimen_drugs(connection: sqlite3.Connection, patient_id: int) -> list[str]:
    meds = rows(connection, """SELECT DISTINCT m.drug_name FROM plan_medications m JOIN treatment_plans p ON p.id = m.treatment_plan_id
                               WHERE p.patient_id = ? AND p.status IN ('active', 'planned')""", (patient_id,))
    return [m["drug_name"] for m in meds]


def _current_cycle(connection: sqlite3.Connection, patient_id: int, onset: str) -> int | None:
    """The assessment window a report belongs to: the first pending/next cycle on or after the onset date."""
    cycle = row_or_none(connection, """SELECT c.id FROM treatment_cycles c JOIN treatment_plans p ON p.id = c.treatment_plan_id
                                       WHERE p.patient_id = ? AND p.status IN ('active', 'planned')
                                         AND COALESCE(c.administered_date, c.planned_date) >= ?
                                       ORDER BY COALESCE(c.administered_date, c.planned_date) LIMIT 1""", (patient_id, onset))
    return cycle["id"] if cycle else None


def create_report(db_path: str, patient_id: int, payload: dict[str, Any], upload_dir: Path) -> dict[str, Any]:
    text = str(payload.get("text") or "").strip()
    symptoms = [s for s in payload.get("symptoms", []) if isinstance(s, str) and s in CHECKLIST][:20]
    checkin = None
    if isinstance(payload.get("checkin"), dict):
        raw = payload["checkin"]
        symptoms = list(dict.fromkeys(s for s in raw.get("symptoms", []) if isinstance(s, str) and s in CHECKLIST))[:12]
        safety = raw.get("safety") or {}
        if not all(safety.get(q["id"]) for q in questionnaire.SAFETY_QUESTIONS):
            raise PortalError(422, "Please answer the safety questions.")
        try:
            checkin = questionnaire.evaluate(symptoms, raw.get("answers") or {}, safety, raw.get("voice") if isinstance(raw.get("voice"), dict) else None)
        except questionnaire.CheckinError as error:
            raise PortalError(422, str(error)) from None
        if checkin["onset"] and not payload.get("onset_date"):
            payload["onset_date"] = checkin["onset"]
        if checkin["temperature"] is not None and payload.get("temperature") in (None, ""):
            payload["temperature"] = checkin["temperature"]
        if checkin["daily_impact"] and not payload.get("daily_impact"):
            payload["daily_impact"] = checkin["daily_impact"]
        if not text and not payload.get("audio_base64"):
            text = "Check-in: " + (", ".join(i["label"] for i in checkin["items"]) or "no new symptoms")
    if not text and not symptoms and not payload.get("audio_base64"):
        raise PortalError(422, "Describe how you are feeling, choose a symptom, or record a voice message.")
    if len(text) > 4000:
        raise PortalError(422, "Please keep the message under 4000 characters.")
    mode = payload.get("input_mode") if payload.get("input_mode") in ("text", "voice") else "text"
    impact = payload.get("daily_impact") if payload.get("daily_impact") in IMPACT_TEXT else None
    onset = payload.get("onset_date") or date.today().isoformat()
    try:
        onset = date.fromisoformat(str(onset)).isoformat()
    except ValueError:
        raise PortalError(422, "Choose the date the symptoms started.") from None
    if onset > date.today().isoformat():
        raise PortalError(422, "The start date cannot be in the future.")
    temperature = payload.get("temperature")
    if temperature in ("", None):
        temperature = None
    else:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError):
            raise PortalError(422, "Temperature must be a number in °C.") from None
        if not 34 <= temperature <= 43:
            raise PortalError(422, "Enter your temperature in °C (for example 37.8).")
    audio_path = audio_type = None
    if payload.get("audio_base64"):
        audio_type = str(payload.get("audio_type") or "audio/webm").split(";")[0].strip()
        if audio_type not in AUDIO_TYPES:
            raise PortalError(415, "Unsupported audio format.")
        try:
            data = base64.b64decode(str(payload["audio_base64"]), validate=True)
        except (binascii.Error, ValueError):
            raise PortalError(422, "The voice recording could not be read.") from None
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise PortalError(413, "The voice recording is too long. Please keep it under about 5 minutes.")
        folder = upload_dir / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid.uuid4().hex}{AUDIO_TYPES[audio_type]}"
        path.write_bytes(data)
        audio_path = str(path)
    if not text:
        text = "(Voice message — no transcript)" if audio_path else "Symptoms selected: " + ", ".join(s.replace("_", " ") for s in symptoms)
    flags = detect_red_flags(text, symptoms, temperature, impact)
    if checkin:
        known = {f["code"] for f in flags}
        flags += [f for f in checkin["red_flags"] if f["code"] not in known]
    now = utc_now()
    with transaction(db_path) as connection:
        cycle_id = _current_cycle(connection, patient_id, onset)
        report_id = connection.execute(
            """INSERT INTO patient_reports (patient_id, cycle_id, text, input_mode, language, onset_date, symptoms_json, daily_impact,
                   temperature, audio_path, audio_type, red_flags_json, ai_status, status, created_at, updated_at, checkin_json, ui_language)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'new', ?, ?, ?, ?)""",
            (patient_id, cycle_id, text, mode, str(payload.get("language") or "")[:20] or None, onset, json.dumps(symptoms), impact,
             temperature, audio_path, audio_type, json.dumps(flags), now, now, json.dumps(checkin) if checkin else None,
             payload.get("ui_language") if payload.get("ui_language") in questionnaire.LANGS else None)).lastrowid
        drugs = _regimen_drugs(connection, patient_id)
        context = patient_context(connection, patient_id, onset, report_id)
    report = {"id": report_id, "text": text, "symptoms": symptoms, "daily_impact": impact, "temperature": temperature, "onset_date": onset,
              "checkin": checkin}

    def run() -> None:
        result, state = triage(report, drugs, context)
        result["context_used"] = context
        with transaction(db_path) as connection:
            connection.execute("UPDATE patient_reports SET ai_json = ?, ai_status = ?, updated_at = ? WHERE id = ?",
                               (json.dumps(result), state, utc_now(), report_id))
    if payload.get("_sync_triage"):
        run()
    else:
        threading.Thread(target=run, daemon=True).start()
    with transaction(db_path) as connection:
        created = report_view(row_or_none(connection, "SELECT * FROM patient_reports WHERE id = ?", (report_id,)), for_patient=True)
    created["emergency_message"] = EMERGENCY_MESSAGE if flags else None
    return created


def report_view(row: dict[str, Any], for_patient: bool = False) -> dict[str, Any]:
    view = {k: row[k] for k in ("id", "patient_id", "cycle_id", "text", "input_mode", "language", "onset_date", "daily_impact",
                                "temperature", "status", "created_at", "reviewed_at")}
    view["symptoms"] = json.loads(row["symptoms_json"] or "[]")
    view["red_flags"] = json.loads(row["red_flags_json"] or "[]")
    view["has_audio"] = bool(row["audio_path"])
    view["impact_text"] = IMPACT_TEXT.get(row["daily_impact"] or "")
    view["patient_message"] = row["patient_message"]
    if for_patient:
        view["is_checkin"] = bool(row["checkin_json"])
        view["status_label"] = {"new": "Sent — waiting for review", "reviewed": "Reviewed by your care team",
                                "recorded": "Reviewed and added to your record", "dismissed": "Reviewed by your care team"}[row["status"]]
        return view
    view.update({"ai": json.loads(row["ai_json"]) if row["ai_json"] else None, "ai_status": row["ai_status"],
                 "doctor_note": row["doctor_note"], "reviewed_by": row["reviewed_by"],
                 "symptom_ids": json.loads(row["symptom_ids_json"] or "[]"), "audio_type": row["audio_type"],
                 "checkin": json.loads(row["checkin_json"]) if row["checkin_json"] else None, "ui_language": row["ui_language"]})
    return view


def list_reports(connection: sqlite3.Connection, patient_id: int | None = None, status: str | None = None, for_patient: bool = False) -> list[dict[str, Any]]:
    where, params = [], []
    if patient_id is not None:
        where.append("r.patient_id = ?"); params.append(patient_id)
    if status == "open":
        where.append("r.status = 'new'")
    elif status in ("new", "reviewed", "recorded", "dismissed"):
        where.append("r.status = ?"); params.append(status)
    sql = f"""SELECT r.*, p.first_name, p.last_name, p.medical_record_number, c.cycle_number, c.treatment_plan_id
              FROM patient_reports r JOIN patients p ON p.id = r.patient_id LEFT JOIN treatment_cycles c ON c.id = r.cycle_id
              {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY r.created_at DESC, r.id DESC LIMIT 200"""
    out = []
    for row in rows(connection, sql, tuple(params)):
        view = report_view(row, for_patient=for_patient)
        view["cycle_number"] = row["cycle_number"]
        if not for_patient:
            if view["symptom_ids"]:
                marks = ",".join("?" * len(view["symptom_ids"]))
                view["recorded"] = [dict(x) for x in rows(connection, f"SELECT id, ctcae_term, grade FROM symptoms WHERE id IN ({marks})", tuple(view["symptom_ids"]))]
            else:
                view["recorded"] = []
            view["context"] = patient_context(connection, row["patient_id"], max(row["onset_date"], row["created_at"][:10]), row["id"])
            view["treatment_plan_id"] = row["treatment_plan_id"]
            view["patient_name"] = f"{row['first_name']} {row['last_name']}"
            view["medical_record_number"] = row["medical_record_number"]
        out.append(view)
    if not for_patient:
        # open reports with red flags first, then other open reports, then reviewed; newest first within each group
        out.sort(key=lambda r: (r["status"] != "new", not r["red_flags"]))
    return out


def review_report(connection: sqlite3.Connection, report_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = row_or_none(connection, "SELECT * FROM patient_reports WHERE id = ?", (report_id,))
    if not row:
        raise PortalError(404, "Report not found")
    status = payload.get("status", row["status"])
    if status not in ("new", "reviewed", "recorded", "dismissed"):
        raise PortalError(422, "Invalid status")
    symptom_ids = json.loads(row["symptom_ids_json"] or "[]")
    if payload.get("symptom_id"):
        sid = int(payload["symptom_id"])
        symptom = row_or_none(connection, "SELECT patient_id FROM symptoms WHERE id = ?", (sid,))
        if not symptom or symptom["patient_id"] != row["patient_id"]:
            raise PortalError(422, "Symptom does not belong to this patient")
        if sid not in symptom_ids:
            symptom_ids.append(sid)
    message = payload.get("patient_message", row["patient_message"])
    message = (str(message).strip()[:1000] or None) if message is not None else None
    note = payload.get("doctor_note", row["doctor_note"])
    note = (str(note).strip()[:2000] or None) if note is not None else None
    reviewed = status != "new"
    connection.execute(
        """UPDATE patient_reports SET status = ?, doctor_note = ?, patient_message = ?, symptom_ids_json = ?,
               reviewed_by = ?, reviewed_at = ?, updated_at = ? WHERE id = ?""",
        (status, note, message, json.dumps(symptom_ids), (payload.get("reviewed_by") or "Dr. R. Khanna") if reviewed else None,
         utc_now() if reviewed else None, utc_now(), report_id))
    return report_view(row_or_none(connection, "SELECT * FROM patient_reports WHERE id = ?", (report_id,)))


def audio_for(connection: sqlite3.Connection, report_id: int, patient_id: int | None = None) -> tuple[Path, str]:
    row = row_or_none(connection, "SELECT audio_path, audio_type, patient_id FROM patient_reports WHERE id = ?", (report_id,))
    if not row or not row["audio_path"] or (patient_id is not None and row["patient_id"] != patient_id):
        raise PortalError(404, "Recording not found")
    return Path(row["audio_path"]), row["audio_type"] or "application/octet-stream"

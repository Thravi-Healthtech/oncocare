"""WhatsApp channel: share plans, prescriptions, discharge summaries and visit reminders; receive symptom reports.

Two modes, chosen with WHATSAPP_MODE:
- "simulator" (default): messages are stored and shown in the clinician app's WhatsApp simulator, which also lets you
  type or record as the patient. Nothing leaves the server.
- "live": messages go through the WhatsApp Business Cloud API (WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID). Inbound
  messages arrive on /api/whatsapp/webhook (verified with WHATSAPP_VERIFY_TOKEN; signed with WHATSAPP_APP_SECRET).

Rules kept from the portal: every clinical message to the patient is composed from the record and approved by a
clinician before it is sent; the automatic replies are fixed texts (acknowledgement, the fixed symptom questions,
the emergency message). AI never writes to the patient. Business-initiated messages need the patient's opt-in, and
outside 24 hours from the patient's last message only Meta-approved templates can be sent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import agent, portal, prescriptions, questionnaire, speech, voice_answers
from . import radiotherapy as rt
from .database import normalise_phone, row_or_none, rows, transaction, utc_now
from .errors import ApiError

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_IDLE_MINUTES = 120
SYNC_TRIAGE = False  # tests set True so report triage runs inline
LANGS = ("en", "hi", "te")
OUT_KINDS = {"plan": "Treatment plan", "prescription": "Prescription", "discharge": "Discharge summary", "reminder": "Visit reminder",
             "document": "Document", "message": "Message from doctor", "reply": "Reply to patient message"}
VISIT_YES, VISIT_NO = "visit_yes", "visit_no"  # reminder button ids: "<kind>:<reminder message id>"
CONFIRM_WORDS = {"confirm", "confirmed", "confirm visit", "पुष्टि", "నిర్ధారించు"}
RESCHEDULE_WORDS = {"reschedule", "change date", "नई तारीख", "తేదీ మార్చండి"}

# ---------------------------------------------------------------------------------------------- configuration
def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def mode() -> str:
    return "live" if _env("WHATSAPP_MODE").lower() == "live" else "simulator"


def clinic() -> str:
    return _env("ONCOCARE_CLINIC_NAME", "Thravi OncoCare")


def care_phone() -> str:
    return _env("ONCOCARE_CARE_PHONE")


def portal_url() -> str:
    return _env("ONCOCARE_PUBLIC_URL", "https://onco.thravi.in").rstrip("/") + "/portal"


def live_ready() -> bool:
    return bool(_env("WHATSAPP_TOKEN") and _env("WHATSAPP_PHONE_NUMBER_ID"))


def shared_number() -> bool:
    """True when the WhatsApp number is also used by another Thravi product: unknown senders then get no automatic reply."""
    return _env("WHATSAPP_SHARED_NUMBER").lower() in {"1", "true", "yes"}


def status() -> dict[str, Any]:
    m = mode()
    return {"mode": m, "live_ready": live_ready(), "display_number": _env("WHATSAPP_DISPLAY_NUMBER") or None, "clinic": clinic(),
            "care_phone": care_phone() or None, "webhook_path": "/api/whatsapp/webhook", "template_language": _env("WHATSAPP_TEMPLATE_LANG", "en"),
            "webhook_signed": bool(_env("WHATSAPP_APP_SECRET")), "verify_token_set": bool(_env("WHATSAPP_VERIFY_TOKEN")),
            "shared_number": shared_number(),
            "agent": "AI agent (guarded)" if agent.available() else "Rule-based (AI off)", "speech": speech.status(),
            "note": None if m == "simulator" or live_ready() else "WHATSAPP_MODE=live but WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID are not set; sends will fail."}


# ---------------------------------------------------------------------------------------------- fixed texts
TEXT: dict[str, dict[str, str]] = {
    "ack": {
        "en": "Thank you. Your message has been sent to your care team at {clinic}. A doctor will review it.{call}",
        "hi": "धन्यवाद। आपका संदेश {clinic} की आपकी देखभाल टीम को भेज दिया गया है। डॉक्टर इसे देखेंगे।{call}",
        "te": "ధన్యవాదాలు. మీ సందేశం {clinic} లోని మీ వైద్య బృందానికి పంపబడింది. డాక్టర్ దీన్ని చూస్తారు.{call}"},
    "call": {
        "en": " If you feel very unwell, call {phone} or go to the nearest emergency department.",
        "hi": " अगर आप बहुत अस्वस्थ महसूस करें, तो {phone} पर कॉल करें या नज़दीकी इमरजेंसी विभाग जाएँ।",
        "te": " మీకు చాలా అస్వస్థంగా అనిపిస్తే, {phone} కి కాల్ చేయండి లేదా దగ్గరలోని ఎమర్జెన్సీ విభాగానికి వెళ్లండి."},
    "hint": {
        "en": "Tip: reply CHECK any time to answer a few short questions about your symptoms.",
        "hi": "सुझाव: अपने लक्षणों के बारे में कुछ छोटे सवालों के जवाब देने के लिए कभी भी CHECK लिखकर भेजें।",
        "te": "సూచన: మీ లక్షణాల గురించి కొన్ని చిన్న ప్రశ్నలకు జవాబు ఇవ్వడానికి ఎప్పుడైనా CHECK అని పంపండి."},
    "emergency": {
        "en": "⚠️ Some of what you described can be serious during cancer treatment. Please call your care team now{care}, or go to the nearest emergency department / call 112. Do not wait for a reply here.",
        "hi": "⚠️ आपने जो बताया उसमें से कुछ कैंसर के इलाज के दौरान गंभीर हो सकता है। कृपया अभी अपनी देखभाल टीम को कॉल करें{care}, या नज़दीकी इमरजेंसी विभाग जाएँ / 112 पर कॉल करें। यहाँ जवाब का इंतज़ार न करें।",
        "te": "⚠️ మీరు చెప్పిన వాటిలో కొన్ని క్యాన్సర్ చికిత్స సమయంలో ప్రమాదకరం కావచ్చు. దయచేసి ఇప్పుడే మీ వైద్య బృందానికి కాల్ చేయండి{care}, లేదా దగ్గరలోని ఎమర్జెన్సీ విభాగానికి వెళ్లండి / 112 కి కాల్ చేయండి. ఇక్కడ జవాబు కోసం ఎదురుచూడకండి."},
    "confirm": {
        "en": "I understood: {symptoms}.\nTo help your doctor assess this, please answer a few short questions (1–2 minutes).\n\nReply 1 to start\nReply 2 to just send your message",
        "hi": "मैंने समझा: {symptoms}।\nडॉक्टर को सही आकलन में मदद के लिए कृपया कुछ छोटे सवालों के जवाब दें (1–2 मिनट)।\n\nशुरू करने के लिए 1 भेजें\nसिर्फ़ अपना संदेश भेजने के लिए 2 भेजें",
        "te": "నాకు అర్థమైంది: {symptoms}.\nడాక్టర్ సరిగా అంచనా వేయడానికి, దయచేసి కొన్ని చిన్న ప్రశ్నలకు జవాబు ఇవ్వండి (1–2 నిమిషాలు).\n\nమొదలుపెట్టడానికి 1 పంపండి\nమీ సందేశం మాత్రమే పంపడానికి 2 పంపండి"},
    "describe": {
        "en": "Tell me how you are feeling, in your own words (for example: \"vomiting since yesterday, very tired\").",
        "hi": "अपने शब्दों में बताइए कि आप कैसा महसूस कर रहे हैं (जैसे: \"कल से उल्टी, बहुत थकान\")।",
        "te": "మీకు ఎలా ఉందో మీ మాటల్లో చెప్పండి (ఉదా: \"నిన్నటి నుండి వాంతులు, చాలా అలసట\")."},
    "reply_number": {
        "en": "Please reply with a number from the list (for example 1).",
        "hi": "कृपया सूची में से एक नंबर भेजें (जैसे 1)।",
        "te": "దయచేసి జాబితాలోని ఒక సంఖ్యను పంపండి (ఉదా: 1)."},
    "safety": {"en": "Last few safety questions:", "hi": "आख़िर में कुछ सुरक्षा सवाल:", "te": "చివరగా కొన్ని భద్రతా ప్రశ్నలు:"},
    "done": {
        "en": "Thank you. Your answers have been sent to your care team at {clinic}. A doctor will review them.{call}",
        "hi": "धन्यवाद। आपके जवाब {clinic} की आपकी देखभाल टीम को भेज दिए गए हैं। डॉक्टर इन्हें देखेंगे।{call}",
        "te": "ధన్యవాదాలు. మీ జవాబులు {clinic} లోని మీ వైద్య బృందానికి పంపబడ్డాయి. డాక్టర్ వాటిని చూస్తారు.{call}"},
    "voice": {
        "en": "Thank you, your voice message has been sent to your care team at {clinic}.{call}",
        "hi": "धन्यवाद, आपका वॉइस मैसेज {clinic} की आपकी देखभाल टीम को भेज दिया गया है।{call}",
        "te": "ధన్యవాదాలు, మీ వాయిస్ మెసేజ్ {clinic} లోని మీ వైద్య బృందానికి పంపబడింది.{call}"},
    "optin": {
        "en": "You will now receive treatment updates and reminders from {clinic} on WhatsApp. Send your symptoms here any time. Reply STOP to stop messages.",
        "hi": "अब आपको {clinic} से इलाज की जानकारी और रिमाइंडर WhatsApp पर मिलेंगे। अपने लक्षण कभी भी यहाँ भेजें। संदेश बंद करने के लिए STOP भेजें।",
        "te": "ఇకపై మీకు {clinic} నుండి చికిత్స సమాచారం, రిమైండర్లు WhatsApp లో వస్తాయి. మీ లక్షణాలను ఎప్పుడైనా ఇక్కడ పంపండి. సందేశాలు ఆపడానికి STOP పంపండి."},
    "optout": {
        "en": "You will no longer receive messages from {clinic} on WhatsApp. Reply START to turn them back on. For urgent problems call your care team or 112.",
        "hi": "अब आपको {clinic} से WhatsApp पर संदेश नहीं मिलेंगे। फिर से शुरू करने के लिए START भेजें। ज़रूरी समस्या में अपनी देखभाल टीम या 112 पर कॉल करें।",
        "te": "ఇకపై మీకు {clinic} నుండి WhatsApp సందేశాలు రావు. మళ్లీ మొదలుపెట్టడానికి START పంపండి. అత్యవసరమైతే మీ వైద్య బృందానికి లేదా 112 కి కాల్ చేయండి."},
    "not_opted": {
        "en": "Reply START if you would also like to receive your treatment plan and visit reminders here.",
        "hi": "अगर आप यहाँ अपना इलाज प्लान और विज़िट रिमाइंडर भी पाना चाहते हैं तो START भेजें।",
        "te": "మీ చికిత్స ప్రణాళిక, అపాయింట్‌మెంట్ రిమైండర్లు కూడా ఇక్కడ కావాలంటే START పంపండి."},
    "help": {
        "en": "{clinic} on WhatsApp:\n• Send your symptoms or questions in your own words, or as a voice note.\n• CHECK – answer a few questions about your symptoms.\n• STOP – stop messages · START – turn them on.\nIn an emergency call 112.",
        "hi": "{clinic} WhatsApp पर:\n• अपने लक्षण या सवाल अपने शब्दों में या वॉइस नोट से भेजें।\n• CHECK – अपने लक्षणों के बारे में कुछ सवालों के जवाब दें।\n• STOP – संदेश बंद · START – चालू।\nइमरजेंसी में 112 पर कॉल करें।",
        "te": "{clinic} WhatsApp లో:\n• మీ లక్షణాలు లేదా ప్రశ్నలను మీ మాటల్లో లేదా వాయిస్ నోట్‌గా పంపండి.\n• CHECK – మీ లక్షణాల గురించి కొన్ని ప్రశ్నలకు జవాబు ఇవ్వండి.\n• STOP – సందేశాలు ఆపండి · START – మళ్లీ ప్రారంభించండి.\nఅత్యవసరమైతే 112 కి కాల్ చేయండి."},
    "unknown": {
        "en": "This number is not registered with {clinic}. Please contact the hospital to link your WhatsApp number to your record. In an emergency call 112.",
        "hi": "यह नंबर {clinic} में पंजीकृत नहीं है। अपना WhatsApp नंबर अपने रिकॉर्ड से जोड़ने के लिए अस्पताल से संपर्क करें। इमरजेंसी में 112 पर कॉल करें।",
        "te": "ఈ నంబర్ {clinic} లో నమోదు కాలేదు. మీ WhatsApp నంబర్‌ను మీ రికార్డుకు జత చేయడానికి ఆసుపత్రిని సంప్రదించండి. అత్యవసరమైతే 112 కి కాల్ చేయండి."},
    "menu": {"en": "Hello from {clinic}. How can we help? Choose an option, or just type or send a voice note.",
             "hi": "{clinic} की ओर से नमस्ते। हम कैसे मदद करें? एक विकल्प चुनें, या लिखें या वॉइस नोट भेजें।",
             "te": "{clinic} నుండి నమస్కారం. మేము ఎలా సహాయం చేయగలం? ఒక ఎంపికను ఎంచుకోండి, లేదా టైప్ చేయండి లేదా వాయిస్ నోట్ పంపండి."},
    "complaint_prompt": {"en": "Please describe your complaint in your own words. You can type it or send a voice note.",
                         "hi": "कृपया अपनी शिकायत अपने शब्दों में बताइए। आप लिख सकते हैं या वॉइस नोट भेज सकते हैं।",
                         "te": "దయచేసి మీ ఫిర్యాదును మీ మాటల్లో చెప్పండి. టైప్ చేయవచ్చు లేదా వాయిస్ నోట్ పంపవచ్చు."},
    "will_ask": {"en": "I understood: {symptoms}. I will ask a few short questions so your doctor can assess this.",
                 "hi": "मैंने समझा: {symptoms}। डॉक्टर के आकलन के लिए मैं कुछ छोटे सवाल पूछूँगा।",
                 "te": "నాకు అర్థమైంది: {symptoms}. డాక్టర్ అంచనా కోసం కొన్ని చిన్న ప్రశ్నలు అడుగుతాను."},
    "answer_hint": {"en": "Reply with the number, or say it in your own words.", "hi": "नंबर भेजें, या अपने शब्दों में बताइए।",
                    "te": "సంఖ్యను పంపండి, లేదా మీ మాటల్లో చెప్పండి."},
    "visit_confirmed": {"en": "Thank you. Your visit at {clinic} on {when} is booked. Please bring your reports and medicines. If anything changes, reply here.",
                        "hi": "धन्यवाद। {clinic} में {when} को आपकी विज़िट बुक हो गई है। कृपया अपनी रिपोर्ट और दवाइयाँ साथ लाएँ। कुछ बदले तो यहाँ जवाब दें।",
                        "te": "ధన్యవాదాలు. {clinic} లో {when} న మీ సందర్శన బుక్ అయింది. దయచేసి మీ రిపోర్టులు, మందులు తీసుకురండి. ఏదైనా మారితే ఇక్కడ జవాబు ఇవ్వండి."},
    "visit_reschedule": {"en": "No problem. Please reply with a day and time that suits you, and the care team at {clinic} will confirm a new visit.",
                         "hi": "कोई बात नहीं। कृपया अपनी सुविधा का दिन और समय लिखकर भेजें, {clinic} की टीम नई विज़िट पक्की करेगी।",
                         "te": "పర్వాలేదు. మీకు అనుకూలమైన రోజు, సమయం జవాబుగా పంపండి, {clinic} బృందం కొత్త సందర్శనను నిర్ధారిస్తుంది."},
    "visit_unknown": {"en": "We could not find that visit. Please reply with your question and the care team will help.",
                      "hi": "हमें वह विज़िट नहीं मिली। कृपया अपना सवाल भेजें, देखभाल टीम मदद करेगी।",
                      "te": "ఆ సందర్శన కనబడలేదు. దయచేసి మీ ప్రశ్న పంపండి, వైద్య బృందం సహాయం చేస్తుంది."},
    "booked_head": {"en": "Booked visits:", "hi": "बुक की गई विज़िट:", "te": "బుక్ అయిన సందర్శనలు:"},
    "schedule_head": {"en": "Your schedule at {clinic}:", "hi": "{clinic} में आपका शेड्यूल:", "te": "{clinic} లో మీ షెడ్యూల్:"},
    "no_plan": {"en": "No treatment is scheduled at the moment.", "hi": "अभी कोई इलाज निर्धारित नहीं है।", "te": "ప్రస్తుతం ఎలాంటి చికిత్స షెడ్యూల్ లేదు."},
    "skip": {"en": "Reply 0 to skip.", "hi": "छोड़ने के लिए 0 भेजें।", "te": "వదిలేయడానికి 0 పంపండి."},
    "temp": {"en": "Reply with the highest temperature in °C (for example 38.2), or 0 if you did not measure it.",
             "hi": "सबसे ज़्यादा तापमान °C में भेजें (जैसे 38.2), या नापा नहीं तो 0 भेजें।",
             "te": "అత్యధిక ఉష్ణోగ్రతను °C లో పంపండి (ఉదా: 38.2), లేదా కొలవకపోతే 0 పంపండి."},
}
DATE_OPTIONS = [  # (label en/hi/te, days ago)
    ({"en": "Today", "hi": "आज", "te": "ఈరోజు"}, 0), ({"en": "Yesterday", "hi": "कल", "te": "నిన్న"}, 1),
    ({"en": "2–3 days ago", "hi": "2–3 दिन पहले", "te": "2–3 రోజుల క్రితం"}, 2),
    ({"en": "About a week ago", "hi": "लगभग एक हफ़्ता पहले", "te": "సుమారు వారం క్రితం"}, 7),
    ({"en": "More than a week ago", "hi": "एक हफ़्ते से ज़्यादा पहले", "te": "వారం కంటే ఎక్కువ క్రితం"}, 10),
]
STOP_WORDS = {"stop", "unsubscribe", "रोकें", "बंद", "ఆపు", "ఆపండి"}
START_WORDS = {"start", "subscribe", "शुरू", "ప్రారంభించు"}
HELP_WORDS = {"help", "मदद", "సహాయం"}
CHECK_WORDS = {"check", "check-in", "checkin", "symptom check-in", "जाँच", "చెక్"}
MENU_WORDS = {"menu", "options", "मेनू", "మెనూ"}
GREETING_WORDS = {"hi", "hello", "hey", "namaste", "namaskar", "नमस्ते", "నమస్తే", "నమస్కారం", "హాయ్"}


def t(key: str, lang: str, **kw: Any) -> str:
    text = TEXT[key].get(lang) or TEXT[key]["en"]
    if "{call}" in text:
        phone = care_phone()
        kw.setdefault("call", t("call", lang, phone=phone) if phone else t("call", lang, phone="112"))
    if "{care}" in text:
        kw.setdefault("care", f" ({care_phone()})" if care_phone() else "")
    return text.format(clinic=clinic(), **kw)


def _lang(patient: dict[str, Any] | None, text: str = "") -> str:
    if re.search(r"[ఀ-౿]", text or ""):
        return "te"
    if re.search(r"[ऀ-ॿ]", text or ""):
        return "hi"
    lang = (patient or {}).get("preferred_language")
    return lang if lang in LANGS else "en"


# ---------------------------------------------------------------------------------------------- templates (live mode)
TEMPLATES: dict[str, dict[str, Any]] = {
    "plan": {"name": "oncocare_treatment_plan", "category": "UTILITY",
             "body": "Hello {{1}}, here is your treatment plan from {{2}}. Plan details: {{3}}. Reply to this message if you have symptoms or questions.",
             "example": ["Lakshmi", "Thravi OncoCare", "Radiotherapy to the right breast, 20 sessions Mon–Sat, 19 done, next session Thu 05 Nov"]},
    "prescription": {"name": "oncocare_prescription", "category": "UTILITY",
                     "body": "Hello {{1}}, here are the medicines prescribed by {{2}} for you: {{3}}. Take them as advised and reply here if you have questions.",
                     "example": ["Lakshmi", "Dr. P. Rao", "Paracetamol 650 mg, 1 tablet three times a day for 5 days"]},
    "discharge": {"name": "oncocare_discharge_summary", "category": "UTILITY",
                  "body": "Hello {{1}}, your {{2}} from {{3}} is ready. You can view and download it in the patient portal at {{4}} or ask us for a printed copy.",
                  "example": ["Lakshmi", "discharge summary", "Thravi OncoCare", "https://onco.thravi.in/portal"]},
    "reminder": {"name": "oncocare_visit_reminder_v2", "category": "UTILITY",
                 "body": "Hello {{1}}, this is a reminder of your visit at {{2}} on {{3}}. Note from the care team: {{4}} Please tap Confirm visit or Reschedule below.",
                 "example": ["Lakshmi", "Thravi OncoCare", "Mon 16 Nov 2026, 10:00 AM", "Purpose: follow-up after radiotherapy."],
                 "buttons": [{"type": "QUICK_REPLY", "text": "Confirm visit"}, {"type": "QUICK_REPLY", "text": "Reschedule"}]},
    "document": {"name": "oncocare_document", "category": "UTILITY", "header": "DOCUMENT",
                 "body": "Hello {{1}}, your care team at {{2}} has shared a document with you: {{3}}. Reply here if you have questions about it.",
                 "example": ["Lakshmi", "Thravi OncoCare", "CT chest report (02 Nov 2026)"]},
    "reply": {"name": "oncocare_care_team_reply", "category": "UTILITY",
              "body": "Hello {{1}}, your care team at {{2}} replied to your message: {{3}}. Reply here if you have more questions.",
              "example": ["Lakshmi", "Thravi OncoCare", "Please apply the hydrogel dressing twice a day and keep the area dry"]},
}


def template_definitions() -> list[dict[str, Any]]:
    """What to submit to Meta (WhatsApp Manager) for live business-initiated messages."""
    return [{"kind": k, "label": OUT_KINDS[k], **v, "language": _env("WHATSAPP_TEMPLATE_LANG", "en")} for k, v in TEMPLATES.items()]


def _one_line(text: str, limit: int = 900) -> str:
    """Template parameters cannot contain new lines, tabs or more than 4 consecutive spaces."""
    flat = re.sub(r"\s*\n+\s*[•\-]?\s*", "; ", text.strip())
    flat = re.sub(r"[\t ]{2,}", " ", flat).strip("; ")
    return flat[:limit]


# ---------------------------------------------------------------------------------------------- record helpers
def _patient(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any]:
    patient = row_or_none(connection, "SELECT * FROM patients WHERE id = ?", (patient_id,))
    if not patient:
        raise ApiError(404, "Patient not found")
    return patient


def wa_number(phone: str | None) -> str | None:
    digits = normalise_phone(phone)
    if not digits:
        return None
    return "91" + digits if len(digits) == 10 else digits


def _fmt_day(iso: str | None) -> str:
    if not iso:
        return "—"
    return date.fromisoformat(iso[:10]).strftime("%a %d %b %Y")


def _last_inbound(connection: sqlite3.Connection, patient_id: int) -> str | None:
    row = row_or_none(connection, "SELECT MAX(created_at) AS t FROM whatsapp_messages WHERE patient_id = ? AND direction = 'in'", (patient_id,))
    return row["t"] if row else None


def window_open(connection: sqlite3.Connection, patient_id: int) -> bool:
    last = _last_inbound(connection, patient_id)
    if not last:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=24)


def _store(connection: sqlite3.Connection, patient_id: int | None, direction: str, kind: str, body: str, status_: str, **extra: Any) -> int:
    now = utc_now()
    fields = {"patient_id": patient_id, "direction": direction, "kind": kind, "body": body, "status": status_, "channel": extra.pop("channel", mode()),
              "created_at": now, "updated_at": now, **extra}
    return connection.execute(f"INSERT INTO whatsapp_messages ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values())).lastrowid


# ---------------------------------------------------------------------------------------------- composing
def _plan_text(connection: sqlite3.Connection, patient: dict[str, Any], lang: str) -> str:
    lines = []
    for course in rows(connection, "SELECT id FROM rt_courses WHERE patient_id = ? AND status IN ('planned', 'active', 'on_hold') AND approved_at IS NOT NULL ORDER BY id DESC",
                       (patient["id"],)):
        w = rt.build(connection, course["id"])
        c, s = w["course"], w["summary"]
        lines.append(f"Radiotherapy: {c['site']}")
        lines.append(f"• {s['fractions_planned']} sessions, {c['treatment_days_label']}. {s['fractions_delivered']} done.")
        for p in w["phases"]:
            lines.append(f"• {p['name'].split('·')[-1].strip().capitalize()}: {p['fractions_planned']} sessions")
        if c["status"] == "on_hold":
            lines.append("• Treatment is paused by your doctor. We will tell you when to restart.")
        elif s["next_fraction_date"]:
            lines.append(f"• Next session: {_fmt_day(s['next_fraction_date'])}. Expected last session: {_fmt_day(s['projected_end_date'])}.")
        lines.append("")
    for plan in rows(connection, "SELECT * FROM treatment_plans WHERE patient_id = ? AND status IN ('active', 'planned') ORDER BY id DESC", (patient["id"],)):
        meds = rows(connection, "SELECT drug_name, dose, dose_unit FROM plan_medications WHERE treatment_plan_id = ? ORDER BY sort_order", (plan["id"],))
        cycles = rows(connection, "SELECT cycle_number, planned_date, administered_date, status FROM treatment_cycles WHERE treatment_plan_id = ? ORDER BY cycle_number",
                      (plan["id"],))
        given = sum(1 for c in cycles if c["status"] in ("given", "modified"))
        nxt = next((c for c in cycles if c["status"] in ("planned", "delayed")), None)
        lines.append(f"Chemotherapy: {plan['regimen_name']}")
        lines.append(f"• Medicines: {', '.join(m['drug_name'] for m in meds)}")
        lines.append(f"• {given} of {len(cycles)} cycles given" + (f", every {plan['cycle_interval_days']} days" if plan["cycle_interval_days"] else "") + ".")
        if nxt:
            lines.append(f"• Next cycle (C{nxt['cycle_number']}): {_fmt_day(nxt['planned_date'])}. A blood test may be needed before it.")
        lines.append("")
    for case in rows(connection, """SELECT * FROM surgical_cases WHERE patient_id = ? AND status IN ('planned', 'scheduled', 'completed', 'cancelled')
                                    AND (surgery_date IS NULL OR surgery_date >= ? OR status = 'completed') ORDER BY surgery_date""", (patient["id"], date.today().isoformat())):
        pre = json.loads(case["preop_json"] or "{}")
        if case["status"] == "cancelled":
            lines += [f"Surgery: {case['procedure_planned']}", "• This operation has been cancelled. Your care team will discuss the next steps with you.", ""]
            continue
        if case["status"] == "completed":
            continue
        lines.append(f"Surgery: {case['procedure_planned']}")
        lines.append(f"• Date: {_fmt_day(case['surgery_date'])}" if case["surgery_date"] else "• Date: to be confirmed")
        if case["admission_date"]:
            lines.append(f"• Admission: {_fmt_day(case['admission_date'])}")
        if pre.get("fasting_from"):
            lines.append(f"• Nothing to eat or drink from {pre['fasting_from']}")
        if pre.get("special_instructions"):
            lines.append(f"• {pre['special_instructions']}")
        if case["surgeon"]:
            lines.append(f"• Surgeon: {case['surgeon']}")
        lines.append("")
    if not lines:
        raise ApiError(422, "No active treatment plan, surgery or approved radiotherapy course to share")
    return "\n".join(lines).strip()


def _medicines_text(meds: list[dict[str, Any]]) -> str:
    out = []
    for m in meds:
        parts = [m["name"], m.get("dose") or ""]
        line = " ".join(p for p in parts if p).strip()
        detail = ", ".join(x for x in (m.get("route"), m.get("frequency"), f"for {m['duration']}" if m.get("duration") else "") if x)
        out.append(f"• {line}" + (f" — {detail}" if detail else "") + (f" ({m['instructions']})" if m.get("instructions") else ""))
    return "\n".join(out)


def _final_summary(connection: sqlite3.Connection, patient_id: int, summary_id: Any) -> dict[str, Any]:
    if summary_id:
        row = row_or_none(connection, "SELECT * FROM discharge_summaries WHERE id = ? AND patient_id = ?", (int(summary_id), patient_id))
    else:
        row = row_or_none(connection, "SELECT * FROM discharge_summaries WHERE patient_id = ? AND status = 'final' ORDER BY finalised_at DESC LIMIT 1", (patient_id,))
    if not row:
        raise ApiError(422, "Finalise a discharge summary first")
    if row["status"] != "final":
        raise ApiError(422, "Only a finalised summary can be shared")
    return row


def _parse_send_at(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        raise ApiError(422, "Send time must be ISO date-time (YYYY-MM-DDTHH:MM)") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=IST)
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def compose(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    kind = payload.get("kind")
    if kind not in ("plan", "prescription", "discharge", "reminder", "document", "message"):
        raise ApiError(422, "Choose what to send")
    meta: dict[str, Any] | None = None
    with transaction(db) as connection:
        patient = _patient(connection, patient_id)
        if not patient["phone"]:
            raise ApiError(422, "Add the patient's mobile number first")
        lang = _lang(patient)
        name = patient["first_name"]
        related_type = related_id = scheduled = None
        params: list[str] = []
        if kind == "plan":
            plan = _plan_text(connection, patient, lang)
            body = f"Hello {name}, here is your treatment plan from {clinic()}:\n\n{plan}\n\nReply to this message if you have symptoms or questions. In an emergency call 112."
            params = [name, clinic(), _one_line(plan)]
        elif kind == "prescription" and (payload.get("prescription_id") or not payload.get("summary_id")) and \
                row_or_none(connection, "SELECT 1 FROM prescriptions WHERE patient_id = ? AND status = 'active'", (patient_id,)):
            rx_row = row_or_none(connection, "SELECT * FROM prescriptions WHERE id = ? AND patient_id = ? AND status = 'active'",
                                 (payload.get("prescription_id"), patient_id)) if payload.get("prescription_id") else None
            rx = prescriptions._view(rx_row) if rx_row else prescriptions.latest_active(connection, patient_id)
            if payload.get("prescription_id") and not rx_row:
                raise ApiError(422, "That prescription is not active")
            doctor = rx["prescribed_by"] or actor
            meds = rx["items"]
            advice = f"\n\nAdvice: {rx['advice']}" if rx.get("advice") else ""
            body = (f"Hello {name}, here are the medicines prescribed by {doctor} for you on {_fmt_day(rx['rx_date'])}:\n\n{_medicines_text(meds)}{advice}"
                    "\n\nTake them as advised and reply here if you have questions.")
            params = [name, doctor, _one_line(_medicines_text(meds) + (f"; Advice: {rx['advice']}" if rx.get("advice") else ""))]
            related_type, related_id = "prescription", rx["id"]
        elif kind == "prescription":
            summary = _final_summary(connection, patient_id, payload.get("summary_id"))
            content = json.loads(summary["content_json"])
            meds = content.get("medications") or []
            if not meds:
                raise ApiError(422, "The finalised summary has no medicines")
            doctor = summary["finalised_by"] or actor
            body = f"Hello {name}, these are the medicines prescribed by {doctor}:\n\n{_medicines_text(meds)}\n\nTake them as advised and reply here if you have questions."
            params = [name, doctor, _one_line(_medicines_text(meds))]
            related_type, related_id = "discharge_summary", summary["id"]
        elif kind == "discharge":
            summary = _final_summary(connection, patient_id, payload.get("summary_id"))
            content = json.loads(summary["content_json"])
            title = (content.get("title") or "discharge summary").lower()
            fu = content.get("follow_up") or {}
            follow = f"\nYour next visit: {_fmt_day(fu['date'])}" + (f" with {fu['with']}" if fu.get("with") else "") + "." if fu.get("date") else ""
            body = (f"Hello {name}, your {title} from {clinic()} is ready. You can view and download it in the patient portal at {portal_url()} "
                    f"or ask us for a printed copy.{follow}")
            params = [name, title, clinic(), portal_url()]
            related_type, related_id = "discharge_summary", summary["id"]
        elif kind == "reminder":
            visit = payload.get("visit_date")
            try:
                visit_day = date.fromisoformat(str(visit)[:10])
            except (TypeError, ValueError):
                raise ApiError(422, "Choose the visit date", {"fields": ["visit_date"]}) from None
            if visit_day < date.today():
                raise ApiError(422, "The visit date is in the past")
            time_text = str(payload.get("visit_time") or "").strip()[:5]
            if time_text and not re.fullmatch(r"\d{2}:\d{2}", time_text):
                raise ApiError(422, "Visit time must be HH:MM")
            when = _fmt_day(visit_day.isoformat()) + (f", {datetime.strptime(time_text, '%H:%M').strftime('%I:%M %p').lstrip('0')}" if time_text else "")
            purpose = str(payload.get("purpose") or "").strip()[:200]
            extra = (f"Purpose: {purpose}." if purpose else "") + (" Please bring your reports and medicines." if payload.get("bring_reports", True) else "")
            body = f"Hello {name}, this is a reminder of your visit at {clinic()} on {when}. {extra.strip()} Reply here if you need to reschedule or have new symptoms."
            params = [name, clinic(), when, extra.strip() or "We look forward to seeing you."]
            body = f"Hello {name}, this is a reminder of your visit at {clinic()} on {when}. {extra.strip()} Please tap Confirm visit or Reschedule below."
            if payload.get("send_now"):
                scheduled = None
            else:
                default_send = datetime.combine(visit_day - timedelta(days=1), datetime.min.time(), IST).replace(hour=10)
                scheduled = _parse_send_at(payload.get("send_at")) or default_send.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                if scheduled > datetime.combine(visit_day, datetime.max.time(), IST).astimezone(timezone.utc).isoformat():
                    raise ApiError(422, "The reminder would be sent after the visit")
                if scheduled <= utc_now():
                    scheduled = None  # the day-before time has passed: send now
            related_type = "visit"
            meta = {"visit_date": visit_day.isoformat(), "visit_time": time_text or None, "purpose": purpose or None}
        elif kind == "document":
            doc = row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ? AND patient_id = ?", (payload.get("document_id"), patient_id))
            if not doc:
                raise ApiError(422, "Choose a document to send", {"fields": ["document_id"]})
            label = f"{doc['title']} ({_fmt_day(doc['doc_date'])})"
            note = str(payload.get("note") or "").strip()[:300]
            body = f"Hello {name}, your care team at {clinic()} has shared a document with you: {label}." + (f"\n\n{note}" if note else "") + \
                "\n\nReply here if you have questions about it."
            params = [name, clinic(), label]
            related_type, related_id = "document", doc["id"]
            meta = {"document_id": doc["id"], "filename": doc["filename"], "content_type": doc["content_type"]}
        else:
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ApiError(422, "Write the message", {"fields": ["text"]})
            body = text[:3500]
        message_id = _store(connection, patient_id, "out", kind, body, "draft", template_name=TEMPLATES.get(kind, {}).get("name"),
                            template_params_json=json.dumps(params) if params else None, related_type=related_type, related_id=related_id,
                            scheduled_for=scheduled, meta_json=json.dumps(meta) if meta else None)
        if kind == "reminder":  # the buttons carry this reminder's id so the patient's answer books the right visit
            connection.execute("UPDATE whatsapp_messages SET buttons_json = ? WHERE id = ?",
                               (json.dumps(visit_buttons(message_id, lang), ensure_ascii=False), message_id))
        return _message(connection, message_id) | {"checks": _checks(connection, patient, kind)}


def _checks(connection: sqlite3.Connection, patient: dict[str, Any], kind: str) -> list[dict[str, str]]:
    out = []
    if not patient["wa_opt_in"]:
        out.append({"severity": "alert", "text": "WhatsApp is turned off for this patient" +
                    (" (they sent STOP)." if patient["wa_opt_in_method"] == "patient_stop" else ".") + " Turn it back on first."})
    if kind == "document" and mode() == "live" and not window_open(connection, patient["id"]):
        out.append({"severity": "info", "text": "Outside the 24-hour window only a PDF can be attached (approved template 'oncocare_document')."})
    if mode() == "live" and not window_open(connection, patient["id"]) and kind not in TEMPLATES:
        out.append({"severity": "alert", "text": "More than 24 hours since the patient's last message: only approved templates can be sent."})
    if mode() == "live" and not window_open(connection, patient["id"]) and kind in TEMPLATES:
        out.append({"severity": "info", "text": f"Will be sent with the approved template '{TEMPLATES[kind]['name']}' (single-line version)."})
    return out


def edit_draft(db: str, message_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    with transaction(db) as connection:
        msg = row_or_none(connection, "SELECT * FROM whatsapp_messages WHERE id = ?", (message_id,))
        if not msg or msg["direction"] != "out":
            raise ApiError(404, "Message not found")
        if msg["status"] != "draft":
            raise ApiError(409, "Only a draft can be edited")
        body = str(payload.get("body") or "").strip()
        if not body:
            raise ApiError(422, "The message is empty")
        fields = {"body": body[:3500], "updated_at": utc_now()}
        if "send_at" in payload:
            fields["scheduled_for"] = _parse_send_at(payload["send_at"])
        connection.execute(f"UPDATE whatsapp_messages SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), message_id))
        return _message(connection, message_id)


def cancel(db: str, message_id: int, actor: str) -> dict[str, Any]:
    with transaction(db) as connection:
        msg = row_or_none(connection, "SELECT * FROM whatsapp_messages WHERE id = ?", (message_id,))
        if not msg or msg["direction"] != "out":
            raise ApiError(404, "Message not found")
        if msg["status"] not in ("draft", "scheduled"):
            raise ApiError(409, "This message has already been sent")
        connection.execute("UPDATE whatsapp_messages SET status = 'cancelled', updated_at = ? WHERE id = ?", (utc_now(), message_id))
        return _message(connection, message_id)


def approve_and_send(db: str, message_id: int, payload: dict[str, Any], actor: str, upload_dir: Path | None = None) -> dict[str, Any]:
    with transaction(db) as connection:
        msg = row_or_none(connection, "SELECT * FROM whatsapp_messages WHERE id = ?", (message_id,))
        if not msg or msg["direction"] != "out":
            raise ApiError(404, "Message not found")
        if msg["status"] != "draft":
            raise ApiError(409, "This message is not a draft")
        patient = _patient(connection, msg["patient_id"])
        if not patient["wa_opt_in"] and not (msg["kind"] in ("reply", "message") and window_open(connection, patient["id"])):
            raise ApiError(422, "WhatsApp is turned off for this patient")
        scheduled = _parse_send_at(payload.get("send_at")) if payload.get("send_at") else msg["scheduled_for"]
        connection.execute("UPDATE whatsapp_messages SET approved_by = ?, scheduled_for = ?, updated_at = ? WHERE id = ?",
                           (actor, scheduled, utc_now(), message_id))
        if scheduled and scheduled > utc_now():
            connection.execute("UPDATE whatsapp_messages SET status = 'scheduled' WHERE id = ?", (message_id,))
            return _message(connection, message_id)
    return deliver(db, message_id)


# ---------------------------------------------------------------------------------------------- delivery
def _graph(path: str, body: dict[str, Any] | None = None, method: str = "POST", raw: bool = False) -> Any:
    version = _env("WHATSAPP_API_VERSION", "v21.0")
    url = path if path.startswith("https://") else f"https://graph.facebook.com/{version}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method if data else "GET",
                                     headers={"Authorization": f"Bearer {_env('WHATSAPP_TOKEN')}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
            return content if raw else json.loads(content or b"{}")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"WhatsApp API HTTP {error.code}: {detail or error.reason}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach graph.facebook.com ({error.reason})") from None


def _live_plan(connection: sqlite3.Connection, msg: dict[str, Any], patient: dict[str, Any]) -> dict[str, Any]:
    """Build the Graph API request for one outbound message using only the database (no network).
    A document's media id is filled in by _send_plan, after the transaction has closed."""
    to = wa_number(patient["phone"])
    if not to:
        raise RuntimeError("The patient has no valid mobile number")
    meta = json.loads(msg.get("meta_json") or "null") or {}
    buttons = json.loads(msg.get("buttons_json") or "null") or []
    document = doc_file = None
    if msg["kind"] == "document":
        doc = row_or_none(connection, "SELECT * FROM patient_documents WHERE id = ?", (meta.get("document_id"),))
        if not doc or not Path(doc["stored_path"]).exists():
            raise RuntimeError("The document file is missing")
        document = {"filename": doc["filename"]}  # "id" is added once the file is uploaded to WhatsApp
        doc_file = (Path(doc["stored_path"]), doc["content_type"] or "application/pdf")
    if window_open(connection, patient["id"]):
        if document:
            document["caption"] = msg["body"][:1024]
            body = {"messaging_product": "whatsapp", "to": to, "type": "document", "document": document}
        elif buttons:
            body = {"messaging_product": "whatsapp", "to": to, "type": "interactive",
                    "interactive": {"type": "button", "body": {"text": msg["body"][:1024]},
                                    "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in buttons[:3]]}}}
        else:
            body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": msg["body"][:4096], "preview_url": True}}
    else:
        template = TEMPLATES.get(msg["kind"])
        if not template or not msg["template_params_json"]:
            raise RuntimeError("More than 24 hours since the patient's last message; this message type has no approved template")
        if document and not str(meta.get("content_type") or "").endswith("pdf"):
            raise RuntimeError("Outside the 24-hour window only PDF documents can be sent; ask the patient to message first or upload a PDF")
        params = json.loads(msg["template_params_json"])
        components: list[dict[str, Any]] = [{"type": "body", "parameters": [{"type": "text", "text": _one_line(str(p), 1000)} for p in params]}]
        if document:
            components.insert(0, {"type": "header", "parameters": [{"type": "document", "document": document}]})
        for index, button in enumerate(buttons[:len(template.get("buttons") or [])]):
            components.append({"type": "button", "sub_type": "quick_reply", "index": str(index), "parameters": [{"type": "payload", "payload": button["id"]}]})
        body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": template["name"], "language": {"code": _env("WHATSAPP_TEMPLATE_LANG", "en")}, "components": components}}
    return {"body": body, "document": document, "doc_file": doc_file}


def _send_plan(plan: dict[str, Any]) -> str:
    """Network part of a send: upload the document (if any), then post the message. Never call inside a transaction."""
    if plan["doc_file"]:
        plan["document"]["id"] = _upload_media_live(*plan["doc_file"])
    reply = _graph(f"{_env('WHATSAPP_PHONE_NUMBER_ID')}/messages", plan["body"])
    return (reply.get("messages") or [{}])[0].get("id") or ""


def _send_live(connection: sqlite3.Connection, msg: dict[str, Any], patient: dict[str, Any]) -> str:
    return _send_plan(_live_plan(connection, msg, patient))


def deliver(db: str, message_id: int) -> dict[str, Any]:
    """Send one approved outbound message now (simulator: mark as sent).

    The WhatsApp calls run with no transaction open. Holding one across them broke live sends: the Graph call takes a
    second, the webhook's status update for an earlier message committed meanwhile, and SQLite (WAL, deferred BEGIN)
    then refused this transaction's write, so the request failed with 500 after the patient had already got the message.
    The message is first claimed (draft/scheduled -> sending) so that a double click or the scheduler cannot send it twice."""
    channel = mode()
    plan, error = None, None
    with transaction(db) as connection:
        msg = row_or_none(connection, "SELECT * FROM whatsapp_messages WHERE id = ?", (message_id,))
        if not msg:
            raise ApiError(404, "Message not found")
        claimed = connection.execute("UPDATE whatsapp_messages SET status = 'sending', updated_at = ? WHERE id = ? AND status IN ('draft', 'scheduled')",
                                     (utc_now(), message_id)).rowcount
        if not claimed:
            return _message(connection, message_id)  # already being sent (or sent) by another request
        if channel == "live":
            try:
                plan = _live_plan(connection, msg, _patient(connection, msg["patient_id"]))
            except Exception as failure:
                error = str(failure)
    wamid = f"sim-{message_id}"
    if plan:
        try:
            wamid = _send_plan(plan)
        except Exception as failure:
            error = str(failure)
    with transaction(db) as connection:
        if error is None:
            connection.execute("UPDATE whatsapp_messages SET status = 'sent', channel = ?, wa_message_id = ?, error = NULL, sent_at = ?, updated_at = ? WHERE id = ?",
                               (channel, wamid, utc_now(), utc_now(), message_id))
        else:
            connection.execute("UPDATE whatsapp_messages SET status = 'failed', channel = ?, error = ?, updated_at = ? WHERE id = ?",
                               (channel, error[:300], utc_now(), message_id))
        return _message(connection, message_id)


MEDIA_DIR: Path | None = None  # set by the API handler: <upload dir>/whatsapp


def _media_dir() -> Path:
    folder = (MEDIA_DIR or Path("uploads") / "whatsapp")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _upload_media_live(path: Path, content_type: str) -> str:
    """Upload a file to WhatsApp media storage; returns the media id."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"messaging_product\"\r\n\r\nwhatsapp\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\n{content_type}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
    body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    version = _env("WHATSAPP_API_VERSION", "v21.0")
    req = urllib.request.Request(f"https://graph.facebook.com/{version}/{_env('WHATSAPP_PHONE_NUMBER_ID')}/media", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {_env('WHATSAPP_TOKEN')}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read()).get("id", "")


def _speak(body: str, lang: str) -> tuple[str, str] | None:
    """Spoken copy of a reply (Sarvam TTS) saved under the media folder; None when speech is off or fails."""
    if not speech.status()["enabled"]:
        return None
    try:
        audio, content_type = speech.synthesize(re.sub(r"https?://\S+", "", body), lang)
    except Exception:
        return None
    path = _media_dir() / f"tts-{uuid.uuid4().hex}{'.mp3' if content_type == 'audio/mpeg' else '.wav'}"
    path.write_bytes(audio)
    return str(path), content_type


def _bot(connection: sqlite3.Connection, patient_id: int | None, body: str, kind: str = "bot", wa_from: str | None = None,
         buttons: list[dict[str, str]] | None = None, meta: dict[str, Any] | None = None, voice_lang: str | None = None) -> int:
    """Automatic reply (fixed text or guarded agent text), sent at once inside the 24-hour window the patient opened.
    `buttons` become WhatsApp reply buttons (max 3); `voice_lang` adds a spoken copy when speech is configured."""
    media = _speak(body, voice_lang) if voice_lang else None
    meta = dict(meta or {})
    if voice_lang:
        meta["voice"] = voice_lang
    message_id = _store(connection, patient_id, "out", kind, body, "draft", wa_from=wa_from,
                        buttons_json=json.dumps(buttons, ensure_ascii=False) if buttons else None,
                        meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
                        media_path=media[0] if media else None, media_type=media[1] if media else None)
    if mode() == "live":
        # Queued, not sent here: this runs inside the caller's transaction, and a WhatsApp call must not hold the
        # database. receive() / run_due() send the queue once their transaction has committed (send_queued).
        connection.execute("UPDATE whatsapp_messages SET status = 'queued', updated_at = ? WHERE id = ?", (utc_now(), message_id))
    else:
        connection.execute("UPDATE whatsapp_messages SET status = 'sent', wa_message_id = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                           (f"sim-{message_id}", utc_now(), utc_now(), message_id))
    return message_id


def _send_bot(db: str, message_id: int) -> None:
    """Send one queued automatic reply (text or reply buttons, plus a spoken copy when one was made)."""
    with transaction(db) as connection:
        if not connection.execute("UPDATE whatsapp_messages SET status = 'sending', updated_at = ? WHERE id = ? AND status = 'queued'",
                                  (utc_now(), message_id)).rowcount:
            return  # another thread is sending it
        msg = row_or_none(connection, "SELECT * FROM whatsapp_messages WHERE id = ?", (message_id,))
        to = msg["wa_from"]
        if not to and msg["patient_id"]:
            to = wa_number((row_or_none(connection, "SELECT phone FROM patients WHERE id = ?", (msg["patient_id"],)) or {}).get("phone"))
    body = msg["body"]
    buttons = json.loads(msg.get("buttons_json") or "null") or []
    wamid, error = None, None
    try:
        if buttons:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "interactive",
                       "interactive": {"type": "button", "body": {"text": body[:1024]},
                                       "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in buttons[:3]]}}}
        else:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body[:4096]}}
        reply = _graph(f"{_env('WHATSAPP_PHONE_NUMBER_ID')}/messages", payload)
        wamid = (reply.get("messages") or [{}])[0].get("id")
        if msg.get("media_path") and Path(msg["media_path"]).exists():
            media_id = _upload_media_live(Path(msg["media_path"]), msg.get("media_type") or "audio/mpeg")
            _graph(f"{_env('WHATSAPP_PHONE_NUMBER_ID')}/messages", {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": media_id}})
    except Exception as failure:
        error = str(failure)[:300]
    with transaction(db) as connection:
        if wamid:  # the text went out; a failed spoken copy is only noted
            connection.execute("UPDATE whatsapp_messages SET status = 'sent', wa_message_id = ?, error = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                               (wamid, error, utc_now(), utc_now(), message_id))
        else:
            connection.execute("UPDATE whatsapp_messages SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (error, utc_now(), message_id))


def send_queued(db: str) -> int:
    """Send automatic replies queued by _bot, oldest first, after the transaction that created them has committed."""
    with transaction(db) as connection:
        ids = [r["id"] for r in rows(connection, "SELECT id FROM whatsapp_messages WHERE status = 'queued' ORDER BY id")]
    for message_id in ids:
        _send_bot(db, message_id)
    return len(ids)


def send_report_reply(db: str, report_id: int, actor: str, upload_dir: Path | None = None) -> dict[str, Any] | None:
    """The clinician's reply to a WhatsApp complaint goes back on WhatsApp (they wrote and sent it)."""
    with transaction(db) as connection:
        report = row_or_none(connection, "SELECT * FROM patient_reports WHERE id = ?", (report_id,))
        if not report or (report.get("source") or "portal") != "whatsapp" or not report["patient_message"]:
            return None
        patient = _patient(connection, report["patient_id"])
        body = f"Reply from your care team at {clinic()}:\n\n{report['patient_message']}"
        message_id = _store(connection, patient["id"], "out", "reply", body, "draft", report_id=report_id, approved_by=actor,
                            template_name=TEMPLATES["reply"]["name"], template_params_json=json.dumps([patient["first_name"], clinic(), report["patient_message"]]))
    return deliver(db, message_id)


def run_due(db: str) -> dict[str, int]:
    """Send scheduled messages that are due and close idle check-in sessions. Called every minute by the server."""
    sent = closed = 0
    with transaction(db) as connection:
        due = [r["id"] for r in rows(connection, "SELECT id FROM whatsapp_messages WHERE status = 'scheduled' AND scheduled_for <= ?", (utc_now(),))]
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=SESSION_IDLE_MINUTES)).replace(microsecond=0).isoformat()
        stale = [r["patient_id"] for r in rows(connection, "SELECT patient_id FROM whatsapp_sessions WHERE updated_at < ?", (cutoff,))]
    for message_id in due:
        deliver(db, message_id)
        sent += 1
    for patient_id in stale:
        with transaction(db) as connection:
            session = _session(connection, patient_id)
            if session:
                _submit(connection, db, patient_id, session, None, partial=True)
                closed += 1
    if mode() == "live":
        send_queued(db)
    return {"sent": sent, "closed": closed}


# ---------------------------------------------------------------------------------------------- consent & views
def set_consent(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    opt_in = bool(payload.get("opt_in"))
    method = payload.get("method") or ("in_clinic_verbal" if opt_in else "clinician_opt_out")
    if opt_in and method not in ("default", "clinician_on", "in_clinic_verbal", "in_clinic_written", "patient_whatsapp"):
        raise ApiError(422, "Unknown setting")
    lang = payload.get("language")
    if lang is not None and lang not in LANGS:
        raise ApiError(422, "Language must be en, hi or te")
    with transaction(db) as connection:
        patient = _patient(connection, patient_id)
        if opt_in and not patient["phone"]:
            raise ApiError(422, "Add the patient's mobile number first")
        connection.execute("UPDATE patients SET wa_opt_in = ?, wa_opt_in_at = ?, wa_opt_in_method = ?, preferred_language = COALESCE(?, preferred_language), updated_at = ? WHERE id = ?",
                           (1 if opt_in else 0, utc_now(), method, lang, utc_now(), patient_id))
        if not opt_in:
            connection.execute("UPDATE whatsapp_messages SET status = 'cancelled', updated_at = ? WHERE patient_id = ? AND status = 'scheduled'", (utc_now(), patient_id))
    return patient_thread(db, patient_id)


def _message(connection: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    msg = row_or_none(connection, "SELECT * FROM whatsapp_messages WHERE id = ?", (message_id,))
    msg["kind_label"] = OUT_KINDS.get(msg["kind"], {"bot": "Automatic reply", "complaint": "Patient message", "answer": "Patient answer",
                                                    "voice": "Voice note", "command": "Patient command"}.get(msg["kind"], msg["kind"]))
    msg["template_params"] = json.loads(msg.pop("template_params_json") or "null")
    msg["meta"] = json.loads(msg.pop("meta_json") or "null")
    msg["buttons"] = json.loads(msg.pop("buttons_json") or "null")
    msg["has_audio"] = bool(msg["media_path"])
    msg.pop("media_path", None)
    return msg


def patient_thread(db: str, patient_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        patient = _patient(connection, patient_id)
        messages = [_message(connection, r["id"]) for r in rows(connection, "SELECT id FROM whatsapp_messages WHERE patient_id = ? ORDER BY created_at, id", (patient_id,))]
        session = _session(connection, patient_id)
        summaries = rows(connection, "SELECT id, kind, finalised_at FROM discharge_summaries WHERE patient_id = ? AND status = 'final' ORDER BY finalised_at DESC", (patient_id,))
        return {"patient": {k: patient[k] for k in ("id", "first_name", "last_name", "medical_record_number", "phone", "preferred_language",
                                                     "wa_opt_in", "wa_opt_in_at", "wa_opt_in_method")} | {"wa_number": wa_number(patient["phone"])},
                "messages": messages, "window_open": window_open(connection, patient_id), "session": session, "final_summaries": summaries,
                "prescriptions": rows(connection, "SELECT id, rx_date, prescribed_by, context FROM prescriptions WHERE patient_id = ? AND status = 'active' "
                                                  "ORDER BY rx_date DESC, id DESC", (patient_id,)),
                "documents": rows(connection, "SELECT id, title, doc_date, doc_type, filename, content_type FROM patient_documents WHERE patient_id = ? "
                                              "ORDER BY doc_date DESC, id DESC", (patient_id,)),
                "appointments": [_appt_view(r) for r in rows(connection, "SELECT * FROM appointments WHERE patient_id = ? ORDER BY appt_date DESC, id DESC", (patient_id,))],
                "status": status()}


def threads(db: str) -> list[dict[str, Any]]:
    with transaction(db) as connection:
        data = rows(connection, """
            SELECT p.id, p.first_name, p.last_name, p.medical_record_number, p.phone, p.wa_opt_in, p.preferred_language,
                   (SELECT body FROM whatsapp_messages m WHERE m.patient_id = p.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_body,
                   (SELECT direction FROM whatsapp_messages m WHERE m.patient_id = p.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_direction,
                   (SELECT created_at FROM whatsapp_messages m WHERE m.patient_id = p.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_at,
                   (SELECT COUNT(*) FROM whatsapp_messages m WHERE m.patient_id = p.id AND m.status = 'scheduled') AS scheduled,
                   (SELECT COUNT(*) FROM patient_reports r WHERE r.patient_id = p.id AND r.status = 'new' AND r.source = 'whatsapp') AS open_reports
            FROM patients p WHERE p.active = 1 AND p.phone IS NOT NULL
            ORDER BY last_at IS NULL, last_at DESC, p.last_name""")
        unmatched = rows(connection, "SELECT wa_from, body, created_at FROM whatsapp_messages WHERE patient_id IS NULL AND direction = 'in' ORDER BY created_at DESC LIMIT 20")
    return {"threads": data, "unmatched": unmatched, "status": status()}


def media_file(db: str, message_id: int, upload_dir: Path) -> Any:
    from .routes_ext import RawResponse
    with transaction(db) as connection:
        msg = row_or_none(connection, "SELECT media_path, media_type FROM whatsapp_messages WHERE id = ?", (message_id,))
    if not msg or not msg["media_path"]:
        raise ApiError(404, "No media")
    return RawResponse(str(msg["media_path"]).encode(), "file:" + (msg["media_type"] or "application/octet-stream"), None)


# ---------------------------------------------------------------------------------------------- inbound conversation (agent)
MENU_IDS = ("menu_complaint", "menu_checkin", "menu_schedule")


VISIT_LABELS = {"en": ("Confirm visit", "Reschedule"), "hi": ("विज़िट पक्की करें", "तारीख बदलें"), "te": ("సందర్శన నిర్ధారించు", "తేదీ మార్చండి")}


def visit_buttons(reminder_id: int, lang: str = "en") -> list[dict[str, str]]:
    yes, no = VISIT_LABELS.get(lang, VISIT_LABELS["en"])
    return [{"id": f"{VISIT_YES}:{reminder_id}", "title": yes}, {"id": f"{VISIT_NO}:{reminder_id}", "title": no}]


def menu_buttons(lang: str) -> list[dict[str, str]]:
    titles = {"en": ("Register complaint", "Symptom check-in", "My schedule"),
              "hi": ("शिकायत दर्ज करें", "लक्षण जाँच", "मेरा शेड्यूल"),
              "te": ("ఫిర్యాదు నమోదు", "లక్షణాల తనిఖీ", "నా షెడ్యూల్")}[lang if lang in LANGS else "en"]
    return [{"id": i, "title": title} for i, title in zip(MENU_IDS, titles)]


def _session(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any] | None:
    row = row_or_none(connection, "SELECT state_json FROM whatsapp_sessions WHERE patient_id = ?", (patient_id,))
    return json.loads(row["state_json"]) if row else None


def _save_session(connection: sqlite3.Connection, patient_id: int, state: dict[str, Any] | None) -> None:
    if state is None:
        connection.execute("DELETE FROM whatsapp_sessions WHERE patient_id = ?", (patient_id,))
    else:
        connection.execute("INSERT INTO whatsapp_sessions (patient_id, state_json, updated_at) VALUES (?, ?, ?) "
                           "ON CONFLICT(patient_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at",
                           (patient_id, json.dumps(state, ensure_ascii=False), utc_now()))


def _queue(keys: list[str]) -> list[list[str]]:
    out = []
    for key in keys:
        for q in questionnaire.questions_for(key):
            out.append([key, q["id"]])
    for q in questionnaire.SAFETY_QUESTIONS:
        out.append(["safety", q["id"]])
    return out


def _question_text(key: str, qid: str, lang: str, first_safety: bool) -> str:
    q = voice_answers.find_question(key, qid)
    if key == "safety":
        head = (t("safety", lang) + "\n\n") if first_safety else ""
    else:
        spec = questionnaire.SYMPTOMS.get(key)
        label = (spec["label"].get(lang) or spec["label"]["en"]) if spec else key.replace("_", " ").capitalize()
        head = f"*{label}*\n"
    text = q["text"].get(lang) or q["text"]["en"]
    if q["type"] == "choice":
        options = "\n".join(f"{i}. {o['label'].get(lang) or o['label']['en']}" for i, o in enumerate(q["options"], start=1))
        return f"{head}{text}\n{options}\n{t('answer_hint', lang)}"
    if q["type"] == "date":
        options = "\n".join(f"{i}. {label.get(lang) or label['en']}" for i, (label, _) in enumerate(DATE_OPTIONS, start=1))
        return f"{head}{text}\n{options}\n{t('skip', lang)}"
    return f"{head}{text}\n{t('temp', lang)}"


def _interpret(key: str, qid: str, reply: str, lang: str) -> tuple[bool, Any]:
    """(understood, value). value None with understood=True means 'skipped'. Numbers first, then the spoken-answer rules
    (which also understand words such as 'three times', 'yesterday', 'హాయ్ లేదు'), then Claude when AI is on."""
    q = voice_answers.find_question(key, qid)
    text = reply.strip()
    number = re.fullmatch(r"\s*(\d{1,2})\s*[.)]?\s*", text)
    if q["type"] == "choice":
        if number and 1 <= int(number.group(1)) <= len(q["options"]):
            return True, q["options"][int(number.group(1)) - 1]["value"]
    elif q["type"] == "date":
        if number and int(number.group(1)) == 0:
            return True, None
        if number and 1 <= int(number.group(1)) <= len(DATE_OPTIONS):
            return True, (date.today() - timedelta(days=DATE_OPTIONS[int(number.group(1)) - 1][1])).isoformat()
    else:
        if number and int(number.group(1)) == 0:
            return True, None
    result = voice_answers.interpret(key, qid, text, lang)
    return (result["value"] is not None), result["value"]


def _submit(connection: sqlite3.Connection, db: str, patient_id: int, state: dict[str, Any], upload_dir: Path | None, partial: bool = False,
            audio: tuple[bytes, str] | None = None, prefix: str = "") -> dict[str, Any] | None:
    """Turn the conversation into one patient report (structured check-in when complete, free text otherwise)."""
    _save_session(connection, patient_id, None)
    text = (prefix + (state.get("text") or "")).strip()
    safety = state.get("safety") or {}
    complete = bool(state.get("keys")) and all(safety.get(q["id"]) for q in questionnaire.SAFETY_QUESTIONS)
    payload: dict[str, Any] = {"text": text, "input_mode": "voice" if state.get("voice") or audio else "text", "language": state.get("lang"),
                               "ui_language": state.get("lang") if state.get("lang") in questionnaire.LANGS else None, "_sync_triage": SYNC_TRIAGE}
    if audio:
        payload.update({"audio_base64": base64.b64encode(audio[0]).decode(), "audio_type": audio[1]})
    if complete:
        payload["checkin"] = {"symptoms": state.get("keys") or [], "answers": state.get("answers") or {}, "safety": safety}
    else:
        answered = []
        for key, answers in (state.get("answers") or {}).items():
            for qid, value in answers.items():
                q = voice_answers.find_question(key, qid)
                if q and value is not None:
                    answered.append(f"{key.replace('_', ' ')} – {q['text']['en']} {questionnaire._label(q, value) if q['type'] != 'date' else value}")
        for qid, value in safety.items():
            q = voice_answers.find_question("safety", qid)
            if q:
                answered.append(f"safety – {q['text']['en']} {questionnaire._label(q, value)}")
        if partial and state.get("stage") == "questions":
            answered.append("(patient stopped answering; questions incomplete)")
        if answered:
            payload["text"] = (text + "\n\nWhatsApp answers: " + "; ".join(answered))[:3900]
    if not payload["text"].strip() and not payload.get("checkin") and not audio:
        return None
    connection.commit()  # create_report opens its own transaction
    report = portal.create_report(db, patient_id, payload, upload_dir or Path("uploads"))
    connection.execute("UPDATE patient_reports SET source = 'whatsapp' WHERE id = ?", (report["id"],))
    for message_id in state.get("message_ids") or []:
        connection.execute("UPDATE whatsapp_messages SET report_id = ? WHERE id = ?", (report["id"], message_id))
    return report


def _emergency_once(connection: sqlite3.Connection, patient_id: int, state: dict[str, Any] | None, lang: str, voice: bool = False) -> None:
    if state is not None and state.get("emergency_sent"):
        return
    _bot(connection, patient_id, t("emergency", lang), "bot", voice_lang=lang if voice else None)
    if state is not None:
        state["emergency_sent"] = True


def record_facts(connection: sqlite3.Connection, patient: dict[str, Any]) -> str:
    """What the agent may quote: treatment schedule, prescribed medicines, next visit, clinic contacts."""
    lines = [f"Patient first name: {patient['first_name']}", f"Clinic: {clinic()}", f"Care team phone: {care_phone() or 'not given; emergency 112'}",
             f"Today: {date.today().isoformat()}"]
    try:
        lines.append("Treatment:\n" + _plan_text(connection, patient, "en"))
    except ApiError:
        lines.append("Treatment: no active treatment plan recorded")
    summary = row_or_none(connection, "SELECT content_json, finalised_at FROM discharge_summaries WHERE patient_id = ? AND status = 'final' ORDER BY finalised_at DESC LIMIT 1",
                          (patient["id"],))
    if summary:
        content = json.loads(summary["content_json"])
        if content.get("medications"):
            lines.append("Medicines prescribed (quote exactly):\n" + _medicines_text(content["medications"]))
        fu = content.get("follow_up") or {}
        if fu.get("date"):
            lines.append(f"Follow-up visit: {_fmt_day(fu['date'])}" + (f" with {fu['with']}" if fu.get("with") else ""))
        if content.get("advice"):
            lines.append(f"Doctor's written advice (quote only): {content['advice']}")
    rx = prescriptions.latest_active(connection, patient["id"])
    if rx:
        lines.append(f"Latest prescription ({rx['rx_date']}, {rx['prescribed_by']}; quote exactly):\n" + _medicines_text(rx["items"])
                     + (f"\nAdvice: {rx['advice']}" if rx.get("advice") else ""))
    for r in rows(connection, "SELECT body, scheduled_for FROM whatsapp_messages WHERE patient_id = ? AND kind = 'reminder' AND status IN ('scheduled', 'sent') "
                              "ORDER BY scheduled_for DESC LIMIT 2", (patient["id"],)):
        lines.append(f"Visit reminder on record: {r['body'][:200]}")
    booked = _booked_lines(connection, patient["id"])
    if booked:
        lines.append("Booked visits (confirmed):\n" + "\n".join(booked))
    return "\n".join(lines)


def _history(connection: sqlite3.Connection, patient_id: int) -> list[dict[str, str]]:
    msgs = rows(connection, "SELECT direction, body FROM whatsapp_messages WHERE patient_id = ? AND status NOT IN ('draft', 'cancelled') ORDER BY created_at DESC, id DESC LIMIT 8",
                (patient_id,))
    return [{"role": "patient" if m["direction"] == "in" else "clinic", "text": m["body"]} for m in reversed(msgs)]


def _schedule_text(connection: sqlite3.Connection, patient: dict[str, Any], lang: str) -> str:
    try:
        plan = _plan_text(connection, patient, lang)
    except ApiError:
        plan = t("no_plan", lang)
    facts = record_facts(connection, patient)
    follow = next((line for line in facts.splitlines() if line.startswith("Follow-up visit:")), "")
    booked = _booked_lines(connection, patient["id"])
    return f"{t('schedule_head', lang)}\n\n{plan}" + (f"\n\n{follow}" if follow else "") + \
        (f"\n\n{t('booked_head', lang)}\n" + "\n".join(booked) if booked else "")


def _labels(keys: list[str], lang: str) -> str:
    return ", ".join((questionnaire.SYMPTOMS[k]["label"].get(lang) or questionnaire.SYMPTOMS[k]["label"]["en"]) for k in keys)


def _start_questions(connection: sqlite3.Connection, pid: int, state: dict[str, Any], lang: str, voice: bool) -> dict[str, Any]:
    state.update({"stage": "questions", "queue": _queue(state["keys"]), "answers": {}, "safety": {}, "retries": 0, "asked_safety": False})
    key, qid = state["queue"][0]
    _save_session(connection, pid, state)
    _bot(connection, pid, _question_text(key, qid, lang, False), voice_lang=lang if voice else None)
    return {"action": "question", "question": [key, qid]}


def _transcribe(audio: tuple[bytes, str], client_transcript: str | None) -> tuple[str | None, str | None, str]:
    """(text, detected lang, method)."""
    if speech.status()["enabled"]:
        try:
            out = speech.transcribe(*audio)
            if out["text"]:
                return out["text"], out["lang"], "sarvam"
        except Exception:
            pass
    if client_transcript and client_transcript.strip():
        return client_transcript.strip()[:2000], None, "browser"
    return None, None, "none"


def receive(db: str, wa_from: str, text: str | None = None, audio: tuple[bytes, str] | None = None, upload_dir: Path | None = None,
            wa_message_id: str | None = None, channel: str | None = None, button_id: str | None = None,
            client_transcript: str | None = None) -> dict[str, Any]:
    """Handle one inbound WhatsApp message (from the webhook or the simulator); live replies go out after it is saved."""
    try:
        return _receive(db, wa_from, text, audio, upload_dir, wa_message_id, channel, button_id, client_transcript)
    finally:
        if mode() == "live":
            send_queued(db)


def _receive(db: str, wa_from: str, text: str | None, audio: tuple[bytes, str] | None, upload_dir: Path | None,
             wa_message_id: str | None, channel: str | None, button_id: str | None, client_transcript: str | None) -> dict[str, Any]:
    text = (text or "").strip()[:4000]
    digits = normalise_phone(wa_from)
    transcript_method = None
    if audio:
        heard, heard_lang, transcript_method = _transcribe(audio, client_transcript)
        text = heard or ""
    with transaction(db) as connection:
        if wa_message_id and row_or_none(connection, "SELECT 1 FROM whatsapp_messages WHERE wa_message_id = ?", (wa_message_id,)):
            return {"duplicate": True}
        patient = row_or_none(connection, "SELECT * FROM patients WHERE phone = ?", (digits,)) if digits else None
        if not patient and shared_number():
            # The number is shared with another Thravi product: a sender we don't know is probably that product's
            # user, so OncoCare neither stores the message nor answers it.
            return {"matched": False, "ignored": "shared_number"}
        lang = _lang(patient, text)
        if audio and transcript_method == "sarvam" and heard_lang in LANGS:
            lang = heard_lang
        voice = bool(audio)
        media_path = None
        if audio:
            path = _media_dir() / f"in-{uuid.uuid4().hex}{speech.AUDIO_EXT.get(audio[1], '.ogg')}"
            path.write_bytes(audio[0])
            media_path = str(path)
        if not patient:
            _store(connection, None, "in", "complaint", text or "(voice note)", "received", wa_from=wa_from, wa_message_id=wa_message_id,
                   channel=channel or mode(), media_path=media_path, media_type=audio[1] if audio else None)
            _bot(connection, None, t("unknown", lang), "bot", wa_from=wa_from)
            return {"matched": False}
        pid = patient["id"]
        if lang != "en" and not patient["preferred_language"]:
            connection.execute("UPDATE patients SET preferred_language = ? WHERE id = ?", (lang, pid))
        state = _session(connection, pid)
        word = text.lower().strip(" .!")
        visit = None
        if button_id and button_id.split(":")[0] in (VISIT_YES, VISIT_NO):
            visit, button_id = button_id, None
        elif not button_id and (state or {}).get("stage") != "questions" and word in CONFIRM_WORDS | RESCHEDULE_WORDS:
            recent = _open_reminder(connection, pid)
            if recent:
                visit = f"{VISIT_YES if word in CONFIRM_WORDS else VISIT_NO}:{recent['id']}"
        if visit:
            reminder = row_or_none(connection, "SELECT buttons_json FROM whatsapp_messages WHERE id = ?", (visit.split(":")[-1],))
            label = next((b["title"] for b in json.loads((reminder or {}).get("buttons_json") or "[]") if b["id"] == visit), visit.split(":")[0])
            in_id = _store(connection, pid, "in", "button", text or label, "received", wa_from=wa_from, wa_message_id=wa_message_id, channel=channel or mode())
            result = {"matched": True, "patient_id": pid, "message_id": in_id}
            return result | _visit_answer(connection, patient, visit, in_id, lang)
        if not button_id and state and state.get("stage") == "menu" and word in ("1", "2", "3"):
            button_id = MENU_IDS[int(word) - 1]
        if button_id and button_id not in MENU_IDS:
            button_id = None
        kind = "voice" if audio else "button" if button_id else "command" if word in STOP_WORDS | START_WORDS | HELP_WORDS | CHECK_WORDS | MENU_WORDS else \
            "answer" if state and state.get("stage") == "questions" else "complaint"
        shown = text or ("🎤 Voice note" if audio else "")
        if button_id and not text:
            shown = next(b["title"] for b in menu_buttons(lang) if b["id"] == button_id)
        meta = {"transcribed": transcript_method} if audio else None
        in_id = _store(connection, pid, "in", kind, shown, "received", wa_from=wa_from, wa_message_id=wa_message_id, channel=channel or mode(),
                       media_path=media_path, media_type=audio[1] if audio else None, meta_json=json.dumps(meta) if meta else None)
        result: dict[str, Any] = {"matched": True, "patient_id": pid, "message_id": in_id, "transcript": text if audio else None}
        say = lambda body, k="bot", buttons=None, m=None: _bot(connection, pid, body, k, buttons=buttons, meta=m, voice_lang=lang if voice else None)  # noqa: E731

        # Commands
        if word in STOP_WORDS:
            connection.execute("UPDATE patients SET wa_opt_in = 0, wa_opt_in_at = ?, wa_opt_in_method = 'patient_stop' WHERE id = ?", (utc_now(), pid))
            connection.execute("UPDATE whatsapp_messages SET status = 'cancelled', updated_at = ? WHERE patient_id = ? AND status = 'scheduled'", (utc_now(), pid))
            _save_session(connection, pid, None)
            say(t("optout", lang))
            return result | {"action": "opt_out"}
        if word in START_WORDS:
            connection.execute("UPDATE patients SET wa_opt_in = 1, wa_opt_in_at = ?, wa_opt_in_method = 'patient_whatsapp' WHERE id = ?", (utc_now(), pid))
            _save_session(connection, pid, {"stage": "menu", "lang": lang})
            say(t("optin", lang), buttons=menu_buttons(lang))
            return result | {"action": "opt_in"}
        if word in HELP_WORDS | MENU_WORDS:
            _save_session(connection, pid, {"stage": "menu", "lang": lang})
            say(t("menu", lang), buttons=menu_buttons(lang))
            return result | {"action": "menu"}
        if word in CHECK_WORDS:
            button_id = "menu_checkin"

        # Menu choices
        if button_id == "menu_complaint":
            _save_session(connection, pid, {"stage": "complaint", "lang": lang, "message_ids": [in_id]})
            say(t("complaint_prompt", lang))
            return result | {"action": "complaint_prompt"}
        if button_id == "menu_checkin":
            _save_session(connection, pid, {"stage": "describe", "lang": lang, "text": "", "message_ids": [in_id]})
            say(t("describe", lang))
            return result | {"action": "check_start"}
        if button_id == "menu_schedule":
            _save_session(connection, pid, None)
            say(_schedule_text(connection, patient, lang), buttons=menu_buttons(lang))
            return result | {"action": "schedule"}

        # A voice note we could not transcribe: keep it for the clinician.
        if audio and not text:
            report = _submit(connection, db, pid, {"text": "", "lang": lang, "message_ids": [in_id], "voice": True}, upload_dir, audio=audio)
            _save_session(connection, pid, None)
            say(t("voice", lang), buttons=menu_buttons(lang))
            if not patient["wa_opt_in"]:
                say(t("not_opted", lang))
            return result | {"action": "voice_report", "report_id": report["id"] if report else None}

        flags = portal.detect_red_flags(text, [], None, None)
        stage = (state or {}).get("stage")

        if stage == "questions":
            state["message_ids"] = (state.get("message_ids") or []) + [in_id]
            state["voice"] = state.get("voice") or voice
            if flags:
                _emergency_once(connection, pid, state, lang, voice)
            key, qid = state["queue"][0]
            understood, value = _interpret(key, qid, text, lang)
            if not understood:
                state["retries"] = state.get("retries", 0) + 1
                if state["retries"] < 2:
                    _save_session(connection, pid, state)
                    say(t("reply_number", lang) + "\n\n" + _question_text(key, qid, lang, False))
                    return result | {"action": "retry"}
                value = None  # give up on this question
            state["retries"] = 0
            if value is not None:
                if key == "safety":
                    state["safety"][qid] = value
                else:
                    state["answers"].setdefault(key, {})[qid] = value
                q = voice_answers.find_question(key, qid)
                if key == "safety" and q.get("flag") and value == q["flag"][0]:
                    _emergency_once(connection, pid, state, lang, voice)
                elif key != "safety":
                    _, _, item_flags = questionnaire._grade(key, state["answers"][key])
                    if item_flags and key not in ("fever", "breathless", "bleeding"):
                        _emergency_once(connection, pid, state, lang, voice)
            state["queue"].pop(0)
            if state["queue"]:
                key, qid = state["queue"][0]
                first_safety = key == "safety" and not state.get("asked_safety")
                if first_safety:
                    state["asked_safety"] = True
                _save_session(connection, pid, state)
                say(_question_text(key, qid, lang, first_safety))
                return result | {"action": "question", "question": [key, qid]}
            try:
                report = _submit(connection, db, pid, state, upload_dir)
            except portal.PortalError:
                report = _submit(connection, db, pid, {**state, "keys": None}, upload_dir)
            if report and report.get("red_flags"):
                _emergency_once(connection, pid, state, lang, voice)
            say(t("done", lang), buttons=menu_buttons(lang))
            return result | {"action": "checkin_submitted", "report_id": report["id"] if report else None}

        if stage == "confirm":
            state["message_ids"] = (state.get("message_ids") or []) + [in_id]
            if word in ("2", "no", "नहीं", "లేదు"):
                report = _submit(connection, db, pid, {**state, "keys": None}, upload_dir)
                say(t("ack", lang), buttons=menu_buttons(lang))
                return result | {"action": "report", "report_id": report["id"] if report else None}
            if word in ("1", "yes", "ok", "हाँ", "हां", "అవును"):
                return result | _start_questions(connection, pid, state, lang, voice)
            state["text"] = (state.get("text", "") + "\n" + text).strip()
            state["keys"] = list(dict.fromkeys(state["keys"] + portal.detect_symptoms(text)["symptoms"]))
            if flags:
                _emergency_once(connection, pid, state, lang, voice)
            _save_session(connection, pid, state)
            say(t("confirm", lang, symptoms=_labels(state["keys"], lang)))
            return result | {"action": "confirm", "symptoms": state["keys"]}

        if stage == "complaint":
            report = _submit(connection, db, pid, {"text": text, "lang": lang, "message_ids": (state.get("message_ids") or []) + [in_id], "voice": voice},
                             upload_dir, audio=audio)
            if flags:
                say(t("emergency", lang))
            say(t("ack", lang), buttons=menu_buttons(lang))
            if not patient["wa_opt_in"]:
                say(t("not_opted", lang))
            return result | {"action": "report", "report_id": report["id"] if report else None, "red_flags": [f["code"] for f in flags]}

        if stage == "describe":
            _save_session(connection, pid, None)
            detected = portal.detect_symptoms(text)["symptoms"]
            if flags or not detected:
                report = _submit(connection, db, pid, {"text": text, "lang": lang, "message_ids": [in_id], "voice": voice}, upload_dir, audio=audio)
                if flags:
                    say(t("emergency", lang))
                say(t("ack", lang), buttons=menu_buttons(lang))
                return result | {"action": "report", "report_id": report["id"] if report else None, "red_flags": [f["code"] for f in flags]}
            state = {"stage": "questions", "lang": lang, "text": text, "keys": detected, "message_ids": [in_id], "voice": voice}
            say(t("will_ask", lang, symptoms=_labels(detected, lang)))
            return result | {"symptoms": detected} | _start_questions(connection, pid, state, lang, voice)

        # No conversation in progress. Safety first: red flags always get the fixed emergency text.
        _save_session(connection, pid, None)
        if flags:
            report = _submit(connection, db, pid, {"text": text, "lang": lang, "message_ids": [in_id], "voice": voice}, upload_dir, audio=audio)
            say(t("emergency", lang))
            say(t("ack", lang))
            if not patient["wa_opt_in"]:
                say(t("not_opted", lang))
            return result | {"action": "report", "report_id": report["id"] if report else None, "red_flags": [f["code"] for f in flags]}

        decision = None
        if agent.available():
            try:
                facts = record_facts(connection, patient)
                decision = agent.decide(facts, _history(connection, pid)[:-1], text, lang, clinic())
                decision["reply"], decision["guard"] = agent.guard(decision["reply"], facts, lang)
            except Exception as error:  # fall back to the rule-based router
                result["agent_error"] = str(error)[:160]
                decision = None
        if decision:
            meta = {"agent": True, "intent": decision["intent"], "guard": decision["guard"], "model": decision["model"], "note": decision["note_for_doctor"]}
            in_meta = json.loads(row_or_none(connection, "SELECT meta_json FROM whatsapp_messages WHERE id = ?", (in_id,))["meta_json"] or "{}")
            in_meta.update({"intent": decision["intent"], "note": decision["note_for_doctor"]})
            connection.execute("UPDATE whatsapp_messages SET meta_json = ? WHERE id = ?", (json.dumps(in_meta, ensure_ascii=False), in_id))
            intent = decision["intent"]
            if intent == "checkin":
                say(decision["reply"], "agent", m=meta)
                if decision["symptoms"]:
                    state = {"stage": "questions", "lang": lang, "text": text, "keys": decision["symptoms"], "message_ids": [in_id], "voice": voice}
                    return result | {"agent": meta, "symptoms": decision["symptoms"]} | _start_questions(connection, pid, state, lang, voice)
                _save_session(connection, pid, {"stage": "describe", "lang": lang, "text": "", "message_ids": [in_id]})
                say(t("describe", lang))
                return result | {"agent": meta, "action": "check_start"}
            if intent in ("complaint", "appointment"):
                prefix = "Appointment request: " if intent == "appointment" else ""
                report = _submit(connection, db, pid, {"text": text, "lang": lang, "message_ids": [in_id], "voice": voice}, upload_dir, audio=audio, prefix=prefix)
                say(decision["reply"], "agent", buttons=menu_buttons(lang), m=meta)
                if not patient["wa_opt_in"]:
                    say(t("not_opted", lang))
                return result | {"agent": meta, "action": "report", "report_id": report["id"] if report else None}
            if intent == "question":
                say(decision["reply"], "agent", m=meta)
                return result | {"agent": meta, "action": "answered"}
            _save_session(connection, pid, {"stage": "menu", "lang": lang})
            say(decision["reply"], "agent", buttons=menu_buttons(lang), m=meta)
            return result | {"agent": meta, "action": "menu"}

        # Rule-based router (AI off or unavailable).
        if word in GREETING_WORDS:
            _save_session(connection, pid, {"stage": "menu", "lang": lang})
            say(t("menu", lang), buttons=menu_buttons(lang))
            return result | {"action": "menu"}
        detected = portal.detect_symptoms(text)["symptoms"] if text else []
        if not detected:
            report = _submit(connection, db, pid, {"text": text, "lang": lang, "message_ids": [in_id], "voice": voice}, upload_dir, audio=audio)
            say(t("ack", lang), buttons=menu_buttons(lang))
            if not patient["wa_opt_in"]:
                say(t("not_opted", lang))
            return result | {"action": "report", "report_id": report["id"] if report else None, "red_flags": []}
        _save_session(connection, pid, {"stage": "confirm", "lang": lang, "text": text, "keys": detected, "message_ids": [in_id], "voice": voice})
        say(t("confirm", lang, symptoms=_labels(detected, lang)))
        return result | {"action": "confirm", "symptoms": detected}


def simulate_inbound(db: str, payload: dict[str, Any], upload_dir: Path) -> dict[str, Any]:
    """The clinician app's simulator: act as the patient's phone."""
    patient_id = payload.get("patient_id")
    with transaction(db) as connection:
        patient = _patient(connection, int(patient_id)) if patient_id else None
    wa_from = wa_number(patient["phone"]) if patient else str(payload.get("wa_from") or "")
    if not wa_from:
        raise ApiError(422, "The patient has no mobile number")
    audio = None
    if payload.get("audio_base64"):
        try:
            audio = (base64.b64decode(str(payload["audio_base64"]), validate=True), str(payload.get("audio_type") or "audio/webm").split(";")[0])
        except ValueError:
            raise ApiError(422, "The recording could not be read") from None
    text = str(payload.get("text") or "")
    button_id = payload.get("button_id") or None
    if not text.strip() and not audio and not button_id:
        raise ApiError(422, "Type a message or record a voice note")
    result = receive(db, wa_from, text, audio, upload_dir, channel="simulator", button_id=button_id,
                     client_transcript=str(payload.get("client_transcript") or "") or None)
    return result | ({"thread": patient_thread(db, patient["id"])} if patient else {})


# ---------------------------------------------------------------------------------------------- visit reminders -> appointments
APPT_STATUSES = ("booked", "reschedule_requested", "cancelled", "attended", "missed")


def _open_reminder(connection: sqlite3.Connection, patient_id: int) -> dict[str, Any] | None:
    """The latest sent reminder for a visit that has not happened yet (used when the patient types CONFIRM / RESCHEDULE)."""
    for r in rows(connection, "SELECT * FROM whatsapp_messages WHERE patient_id = ? AND kind = 'reminder' AND status IN ('sent', 'delivered', 'read') "
                              "ORDER BY sent_at DESC, id DESC LIMIT 5", (patient_id,)):
        meta = json.loads(r.get("meta_json") or "null") or {}
        if meta.get("visit_date") and meta["visit_date"] >= date.today().isoformat():
            return r
    return None


def _when(appt_date: str, appt_time: str | None) -> str:
    return _fmt_day(appt_date) + (f", {datetime.strptime(appt_time, '%H:%M').strftime('%I:%M %p').lstrip('0')}" if appt_time else "")


def _visit_answer(connection: sqlite3.Connection, patient: dict[str, Any], visit: str, in_id: int, lang: str) -> dict[str, Any]:
    """Patient tapped Confirm visit / Reschedule on a reminder: book (or flag) the appointment for that day."""
    choice, _, rid = visit.partition(":")
    pid = patient["id"]
    reminder = row_or_none(connection, "SELECT * FROM whatsapp_messages WHERE id = ? AND patient_id = ? AND kind = 'reminder'", (rid if rid.isdigit() else -1, pid))
    meta = json.loads((reminder or {}).get("meta_json") or "null") or {}
    if not reminder or not meta.get("visit_date"):
        _bot(connection, pid, t("visit_unknown", lang), buttons=menu_buttons(lang))
        return {"action": "visit_unknown"}
    status_ = "booked" if choice == VISIT_YES else "reschedule_requested"
    now = utc_now()
    existing = row_or_none(connection, "SELECT id FROM appointments WHERE reminder_message_id = ?", (reminder["id"],))
    if existing:
        connection.execute("UPDATE appointments SET status = ?, confirmed_at = ?, updated_at = ? WHERE id = ?",
                           (status_, now if status_ == "booked" else None, now, existing["id"]))
        appt_id = existing["id"]
    else:
        appt_id = connection.execute(
            """INSERT INTO appointments (patient_id, appt_date, appt_time, purpose, status, source, reminder_message_id, confirmed_at, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'whatsapp', ?, ?, 'Patient (WhatsApp)', ?, ?)""",
            (pid, meta["visit_date"], meta.get("visit_time"), meta.get("purpose"), status_, reminder["id"], now if status_ == "booked" else None, now, now)).lastrowid
    if status_ == "booked":
        _save_session(connection, pid, None)
        _bot(connection, pid, t("visit_confirmed", lang, when=_when(meta["visit_date"], meta.get("visit_time"))))
    else:  # their next message (the day that suits them) goes to the clinician inbox as a complaint/request
        _save_session(connection, pid, {"stage": "complaint", "lang": lang, "message_ids": [in_id]})
        _bot(connection, pid, t("visit_reschedule", lang))
    return {"action": "visit_" + ("booked" if status_ == "booked" else "reschedule"), "appointment_id": appt_id}


def _appt_view(row: dict[str, Any]) -> dict[str, Any]:
    return row | {"when": _when(row["appt_date"], row["appt_time"]), "upcoming": row["appt_date"] >= date.today().isoformat()}


def appointments(db: str, patient_id: int) -> list[dict[str, Any]]:
    with transaction(db) as connection:
        _patient(connection, patient_id)
        return [_appt_view(r) for r in rows(connection, "SELECT * FROM appointments WHERE patient_id = ? ORDER BY appt_date DESC, appt_time DESC, id DESC", (patient_id,))]


def create_appointment(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    try:
        day = date.fromisoformat(str(payload.get("appt_date"))[:10])
    except (TypeError, ValueError):
        raise ApiError(422, "Choose the visit date", {"fields": ["appt_date"]}) from None
    time_text = str(payload.get("appt_time") or "").strip()[:5] or None
    if time_text and not re.fullmatch(r"\d{2}:\d{2}", time_text):
        raise ApiError(422, "Visit time must be HH:MM")
    with transaction(db) as connection:
        _patient(connection, patient_id)
        now = utc_now()
        appt_id = connection.execute(
            """INSERT INTO appointments (patient_id, appt_date, appt_time, purpose, status, source, confirmed_at, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'booked', 'clinician', ?, ?, ?, ?)""",
            (patient_id, day.isoformat(), time_text, str(payload.get("purpose") or "").strip()[:200] or None, now, actor, now, now)).lastrowid
        return _appt_view(row_or_none(connection, "SELECT * FROM appointments WHERE id = ?", (appt_id,)))


def update_appointment(db: str, appt_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    with transaction(db) as connection:
        appt = row_or_none(connection, "SELECT * FROM appointments WHERE id = ?", (appt_id,))
        if not appt:
            raise ApiError(404, "Appointment not found")
        fields: dict[str, Any] = {}
        if "status" in payload:
            if payload["status"] not in APPT_STATUSES:
                raise ApiError(422, "Unknown appointment status")
            fields["status"] = payload["status"]
            if payload["status"] == "booked" and not appt["confirmed_at"]:
                fields["confirmed_at"] = utc_now()
        if "appt_date" in payload:
            try:
                fields["appt_date"] = date.fromisoformat(str(payload["appt_date"])[:10]).isoformat()
            except ValueError:
                raise ApiError(422, "Visit date must be YYYY-MM-DD") from None
        if "appt_time" in payload:
            fields["appt_time"] = str(payload["appt_time"] or "").strip()[:5] or None
        if not fields:
            raise ApiError(422, "Nothing to change")
        fields["updated_at"] = utc_now()
        connection.execute(f"UPDATE appointments SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), appt_id))
        return _appt_view(row_or_none(connection, "SELECT * FROM appointments WHERE id = ?", (appt_id,)))


def _booked_lines(connection: sqlite3.Connection, patient_id: int) -> list[str]:
    return [f"• {_when(r['appt_date'], r['appt_time'])}" + (f" – {r['purpose']}" if r["purpose"] else "")
            for r in rows(connection, "SELECT * FROM appointments WHERE patient_id = ? AND status = 'booked' AND appt_date >= ? ORDER BY appt_date, appt_time",
                          (patient_id, date.today().isoformat()))]


# ---------------------------------------------------------------------------------------------- doctor actions -> WhatsApp
def notify(db: str, patient_id: int, kind: str, actor: str, **payload: Any) -> dict[str, Any]:
    """Send what the doctor just did in the app (plan, prescription, summary, reminder). The doctor's action is the approval.
    Returns {kind, status, ...}; 'skipped' with a reason when the patient has not opted in or there is nothing to send."""
    with transaction(db) as connection:
        patient = row_or_none(connection, "SELECT phone, wa_opt_in FROM patients WHERE id = ?", (patient_id,))
    if not patient or not patient["phone"]:
        return {"kind": kind, "status": "skipped", "reason": "no mobile number"}
    if not patient["wa_opt_in"]:
        return {"kind": kind, "status": "skipped", "reason": "WhatsApp is turned off for this patient"}
    try:
        draft = compose(db, patient_id, {"kind": kind, **payload}, actor)
        sent = approve_and_send(db, draft["id"], {}, actor)
    except ApiError as error:
        return {"kind": kind, "status": "skipped", "reason": error.message}
    return {"kind": kind, "status": sent["status"], "message_id": sent["id"], "scheduled_for": sent.get("scheduled_for"), "error": sent.get("error")}


# ---------------------------------------------------------------------------------------------- webhook (live)
def verify_webhook(query: dict[str, list[str]]) -> tuple[int, str]:
    token = _env("WHATSAPP_VERIFY_TOKEN")
    if (query.get("hub.mode") or [""])[0] == "subscribe" and token and hmac.compare_digest((query.get("hub.verify_token") or [""])[0], token):
        return 200, (query.get("hub.challenge") or [""])[0]
    return 403, "Verification failed"


def _signature_ok(raw: bytes, header: str | None) -> bool:
    secret = _env("WHATSAPP_APP_SECRET")
    if not secret:
        return os.environ.get("ONCOCARE_PUBLIC", "").lower() not in {"1", "true", "yes"}
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(expected, header)


def handle_webhook(db: str, raw: bytes, signature: str | None, upload_dir: Path) -> tuple[int, dict[str, Any]]:
    if not _signature_ok(raw, signature):
        return 403, {"error": {"message": "Invalid signature"}}
    try:
        payload = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"error": {"message": "Invalid JSON"}}
    handled = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            ours = _env("WHATSAPP_PHONE_NUMBER_ID")
            if ours and str((value.get("metadata") or {}).get("phone_number_id") or "") != ours:
                continue  # an event for another number on the same app (another Thravi product): not ours
            for st in value.get("statuses", []) or []:
                with transaction(db) as connection:
                    connection.execute("UPDATE whatsapp_messages SET status = ?, error = ?, updated_at = ? WHERE wa_message_id = ?",
                                       (st.get("status") if st.get("status") in ("sent", "delivered", "read", "failed") else "sent",
                                        json.dumps(st.get("errors"))[:300] if st.get("errors") else None, utc_now(), st.get("id")))
            for message in value.get("messages", []) or []:
                kind = message.get("type")
                text, audio, button = None, None, None
                if kind == "text":
                    text = (message.get("text") or {}).get("body")
                elif kind == "button":  # quick reply on a template: the payload is the button id we set when sending
                    choice = message.get("button") or {}
                    text, button = choice.get("text"), choice.get("payload") or None
                elif kind == "interactive":
                    inter = message.get("interactive") or {}
                    choice = inter.get("button_reply") or inter.get("list_reply") or {}
                    text, button = choice.get("title"), choice.get("id")
                elif kind in ("audio", "voice"):
                    media = message.get(kind) or {}
                    try:
                        info = _graph(media["id"], method="GET")
                        audio = (_graph(info["url"], method="GET", raw=True), (media.get("mime_type") or "audio/ogg").split(";")[0])
                    except Exception as error:  # store the message even if the media download fails
                        text = f"(Voice note could not be downloaded: {str(error)[:80]})"
                else:
                    text = f"({kind} message received — open WhatsApp on the clinic phone to view)"
                try:
                    handled.append(receive(db, message.get("from", ""), text if not button else "", audio, upload_dir, wa_message_id=message.get("id"),
                                           channel="live", button_id=button))
                except Exception as error:  # never make Meta retry forever on our bug; log in the response
                    handled.append({"error": str(error)[:200]})
    return 200, {"received": len(handled)}

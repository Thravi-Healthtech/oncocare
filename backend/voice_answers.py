"""Turn a spoken answer (English, Hindi or Telugu speech-to-text) into a check-in answer value.

Rules first (numbers, dates, yes/no and per-question phrases in all three languages); Claude is used only when the
rules find nothing or two answers tie. The patient always sees what was heard and which option was chosen, and can tap
a different one.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from . import extraction, questionnaire

NUMBER_WORDS = {
    # English
    "once": 1, "one": 1, "twice": 2, "two": 2, "three": 3, "thrice": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    # Hindi
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "छे": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10,
    "ग्यारह": 11, "बारह": 12,
    # Telugu (stems; tokens often carry suffixes such as సార్లు "times")
    "ఒకటి": 1, "ఒక": 1, "ఒకసారి": 1, "రెండు": 2, "మూడు": 3, "నాలుగు": 4, "ఐదు": 5, "అయిదు": 5, "ఆరు": 6, "ఏడు": 7,
    "ఎనిమిది": 8, "తొమ్మిది": 9, "పది": 10, "పదకొండు": 11, "పన్నెండు": 12,
}
TELUGU_STEMS = sorted((w for w in NUMBER_WORDS if re.match(r"[ఀ-౿]", w)), key=len, reverse=True)

YES = ["yes", "yeah", "yep", "yup", "sure", "correct", "right", "हाँ", "हां", "हा", "जी हाँ", "जी", "बिल्कुल", "है", "హా",
       "అవును", "ఔను", "అవునండి", "ఉంది", "ఉన్నాయి", "ఉన్నది"]
NO = ["no", "nope", "not", "never", "नहीं", "नही", "ना", "नहि", "లేదు", "లేవు", "కాదు", "లేదండి", "లేనే లేదు"]

# Phrases per question id -> option value. Longest matching phrase wins.
HINTS: dict[str, dict[str, list[str]]] = {
    "fluids": {"yes": [], "some": ["some", "a little", "partly", "partially", "only some", "थोड़ा", "थोड़ा सा", "कुछ", "కొంచెం", "కొంత", "కొద్దిగా"],
               "no": ["nothing stays", "everything comes", "comes back", "सब निकल", "कुछ नहीं रुकता", "అన్నీ బయటకు", "నిలవడం లేదు", "ఏమీ నిలవ"]},
    "treatment": {"none": ["nothing", "no medicine", "didn't need", "कुछ नहीं", "कोई दवा नहीं", "ఏమీ లేదు", "ఏ మందు లేదు", "ఏమీ అవసరం"],
                  "usual": ["usual", "my tablets", "regular tablets", "same tablets", "सामान्य", "रोज़ की", "वही गोली", "गोलियाँ", "गोली", "మామూలు", "మాత్రలు", "మాత్ర"],
                  "extra": ["extra", "new medicine", "another medicine", "doctor gave", "नई दवा", "अतिरिक्त", "डॉक्टर ने", "दूसरी दवा", "కొత్త మందు", "అదనపు", "డాక్టర్"],
                  "iv": ["drip", "iv", "saline", "fluids in", "bottle", "ड्रिप", "सलाइन", "बोतल", "డ్రిప్", "సెలైన్", "బాటిల్", "సెలైన్ ఎక్కించ"],
                  "admitted": ["admitted", "admission", "stayed in hospital", "भर्ती", "एडमिट", "ఆసుపత్రిలో చేర", "అడ్మిట్", "చేర్చుకున్నారు"]},
    "frequency": {"never": ["never", "not at all", "कभी नहीं", "बिल्कुल नहीं", "ఎప్పుడూ లేదు", "ఎప్పుడూ రాలేదు", "అస్సలు లేదు"],
                  "rarely": ["rarely", "seldom", "hardly", "very little", "बहुत कम", "कम ही", "అరుదుగా", "చాలా తక్కువ"],
                  "occasionally": ["sometimes", "occasionally", "now and then", "कभी-कभी", "कभी कभी", "अप्पुडप्पुडु", "అప్పుడప్పుడు", "కొన్నిసార్లు"],
                  "frequently": ["often", "frequently", "many times", "a lot", "अक्सर", "बार-बार", "बार बार", "ज़्यादातर", "తరచుగా", "చాలాసార్లు", "చాలా సార్లు"],
                  "almost_constantly": ["always", "all the time", "constantly", "whole day", "हमेशा", "हर समय", "लगातार", "पूरे दिन", "ఎల్లప్పుడూ", "ఎప్పుడూ ఉంటుంది", "రోజంతా", "నిరంతరం"]},
    "severity": {"mild": ["mild", "slight", "a little", "a bit", "little", "हल्का", "हल्की", "थोड़ा", "थोड़ी", "తేలిక", "కొంచెం", "కొద్దిగా"],
                 "moderate": ["moderate", "medium", "average", "okay", "मध्यम", "ठीक-ठाक", "ठीक ठाक", "बीच", "मीडियम", "మధ్యస్థ", "ఓ మోస్తరు", "మోస్తరు", "మీడియం"],
                 "severe": ["severe", "bad", "strong", "a lot", "गंभीर", "तेज़", "तेज", "ज़्यादा", "ज्यादा", "बहुत", "తీవ్ర", "ఎక్కువ", "చాలా"],
                 "very_severe": ["very severe", "very bad", "unbearable", "worst", "extreme", "बहुत गंभीर", "बहुत ज़्यादा", "बहुत ज्यादा", "असहनीय", "बहुत तेज़",
                                 "చాలా తీవ్ర", "చాలా ఎక్కువ", "భరించలేని", "భరించలేను"]},
    "interference": {"none": ["usual", "normal", "no problem", "can do", "manage", "सामान्य", "कर पा रहा", "कर पा रही", "कोई दिक्कत नहीं", "कोई परेशानी नहीं",
                              "మామూలుగా", "చేసుకోగలుగుతున్నా", "ఇబ్బంది లేదు", "చేసుకుంటున్నా"],
                     "instrumental": ["cooking", "shopping", "work", "some things", "housework", "खाना बनाना", "खरीदारी", "काम", "कुछ काम", "ऑफिस",
                                      "వంట", "పని", "షాపింగ్", "కొన్ని పనులు", "ఉద్యోగం"],
                     "selfcare": ["help", "washing", "bathing", "bath", "dressing", "eating", "मदद", "नहाने", "नहाना", "कपड़े", "खाने में मदद",
                                  "సహాయం", "స్నానం", "బట్టలు", "తినడానికి సహాయం"]},
    "eating": {"normal": ["usual", "normal", "as usual", "fine", "सामान्य", "ठीक", "पहले जैसा", "మామూలుగా", "బాగానే"],
               "less": ["less", "little less", "a bit less", "कम", "थोड़ा कम", "తక్కువ", "కొంచెం తక్కువ"],
               "very_little": ["hardly", "nothing", "can't eat", "cannot eat", "barely", "कुछ नहीं", "बिल्कुल नहीं", "नहीं खा पा", "ఏమీ తినలేక", "తినలేకపోతున్నా", "అస్సలు తినడం లేదు"],
               "soft": ["soft", "liquid", "porridge", "नरम", "दलिया", "खिचड़ी", "మెత్తని", "జావ", "గంజి"],
               "cannot": ["can't eat", "cannot eat", "can't drink", "not able", "नहीं खा पा", "खा नहीं पा", "తినలేక", "తాగలేక"]},
    "laxatives": {"none": ["no", "not using", "नहीं", "లేదు"], "sometimes": ["sometimes", "occasionally", "कभी-कभी", "कभी कभी", "అప్పుడప్పుడు", "కొన్నిసార్లు"],
                  "regular": ["every day", "daily", "regularly", "रोज़", "रोज", "हर दिन", "नियमित", "రోజూ", "ప్రతిరోజూ", "క్రమం తప్పకుండా"]},
    "constant": {"comes_goes": ["comes and goes", "on and off", "sometimes", "आता-जाता", "आता जाता", "कभी-कभी", "వస్తూ పోతూ", "అప్పుడప్పుడు"],
                 "constant": ["all the time", "always", "constant", "हर समय", "हमेशा", "लगातार", "ఎప్పుడూ ఉంటుంది", "ఎల్లప్పుడూ", "నిరంతరం"]},
    "conversation": {"no": ["no", "नहीं", "లేదు"], "sometimes": ["sometimes", "कभी-कभी", "कभी कभी", "అప్పుడప్పుడు"], "often": ["often", "always", "अक्सर", "हमेशा", "తరచుగా", "ఎప్పుడూ"]},
    "weight": {"no": ["no", "नहीं", "లేదు"], "unsure": ["not sure", "don't know", "पता नहीं", "मालूम नहीं", "తెలియదు"],
               "little": ["a little", "little", "slightly", "थोड़ा", "కొంచెం", "కొద్దిగా"], "lot": ["a lot", "lot", "much", "काफ़ी", "काफी", "बहुत", "చాలా"]},
    "extent": {"small": ["small", "little", "few spots", "one place", "थोड़ी", "छोटी", "एक जगह", "కొద్ది", "ఒక చోట"],
               "several": ["several", "many places", "few places", "कई", "कुछ हिस्सों", "చాలా చోట్ల", "కొన్ని చోట్ల"],
               "most": ["whole body", "all over", "everywhere", "most", "पूरे शरीर", "हर जगह", "శరీరమంతా", "అంతటా", "ఒళ్లంతా"]},
    "visible": {"slight": ["slight", "little", "only close", "थोड़ी", "हल्की", "కొంచెం"], "clear": ["clearly", "visible", "noticeable", "साफ़", "दिखती", "స్పష్టంగా", "కనిపిస్తోంది"],
                "large": ["very", "huge", "shape changed", "बहुत", "आकार बदल", "చాలా", "ఆకారం మారి"]},
    "amount": {"some": ["some", "little", "few", "थोड़े", "कुछ", "కొంత", "కొంచెం"], "lot": ["a lot", "lot", "most", "काफ़ी", "काफी", "बहुत", "చాలా"]},
    "when": {"moderate": ["walking fast", "stairs", "climbing", "exercise", "तेज़ चलने", "सीढ़ि", "వేగంగా", "మెట్లు"],
             "minimal": ["walking slowly", "dressing", "little work", "light", "धीरे", "कपड़े", "हल्के", "నెమ్మదిగా", "బట్టలు", "చిన్న పనులు"],
             "rest": ["rest", "resting", "sitting", "lying", "all the time", "आराम", "बैठे", "लेटे", "हर समय", "విశ్రాంతి", "కూర్చున్నా", "పడుకున్నా"]},
    "where": {"gums_nose": ["gum", "nose", "मसूड़", "नाक", "చిగుళ్ల", "ముక్కు"], "urine": ["urine", "pee", "पेशाब", "మూత్ర"],
              "stool": ["stool", "black", "motion", "मल", "काला", "शौच", "మలం", "నల్ల"], "vomit": ["vomit", "उल्टी", "వాంతి"],
              "bruises": ["bruise", "patches", "नील", "నీలి", "కమిలి"]},
    "urine": {"yes": ["normal", "usual", "सामान्य", "మామూలుగా"], "less": ["less", "little", "कम", "తక్కువ"],
              "none": ["not at all", "none", "no urine", "बिल्कुल नहीं", "नहीं हो रहा", "అస్సలు రావడం లేదు", "అస్సలు లేదు", "రావడం లేదు"]},
    "fever": {"not_measured": ["didn't measure", "did not measure", "not measured", "didn't check", "no thermometer", "नापा नहीं", "नहीं नापा", "चेक नहीं",
                               "కొలవలేదు", "చూడలేదు", "థర్మామీటర్ లేదు"]},
}
NUMERIC_RANGES = {"episodes": [(1, 2, "1-2"), (3, 5, "3-5"), (6, 99, "6+")],
                  "extra_stools": [(1, 3, "1-3"), (4, 6, "4-6"), (7, 99, "7+")],
                  "last_stool": [(0, 1, "0-1"), (2, 3, "2-3"), (4, 99, "4+")]}
DAY_WORDS = {"today": 0, "आज": 0, "ఈరోజు": 0, "ఈ రోజు": 0, "yesterday": 1, "कल": 1, "నిన్న": 1,
             "day before yesterday": 2, "परसों": 2, "మొన్న": 2}
WEEK_WORDS = ["week", "हफ्ते", "हफ़्ते", "सप्ताह", "వారం"]
DAY_UNIT = ["day", "दिन", "రోజు", "రోజుల"]
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "पहला": 1, "दूसरा": 2, "तीसरा": 3, "चौथा": 4, "पांचवां": 5,
            "మొదటి": 1, "రెండవ": 2, "రెండో": 2, "మూడవ": 3, "మూడో": 3, "నాలుగవ": 4, "ఐదవ": 5}


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"(?<!\d)\.|\.(?!\d)|[,!?;:\"()।]", " ", text)  # keep decimal points (38.5)
    return re.sub(r"\s+", " ", text).strip()


def numbers_in(text: str) -> list[float]:
    text = _norm(text)
    text = re.sub(r"(\d+)\s*(point|दशमलव|పాయింట్)\s*(\d+)", r"\1.\3", text)
    found = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    for token in text.split():
        if token in NUMBER_WORDS:
            found.append(float(NUMBER_WORDS[token]))
            continue
        stem = next((s for s in TELUGU_STEMS if token.startswith(s) and len(s) >= 2 and s != "ఒక"), None)
        if stem:
            found.append(float(NUMBER_WORDS[stem]))
    return found


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.lower()
    if phrase.isascii():
        return re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", text) is not None
    return phrase in text


def _choice(question: dict[str, Any], text: str) -> str | None:
    values = [o["value"] for o in question["options"]]
    qid = question["id"]
    # 1. counts ("3 times", "మూడుసార్లు") for numeric-range questions
    if qid in NUMERIC_RANGES:
        if qid == "last_stool":
            for word, days in sorted(DAY_WORDS.items(), key=lambda kv: -len(kv[0])):
                if _contains(text, word):
                    return next(v for lo, hi, v in NUMERIC_RANGES[qid] if lo <= days <= hi)
        nums = numbers_in(text)
        if nums:
            n = max(nums)
            return next((v for lo, hi, v in NUMERIC_RANGES[qid] if lo <= n <= hi), None)
    # 2. yes/no questions: any negation wins
    if set(values) <= {"yes", "no", "not_measured"}:
        best = _phrase_match(question, text, {v: HINTS.get(qid, {}).get(v, []) for v in values if v not in ("yes", "no")})
        if best:
            return best
        if any(_contains(text, w) for w in NO):
            return "no"
        if any(_contains(text, w) for w in YES):
            return "yes"
        return None
    # 3. phrases for this question, plus the option labels themselves in all languages
    hints = {v: list(HINTS.get(qid, {}).get(v, [])) for v in values}
    if "yes" in values:
        hints["yes"] += YES
    if "no" in values:
        hints["no"] += NO
    best = _phrase_match(question, text, hints)
    if best:
        return best
    # 4. "the second one" / a bare option number
    for word, n in ORDINALS.items():
        if _contains(text, word) and n <= len(values):
            return values[n - 1]
    nums = numbers_in(text)
    tokens = text.split()
    if len(nums) == 1 and len(tokens) <= 3 and 1 <= nums[0] <= len(values) and nums[0] == int(nums[0]):
        return values[int(nums[0]) - 1]
    return None


def _phrase_match(question: dict[str, Any], text: str, hints: dict[str, list[str]]) -> str | None:
    scored: dict[str, int] = {}
    for option in question["options"]:
        value = option["value"]
        phrases = list(hints.get(value, [])) + [label for label in option["label"].values()]
        for phrase in phrases:
            if phrase and _contains(text, _norm(phrase)):
                scored[value] = max(scored.get(value, 0), len(phrase))
    if not scored:
        return None
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None  # tie: let Claude (or the patient) decide
    return ranked[0][0]


def _date(text: str, today: date) -> str | None:
    for word, days in sorted(DAY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if _contains(text, word):
            return (today - timedelta(days=days)).isoformat()
    nums = numbers_in(text)
    if any(_contains(text, w) for w in WEEK_WORDS):
        return (today - timedelta(days=7 * int(nums[0] if nums else 1))).isoformat()
    if nums and any(_contains(text, w) for w in DAY_UNIT):
        return (today - timedelta(days=int(nums[0]))).isoformat()
    return None


def _temperature(text: str) -> float | None:
    nums = [n for n in numbers_in(text) if 30 <= n <= 110]
    if not nums:
        return None
    t = max(nums)
    if t > 45:  # Fahrenheit
        t = (t - 32) * 5 / 9
    return round(t, 1) if 34 <= t <= 43 else None


def _ai(question: dict[str, Any], heard: str, today: date) -> Any:
    if not extraction.ai_status()["enabled"]:
        return None
    if question["type"] == "choice":
        target = "one of these values: " + json.dumps({o["value"]: o["label"]["en"] for o in question["options"]}, ensure_ascii=False)
    elif question["type"] == "date":
        target = f"an ISO date (YYYY-MM-DD) when it started; today is {today.isoformat()}"
    else:
        target = "a temperature in °C (convert °F)"
    prompt = (f"A chemotherapy patient answered a symptom question by voice (speech-to-text, may be Hindi, Telugu or English).\n"
              f"Question: {question['text']['en']}\nPatient said: <<<{heard}>>>\n"
              f"Return ONLY JSON {{\"value\": ...}} where value is {target}, or null if the answer is unclear.")
    try:
        reply = extraction._anthropic_post(extraction.os.environ["ANTHROPIC_API_KEY"],
                                           {"model": extraction.ai_status()["model"], "max_tokens": 60,
                                            "messages": [{"role": "user", "content": prompt}]}, 20)
        raw = "".join(part.get("text", "") for part in reply.get("content", []))
        match = re.search(r"\{.*\}", raw, re.S)
        return json.loads(match.group(0)).get("value") if match else None
    except Exception:
        return None


def find_question(symptom: str, question_id: str) -> dict[str, Any] | None:
    pool = questionnaire.SAFETY_QUESTIONS if symptom == "safety" else questionnaire.questions_for(symptom)
    return next((q for q in pool if q["id"] == question_id), None)


def interpret(symptom: str, question_id: str, heard: str, lang: str = "en", today: date | None = None) -> dict[str, Any]:
    """Map a spoken answer to a value. Returns {value, label, method}; value is None when unclear."""
    today = today or date.today()
    question = find_question(symptom, question_id)
    if not question:
        raise questionnaire.CheckinError("Unknown question")
    text = _norm(heard)[:500]
    value, method = None, "rules"
    if text:
        if question["type"] == "choice":
            value = _choice(question, text)
        elif question["type"] == "date":
            value = _date(text, today)
        else:
            value = _temperature(text)
        if value is None:
            ai = _ai(question, heard[:500], today)
            method = "claude"
            if question["type"] == "choice" and ai in {o["value"] for o in question["options"]}:
                value = ai
            elif question["type"] == "date" and isinstance(ai, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ai) and ai <= today.isoformat():
                value = ai
            elif question["type"] == "number" and isinstance(ai, (int, float)) and 34 <= float(ai) <= 43:
                value = round(float(ai), 1)
    if value is None:
        return {"value": None, "label": None, "method": method}
    if question["type"] == "choice":
        label = next(o["label"] for o in question["options"] if o["value"] == value)
        label = label.get(lang) or label["en"]
    else:
        label = str(value)
    return {"value": value, "label": label, "method": method}

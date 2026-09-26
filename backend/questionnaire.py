"""Patient symptom check-in: a fixed question bank per symptom, in English, Hindi and Telugu.

Each symptom has follow-up questions modelled on NCI PRO-CTCAE (how often / how severe / how much it gets in the way)
plus the specific facts CTCAE grading depends on (e.g. vomiting treatment needed, extra stools per day, whether rest
helps fatigue). A short set of safety questions is asked at every check-in.

`evaluate()` turns answers into an English summary for the clinician, a *suggested* CTCAE grade with the rule that
produced it, and red flags. Grades are suggestions only; the clinician confirms every grade.

Hindi and Telugu wording was written for this prototype and must be reviewed by native speakers before clinical use.
"""
from __future__ import annotations

from datetime import date
from typing import Any

LANGS = ("en", "hi", "te")


def T(en: str, hi: str, te: str) -> dict[str, str]:
    return {"en": en, "hi": hi, "te": te}


def opt(value: str, en: str, hi: str, te: str) -> dict[str, Any]:
    return {"value": value, "label": T(en, hi, te)}


# ---------------------------------------------------------------------------------------------- shared scales
FREQUENCY = [
    opt("never", "Never", "कभी नहीं", "ఎప్పుడూ లేదు"),
    opt("rarely", "Rarely", "बहुत कम", "అరుదుగా"),
    opt("occasionally", "Occasionally", "कभी-कभी", "అప్పుడప్పుడు"),
    opt("frequently", "Frequently", "अक्सर", "తరచుగా"),
    opt("almost_constantly", "Almost all the time", "लगभग हर समय", "దాదాపు ఎల్లప్పుడూ"),
]
SEVERITY = [
    opt("mild", "Mild", "हल्का", "తేలికగా"),
    opt("moderate", "Moderate", "मध्यम", "మధ్యస్థంగా"),
    opt("severe", "Severe", "गंभीर", "తీవ్రంగా"),
    opt("very_severe", "Very severe", "बहुत गंभीर", "చాలా తీవ్రంగా"),
]
INTERFERENCE = [
    opt("none", "I can do my usual activities", "मैं अपने सामान्य काम कर पा रहा/रही हूँ", "నా సాధారణ పనులు చేసుకోగలుగుతున్నాను"),
    opt("instrumental", "It stops me doing some things, like cooking, shopping or work",
        "इसकी वजह से मैं खाना बनाना, खरीदारी या काम जैसे कुछ काम नहीं कर पाता/पाती",
        "దీని వల్ల వంట, షాపింగ్ లేదా ఉద్యోగం వంటి కొన్ని పనులు చేయలేకపోతున్నాను"),
    opt("selfcare", "I need help washing, dressing or eating", "मुझे नहाने, कपड़े पहनने या खाने में मदद चाहिए",
        "స్నానం చేయడానికి, బట్టలు వేసుకోవడానికి లేదా తినడానికి నాకు సహాయం కావాలి"),
]
YES_NO = [opt("yes", "Yes", "हाँ", "అవును"), opt("no", "No", "नहीं", "లేదు")]
EATING = [
    opt("normal", "I eat and drink as usual", "मैं सामान्य रूप से खा-पी रहा/रही हूँ", "మామూలుగా తింటున్నాను, తాగుతున్నాను"),
    opt("less", "I eat less than usual, but I manage", "मैं सामान्य से कम खा रहा/रही हूँ, पर काम चल जाता है",
        "మామూలు కంటే తక్కువ తింటున్నాను, అయినా సరిపోతోంది"),
    opt("very_little", "I can hardly eat or drink anything", "मैं लगभग कुछ भी खा-पी नहीं पा रहा/रही हूँ",
        "దాదాపు ఏమీ తినలేకపోతున్నాను, తాగలేకపోతున్నాను"),
]

Q_FREQ = lambda what_en, what_hi, what_te: {  # noqa: E731
    "id": "frequency", "type": "choice", "options": FREQUENCY, "pro": "frequency",
    "text": T(f"In the last 7 days, how often did you have {what_en}?", f"पिछले 7 दिनों में आपको कितनी बार {what_hi} की तकलीफ़ हुई?",
              f"గత 7 రోజుల్లో {what_te} సమస్య ఎంత తరచుగా వచ్చింది?")}
Q_SEV = lambda what_en, what_hi, what_te: {  # noqa: E731
    "id": "severity", "type": "choice", "options": SEVERITY, "pro": "severity",
    "text": T(f"At its worst, how bad was the {what_en}?", f"सबसे ज़्यादा होने पर {what_hi} की तकलीफ़ कितनी तेज़ थी?",
              f"ఎక్కువగా ఉన్నప్పుడు {what_te} ఎంత తీవ్రంగా ఉంది?")}
Q_ADL = {"id": "interference", "type": "choice", "options": INTERFERENCE, "pro": "interference",
         "text": T("How much does it get in the way of your day?", "इससे आपके रोज़ के कामों पर कितना असर पड़ता है?",
                   "ఇది మీ రోజువారీ పనులకు ఎంత అడ్డుగా ఉంది?")}

# ---------------------------------------------------------------------------------------------- symptom bank
SYMPTOMS: dict[str, dict[str, Any]] = {
    "vomiting": {"term": "Vomiting", "label": T("Vomiting", "उल्टी", "వాంతులు"), "questions": [
        {"id": "episodes", "type": "choice", "text": T("On your worst day, how many times did you vomit in 24 hours?",
                                                        "सबसे खराब दिन पर, 24 घंटे में कितनी बार उल्टी हुई?",
                                                        "ఎక్కువగా ఉన్న రోజున, 24 గంటల్లో ఎన్నిసార్లు వాంతి అయింది?"),
         "options": [opt("1-2", "1–2 times", "1–2 बार", "1–2 సార్లు"), opt("3-5", "3–5 times", "3–5 बार", "3–5 సార్లు"),
                     opt("6+", "6 or more times", "6 या अधिक बार", "6 లేదా అంతకంటే ఎక్కువ సార్లు")]},
        {"id": "fluids", "type": "choice", "text": T("Can you keep water and other drinks down?",
                                                      "क्या पानी और दूसरे पेय पेट में रुक रहे हैं?",
                                                      "నీళ్లు, ఇతర పానీయాలు తాగితే కడుపులో నిలుస్తున్నాయా?"),
         "options": [opt("yes", "Yes", "हाँ", "అవును"), opt("some", "Only some", "सिर्फ़ थोड़ा", "కొంత మాత్రమే"),
                     opt("no", "No, everything comes back up", "नहीं, सब उल्टी में निकल जाता है", "లేదు, అన్నీ వాంతిలో బయటకు వస్తున్నాయి")]},
        {"id": "treatment", "type": "choice", "text": T("What did you need for the vomiting?", "उल्टी के लिए आपको क्या लेना पड़ा?",
                                                         "వాంతుల కోసం మీకు ఏమి అవసరమైంది?"),
         "options": [opt("none", "Nothing", "कुछ नहीं", "ఏమీ లేదు"),
                     opt("usual", "My usual anti-sickness tablets", "मेरी सामान्य उल्टी रोकने की गोलियाँ", "నేను మామూలుగా వాడే వాంతి మాత్రలు"),
                     opt("extra", "Extra or new medicine from a doctor", "डॉक्टर से अतिरिक्त या नई दवा", "డాక్టర్ ఇచ్చిన అదనపు లేదా కొత్త మందు"),
                     opt("iv", "A drip (IV fluids) at a hospital or clinic", "अस्पताल या क्लिनिक में ड्रिप (IV)",
                         "ఆసుపత్రి లేదా క్లినిక్‌లో సెలైన్ (IV)"),
                     opt("admitted", "I was admitted to hospital", "अस्पताल में भर्ती होना पड़ा", "ఆసుపత్రిలో చేరాల్సి వచ్చింది")]},
        Q_FREQ("vomiting", "उल्टी", "వాంతులు"),
    ]},
    "nausea": {"term": "Nausea", "label": T("Nausea (feeling sick)", "जी मिचलाना", "వికారం (కడుపులో తిప్పడం)"), "questions": [
        Q_SEV("nausea", "जी मिचलाना", "వికారం"),
        {"id": "eating", "type": "choice", "options": EATING,
         "text": T("How is the nausea affecting your eating and drinking?", "जी मिचलाने से आपके खाने-पीने पर क्या असर पड़ा है?",
                   "వికారం వల్ల మీ తినడం, తాగడంపై ఎలాంటి ప్రభావం ఉంది?")},
        Q_FREQ("nausea", "जी मिचलाना", "వికారం"),
    ]},
    "diarrhoea": {"term": "Diarrhea", "label": T("Loose stools (diarrhoea)", "दस्त (पतला मल)", "విరేచనాలు (నీళ్ల విరేచనాలు)"), "questions": [
        {"id": "extra_stools", "type": "choice",
         "text": T("On your worst day, how many MORE times than usual did you pass stool?",
                   "सबसे खराब दिन पर, आपको सामान्य से कितनी बार ज़्यादा शौच जाना पड़ा?",
                   "ఎక్కువగా ఉన్న రోజున, మామూలు కంటే ఎన్నిసార్లు ఎక్కువగా మల విసర్జనకు వెళ్లాల్సి వచ్చింది?"),
         "options": [opt("1-3", "1–3 more times", "1–3 बार ज़्यादा", "1–3 సార్లు ఎక్కువ"),
                     opt("4-6", "4–6 more times", "4–6 बार ज़्यादा", "4–6 సార్లు ఎక్కువ"),
                     opt("7+", "7 or more times more", "7 या अधिक बार ज़्यादा", "7 లేదా అంతకంటే ఎక్కువ సార్లు ఎక్కువ")]},
        {"id": "blood", "type": "choice", "options": YES_NO,
         "text": T("Is there blood in the stool, or is it black?", "क्या मल में खून है, या मल काला है?",
                   "మలంలో రక్తం ఉందా, లేదా మలం నల్లగా ఉందా?")},
        Q_ADL,
    ]},
    "constipation": {"term": "Constipation", "label": T("Constipation", "कब्ज़", "మలబద్ధకం"), "questions": [
        {"id": "last_stool", "type": "choice", "text": T("When did you last pass stool?", "आखिरी बार शौच कब हुआ था?",
                                                          "చివరిసారి మల విసర్జన ఎప్పుడు అయింది?"),
         "options": [opt("0-1", "Today or yesterday", "आज या कल", "ఈరోజు లేదా నిన్న"),
                     opt("2-3", "2–3 days ago", "2–3 दिन पहले", "2–3 రోజుల క్రితం"),
                     opt("4+", "4 or more days ago", "4 या अधिक दिन पहले", "4 లేదా అంతకంటే ఎక్కువ రోజుల క్రితం")]},
        {"id": "laxatives", "type": "choice",
         "text": T("Are you using laxatives or enemas?", "क्या आप जुलाब (पेट साफ़ करने की दवा) या एनीमा ले रहे हैं?",
                   "మీరు విరేచన మందులు (లాక్సేటివ్) లేదా ఎనిమా వాడుతున్నారా?"),
         "options": [opt("none", "No", "नहीं", "లేదు"), opt("sometimes", "Sometimes", "कभी-कभी", "అప్పుడప్పుడు"),
                     opt("regular", "Regularly (every day)", "नियमित रूप से (रोज़)", "క్రమం తప్పకుండా (రోజూ)")]},
        Q_ADL,
    ]},
    "mouth_sores": {"term": "Mucositis oral", "label": T("Mouth sores or mouth pain", "मुंह में छाले या दर्द", "నోటి పుండ్లు లేదా నోటి నొప్పి"), "questions": [
        Q_SEV("mouth pain", "मुंह का दर्द", "నోటి నొప్పి"),
        {"id": "eating", "type": "choice",
         "text": T("Can you eat and drink with the mouth sores?", "छालों के साथ क्या आप खा-पी पाते हैं?",
                   "నోటి పుండ్లతో మీరు తినగలుగుతున్నారా, తాగగలుగుతున్నారా?"),
         "options": [opt("normal", "Yes, as usual", "हाँ, सामान्य रूप से", "అవును, మామూలుగా"),
                     opt("soft", "Only soft food", "सिर्फ़ नरम खाना", "మెత్తని ఆహారం మాత్రమే"),
                     opt("cannot", "The pain stops me eating or drinking", "दर्द की वजह से खा-पी नहीं पाता/पाती",
                         "నొప్పి వల్ల తినలేకపోతున్నాను, తాగలేకపోతున్నాను")]},
    ]},
    "tingling": {"term": "Peripheral sensory neuropathy",
                 "label": T("Tingling or numbness in hands or feet", "हाथ-पैरों में झुनझुनी या सुन्नपन", "చేతులు లేదా కాళ్లలో తిమ్మిర్లు, స్పర్శ తెలియకపోవడం"),
                 "questions": [
        Q_SEV("tingling or numbness", "झुनझुनी या सुन्नपन", "తిమ్మిర్లు"),
        {"id": "constant", "type": "choice", "text": T("Is it there all the time?", "क्या यह हर समय रहता है?", "ఇది ఎప్పుడూ ఉంటుందా?"),
         "options": [opt("comes_goes", "It comes and goes", "आता-जाता है", "వస్తూ పోతూ ఉంటుంది"),
                     opt("constant", "It is there all the time", "हर समय रहता है", "ఎప్పుడూ ఉంటుంది")]},
        {"id": "fine_tasks", "type": "choice", "options": YES_NO,
         "text": T("Do you have trouble with buttons, writing or picking up small things?",
                   "क्या बटन लगाने, लिखने या छोटी चीज़ें उठाने में परेशानी होती है?",
                   "బటన్లు పెట్టుకోవడం, రాయడం లేదా చిన్న వస్తువులు పట్టుకోవడంలో ఇబ్బంది ఉందా?")},
        Q_ADL,
    ]},
    "ringing_ears": {"term": "Tinnitus", "label": T("Ringing in the ears", "कानों में आवाज़ (घंटी जैसी)", "చెవుల్లో హోరు లేదా గంట శబ్దం"), "questions": [
        Q_SEV("ringing", "कानों की आवाज़", "చెవుల్లో శబ్దం"), Q_ADL,
    ]},
    "hearing": {"term": "Hearing impaired", "label": T("Change in hearing", "सुनने में बदलाव", "వినికిడిలో మార్పు"), "questions": [
        {"id": "conversation", "type": "choice",
         "text": T("Do you have trouble following conversations or the TV?", "क्या बातचीत या टीवी सुनने-समझने में परेशानी होती है?",
                   "మాటలు లేదా టీవీ వినడంలో ఇబ్బంది ఉందా?"),
         "options": [opt("no", "No", "नहीं", "లేదు"), opt("sometimes", "Sometimes", "कभी-कभी", "అప్పుడప్పుడు"),
                     opt("often", "Often", "अक्सर", "తరచుగా")]},
        Q_ADL,
    ]},
    "tiredness": {"term": "Fatigue", "label": T("Tiredness", "थकान", "అలసట"), "questions": [
        Q_SEV("tiredness", "थकान", "అలసట"),
        {"id": "rest_helps", "type": "choice", "options": YES_NO,
         "text": T("Does rest make the tiredness better?", "क्या आराम करने से थकान कम होती है?", "విశ్రాంతి తీసుకుంటే అలసట తగ్గుతుందా?")},
        Q_ADL,
    ]},
    "appetite": {"term": "Anorexia", "label": T("Loss of appetite", "भूख न लगना", "ఆకలి లేకపోవడం"), "questions": [
        {"id": "eating", "type": "choice", "options": EATING,
         "text": T("How much are you eating?", "आप कितना खा पा रहे हैं?", "మీరు ఎంత తింటున్నారు?")},
        {"id": "weight", "type": "choice", "text": T("Have you lost weight?", "क्या आपका वज़न कम हुआ है?", "మీ బరువు తగ్గిందా?"),
         "options": [opt("no", "No", "नहीं", "లేదు"), opt("unsure", "Not sure", "पता नहीं", "తెలియదు"),
                     opt("little", "Yes, a little", "हाँ, थोड़ा", "అవును, కొంచెం"), opt("lot", "Yes, a lot", "हाँ, काफ़ी", "అవును, చాలా")]},
    ]},
    "fever": {"term": "Fever", "label": T("Fever or chills", "बुखार या ठंड लगना", "జ్వరం లేదా చలి"), "questions": [
        {"id": "max_temp", "type": "number", "min": 35, "max": 43, "step": 0.1, "optional": True,
         "text": T("What was your highest temperature (°C)? Leave blank if you did not measure it.",
                   "आपका सबसे ज़्यादा तापमान कितना था (°C)? अगर नापा नहीं तो खाली छोड़ दें।",
                   "మీ అత్యధిక ఉష్ణోగ్రత ఎంత (°C)? కొలవకపోతే ఖాళీగా వదిలేయండి.")},
        {"id": "chills", "type": "choice", "options": YES_NO,
         "text": T("Did you have shivering or chills?", "क्या आपको कंपकंपी या ठंड लगी?", "మీకు వణుకు లేదా చలి వచ్చిందా?")},
    ]},
    "rash": {"term": "Rash maculo-papular", "label": T("Skin rash", "त्वचा पर दाने", "చర్మంపై దద్దుర్లు"), "questions": [
        {"id": "extent", "type": "choice",
         "text": T("How much of your body does the rash cover?", "दाने शरीर के कितने हिस्से पर हैं?", "దద్దుర్లు శరీరంలో ఎంత భాగంలో ఉన్నాయి?"),
         "options": [opt("small", "Only a small area", "सिर्फ़ थोड़ी सी जगह पर", "కొద్ది ప్రదేశంలో మాత్రమే"),
                     opt("several", "Several parts of the body", "शरीर के कई हिस्सों पर", "శరీరంలో చాలా చోట్ల"),
                     opt("most", "Most of the body", "लगभग पूरे शरीर पर", "దాదాపు శరీరమంతా")]},
        Q_SEV("itching or burning", "खुजली या जलन", "దురద లేదా మంట"), Q_ADL,
    ]},
    "hand_foot": {"term": "Palmar-plantar erythrodysesthesia syndrome",
                  "label": T("Red, sore or peeling palms or soles", "हथेलियों या तलवों में लाली, दर्द या छिलना", "అరచేతులు లేదా అరికాళ్లలో ఎరుపు, నొప్పి, చర్మం ఊడటం"),
                  "questions": [
        {"id": "pain", "type": "choice", "options": YES_NO, "text": T("Is it painful?", "क्या इसमें दर्द है?", "నొప్పిగా ఉందా?")},
        {"id": "peeling", "type": "choice", "options": YES_NO,
         "text": T("Are there blisters, peeling or cracks?", "क्या छाले, त्वचा का छिलना या दरारें हैं?", "బొబ్బలు, చర్మం ఊడటం లేదా పగుళ్లు ఉన్నాయా?")},
        Q_ADL,
    ]},
    "muscle_pain": {"term": "Myalgia", "label": T("Muscle aches", "मांसपेशियों में दर्द", "కండరాల నొప్పులు"), "questions": [
        Q_SEV("muscle pain", "मांसपेशियों का दर्द", "కండరాల నొప్పి"), Q_ADL]},
    "joint_pain": {"term": "Arthralgia", "label": T("Joint pain", "जोड़ों में दर्द", "కీళ్ల నొప్పులు"), "questions": [
        Q_SEV("joint pain", "जोड़ों का दर्द", "కీళ్ల నొప్పి"), Q_ADL]},
    "swelling": {"term": "Edema limbs", "label": T("Swelling of legs or arms", "पैरों या हाथों में सूजन", "కాళ్లు లేదా చేతుల వాపు"), "questions": [
        {"id": "visible", "type": "choice",
         "text": T("How noticeable is the swelling?", "सूजन कितनी दिखाई देती है?", "వాపు ఎంత స్పష్టంగా కనిపిస్తోంది?"),
         "options": [opt("slight", "Slight, I only notice it up close", "थोड़ी, ध्यान से देखने पर ही", "కొంచెం, దగ్గరగా చూస్తేనే తెలుస్తుంది"),
                     opt("clear", "Clearly visible", "साफ़ दिखाई देती है", "స్పష్టంగా కనిపిస్తోంది"),
                     opt("large", "Very large, the shape of the limb has changed", "बहुत ज़्यादा, हाथ/पैर का आकार बदल गया है",
                         "చాలా ఎక్కువగా, కాలు/చేయి ఆకారమే మారిపోయింది")]},
        Q_ADL,
    ]},
    "taste": {"term": "Dysgeusia", "label": T("Change in taste", "स्वाद में बदलाव", "రుచిలో మార్పు"), "questions": [
        {"id": "diet_change", "type": "choice", "options": YES_NO,
         "text": T("Has it changed what you eat?", "क्या इससे आपका खाना बदल गया है?", "దీని వల్ల మీరు తినే ఆహారం మారిందా?")}]},
    "hair_loss": {"term": "Alopecia", "label": T("Hair loss", "बाल झड़ना", "జుట్టు రాలడం"), "questions": [
        {"id": "amount", "type": "choice", "text": T("How much hair have you lost?", "आपके कितने बाल झड़े हैं?", "మీ జుట్టు ఎంత రాలింది?"),
         "options": [opt("some", "Some, only noticeable up close", "थोड़े, पास से देखने पर ही पता चलता है", "కొంత, దగ్గరగా చూస్తేనే తెలుస్తుంది"),
                     opt("lot", "A lot, others can easily see it", "काफ़ी, दूसरों को आसानी से दिखता है", "చాలా, ఇతరులకు సులభంగా కనిపిస్తుంది")]}]},
    "breathless": {"term": "Dyspnea", "label": T("Shortness of breath", "सांस फूलना", "ఊపిరి ఆడకపోవడం"), "questions": [
        {"id": "when", "type": "choice", "text": T("When do you get short of breath?", "सांस कब फूलती है?", "ఊపిరి ఎప్పుడు ఆడటం లేదు?"),
         "options": [opt("moderate", "When walking fast or climbing stairs", "तेज़ चलने या सीढ़ियाँ चढ़ने पर", "వేగంగా నడిచినప్పుడు లేదా మెట్లు ఎక్కినప్పుడు"),
                     opt("minimal", "With light activity, like walking slowly or dressing", "हल्के काम में, जैसे धीरे चलना या कपड़े पहनना",
                         "నెమ్మదిగా నడవడం, బట్టలు వేసుకోవడం వంటి చిన్న పనులకే"),
                     opt("rest", "Even when resting", "आराम करते समय भी", "విశ్రాంతిగా ఉన్నప్పుడు కూడా")]},
        {"id": "chest_pain", "type": "choice", "options": YES_NO,
         "text": T("Do you have chest pain?", "क्या सीने में दर्द है?", "ఛాతీ నొప్పి ఉందా?")},
    ]},
    "bleeding": {"term": None, "label": T("Bleeding or bruising", "खून बहना या नील पड़ना", "రక్తస్రావం లేదా కమిలిన మచ్చలు"), "questions": [
        {"id": "where", "type": "choice", "text": T("Where is the bleeding?", "खून कहाँ से आ रहा है?", "రక్తం ఎక్కడ నుంచి వస్తోంది?"),
         "options": [opt("gums_nose", "Gums or nose", "मसूड़े या नाक", "చిగుళ్లు లేదా ముక్కు"),
                     opt("urine", "In urine", "पेशाब में", "మూత్రంలో"),
                     opt("stool", "In stool, or black stool", "मल में, या काला मल", "మలంలో, లేదా నల్లని మలం"),
                     opt("vomit", "In vomit", "उल्टी में", "వాంతిలో"),
                     opt("bruises", "Bruises without injury", "बिना चोट के नील", "దెబ్బ లేకుండానే కమిలిన మచ్చలు")]},
        {"id": "stopping", "type": "choice", "options": YES_NO,
         "text": T("Does it stop on its own within 10 minutes?", "क्या यह 10 मिनट में अपने आप रुक जाता है?", "ఇది 10 నిమిషాల్లో దానంతట అదే ఆగిపోతుందా?")},
    ]},
    "urine_blood": {"term": "Cystitis noninfective", "label": T("Pain or blood when passing urine", "पेशाब में जलन या खून", "మూత్రంలో మంట లేదా రక్తం"),
                    "questions": [Q_SEV("pain or burning", "दर्द या जलन", "నొప్పి లేదా మంట"),
                                  {"id": "visible_blood", "type": "choice", "options": YES_NO,
                                   "text": T("Can you see blood in the urine?", "क्या पेशाब में खून दिखता है?", "మూత్రంలో రక్తం కనిపిస్తోందా?")}]},
}
GENERIC_QUESTIONS = [Q_SEV("symptom", "तकलीफ़", "సమస్య"), Q_ADL]

ONSET_QUESTION = {"id": "onset", "type": "date", "optional": True,
                  "text": T("When did it start?", "यह कब शुरू हुआ?", "ఇది ఎప్పుడు మొదలైంది?")}

SAFETY_QUESTIONS = [
    {"id": "fever", "type": "choice", "flag": ("yes", "fever", "Temperature 38 °C or more, or chills"),
     "text": T("Have you had a temperature of 38 °C or more, or shivering/chills?",
               "क्या आपको 38 °C या उससे ज़्यादा बुखार, या कंपकंपी/ठंड लगी है?",
               "మీకు 38 °C లేదా అంతకంటే ఎక్కువ జ్వరం, లేదా వణుకు/చలి వచ్చిందా?"),
     "options": [opt("yes", "Yes", "हाँ", "అవును"), opt("no", "No", "नहीं", "లేదు"), opt("not_measured", "I did not measure", "नापा नहीं", "కొలవలేదు")]},
    {"id": "fluids", "type": "choice", "flag": ("no", "fluids", "Cannot drink or keep fluids down"),
     "text": T("Can you drink and keep fluids down?", "क्या आप पानी पी पा रहे हैं और वह पेट में रुकता है?",
               "మీరు నీళ్లు తాగగలుగుతున్నారా, అవి కడుపులో నిలుస్తున్నాయా?"), "options": YES_NO},
    {"id": "urine", "type": "choice", "flag": ("none", "fluids", "Not passing urine"),
     "text": T("Are you passing urine as usual?", "क्या आपको सामान्य रूप से पेशाब हो रहा है?", "మూత్రం మామూలుగా వస్తుందా?"),
     "options": [opt("yes", "Yes", "हाँ", "అవును"), opt("less", "Less than usual", "सामान्य से कम", "మామూలు కంటే తక్కువగా"),
                 opt("none", "Not at all", "बिल्कुल नहीं", "అస్సలు రావడం లేదు")]},
    {"id": "bleeding", "type": "choice", "flag": ("yes", "bleeding", "Bleeding, black stools or unusual bruising"),
     "text": T("Any bleeding, black stools, or unusual bruising?", "क्या कहीं से खून आना, काला मल, या असामान्य नील पड़ना है?",
               "ఏదైనా రక్తస్రావం, నల్లని మలం, లేదా అసాధారణంగా కమిలిన మచ్చలు ఉన్నాయా?"), "options": YES_NO},
    {"id": "breathing", "type": "choice", "flag": ("yes", "breathing", "Shortness of breath or chest pain"),
     "text": T("Any shortness of breath or chest pain?", "क्या सांस फूलना या सीने में दर्द है?", "ఊపిరి ఆడకపోవడం లేదా ఛాతీ నొప్పి ఉందా?"),
     "options": YES_NO},
]


def questions_for(key: str) -> list[dict[str, Any]]:
    spec = SYMPTOMS.get(key)
    return [ONSET_QUESTION] + (spec["questions"] if spec else GENERIC_QUESTIONS)


def bank() -> dict[str, Any]:
    """Everything the portal needs to render the check-in."""
    return {"languages": list(LANGS),
            "symptoms": [{"key": k, "label": v["label"], "questions": questions_for(k)} for k, v in SYMPTOMS.items()],
            "generic_questions": [ONSET_QUESTION] + GENERIC_QUESTIONS,
            "safety": [{k: v for k, v in q.items() if k != "flag"} for q in SAFETY_QUESTIONS]}


# ---------------------------------------------------------------------------------------------- evaluation
class CheckinError(ValueError):
    pass


def _clean(questions: list[dict[str, Any]], raw: dict[str, Any], where: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for q in questions:
        value = (raw or {}).get(q["id"])
        if value in (None, ""):
            continue
        if q["type"] == "choice":
            if value not in {o["value"] for o in q["options"]}:
                raise CheckinError(f"Unexpected answer for {where}")
        elif q["type"] == "number":
            try:
                value = round(float(value), 1)
            except (TypeError, ValueError):
                raise CheckinError("Temperature must be a number in °C.") from None
            if not q["min"] <= value <= q["max"]:
                raise CheckinError("Enter your temperature in °C (for example 37.8).")
        elif q["type"] == "date":
            try:
                value = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise CheckinError("Choose a valid start date.") from None
            if value > date.today().isoformat():
                raise CheckinError("The start date cannot be in the future.")
        out[q["id"]] = value
    return out


def _label(question: dict[str, Any], value: Any) -> str:
    if question["type"] == "choice":
        return next(o["label"]["en"] for o in question["options"] if o["value"] == value)
    if question["type"] == "number":
        return f"{value} °C"
    return str(value)


ADL_TEXT = {"none": "no limitation of daily activities", "instrumental": "limiting instrumental ADL", "selfcare": "limiting self-care ADL"}
SEV_RANK = {"mild": 1, "moderate": 2, "severe": 3, "very_severe": 3}


def _grade(key: str, a: dict[str, Any]) -> tuple[int | None, str, list[tuple[str, str]]]:
    """Suggested CTCAE grade, the reason, and red flags [(code, label)] for one symptom's answers."""
    adl = a.get("interference")
    adl_grade = {"none": 1, "instrumental": 2, "selfcare": 3}.get(adl)
    sev = SEV_RANK.get(a.get("severity"))
    flags: list[tuple[str, str]] = []
    if key == "vomiting":
        if a.get("fluids") == "no" or a.get("episodes") == "6+":
            flags.append(("fluids", "Vomiting: cannot keep fluids down or ≥6 episodes/24 h"))
        t = a.get("treatment")
        if t == "admitted":
            return 3, "Hospital admission for vomiting (G3: hospitalization indicated)", flags
        if a.get("fluids") == "no":
            return 3, "Cannot keep fluids down; hospitalization or IV support may be indicated (G3 criterion is clinician judgement)", flags
        if t in ("iv", "extra"):
            return 2, "Needed " + ("outpatient IV fluids" if t == "iv" else "additional anti-emetic treatment") + " (G2: outpatient IV hydration; medical intervention indicated)", flags
        if t in ("none", "usual"):
            return 1, "No additional intervention needed (G1: intervention not indicated)", flags
        return None, "Treatment needed not reported", flags
    if key in ("nausea", "appetite"):
        eat = a.get("eating")
        if key == "appetite" and a.get("weight") == "lot":
            return 3, "Reports significant weight loss (G3: significant weight loss or malnutrition)", flags
        if eat == "very_little":
            flags.append(("fluids", "Hardly eating or drinking"))
            return 3, "Hardly eating or drinking (G3: inadequate oral caloric or fluid intake)", flags
        if eat == "less":
            return 2, "Eating less than usual (G2: oral intake decreased/altered)", flags
        if eat == "normal":
            return 1, "Eating as usual (G1: no alteration in eating habits)", flags
        return None, "Effect on eating not reported", flags
    if key == "diarrhoea":
        if a.get("blood") == "yes":
            flags.append(("bleeding", "Blood in stool or black stool"))
        n = a.get("extra_stools")
        if n == "7+" or adl == "selfcare":
            flags.append(("diarrhoea", "Severe diarrhoea (≥7 extra stools/day or needs help with self-care)"))
            return 3, "≥7 stools/day over baseline or limiting self-care ADL (G3)", flags
        if n == "4-6" or adl == "instrumental":
            return 2, "4–6 stools/day over baseline or limiting instrumental ADL (G2)", flags
        if n == "1-3":
            return 1, "<4 stools/day over baseline (G1)", flags
        return None, "Stool frequency not reported", flags
    if key == "constipation":
        if adl == "selfcare":
            return 3, "Limiting self-care ADL (G3; manual evacuation not assessed)", flags
        if a.get("laxatives") == "regular" or adl == "instrumental":
            return 2, "Regular laxative use or limiting instrumental ADL (G2)", flags
        return 1, "Occasional symptoms / occasional laxatives (G1)", flags
    if key == "mouth_sores":
        if a.get("eating") == "cannot":
            flags.append(("fluids", "Mouth pain stops eating or drinking"))
            return 3, "Severe pain interfering with oral intake (G3)", flags
        if a.get("eating") == "soft" or (sev or 0) >= 2:
            return 2, "Moderate pain or modified diet needed (G2)", flags
        return 1, "Mild symptoms, eating normally (G1)", flags
    if key == "tiredness":
        if adl == "selfcare":
            return 3, "Not relieved by rest and limiting self-care ADL (G3)" if a.get("rest_helps") == "no" else "Limiting self-care ADL (G3 requires not relieved by rest; confirm)", flags
        if a.get("rest_helps") == "no" or adl == "instrumental":
            return 2, "Not relieved by rest and/or limiting instrumental ADL (G2)", flags
        if a.get("rest_helps") == "yes":
            return 1, "Relieved by rest (G1)", flags
        return adl_grade, f"Based on daily-activity impact: {ADL_TEXT.get(adl, 'not reported')}", flags
    if key == "fever":
        t = a.get("max_temp")
        flags.append(("fever", "Fever or chills during chemotherapy"))
        if t is None:
            return None, "Temperature not measured; grade needs a measured temperature", flags
        if t > 40:
            return 3, f"{t} °C (>40.0 °C: G3 if ≤24 h, G4 if >24 h)", flags
        if t > 39:
            return 2, f"{t} °C (>39.0–40.0 °C: G2)", flags
        if t >= 38:
            return 1, f"{t} °C (38.0–39.0 °C: G1)", flags
        return None, f"{t} °C is below the CTCAE fever threshold (38.0 °C)", flags
    if key == "rash":
        ext = a.get("extent")
        if ext == "most" and ((sev or 0) >= 2 or adl == "selfcare"):
            return 3, "Most of body with moderate/severe symptoms or limiting self-care ADL (G3; >30% BSA)", flags
        if ext in ("several", "most") or adl == "instrumental":
            return 2, "Several areas / widespread or limiting instrumental ADL (G2; ~10–30% BSA or >30% with mild symptoms)", flags
        return 1, "Small area (G1; <10% BSA)", flags
    if key == "hand_foot":
        if a.get("pain") == "yes" and adl == "selfcare":
            return 3, "Painful skin changes limiting self-care ADL (G3)", flags
        if a.get("pain") == "yes":
            return 2, "Skin changes with pain (G2)", flags
        return 1, "Skin changes without pain (G1)", flags
    if key == "tingling":
        if adl == "selfcare":
            return 3, "Severe symptoms limiting self-care ADL (G3)", flags
        if adl == "instrumental" or a.get("fine_tasks") == "yes":
            return 2, "Moderate symptoms limiting instrumental ADL, e.g. buttons or writing (G2)", flags
        return 1, "Symptoms without functional limitation (G1 in practice; v5 G1 wording is 'asymptomatic')", flags
    if key == "hearing":
        if adl == "selfcare":
            return 3, "Limiting self-care ADL (G3)", flags
        if adl == "instrumental" or a.get("conversation") == "often":
            return 2, "Hearing loss limiting instrumental ADL (G2); audiometry needed to confirm", flags
        return 1, "Subjective change in hearing without documented loss (G1)", flags
    if key == "swelling":
        if a.get("visible") == "large" or adl == "selfcare":
            return 3, "Gross deviation from normal contour or limiting self-care ADL (G3; measure to confirm)", flags
        if a.get("visible") == "clear" or adl == "instrumental":
            return 2, "Readily apparent swelling or limiting instrumental ADL (G2; measure to confirm)", flags
        return 1, "Swelling only on close inspection (G1)", flags
    if key == "taste":
        return (2, "Change in diet (G2)", flags) if a.get("diet_change") == "yes" else (1, "No change in diet (G1)", flags) if a.get("diet_change") == "no" else (None, "Diet change not reported", flags)
    if key == "hair_loss":
        return (2, "≥50% hair loss, readily apparent (G2)", flags) if a.get("amount") == "lot" else (1, "<50% hair loss (G1)", flags) if a.get("amount") == "some" else (None, "Amount not reported", flags)
    if key == "breathless":
        flags.append(("breathing", "Breathing difficulty or chest pain"))
        w = a.get("when")
        g = {"moderate": 1, "minimal": 2, "rest": 3}.get(w)
        return g, {1: "Short of breath with moderate exertion (G1)", 2: "Short of breath with minimal exertion (G2)",
                   3: "Short of breath at rest (G3)"}.get(g, "Exertion level not reported"), flags
    if key == "bleeding":
        flags.append(("bleeding", "Bleeding or unexplained bruising"))
        return None, "Choose the CTCAE bleeding term by site (e.g. epistaxis, hematuria, GI hemorrhage)", flags
    if key == "urine_blood":
        if a.get("visible_blood") == "yes":
            return 2, "Visible blood in urine (G2 cystitis: moderate hematuria)", flags
        return (sev and min(sev, 3)), "Based on severity of urinary symptoms", flags
    # pain-type and generic symptoms: severity + ADL
    grades = [g for g in (adl_grade, sev) if g]
    if not grades:
        return None, "Severity and impact not reported", flags
    g = max(grades)
    return g, f"Severity {a.get('severity', 'not reported')}, {ADL_TEXT.get(adl, 'impact not reported')}", flags


def evaluate(selected: list[str], answers: dict[str, Any], safety: dict[str, Any]) -> dict[str, Any]:
    """Validate answers and build the clinician-facing summary. Raises CheckinError on invalid input."""
    items, flags = [], []
    for key in selected:
        questions = questions_for(key)
        clean = _clean(questions, (answers or {}).get(key) or {}, key)
        spec = SYMPTOMS.get(key)
        grade, rationale, item_flags = _grade(key, clean)
        term = spec["term"] if spec else None
        items.append({
            "key": key, "term": term, "label": spec["label"]["en"] if spec else key.replace("_", " ").capitalize(),
            "onset": clean.get("onset"),
            "answers": [{"id": q["id"], "question": q["text"]["en"], "answer": _label(q, clean[q["id"]]), "value": clean[q["id"]]}
                        for q in questions if q["id"] in clean and q["id"] != "onset"],
            "pro": {k: clean.get(k) for k in ("frequency", "severity", "interference") if clean.get(k)},
            "grade_suggestion": grade if term else None, "rationale": rationale,
        })
        flags += item_flags
    safety_clean = _clean(SAFETY_QUESTIONS, safety or {}, "safety")
    safety_out = []
    for q in SAFETY_QUESTIONS:
        if q["id"] in safety_clean:
            value = safety_clean[q["id"]]
            safety_out.append({"id": q["id"], "question": q["text"]["en"], "answer": _label(q, value), "value": value})
            trigger, code, label = q["flag"]
            if value == trigger:
                flags.append((code, label))
    seen, red_flags = set(), []
    for code, label in flags:
        if code not in seen:
            seen.add(code)
            red_flags.append({"code": code, "label": label, "why": "Check-in answer"})
    adl_levels = [i["pro"].get("interference") for i in items if i["pro"].get("interference")]
    impact = "selfcare" if "selfcare" in adl_levels else "some" if "instrumental" in adl_levels else "none" if adl_levels else None
    temps = [a["value"] for i in items for a in i["answers"] if a["id"] == "max_temp"]
    onsets = [i["onset"] for i in items if i["onset"]]
    return {"items": items, "safety": safety_out, "red_flags": red_flags, "daily_impact": impact,
            "temperature": max(temps) if temps else None, "onset": min(onsets) if onsets else None}


def summary_text(checkin: dict[str, Any]) -> str:
    """Plain-English summary used in AI prompts and in the clinician's CTCAE note."""
    parts = []
    for item in checkin.get("items", []):
        answers = "; ".join(f"{a['question']} {a['answer']}" for a in item["answers"])
        parts.append(f"{item['label']}" + (f" (since {item['onset']})" if item.get("onset") else "") + (f": {answers}" if answers else ""))
    if checkin.get("safety"):
        parts.append("Safety: " + "; ".join(f"{a['question']} {a['answer']}" for a in checkin["safety"]))
    return "\n".join(parts)

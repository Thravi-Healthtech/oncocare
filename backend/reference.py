"""Stored clinical reference data for the prototype.

CTCAE criteria below are transcribed from CTCAE v5.0 (NCI, 27 Nov 2017) for the subset
of terms the prototype supports. They are displayed verbatim to the clinician and used for
deterministic lab grading only. Nothing here is generated at runtime.

Drug -> adverse event associations are a curated shortlist of commonly labelled toxicities
used only to order/shortlist terms and to annotate "known association". They are not a
causality assessment.
"""
from __future__ import annotations

CTCAE_VERSION = "6.0"
CTCAE_SOURCE = "NCI Common Terminology Criteria for Adverse Events v6.0 (published 2025-07-22)"
# Provenance of the built-in criteria text. The official NCI v6.0 workbook can be imported with
# `python3 -m backend.context import-ctcae <file.xlsx>`; imported text then replaces these entries.
V6_VERIFIED = {
    # Checked against the NCI v6.0 PDF or the PharmaSUG 2026 DS-131 v6.0 review.
    "Anemia", "Febrile neutropenia", "Alanine aminotransferase increased",
}
CTCAE_SOURCE_OVERRIDES = {
    "Aspartate aminotransferase increased": "CTCAE v6.0 wording mirrored from ALT (PharmaSUG 2026 DS-131 reports the same change); confirm with the NCI workbook",
}
V6_BASELINE_RULES = {"Alanine aminotransferase increased", "Aspartate aminotransferase increased"}

# ---------------------------------------------------------------------------
# CTCAE terms: clinician-graded (symptom) terms and lab-derived terms.
# ---------------------------------------------------------------------------
CTCAE_TERMS: dict[str, dict] = {
    # Gastrointestinal / general
    "Nausea": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Loss of appetite without alteration in eating habits",
        2: "Oral intake decreased without significant weight loss, dehydration or malnutrition",
        3: "Inadequate oral caloric or fluid intake; tube feeding, TPN, or hospitalization indicated"}},
    "Vomiting": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Intervention not indicated",
        2: "Outpatient IV hydration; medical intervention indicated",
        3: "Tube feeding, TPN, or hospitalization indicated",
        4: "Life-threatening consequences",
        5: "Death"}},
    "Diarrhea": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Increase of <4 stools per day over baseline; mild increase in ostomy output compared to baseline",
        2: "Increase of 4 - 6 stools per day over baseline; moderate increase in ostomy output compared to baseline; limiting instrumental ADL",
        3: "Increase of >=7 stools per day over baseline; hospitalization indicated; severe increase in ostomy output compared to baseline; limiting self care ADL",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Constipation": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Occasional or intermittent symptoms; occasional use of stool softeners, laxatives, dietary modification, or enema",
        2: "Persistent symptoms with regular use of laxatives or enemas; limiting instrumental ADL",
        3: "Obstipation with manual evacuation indicated; limiting self care ADL",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Mucositis oral": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Asymptomatic or mild symptoms; intervention not indicated",
        2: "Moderate pain or ulcer that does not interfere with oral intake; modified diet indicated",
        3: "Severe pain; interfering with oral intake",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Anorexia": {"category": "Metabolism and nutrition disorders", "kind": "clinical", "grades": {
        1: "Loss of appetite without alteration in eating habits",
        2: "Oral intake altered without significant weight loss or malnutrition; oral nutritional supplements indicated",
        3: "Associated with significant weight loss or malnutrition (e.g., inadequate oral caloric and/or fluid intake); tube feeding or TPN indicated",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Dysgeusia": {"category": "Nervous system disorders", "kind": "clinical", "grades": {
        1: "Altered taste but no change in diet",
        2: "Altered taste with change in diet (e.g., oral supplements); noxious or unpleasant taste; loss of taste"}},
    "Fatigue": {"category": "General disorders", "kind": "clinical", "grades": {
        1: "Fatigue relieved by rest",
        2: "Fatigue not relieved by rest; limiting instrumental ADL",
        3: "Fatigue not relieved by rest, limiting self care ADL"}},
    "Fever": {"category": "General disorders", "kind": "clinical", "grades": {
        1: "38.0 - 39.0 degrees C (100.4 - 102.2 degrees F)",
        2: ">39.0 - 40.0 degrees C (102.3 - 104.0 degrees F)",
        3: ">40.0 degrees C (>104.0 degrees F) for <=24 hrs",
        4: ">40.0 degrees C (>104.0 degrees F) for >24 hrs",
        5: "Death"}},
    "Edema limbs": {"category": "General disorders", "kind": "clinical", "grades": {
        1: "5 - 10% inter-limb discrepancy in volume or circumference at point of greatest visible difference; swelling or obscuration of anatomic architecture on close inspection",
        2: ">10 - 30% inter-limb discrepancy in volume or circumference at point of greatest visible difference; readily apparent obscuration of anatomic architecture; obliteration of skin folds; readily apparent deviation from normal anatomic contour; limiting instrumental ADL",
        3: ">30% inter-limb discrepancy in volume; gross deviation from normal anatomic contour; limiting self care ADL"}},
    "Infusion related reaction": {"category": "General disorders", "kind": "clinical", "grades": {
        1: "Mild transient reaction; infusion interruption not indicated; intervention not indicated",
        2: "Therapy or infusion interruption indicated but responds promptly to symptomatic treatment (e.g., antihistamines, NSAIDS, narcotics, IV fluids); prophylactic medications indicated for <=24 hrs",
        3: "Prolonged (e.g., not rapidly responsive to symptomatic medication and/or brief interruption of infusion); recurrence of symptoms following initial improvement; hospitalization indicated for clinical sequelae",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Febrile neutropenia": {"category": "Blood and lymphatic system disorders", "kind": "clinical", "grades": {
        3: "ANC <1000/mm3 with a single temperature of >38.3 degrees C (101 degrees F) or a sustained temperature of >=38 degrees C (100.4 degrees F) for more than one hour",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    # Nervous system / ear
    "Peripheral sensory neuropathy": {"category": "Nervous system disorders", "kind": "clinical", "grades": {
        1: "Asymptomatic",
        2: "Moderate symptoms; limiting instrumental ADL",
        3: "Severe symptoms; limiting self care ADL",
        4: "Life-threatening consequences; urgent intervention indicated"}},
    "Peripheral motor neuropathy": {"category": "Nervous system disorders", "kind": "clinical", "grades": {
        1: "Asymptomatic; clinical or diagnostic observations only",
        2: "Moderate symptoms; limiting instrumental ADL",
        3: "Severe symptoms; limiting self care ADL",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Hearing impaired": {"category": "Ear and labyrinth disorders", "kind": "clinical", "grades": {
        1: "Adults enrolled on a monitoring program: threshold shift of 15 - 25 dB averaged at 2 contiguous test frequencies in at least one ear; or subjective change in hearing in the absence of documented hearing loss",
        2: "Adults enrolled on a monitoring program: threshold shift of >25 dB averaged at 2 contiguous test frequencies in at least one ear; hearing loss but hearing aid or intervention not indicated; limiting instrumental ADL",
        3: "Hearing loss with hearing aid or intervention indicated; limiting self care ADL",
        4: "Profound bilateral hearing loss (>80 dB at 2 kHz and above); nonserviceable hearing"}},
    "Tinnitus": {"category": "Ear and labyrinth disorders", "kind": "clinical", "grades": {
        1: "Mild symptoms; intervention not indicated",
        2: "Moderate symptoms; limiting instrumental ADL",
        3: "Severe symptoms; limiting self care ADL"}},
    # Skin / musculoskeletal
    "Alopecia": {"category": "Skin and subcutaneous tissue disorders", "kind": "clinical", "grades": {
        1: "Hair loss of <50% of normal for that individual that is not obvious from a distance but only on close inspection; a different hair style may be required to cover the hair loss but it does not require a wig or hair piece to camouflage",
        2: "Hair loss of >=50% normal for that individual that is readily apparent to others; a wig or hair piece is necessary if the patient desires to completely camouflage the hair loss; associated with psychosocial impact"}},
    "Palmar-plantar erythrodysesthesia syndrome": {"category": "Skin and subcutaneous tissue disorders", "kind": "clinical", "grades": {
        1: "Minimal skin changes or dermatitis (e.g., erythema, edema, or hyperkeratosis) without pain",
        2: "Skin changes (e.g., peeling, blisters, bleeding, fissures, edema, or hyperkeratosis) with pain; limiting instrumental ADL",
        3: "Severe skin changes (e.g., peeling, blisters, bleeding, fissures, edema, or hyperkeratosis) with pain; limiting self care ADL"}},
    "Rash maculo-papular": {"category": "Skin and subcutaneous tissue disorders", "kind": "clinical", "grades": {
        1: "Macules/papules covering <10% BSA with or without symptoms (e.g., pruritus, burning, tightness)",
        2: "Macules/papules covering 10 - 30% BSA with or without symptoms (e.g., pruritus, burning, tightness); limiting instrumental ADL; rash covering >30% BSA with or without mild symptoms",
        3: "Macules/papules covering >30% BSA with moderate or severe symptoms; limiting self care ADL"}},
    "Myalgia": {"category": "Musculoskeletal disorders", "kind": "clinical", "grades": {
        1: "Mild pain", 2: "Moderate pain; limiting instrumental ADL", 3: "Severe pain; limiting self care ADL"}},
    "Arthralgia": {"category": "Musculoskeletal disorders", "kind": "clinical", "grades": {
        1: "Mild pain", 2: "Moderate pain; limiting instrumental ADL", 3: "Severe pain; limiting self care ADL"}},
    # Organ toxicity (clinical)
    "Acute kidney injury": {"category": "Renal and urinary disorders", "kind": "clinical", "grades": {
        3: "Hospitalization indicated",
        4: "Life-threatening consequences; dialysis indicated",
        5: "Death"}},
    "Cystitis noninfective": {"category": "Renal and urinary disorders", "kind": "clinical", "grades": {
        1: "Microscopic hematuria; minimal increase in frequency, urgency, dysuria, or nocturia; new onset of incontinence",
        2: "Moderate hematuria; moderate increase in frequency, urgency, dysuria, nocturia or incontinence; urinary catheter placement or bladder irrigation indicated; limiting instrumental ADL",
        3: "Gross hematuria; transfusion, IV medications, or hospitalization indicated; elective invasive intervention indicated",
        4: "Life-threatening consequences; urgent invasive intervention indicated",
        5: "Death"}},
    "Ejection fraction decreased": {"category": "Investigations", "kind": "clinical", "grades": {
        2: "Resting ejection fraction (EF) 50 - 40%; 10 - 19% drop from baseline",
        3: "Resting ejection fraction (EF) 39 - 20%; >=20% drop from baseline",
        4: "Resting ejection fraction (EF) <20%"}},
    "Pneumonitis": {"category": "Respiratory disorders", "kind": "clinical", "grades": {
        1: "Asymptomatic; clinical or diagnostic observations only; intervention not indicated",
        2: "Symptomatic; medical intervention indicated; limiting instrumental ADL",
        3: "Severe symptoms; limiting self care ADL; oxygen indicated",
        4: "Life-threatening respiratory compromise; urgent intervention indicated (e.g., tracheotomy or intubation)",
        5: "Death"}},
    "Dyspnea": {"category": "Respiratory disorders", "kind": "clinical", "grades": {
        1: "Shortness of breath with moderate exertion",
        2: "Shortness of breath with minimal exertion; limiting instrumental ADL",
        3: "Shortness of breath at rest; limiting self care ADL",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    # Radiotherapy-related terms (weekly on-treatment review). CTCAE v5.0 text carried forward.
    "Dermatitis radiation": {"category": "Injury, poisoning and procedural complications", "kind": "clinical", "grades": {
        1: "Faint erythema or dry desquamation",
        2: "Moderate to brisk erythema; patchy moist desquamation, mostly confined to skin folds and creases; moderate edema",
        3: "Moist desquamation in areas other than skin folds and creases; bleeding induced by minor trauma or abrasion",
        4: "Life-threatening consequences; skin necrosis or ulceration of full thickness dermis; spontaneous bleeding from involved site; skin graft indicated",
        5: "Death"}},
    "Dry mouth": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Symptomatic (e.g., dry or thick saliva) without significant dietary alteration; unstimulated saliva flow >0.2 ml/min",
        2: "Moderate symptoms; oral intake alterations (e.g., copious water, other lubricants, diet limited to purees and/or soft, moist foods); unstimulated saliva 0.1 to 0.2 ml/min",
        3: "Inability to adequately aliment orally; tube feeding or TPN indicated; unstimulated saliva <0.1 ml/min"}},
    "Dysphagia": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Symptomatic, able to eat regular diet",
        2: "Symptomatic and altered eating/swallowing",
        3: "Severely altered eating/swallowing; tube feeding, TPN, or hospitalization indicated",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Esophagitis": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Asymptomatic; clinical or diagnostic observations only; intervention not indicated",
        2: "Symptomatic; altered eating/swallowing; oral supplements indicated",
        3: "Severely altered eating/swallowing; tube feeding, TPN or hospitalization indicated",
        4: "Life-threatening consequences; urgent operative intervention indicated",
        5: "Death"}},
    "Pharyngeal mucositis": {"category": "Respiratory disorders", "kind": "clinical", "grades": {
        1: "Endoscopic findings only; minimal symptoms with normal oral intake; mild pain but analgesics not indicated",
        2: "Moderate pain and analgesics indicated; altered oral intake; limiting instrumental ADL",
        3: "Severe pain; unable to adequately aliment or hydrate orally; limiting self care ADL",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Laryngeal mucositis": {"category": "Respiratory disorders", "kind": "clinical", "grades": {
        1: "Endoscopic findings only; mild discomfort with normal intake",
        2: "Moderate discomfort; altered oral intake",
        3: "Severe pain; severely altered eating/swallowing; medical intervention indicated",
        4: "Life-threatening airway compromise; urgent intervention indicated (e.g., tracheotomy or intubation)",
        5: "Death"}},
    "Hoarseness": {"category": "Respiratory disorders", "kind": "clinical", "grades": {
        1: "Mild or intermittent voice change; fully understandable; self-resolves",
        2: "Moderate or persistent voice changes; may require occasional repetition but understandable on telephone; medical evaluation indicated",
        3: "Severe voice changes including predominantly whispered speech"}},
    "Proctitis": {"category": "Gastrointestinal disorders", "kind": "clinical", "grades": {
        1: "Rectal discomfort, intervention not indicated",
        2: "Symptoms (e.g., rectal discomfort, passing blood or mucus); medical intervention indicated; limiting instrumental ADL",
        3: "Severe symptoms; fecal urgency or stool incontinence; limiting self care ADL",
        4: "Life-threatening consequences; urgent intervention indicated",
        5: "Death"}},
    "Urinary frequency": {"category": "Renal and urinary disorders", "kind": "clinical", "grades": {
        1: "Present",
        2: "Limiting instrumental ADL; medical management indicated"}},
    "Breast pain": {"category": "Reproductive system and breast disorders", "kind": "clinical", "grades": {
        1: "Mild pain", 2: "Moderate pain; limiting instrumental ADL", 3: "Severe pain; limiting self care ADL"}},
    "Weight loss": {"category": "Investigations", "kind": "clinical", "grades": {
        1: "5 to <10% from baseline; intervention not indicated",
        2: "10 - <20% from baseline; nutritional support indicated",
        3: ">=20% from baseline; tube feeding or TPN indicated"}},
    # Lab-derived terms
    "Neutrophil count decreased": {"category": "Investigations", "kind": "lab", "lab": "ANC", "grades": {
        1: "<LLN - 1500/mm3; <LLN - 1.5 x 10e9 /L", 2: "<1500 - 1000/mm3; <1.5 - 1.0 x 10e9 /L",
        3: "<1000 - 500/mm3; <1.0 - 0.5 x 10e9 /L", 4: "<500/mm3; <0.5 x 10e9 /L"}},
    "Platelet count decreased": {"category": "Investigations", "kind": "lab", "lab": "Platelets", "grades": {
        1: "<LLN - 75,000/mm3; <LLN - 75.0 x 10e9 /L", 2: "<75,000 - 50,000/mm3; <75.0 - 50.0 x 10e9 /L",
        3: "<50,000 - 25,000/mm3; <50.0 - 25.0 x 10e9 /L", 4: "<25,000/mm3; <25.0 x 10e9 /L"}},
    "White blood cell decreased": {"category": "Investigations", "kind": "lab", "lab": "WBC", "grades": {
        1: "<LLN - 3000/mm3; <LLN - 3.0 x 10e9 /L", 2: "<3000 - 2000/mm3; <3.0 - 2.0 x 10e9 /L",
        3: "<2000 - 1000/mm3; <2.0 - 1.0 x 10e9 /L", 4: "<1000/mm3; <1.0 x 10e9 /L"}},
    "Anemia": {"category": "Blood and lymphatic system disorders", "kind": "lab", "lab": "Hemoglobin", "grades": {
        1: "Hemoglobin (Hgb) <LLN - 10.0 g/dL; <LLN - 6.2 mmol/L; <LLN - 100 g/L",
        2: "Hgb <10.0 - 8.0 g/dL; <6.2 - 4.9 mmol/L; <100 - 80g/L",
        3: "Hgb <8.0 g/dL; <4.9 mmol/L; <80 g/L; transfusion indicated",
        4: "Life-threatening consequences; urgent intervention indicated", 5: "Death"}},
    "Creatinine increased": {"category": "Investigations", "kind": "lab", "lab": "Creatinine", "grades": {
        1: ">ULN - 1.5 x ULN", 2: ">1.5 - 3.0 x baseline; >1.5 - 3.0 x ULN",
        3: ">3.0 x baseline; >3.0 - 6.0 x ULN", 4: ">6.0 x ULN"}},
    "Hypomagnesemia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Magnesium", "grades": {
        1: "<LLN - 1.2 mg/dL; <LLN - 0.5 mmol/L", 2: "<1.2 - 0.9 mg/dL; <0.5 - 0.4 mmol/L",
        3: "<0.9 - 0.7 mg/dL; <0.4 - 0.3 mmol/L", 4: "<0.7 mg/dL; <0.3 mmol/L; life-threatening consequences", 5: "Death"}},
    "Hypokalemia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Potassium", "grades": {
        1: "<LLN - 3.0 mmol/L", 2: "Symptomatic with <LLN - 3.0 mmol/L; intervention indicated",
        3: "<3.0 - 2.5 mmol/L; hospitalization indicated", 4: "<2.5 mmol/L; life-threatening consequences", 5: "Death"}},
    "Hyperkalemia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Potassium", "grades": {
        1: ">ULN - 5.5 mmol/L", 2: ">5.5 - 6.0 mmol/L; intervention initiated",
        3: ">6.0 - 7.0 mmol/L; hospitalization indicated", 4: ">7.0 mmol/L; life-threatening consequences", 5: "Death"}},
    "Hyponatremia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Sodium", "grades": {
        1: "<LLN - 130 mmol/L", 2: "125-129 mmol/L and asymptomatic",
        3: "125-129 mmol/L symptomatic; 120-124 mmol/L regardless of symptoms", 4: "<120 mmol/L; life-threatening consequences", 5: "Death"}},
    "Alanine aminotransferase increased": {"category": "Investigations", "kind": "lab", "lab": "ALT", "grades": {
        1: ">ULN - 3.0 x ULN if baseline was normal or less than normal; 1.0 - 1.5 x baseline if baseline was >ULN",
        2: ">3.0 - 5.0 x ULN if baseline was normal or less than normal; >1.5 - 2.0 x baseline if baseline was >ULN",
        3: ">5.0 - 20.0 x ULN if baseline was normal or less than normal; >2.0 - 4.0 x baseline if baseline was >ULN",
        4: ">20.0 x ULN if baseline was normal or less than normal; >4.0 x baseline if baseline was >ULN"}},
    "Aspartate aminotransferase increased": {"category": "Investigations", "kind": "lab", "lab": "AST", "grades": {
        1: ">ULN - 3.0 x ULN if baseline was normal or less than normal; 1.0 - 1.5 x baseline if baseline was >ULN",
        2: ">3.0 - 5.0 x ULN if baseline was normal or less than normal; >1.5 - 2.0 x baseline if baseline was >ULN",
        3: ">5.0 - 20.0 x ULN if baseline was normal or less than normal; >2.0 - 4.0 x baseline if baseline was >ULN",
        4: ">20.0 x ULN if baseline was normal or less than normal; >4.0 x baseline if baseline was >ULN"}},
    "Blood bilirubin increased": {"category": "Investigations", "kind": "lab", "lab": "Bilirubin", "grades": {
        1: ">ULN - 1.5 x ULN if baseline was normal", 2: ">1.5 - 3.0 x ULN if baseline was normal",
        3: ">3.0 - 10.0 x ULN if baseline was normal", 4: ">10.0 x ULN if baseline was normal"}},
    "Alkaline phosphatase increased": {"category": "Investigations", "kind": "lab", "lab": "ALP", "grades": {
        1: ">ULN - 2.5 x ULN if baseline was normal", 2: ">2.5 - 5.0 x ULN if baseline was normal",
        3: ">5.0 - 20.0 x ULN if baseline was normal", 4: ">20.0 x ULN if baseline was normal"}},
    "Hypoalbuminemia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Albumin", "grades": {
        1: "<LLN - 3 g/dL; <LLN - 30 g/L", 2: "<3 - 2 g/dL; <30 - 20 g/L",
        3: "<2 g/dL; <20 g/L", 4: "Life-threatening consequences; urgent intervention indicated", 5: "Death"}},
    "Hypocalcemia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Calcium", "grades": {
        1: "Corrected serum calcium of <LLN - 8.0 mg/dL; <LLN - 2.0 mmol/L", 2: "Corrected serum calcium of <8.0 - 7.0 mg/dL; <2.0 - 1.75 mmol/L; symptomatic",
        3: "Corrected serum calcium of <7.0 - 6.0 mg/dL; <1.75 - 1.5 mmol/L; hospitalization indicated", 4: "Corrected serum calcium of <6.0 mg/dL; <1.5 mmol/L; life-threatening consequences", 5: "Death"}},
    "Hypercalcemia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Calcium", "grades": {
        1: "Corrected serum calcium of >ULN - 11.5 mg/dL; >ULN - 2.9 mmol/L", 2: "Corrected serum calcium of >11.5 - 12.5 mg/dL; >2.9 - 3.1 mmol/L; symptomatic",
        3: "Corrected serum calcium of >12.5 - 13.5 mg/dL; >3.1 - 3.4 mmol/L; hospitalization indicated", 4: "Corrected serum calcium of >13.5 mg/dL; >3.4 mmol/L; life-threatening consequences", 5: "Death"}},
    "Chronic kidney disease": {"category": "Renal and urinary disorders", "kind": "lab", "lab": "eGFR", "grades": {
        1: "eGFR or CrCl <LLN - 60 ml/min/1.73 m2 or proteinuria 2+ present; urine protein/creatinine >0.5", 2: "eGFR or CrCl 59 - 30 ml/min/1.73 m2",
        3: "eGFR or CrCl 29 - 15 ml/min/1.73 m2", 4: "eGFR or CrCl <15 ml/min/1.73 m2; dialysis or renal transplant indicated", 5: "Death"}},
    "Hyperuricemia": {"category": "Metabolism and nutrition disorders", "kind": "lab", "lab": "Uric acid", "grades": {
        1: ">ULN without physiologic consequences", 3: ">ULN with physiologic consequences", 4: "Life-threatening consequences", 5: "Death"}},
}

# ---------------------------------------------------------------------------
# Laboratory catalog (canonical units used for trends and CTCAE grading).
# ---------------------------------------------------------------------------
LAB_CATALOG: dict[str, dict] = {
    "Hemoglobin": {"unit": "g/dL", "low": 12.0, "high": 15.5, "panel": "CBP", "worse": "down",
                   "aliases": ["haemoglobin", "hemoglobin", "hb", "hgb"],
                   "conversions": {"g/l": 0.1}},
    "WBC": {"unit": "10^3/uL", "low": 4.0, "high": 11.0, "panel": "CBP", "worse": "down",
            "aliases": ["total wbc", "wbc count", "wbc", "total leucocyte count", "total leukocyte count", "tlc", "white blood cell"],
            "conversions": {"/ul": 0.001, "cells/ul": 0.001, "/cumm": 0.001, "/mm3": 0.001, "10^9/l": 1.0}},
    "ANC": {"unit": "/uL", "low": 1500, "high": 8000, "panel": "CBP", "worse": "down",
            "aliases": ["absolute neutrophil count", "anc", "neutrophils absolute", "abs neutrophils", "neutrophil count absolute"],
            "conversions": {"10^3/ul": 1000, "10^9/l": 1000, "cells/ul": 1.0, "/cumm": 1.0, "/mm3": 1.0}},
    "Platelets": {"unit": "10^3/uL", "low": 150, "high": 450, "panel": "CBP", "worse": "down",
                  "aliases": ["platelet count", "platelets", "plt"],
                  "conversions": {"/ul": 0.001, "/cumm": 0.001, "/mm3": 0.001, "10^9/l": 1.0, "lakh/cumm": 100.0, "lakhs/cumm": 100.0}},
    "Creatinine": {"unit": "mg/dL", "low": 0.6, "high": 1.1, "panel": "RFT", "worse": "up",
                   "aliases": ["serum creatinine", "s. creatinine", "creatinine"],
                   "conversions": {"umol/l": 1 / 88.4, "µmol/l": 1 / 88.4}},
    "Urea": {"unit": "mg/dL", "low": 15, "high": 40, "panel": "RFT", "worse": "up",
             "aliases": ["blood urea", "serum urea", "urea"], "conversions": {}},
    "Sodium": {"unit": "mmol/L", "low": 135, "high": 145, "panel": "RFT", "worse": "both",
               "aliases": ["serum sodium", "sodium", "na+"], "conversions": {"meq/l": 1.0}},
    "Potassium": {"unit": "mmol/L", "low": 3.5, "high": 5.1, "panel": "RFT", "worse": "both",
                  "aliases": ["serum potassium", "potassium", "k+"], "conversions": {"meq/l": 1.0}},
    "Magnesium": {"unit": "mg/dL", "low": 1.7, "high": 2.2, "panel": "RFT", "worse": "down",
                  "aliases": ["serum magnesium", "magnesium", "mg++"], "conversions": {"mmol/l": 2.43, "meq/l": 1.215}},
    "AST": {"unit": "U/L", "low": 0, "high": 40, "panel": "LFT", "worse": "up",
            "aliases": ["sgot", "ast", "aspartate aminotransferase"], "conversions": {"iu/l": 1.0}},
    "ALT": {"unit": "U/L", "low": 0, "high": 41, "panel": "LFT", "worse": "up",
            "aliases": ["sgpt", "alt", "alanine aminotransferase"], "conversions": {"iu/l": 1.0}},
    "Bilirubin": {"unit": "mg/dL", "low": 0.2, "high": 1.2, "panel": "LFT", "worse": "up",
                  "aliases": ["total bilirubin", "bilirubin total", "serum bilirubin", "bilirubin"],
                  "conversions": {"umol/l": 1 / 17.1, "µmol/l": 1 / 17.1}},
    "Direct bilirubin": {"unit": "mg/dL", "low": 0.0, "high": 0.3, "panel": "LFT", "worse": "up",
                         "aliases": ["direct bilirubin", "conjugated bilirubin", "bilirubin direct"], "conversions": {"umol/l": 1 / 17.1}},
    "ALP": {"unit": "U/L", "low": 44, "high": 147, "panel": "LFT", "worse": "up",
            "aliases": ["alkaline phosphatase", "alk phos", "alp"], "conversions": {"iu/l": 1.0}},
    "Total protein": {"unit": "g/dL", "low": 6.4, "high": 8.3, "panel": "LFT", "worse": "down",
                      "aliases": ["total protein", "serum protein", "total proteins"], "conversions": {"g/l": 0.1}},
    "Albumin": {"unit": "g/dL", "low": 3.5, "high": 5.2, "panel": "LFT", "worse": "down",
                "aliases": ["serum albumin", "albumin"], "conversions": {"g/l": 0.1}},
    "eGFR": {"unit": "mL/min/1.73m2", "low": 90, "high": 200, "panel": "RFT", "worse": "down",
             "aliases": ["egfr", "estimated gfr", "egfr (ckd-epi)"], "conversions": {"ml/min": 1.0, "ml/min/1.73m²": 1.0, "ml/min/1.73m^2": 1.0}},
    "Uric acid": {"unit": "mg/dL", "low": 2.6, "high": 6.0, "panel": "RFT", "worse": "up",
                  "aliases": ["serum uric acid", "uric acid"], "conversions": {"umol/l": 1 / 59.48}},
    "Chloride": {"unit": "mmol/L", "low": 98, "high": 107, "panel": "RFT", "worse": "both",
                 "aliases": ["serum chloride", "chloride", "cl-"], "conversions": {"meq/l": 1.0}},
    "Calcium": {"unit": "mg/dL", "low": 8.6, "high": 10.3, "panel": "RFT", "worse": "both",
                "aliases": ["serum calcium", "total calcium", "calcium"], "conversions": {"mmol/l": 4.008}},
}

# Legacy spellings stored by earlier versions of the prototype.
UNIT_SYNONYMS = {"/µl": "/ul", "10³/µl": "10^3/ul", "10^3/µl": "10^3/ul", "x10^3/ul": "10^3/ul", "k/ul": "10^3/ul",
                 "10*3/ul": "10^3/ul", "x10^9/l": "10^9/l", "10e9/l": "10^9/l", "cells/µl": "cells/ul", "µmol/l": "umol/l"}

# ---------------------------------------------------------------------------
# Drug -> commonly labelled CTCAE terms (shortlist only; not causality).
# ---------------------------------------------------------------------------
DRUG_TOXICITIES: dict[str, dict] = {
    "Cisplatin": {"class": "Platinum", "aliases": ["cisplatin", "cddp"], "terms": [
        "Nausea", "Vomiting", "Creatinine increased", "Acute kidney injury", "Chronic kidney disease", "Hypomagnesemia", "Hypokalemia",
        "Hypocalcemia", "Hyponatremia", "Hyperuricemia",
        "Hearing impaired", "Tinnitus", "Peripheral sensory neuropathy", "Neutrophil count decreased",
        "Platelet count decreased", "Anemia", "Fatigue", "Anorexia"]},
    "Carboplatin": {"class": "Platinum", "aliases": ["carboplatin"], "terms": [
        "Platelet count decreased", "Neutrophil count decreased", "Anemia", "Nausea", "Vomiting",
        "Hypomagnesemia", "Fatigue", "Infusion related reaction", "Peripheral sensory neuropathy"]},
    "Oxaliplatin": {"class": "Platinum", "aliases": ["oxaliplatin"], "terms": [
        "Peripheral sensory neuropathy", "Nausea", "Vomiting", "Diarrhea", "Neutrophil count decreased",
        "Platelet count decreased", "Infusion related reaction", "Fatigue"]},
    "Paclitaxel": {"class": "Taxane", "aliases": ["paclitaxel", "taxol"], "terms": [
        "Peripheral sensory neuropathy", "Neutrophil count decreased", "Alopecia", "Myalgia", "Arthralgia",
        "Infusion related reaction", "Fatigue", "Anemia", "Mucositis oral"]},
    "Docetaxel": {"class": "Taxane", "aliases": ["docetaxel", "taxotere"], "terms": [
        "Neutrophil count decreased", "Febrile neutropenia", "Alopecia", "Edema limbs", "Mucositis oral",
        "Diarrhea", "Fatigue", "Peripheral sensory neuropathy", "Infusion related reaction"]},
    "Fluorouracil": {"class": "Antimetabolite", "aliases": ["fluorouracil", "5-fu", "5fu", "5-fluorouracil"], "terms": [
        "Diarrhea", "Mucositis oral", "Palmar-plantar erythrodysesthesia syndrome", "Neutrophil count decreased",
        "Nausea", "Fatigue"]},
    "Capecitabine": {"class": "Antimetabolite", "aliases": ["capecitabine", "xeloda"], "terms": [
        "Palmar-plantar erythrodysesthesia syndrome", "Diarrhea", "Nausea", "Fatigue", "Blood bilirubin increased",
        "Neutrophil count decreased"]},
    "Gemcitabine": {"class": "Antimetabolite", "aliases": ["gemcitabine"], "terms": [
        "Neutrophil count decreased", "Platelet count decreased", "Anemia", "Alanine aminotransferase increased",
        "Aspartate aminotransferase increased", "Fever", "Nausea", "Fatigue", "Edema limbs", "Pneumonitis"]},
    "Pemetrexed": {"class": "Antimetabolite", "aliases": ["pemetrexed"], "terms": [
        "Neutrophil count decreased", "Anemia", "Fatigue", "Mucositis oral", "Rash maculo-papular", "Creatinine increased"]},
    "Doxorubicin": {"class": "Anthracycline", "aliases": ["doxorubicin", "adriamycin"], "terms": [
        "Neutrophil count decreased", "Febrile neutropenia", "Alopecia", "Nausea", "Vomiting", "Mucositis oral",
        "Ejection fraction decreased", "Fatigue", "Anemia"]},
    "Cyclophosphamide": {"class": "Alkylating agent", "aliases": ["cyclophosphamide", "cytoxan"], "terms": [
        "Neutrophil count decreased", "Nausea", "Vomiting", "Alopecia", "Cystitis noninfective", "Anemia", "Fatigue"]},
    "Etoposide": {"class": "Topoisomerase inhibitor", "aliases": ["etoposide", "vp-16"], "terms": [
        "Neutrophil count decreased", "Platelet count decreased", "Alopecia", "Nausea", "Mucositis oral", "Anemia"]},
    "Irinotecan": {"class": "Topoisomerase inhibitor", "aliases": ["irinotecan"], "terms": [
        "Diarrhea", "Neutrophil count decreased", "Nausea", "Vomiting", "Alopecia", "Fatigue"]},
    "Methotrexate": {"class": "Antimetabolite", "aliases": ["methotrexate", "mtx", "metho"], "terms": [
        "Mucositis oral", "Neutrophil count decreased", "Platelet count decreased", "Anemia", "Alanine aminotransferase increased",
        "Aspartate aminotransferase increased", "Creatinine increased", "Nausea", "Diarrhea", "Fatigue", "Pneumonitis", "Rash maculo-papular"]},
    "Vincristine": {"class": "Vinca alkaloid", "aliases": ["vincristine"], "terms": [
        "Peripheral sensory neuropathy", "Peripheral motor neuropathy", "Constipation"]},
    "Trastuzumab": {"class": "Anti-HER2 antibody", "aliases": ["trastuzumab", "herceptin"], "terms": [
        "Ejection fraction decreased", "Infusion related reaction", "Diarrhea", "Fatigue"]},
}

# Plain-language phrases that help the clinician find a CTCAE term (search aid only).
SYMPTOM_KEYWORDS: dict[str, list[str]] = {
    "Peripheral sensory neuropathy": ["tingling", "numbness", "pins and needles", "paresthesia", "burning feet", "neuropathy"],
    "Peripheral motor neuropathy": ["weakness in legs", "foot drop", "difficulty walking", "motor"],
    "Nausea": ["nausea", "nauseous", "queasy", "feels sick", "feel sick", "feeling sick"],
    "Vomiting": ["vomit", "vomiting", "throwing up", "emesis"],
    "Diarrhea": ["diarrhea", "diarrhoea", "loose stool", "loose motion", "watery stool"],
    "Constipation": ["constipation", "no bowel movement", "hard stool"],
    "Mucositis oral": ["mouth ulcer", "mouth sore", "stomatitis", "mucositis", "oral ulcer"],
    "Fatigue": ["tired", "fatigue", "weakness", "lethargy", "exhausted"],
    "Anorexia": ["loss of appetite", "not eating", "poor appetite", "anorexia"],
    "Hearing impaired": ["hearing loss", "hard of hearing", "can't hear", "reduced hearing"],
    "Tinnitus": ["ringing", "tinnitus", "buzzing in ears"],
    "Alopecia": ["hair loss", "alopecia", "hair fall"],
    "Fever": ["fever", "temperature", "pyrexia"],
    "Febrile neutropenia": ["neutropenic fever", "fever with low counts"],
    "Palmar-plantar erythrodysesthesia syndrome": ["hand foot", "hand-foot", "peeling palms", "red palms"],
    "Rash maculo-papular": ["rash", "skin eruption"],
    "Infusion related reaction": ["infusion reaction", "flushing during infusion", "chills during infusion"],
    "Myalgia": ["muscle pain", "myalgia", "body ache"],
    "Arthralgia": ["joint pain", "arthralgia"],
    "Edema limbs": ["swelling", "edema", "oedema", "swollen legs"],
    "Dysgeusia": ["taste change", "metallic taste", "altered taste"],
    "Cystitis noninfective": ["blood in urine", "hematuria", "burning urination"],
    "Pneumonitis": ["dry cough"],
    "Dyspnea": ["breathless", "shortness of breath", "short of breath", "breathlessness"],
    "Acute kidney injury": ["reduced urine", "kidney injury"],
    "Dermatitis radiation": ["skin burn", "burning skin", "skin is burning", "skin peeling", "peeling skin", "skin is peeling", "red skin", "skin is red", "skin redness", "radiation burn", "skin darkening", "itchy skin", "skin is raw", "weeping skin", "blistered skin"],
    "Dry mouth": ["dry mouth", "thick saliva", "no saliva", "xerostomia"],
    "Dysphagia": ["difficulty swallowing", "trouble swallowing", "hard to swallow", "painful swallowing", "pain on swallowing", "food sticking"],
    "Esophagitis": ["heartburn", "chest burning when swallowing", "esophagitis", "oesophagitis"],
    "Pharyngeal mucositis": ["sore throat", "throat pain", "throat ulcer"],
    "Hoarseness": ["hoarse", "hoarseness", "voice change", "lost my voice", "husky voice"],
    "Proctitis": ["rectal pain", "pain in back passage", "blood from back passage", "mucus in stool", "proctitis"],
    "Urinary frequency": ["frequent urination", "passing urine often", "urinating often", "urgency"],
    "Breast pain": ["breast pain", "pain in breast", "breast tenderness"],
    "Weight loss": ["weight loss", "losing weight", "lost weight"],
}


def normalise_drug(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().lower()
    for canonical, info in DRUG_TOXICITIES.items():
        if key == canonical.lower() or key in info["aliases"]:
            return canonical
    for canonical, info in DRUG_TOXICITIES.items():
        if any(alias in key for alias in info["aliases"]):
            return canonical
    return None


def drugs_associated_with(term: str, drug_names: list[str]) -> list[str]:
    matches = []
    for drug in drug_names:
        canonical = normalise_drug(drug)
        if canonical and term in DRUG_TOXICITIES[canonical]["terms"] and drug not in matches:
            matches.append(drug)
    return matches


def normalise_unit(unit: str | None) -> str:
    key = (unit or "").strip().lower().replace(" ", "")
    return UNIT_SYNONYMS.get(key, key)


def to_canonical(test_name: str, value: float, unit: str | None) -> tuple[float | None, str | None]:
    """Convert a value to the catalog's canonical unit. Returns (value, None) or (None, reason)."""
    catalog = LAB_CATALOG.get(test_name)
    if not catalog:
        return None, "Test is not in the stored lab catalog"
    unit_key = normalise_unit(unit)
    if unit_key == normalise_unit(catalog["unit"]) or unit_key == "":
        return value, None
    factor = catalog["conversions"].get(unit_key)
    if factor is None:
        return None, f"Unit '{unit}' cannot be converted to {catalog['unit']}"
    return value * factor, None


def match_lab_test(label: str) -> str | None:
    text = label.strip().lower()
    for name in LAB_CATALOG:
        if text == name.lower():
            return name
    for name, info in LAB_CATALOG.items():
        if text in info["aliases"]:
            return name
    return None


def term_provenance(term: str) -> str:
    info = CTCAE_TERMS.get(term, {})
    if info.get("source"):
        return info["source"]
    if term in CTCAE_SOURCE_OVERRIDES:
        return CTCAE_SOURCE_OVERRIDES[term]
    if term in V6_VERIFIED:
        return "CTCAE v6.0"
    return "CTCAE v5.0 text (carried forward; import the NCI v6.0 workbook to replace)"


def reference_payload() -> dict:
    return {
        "ctcae_version": CTCAE_VERSION,
        "ctcae_provenance": {term: term_provenance(term) for term in CTCAE_TERMS},
        "ctcae_source": CTCAE_SOURCE,
        "ctcae_terms": [
            {"term": term, "category": info["category"], "kind": info["kind"], "lab": info.get("lab"),
             "grades": {str(grade): text for grade, text in info["grades"].items()},
             "keywords": SYMPTOM_KEYWORDS.get(term, [])}
            for term, info in CTCAE_TERMS.items()
        ],
        "lab_catalog": [
            {"test_name": name, "unit": info["unit"], "ref_low": info["low"], "ref_high": info["high"],
             "panel": info["panel"], "worse_direction": info["worse"]}
            for name, info in LAB_CATALOG.items()
        ],
        "drugs": [
            {"drug_name": name, "class": info["class"], "ctcae_terms": info["terms"]}
            for name, info in DRUG_TOXICITIES.items()
        ],
    }

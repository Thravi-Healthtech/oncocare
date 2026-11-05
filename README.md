# Thravi OncoCare

Oncology workflow prototype: chemotherapy cycle intelligence, radiation oncology, running and discharge summaries, and a WhatsApp channel for patients.
The system organises evidence across cycles; it never recommends a treatment action.

## What works now

**Patients dashboard (full CRUD)**
- Search by name, MRN or diagnosis; filter active/inactive.
- Summary tiles: cycles due in 7 days, open toxicity ≥ G2, patients without a plan.
- Create, edit (including marking inactive) and delete patients (with confirmation; cascades to plans, labs, uploads).

**Chemotherapy plan per patient**
- Step 1: regimen, intent, first cycle date, number of cycles, interval, and one or more medications with dose, unit and route.
- Step 2: every cycle is pre-filled with the same drugs and doses. Change any cycle's date, dose, unit or route, or add/remove a drug, before anything is saved. Cycles that differ from the default are highlighted.
- Plan and all cycles are saved atomically. Planned cycles can be edited later (previous version kept) and cycles can be added or deleted.

**Recording each cycle**
- Outcomes: given as planned, given with modification (per-drug actual dose or hold), delayed (new date, optionally shifting later cycles), held, or discontinue plan.
- Anything other than "given as planned" requires a reason category and a clinical rationale. Doses that differ from the plan cannot be saved as "given as planned".
- Every record appends a decision with a snapshot of the labs, CTCAE grades and attention items shown at the time. Edits keep the previous version.

**Pre-cycle labs**
- Optional per cycle. Cycles without labs are marked in the cycle list, lab windows, trend table, charts, CTCAE matrix and longitudinal view.
- Manual entry of a full panel, or report upload (PDF with a text layer, text or CSV). Extracted values stay unverified until a clinician confirms or rejects each one (editable before verification). The original file is stored and linked.
- If `ANTHROPIC_API_KEY` is set, uploads (including scanned images) are sent to Claude for extraction (model via `ONCOCARE_EXTRACTION_MODEL`, default `claude-sonnet-4-5`). Otherwise a rule-based parser reads the text layer. Units are converted to catalog units (for example lakh/cumm → 10³/µL).
- The trend analysis runs from baseline (first pre-cycle value) to before each cycle. It shows % change from baseline and from the previous value, how many consecutive moves there were, whether the change is in the unfavourable direction, abnormal flags, and a written reasoning per test.

**CTCAE v5.0**
- Symptom capture: a drug-relevant shortlist (from the regimen's drugs) comes first, then all supported terms, with a plain-language search aid ("tingling fingers" → peripheral sensory neuropathy). Stored grading criteria are shown and the clinician confirms the grade.
- Lab-derived grades are computed deterministically from verified values (ANC, platelets, WBC, Hb, creatinine including the ×baseline rule, Mg, K, Na, ALT/AST, bilirubin). Each window uses the worst value.
- Each grade comes with reasoning: the threshold that applied, the change from the previous window (new/worsening/improving/stable), and any commonly labelled association with regimen drugs plus cumulative dose. Association is not causality.

**Longitudinal view**
- Per cycle: drugs and doses (planned → actual) → pre-cycle labs → CTCAE items with reasoning → clinician decision.
- A CTCAE grade matrix (terms × cycles), lab trend strips and per-test charts with the reference band.

## v0.3: AI lab intake, CTCAE v6.0 + DailyMed context, trend flags

**Sample reports.** `sample_reports/` holds synthetic CBP + LFT + RFT reports for demo patient Priya Raman (HN-24077, cisplatin 75 mg/m² q21d): four text-layer PDFs (baseline and before cycles 2–4) and a phone-photo JPG of the cycle-4 report. They show progressive cisplatin nephro-, myelo- and hepatotoxicity. Regenerate with `python3 scripts/make_sample_reports.py` (needs reportlab and pillow). Start the server with `--seed` to add Priya (and any other missing demo patient) to your database.

**Upload with date of recording.** The upload form now requires the date the sample was collected. Values are saved against that date. If the report prints a different date, the document gets a note to check.

**AI extraction.** Click the **AI extraction** badge in the top bar (or **AI settings** in the upload dialog), paste your Anthropic API key, choose a model and press **Test and save**. The key is checked with a tiny request and saved to `.env`, and no restart is needed. You can also copy `.env.example` to `.env` by hand. Uploaded PDFs, photos and scans then go to Claude (`claude-sonnet-5` by default, set with `ONCOCARE_EXTRACTION_MODEL`). When a PDF has a text layer, each AI value is cross-checked against the offline parser, and any disagreement is noted on the value. Without a key, text PDFs still work through the offline parser. Everything stays unverified until a clinician confirms it.

**CTCAE v6.0 context.** Grading is labelled CTCAE v6.0 (NCI, published 22 Jul 2025). The v6.0 baseline rules for ALT/AST are applied: when the baseline is above ULN, grades are multiples of the baseline. Each term shows where its wording came from. Terms without verified v6.0 wording show v5.0 text until you import the official NCI workbook:
```bash
python3 -m backend.context import-ctcae ~/Downloads/CTCAE_v6.0.xlsx
```
If an imported lab term's wording differs from the built-in thresholds, the grade reasoning says so.

**DailyMed context.** For each regimen drug the server fetches the DailyMed structured product label (Boxed Warning, Warnings and Precautions, Adverse Reactions). It maps the label text to CTCAE terms and caches it in `data/context/dailymed/`. Each CTCAE item then shows the quoted label sentence and a link, and the symptom form shortlists terms from the label. The fetch runs in the background when a plan is opened. You can also start it with "Fetch DailyMed labels" on the CTCAE page, or run `python3 -m backend.context refresh-dailymed cisplatin`. When a label isn't available offline, the curated shortlist is used and marked as such.

**Lab trend flags.** Each test is checked before each cycle against these rules:
- newly outside the reference range, or outside it at consecutive checks
- an unfavourable change from baseline beyond a per-test threshold (e.g. creatinine 30%, platelets 30%, sodium 4%)
- a large change in one interval
- worsening at three or more consecutive checks
- a CTCAE grade rise
- a creatinine rise matching the KDIGO AKI magnitude (≥0.3 mg/dL or ≥1.5× baseline; timing is not assessed)
- a straight-line projection that crosses a limit by the next cycle

Flags are grouped per test in the Labs page and in pre-cycle review. They are attention aids only.

## v0.4: Patient portal

Patients open **http://127.0.0.1:4173/portal** (mobile-friendly) and sign in with the mobile number on their patient record and the temporary password (see v0.5). Access is turned on or off per patient under **Patient portal → Turn on access**. There is no lockout after wrong attempts (removed for the demo). Turning access off signs out any open session. Sessions use an HttpOnly cookie and last 12 hours.

**What patients see (view-only):**
- Their plan: drugs, doses, and each cycle's date and status in plain language.
- Verified lab results grouped by date, marked "Usual / High / Low" against the reference range, with small trend lines and the original report file.
- Unverified values, CTCAE grades, clinical rationale and internal notes are never shown.

**Symptom messages:**
- **Speak:** browser speech-to-text in English, Hindi, Telugu, Tamil, Kannada, Malayalam, Marathi or Bengali. The patient can edit the transcript, and the audio recording can optionally be attached.
- **Type**, plus a symptom checklist, the effect on daily life (mapped to CTCAE's instrumental vs self-care ADL wording), onset date and temperature.
- **Red flags** are detected with fixed rules: temperature ≥ 38 °C, fever or chills, bleeding, breathlessness or chest pain, unable to keep fluids down, severe vomiting or diarrhoea, confusion or fainting, signs of anaphylaxis, or needing help with self-care. The patient is told immediately to call the care team or 112 and not to wait for a reply.

**Doctor side:**
- The **Patient messages** inbox (sidebar badge) lists red-flag messages first. Each message's cycle window also gets an attention item, and patient rows show a ✉ count.
- Each message shows the text, audio, checklist and impact.
- With AI extraction on, Claude adds an English summary and candidate CTCAE terms with a *suggested* grade and the quoted evidence. Without AI, keyword suggestions are shown.
- **Grade & record** opens the CTCAE form pre-filled; the clinician still selects and confirms the grade. The message is then linked to the recorded toxicity.
- **Reply / mark reviewed** sends a message the patient sees in the portal and saves an internal note they don't see.

Speech-to-text uses the browser's speech service (Chrome, Edge, Safari). Where it isn't available, the portal still records audio for the clinician and the patient can type.

## v0.5: Sign-in and between-cycle review

**Temporary sign-in (until OTP is added).** Both portals use mobile number + password. The password for every account is `1234`, set by `ONCOCARE_DEV_PASSWORD` in `.env`. This is not secure and is for local testing only.

- **Doctor:** open http://127.0.0.1:4173/clinician (or **Clinician sign in** on the home page) and sign in with **8008475309** / **1234** (Dr. R. Khanna, created automatically). All `/api/*` doctor routes now require this session. "Recorded by" and "Confirmed by" default to the signed-in clinician.
- **Patients:** sign in at `/portal` with the mobile number saved on their record (**Edit patient → Mobile number**; numbers must be unique, and `+91` or a leading 0 is ignored). Demo patients seeded with `--seed` use 9000000018, 9000000031 and 9000000077.

**Patient symptoms in the between-cycle window.** On a plan's **CTCAE** page, symptoms are grouped by between-cycle assessment window. Each window lists the patient's portal reports by date (onset date and sent date), alongside the clinician-recorded toxicities. A banner shows how many reports are waiting and jumps to their window. **Review & add as CTCAE toxicity** opens the CTCAE form pre-filled from the report. The clinician picks and confirms the grade, and the report is then marked "Recorded as CTCAE" and linked to that entry. Pre-cycle review also lists open patient reports for the upcoming cycle.

## v0.6: Multilingual symptom triage with patient context

- Fixed: newer Claude models reject the `temperature` setting (HTTP 400), which made AI triage and AI lab reading fail. It is no longer sent, and if the API rejects a sampling setting the request is retried without it.
- Each patient message is analysed together with the patient's record: age, diagnosis, regimen and doses, days since the last dose, next cycle, clinician-recorded CTCAE toxicities, the latest verified labs (with previous value and CTCAE grade) and earlier messages. The same **Patient context** panel is shown on the message card.
- AI output now includes an English translation of messages in any language, possible red flags for the clinician, and questions to ask for accurate grading.
- **Analyse with AI / Re-analyse** re-runs triage for a message (for example after an AI failure, or once new labs are entered).
- Without AI, keyword matching and red-flag rules also recognise common symptom words in Hindi, Telugu, Tamil, Kannada, Malayalam, Marathi and Bengali (e.g. వాంతులు, उल्टी, बुखार, జ్వరం).

## v0.7: Structured symptom check-in (patient questionnaire)

Patients no longer send a single free-text message. The portal's **Symptoms** tab runs a five-step check-in in **English, Hindi or Telugu** (language buttons at the top; the choice is remembered on that device):

1. **Describe** – speak (browser speech-to-text, 8 languages) or type in their own words. Or pick from a list, or choose "No new symptoms".
2. **Confirm symptoms** – symptoms detected from their words (keyword rules for 7 Indian languages, plus Claude when AI is on) are pre-ticked; symptoms common with their regimen are listed first.
3. **Follow-up questions per symptom** – a fixed question bank (`backend/questionnaire.py`) modelled on NCI PRO-CTCAE (how often / how severe / how much it gets in the way) plus the facts CTCAE grading needs, e.g. vomiting: episodes on the worst day, fluids kept down, treatment needed (none → extra medicine → IV drip → admission); diarrhoea: extra stools per day, blood; fatigue: relieved by rest; fever: highest temperature; breathlessness: exertion level. Each symptom also asks when it started.
4. **Safety check** (every time): fever ≥38 °C/chills, keeping fluids down, passing urine, bleeding/black stools, breathlessness/chest pain.
5. **Check and send.**

Red-flag answers (e.g. cannot keep fluids down, ≥6 vomits/24 h, fever, bleeding, breathlessness, ≥7 extra stools) show the "call your care team / 112" message immediately.

**Doctor side:** each check-in appears in Patient messages and in the report's between-cycle window with a **Patient check-in** panel: every symptom's answers in English, the mapped CTCAE term, a **rule-based suggested grade** with the criterion it matched, and the safety answers. **Grade & record** opens the CTCAE form with the term, onset date, suggested grade (highlighted, not selected) and a note containing the answers. A multi-symptom check-in stays "Awaiting review" until every graded symptom has been recorded. AI triage (when on) also receives the answers.

Hindi and Telugu wording was written for this prototype and should be checked by native speakers before use with patients. Suggested grades are prompts only; the clinician selects and confirms every grade.

## v0.8: Spoken follow-up questions

When the patient describes their symptoms by **voice**, the check-in continues as a spoken conversation **in the same language** (English, Hindi or Telugu; the question language switches to match the speech language):

- The app reads each follow-up question and its answer choices aloud (browser text-to-speech), then listens for the answer (browser speech-to-text).
- The spoken answer is matched to an answer choice (`backend/voice_answers.py`). Rules come first: counts ("మూడుసార్లు", "तीन बार", "3 times"), dates ("నిన్న", "3 दिन से", "last week"), temperatures (°F converted), yes/no and phrases for each question in all three languages. Claude is used only when the rules can't decide and AI is on.
- The patient sees what was heard and which answer was picked, and can tap a different answer or press 🎤 on any question to answer it again. If an answer isn't understood twice, the app asks them to tap it.
- Safety questions are asked aloud the same way. When everything is answered, the app moves on by itself.
- **Recording:** the patient's voice is recorded while they speak (describe step and every answer) into one audio file sent with the check-in. The questions themselves are not recorded.
- **Doctor view:** each answer shows what the patient said (🎤 "…") under the chosen answer, the panel says "questions asked and answered by voice", and the recording plays on the card. AI triage also receives the transcripts.

The spoken questions need a text-to-speech voice for the language on the patient's device. Chrome and Safari usually have Hindi; for Telugu on a Mac, add a voice in System Settings › Accessibility › Spoken Content › System voice › Manage voices. Without one, the questions are shown on screen and the patient can still answer by speaking. Use Chrome, Edge or Safari for speech recognition.

## v0.9: Radiation oncology, running summary, discharge summaries, WhatsApp

### Radiation oncology (from the paper RT case sheet)
Open a patient → **Radiotherapy courses → + New RT course**. The course workspace has four pages (sidebar):

- **Prescription & plan** (cover sheet + plan sheet): site, site group, laterality, diagnosis, stage, histopathology, intent, machine, consultant, treatment days (e.g. Mon–Sat), baseline weight, concurrent chemotherapy plan and set-up data (head rest, arm position, immobilisation, bladder, bolus, knee rest). One row per **phase** (e.g. 40.5 Gy/15# VMAT 6MV + 10 Gy/5# electron boost) with EQD2 (α/β 10 and 3, no time factor; reference only). IMRT/VMAT **dose constraints** (structure, constraint, achieved, met) and per-phase **isocentre shift / SSD**. The plan must be **approved** by the physicist and radiation oncologist before any fraction can be recorded.
- **Daily treatment** (RT summary sheet): record each fraction with date, phase (auto-selects the current one), dose, MU and RTT sign. Phase and course **running totals are computed, never typed**. A dose different from the prescription needs a note; extra (gap-compensation) and same-day second fractions must be confirmed. **Missed treatment days** are found from the course's treatment days and listed until a reason is recorded (public holiday, machine downtime, toxicity…). Overall treatment time is compared with the uninterrupted schedule and flagged.
- **Weekly review** ("Early reactions during radiation therapy"): the sheet's rows (skin, mucous membrane, salivary gland, pharynx & oesophagus, larynx, lower GI, genitourinary, plus general) graded with **CTCAE terms** (dermatitis radiation, mucositis oral, dry mouth, dysphagia, oesophagitis, pharyngeal/laryngeal mucositis, hoarseness, diarrhoea, proctitis, cystitis, urinary frequency, fatigue…). Rows expected for the treated site come first. Weight is recorded with % change and the CTCAE weight-loss grade. Each review ends in a signed **decision**: continue / continue with supportive care / pause / re-plan / stop (reason + rationale required except "continue"). Pause and stop change the course status. Open patient messages appear on this page with **Use in week N review**, which pre-fills suggested grades (not selected until signed).
- **Completion**: dates, fractions, total dose, overall time, missed days and gap correction, signed. The course can be reopened with a reason.

New CTCAE terms carry the v5.0 text forward (marked as such) until the NCI v6.0 workbook is imported. The patient portal shows radiotherapy in plain language (sessions done, next session, expected last session).

### Running summary
Patient record → **Open running summary** (or sidebar **Running summary**). It shows what the patient is on now, worst toxicities, open messages and abnormal labs, and a **timeline** of every source item (chemo cycles and decisions, radiotherapy weeks, missed days and weekly reviews, CTCAE toxicities, lab panels, patient messages from the portal or WhatsApp with their interpretation and outcome, clinical notes, uploaded reports). **Generate summary** writes a short narrative from those sources only: with AI on, Claude writes it; otherwise fixed rules do. **Every sentence cites its sources** (S1, S2…, clickable); sentences without a valid source are highlighted for checking. Clinical notes and uploaded reports (imaging, histopathology, referral…; text extracted, optional AI summary) are added from the same page.

### Discharge / end-of-treatment summary
**Create discharge summary** → choose type and period → a **draft** is built from the record: treatment given (chemo and radiotherapy with phases, interruptions, completion), course during treatment (cited narrative), toxicities, complaints between visits and how they were handled, investigations and reports. The clinician edits the diagnosis and narrative and enters **condition at discharge, medicines, advice and follow-up** (never generated). **Review & finalise** checks required sections and locks the summary; changes need a **new version** (the previous one is marked superseded). **Print / PDF** opens a printable page (citations removed). Finalised summaries appear in the patient portal.

### WhatsApp
Sidebar **WhatsApp**. Default `WHATSAPP_MODE=simulator`: nothing leaves the server; the phone panel lets you type or record a voice note *as the patient*.
- **Consent**: record in-clinic consent (or the patient sends START). STOP turns messages off and cancels scheduled reminders.
- **Send to patient**: treatment plan (chemo + radiotherapy in plain language), prescription (medicines from a finalised summary), discharge summary (portal link + next visit), **visit reminder** (sent the day before at 10:00 IST unless another time is chosen; the server checks every minute) and free text. Every message is composed from the record, shown for editing and must be **approved** before it is sent or scheduled.
- **Patient replies in the same chat**: free text or voice notes become patient messages in the inbox (marked *WhatsApp*). Red-flag words get the fixed emergency reply immediately. When symptoms are recognised the patient is offered the **same fixed check-in questions** as the portal (numbered replies, or words interpreted by the existing rules), in English, Hindi or Telugu; the answers produce suggested CTCAE grades for the clinician. New radiotherapy check-in symptoms: skin reaction, dry mouth, swallowing difficulty. Unfinished check-ins are submitted after 2 hours so nothing is lost. All automatic replies are fixed texts; AI never writes to the patient.
- **Doctor's reply** from the inbox goes back on WhatsApp.
- **Live mode**: set `WHATSAPP_MODE=live`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` (see `.env.example`), point the Meta webhook to `https://<host>/api/whatsapp/webhook`, and get the five UTILITY templates approved (WhatsApp page → Going live → view). Outside 24 hours from the patient's last message only those templates are used.

Clinical dates use the clinic's time zone (`ONCOCARE_TZ`, default Asia/Kolkata), so "today" is correct for India when the server runs in UTC. The demo seed adds **Lakshmi Varma (HN-24102, mobile 9000000102)** on adjuvant breast radiotherapy (19 of 20 fractions, a public-holiday gap, week 4 G2 dermatitis) and a completed pelvic chemoradiation course for Ananya Mehta.

## Run locally

No installation or build step is needed. Python 3.10+ and SQLite from the standard library are enough.

```bash
python3 -m backend.server --seed
```

Open <http://127.0.0.1:4173> for the home page, which lists the features and has **Clinician sign in** (`/clinician`) and **Patient sign in** (`/portal`) buttons. Existing databases are migrated automatically when the server starts. Legacy single-drug cycles become per-drug rows, and the old `completed`/`administered` statuses become `given`. `--seed` only adds the two synthetic demo patients to an empty database. To see them with an existing database, start with a fresh file, for example `--db data/demo.db --seed`.

Run the tests:

```bash
python3 -m unittest discover -s tests -v
```

## API overview

| Area | Endpoints |
|---|---|
| Reference | `GET /api/reference`, `GET /api/ctcae/suggest?text=&drugs=` |
| Patients | `GET|POST /api/patients` (`search`, `status`), `GET|PATCH|DELETE /api/patients/{id}` |
| Plans | `GET|POST /api/patients/{id}/treatment-plans`, `GET|PATCH|DELETE /api/treatment-plans/{id}` |
| Cycles | `GET|POST /api/treatment-plans/{id}/cycles`, `GET|PATCH|DELETE /api/cycles/{id}`, `POST /api/cycles/{id}/record`, `GET /api/cycles/{id}/revisions`, `GET /api/cycles/{id}/decisions` |
| Labs | `GET|POST /api/cycles/{id}/labs` (single or `results[]`), `GET|POST /api/cycles/{id}/lab-documents`, `GET /api/lab-documents/{id}/file`, `GET|PATCH|DELETE /api/labs/{id}`, `POST /api/labs/{id}/verify`, `GET /api/patients/{id}/lab-trends` |
| Symptoms | `GET|POST /api/patients/{id}/symptoms`, `GET|PATCH|DELETE /api/symptoms/{id}` |
| Analysis | `GET /api/treatment-plans/{id}/longitudinal` — cycles, windows, lab trends, CTCAE with reasoning, matrix, attention items |

## Code map

- `backend/reference.py`: stored CTCAE v5.0 criteria for supported terms, lab catalog and unit conversions, drug → labelled toxicity shortlist, symptom keywords.
- `backend/analysis.py`: deterministic lab grading, baseline trends, per-window CTCAE with reasoning, and the longitudinal build.
- `backend/extraction.py`: text/PDF text-layer parsing, the optional Claude adapter, and unit normalisation.
- `backend/api.py`, `backend/database.py`: routes, schema, migrations and synthetic seed data.
- `index.html`, `app.js`, `styles.css`: single-page UI with hash routes (`#patients`, `#patient/{id}`, `#plan/{id}/{cycles|review|labs|toxicities|timeline}`).

## Prototype assumptions and limits

- Synthetic data only. There is no authentication, and the clinician name is a fixed placeholder.
- The CTCAE set covers the terms relevant to the seeded drugs, not the full dictionary. Hypokalemia and hyponatremia grades that depend on symptoms are flagged for clinician review. Liver grades assume a normal baseline and flag when it was not.
- The drug → toxicity mapping is a curated shortlist and is not a causality assessment.
- Scanned images need the Claude adapter (or manual entry). The rule-based parser only reads text layers.

## Hosting (onco.thravi.in)

See [deploy/aws/README.md](deploy/aws/README.md). Run `./deploy/aws/deploy.sh` to create or update the AWS deployment.

"use strict";

const P = { me: null, lang: "en", bank: null, ci: null, rec: null, voiceQA: false, vc: { token: 0, recognition: null, utterance: null }, plans: [], labs: null, reports: [], mode: "voice", recognition: null, recorder: null, chunks: [], audioBlob: null,
  listening: false, spokenBase: "", usedVoice: false };
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function esc(v) { return String(v ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c])); }
function fmtDate(v, opts = { day: "numeric", month: "short", year: "numeric" }) {
  if (!v) return "—";
  const d = new Date(`${String(v).slice(0, 10)}T00:00:00`);
  return Number.isNaN(d.getTime()) ? v : new Intl.DateTimeFormat("en-IN", opts).format(d);
}
function fmtWhen(v) {
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? v : new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }).format(d);
}
function fmtNum(v) { const n = Number(v); return Math.abs(n) >= 100 ? n.toLocaleString("en-IN", { maximumFractionDigits: 0 }) : String(Math.round(n * 100) / 100); }
function unit(u) { return esc(u || "").replace("10^3/uL", "×10³/µL").replace("/uL", "/µL").replace("mg/m2", "mg/m²"); }
function today() { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); }
function daysUntil(iso) { return Math.round((new Date(`${iso}T00:00:00`) - new Date(`${today()}T00:00:00`)) / 86400000); }
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 4200); }
function formError(form, msg) { const b = $(".form-error", form); b.textContent = msg || ""; b.hidden = !msg; }

async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  } catch {
    throw new Error("Can't reach the portal right now. Check your connection and try again.");
  }
  let body = {};
  try { body = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && !path.endsWith("/login")) { showLogin(); throw new Error(body.error?.message || "Please sign in again."); }
  if (!res.ok) throw new Error(body.error?.message || `Something went wrong (${res.status}).`);
  return body.data;
}

/* ---------------------------------------------------------------- auth */
function showLogin() { $("#appView").hidden = true; $("#loginView").hidden = false; }
async function signIn(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const phone = form.elements.phone.value.trim(), password = form.elements.password.value;
  if (!phone || !password) return formError(form, "Enter your mobile number and password.");
  const btn = $("button[type=submit]", form);
  btn.disabled = true; formError(form, "");
  try {
    P.me = await api("/api/portal/login", { method: "POST", body: JSON.stringify({ phone, password }) });
    form.reset();
    await start();
  } catch (e) { formError(form, e.message); } finally { btn.disabled = false; }
}
async function signOut() {
  stopVoice(true);
  try { await api("/api/portal/logout", { method: "POST" }); } catch { /* ignore */ }
  stopConversation(); recDiscard();
  P.me = null; P.bank = null; P.voiceQA = false; newCheckin(); showDescribe(); $("#sentPanel").innerHTML = ""; location.hash = ""; showLogin();
}

/* ---------------------------------------------------------------- load + route */
async function start() {
  P.me = P.me || await api("/api/portal/me");
  $("#loginView").hidden = true; $("#appView").hidden = false;
  $("#patientName").textContent = `${P.me.first_name} ${P.me.last_name}`;
  $("#patientMrn").textContent = P.me.medical_record_number;
  $("#greeting").textContent = `Hello, ${P.me.first_name}`;
  await refresh();
  route();
}
async function refresh() {
  const [plans, labs, reports, bank] = await Promise.all([api("/api/portal/plans"), api("/api/portal/labs"), api("/api/portal/reports"),
    P.bank ? Promise.resolve(P.bank) : api("/api/portal/checkin/bank")]);
  Object.assign(P, { plans, labs, reports, bank });
  renderHome(); renderPlan(); renderReports(); renderHistory();
}
function route() {
  const tab = ["home", "plan", "reports", "symptoms"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "home";
  $$(".view").forEach(v => v.classList.toggle("active", v.dataset.view === tab));
  $$(".tabs a").forEach(a => { a.classList.toggle("active", a.dataset.tab === tab); a.setAttribute("aria-current", a.dataset.tab === tab ? "page" : "false"); });
  if (tab !== "symptoms") { stopVoice(false); stopConversation(); }
  window.scrollTo({ top: 0 });
}

/* ---------------------------------------------------------------- home */
function activePlan() { return P.plans.find(p => p.status === "active") || P.plans[0]; }
function renderHome() {
  const plan = activePlan();
  const cards = [];
  if (plan) {
    const next = plan.next_cycle;
    const pct = plan.cycles.length ? Math.round((plan.given / plan.cycles.length) * 100) : 0;
    const days = next ? daysUntil(next.planned_date) : null;
    cards.push(`<article class="card hero">
      <p class="eyebrow">${next ? "Next treatment" : "Treatment"}</p>
      <div class="big">${next ? `Cycle ${next.cycle_number} · ${fmtDate(next.planned_date, { weekday: "short", day: "numeric", month: "short" })}` : "No cycle scheduled"}</div>
      <p>${esc(plan.regimen_name)}${next && days !== null ? ` · ${days === 0 ? "today" : days > 0 ? `in ${days} day${days === 1 ? "" : "s"}` : `${-days} day${days === -1 ? "" : "s"} ago`}` : ""}</p>
      <div class="progress" aria-label="${plan.given} of ${plan.cycles.length} cycles given"><span style="width:${pct}%"></span></div>
      <p class="small">${plan.given} of ${plan.cycles.length} cycles given</p>
    </article>`);
  } else {
    cards.push(`<article class="card"><h2>No treatment plan yet</h2><p class="muted">Your care team will add your plan here.</p></article>`);
  }
  const replies = P.reports.filter(r => r.patient_message).slice(0, 1);
  replies.forEach(r => cards.push(`<article class="card"><p class="eyebrow">Message from your care team</p><p>${esc(r.patient_message)}</p><p class="muted small">About your message of ${fmtDate(r.created_at)}</p></article>`));
  const latest = P.labs?.reports?.[0];
  if (latest) cards.push(`<article class="card cta"><div><p class="eyebrow">Latest blood test</p><h2>${fmtDate(latest.collection_date)}</h2><p class="muted small">${latest.values.length} results${latest.outside_range ? ` · ${latest.outside_range} outside the usual range` : " · all within the usual range"}</p></div><a class="button secondary" href="#reports">View</a></article>`);
  const open = P.reports.filter(r => r.status === "new").length;
  cards.push(`<article class="card cta"><div><p class="eyebrow">Symptoms</p><h2>How are you feeling?</h2><p class="muted small">${open ? `${open} message${open === 1 ? "" : "s"} waiting for review` : "Tell your care team by voice or text."}</p></div><a class="button primary" href="#symptoms">Report</a></article>`);
  $("#homeCards").innerHTML = cards.join("");
}

/* ---------------------------------------------------------------- plan */
function renderPlan() {
  if (!P.plans.length) { $("#planContent").innerHTML = '<div class="empty">No treatment plan has been added yet.</div>'; return; }
  $("#planContent").innerHTML = P.plans.map(plan => `<article class="card">
      <div class="pills"><span class="pill ${plan.status === "active" ? "ok" : ""}">${esc(plan.status.replace("_", " "))}</span>${plan.cycle_interval_days ? `<span class="pill">every ${plan.cycle_interval_days} days</span>` : ""}</div>
      <h2 style="margin-top:10px">${esc(plan.regimen_name)}</h2>
      <p class="muted small">${plan.start_date ? `Started ${fmtDate(plan.start_date)}` : ""}</p>
      <div class="drug-list">${plan.medications.map(m => `<div><strong>${esc(m.drug_name)}</strong><span>${fmtNum(m.dose)} ${unit(m.dose_unit)}${m.route ? ` · ${esc(m.route)}` : ""}</span></div>`).join("")}</div>
      <h3>Cycles</h3>
      <ol class="timeline">${plan.cycles.map(c => {
        const done = ["given", "modified"].includes(c.status);
        const isNext = plan.next_cycle && c.id === plan.next_cycle.id;
        return `<li class="${done ? "done" : ""} ${isNext ? "next" : ""}"><span class="cnum">C${c.cycle_number}</span>
          <div><strong>${fmtDate(c.administered_date || c.planned_date, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</strong>
          <small>${c.medications.map(m => `${esc(m.drug_name)} ${fmtNum(m.actual_dose ?? m.planned_dose)} ${unit(m.dose_unit)}${m.status === "held" ? " (not given)" : ""}`).join(" · ")}</small></div>
          <span class="pill ${done ? "ok" : c.status === "delayed" ? "warn" : ["held", "discontinued", "cancelled"].includes(c.status) ? "alert" : isNext ? "teal" : ""}">${esc(isNext ? "Next" : c.status_label)}</span></li>`;
      }).join("")}</ol>
    </article>`).join("");
}

/* ---------------------------------------------------------------- lab reports */
function spark(points) {
  if (points.length < 2) return "";
  const vals = points.map(p => p.value), lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const x = i => 4 + (i * 132) / (points.length - 1), y = v => 30 - ((v - lo) / span) * 26;
  return `<svg viewBox="0 0 140 34" role="img" aria-label="${points.map(p => fmtNum(p.value)).join(", ")}"><polyline points="${points.map((p, i) => `${x(i)},${y(p.value)}`).join(" ")}" />${points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.value)}" r="2.8" class="${p.flag !== "normal" ? "out" : ""}" />`).join("")}</svg>`;
}
function renderReports() {
  const L = P.labs;
  if (!L || !L.reports.length) { $("#reportsContent").innerHTML = '<div class="empty">No checked lab results yet. They will appear here after your care team reviews them.</div>'; return; }
  const docsFor = day => L.documents.filter(d => d.recording_date === day);
  const panels = { CBP: "Blood count", LFT: "Liver", RFT: "Kidney and salts" };
  $("#reportsContent").innerHTML = L.reports.map((r, i) => `<details class="card report" ${i === 0 ? "open" : ""}>
      <summary><div><span class="date">${fmtDate(r.collection_date)}</span><small>${r.cycle_number ? `Before cycle ${r.cycle_number}` : ""} · ${r.values.length} results</small></div>
        <span class="pill ${r.outside_range ? "warn" : "ok"}">${r.outside_range ? `${r.outside_range} outside range` : "All in range"}</span></summary>
      <div class="values">${Object.entries(panels).map(([key, label]) => {
        const vals = r.values.filter(v => v.panel === key);
        return vals.length ? `<p class="panel-title">${label}</p>` + vals.map(v => `<div class="value-row"><div><strong>${esc(v.test_name)}</strong><small>Usual ${v.ref_low ?? "—"}–${v.ref_high ?? "—"} ${unit(v.unit)}</small></div><span class="v">${fmtNum(v.value)} <small>${unit(v.unit)}</small></span><span class="tag ${v.flag}">${v.flag === "normal" ? "Usual" : v.flag === "high" ? "High" : "Low"}</span></div>`).join("") : "";
      }).join("")}</div>
      ${docsFor(r.collection_date).length ? `<div class="doc-links">${docsFor(r.collection_date).map(d => `<a href="/api/portal/documents/${d.id}/file" target="_blank" rel="noopener">📄 View original report</a>`).join("")}</div>` : ""}
    </details>`).join("") + (L.series.some(s => s.points.length > 1) ? `<h2 class="section-title">How your results have changed</h2>
    <div class="trend-grid">${L.series.filter(s => s.points.length > 1).map(s => `<div class="trend-tile"><strong>${esc(s.test_name)}</strong><small>${fmtNum(s.points[0].value)} → ${fmtNum(s.points.at(-1).value)} ${unit(s.unit)}</small>${spark(s.points)}</div>`).join("")}</div>` : "");
}

/* ---------------------------------------------------------------- symptom check-in */
const STR = {
  en: { vcToggle: "Ask me the questions aloud", vcSpeaking: "Asking…", vcListening: "Listening – please answer now", vcNotSure: "Sorry, I didn't understand. Please say it again or tap an answer.", vcRepeat: "Sorry, I didn't catch that.", vcTapInstead: "Please tap your answer on the screen.", vcAnswerByVoice: "Answer by voice", vcNoVoice: "This device has no voice installed for this language, so the questions are shown but not read aloud. You can still answer by speaking. (Mac: System Settings › Accessibility › Spoken Content › System voice › Manage voices.)", vcConfirm: "Please check the symptoms we found. Tap to add or remove any, then press Next.", vcReview: "Please check your answers, then press Send.", vcAbout: "Now some questions about {s}.",
    tellTeam: "Tell your care team", feelingTitle: "How are you feeling?", myMessages: "My messages", langLabel: "Language",
    emergencyNote: '<strong>Not for emergencies.</strong> If you have a temperature of 38 °C or more, bleeding, trouble breathing, chest pain, confusion, or you can\'t keep fluids down, call your care team now or dial <a href="tel:112">112</a>.',
    step1: "Step 1 of 5 · Describe", describeTitle: "Tell us in your own words how you feel", speak: "🎤 Speak", type: "⌨ Type", speechLang: "Speech language",
    micStart: "Tap to start speaking", micListening: "Listening… tap to stop", micMore: "Tap to add more",
    voiceHint: "Speak naturally: what you feel and since when. You can edit the words before going on.",
    attachAudio: "Send my voice recording to my care team too", textVoice: "What you said (you can edit)", textType: "Describe how you are feeling",
    placeholder: "e.g. I have been vomiting since yesterday and feel very tired", next: "Next", back: "Back",
    pickFromList: "Choose symptoms from a list instead", noSymptoms: "No new symptoms – just answer the safety questions",
    needInput: "Tell us how you feel, or choose from the list.", checking: "Checking…",
    step2: "Step 2 of 5 · Confirm symptoms", confirmTitle: "Which of these are you having?",
    confirmHintFound: "We found these in what you told us. Add or remove any.", confirmHintNone: "Tap everything you are having.",
    commonTag: "common with your treatment", otherSymptom: "Something else", stepSymptom: "Step 3 of 5 · Symptom {i} of {n}",
    answerAll: "Please answer each question.", optional: "optional", stepSafety: "Step 4 of 5 · Safety check", safetyTitle: "A few safety questions",
    safetyHint: "We ask these every time, even if you feel well.", stepReview: "Step 5 of 5 · Check and send", reviewTitle: "Check your answers",
    edit: "Change", send: "Send to my care team", sending: "Sending…", yourWords: "Your words", noNew: "No new symptoms",
    sentTitle: "Sent to your care team ✓", sentBody: "Your care team will review it. You'll see their reply under \"My messages\".",
    urgentTitle: "Please call your care team now",
    urgentBody: "Some of what you told us can be serious during cancer treatment. Call your care team now, or go to the nearest emergency department. Do not wait for a reply here.",
    call112: "Call 112 (emergency)", newCheckin: "Start a new check-in", checkinTag: "Check-in", noMessages: "You have not sent any messages yet.",
    reply: "Reply from your care team", urgentNoted: "Urgent symptoms noted", pickOne: "Choose at least one symptom, or go back and choose \"No new symptoms\"." },
  hi: { vcToggle: "सवाल बोलकर पूछें", vcSpeaking: "पूछ रहे हैं…", vcListening: "सुन रहे हैं – अब जवाब दें", vcNotSure: "माफ़ कीजिए, समझ नहीं आया। फिर से बोलें या जवाब पर दबाएं।", vcRepeat: "माफ़ कीजिए, सुनाई नहीं दिया।", vcTapInstead: "कृपया स्क्रीन पर अपना जवाब दबाएं।", vcAnswerByVoice: "बोलकर जवाब दें", vcNoVoice: "इस डिवाइस पर इस भाषा की आवाज़ इंस्टॉल नहीं है, इसलिए सवाल पढ़कर नहीं सुनाए जाएंगे। आप फिर भी बोलकर जवाब दे सकते हैं।", vcConfirm: "कृपया हमें मिले लक्षण जांच लें। जोड़ने या हटाने के लिए दबाएं, फिर आगे दबाएं।", vcReview: "कृपया अपने जवाब जांच लें, फिर भेजें दबाएं।", vcAbout: "अब {s} के बारे में कुछ सवाल।",
    tellTeam: "अपनी केयर टीम को बताएं", feelingTitle: "आप कैसा महसूस कर रहे हैं?", myMessages: "मेरे संदेश", langLabel: "भाषा",
    emergencyNote: '<strong>आपातकाल के लिए नहीं।</strong> अगर आपको 38 °C या उससे ज़्यादा बुखार, खून आना, सांस लेने में तकलीफ़, सीने में दर्द, भ्रम हो, या पानी पेट में नहीं रुक रहा, तो अभी अपनी केयर टीम को फ़ोन करें या <a href="tel:112">112</a> डायल करें।',
    step1: "चरण 1 / 5 · बताएं", describeTitle: "अपने शब्दों में बताएं कि आप कैसा महसूस कर रहे हैं", speak: "🎤 बोलें", type: "⌨ लिखें", speechLang: "बोलने की भाषा",
    micStart: "बोलना शुरू करने के लिए दबाएं", micListening: "सुन रहे हैं… रोकने के लिए दबाएं", micMore: "और जोड़ने के लिए दबाएं",
    voiceHint: "आराम से बोलें: क्या महसूस हो रहा है और कब से। आगे बढ़ने से पहले आप शब्द बदल सकते हैं।",
    attachAudio: "मेरी आवाज़ की रिकॉर्डिंग भी केयर टीम को भेजें", textVoice: "आपने जो कहा (आप बदल सकते हैं)", textType: "बताएं आप कैसा महसूस कर रहे हैं",
    placeholder: "जैसे: कल से उल्टी हो रही है और बहुत थकान है", next: "आगे", back: "पीछे",
    pickFromList: "सूची से लक्षण चुनें", noSymptoms: "कोई नया लक्षण नहीं – सिर्फ़ सुरक्षा सवालों के जवाब दें",
    needInput: "बताएं कि आप कैसा महसूस कर रहे हैं, या सूची से चुनें।", checking: "जांच रहे हैं…",
    step2: "चरण 2 / 5 · लक्षण पक्के करें", confirmTitle: "इनमें से आपको क्या हो रहा है?",
    confirmHintFound: "आपकी बात से हमें ये मिले। जो चाहें जोड़ें या हटाएं।", confirmHintNone: "जो भी हो रहा है, उस पर दबाएं।",
    commonTag: "आपके इलाज में आम", otherSymptom: "कुछ और", stepSymptom: "चरण 3 / 5 · लक्षण {i} / {n}",
    answerAll: "कृपया हर सवाल का जवाब दें।", optional: "वैकल्पिक", stepSafety: "चरण 4 / 5 · सुरक्षा जांच", safetyTitle: "कुछ सुरक्षा सवाल",
    safetyHint: "ये सवाल हर बार पूछे जाते हैं, चाहे आप ठीक महसूस करें।", stepReview: "चरण 5 / 5 · जांचें और भेजें", reviewTitle: "अपने जवाब जांच लें",
    edit: "बदलें", send: "मेरी केयर टीम को भेजें", sending: "भेज रहे हैं…", yourWords: "आपके शब्द", noNew: "कोई नया लक्षण नहीं",
    sentTitle: "आपकी केयर टीम को भेज दिया गया ✓", sentBody: "आपकी केयर टीम इसे देखेगी। उनका जवाब \"मेरे संदेश\" में दिखेगा।",
    urgentTitle: "कृपया अभी अपनी केयर टीम को फ़ोन करें",
    urgentBody: "आपने जो बताया उसमें से कुछ बातें कीमोथेरेपी के दौरान गंभीर हो सकती हैं। अभी अपनी केयर टीम को फ़ोन करें, या नज़दीकी इमरजेंसी विभाग जाएं। यहां जवाब का इंतज़ार न करें।",
    call112: "112 पर कॉल करें (इमरजेंसी)", newCheckin: "नई जांच शुरू करें", checkinTag: "जांच", noMessages: "आपने अभी तक कोई संदेश नहीं भेजा है।",
    reply: "आपकी केयर टीम का जवाब", urgentNoted: "गंभीर लक्षण दर्ज", pickOne: "कम से कम एक लक्षण चुनें, या पीछे जाकर \"कोई नया लक्षण नहीं\" चुनें।" },
  te: { vcToggle: "ప్రశ్నలు గట్టిగా చదివి అడగండి", vcSpeaking: "అడుగుతున్నాం…", vcListening: "వింటున్నాం – ఇప్పుడు జవాబు చెప్పండి", vcNotSure: "క్షమించండి, అర్థం కాలేదు. మళ్లీ చెప్పండి లేదా జవాబును నొక్కండి.", vcRepeat: "క్షమించండి, వినిపించలేదు.", vcTapInstead: "దయచేసి మీ జవాబును తెరపై నొక్కండి.", vcAnswerByVoice: "మాట్లాడి జవాబు ఇవ్వండి", vcNoVoice: "ఈ పరికరంలో ఈ భాష వాయిస్ ఇన్‌స్టాల్ కాలేదు, కాబట్టి ప్రశ్నలు చదివి వినిపించవు. అయినా మీరు మాట్లాడి జవాబు ఇవ్వవచ్చు.", vcConfirm: "మేము గుర్తించిన లక్షణాలను సరిచూడండి. జోడించడానికి లేదా తీసేయడానికి నొక్కండి, తర్వాత తరువాత నొక్కండి.", vcReview: "మీ జవాబులు సరిచూసుకుని, పంపండి నొక్కండి.", vcAbout: "ఇప్పుడు {s} గురించి కొన్ని ప్రశ్నలు.",
    tellTeam: "మీ వైద్య బృందానికి తెలియజేయండి", feelingTitle: "మీరు ఎలా ఉన్నారు?", myMessages: "నా సందేశాలు", langLabel: "భాష",
    emergencyNote: '<strong>అత్యవసర పరిస్థితులకు కాదు.</strong> మీకు 38 °C లేదా అంతకంటే ఎక్కువ జ్వరం, రక్తస్రావం, ఊపిరి ఆడకపోవడం, ఛాతీ నొప్పి, అయోమయం ఉంటే, లేదా నీళ్లు కడుపులో నిలవకపోతే, వెంటనే మీ వైద్య బృందానికి ఫోన్ చేయండి లేదా <a href="tel:112">112</a> కు కాల్ చేయండి.',
    step1: "దశ 1 / 5 · వివరించండి", describeTitle: "మీరు ఎలా ఉన్నారో మీ మాటల్లో చెప్పండి", speak: "🎤 మాట్లాడండి", type: "⌨ టైప్ చేయండి", speechLang: "మాట్లాడే భాష",
    micStart: "మాట్లాడటం మొదలుపెట్టడానికి నొక్కండి", micListening: "వింటున్నాం… ఆపడానికి నొక్కండి", micMore: "ఇంకా చెప్పడానికి నొక్కండి",
    voiceHint: "సహజంగా చెప్పండి: ఏమి అనిపిస్తోంది, ఎప్పటి నుంచి. ముందుకు వెళ్లే ముందు మాటలను మార్చుకోవచ్చు.",
    attachAudio: "నా వాయిస్ రికార్డింగ్‌ను కూడా వైద్య బృందానికి పంపండి", textVoice: "మీరు చెప్పినది (మార్చుకోవచ్చు)", textType: "మీరు ఎలా ఉన్నారో వివరించండి",
    placeholder: "ఉదా: నిన్నటి నుంచి వాంతులు అవుతున్నాయి, చాలా అలసటగా ఉంది", next: "తరువాత", back: "వెనక్కి",
    pickFromList: "జాబితా నుంచి లక్షణాలు ఎంచుకోండి", noSymptoms: "కొత్త లక్షణాలు లేవు – భద్రతా ప్రశ్నలకు మాత్రమే జవాబు ఇవ్వండి",
    needInput: "మీరు ఎలా ఉన్నారో చెప్పండి, లేదా జాబితా నుంచి ఎంచుకోండి.", checking: "తనిఖీ చేస్తున్నాం…",
    step2: "దశ 2 / 5 · లక్షణాలను నిర్ధారించండి", confirmTitle: "వీటిలో మీకు ఏమేమి ఉన్నాయి?",
    confirmHintFound: "మీరు చెప్పిన దాంట్లో ఇవి కనిపించాయి. కావాలంటే జోడించండి లేదా తీసేయండి.", confirmHintNone: "మీకు ఉన్న వాటన్నింటినీ నొక్కండి.",
    commonTag: "మీ చికిత్సలో సాధారణం", otherSymptom: "ఇంకేదైనా", stepSymptom: "దశ 3 / 5 · లక్షణం {i} / {n}",
    answerAll: "దయచేసి ప్రతి ప్రశ్నకు జవాబు ఇవ్వండి.", optional: "ఐచ్ఛికం", stepSafety: "దశ 4 / 5 · భద్రతా తనిఖీ", safetyTitle: "కొన్ని భద్రతా ప్రశ్నలు",
    safetyHint: "మీరు బాగానే ఉన్నా, ఇవి ప్రతిసారీ అడుగుతాం.", stepReview: "దశ 5 / 5 · సరిచూసి పంపండి", reviewTitle: "మీ జవాబులు సరిచూసుకోండి",
    edit: "మార్చండి", send: "నా వైద్య బృందానికి పంపండి", sending: "పంపుతున్నాం…", yourWords: "మీ మాటలు", noNew: "కొత్త లక్షణాలు లేవు",
    sentTitle: "మీ వైద్య బృందానికి పంపాం ✓", sentBody: "మీ వైద్య బృందం దీన్ని చూస్తుంది. వారి జవాబు \"నా సందేశాలు\"లో కనిపిస్తుంది.",
    urgentTitle: "దయచేసి వెంటనే మీ వైద్య బృందానికి ఫోన్ చేయండి",
    urgentBody: "మీరు చెప్పిన వాటిలో కొన్ని కీమోథెరపీ సమయంలో ప్రమాదకరం కావచ్చు. వెంటనే మీ వైద్య బృందానికి ఫోన్ చేయండి, లేదా దగ్గరలోని అత్యవసర విభాగానికి వెళ్లండి. ఇక్కడ జవాబు కోసం ఎదురుచూడకండి.",
    call112: "112 కు కాల్ చేయండి (అత్యవసరం)", newCheckin: "కొత్తగా మొదలుపెట్టండి", checkinTag: "తనిఖీ", noMessages: "మీరు ఇంకా ఏ సందేశమూ పంపలేదు.",
    reply: "మీ వైద్య బృందం జవాబు", urgentNoted: "తీవ్ర లక్షణాలు గుర్తించాం", pickOne: "కనీసం ఒక లక్షణం ఎంచుకోండి, లేదా వెనక్కి వెళ్లి \"కొత్త లక్షణాలు లేవు\" ఎంచుకోండి." },
};
const SPEECH_FOR = { en: "en-IN", hi: "hi-IN", te: "te-IN" };
function lang() { return P.lang || "en"; }
function t(key, vars = {}) { return (STR[lang()][key] ?? STR.en[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? ""); }
function L(obj) { return obj ? (obj[lang()] || obj.en) : ""; }
function newCheckin() { P.ci = { step: "describe", selected: [], detected: [], answers: {}, safety: {}, voice: {}, idx: 0, fromList: false, none: false }; }
function symptomSpec(key) {
  const s = P.bank?.symptoms.find(x => x.key === key);
  return s || { key, label: { en: STR.en.otherSymptom, hi: STR.hi.otherSymptom, te: STR.te.otherSymptom }, questions: P.bank?.generic_questions || [] };
}
function setLang(code) {
  P.lang = STR[code] ? code : "en";
  try { localStorage.setItem("oncocare.portal.lang", P.lang); } catch { /* private mode */ }
  $$("[data-ui-lang]").forEach(b => b.classList.toggle("active", b.dataset.uiLang === P.lang));
  $$("[data-t]").forEach(el => { el.textContent = t(el.dataset.t); });
  $$("[data-t-html]").forEach(el => { el.innerHTML = t(el.dataset.tHtml); });
  $("#reportText").placeholder = t("placeholder");
  $("#textLabel").textContent = P.mode === "voice" ? t("textVoice") : t("textType");
  $("#micLabel").textContent = P.listening ? t("micListening") : P.usedVoice ? t("micMore") : t("micStart");
  if (!P.listening && SPEECH_FOR[P.lang]) $("#speechLang").value = SPEECH_FOR[P.lang];
  document.documentElement.lang = P.lang;
  if (P.ci && P.ci.step !== "describe") renderWizard();
  renderHistory();
}
function setMode(mode) {
  P.mode = mode;
  $$(".mode").forEach(b => { b.classList.toggle("active", b.dataset.mode === mode); b.setAttribute("aria-selected", String(b.dataset.mode === mode)); });
  $("#voicePanel").hidden = mode !== "voice";
  $("#textLabel").textContent = mode === "voice" ? t("textVoice") : t("textType");
  if (mode !== "voice") stopVoice(false);
}
/* ---- one recording per check-in: resumed while the patient speaks, paused otherwise */
async function recResume() {
  if (!$("#attachAudio").checked || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return false;
  if (P.rec?.recorder.state === "recording") return true;
  if (P.rec?.recorder.state === "paused") { P.rec.recorder.resume(); return true; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find(x => MediaRecorder.isTypeSupported?.(x)) || "";
    const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    P.rec = { stream, recorder, chunks: [] };
    recorder.ondataavailable = e => { if (e.data.size) P.rec?.chunks.push(e.data); };
    recorder.start(1000);
    return true;
  } catch {
    toast("Microphone permission was not given. You can still type or tap your answers.");
    return false;
  }
}
function recBlob() { return P.rec?.chunks.length ? new Blob(P.rec.chunks, { type: P.rec.recorder.mimeType || "audio/webm" }) : null; }
function recPause() {
  if (P.rec?.recorder.state !== "recording") return;
  try { P.rec.recorder.requestData(); P.rec.recorder.pause(); } catch { /* noop */ }
  setTimeout(() => {
    const blob = recBlob();
    if (blob) { $("#audioPreview").src = URL.createObjectURL(blob); $("#audioPreview").hidden = false; }
  }, 250);
}
function recFinish() {
  return new Promise(resolve => {
    if (!P.rec) return resolve(null);
    const { recorder, stream } = P.rec;
    const done = () => { stream.getTracks().forEach(x => x.stop()); const blob = recBlob(); P.rec = null; resolve(blob); };
    if (recorder.state === "inactive") return done();
    recorder.onstop = done;
    try { recorder.stop(); } catch { done(); }
  });
}
function recDiscard() {
  if (!P.rec) return;
  try { P.rec.recorder.state !== "inactive" && P.rec.recorder.stop(); } catch { /* noop */ }
  P.rec.stream.getTracks().forEach(x => x.stop());
  P.rec = null; $("#audioPreview").hidden = true;
}

async function toggleVoice() { if (P.listening) stopVoice(false); else await startVoice(); }
async function startVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const wantAudio = $("#attachAudio").checked;
  if (!SR && !(wantAudio && navigator.mediaDevices?.getUserMedia)) { toast("Voice isn't available in this browser. Please type instead."); setMode("text"); return; }
  await recResume();
  if (SR) {
    const rec = new SR();
    rec.lang = $("#speechLang").value;
    rec.continuous = true;
    rec.interimResults = true;
    P.spokenBase = $("#reportText").value ? $("#reportText").value.trim() + " " : "";
    let finalText = "";
    rec.onresult = e => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalText += e.results[i][0].transcript + " ";
        else interim += e.results[i][0].transcript;
      }
      $("#reportText").value = (P.spokenBase + finalText + interim).trimStart();
    };
    rec.onerror = e => { if (e.error === "not-allowed") toast("Microphone permission was not given."); else if (e.error !== "no-speech" && e.error !== "aborted") toast(`Voice to text stopped (${e.error}). You can type or try again.`); };
    rec.onend = () => { if (P.listening) { try { rec.start(); } catch { stopVoice(false); } } };
    try { rec.start(); } catch { /* already started */ }
    P.recognition = rec;
  } else {
    $("#voiceHint").textContent = "This browser records your voice but can't turn it into text. Your recording will be sent; you can also type a short summary.";
  }
  P.listening = true; P.usedVoice = true;
  $("#micButton").classList.add("recording"); $("#micButton").setAttribute("aria-pressed", "true");
  $("#micLabel").textContent = t("micListening");
}
function stopVoice(discard) {
  if (discard) recDiscard(); else recPause();
  if (!P.listening) return;
  P.listening = false;
  try { P.recognition?.stop(); } catch { /* noop */ }
  P.recognition = null;
  $("#micButton").classList.remove("recording"); $("#micButton").setAttribute("aria-pressed", "false");
  $("#micLabel").textContent = P.usedVoice ? t("micMore") : t("micStart");
}

/* ---- spoken follow-up questions: the app asks aloud in the patient's language and listens for the answer */
const SR_CLASS = () => window.SpeechRecognition || window.webkitSpeechRecognition;
function voices() { try { return window.speechSynthesis?.getVoices() || []; } catch { return []; } }
function voiceFor(code) {
  const tag = SPEECH_FOR[code] || "en-IN", base = tag.slice(0, 2);
  const all = voices();
  return all.find(v => v.lang === tag) || all.find(v => v.lang?.replace("_", "-").startsWith(base + "-")) || all.find(v => v.lang?.startsWith(base)) || null;
}
function canSpeak() { return !!window.speechSynthesis && !!voiceFor(lang()); }
function speak(text) {
  return new Promise(resolve => {
    const synth = window.speechSynthesis, voice = voiceFor(lang());
    if (!synth || !voice || !text) return resolve(false);
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.voice = voice; u.lang = voice.lang; u.rate = lang() === "en" ? 0.95 : 0.9;
    let finished = false;
    const end = ok => { if (!finished) { finished = true; clearTimeout(timer); resolve(ok); } };
    const timer = setTimeout(() => end(true), 2500 + text.length * 110);  // onend is unreliable in some browsers
    u.onend = () => end(true); u.onerror = () => end(false);
    P.vc.utterance = u;  // keep a reference (Chrome can garbage-collect it mid-speech)
    synth.speak(u);
  });
}
function listenOnce(onInterim) {
  return new Promise(resolve => {
    const SR = SR_CLASS();
    if (!SR) return resolve(null);
    const rec = new SR();
    rec.lang = SPEECH_FOR[lang()] || "en-IN"; rec.continuous = false; rec.interimResults = true; rec.maxAlternatives = 1;
    let best = "", settled = false;
    const finish = () => { if (!settled) { settled = true; clearTimeout(timer); recPause(); resolve(best.trim() || null); } };
    const timer = setTimeout(() => { try { rec.stop(); } catch { finish(); } }, 12000);
    rec.onresult = e => {
      let text = "";
      for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
      best = text; onInterim?.(text);
      if (e.results[e.results.length - 1].isFinal) { try { rec.stop(); } catch { finish(); } }
    };
    rec.onerror = e => { if (e.error === "not-allowed") { toast("Microphone permission was not given."); P.voiceQA = false; } finish(); };
    rec.onend = finish;
    P.vc.recognition = rec;
    recResume().then(() => { try { rec.start(); } catch { finish(); } });
  });
}
function stopConversation() {
  P.vc.token += 1;
  try { window.speechSynthesis?.cancel(); } catch { /* noop */ }
  try { P.vc.recognition?.abort(); } catch { /* noop */ }
  P.vc.recognition = null;
  recPause();
  $$("#wizard .q.asking").forEach(el => el.classList.remove("asking"));
  setVcStatus("");
}
function setVcStatus(state, text = "") {
  const bar = $("#wizard .vc-status");
  if (!bar) return;
  bar.dataset.state = state;
  bar.textContent = state === "speaking" ? `🔊 ${t("vcSpeaking")}` : state === "listening" ? `🎤 ${t("vcListening")}` : state === "thinking" ? `… ${t("checking")}` : text;
}
function questionSpeech(q) {
  const or = { en: "or", hi: "या", te: "లేదా" }[lang()];
  if (q.type !== "choice") return L(q.text);
  const labels = q.options.map(o => L(o.label));
  return `${L(q.text)} ${labels.slice(0, -1).join(", ")} ${or} ${labels.at(-1)}.`;
}
function markHeard(qEl, heard, label, ok) {
  const box = qEl?.querySelector(".vc-heard");
  if (!box) return;
  box.hidden = !heard && !label;
  box.className = `vc-heard ${ok === false ? "unclear" : ""}`;
  box.innerHTML = `${heard ? `<span>🎤 “${esc(heard)}”</span>` : ""}${label ? `<strong>→ ${esc(label)}</strong>` : ok === false ? `<strong>${esc(t("vcNotSure"))}</strong>` : ""}`;
}
async function askByVoice(questions, prefix, store, token) {
  const voiceStore = P.ci.voice[prefix] || (P.ci.voice[prefix] = {});
  for (const q of questions) {
    if (token !== P.vc.token) return false;
    if (store[q.id] !== undefined && store[q.id] !== "") continue;
    const qEl = $(`#wizard .q[data-q="${CSS.escape(q.id)}"]`);
    $$("#wizard .q.asking").forEach(el => el.classList.remove("asking"));
    qEl?.classList.add("asking");
    qEl?.scrollIntoView({ behavior: "smooth", block: "center" });
    let answered = false;
    for (let attempt = 0; attempt < 2 && !answered; attempt++) {
      setVcStatus("speaking");
      await speak(attempt === 0 ? questionSpeech(q) : `${t("vcRepeat")} ${questionSpeech(q)}`);
      if (token !== P.vc.token) return false;
      setVcStatus("listening");
      const heard = await listenOnce(text => markHeard(qEl, text, "", null));
      if (token !== P.vc.token) return false;
      if (!heard) { if (q.optional) break; continue; }
      setVcStatus("thinking");
      let result = { value: null };
      try { result = await api("/api/portal/checkin/interpret", { method: "POST", body: JSON.stringify({ symptom: prefix, question: q.id, heard, lang: lang() }) }); } catch { /* fall back to tapping */ }
      if (token !== P.vc.token) return false;
      if (result.value !== null && result.value !== undefined) {
        voiceStore[q.id] = heard;
        store[q.id] = result.value;
        const input = q.type === "choice" ? $(`#wizard input[name="${CSS.escape(`${prefix}.${q.id}`)}"][value="${CSS.escape(String(result.value))}"]`) : $(`#wizard input[name="${CSS.escape(`${prefix}.${q.id}`)}"]`);
        if (input) { if (q.type === "choice") input.checked = true; else input.value = result.value; }
        qEl?.classList.remove("missing");
        markHeard(qEl, heard, q.type === "date" ? fmtDate(result.value) : q.type === "number" ? `${result.value} °C` : result.label, true);
        answered = true;
      } else {
        markHeard(qEl, heard, "", false);
      }
    }
    if (!answered && !q.optional) {
      setVcStatus("idle", t("vcTapInstead"));
      await speak(t("vcTapInstead"));
      qEl?.classList.remove("asking");
      return false;  // wait for the patient to tap; the 🎤 button on the question restarts the conversation
    }
    await new Promise(r => setTimeout(r, 500));
  }
  qElClear();
  return true;
}
function qElClear() { $$("#wizard .q.asking").forEach(el => el.classList.remove("asking")); }
async function runConversation({ intro = true } = {}) {
  if (!P.voiceQA || !SR_CLASS()) return;
  const ci = P.ci, token = ++P.vc.token;
  if (ci.step === "confirm") { setVcStatus("speaking"); await speak(t("vcConfirm")); if (token === P.vc.token) setVcStatus("idle", ""); return; }
  if (ci.step === "review") { await speak(t("vcReview")); return; }
  let questions, prefix, store;
  if (ci.step === "symptom") {
    prefix = ci.selected[ci.idx]; questions = symptomSpec(prefix).questions; store = ci.answers[prefix] || (ci.answers[prefix] = {});
    if (intro) { setVcStatus("speaking"); await speak(t("vcAbout", { s: L(symptomSpec(prefix).label) })); }
  } else if (ci.step === "safety") {
    prefix = "safety"; questions = P.bank.safety; store = ci.safety;
    if (intro) { setVcStatus("speaking"); await speak(t("safetyHint")); }
  } else return;
  if (token !== P.vc.token) return;
  const complete = await askByVoice(questions, prefix, store, token);
  if (complete && token === P.vc.token) {
    setVcStatus("idle", "✓");
    await new Promise(r => setTimeout(r, 700));
    if (token === P.vc.token) wizardAction("next");
  }
}
function blobToBase64(blob) {
  return new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(String(r.result).split(",")[1] || ""); r.onerror = () => reject(new Error("Could not read the recording.")); r.readAsDataURL(blob); });
}
function showDescribe() {
  $("#reportForm").hidden = false; $("#wizard").hidden = true;
}
async function goConfirm(fromList) {
  const form = $("#reportForm");
  if (P.listening) { stopVoice(false); await new Promise(r => setTimeout(r, 350)); }
  const text = $("#reportText").value.trim();
  const audio = $("#attachAudio").checked && P.mode === "voice" ? recBlob() : null;
  if (!fromList && !text && !audio) return formError(form, t("needInput"));
  formError(form, "");
  // spoken answers → ask the follow-up questions aloud, in the language the patient spoke
  P.voiceQA = P.usedVoice && P.mode === "voice" && !!SR_CLASS();
  const spoken = $("#speechLang").value.slice(0, 2);
  if (P.voiceQA && STR[spoken] && spoken !== lang()) setLang(spoken);
  P.ci.fromList = fromList; P.ci.none = false;
  if (text && !fromList) {
    const btn = $("button[type=submit]", form);
    btn.disabled = true; btn.textContent = t("checking");
    try { P.ci.detected = (await api("/api/portal/checkin/detect", { method: "POST", body: JSON.stringify({ text }) })).symptoms; }
    catch { P.ci.detected = []; }
    finally { btn.disabled = false; btn.textContent = t("next"); }
    P.ci.selected = [...new Set([...P.ci.detected, ...P.ci.selected])];
  }
  P.ci.step = "confirm";
  renderWizard();
}
function goSafetyOnly() {
  if (P.listening) stopVoice(false);
  P.ci.none = true; P.ci.selected = []; P.ci.step = "safety"; renderWizard();
}
function stepHead(stepText, title, hint = "") {
  return `<p class="step-count">${esc(stepText)}</p><h2 class="step-title">${esc(title)}</h2>${hint ? `<p class="muted small">${esc(hint)}</p>` : ""}`;
}
function qHtml(q, prefix, value) {
  const name = `${prefix}.${q.id}`;
  const heard = P.ci?.voice?.[prefix]?.[q.id];
  const mic = SR_CLASS() ? ` <button type="button" class="q-mic" data-wz="ask" data-prefix="${esc(prefix)}" data-qid="${esc(q.id)}" title="${esc(t("vcAnswerByVoice"))}" aria-label="${esc(t("vcAnswerByVoice"))}">🎤</button>` : "";
  const opt = (q.optional ? ` <small class="muted">(${esc(t("optional"))})</small>` : "") + mic;
  const heardBox = `<div class="vc-heard" ${heard ? "" : "hidden"}>${heard ? `<span>🎤 “${esc(heard)}”</span>` : ""}</div>`;
  if (q.type === "choice") {
    return `<fieldset class="q" data-q="${q.id}" data-required="${q.optional ? "" : "1"}"><legend>${esc(L(q.text))}${opt}</legend>
      <div class="opts ${q.options.length <= 3 && q.options.every(o => o.label.en.length <= 22) ? "row" : ""}">${q.options.map(o => `<label class="opt"><input type="radio" name="${esc(name)}" value="${esc(o.value)}" ${value === o.value ? "checked" : ""} /><span>${esc(L(o.label))}</span></label>`).join("")}</div>${heardBox}</fieldset>`;
  }
  if (q.type === "number") {
    return `<label class="field q" data-q="${q.id}"><span>${esc(L(q.text))}${opt}</span><input type="number" name="${esc(name)}" inputmode="decimal" step="${q.step || 0.1}" min="${q.min}" max="${q.max}" value="${value ?? ""}" placeholder="37.8" />${heardBox}</label>`;
  }
  return `<label class="field q" data-q="${q.id}"><span>${esc(L(q.text))}${opt}</span><input type="date" name="${esc(name)}" max="${today()}" value="${value ?? ""}" />${heardBox}</label>`;
}
function readQuestions(questions, prefix, store, validate) {
  const missing = [];
  for (const q of questions) {
    const name = `${prefix}.${q.id}`;
    let value;
    if (q.type === "choice") value = $(`#wizard input[name="${CSS.escape(name)}"]:checked`)?.value;
    else value = $(`#wizard input[name="${CSS.escape(name)}"]`)?.value;
    if (value) store[q.id] = q.type === "number" ? Number(value) : value; else delete store[q.id];
    if (validate && !value && !q.optional) missing.push(q.id);
    if (validate && q.type === "number" && value && (Number(value) < q.min || Number(value) > q.max)) missing.push(q.id);
  }
  $$("#wizard .q").forEach(el => el.classList.toggle("missing", missing.includes(el.dataset.q)));
  return missing.length === 0;
}
function answerText(q, v) {
  if (v === undefined || v === "") return null;
  if (q.type === "choice") return L(q.options.find(o => o.value === v)?.label) || v;
  if (q.type === "number") return `${v} °C`;
  return fmtDate(v);
}
function renderWizard() {
  const ci = P.ci, w = $("#wizard");
  if (!P.bank) return;
  stopConversation();
  $("#reportForm").hidden = true; w.hidden = false;
  const vcBar = SR_CLASS() && ["confirm", "symptom", "safety", "review"].includes(ci.step) ? `<div class="vc-bar">
      <label class="vc-toggle"><input type="checkbox" id="voiceQA" ${P.voiceQA ? "checked" : ""} /><span>${esc(t("vcToggle"))}</span></label>
      <span class="vc-status" data-state="idle"></span></div>
      ${P.voiceQA && !canSpeak() ? `<p class="vc-note">${esc(t("vcNoVoice"))}</p>` : ""}` : "";
  const nav = (nextLabel = t("next"), action = "next") => `<div class="wz-nav"><button class="button secondary" type="button" data-wz="back">${esc(t("back"))}</button><button class="button primary" type="button" data-wz="${action}">${esc(nextLabel)}</button></div><p class="form-error" role="alert" hidden></p>`;
  if (ci.step === "confirm") {
    const common = new Set(P.bank.common || []);
    const tagCommon = common.size > 0 && common.size <= 6;  // a tag on every chip is noise
    const keys = [...P.bank.symptoms.map(s => s.key).sort((a, b) => (common.has(b) - common.has(a))), "other"];
    w.innerHTML = vcBar + stepHead(t("step2"), t("confirmTitle"), ci.detected.length && !ci.fromList ? t("confirmHintFound") : t("confirmHintNone")) +
      `<div class="sym-grid">${keys.map(k => `<label class="sym ${ci.detected.includes(k) ? "found" : ""}"><input type="checkbox" value="${k}" ${ci.selected.includes(k) ? "checked" : ""} /><span>${esc(L(symptomSpec(k).label))}${tagCommon && common.has(k) ? `<small>${esc(t("commonTag"))}</small>` : ""}</span></label>`).join("")}</div>` + nav();
  } else if (ci.step === "symptom") {
    const key = ci.selected[ci.idx], spec = symptomSpec(key), store = ci.answers[key] || (ci.answers[key] = {});
    w.innerHTML = vcBar + stepHead(t("stepSymptom", { i: ci.idx + 1, n: ci.selected.length }), L(spec.label)) +
      spec.questions.map(q => qHtml(q, key, store[q.id])).join("") + nav();
  } else if (ci.step === "safety") {
    w.innerHTML = vcBar + stepHead(t("stepSafety"), t("safetyTitle"), t("safetyHint")) + P.bank.safety.map(q => qHtml(q, "safety", ci.safety[q.id])).join("") + nav();
  } else if (ci.step === "review") {
    const text = $("#reportText").value.trim();
    const blocks = ci.selected.map((key, i) => {
      const spec = symptomSpec(key), a = ci.answers[key] || {};
      const rows = spec.questions.map(q => [q, answerText(q, a[q.id])]).filter(([, v]) => v);
      return `<div class="rv-block"><div class="rv-head"><strong>${esc(L(spec.label))}</strong><button class="link-button small" type="button" data-wz="edit" data-idx="${i}">${esc(t("edit"))}</button></div>
        <dl>${rows.map(([q, v]) => `<dt>${esc(L(q.text))}</dt><dd>${esc(v)}</dd>`).join("")}</dl></div>`;
    }).join("");
    const safety = P.bank.safety.map(q => `<dt>${esc(L(q.text))}</dt><dd>${esc(answerText(q, ci.safety[q.id]) || "—")}</dd>`).join("");
    w.innerHTML = vcBar + stepHead(t("stepReview"), t("reviewTitle")) +
      (text ? `<div class="rv-block"><div class="rv-head"><strong>${esc(t("yourWords"))}</strong></div><p class="quote">${esc(text)}</p></div>` : "") +
      (blocks || `<div class="rv-block"><strong>${esc(t("noNew"))}</strong></div>`) +
      `<div class="rv-block"><div class="rv-head"><strong>${esc(t("safetyTitle"))}</strong><button class="link-button small" type="button" data-wz="edit-safety">${esc(t("edit"))}</button></div><dl>${safety}</dl></div>` +
      nav(t("send"), "send");
  }
  w.scrollIntoView({ behavior: "smooth", block: "start" });
  if (P.voiceQA) setTimeout(runConversation, 400);
}
function wizardError(msg) { const b = $("#wizard .form-error"); if (b) { b.textContent = msg || ""; b.hidden = !msg; } }
function saveCurrent(validate) {
  const ci = P.ci;
  if (ci.step === "confirm") { ci.selected = $$("#wizard .sym input:checked").map(i => i.value); return true; }
  if (ci.step === "symptom") { const key = ci.selected[ci.idx]; return readQuestions(symptomSpec(key).questions, key, ci.answers[key] || (ci.answers[key] = {}), validate); }
  if (ci.step === "safety") return readQuestions(P.bank.safety, "safety", ci.safety, validate);
  return true;
}
async function wizardAction(action, el) {
  const ci = P.ci;
  if (action === "ask") {
    // answer this question (and any later unanswered ones) by voice
    P.voiceQA = true;
    const toggle = $("#voiceQA"); if (toggle) toggle.checked = true;
    const store = el.dataset.prefix === "safety" ? ci.safety : (ci.answers[el.dataset.prefix] || (ci.answers[el.dataset.prefix] = {}));
    saveCurrent(false);
    delete store[el.dataset.qid];
    const input = $(`#wizard input[name="${CSS.escape(`${el.dataset.prefix}.${el.dataset.qid}`)}"]:checked`); if (input) input.checked = false;
    stopConversation();
    const token = P.vc.token;
    const questions = el.dataset.prefix === "safety" ? P.bank.safety : symptomSpec(el.dataset.prefix).questions;
    const from = questions.findIndex(q => q.id === el.dataset.qid);
    const complete = await askByVoice(questions.slice(from), el.dataset.prefix, store, token);
    if (complete && token === P.vc.token && questions.every(q => q.optional || (store[q.id] !== undefined && store[q.id] !== ""))) {
      await new Promise(r => setTimeout(r, 700));
      if (token === P.vc.token) wizardAction("next");
    }
    return;
  }
  stopConversation();
  if (action === "back") {
    saveCurrent(false);
    if (ci.step === "confirm") { showDescribe(); return; }
    if (ci.step === "symptom") { if (ci.idx > 0) ci.idx -= 1; else ci.step = "confirm"; }
    else if (ci.step === "safety") { if (ci.none) { showDescribe(); return; } if (ci.selected.length) { ci.step = "symptom"; ci.idx = ci.selected.length - 1; } else ci.step = "confirm"; }
    else if (ci.step === "review") ci.step = "safety";
    return renderWizard();
  }
  if (action === "edit") { ci.step = "symptom"; ci.idx = Number(el.dataset.idx); ci.returnToReview = true; return renderWizard(); }
  if (action === "edit-safety") { ci.step = "safety"; return renderWizard(); }
  if (action === "next") {
    if (!saveCurrent(true)) { $("#wizard .q.missing")?.scrollIntoView({ behavior: "smooth", block: "start" }); return wizardError(t("answerAll")); }
    if (ci.step === "confirm") {
      if (!ci.selected.length) return wizardError(t("pickOne"));
      ci.answers = Object.fromEntries(Object.entries(ci.answers).filter(([k]) => ci.selected.includes(k)));
      ci.step = "symptom"; ci.idx = 0;
    } else if (ci.step === "symptom") {
      if (ci.returnToReview) { ci.returnToReview = false; ci.step = "review"; }
      else if (ci.idx < ci.selected.length - 1) ci.idx += 1; else ci.step = "safety";
    } else if (ci.step === "safety") ci.step = "review";
    return renderWizard();
  }
  if (action === "send") return sendCheckin(el);
}
async function sendCheckin(btn) {
  const ci = P.ci;
  const text = $("#reportText").value.trim();
  const audio = $("#attachAudio").checked ? await recFinish() : (recDiscard(), null);
  const spoken = P.usedVoice || Object.keys(ci.voice).length > 0;
  const payload = { text, symptoms: ci.selected, input_mode: spoken ? "voice" : "text", language: spoken ? (SPEECH_FOR[lang()] || $("#speechLang").value) : lang(),
    ui_language: lang(), checkin: { symptoms: ci.selected, answers: ci.answers, safety: ci.safety, voice: ci.voice } };
  if (audio) {
    if (audio.size > 8_000_000) return wizardError("The recording is too long. Please keep it under about 5 minutes.");
    payload.audio_base64 = await blobToBase64(audio);
    payload.audio_type = audio.type || "audio/webm";
  }
  btn.disabled = true; btn.textContent = t("sending"); wizardError("");
  try {
    const sent = await api("/api/portal/reports", { method: "POST", body: JSON.stringify(payload) });
    $("#reportForm").reset(); $("#attachAudio").checked = true;
    P.usedVoice = false; P.voiceQA = false; $("#audioPreview").hidden = true; $("#micLabel").textContent = t("micStart");
    newCheckin(); showDescribe();
    $("#sentPanel").innerHTML = sent.emergency_message
      ? `<article class="card alert-card" role="alert"><h2>${esc(t("urgentTitle"))}</h2><p>${esc(t("urgentBody"))}</p>${lang() === "en" ? `<p class="small">We noticed: ${sent.red_flags.map(f => esc(f.label)).join("; ")}.</p>` : ""}<a class="button full" href="tel:112">${esc(t("call112"))}</a></article>`
      : `<article class="card ok-card"><h2>${esc(t("sentTitle"))}</h2><p class="muted">${esc(t("sentBody"))}</p></article>`;
    $("#sentPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    await refresh();
  } catch (e) { wizardError(e.message); btn.disabled = false; btn.textContent = t("send"); }
}
function renderHistory() {
  if (!$("#reportHistory")) return;
  $("#reportHistory").innerHTML = P.reports.length ? P.reports.map(r => `<article class="card msg">
      <div class="meta"><span>${fmtWhen(r.created_at)}</span><span>· ${r.is_checkin ? `✓ ${esc(t("checkinTag"))}` : r.input_mode === "voice" ? "🎤 voice" : "⌨ text"}</span>
        <span class="pill ${r.status === "new" ? "warn" : "ok"}">${esc(r.status_label)}</span>${r.red_flags.length ? `<span class="pill alert">${esc(t("urgentNoted"))}</span>` : ""}</div>
      ${r.text && !r.text.startsWith("Check-in:") ? `<p class="quote">${esc(r.text)}</p>` : ""}
      ${r.symptoms.length ? `<div class="pills">${r.symptoms.map(s => `<span class="pill">${esc(L(symptomSpec(s).label))}</span>`).join("")}</div>` : r.is_checkin ? `<div class="pills"><span class="pill">${esc(t("noNew"))}</span></div>` : ""}
      ${r.has_audio ? `<audio controls preload="none" src="/api/portal/reports/${r.id}/audio"></audio>` : ""}
      ${r.patient_message ? `<div class="reply"><strong>${esc(t("reply"))}</strong>${esc(r.patient_message)}</div>` : ""}
    </article>`).join("") : `<div class="empty">${esc(t("noMessages"))}</div>`;
}

/* ---------------------------------------------------------------- wire up */
$("#loginForm").addEventListener("submit", signIn);
$("#signOut").addEventListener("click", signOut);
$("#reportForm").addEventListener("submit", e => { e.preventDefault(); goConfirm(false); });
$("#pickFromList").addEventListener("click", () => goConfirm(true));
$("#noSymptoms").addEventListener("click", goSafetyOnly);
$("#wizard").addEventListener("change", e => {
  if (e.target.id === "voiceQA") { P.voiceQA = e.target.checked; if (P.voiceQA) { if (!canSpeak()) renderWizard(); else runConversation(); } else stopConversation(); return; }
  // a tap during the spoken questions answers that question; carry on with the next one
  if (P.voiceQA && e.target.closest(".q")?.classList.contains("asking")) {
    saveCurrent(false); stopConversation(); setTimeout(() => runConversation({ intro: false }), 300);
  } e.target.closest(".q")?.classList.remove("missing"); if (!$("#wizard .q.missing")) wizardError(""); });
$("#wizard").addEventListener("click", e => { const b = e.target.closest("[data-wz]"); if (b) wizardAction(b.dataset.wz, b); });
$("#micButton").addEventListener("click", toggleVoice);
$$(".mode").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));
$$("[data-ui-lang]").forEach(b => b.addEventListener("click", () => setLang(b.dataset.uiLang)));
window.addEventListener("hashchange", route);
newCheckin();
try { window.speechSynthesis?.getVoices(); window.speechSynthesis?.addEventListener?.("voiceschanged", () => {}); } catch { /* noop */ }
let savedLang = "en";
try { savedLang = localStorage.getItem("oncocare.portal.lang") || "en"; } catch { /* private mode */ }
setLang(savedLang);
if (!(window.SpeechRecognition || window.webkitSpeechRecognition)) {
  $("#voiceHint").textContent = "This browser can record your voice for your care team but can't turn it into text. Chrome, Edge or Safari can.";
}
start().catch(() => showLogin());
document.addEventListener("visibilitychange", () => { if (!document.hidden && P.me && !P.listening) refresh().catch(() => {}); });
setInterval(() => { if (P.me && !P.listening && !document.hidden) refresh().catch(() => {}); }, 60000);

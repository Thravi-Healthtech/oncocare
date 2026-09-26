"use strict";

/* ------------------------------------------------------------------ state */
const S = {
  reference: null,
  patients: [],
  patient: null,
  planId: null,
  L: null,               // longitudinal analysis for the open plan
  view: "patients",
  reviewCycleId: null,
  planDraft: null,       // plan builder draft
  editingPatientId: null,
  editingPlan: false,
  cycleDraft: null,      // {id|null, meds:[]}
  recordingCycleId: null,
  editingSymptomId: null,
  confirmAction: null,
  searchSeq: 0,
};
const PLAN_VIEWS = ["cycles", "review", "labs", "toxicities", "timeline"];
const UNITS = ["mg/m2", "mg/kg", "mg", "AUC", "units/m2", "g/m2", "mcg/m2"];
const STATUS_LABEL = { planned: "Planned", given: "Given", modified: "Modified", delayed: "Delayed", held: "Held", discontinued: "Discontinued", cancelled: "Cancelled" };
const OUTCOME_LABEL = { given: "Given as planned", modified: "Given with modification", delayed: "Delayed", held: "Not given (held)", discontinued: "Plan discontinued" };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ------------------------------------------------------------------ helpers */
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  } catch {
    throw new Error("The OncoCare service is unavailable. Start the local server (python3 -m backend.server --seed) and reload.");
  }
  let result = {};
  try { result = await response.json(); } catch { /* empty body */ }
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    showDoctorLogin();
    throw new Error("Your session has ended. Please sign in again.");
  }
  if (!response.ok) {
    const reason = result.error?.details?.reason || result.error?.message || `Request failed (${response.status})`;
    if (response.status === 409 && /medical_record_number/i.test(reason)) throw new Error("That medical record number is already in use.");
    if (response.status === 409 && /treatment_cycles\./i.test(reason)) throw new Error("That cycle number is already used in this plan.");
    if (response.status === 409 && /patients\.phone/i.test(reason)) throw new Error("That mobile number is already registered to another patient.");
    const fields = result.error?.details?.fields;
    throw new Error(fields ? `${result.error.message}: ${fields.join(", ").replaceAll("_", " ")}` : reason);
  }
  return result.data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
const patch = (path, body) => api(path, { method: "PATCH", body: JSON.stringify(body) });
const del = path => api(path, { method: "DELETE" });

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
}
function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(`${String(value).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric" }).format(d);
}
function fmtDateTime(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? fmtDate(value) : new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }).format(d);
}
function fmtShort(value) {
  if (!value) return "—";
  const d = new Date(`${value}T00:00:00`);
  return new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short" }).format(d);
}
function fmtNum(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Math.abs(n) >= 100) return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return String(Math.round(n * 100) / 100);
}
function fmtUnit(u) { return esc(u || "").replace("10^3/uL", "10³/µL").replace("/uL", "/µL").replace("mg/m2", "mg/m²"); }
function todayISO() { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 10); }
function addDays(iso, days) { const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); }
function age(dob) {
  if (!dob) return null;
  const b = new Date(`${dob}T00:00:00`), n = new Date();
  let a = n.getFullYear() - b.getFullYear();
  if (n < new Date(n.getFullYear(), b.getMonth(), b.getDate())) a--;
  return a;
}
function patientName(p) { return `${p.first_name} ${p.last_name}`.trim(); }
function initials(p) { return `${p.first_name?.[0] || ""}${p.last_name?.[0] || ""}`.toUpperCase(); }
function statusPill(status) {
  const tone = { given: "complete", modified: "modified", delayed: "planned", held: "alert", discontinued: "alert", cancelled: "neutral", planned: "neutral" }[status] || "neutral";
  return `<span class="status-pill ${tone}">${esc(STATUS_LABEL[status] || status)}</span>`;
}
function gradeBadge(grade, extra = "") {
  if (!grade) return `<span class="grade-badge grade-0 ${extra}" title="No CTCAE grade">G0</span>`;
  return `<span class="grade-badge grade-${grade} ${extra}" title="CTCAE grade ${grade}">G${grade}</span>`;
}
function labDot(status) {
  const label = { verified: "Labs verified", unverified: "Labs awaiting verification", missing: "No pre-cycle labs" }[status];
  return `<span class="lab-marker ${status}" title="${label}"><i class="lab-dot ${status}"></i>${label}</span>`;
}
function pretty(text) { return esc(text).replaceAll("10^3/uL", "10³/µL").replaceAll("/uL", "/µL").replaceAll("mg/m2", "mg/m²"); }
function isBad(t) { return Boolean(t && (t.meaningful_change || t.latest.abnormal)); }
function fmtAxis(v) { return fmtNum(Number(Number(v).toPrecision(2))); }
function trendArrow(direction) { return direction === "up" ? "↑" : direction === "down" ? "↓" : "→"; }
function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3600);
}
function formError(form, message) {
  const box = $(".form-error", form);
  box.textContent = message || "";
  box.hidden = !message;
}
async function withBusy(form, fn) {
  const submit = $('button[type="submit"]', form);
  if (submit) submit.disabled = true;
  formError(form, "");
  try { await fn(); } catch (error) { formError(form, error.message); } finally { if (submit) submit.disabled = false; }
}
function openModal(id) {
  const modal = document.getElementById(id);
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  setTimeout(() => $("input:not([type=hidden]):not([type=radio]):not([type=checkbox]), select, textarea", modal)?.focus(), 20);
}
function closeModals() {
  $$(".modal").forEach(m => { m.hidden = true; });
  document.body.style.overflow = "";
}
function confirmDialog(title, body, action, okLabel = "Delete") {
  $("#confirmTitle").textContent = title;
  $("#confirmBody").textContent = body;
  $("#confirmOk").textContent = okLabel;
  S.confirmAction = action;
  openModal("confirmModal");
}
function cyclesOf() { return S.L?.cycles || []; }
function cycleById(id) { return cyclesOf().find(c => c.id === Number(id)); }
function regimenDrugs() { return (S.L?.medications || []).map(m => m.drug_name); }
function termInfo(term) { return S.reference?.ctcae_terms.find(t => t.term === term); }
function catalog(test) { return S.reference?.lab_catalog.find(t => t.test_name === test); }

/* ------------------------------------------------------------------ patient messages */
const CHECKLIST_LABEL = { nausea: "Feeling sick", vomiting: "Vomiting", diarrhoea: "Loose stools", constipation: "Constipation", mouth_sores: "Mouth sores",
  appetite: "Poor appetite", taste: "Taste change", tiredness: "Tiredness", tingling: "Tingling / numbness", hearing: "Hearing change",
  ringing_ears: "Ringing in ears", hair_loss: "Hair loss", rash: "Rash", muscle_pain: "Muscle pain", joint_pain: "Joint pain", swelling: "Swelling",
  fever: "Fever / chills", bleeding: "Bleeding / bruising", breathless: "Short of breath", urine_blood: "Blood in urine", hand_foot: "Hand-foot skin", other: "Other" };
const REPORT_STATUS = { new: ["Awaiting review", "chip-warn"], reviewed: ["Reviewed", "chip-ok"], recorded: ["Recorded as CTCAE", "chip-teal"], dismissed: ["Dismissed", ""] };
const SAFETY_SHORT = { fever: "Fever ≥38 °C / chills", fluids: "Keeps fluids down", urine: "Passing urine", bleeding: "Bleeding / black stool", breathing: "Breathless / chest pain" };
const SAFETY_BAD = { fever: ["yes"], fluids: ["no"], urine: ["less", "none"], bleeding: ["yes"], breathing: ["yes"] };
function checkinHtml(r, { actions = false, compact = false } = {}) {
  const c = r.checkin;
  if (!c) return "";
  const recorded = new Map((r.recorded || []).map(x => [x.ctcae_term, x]));
  if (compact) {
    return `<div class="ci-compact">${c.items.map(i => `<span class="chip">${esc(i.label)}${i.grade_suggestion ? ` · G${i.grade_suggestion}?` : ""}</span>`).join("") || '<span class="chip">No new symptoms</span>'}</div>`;
  }
  const items = c.items.map(i => {
    const done = i.term && recorded.get(i.term);
    return `<div class="ci-item">
      <div class="ci-head">
        <div><strong>${esc(i.label)}</strong>${i.term && i.term !== i.label ? ` <span class="muted small">→ ${esc(i.term)}</span>` : i.term ? "" : ' <span class="muted small">→ choose CTCAE term</span>'}
          ${i.onset ? `<span class="muted small"> · since ${fmtShort(i.onset)}</span>` : ""}</div>
        <div class="ci-grade">
          ${i.grade_suggestion ? `<span class="grade-badge grade-${i.grade_suggestion}" title="Suggested from answers; not confirmed">G${i.grade_suggestion}?</span>` : ""}
          ${done ? `<span class="chip chip-teal">✓ Recorded G${done.grade}</span>` : actions ? `<button class="button ${i.grade_suggestion ? "primary" : "secondary"} small" data-report-record="${r.id}" data-item="${esc(i.key)}" data-term="${esc(i.term || "")}" data-grade="${i.grade_suggestion || ""}">Grade &amp; record</button>` : ""}
        </div>
      </div>
      <table class="ci-answers"><tbody>${i.answers.map(a => `<tr><th>${esc(a.question)}</th><td>${esc(a.answer)}${a.heard ? `<small class="ci-heard">🎤 “${esc(a.heard)}”</small>` : ""}</td></tr>`).join("")}</tbody></table>
      ${i.onset_heard ? `<p class="ci-rationale">Onset — patient said 🎤 “${esc(i.onset_heard)}”</p>` : ""}
      <p class="ci-rationale">${i.grade_suggestion ? "Suggested" : "No grade suggested"}: ${esc(i.rationale)}</p>
    </div>`;
  }).join("");
  const safety = (c.safety || []).map(a => `<span class="chip ${SAFETY_BAD[a.id]?.includes(a.value) ? "chip-alert" : ""}" title="${esc(a.question)}${a.heard ? ` — patient said: ${esc(a.heard)}` : ""}">${a.heard ? "🎤 " : ""}${esc(SAFETY_SHORT[a.id] || a.id)}: ${esc(a.answer)}</span>`).join("");
  return `<section class="checkin">
    <p class="eyebrow">Patient check-in${c.by_voice ? " · questions asked and answered by voice" : ""}${r.ui_language && r.ui_language !== "en" ? ` · answered in ${{ hi: "Hindi", te: "Telugu" }[r.ui_language] || r.ui_language}` : ""}</p>
    ${items || '<p class="muted small">No new symptoms reported.</p>'}
    ${safety ? `<div class="ci-safety"><span class="muted small">Safety check</span>${safety}</div>` : ""}
  </section>`;
}
function reportContextHtml(ctx, open = false) {
  if (!ctx) return "";
  const lg = ctx.last_given, nc = ctx.next_cycle, reg = ctx.regimen;
  const timing = lg ? `C${lg.cycle} given ${fmtShort(lg.date)} · <strong>day ${lg.days_since}</strong> after dose${lg.doses?.length ? ` (${esc(lg.doses.join(", "))})` : ""}`
    : "No cycle recorded as given yet";
  const due = ctx.last_due ? ` · <span class="text-amber">C${ctx.last_due.cycle} was due ${fmtShort(ctx.last_due.date)}, not recorded</span>` : "";
  const labs = (ctx.labs || []).map(l => `<span class="ctx-lab ${l.flag ? "flag-" + l.flag : ""}" title="${esc(fmtShort(l.date))}${l.previous != null ? ` · previous ${l.previous}` : ""}">${esc(l.test)} <b>${l.value}</b>${l.flag ? (l.flag === "high" ? " ↑" : " ↓") : ""}${l.ctcae ? ` <em>${esc(l.ctcae.replace(/^.* (G\d)$/, "$1"))}</em>` : ""}</span>`).join("");
  const tox = (ctx.toxicities || []).map(t => `<span class="chip">${esc(t.ctcae_term)} G${t.grade}</span>`).join("");
  return `<details class="report-context" ${open ? "open" : ""}>
    <summary>Patient context <span class="muted small">as of ${fmtShort(ctx.as_of)}</span></summary>
    <dl>
      <dt>Patient</dt><dd>${ctx.age != null ? `${ctx.age} y` : "Age not recorded"}${ctx.sex ? ` · ${esc(ctx.sex)}` : ""}${ctx.diagnosis ? ` · ${esc(ctx.diagnosis)}` : ""}</dd>
      <dt>Regimen</dt><dd>${reg ? `${esc(reg.name)}${reg.drugs?.length ? ` — ${esc(reg.drugs.join(", "))}` : ""}` : "No active plan"}</dd>
      <dt>Timing</dt><dd>${timing}${due}${nc ? ` · next C${nc.cycle} ${fmtShort(nc.date)} (in ${nc.days_until} d)` : ""}</dd>
      <dt>Recorded CTCAE</dt><dd>${tox || '<span class="muted">None recorded</span>'}</dd>
      <dt>Latest labs</dt><dd>${labs ? `<div class="ctx-labs">${labs}</div><small class="muted">${fmtShort(ctx.labs[0].date)}</small>` : '<span class="muted">No verified labs</span>'}</dd>
      ${ctx.previous_reports?.length ? `<dt>Earlier messages</dt><dd>${ctx.previous_reports.slice(0, 3).map(p => `<div class="small">${fmtShort(p.date)} — ${esc(p.summary)}</div>`).join("")}</dd>` : ""}
    </dl>
  </details>`;
}
function reportCardHtml(r, { showPatient = false, actions = false, compact = false } = {}) {
  const ai = r.ai;
  const [statusLabel, statusTone] = REPORT_STATUS[r.status] || [r.status, ""];
  const candidates = ai?.candidates || [];
  const extra = ai?.keyword_candidates || [];
  return `<article class="report-card ${r.red_flags.length && r.status === "new" ? "urgent" : ""} ${compact ? "compact" : ""}" data-report="${r.id}">
    <header>
      ${showPatient ? `<a class="report-patient" href="#patient/${r.patient_id}"><strong>${esc(r.patient_name)}</strong><small>${esc(r.medical_record_number)}</small></a>` : ""}
      <span class="chip ${statusTone}">${statusLabel}</span>
      <span class="muted small">Sent ${fmtDateTime(r.created_at)} · ${r.input_mode === "voice" ? "🎤 voice" : "⌨ text"}${r.language ? ` · ${esc(r.language)}` : ""}${r.cycle_number ? ` · before C${r.cycle_number}` : ""}${r.onset_date ? ` · onset ${fmtShort(r.onset_date)}` : ""}</span>
    </header>
    ${r.red_flags.length ? `<div class="red-flags">${r.red_flags.map(f => `<span class="chip chip-alert" title="${esc(f.why)}">⚠ ${esc(f.label)}</span>`).join("")}<small>Patient was told to contact the care team or emergency services now.</small></div>` : ""}
    ${r.checkin && r.text.startsWith("Check-in:") ? "" : `<blockquote class="patient-quote">${esc(r.text)}${ai?.translation_en && ai.translation_en.trim() !== r.text.trim() ? `<span class="translation"><b>English${ai.language ? ` (from ${esc(ai.language)})` : ""}:</b> ${esc(ai.translation_en)}</span>` : ""}</blockquote>`}
    ${r.has_audio ? `<audio controls preload="none" src="/api/patient-reports/${r.id}/audio"></audio>` : ""}
    ${checkinHtml(r, { actions, compact })}
    <div class="drug-chips">${(r.checkin ? [] : r.symptoms).map(k => `<span class="chip">${esc(CHECKLIST_LABEL[k] || k)}</span>`).join("")}${r.impact_text ? `<span class="chip chip-teal">${esc(r.impact_text)}</span>` : ""}${r.temperature ? `<span class="chip ${r.temperature >= 38 ? "chip-alert" : ""}">${r.temperature} °C</span>` : ""}</div>
    ${compact || (r.status !== "new" && actions) ? "" : `<div class="ai-box">
      <div class="ai-head"><p class="eyebrow">${ai?.method === "claude" ? `AI suggestion · ${esc(ai.model || "Claude")}` : r.ai_status === "pending" ? "AI suggestion · processing…" : "Keyword suggestion"}</p>
        ${r.ai_status !== "pending" ? `<button class="link-button small" type="button" data-report-retriage="${r.id}">${ai?.method === "claude" ? "Re-analyse" : "Analyse with AI"}</button>` : ""}</div>
      ${ai?.error ? `<p class="ai-error">AI couldn't analyse this message: ${esc(ai.error)} Showing keyword matches instead.</p>` : ""}
      ${ai?.summary_en ? `<p class="ai-summary">${esc(ai.summary_en)}</p>` : ""}
      ${ai?.possible_red_flags?.length ? `<div class="red-flags ai-flags">${ai.possible_red_flags.map(f => `<span class="chip chip-amber" title="AI-suggested, not rule-based">⚑ ${esc(f)}</span>`).join("")}</div>` : ""}
      ${candidates.length || extra.length ? `<div class="candidate-list">${[...candidates, ...extra.map(c => ({ ...c, keyword: true }))].map(c => `<div class="candidate">
          <div><strong>${esc(c.term)}</strong>${c.grade_suggestion ? ` <span class="grade-badge grade-${c.grade_suggestion} small" title="Suggested, not confirmed">G${c.grade_suggestion}?</span>` : ""}${c.associated_drugs?.length ? ` <span class="chip">${esc(c.associated_drugs.join(", "))}</span>` : ""}
            <small>${esc(c.evidence || "")}${c.rationale && !c.keyword ? ` — ${esc(c.rationale)}` : ""}</small></div>
          ${actions ? `<button class="button secondary small" data-report-record="${r.id}" data-term="${esc(c.term)}" data-grade="${c.grade_suggestion || ""}">Grade &amp; record</button>` : ""}
        </div>`).join("")}</div>` : '<p class="muted small">No CTCAE term suggested. Choose one when recording.</p>'}
      ${ai?.questions_for_clinician?.length ? `<p class="muted small">Consider asking: ${ai.questions_for_clinician.map(esc).join(" · ")}</p>` : ""}
    </div>`}
    ${compact ? "" : reportContextHtml(r.context || ai?.context_used, showPatient)}
    ${r.patient_message ? `<div class="reply-line"><strong>Reply to patient:</strong> ${esc(r.patient_message)}</div>` : ""}
    ${r.doctor_note ? `<div class="reply-line muted"><strong>Internal note:</strong> ${esc(r.doctor_note)}</div>` : ""}
    <footer>
      ${showPatient && r.treatment_plan_id ? `<a class="button primary small" href="#plan/${r.treatment_plan_id}/toxicities">Review in plan →</a>` : ""}
      ${actions ? (r.checkin?.items?.length ? `<button class="button secondary small" data-report-record="${r.id}">Add a different toxicity</button>`
        : `<button class="button ${r.status === "new" ? "primary" : "secondary"} small" data-report-record="${r.id}">${r.status === "new" ? "Review &amp; add as CTCAE toxicity" : "Add another toxicity"}</button>`) : ""}
      ${!compact ? `<button class="button secondary small" data-report-reply="${r.id}">${r.status === "new" ? "Reply / mark reviewed" : "Edit reply"}</button>` : ""}
      ${r.status === "new" && !compact ? `<button class="button secondary small" data-report-dismiss="${r.id}">Dismiss</button>` : ""}
    </footer>
    ${compact ? "" : `<form class="reply-form hidden" data-reply-form="${r.id}">
      <label class="field"><span>Message to patient (shown in their portal)</span><textarea name="patient_message" rows="2" maxlength="1000" placeholder="e.g. Thank you. Please keep a note of it; we will review before your next cycle.">${esc(r.patient_message || "")}</textarea></label>
      <label class="field"><span>Internal note (not shown to patient)</span><textarea name="doctor_note" rows="2" maxlength="2000">${esc(r.doctor_note || "")}</textarea></label>
      <div class="modal-actions"><button class="button secondary small" type="button" data-report-reply="${r.id}">Cancel</button><button class="button primary small" type="submit">Save and mark reviewed</button></div>
    </form>`}
  </article>`;
}
async function loadInbox() {
  const filter = $("#inboxFilter").value;
  try {
    const list = await api(`/api/patient-reports${filter ? `?status=${filter}` : ""}`);
    S.inbox = list;
    $("#inboxList").innerHTML = list.length ? list.map(r => reportCardHtml(r, { showPatient: true })).join("") : '<div class="queue-empty">No patient messages awaiting review.</div>';
  } catch (e) { $("#inboxList").innerHTML = `<div class="queue-empty">${esc(e.message)}</div>`; }
  refreshInboxBadge();
}
async function refreshInboxBadge() {
  try {
    const list = await api("/api/patient-reports?status=open");
    const badge = $("#inboxBadge");
    badge.textContent = list.length;
    badge.classList.toggle("hidden", !list.length);
    badge.classList.toggle("urgent", list.some(r => r.red_flags.length));
  } catch { /* ignore */ }
}
function findReport(id) {
  const all = [...(S.L?.patient_reports || []), ...(S.patient?.reports || []), ...(S.inbox || [])];
  return all.find(r => r.id === Number(id));
}
async function afterReportChange() {
  if (S.view === "inbox") await loadInbox();
  if (S.L) await refreshPlan();
  else if (S.patient) { await loadPatient(S.patient.id); }
  refreshInboxBadge();
}
/* ------------------------------------------------------------------ routing */
function navigate(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
async function route() {
  const [head, idRaw, tab] = location.hash.replace(/^#/, "").split("/");
  const id = Number(idRaw);
  try {
    if (head === "patient" && id) {
      if (S.patient?.id !== id) S.planId = null, S.L = null;
      await loadPatient(id);
      return show("patient");
    }
    if (head === "inbox") {
      show("inbox");
      return loadInbox();
    }
    if (head === "plan" && id) {
      await loadPlan(id);
      return show(PLAN_VIEWS.includes(tab) ? tab : "cycles");
    }
  } catch (error) {
    showToast(error.message);
  }
  show("patients");
  loadPatients();
}
function show(view) {
  S.view = view;
  $$(".view").forEach(v => v.classList.toggle("active", v.dataset.view === view));
  $("#navPatient").classList.toggle("hidden", !S.patient || view === "patients" || view === "inbox");
  $("#navPlan").classList.toggle("hidden", !S.L || !PLAN_VIEWS.includes(view));
  $$(".nav-item").forEach(item => {
    const nav = item.dataset.nav;
    item.classList.toggle("active", nav === view);
    if (nav === "patient" && S.patient) item.href = `#patient/${S.patient.id}`;
    if (PLAN_VIEWS.includes(nav) && S.L) item.href = `#plan/${S.L.plan.id}/${nav}`;
  });
  updateHeader();
  $(".sidebar").classList.remove("open");
  $("#mainContent").focus({ preventScroll: true });
  window.scrollTo({ top: 0 });
}
function updateHeader() {
  const p = view => S.view === view;
  const patient = p("patients") || p("inbox") ? null : S.patient;
  $("#selectedPatientAvatar").textContent = patient ? initials(patient) : "—";
  $("#selectedPatientName").textContent = patient ? patientName(patient) : "Select a patient";
  $("#selectedPatientMeta").textContent = patient ? `${patient.medical_record_number}${patient.diagnosis ? " · " + patient.diagnosis : ""}` : "Search or create a patient";
  if (S.patient) $("#navPatientName").textContent = patientName(S.patient);
  if (S.L) $("#navPlanName").textContent = S.L.plan.regimen_name;
}

/* ------------------------------------------------------------------ patients dashboard */
async function loadPatients() {
  const seq = ++S.searchSeq;
  const search = $("#patientSearch").value.trim();
  const status = $("#patientStatusFilter").value;
  $("#patientListStatus").textContent = "Loading patients…";
  try {
    const params = new URLSearchParams();
    if (search) params.set("search", search);
    if (status !== "all") params.set("status", status);
    const list = await api(`/api/patients?${params}`);
    if (seq !== S.searchSeq) return;
    S.patients = list;
    renderPatients(search);
  } catch (error) {
    if (seq !== S.searchSeq) return;
    $("#patientListStatus").textContent = "Unable to load patients";
    $("#patientList").innerHTML = `<div class="patient-empty"><h2>Service unavailable</h2><p>${esc(error.message)}</p><button class="button secondary" data-action="retry-patients">Retry</button></div>`;
  }
}
function renderPatients(search) {
  const list = S.patients;
  const today = todayISO(), week = addDays(today, 7);
  const active = list.filter(p => p.active).length;
  const due = list.filter(p => p.next_cycle_date && p.next_cycle_date <= week).length;
  const tox = list.filter(p => (p.open_toxicity_grade || 0) >= 2).length;
  $("#patientMetrics").innerHTML = [
    ["Patients shown", list.length, `${active} active`, "♙", ""],
    ["Cycles due ≤ 7 days", due, "next planned cycle", "◎", due ? "pending" : ""],
    ["Open toxicity ≥ G2", tox, "clinician-confirmed", "!", tox ? "alert" : ""],
    ["Patient messages", list.reduce((n, p) => n + (p.open_reports || 0), 0), `${list.reduce((n, p) => n + (p.urgent_reports || 0), 0)} with red flags`, "✉", list.some(p => p.urgent_reports) ? "alert" : list.some(p => p.open_reports) ? "pending" : ""],
  ].map(([l, v, d, i, t]) => `<article class="metric-card"><div><small>${l}</small><strong>${v}</strong><p>${d}</p></div><span class="metric-dot ${t}">${i}</span></article>`).join("");
  $("#patientListStatus").textContent = `${list.length} patient${list.length === 1 ? "" : "s"}${search ? " matching" : ""}`;
  $("#patientList").innerHTML = list.length ? list.map(p => {
    const a = age(p.date_of_birth);
    const next = p.next_cycle_date ? `<span class="chip ${p.next_cycle_date < today ? "chip-alert" : p.next_cycle_date <= week ? "chip-warn" : ""}">C${p.next_cycle_number} · ${fmtShort(p.next_cycle_date)}</span>` : "";
    return `<div class="patient-row ${p.active ? "" : "inactive"}">
      <button class="patient-row-open" data-open-patient="${p.id}" aria-label="Open ${esc(patientName(p))}">
        <span class="avatar">${esc(initials(p))}</span>
        <span class="patient-row-main"><strong>${esc(patientName(p))}${p.active ? "" : ' <em class="inactive-tag">Inactive</em>'}</strong><small>${esc(p.medical_record_number)}${p.phone ? ` · ☎ ${esc(p.phone)}` : ""}${a !== null ? ` · ${a} y` : ""}${p.sex ? ` · ${esc(p.sex)}` : ""}</small></span>
        <span class="patient-row-detail"><span>${esc(p.diagnosis || "No diagnosis recorded")}</span><small>${p.active_regimen ? esc(p.active_regimen) : p.plan_count ? `${p.plan_count} plan(s)` : "No chemotherapy plan"}</small></span>
        <span class="patient-row-signals">${p.open_reports ? `<span class="chip ${p.urgent_reports ? "chip-alert" : "chip-warn"}" title="Patient messages awaiting review">✉ ${p.open_reports}</span>` : ""}${next}${p.open_toxicity_grade ? gradeBadge(p.open_toxicity_grade, "small") : ""}</span>
      </button>
      <div class="row-actions"><button class="icon-action" data-edit-patient="${p.id}" title="Edit patient" aria-label="Edit ${esc(patientName(p))}">✎</button><button class="icon-action danger" data-delete-patient="${p.id}" title="Delete patient" aria-label="Delete ${esc(patientName(p))}">🗑</button></div>
    </div>`;
  }).join("") : `<div class="patient-empty"><h2>No patients found</h2><p>${search ? "Try another name, MRN or diagnosis." : "Create the first patient to begin."}</p><button class="button primary" data-action="new-patient">Create patient</button></div>`;
}

function openPatientForm(patient = null) {
  const form = $("#patientForm");
  form.reset();
  formError(form, "");
  S.editingPatientId = patient?.id || null;
  $("#patientModalTitle").textContent = patient ? "Edit patient" : "Create patient";
  $("#patientActiveField").classList.toggle("hidden", !patient);
  form.elements.date_of_birth.max = todayISO();
  if (patient) {
    for (const f of ["medical_record_number", "first_name", "last_name", "date_of_birth", "sex", "diagnosis", "phone"]) form.elements[f].value = patient[f] || "";
    form.elements.active.value = patient.active ? "1" : "0";
  }
  openModal("patientModal");
}
async function submitPatient(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  for (const k of Object.keys(data)) data[k] = String(data[k]).trim();
  const missing = ["medical_record_number", "first_name", "last_name"].filter(k => !data[k]);
  if (missing.length) return formError(form, "Medical record number, first name and last name are required.");
  if (!S.editingPatientId) delete data.active; else data.active = data.active === "1";
  for (const k of ["date_of_birth", "sex", "diagnosis", "phone"]) if (!data[k]) data[k] = null;
  if (data.phone && data.phone.replace(/\D/g, "").length < 8) return formError(form, "Enter a valid mobile number (10 digits).");
  await withBusy(form, async () => {
    const saved = S.editingPatientId ? await patch(`/api/patients/${S.editingPatientId}`, data) : await post("/api/patients", data);
    closeModals();
    showToast(S.editingPatientId ? "Patient updated." : "Patient created.");
    if (S.view === "patients") await loadPatients();
    if (S.editingPatientId && S.patient?.id === saved.id) { await loadPatient(saved.id); updateHeader(); if (S.L) { S.L.patient = S.patient; renderPlan(); } }
    else if (!S.editingPatientId) navigate(`#patient/${saved.id}`);
  });
}
function deletePatient(id) {
  const p = S.patients.find(x => x.id === id) || (S.patient?.id === id ? S.patient : null);
  confirmDialog(`Delete ${p ? patientName(p) : "patient"}?`, "This permanently removes the patient with all plans, cycles, labs, uploaded reports and toxicity records. Consider marking the patient inactive instead.", async () => {
    await del(`/api/patients/${id}`);
    if (S.patient?.id === id) { S.patient = null; S.L = null; }
    showToast("Patient deleted.");
    navigate("#patients");
    loadPatients();
  });
}

/* ------------------------------------------------------------------ patient record */
async function loadPatient(id) {
  const [patient, portal, reports] = await Promise.all([
    api(`/api/patients/${id}`), api(`/api/patients/${id}/portal`).catch(() => null), api(`/api/patients/${id}/patient-reports`).catch(() => [])]);
  S.patient = { ...patient, portal, reports };
  renderPatient();
}
function renderPatient() {
  const p = S.patient;
  const a = age(p.date_of_birth);
  $("#patientTitle").textContent = patientName(p);
  $("#patientSubtitle").textContent = `${p.medical_record_number} · ${p.active ? "Active" : "Inactive"}${a !== null ? ` · ${a} years` : ""}${p.phone ? ` · ☎ ${p.phone}` : ""}`;
  const plans = p.treatment_plans || [];
  $("#patientProfile").innerHTML = `
    <div class="patient-profile-grid">
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Identity</p><h2>Demographics</h2></div></div>
        <dl class="definition-grid">
          <div><dt>Medical record number</dt><dd>${esc(p.medical_record_number)}</dd></div>
          <div><dt>Date of birth</dt><dd>${p.date_of_birth ? `${fmtDate(p.date_of_birth)} (${a} y)` : "Not recorded"}</dd></div>
          <div><dt>Sex</dt><dd>${esc(p.sex || "Not recorded")}</dd></div>
          <div><dt>Status</dt><dd>${p.active ? "Active" : "Inactive"}</dd></div>
          <div><dt>Mobile</dt><dd>${esc(p.phone || "Not recorded")}</dd></div>
        </dl>
      </article>
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Clinical context</p><h2>Diagnosis</h2></div></div><p class="diagnosis-text">${esc(p.diagnosis || "No diagnosis recorded yet.")}</p></article>
      <article class="panel span-full portal-panel"><div class="panel-heading"><div><p class="eyebrow">Patient portal</p><h2>View-only portal and symptom messages</h2></div>
          <div class="heading-actions">${p.portal?.enabled ? `<button class="button danger-outline small" data-action="portal-disable">Turn off portal access</button>` : `<button class="button primary small" data-action="portal-enable">Turn on portal access</button>`}</div></div>
        <p class="muted">${!p.phone ? "Add the patient's mobile number (Edit patient) so they can sign in to the portal." :
          p.portal?.enabled ? `Patient signs in at <code>${esc(location.origin)}/portal</code> with mobile <strong>${esc(p.phone)}</strong> and the temporary password (OTP sign-in coming). Last sign-in: ${p.portal.last_login_at ? fmtDate(p.portal.last_login_at) : "never"}.` :
          "Portal access is turned off; the patient cannot sign in."}</p>
        ${(p.reports || []).length ? `<div class="report-list compact">${p.reports.slice(0, 5).map(r => reportCardHtml(r, { compact: true })).join("")}</div>` : ""}
      </article>
      <article class="panel span-full"><div class="panel-heading"><div><p class="eyebrow">Chemotherapy</p><h2>Treatment plans</h2></div><button class="button primary" data-action="new-plan">+ Create plan</button></div>
        ${plans.length ? plans.map(plan => {
          const c = plan.cycle_counts;
          const pct = c.total ? Math.round((c.given / c.total) * 100) : 0;
          return `<div class="plan-row">
            <div class="plan-row-main"><strong>${esc(plan.regimen_name)}</strong>
              <small>${esc(plan.intent || "Intent not recorded")} · ${esc(plan.status.replace("_", " "))}${plan.start_date ? ` · from ${fmtDate(plan.start_date)}` : ""}${plan.cycle_interval_days ? ` · every ${plan.cycle_interval_days} days` : ""}</small>
              <div class="drug-chips">${plan.medications.map(m => `<span class="chip">${esc(m.drug_name)} ${fmtNum(m.dose)} ${fmtUnit(m.dose_unit)}</span>`).join("")}</div>
            </div>
            <div class="plan-row-progress"><div class="progress-track"><span style="width:${pct}%"></span></div><small>${c.given}/${c.total} given${c.modified ? ` · ${c.modified} modified` : ""}${plan.next_cycle ? ` · next C${plan.next_cycle.cycle_number} ${fmtShort(plan.next_cycle.planned_date)}` : ""}</small></div>
            <div class="plan-row-actions"><a class="button secondary" href="#plan/${plan.id}/cycles">Open plan</a><button class="icon-action danger" data-delete-plan="${plan.id}" title="Delete plan" aria-label="Delete plan">🗑</button></div>
          </div>`;
        }).join("") : '<div class="queue-empty">No chemotherapy plan yet. Create a plan to schedule cycles, drugs and doses.</div>'}
      </article>
    </div>`;
}

/* ------------------------------------------------------------------ plan builder */
function newMed(drug = "", dose = "", unit = "mg/m2", route = "IV infusion") { return { drug_name: drug, planned_dose: dose, dose_unit: unit, route }; }
function medRowHtml(med, attrs, removable) {
  return `<div class="med-row" ${attrs}>
    <label class="field"><span>Drug</span><input list="drugList" data-f="drug_name" value="${esc(med.drug_name)}" maxlength="120" placeholder="e.g. Cisplatin" required /></label>
    <label class="field"><span>Dose</span><input type="number" data-f="planned_dose" value="${esc(med.planned_dose)}" min="0" step="any" required /></label>
    <label class="field"><span>Unit</span><input list="unitList" data-f="dose_unit" value="${esc(med.dose_unit)}" maxlength="40" required /></label>
    <label class="field"><span>Route</span><input data-f="route" value="${esc(med.route || "")}" maxlength="80" /></label>
    <button type="button" class="icon-action danger remove-med" data-remove-med ${removable ? "" : "disabled"} aria-label="Remove drug">×</button>
  </div>`;
}
function renderPlanMeds() {
  $("#planMedRows").innerHTML = S.planDraft.meds.map((m, i) => medRowHtml(m, `data-plan-med="${i}"`, S.planDraft.meds.length > 1)).join("");
}
function openPlanForm(edit = false) {
  const form = $("#planForm");
  form.reset();
  formError(form, "");
  S.editingPlan = edit;
  $("#planModalTitle").textContent = edit ? "Edit plan details" : "Create chemotherapy plan";
  $("#planModalStep").textContent = edit ? "Plan details" : "Step 1 of 2 · Regimen";
  $("#planSubmit").textContent = edit ? "Save changes" : "Review cycles →";
  $("#planMedsSection").classList.toggle("hidden", edit);
  $(".plan-schedule-fields").classList.toggle("hidden", edit);
  if (edit) {
    const plan = S.L.plan;
    for (const f of ["regimen_name", "intent", "status", "notes"]) form.elements[f].value = plan[f] || "";
  } else {
    S.planDraft = { meds: [newMed()] };
    form.elements.start_date.value = todayISO();
    renderPlanMeds();
  }
  openModal("planModal");
}
async function submitPlan(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const v = Object.fromEntries(new FormData(form));
  if (!String(v.regimen_name || "").trim()) return formError(form, "Enter a regimen name.");
  if (S.editingPlan) {
    return withBusy(form, async () => {
      await patch(`/api/treatment-plans/${S.L.plan.id}`, { regimen_name: v.regimen_name.trim(), intent: v.intent || null, status: v.status, notes: v.notes.trim() || null });
      closeModals();
      await refreshPlan();
      showToast("Plan details updated.");
    });
  }
  const count = Number(v.cycle_count), interval = Number(v.cycle_interval);
  if (!v.start_date) return formError(form, "Enter the first cycle date.");
  if (!Number.isInteger(count) || count < 1 || count > 24) return formError(form, "Number of cycles must be 1–24.");
  if (!Number.isInteger(interval) || interval < 1 || interval > 365) return formError(form, "Days between cycles must be 1–365.");
  const meds = S.planDraft.meds.map(m => ({ ...m, drug_name: m.drug_name.trim(), dose_unit: m.dose_unit.trim(), route: (m.route || "").trim() }));
  const bad = meds.find(m => !m.drug_name || m.planned_dose === "" || Number(m.planned_dose) < 0 || !m.dose_unit);
  if (bad) return formError(form, "Every drug needs a name, a non-negative dose and a unit.");
  const names = meds.map(m => m.drug_name.toLowerCase());
  if (new Set(names).size !== names.length) return formError(form, "Each drug should appear once.");
  S.planDraft = {
    meds,
    header: { regimen_name: v.regimen_name.trim(), intent: v.intent || null, status: v.status, start_date: v.start_date, cycle_interval_days: interval, notes: v.notes.trim() || null },
    cycles: Array.from({ length: count }, (_, i) => ({ cycle_number: i + 1, planned_date: addDays(v.start_date, i * interval), meds: meds.map(m => ({ ...m })) })),
  };
  renderPlanPreview();
  closeModals();
  openModal("planPreviewModal");
}
function renderPlanPreview() {
  const d = S.planDraft;
  $("#planPreviewSummary").textContent = `${d.header.regimen_name} · ${d.cycles.length} cycles every ${d.header.cycle_interval_days} days · ${patientName(S.patient)}. Change any date, drug or dose before saving.`;
  $("#planPreviewCycles").innerHTML = d.cycles.map((c, ci) => {
    const changed = c.meds.length !== d.meds.length || c.meds.some((m, i) => !d.meds[i] || m.drug_name !== d.meds[i].drug_name || Number(m.planned_dose) !== Number(d.meds[i].planned_dose) || m.dose_unit !== d.meds[i].dose_unit);
    return `<section class="preview-cycle ${changed ? "changed" : ""}" data-cycle-index="${ci}">
      <header><strong>Cycle ${c.cycle_number}</strong><label class="inline-field"><span>Date</span><input type="date" data-cycle-date="${ci}" value="${c.planned_date}" required /></label>${changed ? '<span class="chip chip-warn">Differs from default</span>' : ""}<span class="spacer"></span><button type="button" class="button secondary small" data-add-preview-med="${ci}">+ Drug</button></header>
      ${c.meds.map((m, mi) => medRowHtml(m, `data-preview-med="${ci}:${mi}"`, c.meds.length > 1)).join("")}
    </section>`;
  }).join("");
}
async function submitPlanPreview(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const d = S.planDraft;
  for (const c of d.cycles) {
    if (!c.planned_date) return formError(form, `Cycle ${c.cycle_number} needs a date.`);
    const bad = c.meds.find(m => !String(m.drug_name).trim() || m.planned_dose === "" || Number(m.planned_dose) < 0 || !String(m.dose_unit).trim());
    if (bad) return formError(form, `Cycle ${c.cycle_number}: every drug needs a name, dose and unit.`);
  }
  const dates = d.cycles.map(c => c.planned_date);
  const unordered = dates.some((x, i) => i && x < dates[i - 1]);
  await withBusy(form, async () => {
    const plan = await post(`/api/patients/${S.patient.id}/treatment-plans`, {
      ...d.header,
      medications: d.meds.map(m => ({ drug_name: m.drug_name, dose: Number(m.planned_dose), dose_unit: m.dose_unit, route: m.route || null })),
      cycles: d.cycles.map(c => ({ cycle_number: c.cycle_number, planned_date: c.planned_date,
        medications: c.meds.map(m => ({ drug_name: String(m.drug_name).trim(), planned_dose: Number(m.planned_dose), dose_unit: String(m.dose_unit).trim(), route: (m.route || "").trim() || null })) })),
    });
    closeModals();
    S.planDraft = null;
    showToast(`Plan saved with ${d.cycles.length} cycles.${unordered ? " Note: cycle dates are not in order." : ""}`);
    navigate(`#plan/${plan.id}/cycles`);
  });
}
function deletePlan(id) {
  const plan = S.patient?.treatment_plans?.find(p => p.id === id);
  confirmDialog(`Delete ${plan ? plan.regimen_name : "this plan"}?`, "All cycles, recorded doses, pre-cycle labs and decisions in this plan will be removed. Symptoms stay on the patient record.", async () => {
    await del(`/api/treatment-plans/${id}`);
    if (S.L?.plan.id === id) S.L = null;
    await loadPatient(S.patient.id);
    showToast("Plan deleted.");
    navigate(`#patient/${S.patient.id}`);
  });
}

/* ------------------------------------------------------------------ plan workspace */
async function loadPlan(id) {
  const data = await api(`/api/treatment-plans/${id}/longitudinal`);
  S.L = data;
  S.planId = id;
  if (!S.patient || S.patient.id !== data.patient.id) await loadPatient(data.patient.id);
  if (!S.reviewCycleId || !cycleById(S.reviewCycleId)) S.reviewCycleId = data.next_cycle_id || data.cycles.at(-1)?.id || null;
  renderPlan();
}
async function refreshPlan() {
  if (!S.L) return;
  await loadPlan(S.L.plan.id);
  await loadPatient(S.L.patient.id);
  updateHeader();
}
function renderPlan() {
  const L = S.L;
  $$(".plan-subtitle").forEach(el => { el.textContent = `${patientName(L.patient)} · ${L.plan.regimen_name}`; });
  $("#cyclesTitle").textContent = L.plan.regimen_name;
  renderPlanHeader();
  renderCycleList();
  renderReview();
  renderLabs();
  renderToxicities();
  renderTimeline();
  updateHeader();
}
function renderPlanHeader() {
  const L = S.L, s = L.summary, plan = L.plan;
  const next = cycleById(L.next_cycle_id);
  const pct = s.planned_cycles ? Math.round((s.given_cycles / s.planned_cycles) * 100) : 0;
  $("#planHeader").innerHTML = `
    <article class="cycle-hero">
      <div class="cycle-ring" style="--pct:${pct}"><span>${s.given_cycles}</span><small>of ${s.planned_cycles}</small></div>
      <div class="cycle-hero-copy">
        <div class="inline-heading"><span class="status-pill ${plan.status === "active" ? "complete" : "neutral"}">${esc(plan.status.replace("_", " "))}</span><span class="muted">${esc(plan.intent || "Intent not recorded")}${plan.cycle_interval_days ? ` · every ${plan.cycle_interval_days} days` : ""}</span></div>
        <h2>${esc(plan.regimen_name)}</h2>
        <p>${esc(L.patient.diagnosis || "")}</p>
        <div class="dose-line">
          ${L.medications.map(m => `<span><small>${esc(m.drug_name)}</small><strong>${fmtNum(m.dose)} ${fmtUnit(m.dose_unit)}${m.route ? ` · ${esc(m.route)}` : ""}</strong></span>`).join("")}
          <span><small>Next cycle</small><strong>${next ? `C${next.cycle_number} · ${fmtDate(next.planned_date)}` : "None pending"}</strong></span>
        </div>
        ${plan.notes ? `<p class="plan-notes">${esc(plan.notes)}</p>` : ""}
      </div>
      ${next ? `<a class="button light" href="#plan/${plan.id}/review" data-review-cycle="${next.id}">Review C${next.cycle_number} →</a>` : ""}
    </article>
    <div class="metric-grid">
      <article class="metric-card"><div><small>Given</small><strong>${s.given_cycles}/${s.planned_cycles}</strong><p>${s.modified_cycles} with modification</p></div><span class="metric-dot">✓</span></article>
      <article class="metric-card"><div><small>Delayed or held</small><strong>${s.delayed_or_held}</strong><p>cycles</p></div><span class="metric-dot ${s.delayed_or_held ? "pending" : ""}">⏸</span></article>
      <article class="metric-card"><div><small>Cycles without labs</small><strong>${s.cycles_missing_labs}</strong><p>pre-cycle labs optional</p></div><span class="metric-dot ${s.cycles_missing_labs ? "pending" : ""}">⌁</span></article>
      <article class="metric-card"><div><small>Cumulative dose</small><strong class="small-strong">${s.cumulative_exposure.length ? s.cumulative_exposure.map(e => `${esc(e.drug_name)} ${fmtNum(e.cumulative_dose)} ${fmtUnit(e.dose_unit)}`).join("<br>") : "—"}</strong><p>actual doses given</p></div><span class="metric-dot">Σ</span></article>
    </div>`;
}
function medsSummary(cycle) {
  return cycle.medications.map(m => {
    const given = m.actual_dose !== null && m.actual_dose !== undefined;
    let dose = `${fmtNum(m.planned_dose)} ${fmtUnit(m.dose_unit)}`;
    let cls = "";
    if (m.status === "held") { dose = `<s>${dose}</s> held`; cls = "chip-alert"; }
    else if (m.status === "modified" && given) {
      const pct = m.planned_dose ? Math.round(((m.actual_dose - m.planned_dose) / m.planned_dose) * 100) : 0;
      dose = `${fmtNum(m.planned_dose)} → <b>${fmtNum(m.actual_dose)}</b> ${fmtUnit(m.dose_unit)} (${pct > 0 ? "+" : ""}${pct}%)`;
      cls = "chip-warn";
    } else if (given) cls = "chip-ok";
    return `<span class="chip ${cls}">${esc(m.drug_name)} ${dose}</span>`;
  }).join("");
}
function renderCycleList() {
  const cycles = cyclesOf();
  $("#cycleList").innerHTML = cycles.length ? cycles.map(c => {
    const pending = ["planned", "delayed"].includes(c.status);
    const isNext = c.id === S.L.next_cycle_id;
    return `<div class="cycle-row ${isNext ? "next" : ""} status-${c.status}">
      <div class="cycle-num"><span>C${c.cycle_number}</span></div>
      <div class="cycle-main">
        <div class="cycle-main-top">${statusPill(c.status)}${isNext ? '<span class="chip chip-teal">Next</span>' : ""}</div>
        <small>Planned ${fmtDate(c.planned_date)}${c.administered_date ? ` · given ${fmtDate(c.administered_date)}` : ""}</small>
        ${c.modification_reason ? `<p class="cycle-reason"><b>${esc(c.reason_category || "Reason")}:</b> ${esc(c.modification_reason)}</p>` : ""}
      </div>
      <div class="cycle-meds">${medsSummary(c)}</div>
      <div class="cycle-signals">${labDot(c.labs_status)}${c.worst_grade ? `<span class="signal-grade">Worst CTCAE ${gradeBadge(c.worst_grade, "small")}</span>` : ""}</div>
      <div class="cycle-actions">
        ${pending || c.status === "delayed" ? `<button class="button primary small" data-record-cycle="${c.id}">Record</button>` : `<button class="button secondary small" data-record-cycle="${c.id}">Amend</button>`}
        <button class="button secondary small" data-review-cycle="${c.id}">Review</button>
        ${pending ? `<button class="icon-action" data-edit-cycle="${c.id}" title="Edit planned drugs and doses" aria-label="Edit cycle ${c.cycle_number}">✎</button>` : ""}
      </div>
    </div>`;
  }).join("") : '<div class="queue-empty">No cycles yet. Add a cycle to this plan.</div>';
}

/* ------------------------------------------------------------------ edit / add planned cycle */
function openCycleForm(cycleId = null) {
  const form = $("#cycleForm");
  form.reset();
  formError(form, "");
  const cycle = cycleId ? cycleById(cycleId) : null;
  const cycles = cyclesOf();
  const last = cycles.at(-1);
  S.cycleDraft = {
    id: cycle?.id || null,
    meds: cycle ? cycle.medications.map(m => ({ id: m.id, drug_name: m.drug_name, planned_dose: m.planned_dose, dose_unit: m.dose_unit, route: m.route || "" }))
      : S.L.medications.map(m => newMed(m.drug_name, m.dose, m.dose_unit, m.route || "")),
  };
  $("#cycleModalTitle").textContent = cycle ? `Edit cycle ${cycle.cycle_number}` : "Add cycle";
  form.elements.cycle_number.value = cycle ? cycle.cycle_number : (last?.cycle_number || 0) + 1;
  form.elements.planned_date.value = cycle ? cycle.planned_date : last ? addDays(last.planned_date, S.L.plan.cycle_interval_days || 21) : todayISO();
  $('[data-action="delete-cycle"]').classList.toggle("hidden", !cycle);
  renderCycleMeds();
  openModal("cycleModal");
}
function renderCycleMeds() {
  $("#cycleMedRows").innerHTML = S.cycleDraft.meds.map((m, i) => medRowHtml(m, `data-cycle-med="${i}"`, S.cycleDraft.meds.length > 1)).join("");
}
async function submitCycle(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const number = Number(form.elements.cycle_number.value);
  const planned = form.elements.planned_date.value;
  if (!Number.isInteger(number) || number < 1) return formError(form, "Enter a positive cycle number.");
  if (!planned) return formError(form, "Enter the planned date.");
  const meds = S.cycleDraft.meds.map(m => ({ ...(m.id ? { id: m.id } : {}), drug_name: String(m.drug_name).trim(), planned_dose: Number(m.planned_dose), dose_unit: String(m.dose_unit).trim(), route: String(m.route || "").trim() || null }));
  if (meds.some(m => !m.drug_name || !m.dose_unit || !Number.isFinite(m.planned_dose) || m.planned_dose < 0)) return formError(form, "Every drug needs a name, a non-negative dose and a unit.");
  await withBusy(form, async () => {
    if (S.cycleDraft.id) await patch(`/api/cycles/${S.cycleDraft.id}`, { cycle_number: number, planned_date: planned, medications: meds });
    else await post(`/api/treatment-plans/${S.L.plan.id}/cycles`, { cycle_number: number, planned_date: planned, medications: meds });
    closeModals();
    await refreshPlan();
    showToast(S.cycleDraft.id ? "Cycle updated. The previous version was preserved." : "Cycle added.");
  });
}

/* ------------------------------------------------------------------ record outcome */
function openRecordForm(cycleId) {
  const cycle = cycleById(cycleId);
  if (!cycle) return;
  S.recordingCycleId = cycle.id;
  const form = $("#recordForm");
  form.reset();
  formError(form, "");
  $("#recordModalTitle").textContent = `Record cycle ${cycle.cycle_number}`;
  $("#recordModalSub").textContent = `Planned ${fmtDate(cycle.planned_date)} · ${S.L.plan.regimen_name}`;
  form.elements.administered_date.value = cycle.administered_date || (cycle.planned_date <= todayISO() ? cycle.planned_date : todayISO());
  form.elements.administered_date.max = todayISO();
  form.elements.recorded_by.value = clinicianName();
  form.elements.new_planned_date.value = addDays(cycle.planned_date, 7);
  const alerts = cycle.attention.filter(a => a.level !== "info");
  $("#recordEvidence").innerHTML = `<div class="evidence-strip">${labDot(cycle.labs_status)}${cycle.ctcae.filter(i => i.grade).slice(0, 5).map(i => `<span class="chip">${esc(i.term)} ${gradeBadge(i.grade, "small")}</span>`).join("")}${alerts.length ? "" : '<span class="muted">No alert-level items in this window.</span>'}</div>`;
  $("#recordMedRows").innerHTML = cycle.medications.map(m => `<tr data-record-med="${m.id}" data-planned="${m.planned_dose}">
      <td><strong>${esc(m.drug_name)}</strong><small class="block muted">${esc(m.route || "")}</small></td>
      <td>${fmtNum(m.planned_dose)} ${fmtUnit(m.dose_unit)}</td>
      <td><div class="input-suffix light"><input type="number" min="0" step="any" data-actual value="${m.actual_dose ?? m.planned_dose}" aria-label="${esc(m.drug_name)} actual dose" /><span>${fmtUnit(m.dose_unit)}</span></div></td>
      <td class="dose-change" data-change>—</td>
      <td><input type="checkbox" data-hold ${m.status === "held" ? "checked" : ""} aria-label="Hold ${esc(m.drug_name)}" /></td>
    </tr>`).join("");
  const preset = ["given", "modified", "delayed", "held", "discontinued"].includes(cycle.status) ? cycle.status : "given";
  form.elements.outcome.value = preset;
  if (cycle.modification_reason) form.elements.rationale.value = cycle.modification_reason;
  if (cycle.reason_category) form.elements.reason_category.value = cycle.reason_category;
  updateRecordForm();
  openModal("recordModal");
}
function updateRecordForm() {
  const form = $("#recordForm");
  const outcome = form.elements.outcome.value;
  $("#recordGiven").classList.toggle("hidden", !["given", "modified"].includes(outcome));
  $("#recordDelay").classList.toggle("hidden", outcome !== "delayed");
  let anyChange = false;
  $$("#recordMedRows tr").forEach(row => {
    const planned = Number(row.dataset.planned);
    const hold = $("[data-hold]", row).checked;
    const input = $("[data-actual]", row);
    input.disabled = hold || outcome === "given";
    if (outcome === "given") { input.value = planned; $("[data-hold]", row).checked = false; }
    const actual = Number(input.value);
    const cell = $("[data-change]", row);
    if (hold && outcome !== "given") { cell.innerHTML = '<span class="chip chip-alert">Held</span>'; anyChange = true; }
    else if (input.value !== "" && Math.abs(actual - planned) > 1e-9) {
      const pct = planned ? Math.round(((actual - planned) / planned) * 100) : 0;
      cell.innerHTML = `<span class="chip chip-warn">${pct > 0 ? "+" : ""}${pct}%</span>`;
      anyChange = true;
    } else cell.textContent = "As planned";
    $("[data-hold]", row).disabled = outcome === "given";
  });
  $(".req-note").classList.toggle("hidden", outcome === "given");
  form.dataset.anyChange = anyChange ? "1" : "";
}
async function submitRecord(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const v = Object.fromEntries(new FormData(form));
  const outcome = v.outcome;
  if (!outcome) return formError(form, "Choose what happened with this cycle.");
  if (outcome !== "given" && (!v.reason_category || !String(v.rationale).trim())) return formError(form, "Add a reason category and a clinical rationale for any change from the plan.");
  const body = { outcome, reason_category: v.reason_category || null, rationale: String(v.rationale || "").trim() || null, recorded_by: v.recorded_by };
  if (["given", "modified"].includes(outcome)) {
    if (!v.administered_date) return formError(form, "Enter the administration date.");
    body.administered_date = v.administered_date;
    body.medications = $$("#recordMedRows tr").map(row => {
      const hold = $("[data-hold]", row).checked;
      const raw = $("[data-actual]", row).value;
      return { id: Number(row.dataset.recordMed), status: hold ? "held" : undefined, actual_dose: hold ? null : raw === "" ? null : Number(raw) };
    });
    if (outcome === "modified" && !form.dataset.anyChange && v.administered_date === cycleById(S.recordingCycleId).planned_date) return formError(form, "No dose change recorded. Change a dose, hold a drug, or choose “Given as planned”.");
  }
  if (outcome === "delayed") {
    if (!v.new_planned_date) return formError(form, "Enter the new planned date.");
    body.new_planned_date = v.new_planned_date;
    body.shift_following = form.elements.shift_following.checked;
  }
  await withBusy(form, async () => {
    await post(`/api/cycles/${S.recordingCycleId}/record`, body);
    closeModals();
    await refreshPlan();
    showToast(`Cycle recorded: ${OUTCOME_LABEL[outcome]}. Evidence snapshot saved with the decision.`);
  });
}

/* ------------------------------------------------------------------ pre-cycle review */
function renderReview() {
  const cycles = cyclesOf();
  $("#reviewCycleSelect").innerHTML = cycles.map(c => `<option value="${c.id}" ${c.id === S.reviewCycleId ? "selected" : ""}>Cycle ${c.cycle_number} · ${STATUS_LABEL[c.status] || c.status}${c.id === S.L.next_cycle_id ? " · next" : ""}</option>`).join("");
  const cycle = cycleById(S.reviewCycleId);
  if (!cycle) { $("#reviewContent").innerHTML = '<div class="queue-empty">No cycles in this plan.</div>'; return; }
  $("#reviewTitle").textContent = `Cycle ${cycle.cycle_number} review`;
  const prev = cycles.filter(c => c.cycle_number < cycle.cycle_number && ["given", "modified"].includes(c.status)).at(-1);
  const trends = S.L.lab_trends;
  const labRows = cycle.labs.map(r => {
    const t = trends.find(x => x.test_name === r.test_name);
    const before = t ? t.points.filter(p => p.cycle_number < cycle.cycle_number) : [];
    const prevPoint = before.at(-1);
    const bad = r.abnormal || (t && t.meaningful_change);
    return `<tr>
      <td><strong>${esc(r.test_name)}</strong><small class="block muted">${fmtShort(r.collection_date)}</small></td>
      <td><span class="value-strong ${r.abnormal ? "value-abnormal" : ""}">${fmtNum(r.value)}</span> <small>${fmtUnit(r.unit)}</small></td>
      <td>${fmtNum(r.ref_low)}–${fmtNum(r.ref_high)}</td>
      <td>${t ? `${fmtNum(t.baseline.value)} <small class="muted">C${t.baseline.cycle_number}</small>` : "—"}</td>
      <td>${prevPoint ? `${fmtNum(prevPoint.value)} <small class="muted">C${prevPoint.cycle_number}</small>` : "—"}</td>
      <td><span class="trend-chip ${r.direction} ${bad ? "bad" : "good"}">${trendArrow(r.direction)} ${r.pct_from_baseline === null || r.pct_from_baseline === undefined ? "—" : `${r.pct_from_baseline > 0 ? "+" : ""}${fmtNum(r.pct_from_baseline)}%`}</span></td>
      <td>${t ? sparkline(t, cycle.cycle_number) : ""}</td>
      <td>${r.ctcae_grade ? gradeBadge(r.ctcae_grade, "small") : '<span class="muted">—</span>'}</td>
      <td class="flag-cell">${flagChips(r.flags)}</td>
    </tr>`;
  }).join("");
  const missingLabs = cycle.labs_status === "missing";
  $("#reviewContent").innerHTML = `
    <div class="review-grid">
      <div class="review-main">
        <article class="panel evidence-panel">
          <div class="panel-heading"><div><p class="eyebrow">1 · Treatment context</p><h2>Planned for cycle ${cycle.cycle_number}</h2></div>${statusPill(cycle.status)}</div>
          <div class="context-row">
            <span><small>Planned date</small><strong>${fmtDate(cycle.planned_date)}</strong></span>
            <span><small>Previous cycle</small><strong>${prev ? `C${prev.cycle_number} · ${STATUS_LABEL[prev.status]} ${fmtShort(prev.administered_date)}` : "None (first cycle)"}</strong></span>
            <span><small>Cumulative before this cycle</small><strong>${cycle.exposure_before.length ? cycle.exposure_before.map(e => `${esc(e.drug_name)} ${fmtNum(e.cumulative_dose)} ${fmtUnit(e.dose_unit)}`).join(" · ") : "No prior exposure"}</strong></span>
          </div>
          <div class="drug-chips top-gap-sm">${medsSummary(cycle)}</div>
          ${prev ? `<p class="muted top-gap-sm">Previous doses: ${prev.medications.map(m => `${esc(m.drug_name)} ${m.actual_dose === null ? "held" : `${fmtNum(m.actual_dose)} ${fmtUnit(m.dose_unit)}`}`).join(" · ")}${prev.modification_reason ? ` — ${esc(prev.modification_reason)}` : ""}</p>` : ""}
        </article>
        <article class="panel evidence-panel">
          <div class="panel-heading"><div><p class="eyebrow">2 · Attention</p><h2>Changes since the previous cycle</h2></div><span class="count-badge ${cycle.attention.some(a => a.level === "alert") ? "" : "warning"}">${cycle.attention.length}</span></div>
          ${cycle.attention.length ? (() => {
            const item = a => `<div class="attention-item"><span class="signal-icon ${a.level}">${a.level === "alert" ? "!" : a.level === "warning" ? "▲" : a.direction === "down" ? "↘" : "↗"}</span><div><strong>${a.test_name && !a.text.startsWith(a.test_name) ? esc(a.test_name) + " — " : ""}${esc(a.text)}</strong>${a.detail ? `<p>${pretty(a.detail)}</p>` : ""}</div><span class="trend-label ${a.level}">${{ alert: "Review", warning: "Check", info: "Note" }[a.level]}</span></div>`;
            const main = cycle.attention.filter(a => a.level !== "info"), info = cycle.attention.filter(a => a.level === "info");
            return `<div class="attention-list">${main.map(item).join("")}</div>${info.length ? `<details class="quiet-flags"><summary>${info.length} informational note(s)</summary><div class="attention-list">${info.map(item).join("")}</div></details>` : ""}`;
          })() : '<div class="queue-empty">Nothing flagged for this window.</div>'}
        </article>
        <article class="panel evidence-panel">
          <div class="panel-heading"><div><p class="eyebrow">3 · CTCAE</p><h2>Toxicity grades with reasoning</h2></div><button class="text-button" data-action="add-toxicity" data-cycle="${cycle.id}">+ Record toxicity</button></div>
          ${ctcaeListHtml(cycle.ctcae, cycle)}
          ${(() => { const pr = (S.L.patient_reports || []).filter(r => r.cycle_id === cycle.id && r.status === "new");
            return pr.length ? `<p class="eyebrow top-gap-sm">Patient-reported in this window · to review</p><div class="report-list">${pr.map(r => reportCardHtml(r, { actions: true })).join("")}</div>` : ""; })()}
        </article>
        <article class="panel evidence-panel">
          <div class="panel-heading"><div><p class="eyebrow">4 · Pre-cycle labs</p><h2>Latest verified values vs baseline</h2></div><div class="heading-actions"><button class="text-button" data-action="upload-labs" data-cycle="${cycle.id}">Upload</button><button class="text-button" data-action="manual-labs" data-cycle="${cycle.id}">+ Enter</button></div></div>
          ${missingLabs ? `<div class="missing-marker"><strong>No pre-cycle labs for cycle ${cycle.cycle_number}.</strong> Labs are optional; this cycle will stay marked until results are added.</div>` : ""}
          ${cycle.lab_counts.unverified ? `<div class="missing-marker warn"><strong>${cycle.lab_counts.unverified} extracted value(s) await verification</strong> and are not included below. <a href="#plan/${S.L.plan.id}/labs">Verify now →</a></div>` : ""}
          ${cycle.labs.length ? `<div class="table-wrap"><table class="review-lab-table"><thead><tr><th>Test</th><th>Current</th><th>Reference</th><th>Baseline</th><th>Previous</th><th>vs baseline</th><th>Trend</th><th>CTCAE</th><th>Flags</th></tr></thead><tbody>${labRows}</tbody></table></div>` : ""}
        </article>
      </div>
      <aside class="decision-panel"><div class="decision-sticky">
        <p class="eyebrow">Clinician action</p>
        <h2>Record cycle ${cycle.cycle_number}</h2>
        <p class="decision-help">The system organises evidence only. It does not recommend proceeding, modifying, delaying or stopping.</p>
        <button class="button primary full" data-record-cycle="${cycle.id}">${["planned", "delayed"].includes(cycle.status) ? "Record outcome" : "Amend record"}</button>
        ${cycle.decisions.length ? `<div class="decision-history"><p class="eyebrow">Decision history</p>${cycle.decisions.map(d => `<div class="decision-entry"><strong>${esc(OUTCOME_LABEL[d.outcome] || d.outcome)}</strong><small>${esc(d.decided_by)} · ${fmtDate(d.decided_at)}</small>${d.rationale ? `<p>${esc(d.reason_category ? d.reason_category + ": " : "")}${esc(d.rationale)}</p>` : ""}</div>`).join("")}</div>` : '<p class="decision-help">No decision recorded yet.</p>'}
      </div></aside>
    </div>`;
}
function ctcaeListHtml(items, cycle = null) {
  if (!items.length) {
    if (cycle && cycle.labs_status === "missing" && !cycle.symptoms.length) return '<div class="queue-empty">Not assessed yet — no labs or symptoms recorded for this window.</div>';
    return '<div class="queue-empty">No CTCAE grades in this window (no graded symptoms and no verified lab value meeting a CTCAE threshold).</div>';
  }
  return `<div class="ctcae-list">${items.map(i => `<details class="ctcae-item ${i.trend}">
    <summary>${gradeBadge(i.grade)}<span class="ctcae-term"><strong>${esc(i.term)}</strong><small>${i.source === "lab" ? "Lab-derived" : "Clinician-confirmed"}${i.status ? ` · ${esc(i.status)}` : ""}${i.evidence ? ` · ${pretty(i.evidence)}` : ""}</small></span>
      <span class="trend-label ${["worsening", "new"].includes(i.trend) && i.grade >= 2 ? "alert" : ""}">${esc(i.trend)}${i.previous_grade ? ` (was G${i.previous_grade})` : ""}</span>
      ${i.associated_drugs.length ? `<span class="chip">${esc(i.associated_drugs.join(", "))}</span>` : ""}</summary>
    <p class="reasoning">${pretty(i.reasoning)}</p>
    ${(i.label_evidence || []).map(e => `<blockquote class="label-quote"><span class="src-tag dailymed">DailyMed</span> <b>${esc(e.drug)}</b> · ${esc(e.section)}${e.boxed ? " (boxed warning)" : ""}: “${esc(e.text)}” ${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">label ↗</a>` : ""}</blockquote>`).join("")}
    <p class="muted">${esc(i.confirmation)} · Criteria: ${esc(i.criteria_source || "stored reference")} · Association: ${esc(i.association_source || "curated shortlist")}</p>
  </details>`).join("")}</div>`;
}

/* ------------------------------------------------------------------ labs */
function renderLabs() {
  const L = S.L;
  $("#labWindows").innerHTML = L.cycles.map(c => `<button class="lab-window ${c.labs_status}" data-action="manual-labs" data-cycle="${c.id}" title="Enter labs before cycle ${c.cycle_number}">
      <span class="lab-window-head"><strong>Before C${c.cycle_number}</strong><i class="lab-dot ${c.labs_status}"></i></span>
      <small>${c.cycle_number === 1 ? "Baseline · " : ""}${fmtShort(c.planned_date)} · ${STATUS_LABEL[c.status]}</small>
      <span class="lab-window-count">${c.labs_status === "missing" ? "No labs" : `${c.lab_counts.verified} verified${c.lab_counts.unverified ? ` · ${c.lab_counts.unverified} pending` : ""}`}</span>
      ${c.documents.map(d => `<a class="doc-link" href="/api/lab-documents/${d.id}/file" target="_blank" rel="noopener">📄 ${esc(d.filename)}</a>`).join("")}
    </button>`).join("");
  const pending = L.cycles.flatMap(c => c.pending_labs.map(l => ({ ...l, cycle: c })));
  $("#verificationCount").textContent = pending.length;
  $("#verificationPanel").classList.toggle("quiet", !pending.length);
  const pendingDocs = L.cycles.flatMap(c => c.documents.map(d => ({ ...d, cycle: c }))).filter(d => pending.some(l => l.document_id === d.id));
  $("#verificationQueue").innerHTML = pending.length ? `
    ${pendingDocs.map(d => `<div class="doc-note"><span class="method-badge ${d.extraction_method}">${d.extraction_method === "claude" ? "AI" : "Parser"}</span><div><strong>${esc(d.filename)}</strong> · before C${d.cycle.cycle_number} · recorded ${fmtDate(d.recording_date)}${d.report_date && d.report_date !== d.recording_date ? ` · <span class="value-abnormal">report says ${fmtDate(d.report_date)}</span>` : ""}<small class="block muted">${esc(d.extraction_note || "")}</small></div><a class="text-button" href="/api/lab-documents/${d.id}/file" target="_blank" rel="noopener">Open original</a></div>`).join("")}
    <div class="table-wrap"><table class="verify-table"><thead><tr><th>Window</th><th>Test</th><th>Value</th><th>Unit</th><th>Ref low</th><th>Ref high</th><th>Collected</th><th>Source</th><th></th></tr></thead><tbody>
    ${pending.map(l => `<tr data-lab-row="${l.id}">
      <td>C${l.cycle.cycle_number}</td>
      <td><select data-f="test_name">${S.reference.lab_catalog.map(t => `<option ${t.test_name === l.test_name ? "selected" : ""}>${esc(t.test_name)}</option>`).join("")}</select></td>
      <td><input type="number" step="any" data-f="value" value="${l.value}" /></td>
      <td><input data-f="unit" value="${esc(l.unit)}" /></td>
      <td><input type="number" step="any" data-f="ref_low" value="${l.ref_low ?? ""}" /></td>
      <td><input type="number" step="any" data-f="ref_high" value="${l.ref_high ?? ""}" /></td>
      <td><input type="date" data-f="collection_date" value="${l.collection_date}" max="${todayISO()}" /></td>
      <td><small class="muted" title="${esc(l.source_document || "")}">${l.source === "upload:claude" ? "AI" : esc(l.source.replace("upload:", ""))} · ${pretty((l.source_document || "").slice(0, 60))}</small></td>
      <td class="queue-actions"><button class="mini-button" data-reject-lab="${l.id}">Reject</button><button class="mini-button confirm" data-verify-lab="${l.id}">Verify</button></td>
    </tr>`).join("")}</tbody></table></div>
    <div class="modal-actions"><button class="button secondary small" data-action="verify-all">Verify all ${pending.length} as shown</button></div>` : '<div class="queue-empty">No extracted values are waiting for review.</div>';
  renderTrendTable();
  renderLabFlags();
  $("#labCharts").innerHTML = L.lab_trends.map(t => `<article class="panel chart-card ${t.flags?.some(f => f.severity === "alert") ? "alert" : ""}">
      <div class="chart-head"><div><strong>${esc(t.test_name)}</strong><small>${fmtUnit(t.unit)} · ref ${fmtNum(t.reference_range.low)}–${fmtNum(t.reference_range.high)}</small></div>
      <span class="trend-chip ${t.overall_direction} ${isBad(t) ? "bad" : "good"}">${trendArrow(t.overall_direction)} ${t.latest.pct_from_baseline > 0 ? "+" : ""}${fmtNum(t.latest.pct_from_baseline)}%</span></div>
      ${lineChart(t)}
      ${t.flags?.length ? `<div class="chart-flags">${t.flags.slice(0, 3).map(f => `<span class="flag-chip ${f.severity}" title="${esc(f.detail)}">${flagIcon(f.severity)} ${esc(f.title)}</span>`).join("")}</div>` : ""}
    </article>`).join("");
}
const PANEL_NAMES = { CBP: "Complete blood picture (CBP)", LFT: "Liver function (LFT)", RFT: "Renal function & electrolytes (RFT)" };
function flagIcon(sev) { return sev === "alert" ? "!" : sev === "warning" ? "▲" : "i"; }
function flagChips(flags) {
  if (!flags?.length) return '<span class="muted">—</span>';
  const counts = { alert: 0, warning: 0, info: 0 };
  flags.forEach(f => { counts[f.severity] += 1; });
  return Object.entries(counts).filter(([, n]) => n).map(([sev, n]) => `<span class="flag-chip ${sev}" title="${esc(flags.filter(f => f.severity === sev).map(f => f.title).join("; "))}">${flagIcon(sev)} ${n}</span>`).join("");
}
function renderTrendTable() {
  const L = S.L;
  if (!L.lab_trends.length) { $("#labTrendTable").innerHTML = '<div class="queue-empty">No verified lab values yet. Upload a report or enter labs before a cycle to start the trend.</div>'; return; }
  const cols = L.cycles;
  const panels = [...new Set(L.lab_trends.map(t => t.panel))];
  $("#labTrendTable").innerHTML = `<div class="table-wrap"><table class="trend-table"><thead><tr><th>Test</th>${cols.map(c => `<th class="num">C${c.cycle_number}${c.cycle_number === 1 ? "<small>baseline</small>" : ""}</th>`).join("")}<th>Change</th><th>CTCAE</th><th>Flags</th></tr></thead><tbody>
    ${panels.map(panel => `<tr class="panel-row"><td colspan="${cols.length + 4}">${esc(PANEL_NAMES[panel] || panel)}</td></tr>` + L.lab_trends.filter(t => t.panel === panel).map(t => `<tr class="${t.flags?.some(f => f.severity === "alert") ? "flag-row alert" : t.meaningful_change ? "flag-row" : ""}">
      <td><strong>${esc(t.test_name)}</strong><small class="block muted">${fmtUnit(t.unit)}</small></td>
      ${cols.map(c => {
        const p = t.points.find(x => x.cycle_number === c.cycle_number);
        if (!p) return `<td class="num empty-cell" title="${c.labs_status === "missing" ? "No pre-cycle labs" : "Not measured"}">${c.labs_status === "missing" ? '<i class="lab-dot missing"></i>' : "·"}</td>`;
        return `<td class="num ${p.abnormal ? "value-abnormal" : ""}" title="${esc(p.collection_date)}${p.ctcae_grade ? ` · CTCAE G${p.ctcae_grade}` : ""}">${fmtNum(p.value)}${p.ctcae_grade ? `<sup>G${p.ctcae_grade}</sup>` : ""}</td>`;
      }).join("")}
      <td><span class="trend-chip ${t.overall_direction} ${isBad(t) ? "bad" : "good"}">${trendArrow(t.overall_direction)} ${t.points.length > 1 ? `${t.latest.pct_from_baseline > 0 ? "+" : ""}${fmtNum(t.latest.pct_from_baseline)}%` : "—"}</span></td>
      <td>${t.latest.ctcae_grade ? gradeBadge(t.latest.ctcae_grade, "small") : '<span class="muted">—</span>'}</td>
      <td class="flag-cell">${flagChips(t.flags)}</td>
    </tr><tr class="reason-row"><td colspan="${cols.length + 4}"><details><summary>Reasoning${t.flags?.length ? ` · ${t.flags.length} flag${t.flags.length > 1 ? "s" : ""}` : ""}</summary><p class="reasoning">${pretty(t.reasoning)}</p>${(t.flags || []).map(f => `<p class="reasoning flag-line ${f.severity}"><b>${flagIcon(f.severity)} ${esc(f.title)}.</b> ${pretty(f.detail)}</p>`).join("")}</details></td></tr>`).join("")).join("")}
  </tbody></table></div>`;
}
function renderLabFlags() {
  const order = { alert: 0, warning: 1, info: 2 };
  const groups = S.L.lab_trends.filter(t => t.flags?.length).map(t => {
    const flags = [...t.flags].sort((a, b) => order[a.severity] - order[b.severity]);
    return { t, flags, top: flags[0].severity };
  }).sort((a, b) => order[a.top] - order[b.top] || b.flags.length - a.flags.length);
  const counts = { alert: 0, warning: 0, info: 0 };
  groups.forEach(g => { counts[g.top] += 1; });
  const row = g => `<details class="flag-item ${g.top}">
      <summary><span class="flag-icon">${flagIcon(g.top)}</span><div><strong>${esc(g.t.test_name)} — ${esc(g.flags[0].title)}</strong>
        <p>${g.flags.slice(1).map(f => esc(f.title)).join(" · ") || pretty(g.flags[0].detail)}</p></div>
        <span class="chip">${esc(g.t.panel)} · ${fmtNum(g.t.baseline.value)} → ${fmtNum(g.t.latest.value)} ${fmtUnit(g.t.unit)}</span></summary>
      <ul class="flag-details">${g.flags.map(f => `<li class="${f.severity}"><b>${flagIcon(f.severity)} ${esc(f.title)}.</b> ${pretty(f.detail)}</li>`).join("")}</ul>
    </details>`;
  const main = groups.filter(g => g.top !== "info"), quiet = groups.filter(g => g.top === "info");
  $("#labFlags").innerHTML = groups.length ? `<div class="flag-summary">${[["alert", "need review"], ["warning", "to check"], ["info", "informational"]].map(([k, label]) => `<span class="flag-chip ${k}">${flagIcon(k)} ${counts[k]} test${counts[k] === 1 ? "" : "s"} ${label}</span>`).join("")}<span class="muted">One row per test, latest verified window. Expand a row for every rule that fired.</span></div>
    <div class="flag-list">${main.map(row).join("") || '<div class="queue-empty">No alert or warning flags.</div>'}</div>
    ${quiet.length ? `<details class="quiet-flags"><summary>${quiet.length} test(s) with informational flags only</summary><div class="flag-list">${quiet.map(row).join("")}</div></details>` : ""}`
    : '<div class="queue-empty">No trend flags. Flags appear once verified values exist for at least one cycle window.</div>';
}
function openLabForm(cycleId) {
  if (!cyclesOf().length) { showToast("This plan has no cycles yet. Add a cycle before entering labs."); return; }
  const form = $("#labForm");
  form.reset();
  formError(form, "");
  fillCycleSelect(form.elements.cycle_id, cycleId);
  form.elements.collection_date.value = defaultCollectionDate(Number(form.elements.cycle_id.value));
  form.elements.collection_date.max = todayISO();
  $("#labEntryRows").innerHTML = S.reference.lab_catalog.map(t => `<tr data-test="${esc(t.test_name)}">
    <td><strong>${esc(t.test_name)}</strong><small class="block muted">${esc(t.panel)}</small></td>
    <td><input type="number" step="any" data-f="value" aria-label="${esc(t.test_name)} value" /></td>
    <td><input data-f="unit" value="${esc(t.unit)}" aria-label="${esc(t.test_name)} unit" /></td>
    <td><input type="number" step="any" data-f="ref_low" value="${t.ref_low}" aria-label="${esc(t.test_name)} reference low" /></td>
    <td><input type="number" step="any" data-f="ref_high" value="${t.ref_high}" aria-label="${esc(t.test_name)} reference high" /></td></tr>`).join("");
  openModal("labModal");
}
function defaultCollectionDate(cycleId) {
  const c = cycleById(cycleId);
  if (!c) return todayISO();
  const day = addDays(c.planned_date, -1);
  return day > todayISO() ? todayISO() : day;
}
function fillCycleSelect(select, cycleId) {
  const target = Number(cycleId) || S.L.next_cycle_id || cyclesOf().at(-1)?.id;
  select.innerHTML = cyclesOf().map(c => `<option value="${c.id}" ${c.id === target ? "selected" : ""}>Before cycle ${c.cycle_number} · ${fmtShort(c.planned_date)} (${STATUS_LABEL[c.status]})${c.cycle_number === 1 ? " · baseline" : ""}</option>`).join("");
}
async function submitLabs(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.elements.confirmed.checked) return formError(form, "Confirm that you checked the values against the source report.");
  const results = $$("#labEntryRows tr").map(row => {
    const get = f => $(`[data-f="${f}"]`, row).value.trim();
    return { test_name: row.dataset.test, value: get("value"), unit: get("unit"), ref_low: get("ref_low") === "" ? null : Number(get("ref_low")), ref_high: get("ref_high") === "" ? null : Number(get("ref_high")) };
  }).filter(r => r.value !== "").map(r => ({ ...r, value: Number(r.value) }));
  if (!results.length) return formError(form, "Enter at least one value.");
  await withBusy(form, async () => {
    await post(`/api/cycles/${form.elements.cycle_id.value}/labs`, { results, collection_date: form.elements.collection_date.value, verified_by: clinicianName(), source: "manual" });
    closeModals();
    await refreshPlan();
    showToast(`${results.length} verified value(s) saved. Trends and CTCAE grades updated.`);
  });
}
function renderAiStatus() {
  const ai = S.health?.ai_extraction;
  const badge = $("#aiBadge");
  badge.textContent = ai?.enabled ? `AI extraction: on` : "AI extraction: off";
  badge.classList.toggle("on", Boolean(ai?.enabled));
  badge.title = ai?.enabled ? `${ai.model} · key ${ai.key_hint || ""}` : "Click to add an Anthropic API key";
}
function openAiSettings() {
  const form = $("#aiForm");
  form.reset();
  formError(form, "");
  const ai = S.health?.ai_extraction;
  $("#aiModalStatus").textContent = ai?.enabled ? `On · ${ai.model} · key ${ai.key_hint}. Enter a new key only to replace it.` : "Off · text PDFs use the offline parser; photos and scans need AI.";
  if (ai?.model) form.elements.model.value = ai.model;
  openModal("aiModal");
}
async function submitAiSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const key = form.elements.api_key.value.trim();
  if (!key && !S.health?.ai_extraction?.enabled) return formError(form, "Enter your Anthropic API key.");
  await withBusy(form, async () => {
    const status = await post("/api/settings/ai", { api_key: key || null, model: form.elements.model.value });
    S.health = { ...(S.health || {}), ai_extraction: status };
    renderAiStatus();
    closeModals();
    showToast(`AI extraction is on (${status.model}). New uploads will be read by Claude.`);
  });
}
async function normaliseImage(file) {
  // Phone photos can be large or in formats the API rejects; re-encode to JPEG ≤ 2000 px (keeps it well under the 5 MB image limit).
  if (!file.type.startsWith("image/") || (file.size < 1_500_000 && ["image/jpeg", "image/png"].includes(file.type))) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 2000 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.88));
    return blob ? new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" }) : file;
  } catch {
    throw new Error("This image format could not be read in the browser. Export the photo as JPEG or PDF and try again.");
  }
}
function openUploadForm(cycleId) {
  if (!cyclesOf().length) { showToast("This plan has no cycles yet. Add a cycle (Cycles → + Add cycle) before uploading labs."); return; }
  const form = $("#uploadForm");
  form.reset();
  formError(form, "");
  fillCycleSelect(form.elements.cycle_id, cycleId);
  form.elements.recording_date.max = todayISO();
  form.elements.recording_date.value = defaultCollectionDate(Number(form.elements.cycle_id.value));
  form.dataset.dateTouched = "";
  $("#dropzoneLabel").textContent = "Choose a lab report";
  const ai = S.health?.ai_extraction;
  $("#aiStatusLine").textContent = ai?.enabled ? `AI extraction is on (${ai.model}) for PDFs, photos and scans.` : "AI extraction is off. Text PDFs still work offline; photos and scans need AI.";
  $("#aiNote").classList.toggle("off", !ai?.enabled);
  openModal("uploadModal");
}
async function submitUpload(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = $("#labFile").files[0];
  if (!file) return formError(form, "Choose a report file.");
  if (!form.elements.recording_date.value) return formError(form, "Select the date of recording.");
  if (form.elements.recording_date.value > todayISO()) return formError(form, "The date of recording cannot be in the future.");
  await withBusy(form, async () => {
    const original = file;
    const upload = await normaliseImage(original);
    if (upload.size > 10_000_000) throw new Error("The file is larger than 10 MB. Compress it or upload a PDF.");
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
      reader.onerror = () => reject(new Error("Could not read the file."));
      reader.readAsDataURL(upload);
    });
    const result = await post(`/api/cycles/${form.elements.cycle_id.value}/lab-documents`, {
      filename: upload.name, content_type: upload.type || "", data_base64: base64, recording_date: form.elements.recording_date.value,
      recording_date_confirmed: Boolean(form.dataset.dateTouched),
    });
    closeModals();
    await refreshPlan();
    navigate(`#plan/${S.L.plan.id}/labs`);
    const n = result.candidates.length;
    const how = result.document.extraction_method === "claude" ? "AI" : "text-layer parser";
    const aiFailed = /AI extraction failed/.test(result.document.extraction_note || "");
    const dated = `dated ${fmtDate(result.document.recording_date)}`;
    if (aiFailed) showToast(`AI extraction failed for ${original.name}; ${n ? `the offline parser read ${n} value(s), ${dated}.` : "no values could be read."} ${result.document.extraction_note}`);
    else if (n) showToast(`${n} value(s) read from ${original.name} by ${how}, ${dated}. Verify each before use.`);
    else if (!S.health?.ai_extraction?.enabled) { showToast(`${original.name} has no readable text layer (photo or scan). Turn on AI extraction to read it.`); openAiSettings(); }
    else showToast(`No lab values were found in ${original.name}. ${result.document.extraction_note || ""}`);
  });
}
function labRowValues(row) {
  const get = f => $(`[data-f="${f}"]`, row).value;
  return { test_name: get("test_name"), value: Number(get("value")), unit: get("unit").trim(), ref_low: get("ref_low") === "" ? null : Number(get("ref_low")), ref_high: get("ref_high") === "" ? null : Number(get("ref_high")), collection_date: get("collection_date"), verified_by: clinicianName() };
}
async function verifyLab(id, quiet = false) {
  const row = $(`[data-lab-row="${id}"]`);
  await post(`/api/labs/${id}/verify`, labRowValues(row));
  if (!quiet) { await refreshPlan(); showToast("Value verified and added to trends."); }
}

/* ------------------------------------------------------------------ toxicities */
function renderContextPanel() {
  const L = S.L, ctx = L.context || {};
  const status = S.health?.context?.ctcae;
  const labels = Object.entries(ctx.dailymed || {});
  const missing = ctx.dailymed_missing || [];
  $("#contextPanel").innerHTML = `<article class="panel context-panel">
    <div class="panel-heading"><div><p class="eyebrow">Context layer</p><h2>CTCAE v6.0 + DailyMed drug labels</h2></div>
      ${missing.length ? `<button class="button secondary small" data-action="refresh-dailymed">Fetch DailyMed labels (${missing.length})</button>` : `<button class="button secondary small" data-action="refresh-dailymed">Refresh labels</button>`}</div>
    <div class="context-grid">
      <div><p class="eyebrow">Grading reference</p><p><strong>CTCAE v${esc(status?.version || "6.0")}</strong> (NCI, published 22 Jul 2025). ${status ? `${status.terms_with_v6_text} of ${status.terms_total} stored terms carry v6.0 wording${status.imported_workbook ? ` from ${esc(status.imported_workbook.source_file)}` : ""}.` : ""}
        ${status && !status.imported_workbook ? `<small class="block muted">Others show v5.0 wording until the NCI v6.0 workbook is imported: <code>python3 -m backend.context import-ctcae CTCAE_v6.xlsx</code>. v6.0 baseline-aware rules for ALT/AST are applied.</small>` : ""}</p></div>
      <div><p class="eyebrow">Drug labels</p>${labels.map(([drug, l]) => `<p><span class="src-tag dailymed">DailyMed</span> <strong>${esc(drug)}</strong> — ${esc(l.title || "")} <small class="muted">(published ${esc(l.published_date || "?")}, ${l.terms.length} CTCAE terms matched)</small> <a href="${esc(l.url)}" target="_blank" rel="noopener">label ↗</a></p>`).join("")}
        ${missing.map(d => `<p><span class="src-tag curated">Curated</span> <strong>${esc(d)}</strong> — no DailyMed label cached yet; using the curated shortlist.</p>`).join("")}</div>
    </div>
  </article>`;
}
function renderToxicities() {
  const L = S.L;
  const cols = L.cycles;
  renderContextPanel();
  const reports = L.patient_reports || [];
  const open = reports.filter(r => r.status === "new");
  $("#patientReportsPanel").innerHTML = open.length ? `<article class="panel review-banner ${open.some(r => r.red_flags.length) ? "urgent-panel" : ""}">
    <div><p class="eyebrow">Patient portal</p><h2>${open.length} patient-reported symptom message${open.length === 1 ? "" : "s"} to review</h2>
      <p class="muted">Each message sits in its between-cycle assessment window below with its date. Review it, grade and add it as a CTCAE toxicity, or reply to the patient.</p></div>
    <div class="drug-chips">${open.map(r => `<a class="chip ${r.red_flags.length ? "chip-alert" : "chip-warn"}" href="#window-${r.cycle_id}" data-jump-window="${r.cycle_id}">${fmtShort(r.onset_date || r.created_at.slice(0, 10))} · ${esc(r.text.slice(0, 40))}${r.text.length > 40 ? "…" : ""}</a>`).join("")}</div>
  </article>` : "";
  $("#ctcaeMatrix").innerHTML = `<article class="panel">
    <div class="panel-heading"><div><p class="eyebrow">CTCAE v6.0 · worst grade per cycle window</p><h2>Toxicity grade matrix</h2></div><div class="legend-inline">${[1, 2, 3, 4].map(g => `<span>${gradeBadge(g, "small")}</span>`).join("")}<span><i class="src-tag lab">Lab</i> computed</span><span><i class="src-tag clinical">Clinician</i> confirmed</span></div></div>
    ${L.ctcae_matrix.length ? `<div class="table-wrap"><table class="matrix-table"><thead><tr><th>Term</th>${cols.map(c => `<th class="num">C${c.cycle_number}</th>`).join("")}<th>Known association</th></tr></thead><tbody>
      ${L.ctcae_matrix.map(r => `<tr><td><strong>${esc(r.term)}</strong> <i class="src-tag ${r.kind}">${r.kind === "lab" ? "Lab" : "Clinician"}</i></td>
        ${cols.map(c => { const g = r.grades[String(c.cycle_number)]; return `<td class="num heat heat-${g || 0}" title="${g ? `Grade ${g} before cycle ${c.cycle_number}` : c.labs_status === "missing" && r.kind === "lab" ? "No labs" : "Not graded"}">${g ? `G${g}` : r.kind === "lab" && c.labs_status === "missing" ? '<i class="lab-dot missing"></i>' : ""}</td>`; }).join("")}
        <td>${r.associated_drugs.length ? esc(r.associated_drugs.join(", ")) : `<span class="muted">${L.context?.dailymed && Object.keys(L.context.dailymed).length ? "Not on regimen labels" : "None stored"}</span>`}</td></tr>`).join("")}
    </tbody></table></div>` : '<div class="queue-empty">No CTCAE grades yet.</div>'}
  </article>`;
  const reportsByCycle = {};
  (L.patient_reports || []).forEach(r => { (reportsByCycle[r.cycle_id] ||= []).push(r); });
  const windows = L.cycles.filter(c => c.symptoms.length || reportsByCycle[c.id]?.length || c.id === L.next_cycle_id);
  $("#symptomList").innerHTML = windows.length ? windows.map(c => {
    const reports = reportsByCycle[c.id] || [];
    const open = reports.filter(r => r.status === "new");
    const items = [
      ...c.symptoms.map(sym => ({ date: sym.onset_date, kind: "symptom", sym })),
      ...reports.map(r => ({ date: (r.onset_date || r.created_at.slice(0, 10)), kind: "report", r })),
    ].sort((x, y) => x.date.localeCompare(y.date) || (x.kind === "report" ? -1 : 1));
    return `<section class="symptom-window ${open.length ? "has-open" : ""}" id="window-${c.id}">
      <header class="window-head"><div><p class="eyebrow">Before cycle ${c.cycle_number} · ${c.window.from ? `${fmtShort(c.window.from)} – ` : "up to "}${fmtShort(c.window.to)}${c.id === L.next_cycle_id ? " · current window" : ""}</p>
        <small class="muted">${c.symptoms.length} confirmed · ${reports.length} patient message${reports.length === 1 ? "" : "s"}${open.length ? ` · <b class="value-abnormal">${open.length} to review</b>` : ""}</small></div>
        <button class="button secondary small" data-action="add-toxicity" data-cycle="${c.id}">+ Add toxicity</button></header>
      ${items.length ? items.map(item => item.kind === "report" ? `<div class="window-item patient">
          <div class="window-date"><strong>${fmtShort(item.date)}</strong><small>${item.r.onset_date ? "onset" : "reported"}</small><i class="src-tag patient">Patient</i></div>
          ${reportCardHtml(item.r, { actions: true, inWindow: true })}</div>` : `<div class="window-item">
          <div class="window-date"><strong>${fmtShort(item.date)}</strong><small>onset</small><i class="src-tag clinical">Clinician</i></div>
          <div class="toxicity-card">
            <div><h3>${esc(item.sym.ctcae_term)}</h3><p>${esc(item.sym.note || "No notes")}</p></div>
            <div><small>Grade</small>${gradeBadge(item.sym.grade)}</div>
            <div><small>Onset</small><strong>${fmtShort(item.sym.onset_date)}${item.sym.resolution_date ? ` → ${fmtShort(item.sym.resolution_date)}` : ""}</strong></div>
            <div><small>Status</small><strong>${esc(item.sym.status)}</strong></div>
            <div><small>Suspected</small><strong>${esc(item.sym.suspected_drug || "—")}</strong></div>
            <div><button class="icon-action" data-edit-symptom="${item.sym.id}" title="Edit" aria-label="Edit ${esc(item.sym.ctcae_term)}">✎</button></div>
          </div></div>`).join("") : '<div class="queue-empty">Nothing recorded in this window yet.</div>'}
    </section>`;
  }).join("") : '<div class="queue-empty">No symptoms recorded for this plan.</div>';
}
function openToxicityForm(cycleId = null, symptom = null, prefill = null) {
  const form = $("#toxicityForm");
  form.reset();
  formError(form, "");
  S.editingSymptomId = symptom?.id || null;
  S.fromReportId = prefill?.reportId || null;
  $("#toxicityModalTitle").textContent = symptom ? "Edit CTCAE toxicity" : "Record CTCAE toxicity";
  $('[data-action="delete-symptom"]').classList.toggle("hidden", !symptom);
  fillCycleSelect(form.elements.cycle_id, symptom?.cycle_id || cycleId);
  form.dataset.windowTouched = cycleId || symptom ? "1" : "";
  form.elements.onset_date.max = todayISO();
  form.elements.resolution_date.max = todayISO();
  form.elements.onset_date.value = symptom?.onset_date || todayISO();
  form.elements.confirmed_by.value = clinicianName();
  const drugs = regimenDrugs();
  $("#suspectedDrug").innerHTML = `<option value="">Not attributed</option>${drugs.map(d => `<option>${esc(d)}</option>`).join("")}`;
  fillTermSelect(symptom?.ctcae_term);
  if (symptom) {
    form.elements.status.value = symptom.status;
    form.elements.resolution_date.value = symptom.resolution_date || "";
    form.elements.suspected_drug.value = symptom.suspected_drug || "";
    form.elements.note.value = symptom.note || "";
    form.elements.confirmed_by.value = symptom.confirmed_by;
  }
  $("#termSuggestions").innerHTML = "";
  if (prefill) {
    if (prefill.term) $("#toxicityTerm").value = prefill.term;
    form.elements.onset_date.value = prefill.onset || todayISO();
    form.elements.note.value = prefill.note || "";
    form.elements.description.value = prefill.text?.slice(0, 200) || "";
    if (!prefill.term) suggestTerms();
  }
  updateCriteria(symptom?.grade);
  if (prefill?.grade) $(`#gradeOptions input[value="${prefill.grade}"]`)?.closest("label")?.classList.add("suggested");
  $("#toxicityModalTitle").textContent = prefill ? "Record patient-reported toxicity" : $("#toxicityModalTitle").textContent;
  openModal("toxicityModal");
}
function drugTerms(drug) {
  const label = S.L?.context?.dailymed?.[drug];
  if (label) return { source: "DailyMed", terms: label.terms };
  const curated = S.reference.drugs.find(d => d.drug_name.toLowerCase() === drug.toLowerCase() || drug.toLowerCase().startsWith(d.drug_name.toLowerCase().slice(0, 5)));
  return { source: "curated shortlist", terms: curated ? curated.ctcae_terms : [] };
}
function fillTermSelect(selected) {
  const drugs = regimenDrugs();
  const clinical = S.reference.ctcae_terms.filter(t => t.kind === "clinical");
  const assocSet = new Set(drugs.flatMap(d => drugTerms(d).terms));
  const assoc = clinical.filter(t => assocSet.has(t.term));
  const rest = clinical.filter(t => !assocSet.has(t.term));
  const opt = t => `<option value="${esc(t.term)}" ${t.term === selected ? "selected" : ""}>${esc(t.term)}</option>`;
  $("#toxicityTerm").innerHTML = (assoc.length ? `<optgroup label="Associated with ${esc(drugs.join(" / "))}">${assoc.map(opt).join("")}</optgroup>` : "") + `<optgroup label="All supported CTCAE terms">${rest.map(opt).join("")}</optgroup>`;
}
function updateCriteria(selectedGrade) {
  const term = $("#toxicityTerm").value;
  const info = termInfo(term);
  if (!info) return;
  const drugs = regimenDrugs().filter(d => drugTerms(d).terms.includes(term));
  const sources = [...new Set(drugs.map(d => drugTerms(d).source))].join(" + ");
  const prov = S.reference.ctcae_provenance?.[term] || "stored reference";
  $("#criteriaAssoc").textContent = `${prov}${drugs.length ? ` · listed for ${drugs.join(", ")} (${sources})` : ""}`;
  $("#criteriaList").innerHTML = Object.entries(info.grades).map(([g, text]) => `<div class="criteria-item"><strong>Grade ${g}</strong><span>${esc(text)}</span></div>`).join("");
  $("#gradeOptions").innerHTML = Object.keys(info.grades).map(g => `<label><input type="radio" name="grade" value="${g}" ${Number(g) === Number(selectedGrade) ? "checked" : ""} /><span>Grade ${g}</span></label>`).join("");
  if (drugs.length === 1 && !$("#suspectedDrug").value && !S.editingSymptomId) $("#suspectedDrug").value = drugs[0];
}
let suggestTimer;
function suggestTerms() {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(async () => {
    const text = $("#symptomDescription").value.trim();
    if (text.length < 3) { $("#termSuggestions").innerHTML = ""; return; }
    try {
      const data = await api(`/api/ctcae/suggest?text=${encodeURIComponent(text)}&drugs=${encodeURIComponent(regimenDrugs().join(","))}`);
      $("#termSuggestions").innerHTML = data.matches.length ? `<span class="muted">Possible terms:</span>${data.matches.slice(0, 5).map(m => `<button type="button" class="chip chip-button" data-pick-term="${esc(m.term)}">${esc(m.term)}${m.associated_drugs.length ? ` · ${esc(m.associated_drugs.join(", "))}` : ""}</button>`).join("")}` : '<span class="muted">No matching term — pick from the list.</span>';
    } catch { /* search aid only */ }
  }, 250);
}
async function submitToxicity(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const v = Object.fromEntries(new FormData(form));
  if (!v.grade) return formError(form, "Select the CTCAE grade.");
  if (!form.elements.confirmed.checked) return formError(form, "Confirm that you reviewed the CTCAE criteria.");
  if (v.status === "resolved" && !v.resolution_date) return formError(form, "Add the resolution date for a resolved toxicity.");
  const body = { cycle_id: Number(v.cycle_id), ctcae_term: v.ctcae_term, grade: Number(v.grade), onset_date: v.onset_date, status: v.status,
    resolution_date: v.resolution_date || null, suspected_drug: v.suspected_drug || null, note: v.note.trim() || null, confirmed_by: v.confirmed_by.trim() || clinicianName() };
  await withBusy(form, async () => {
    if (S.editingSymptomId) await patch(`/api/symptoms/${S.editingSymptomId}`, body);
    else {
      const created = await post(`/api/patients/${S.L.patient.id}/symptoms`, body);
      if (S.fromReportId) {
        // a multi-symptom check-in stays "awaiting review" until every graded symptom has been recorded
        const r = findReport(S.fromReportId);
        const recordedTerms = new Set([...(r?.recorded || []).map(x => x.ctcae_term), created.ctcae_term]);
        const pending = (r?.checkin?.items || []).filter(i => i.term && !recordedTerms.has(i.term));
        await patch(`/api/patient-reports/${S.fromReportId}`, { status: pending.length ? (r?.status || "new") : "recorded", symptom_id: created.id });
      }
    }
    S.fromReportId = null;
    refreshInboxBadge();
    closeModals();
    await refreshPlan();
    showToast(S.editingSymptomId ? "Toxicity updated." : "Clinician-confirmed toxicity saved.");
  });
}

/* ------------------------------------------------------------------ longitudinal */
function renderTimeline() {
  const L = S.L;
  $("#timelineSummary").innerHTML = `<article class="panel"><div class="panel-heading"><div><p class="eyebrow">Across the plan</p><h2>Lab trends from baseline</h2></div><a class="text-button" href="#plan/${L.plan.id}/labs">Full lab analysis →</a></div>
    ${L.lab_trends.length ? `<div class="trend-strip">${L.lab_trends.map(t => `<div class="trend-mini ${t.meaningful_change ? "flag" : ""}"><div><strong>${esc(t.test_name)}</strong><small>${fmtNum(t.baseline.value)} → ${fmtNum(t.latest.value)} ${fmtUnit(t.unit)}</small></div>${sparkline(t)}<span class="trend-chip ${t.overall_direction} ${isBad(t) ? "bad" : "good"}">${trendArrow(t.overall_direction)} ${t.points.length > 1 ? `${t.latest.pct_from_baseline > 0 ? "+" : ""}${fmtNum(t.latest.pct_from_baseline)}%` : "—"}</span></div>`).join("")}</div>` : '<div class="queue-empty">No verified labs yet.</div>'}
  </article>`;
  $("#timeline").innerHTML = L.cycles.map(c => {
    const pending = ["planned", "delayed"].includes(c.status);
    const abnormal = c.labs.filter(r => r.abnormal);
    const decision = c.decisions[0];
    return `<article class="timeline-item ${pending ? "planned-item" : ""} status-${c.status}"><div class="timeline-card">
      <div class="timeline-head"><div><h2>Cycle ${c.cycle_number}</h2><p>${c.administered_date ? `Given ${fmtDate(c.administered_date)}` : `Planned ${fmtDate(c.planned_date)}`}${c.window.from ? ` · window ${fmtShort(c.window.from)}–${fmtShort(c.window.to)}` : " · baseline window"}</p></div><div class="timeline-head-right">${labDot(c.labs_status)}${statusPill(c.status)}</div></div>
      <div class="timeline-grid">
        <section><p class="eyebrow">Drugs &amp; doses</p><div class="drug-chips">${medsSummary(c)}</div>${c.exposure_before.length ? `<small class="muted block top-gap-sm">Cumulative before: ${c.exposure_before.map(e => `${esc(e.drug_name)} ${fmtNum(e.cumulative_dose)} ${fmtUnit(e.dose_unit)}`).join(", ")}</small>` : ""}</section>
        <section><p class="eyebrow">Pre-cycle labs</p>${c.labs_status === "missing" ? `<div class="missing-marker compact">No pre-cycle labs${pending ? " yet" : ""}. <button class="text-button" data-action="manual-labs" data-cycle="${c.id}">Add</button></div>` :
          `<div class="lab-chips">${abnormal.length ? abnormal.map(r => `<span class="chip chip-alert">${esc(r.test_name)} ${fmtNum(r.value)} ${fmtUnit(r.unit)} ${trendArrow(r.direction)}</span>`).join("") : ""}<span class="chip">${c.labs.length - abnormal.length} in range</span>${c.lab_counts.unverified ? `<span class="chip chip-warn">${c.lab_counts.unverified} unverified</span>` : ""}</div>`}</section>
        <section class="span-all"><p class="eyebrow">CTCAE ${c.worst_grade ? `· worst ${gradeBadge(c.worst_grade, "small")}` : ""}</p>${ctcaeListHtml(c.ctcae.filter(i => i.grade), c)}</section>
        <section class="span-all decision-line"><p class="eyebrow">Clinician decision</p>${decision ? `<p><strong>${esc(OUTCOME_LABEL[decision.outcome] || decision.outcome)}</strong>${decision.reason_category ? ` · ${esc(decision.reason_category)}` : ""} — ${esc(decision.rationale || "No rationale required")} <small class="muted">(${esc(decision.decided_by)}, ${fmtDate(decision.decided_at)}${c.decisions.length > 1 ? `, ${c.decisions.length - 1} earlier revision(s)` : ""})</small></p>` : `<p class="muted">Pending. <button class="text-button" data-record-cycle="${c.id}">Record outcome</button></p>`}</section>
      </div>
    </div></article>`;
  }).join("");
}

/* ------------------------------------------------------------------ charts (inline SVG) */
function sparkline(t, uptoCycle = null) {
  const pts = t.points.filter(p => uptoCycle === null || p.cycle_number <= uptoCycle);
  if (pts.length < 2) return '<span class="muted small">—</span>';
  const w = 84, h = 26, pad = 3;
  const values = pts.map(p => p.value);
  const lo = Math.min(...values, t.reference_range.low ?? Infinity), hi = Math.max(...values, t.reference_range.high ?? -Infinity);
  const span = hi - lo || 1;
  const x = i => pad + (i * (w - pad * 2)) / (pts.length - 1);
  const y = v => h - pad - ((v - lo) / span) * (h - pad * 2);
  const band = t.reference_range.low !== null && t.reference_range.high !== null ? `<rect x="0" y="${y(t.reference_range.high)}" width="${w}" height="${Math.max(1, y(t.reference_range.low) - y(t.reference_range.high))}" class="ref-band" />` : "";
  const last = pts.at(-1);
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${esc(t.test_name)} trend ${pts.map(p => fmtNum(p.value)).join(", ")}">${band}<polyline points="${pts.map((p, i) => `${x(i)},${y(p.value)}`).join(" ")}" /><circle cx="${x(pts.length - 1)}" cy="${y(last.value)}" r="2.6" class="${last.abnormal ? "abn" : ""}" /></svg>`;
}
function lineChart(t) {
  const cycles = S.L.cycles;
  const w = 320, h = 150, l = 40, r = 12, top = 12, b = 26;
  const values = t.points.map(p => p.value);
  const lo0 = Math.min(...values, t.reference_range.low ?? Infinity), hi0 = Math.max(...values, t.reference_range.high ?? -Infinity);
  const padV = (hi0 - lo0 || Math.abs(hi0) || 1) * 0.12;
  const lo = Math.max(0, lo0 - padV), hi = hi0 + padV;
  const n = cycles.length;
  const x = num => l + ((num - 1) * (w - l - r)) / Math.max(1, n - 1);
  const y = v => top + (1 - (v - lo) / (hi - lo || 1)) * (h - top - b);
  const ticks = [lo, (lo + hi) / 2, hi];
  const band = t.reference_range.low !== null && t.reference_range.high !== null
    ? `<rect x="${l}" y="${y(Math.min(hi, t.reference_range.high))}" width="${w - l - r}" height="${Math.max(0, y(Math.max(lo, t.reference_range.low)) - y(Math.min(hi, t.reference_range.high)))}" class="ref-band" />` : "";
  const path = t.points.map((p, i) => `${i ? "L" : "M"}${x(p.cycle_number)},${y(p.value)}`).join(" ");
  const missing = cycles.filter(c => c.labs_status === "missing").map(c => `<text x="${x(c.cycle_number)}" y="${h - b + 2}" class="missing-tick" text-anchor="middle">◌</text>`).join("");
  return `<svg class="line-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(t.test_name)} by cycle: ${t.points.map(p => `C${p.cycle_number} ${fmtNum(p.value)}`).join(", ")}">
    ${ticks.map(v => `<line x1="${l}" x2="${w - r}" y1="${y(v)}" y2="${y(v)}" class="grid" /><text x="${l - 6}" y="${y(v) + 3}" text-anchor="end" class="axis">${fmtAxis(v)}</text>`).join("")}
    ${band}
    ${cycles.map(c => `<text x="${x(c.cycle_number)}" y="${h - 6}" text-anchor="middle" class="axis">C${c.cycle_number}</text>`).join("")}
    ${missing}
    <path d="${path}" class="series" />
    ${t.points.map(p => `<circle cx="${x(p.cycle_number)}" cy="${y(p.value)}" r="4" class="${p.abnormal ? "abn" : ""}" data-tip="C${p.cycle_number} · ${fmtShort(p.collection_date)}|${fmtNum(p.value)} ${t.unit}${p.pct_from_baseline ? ` (${p.pct_from_baseline > 0 ? "+" : ""}${fmtNum(p.pct_from_baseline)}% vs baseline)` : " (baseline)"}${p.ctcae_grade ? `|CTCAE G${p.ctcae_grade}` : ""}" /><circle cx="${x(p.cycle_number)}" cy="${y(p.value)}" r="12" class="hit" data-tip="C${p.cycle_number} · ${fmtShort(p.collection_date)}|${fmtNum(p.value)} ${t.unit}${p.pct_from_baseline ? ` (${p.pct_from_baseline > 0 ? "+" : ""}${fmtNum(p.pct_from_baseline)}% vs baseline)` : " (baseline)"}${p.ctcae_grade ? `|CTCAE G${p.ctcae_grade}` : ""}" />`).join("")}
  </svg>`;
}
function moveTooltip(event) {
  const tip = $("#chartTooltip");
  const target = event.target.closest("[data-tip]");
  if (!target) { tip.hidden = true; return; }
  const [head, ...rest] = target.dataset.tip.split("|");
  tip.innerHTML = `<strong>${esc(head)}</strong>${rest.map(r => `<span>${fmtUnit(r)}</span>`).join("")}`;
  tip.hidden = false;
  const x = Math.min(window.innerWidth - tip.offsetWidth - 12, event.clientX + 14);
  tip.style.left = `${x}px`;
  tip.style.top = `${event.clientY - tip.offsetHeight - 10}px`;
}

/* ------------------------------------------------------------------ events */
function bindDraftInputs() {
  document.addEventListener("input", event => {
    const el = event.target;
    const f = el.dataset.f;
    const planRow = el.closest("[data-plan-med]");
    if (planRow && f) S.planDraft.meds[Number(planRow.dataset.planMed)][f] = el.value;
    const prevRow = el.closest("[data-preview-med]");
    if (prevRow && f) { const [ci, mi] = prevRow.dataset.previewMed.split(":").map(Number); S.planDraft.cycles[ci].meds[mi][f] = el.value; }
    if (el.dataset.cycleDate !== undefined) S.planDraft.cycles[Number(el.dataset.cycleDate)].planned_date = el.value;
    const cycRow = el.closest("[data-cycle-med]");
    if (cycRow && f) S.cycleDraft.meds[Number(cycRow.dataset.cycleMed)][f] = el.value;
    if (el.closest("#recordMedRows")) updateRecordForm();
    if (el.id === "symptomDescription") suggestTerms();
  });
  // Re-render preview "differs" marker when leaving a field.
  $("#planPreviewCycles").addEventListener("change", () => { const scroll = $("#planPreviewModal .modal-card").scrollTop; renderPlanPreview(); $("#planPreviewModal .modal-card").scrollTop = scroll; });
}

document.addEventListener("click", async event => {
  const t = event.target;
  const closest = sel => t.closest(sel);
  if (closest("[data-close-modal]")) return closeModals();
  const open = closest("[data-open-patient]");
  if (open) return navigate(`#patient/${open.dataset.openPatient}`);
  const editP = closest("[data-edit-patient]");
  if (editP) { const p = S.patients.find(x => x.id === Number(editP.dataset.editPatient)); return openPatientForm(p); }
  const delP = closest("[data-delete-patient]");
  if (delP) return deletePatient(Number(delP.dataset.deletePatient));
  const delPlan = closest("[data-delete-plan]");
  if (delPlan) return deletePlan(Number(delPlan.dataset.deletePlan));
  const rec = closest("[data-record-cycle]");
  if (rec) return openRecordForm(Number(rec.dataset.recordCycle));
  const rev = closest("[data-review-cycle]");
  if (rev) { event.preventDefault(); S.reviewCycleId = Number(rev.dataset.reviewCycle); renderReview(); return navigate(`#plan/${S.L.plan.id}/review`); }
  const editC = closest("[data-edit-cycle]");
  if (editC) return openCycleForm(Number(editC.dataset.editCycle));
  const editS = closest("[data-edit-symptom]");
  if (editS) { const s = cyclesOf().flatMap(c => c.symptoms).find(x => x.id === Number(editS.dataset.editSymptom)); return openToxicityForm(null, s); }
  const pick = closest("[data-pick-term]");
  if (pick) { $("#toxicityTerm").value = pick.dataset.pickTerm; return updateCriteria(); }
  const verify = closest("[data-verify-lab]");
  if (verify) { verify.disabled = true; try { await verifyLab(Number(verify.dataset.verifyLab)); } catch (e) { showToast(e.message); verify.disabled = false; } return; }
  const reject = closest("[data-reject-lab]");
  if (reject) { try { await patch(`/api/labs/${reject.dataset.rejectLab}`, { verification_status: "rejected" }); await refreshPlan(); showToast("Extracted value rejected. The source document is retained."); } catch (e) { showToast(e.message); } return; }
  const removeMed = closest("[data-remove-med]");
  if (removeMed) {
    const planRow = removeMed.closest("[data-plan-med]"), prevRow = removeMed.closest("[data-preview-med]"), cycRow = removeMed.closest("[data-cycle-med]");
    if (planRow) { S.planDraft.meds.splice(Number(planRow.dataset.planMed), 1); renderPlanMeds(); }
    if (prevRow) { const [ci, mi] = prevRow.dataset.previewMed.split(":").map(Number); S.planDraft.cycles[ci].meds.splice(mi, 1); renderPlanPreview(); }
    if (cycRow) { S.cycleDraft.meds.splice(Number(cycRow.dataset.cycleMed), 1); renderCycleMeds(); }
    return;
  }
  const addPrev = closest("[data-add-preview-med]");
  if (addPrev) { S.planDraft.cycles[Number(addPrev.dataset.addPreviewMed)].meds.push(newMed()); return renderPlanPreview(); }

  const jump = closest("[data-jump-window]");
  if (jump) { event.preventDefault(); document.getElementById(`window-${jump.dataset.jumpWindow}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  const recordBtn = closest("[data-report-record]");
  if (recordBtn) {
    const r = findReport(recordBtn.dataset.reportRecord);
    if (!r || !S.L) return;
    const item = recordBtn.dataset.item ? r.checkin?.items.find(i => i.key === recordBtn.dataset.item) : null;
    const note = item
      ? `Patient check-in via portal (${fmtDate(r.created_at)}): ${item.label}${item.onset ? ` since ${fmtDate(item.onset)}` : ""}. ${item.answers.map(a => `${a.question} ${a.answer}`).join("; ")}.${item.grade_suggestion ? ` Rule-based suggestion G${item.grade_suggestion}: ${item.rationale}.` : ""}${r.text && !r.text.startsWith("Check-in:") ? ` Patient's words: "${r.text.slice(0, 300)}"` : ""}`
      : `Patient-reported via portal (${fmtDate(r.created_at)}, ${r.input_mode}): "${r.text.slice(0, 600)}"${r.impact_text ? ` · ${r.impact_text}` : ""}${r.temperature ? ` · ${r.temperature} °C` : ""}`;
    return openToxicityForm(r.cycle_id, null, { reportId: r.id, term: recordBtn.dataset.term || null, grade: recordBtn.dataset.grade || null,
      onset: item?.onset || r.onset_date, note: note.slice(0, 990), text: item ? `${item.label}: ${item.answers.map(a => a.answer).join(", ")}` : r.text });
  }
  const replyBtn = closest("[data-report-reply]");
  if (replyBtn) { replyBtn.closest(".report-card")?.querySelector("[data-reply-form]")?.classList.toggle("hidden"); return; }
  const retriageBtn = closest("[data-report-retriage]");
  if (retriageBtn) {
    retriageBtn.disabled = true; retriageBtn.textContent = "Analysing…";
    try {
      const r = await post(`/api/patient-reports/${retriageBtn.dataset.reportRetriage}/triage`, {});
      showToast(r.ai?.method === "claude" ? "AI analysis updated." : (r.ai?.error || "Keyword analysis updated."));
      await afterReportChange();
    } catch (e) { showToast(e.message); retriageBtn.disabled = false; retriageBtn.textContent = "Analyse with AI"; }
    return;
  }
  const dismissBtn = closest("[data-report-dismiss]");
  if (dismissBtn) {
    try { await patch(`/api/patient-reports/${dismissBtn.dataset.reportDismiss}`, { status: "dismissed" }); showToast("Message dismissed."); await afterReportChange(); } catch (e) { showToast(e.message); }
    return;
  }
  const action = closest("[data-action]");
  if (!action) return;
  const cycleId = action.dataset.cycle ? Number(action.dataset.cycle) : null;
  switch (action.dataset.action) {
    case "new-patient": return openPatientForm();
    case "retry-patients": return loadPatients();
    case "edit-patient": return openPatientForm(S.patient);
    case "delete-patient": return deletePatient(S.patient.id);
    case "new-plan": return openPlanForm(false);
    case "edit-plan": return openPlanForm(true);
    case "add-plan-med": S.planDraft.meds.push(newMed()); return renderPlanMeds();
    case "plan-back": closeModals(); openModal("planModal"); return renderPlanMeds();
    case "add-cycle": return openCycleForm(null);
    case "add-cycle-med": S.cycleDraft.meds.push(newMed()); return renderCycleMeds();
    case "delete-cycle": {
      const c = cycleById(S.cycleDraft.id);
      closeModals();
      return confirmDialog(`Delete cycle ${c.cycle_number}?`, "Its planned doses, pre-cycle labs and decisions will be removed.", async () => { await del(`/api/cycles/${c.id}`); await refreshPlan(); showToast("Cycle deleted."); });
    }
    case "manual-labs": event.preventDefault(); return openLabForm(cycleId);
    case "upload-labs": return openUploadForm(cycleId);
    case "add-toxicity": return openToxicityForm(cycleId);
    case "delete-symptom": {
      const id = S.editingSymptomId;
      closeModals();
      return confirmDialog("Delete this toxicity record?", "The clinician-confirmed grade will be removed from the CTCAE history.", async () => { await del(`/api/symptoms/${id}`); await refreshPlan(); showToast("Toxicity record deleted."); });
    }
    case "ai-settings": return openAiSettings();
    case "portal-enable": {
      try { await post(`/api/patients/${S.patient.id}/portal`, {}); await loadPatient(S.patient.id); showToast(S.patient.phone ? "Portal access turned on." : "Portal access turned on. Add a mobile number so the patient can sign in."); } catch (e) { showToast(e.message); }
      return;
    }
    case "portal-disable": return confirmDialog("Turn off portal access?", "The patient will be signed out and cannot view their record or send messages until access is turned back on.", async () => { await del(`/api/patients/${S.patient.id}/portal`); await loadPatient(S.patient.id); showToast("Portal access turned off."); }, "Turn off");
    case "sign-out": {
      try { await post("/api/auth/logout", {}); } catch { /* ignore */ }
      showDoctorLogin();
      return;
    }
    case "refresh-dailymed": {
      action.disabled = true;
      action.textContent = "Fetching from DailyMed…";
      try {
        const res = await post("/api/context/dailymed/refresh", { drugs: regimenDrugs() });
        const ok = res.filter(r => r.ok), bad = res.filter(r => !r.ok);
        S.health = await fetch("/health").then(r => r.json()).catch(() => S.health);
        await refreshPlan();
        showToast(`${ok.length ? `DailyMed labels loaded: ${ok.map(r => r.drug).join(", ")}.` : ""} ${bad.length ? `Not available: ${bad.map(r => `${r.drug} (${r.error})`).join("; ")}` : ""}`.trim());
      } catch (e) { showToast(e.message); action.disabled = false; }
      return;
    }
    case "verify-all": {
      action.disabled = true;
      try { for (const row of $$("[data-lab-row]")) await verifyLab(Number(row.dataset.labRow), true); await refreshPlan(); showToast("All extracted values verified."); }
      catch (e) { showToast(e.message); await refreshPlan(); }
      return;
    }
  }
});

document.addEventListener("submit", async event => {
  const form = event.target.closest("[data-reply-form]");
  if (!form) return;
  event.preventDefault();
  const v = Object.fromEntries(new FormData(form));
  try {
    await patch(`/api/patient-reports/${form.dataset.replyForm}`, { status: findReport(form.dataset.replyForm)?.status === "recorded" ? "recorded" : "reviewed", patient_message: v.patient_message, doctor_note: v.doctor_note });
    showToast(v.patient_message.trim() ? "Reviewed. The reply is now visible in the patient's portal." : "Marked as reviewed.");
    await afterReportChange();
  } catch (e) { showToast(e.message); }
});
$("#inboxFilter").addEventListener("change", loadInbox);
$("#confirmOk").addEventListener("click", async () => {
  const action = S.confirmAction;
  closeModals();
  if (action) { try { await action(); } catch (e) { showToast(e.message); } }
});
$("#patientForm").addEventListener("submit", submitPatient);
$("#planForm").addEventListener("submit", submitPlan);
$("#planPreviewForm").addEventListener("submit", submitPlanPreview);
$("#cycleForm").addEventListener("submit", submitCycle);
$("#recordForm").addEventListener("submit", submitRecord);
$("#recordForm").addEventListener("change", updateRecordForm);
$("#labForm").addEventListener("submit", submitLabs);
$("#labForm").elements.cycle_id.addEventListener("change", e => { $("#labForm").elements.collection_date.value = defaultCollectionDate(Number(e.target.value)); });
$("#uploadForm").elements.cycle_id.addEventListener("change", e => { if (!$("#uploadForm").dataset.dateTouched) $("#uploadForm").elements.recording_date.value = defaultCollectionDate(Number(e.target.value)); });
$("#uploadForm").elements.recording_date.addEventListener("input", () => { $("#uploadForm").dataset.dateTouched = "1"; });
$("#aiForm").addEventListener("submit", submitAiSettings);
$("#uploadForm").addEventListener("submit", submitUpload);
$("#toxicityForm").addEventListener("submit", submitToxicity);
$("#toxicityTerm").addEventListener("change", () => updateCriteria());
$("#toxicityForm").elements.cycle_id.addEventListener("change", () => { $("#toxicityForm").dataset.windowTouched = "1"; });
$("#toxicityForm").elements.onset_date.addEventListener("change", e => {
  const form = $("#toxicityForm");
  if (form.dataset.windowTouched) return;
  const match = cyclesOf().find(c => e.target.value <= (c.administered_date || c.planned_date));
  if (match) form.elements.cycle_id.value = match.id;
});
$("#labFile").addEventListener("change", e => { $("#dropzoneLabel").textContent = e.target.files[0]?.name || "Choose a lab report"; });
$("#reviewCycleSelect").addEventListener("change", e => { S.reviewCycleId = Number(e.target.value); renderReview(); });
$("#menuButton").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
let searchTimer;
$("#patientSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadPatients, 200); });
$("#patientStatusFilter").addEventListener("change", loadPatients);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModals(); });
document.addEventListener("mousemove", moveTooltip);
window.addEventListener("hashchange", route);

function clinicianName() { return S.me?.name || ""; }
function showDoctorLogin() {
  S.me = null;
  closeModals();
  $("#doctorLogin").hidden = false;
  $(".app-shell").hidden = true;
  setTimeout(() => $("#doctorLoginForm [name=phone]")?.focus(), 30);
}
function renderClinician() {
  const me = S.me;
  $("#clinicianName").textContent = me ? me.name : "Signed out";
  $("#clinicianRole").textContent = me ? `${me.specialty || "Clinician"} · ${me.phone}` : "";
  $("#clinicianInitials").textContent = me ? me.name.replace(/^Dr\.?\s*/i, "").split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase() : "—";
}
async function doctorSignIn(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const phone = form.elements.phone.value.trim(), password = form.elements.password.value;
  if (!phone || !password) return formError(form, "Enter your mobile number and password.");
  await withBusy(form, async () => {
    S.me = await post("/api/auth/login", { phone, password });
    form.reset();
    $("#doctorLogin").hidden = true;
    $(".app-shell").hidden = false;
    await bootApp();
  });
}
let booted = false;
async function bootApp() {
  renderClinician();
  if (booted) { route(); refreshInboxBadge(); return; }
  booted = true;
  try {
    const health = await fetch("/health").then(r => r.json()).catch(() => ({}));
    S.health = health;
    renderAiStatus();
    if ((health.api_version || 1) < 3) {
      document.body.insertAdjacentHTML("afterbegin", '<div class="server-banner" role="alert"><strong>The backend server is running old code.</strong> Stop it (Ctrl+C) and start it again with <code>python3 -m backend.server --seed</code>, then reload this page. Saving plans will fail until then.</div>');
    }
    S.reference = await api("/api/reference");
    $("#drugList").innerHTML = S.reference.drugs.map(d => `<option value="${esc(d.drug_name)}">${esc(d.class)}</option>`).join("");
  } catch (error) {
    showToast(error.message);
  }
  refreshInboxBadge();
  setInterval(() => { if (S.me) refreshInboxBadge(); }, 60000);
  if (!location.hash) history.replaceState(null, "", "#patients");
  route();
}
(async function init() {
  document.body.insertAdjacentHTML("beforeend", `<datalist id="drugList"></datalist><datalist id="unitList">${UNITS.map(u => `<option value="${u}">`).join("")}</datalist>`);
  bindDraftInputs();
  $("#doctorLoginForm").addEventListener("submit", doctorSignIn);
  try {
    S.me = await api("/api/auth/me");
  } catch {
    return showDoctorLogin();
  }
  $("#doctorLogin").hidden = true;
  $(".app-shell").hidden = false;
  await bootApp();
})();

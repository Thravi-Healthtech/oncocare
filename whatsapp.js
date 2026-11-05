"use strict";
/* WhatsApp: consent, clinician-approved outbound messages, reminders and the patient chat (simulator or live). */

const WA_KIND = { plan: "Treatment plan", prescription: "Prescription", discharge: "Discharge summary", reminder: "Visit reminder", message: "Message", reply: "Reply to patient message", bot: "Automatic reply" };
const WA_CONSENT = { in_clinic_verbal: "verbal consent in clinic", in_clinic_written: "written consent in clinic", patient_whatsapp: "patient sent START", patient_stop: "patient sent STOP", clinician_opt_out: "turned off by clinician" };
const LANG_NAME = { en: "English", hi: "Hindi", te: "Telugu" };

EXT.routes.whatsapp = async id => {
  S.waPatientId = id || S.waPatientId || null;
  await loadWa();
  return "whatsapp";
};
EXT.patientPanels.push(p => `<article class="panel span-full wa-panel"><div class="panel-heading"><div><p class="eyebrow">WhatsApp</p><h2>Patient messaging</h2></div><a class="button secondary" href="#whatsapp/${p.id}">Open chat</a></div>
  <p class="muted">${!p.phone ? "Add the patient's mobile number to use WhatsApp." : p.wa_opt_in ? `✓ Opted in (${esc(WA_CONSENT[p.wa_opt_in_method] || p.wa_opt_in_method || "")}${p.wa_opt_in_at ? `, ${fmtDate(p.wa_opt_in_at)}` : ""}) · +${esc(waNumber(p.phone))}${p.preferred_language ? ` · ${LANG_NAME[p.preferred_language] || p.preferred_language}` : ""}` : `Not opted in. Record the patient's consent before sending plans or reminders. Patients can still message the clinic; replies to their messages are allowed.`}</p>
  ${p.phone ? `<div class="heading-actions wrap">${p.wa_opt_in ? "" : `<button class="button primary small" data-wa="consent" data-patient="${p.id}">Record consent</button>`}
    <button class="button secondary small" data-wa-share="plan" data-patient="${p.id}">Share treatment plan</button>
    <button class="button secondary small" data-wa-share="reminder" data-patient="${p.id}">Visit reminder</button>
    ${(p.discharge_summaries || []).some(d => d.status === "final") ? `<button class="button secondary small" data-wa-share="discharge" data-patient="${p.id}">Share discharge summary</button><button class="button secondary small" data-wa-share="prescription" data-patient="${p.id}">Send prescription</button>` : ""}</div>` : ""}
</article>`);
function waNumber(phone) { const d = String(phone || "").replace(/\D/g, ""); return d.length === 10 ? `91${d}` : d; }

/* ------------------------------------------------------------------ data */
async function loadWa() {
  const list = await api("/api/whatsapp/threads");
  S.wa = list;
  if (!S.waPatientId && list.threads.length) S.waPatientId = list.threads[0].id;
  S.waThread = S.waPatientId ? await api(`/api/patients/${S.waPatientId}/whatsapp`) : null;
  renderWa();
  updateWaBadge();
}
async function refreshThread() {
  if (!S.waPatientId) return;
  S.waThread = await api(`/api/patients/${S.waPatientId}/whatsapp`);
  renderWa();
}
function updateWaBadge() {
  const n = (S.wa?.threads || []).reduce((sum, t) => sum + (t.open_reports || 0), 0);
  const badge = $("#waBadge");
  badge.textContent = n;
  badge.classList.toggle("hidden", !n);
}

/* ------------------------------------------------------------------ render */
function tick(m) {
  if (m.direction === "in") return "";
  return { draft: '<span class="tick">draft</span>', scheduled: `<span class="tick">🕑 ${fmtDateTime(m.scheduled_for)}</span>`, sent: '<span class="tick">✓</span>', delivered: '<span class="tick">✓✓</span>',
    read: '<span class="tick read">✓✓</span>', failed: `<span class="tick fail" title="${esc(m.error || "")}">✗ failed</span>`, cancelled: '<span class="tick">cancelled</span>' }[m.status] || "";
}
function bubble(m) {
  const out = m.direction === "out";
  const label = out ? (m.kind === "bot" ? "Automatic reply" : `${WA_KIND[m.kind] || m.kind}${m.approved_by ? ` · approved by ${m.approved_by}` : ""}`) : (m.kind === "voice" ? "Voice note" : "");
  const body = esc(m.body).replace(/\*(.+?)\*/g, "<b>$1</b>").replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  return `<div class="wa-msg ${out ? "out" : "in"} ${m.kind === "bot" ? "bot" : ""} ${["scheduled", "cancelled", "failed"].includes(m.status) ? m.status : ""}">
    ${label ? `<small class="wa-label">${esc(label)}</small>` : ""}
    <div class="wa-text">${body}</div>
    ${m.has_audio && m.report_id ? `<audio controls preload="none" src="/api/patient-reports/${m.report_id}/audio"></audio>` : ""}
    <div class="wa-meta">${m.report_id && !out ? `<a href="#inbox" class="wa-link">in inbox</a>` : ""}<span>${new Date(m.sent_at || m.created_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}</span>${tick(m)}</div>
    ${m.status === "scheduled" ? `<button class="text-button small" data-wa="cancel" data-message="${m.id}">Cancel</button>` : ""}
  </div>`;
}
function renderWa() {
  const st = S.wa?.status || {};
  $("#waModeBadge").className = `chip ${st.mode === "live" ? "chip-wa" : "chip-warn"}`;
  $("#waModeBadge").textContent = st.mode === "live" ? `Live · ${st.display_number || "WhatsApp Business"}` : "Simulator · nothing leaves the server";
  const threads = S.wa?.threads || [];
  const t = S.waThread;
  const p = t?.patient;
  const messages = (t?.messages || []).filter(m => !["draft", "cancelled"].includes(m.status));
  let lastDay = "";
  const chat = messages.map(m => {
    const day = (m.sent_at || m.created_at).slice(0, 10);
    const sep = day !== lastDay ? `<div class="wa-day">${fmtDate(day)}</div>` : "";
    lastDay = day;
    return sep + bubble(m);
  }).join("");
  const scheduled = (t?.messages || []).filter(m => m.status === "scheduled");
  $("#waContent").innerHTML = `<div class="wa-layout">
    <aside class="wa-threads panel">
      <input type="search" id="waSearch" placeholder="Find patient…" value="${esc(S.waSearch || "")}" />
      <div class="wa-thread-list">${threads.filter(x => !S.waSearch || `${x.first_name} ${x.last_name} ${x.medical_record_number}`.toLowerCase().includes(S.waSearch.toLowerCase())).map(x => `
        <a class="wa-thread ${x.id === S.waPatientId ? "active" : ""}" href="#whatsapp/${x.id}">
          <span class="avatar">${esc(initials(x))}</span>
          <span class="wa-thread-main"><strong>${esc(x.first_name)} ${esc(x.last_name)}</strong><small>${x.last_body ? `${x.last_direction === "in" ? "" : "You: "}${esc(x.last_body.slice(0, 60))}` : esc(x.medical_record_number)}</small></span>
          <span class="wa-thread-side">${x.open_reports ? `<span class="nav-badge">${x.open_reports}</span>` : ""}${x.wa_opt_in ? "" : '<small class="muted">no opt-in</small>'}</span>
        </a>`).join("") || '<p class="muted">Patients with a mobile number appear here.</p>'}</div>
      ${S.wa?.unmatched?.length ? `<details class="wa-unmatched"><summary>${S.wa.unmatched.length} message(s) from unregistered numbers</summary>${S.wa.unmatched.map(u => `<p class="small"><strong>+${esc(u.wa_from)}</strong> ${fmtDateTime(u.created_at)}<br>${esc(u.body)}</p>`).join("")}</details>` : ""}
    </aside>
    ${p ? `<section class="wa-phone">
      <header class="wa-head"><span class="avatar">${esc(initials(p))}</span><div><strong>${esc(p.first_name)} ${esc(p.last_name)}</strong><small>+${esc(p.wa_number || "")} · ${esc(st.clinic || "")}</small></div>
        <span class="wa-window ${t.window_open ? "open" : ""}" title="WhatsApp allows free-form messages for 24 hours after the patient's last message; otherwise only approved templates">${t.window_open ? "24-h window open" : "Templates only"}</span></header>
      <div class="wa-body" id="waBody">${chat || '<div class="wa-empty">No messages yet. Share the treatment plan, or type as the patient below to try the symptom flow.</div>'}</div>
      ${st.mode === "simulator" ? `<form class="wa-compose" id="waSimForm"><div class="wa-quick">${["1", "2", "CHECK", "HELP", "STOP"].map(q => `<button type="button" class="chip" data-wa="quick" data-text="${q}">${q}</button>`).join("")}<span class="muted small">Simulator: you are typing as the patient</span></div>
        <div class="wa-input"><input name="text" placeholder="Message as ${esc(p.first_name)} — e.g. my skin is peeling where I get radiation" autocomplete="off" />
        <button type="button" class="wa-mic" data-wa="record" title="Record a voice note as the patient" aria-label="Record voice note">🎤</button><button class="wa-send" type="submit" aria-label="Send">➤</button></div></form>`
      : `<div class="wa-compose live-note">Patients message your WhatsApp Business number; their messages arrive here and in Patient messages.</div>`}
    </section>
    <aside class="wa-side">
      <article class="panel"><p class="eyebrow">Consent</p>
        ${p.wa_opt_in ? `<p><strong>✓ Opted in</strong><br><span class="muted">${esc(WA_CONSENT[p.wa_opt_in_method] || p.wa_opt_in_method || "")}${p.wa_opt_in_at ? ` · ${fmtDate(p.wa_opt_in_at)}` : ""}</span></p><button class="text-button" data-wa="consent" data-patient="${p.id}">Change</button>`
          : `<p class="muted">${p.wa_opt_in_method === "patient_stop" ? "The patient sent STOP." : "Not opted in."} Plans, prescriptions and reminders need the patient's consent.</p><button class="button primary small" data-wa="consent" data-patient="${p.id}">Record consent</button>`}
        <p class="muted small">Language: ${LANG_NAME[p.preferred_language] || "English"} (automatic replies and questions)</p>
      </article>
      <article class="panel top-gap"><p class="eyebrow">Send to patient</p><p class="muted small">Composed from the record; you review and approve every message.</p>
        <div class="wa-actions">
          <button class="button secondary" data-wa-share="plan" data-patient="${p.id}">Treatment plan</button>
          <button class="button secondary" data-wa-share="reminder" data-patient="${p.id}">Visit reminder</button>
          <button class="button secondary" data-wa-share="discharge" data-patient="${p.id}" ${t.final_summaries.length ? "" : "disabled title=\"Finalise a discharge summary first\""}>Discharge summary</button>
          <button class="button secondary" data-wa-share="prescription" data-patient="${p.id}" ${t.final_summaries.length ? "" : "disabled title=\"Finalise a discharge summary with medicines first\""}>Prescription</button>
          <button class="button secondary" data-wa-share="message" data-patient="${p.id}">Free-text message</button>
        </div>
      </article>
      ${scheduled.length ? `<article class="panel top-gap"><p class="eyebrow">Scheduled</p>${scheduled.map(m => `<div class="note-item"><strong>${esc(WA_KIND[m.kind] || m.kind)}</strong><p class="muted small">${fmtDateTime(m.scheduled_for)}</p><button class="text-button" data-wa="cancel" data-message="${m.id}">Cancel</button></div>`).join("")}</article>` : ""}
      ${st.mode === "live" ? `<article class="panel top-gap"><p class="eyebrow">Live connection</p><p class="muted small">Webhook: <code>${esc(location.origin)}/api/whatsapp/webhook</code>${st.webhook_signed ? " · signed" : " · <span class=\"text-amber\">set WHATSAPP_APP_SECRET</span>"}</p>${st.note ? `<p class="ai-error">${esc(st.note)}</p>` : ""}</article>` : `<article class="panel top-gap"><p class="eyebrow">Going live</p><p class="muted small">Set <code>WHATSAPP_MODE=live</code>, the access token and phone number ID in <code>.env</code>, and submit the five message templates (<button class="text-button small" data-wa="templates">view</button>) for approval.</p></article>`}
    </aside>` : `<section class="wa-phone"><div class="wa-empty">Choose a patient.</div></section><aside></aside>`}
  </div>`;
  const body = $("#waBody");
  if (body) body.scrollTop = body.scrollHeight;
}

/* ------------------------------------------------------------------ compose & approve */
function waModal(title, sub, html, submit) {
  $("#waModalTitle").textContent = title;
  $("#waModalSub").textContent = sub;
  $("#waFormBody").innerHTML = html;
  $("#waSubmit").textContent = submit;
  formError($("#waForm"), "");
  openModal("waModal");
}
async function startShare(kind, opts) {
  const pid = Number(opts.patient || S.patient?.id || S.waPatientId);
  if (!pid) return showToast("Choose a patient first.");
  S.waCompose = { kind, pid, draft: null, opts };
  const thread = await api(`/api/patients/${pid}/whatsapp`);
  S.waCompose.thread = thread;
  const name = `${thread.patient.first_name} ${thread.patient.last_name}`;
  if (kind === "reminder") {
    const d = opts.visit || addDays(todayISO(), 7);
    return waModal("Visit reminder", `${name} · sent the day before at 10:00 unless you choose a time`, `
      <div class="form-grid three"><label class="field"><span>Visit date</span><input type="date" name="visit_date" min="${todayISO()}" value="${esc(d)}" required /></label>
      <label class="field"><span>Visit time</span><input type="time" name="visit_time" value="10:00" /></label>
      <label class="field"><span>Send at (optional)</span><input type="datetime-local" name="send_at" /></label>
      <label class="field span-3"><span>Purpose</span><input name="purpose" maxlength="200" value="${esc(opts.with ? `Follow-up with ${opts.with}` : "")}" placeholder="e.g. Weekly review during radiotherapy" /></label></div>
      <label class="confirm-check"><input type="checkbox" name="bring_reports" checked /><span>Remind them to bring reports and medicines</span></label>`, "Preview message");
  }
  if (kind === "message") {
    return waModal("Message the patient", `${name}${thread.window_open ? "" : " · outside the 24-hour window (live mode needs a template)"}`, `<label class="field"><span>Message</span><textarea name="text" rows="5" maxlength="3500" required></textarea></label>`, "Preview message");
  }
  if ((kind === "discharge" || kind === "prescription") && !opts.summary && thread.final_summaries.length > 1) {
    return waModal(WA_KIND[kind], name, `<label class="field"><span>Summary</span><select name="summary_id">${thread.final_summaries.map(s => `<option value="${s.id}">${esc(s.kind.replace("_", " "))} · finalised ${fmtDate(s.finalised_at)}</option>`).join("")}</select></label>`, "Preview message");
  }
  return composeNow({ summary_id: opts.summary || null });
}
async function composeNow(extra) {
  const c = S.waCompose;
  const draft = await post(`/api/patients/${c.pid}/whatsapp/compose`, { kind: c.kind, ...extra });
  c.draft = draft;
  const p = c.thread.patient;
  const scheduled = draft.scheduled_for && draft.scheduled_for > new Date().toISOString();
  waModal(`${WA_KIND[c.kind]} · review before sending`, `To ${p.first_name} ${p.last_name} · +${p.wa_number}`, `
    ${draft.checks.map(ch => `<p class="${ch.severity === "alert" ? "check-alert" : "safety-banner"}">${esc(ch.text)}${ch.severity === "alert" && !p.wa_opt_in ? ` <button type="button" class="text-button" data-wa="consent" data-patient="${p.id}">Record consent</button>` : ""}</p>`).join("")}
    <div class="wa-preview"><div class="wa-msg out"><div class="wa-text" id="waPreviewText">${esc(draft.body)}</div></div></div>
    <label class="field"><span>Message (you can edit before approving)</span><textarea name="body" rows="9" maxlength="3500">${esc(draft.body)}</textarea></label>
    ${scheduled ? `<p class="muted">Will be sent on <strong>${fmtDateTime(draft.scheduled_for)}</strong>.</p>` : ""}
    ${draft.template_name ? `<p class="muted small">Outside the 24-hour window, live mode sends the approved template <code>${esc(draft.template_name)}</code> with these details on one line.</p>` : ""}
    <label class="confirm-check"><input type="checkbox" name="approve" /><span>I checked this message against the record and approve sending it to the patient.</span></label>`,
    scheduled ? "Approve & schedule" : "Approve & send");
}
$("#waForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget, f = form.elements, c = S.waCompose;
  await withBusy(form, async () => {
    if (!c.draft) {
      if (c.kind === "reminder") return composeNow({ visit_date: f.visit_date.value, visit_time: f.visit_time.value, purpose: f.purpose.value, bring_reports: f.bring_reports.checked, send_at: f.send_at.value || null });
      if (c.kind === "message") return composeNow({ text: f.text.value });
      return composeNow({ summary_id: f.summary_id?.value || null });
    }
    if (!f.approve.checked) throw new Error("Tick the approval box to send.");
    if (f.body.value.trim() !== c.draft.body.trim()) await patch(`/api/whatsapp/messages/${c.draft.id}`, { body: f.body.value });
    const sent = await post(`/api/whatsapp/messages/${c.draft.id}/send`, {});
    closeModals();
    showToast(sent.status === "scheduled" ? `Reminder scheduled for ${fmtDateTime(sent.scheduled_for)}.` : sent.status === "sent" ? "Sent on WhatsApp." : `Not sent: ${sent.error || sent.status}`);
    c.draft = null;
    if (S.view === "whatsapp") { S.waPatientId = c.pid; await loadWa(); }
  });
});
$("#waModal").addEventListener("input", e => { if (e.target.name === "body") $("#waPreviewText").textContent = e.target.value; });

function openConsent(pid) {
  const resume = !$("#waModal").hidden && S.waCompose?.draft ? S.waCompose : null;
  if (resume) closeModals();
  const thread = S.waCompose?.thread?.patient?.id === pid ? S.waCompose.thread : S.waThread?.patient?.id === pid ? S.waThread : null;
  const p = thread?.patient || (S.patient?.id === pid ? S.patient : {});
  $("#recDialogEyebrow").textContent = "WhatsApp";
  $("#recDialogTitle").textContent = "Patient consent for WhatsApp";
  $("#recDialogSub").textContent = "Needed before sending treatment plans, prescriptions, summaries or reminders. The patient can reply STOP at any time.";
  $("#recDialogBody").innerHTML = `<fieldset class="outcome-options compact-options"><legend>Consent</legend>
      <label><input type="radio" name="opt" value="in_clinic_written" ${p.wa_opt_in_method !== "in_clinic_verbal" ? "checked" : ""} /><span><strong>Opted in — written consent in clinic</strong></span></label>
      <label><input type="radio" name="opt" value="in_clinic_verbal" ${p.wa_opt_in_method === "in_clinic_verbal" ? "checked" : ""} /><span><strong>Opted in — verbal consent in clinic</strong></span></label>
      <label><input type="radio" name="opt" value="off" /><span><strong>Not opted in / withdraw</strong><small>Scheduled reminders are cancelled</small></span></label></fieldset>
    <label class="field"><span>Language for automatic replies and questions</span><select name="language">${Object.entries(LANG_NAME).map(([k, v]) => `<option value="${k}" ${p.preferred_language === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <p class="muted small">Hindi and Telugu texts are prototype translations awaiting native-speaker review.</p>`;
  $("#recDialogSubmit").textContent = "Save consent";
  formError($("#recDialogForm"), "");
  S.recSubmit = async f => {
    const opt = f.opt.value;
    await post(`/api/patients/${pid}/whatsapp/consent`, { opt_in: opt !== "off", method: opt === "off" ? "clinician_opt_out" : opt, language: f.language.value });
    showToast(opt === "off" ? "WhatsApp messages turned off for this patient." : "Consent recorded.");
    if (S.patient?.id === pid) await loadPatient(pid);
    if (S.view === "whatsapp") await loadWa();
    if (resume && opt !== "off") {
      await del(`/api/whatsapp/messages/${resume.draft.id}`).catch(() => {});
      setTimeout(() => startShare(resume.kind, resume.opts).catch(e => showToast(e.message)), 50);
    }
  };
  openModal("recDialog");
}
function showTemplates() {
  api("/api/whatsapp/templates").then(list => {
    $("#recDialogEyebrow").textContent = "WhatsApp Business";
    $("#recDialogTitle").textContent = "Message templates to submit to Meta";
    $("#recDialogSub").textContent = "Business-initiated messages outside the 24-hour window need these approved UTILITY templates.";
    $("#recDialogBody").innerHTML = list.map(t => `<div class="template-card"><strong>${esc(t.name)}</strong> <span class="chip">${esc(t.category)}</span> <span class="chip">${esc(t.language)}</span>
      <p>${esc(t.body)}</p><p class="muted small">Example: ${t.example.map(esc).join(" | ")}</p></div>`).join("");
    $("#recDialogSubmit").textContent = "Close";
    S.recSubmit = async () => {};
    formError($("#recDialogForm"), "");
    openModal("recDialog");
  }).catch(e => showToast(e.message));
}

/* ------------------------------------------------------------------ simulator */
async function simulate(payload) {
  const result = await post("/api/whatsapp/simulate", { patient_id: S.waPatientId, ...payload });
  if (result.thread) { S.waThread = result.thread; renderWa(); }
  if (result.report_id || result.action === "checkin_submitted") refreshInboxBadge();
  api("/api/whatsapp/threads").then(l => { S.wa = l; updateWaBadge(); if (S.view === "whatsapp") renderWa(); }).catch(() => {});
}
async function toggleRecording(button) {
  if (S.waRecorder) { S.waRecorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") return showToast("This browser cannot record audio.");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    const rec = new MediaRecorder(stream);
    rec.ondataavailable = e => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      S.waRecorder = null;
      button.classList.remove("recording");
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      const data = await new Promise(resolve => { const r = new FileReader(); r.onload = () => resolve(String(r.result).split(",")[1]); r.readAsDataURL(blob); });
      try { await simulate({ audio_base64: data, audio_type: blob.type }); } catch (e) { showToast(e.message); }
    };
    rec.start();
    S.waRecorder = rec;
    button.classList.add("recording");
    showToast("Recording as the patient… press 🎤 again to send.");
  } catch { showToast("Microphone permission was not given."); }
}

/* ------------------------------------------------------------------ events */
document.addEventListener("click", async event => {
  const share = event.target.closest("[data-wa-share]");
  if (share) {
    event.preventDefault();
    try { await startShare(share.dataset.waShare, { patient: share.dataset.patient, summary: share.dataset.summary, visit: share.dataset.visit, with: share.dataset.with }); }
    catch (e) { showToast(e.message); }
    return;
  }
  const el = event.target.closest("[data-wa]");
  if (!el) return;
  try {
    switch (el.dataset.wa) {
      case "consent": return openConsent(Number(el.dataset.patient));
      case "quick": return simulate({ text: el.dataset.text });
      case "record": return toggleRecording(el);
      case "templates": return showTemplates();
      case "cancel": await del(`/api/whatsapp/messages/${el.dataset.message}`); showToast("Scheduled message cancelled."); return refreshThread();
    }
  } catch (e) { showToast(e.message); }
});
document.addEventListener("submit", async event => {
  if (event.target.id !== "waSimForm") return;
  event.preventDefault();
  const input = event.target.elements.text;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  try { await simulate({ text }); } catch (e) { showToast(e.message); input.value = text; }
  $("#waSimForm input")?.focus();
});
document.addEventListener("input", event => {
  if (event.target.id === "waSearch") {
    S.waSearch = event.target.value;
    const pos = event.target.selectionStart;
    renderWa();
    const box = $("#waSearch");
    box.focus();
    box.setSelectionRange(pos, pos);
  }
});
setInterval(() => { if (S.me && S.view === "whatsapp" && !S.waRecorder && !document.querySelector(".modal:not([hidden])") && document.activeElement?.name !== "text") refreshThread().catch(() => {}); }, 8000);
setTimeout(() => { if (S.me) api("/api/whatsapp/threads").then(l => { S.wa = S.wa || l; updateWaBadge(); }).catch(() => {}); }, 1500);
setInterval(() => { if (S.me) api("/api/whatsapp/threads").then(l => { S.wa = l; updateWaBadge(); }).catch(() => {}); }, 60000);

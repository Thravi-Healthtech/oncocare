"use strict";
/* Stand-alone prescriptions: written at any visit (consultation, chemo day, between cycles), printable, sent on WhatsApp. */

const RX_CONTEXT = { consultation: "Consultation", chemo_day: "Chemotherapy day", between_cycles: "Between cycles", radiotherapy: "During radiotherapy",
  post_op: "After surgery", follow_up: "Follow-up visit", other: "Other" };
const RX_COMMON = ["Ondansetron", "Domperidone", "Pantoprazole", "Paracetamol", "Dexamethasone", "Loperamide", "Lactulose", "Metoclopramide",
  "Granisetron", "Aprepitant", "Folic acid", "Vitamin B12", "Calcium + Vitamin D3", "Iron (ferrous sulphate)", "Chlorhexidine mouthwash",
  "Benzydamine mouthwash", "Clotrimazole mouth paint", "Fluconazole", "Cetirizine", "Tramadol", "Morphine (oral)", "Filgrastim (G-CSF)",
  "Enoxaparin", "Allopurinol", "Hydrogel dressing", "Aloe vera gel", "Moisturising cream (urea 10%)"];

EXT.patientLoaders.push(async p => { p.prescriptions = await api(`/api/patients/${p.id}/prescriptions`).catch(() => []); });
EXT.patientPanels.push(p => {
  const list = p.prescriptions || [];
  return `<article class="panel span-full rx-panel"><div class="panel-heading"><div><p class="eyebrow">Medicines between cycles</p><h2>Prescriptions</h2></div>
      <div class="heading-actions"><button class="button primary" data-rx="new" data-patient="${p.id}">Write prescription</button></div></div>
    ${list.length ? list.map(rx => `<div class="plan-row ${rx.status === "cancelled" ? "muted-row" : ""}"><div class="plan-row-main">
        <strong>${fmtDate(rx.rx_date)} · ${esc(rx.context_label)}${rx.status === "cancelled" ? ' <span class="chip chip-warn">Cancelled</span>' : ""}</strong>
        <small>${rx.items.map(m => esc([m.name, m.dose, m.frequency, m.duration ? `for ${m.duration}` : ""].filter(Boolean).join(" "))).join(" · ")}</small>
        <small class="muted">${esc(rx.prescribed_by)}${rx.source === "conversation" ? " · drafted from the consultation" : ""}${rx.indication ? ` · ${esc(rx.indication)}` : ""}${rx.status === "cancelled" ? ` · cancelled: ${esc(rx.cancel_reason || "")}` : ""}</small>
        ${rx.consult_notes || rx.transcript ? `<details class="rx-notes"><summary>Consultation notes</summary>${rx.consult_notes ? `<p>${esc(rx.consult_notes)}</p>` : ""}${rx.transcript ? `<p class="muted small"><strong>Transcript:</strong> ${esc(rx.transcript)}</p>` : ""}</details>` : ""}</div>
        <div class="plan-row-actions"><a class="button secondary small" href="/api/prescriptions/${rx.id}/print" target="_blank" rel="noopener">Print / PDF</a>
        ${rx.status === "active" ? `${p.phone ? `<button class="button secondary small" data-wa-share="prescription" data-patient="${p.id}" data-prescription="${rx.id}">Send on WhatsApp</button>` : ""}
          <button class="text-button" data-rx="cancel" data-id="${rx.id}" data-patient="${p.id}">Cancel</button>` : ""}</div></div>`).join("")
      : '<div class="queue-empty">No prescriptions yet. Write one at any visit — for example anti-sickness medicines on a chemo day or a mouthwash between cycles.</div>'}
  </article>`;
});

function rxMedRows(items) {
  return items.map((m, i) => medRow(m, i, false)).join("");
}
function openPrescription(pid, from) {
  const p = S.patient?.id === pid ? S.patient : { id: pid };
  const plans = (p.treatment_plans || p.plans || []).filter(x => x && x.id);
  const seed = from?.items?.length ? from.items : [{}, {}];
  recDialog({ eyebrow: "Prescription", title: `Write prescription${p.first_name ? ` · ${p.first_name} ${p.last_name}` : ""}`,
    sub: "Signed in your name when you save. To change it later, cancel it and write a new one.",
    body: `<section class="rx-scribe"><div class="rx-scribe-head"><div><strong>Consultation conversation</strong><small class="muted" id="rxScribeStatus">Record the conversation with the patient, or type it. Claude Haiku turns it into the prescription below for you to check.</small></div>
        <div class="heading-actions"><select name="scribe_lang" aria-label="Main language spoken" title="Main language (used by the browser's speech recognition)"><option value="en-IN">English</option><option value="hi-IN">Hindi</option><option value="te-IN">Telugu</option></select>
        <button type="button" class="button secondary small" data-rx="record">🎙 Record</button></div></div>
        <textarea name="transcript" rows="4" maxlength="30000" placeholder="The conversation appears here as you talk. You can correct it or type it."></textarea>
        <div class="heading-actions wrap"><button type="button" class="button primary small" data-rx="draft">Fill prescription from conversation</button><span class="muted small" id="rxDraftStatus"></span></div>
        <div id="rxDraftNote"></div>
        <label class="field"><span>Consultation notes (for the record, not printed for the patient)</span><textarea name="consult_notes" rows="2" maxlength="4000" placeholder="e.g. Nausea grade 2 since cycle 2, eating less. Plan: add olanzapine at night."></textarea></label></section>
      <div class="form-grid three"><label class="field"><span>Date</span><input type="date" name="rx_date" max="${todayISO()}" value="${todayISO()}" required /></label>
      <label class="field"><span>Visit</span><select name="context">${Object.entries(RX_CONTEXT).map(([k, v]) => `<option value="${k}" ${k === (from?.context || "consultation") ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      ${plans.length ? `<label class="field"><span>Related plan (optional)</span><select name="treatment_plan_id"><option value="">—</option>${plans.map(x => `<option value="${x.id}">${esc(x.regimen_name || x.name || `Plan ${x.id}`)}</option>`).join("")}</select></label>` : "<span></span>"}
      <label class="field span-3"><span>Reason (optional)</span><input name="indication" maxlength="300" value="${esc(from?.indication || "")}" placeholder="e.g. Grade 2 nausea after cycle 2; oral mucositis" /></label></div>
      <datalist id="rxDrugList">${RX_COMMON.map(d => `<option value="${esc(d)}"></option>`).join("")}</datalist>
      <div class="table-wrap"><table class="med-table"><thead><tr><th>Medicine</th><th>Dose</th><th>Route</th><th>How often</th><th>For how long</th><th>Instructions</th><th></th></tr></thead>
      <tbody id="rxMedRows">${rxMedRows(seed)}</tbody></table></div>
      <button type="button" class="button secondary small" data-rx="add-row">+ Add medicine</button>
      <label class="field top-gap"><span>Advice (optional)</span><textarea name="advice" rows="2" maxlength="1000" placeholder="e.g. Drink 2–3 litres of water a day. Call the ward if fever above 38 °C.">${esc(from?.advice || "")}</textarea></label>
      ${waTick(p, "Send on WhatsApp after saving")}`,
    submit: "Sign & save",
    onSubmit: async f => {
      const items = $$("#rxMedRows tr").map(tr => Object.fromEntries(["name", "dose", "route", "frequency", "duration", "instructions"].map(k => [k, $(`[name=${k}]`, tr).value.trim()])))
        .filter(m => Object.values(m).some(Boolean));
      if (!items.length) throw new Error("Add at least one medicine.");
      rxStopRecording();
      const saved = await post(`/api/patients/${pid}/prescriptions`, { rx_date: f.rx_date.value, context: f.context.value, treatment_plan_id: f.treatment_plan_id?.value || null,
        indication: f.indication.value, advice: f.advice.value, items, notify_whatsapp: f.notify_whatsapp?.checked || false,
        consult_notes: f.consult_notes.value, transcript: f.transcript.value });
      showToast(`Prescription saved (${saved.items.length} medicine${saved.items.length > 1 ? "s" : ""}).${waResultText(saved.whatsapp)}`);
      if (S.patient?.id === pid) await loadPatient(pid);
    } });
  $$("#rxMedRows [name=name]").forEach(input => input.setAttribute("list", "rxDrugList"));
  S.rxPatient = pid;
  api("/api/scribe/status").then(st => { S.rxScribe = st; rxScribeHint(); }).catch(() => {});
}

/* ------------------------------------------------------------------ consultation scribe */
function rxScribeHint(text) {
  const st = S.rxScribe || {};
  const el = $("#rxScribeStatus");
  if (!el) return;
  el.textContent = text || `Transcription: ${st.sarvam ? "Sarvam AI" : "browser speech recognition (set SARVAM_API_KEY for Sarvam)"} · drafting: ${st.ai ? "Claude Haiku" : "AI is off — fill the form by hand"}.`;
}
function rxAppend(text) {
  const box = $("#recDialogForm [name=transcript]");
  if (!box || !text?.trim()) return;
  box.value = (box.value.trim() ? box.value.trim() + " " : "") + text.trim();
  box.scrollTop = box.scrollHeight;
}
async function rxBlobToBase64(blob) {
  return new Promise(resolve => { const r = new FileReader(); r.onload = () => resolve(String(r.result).split(",")[1]); r.readAsDataURL(blob); });
}
async function rxStartRecording(button) {
  const st = S.rxScribe || await api("/api/scribe/status").catch(() => ({}));
  S.rxScribe = st;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!st.sarvam && !SR) return showToast("This browser cannot transcribe speech. Type the conversation instead (Chrome or Edge can transcribe).");
  let stream = null;
  if (st.sarvam) {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") return showToast("This browser cannot record audio.");
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch { return showToast("Microphone permission was not given."); }
  }
  const R = S.rxRec = { on: true, stream, started: Date.now(), seq: 0, next: 0, done: {}, pending: 0, sr: null };
  button.textContent = "■ Stop"; button.classList.add("recording");
  const tick = () => {
    if (!R.on) return;
    if ($("#recDialog").hidden) return rxStopRecording();
    const secs = Math.round((Date.now() - R.started) / 1000);
    rxScribeHint(`● Recording ${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")} · ${st.sarvam ? "Sarvam AI transcribes every ~25 s" : "browser speech recognition"}${R.pending ? ` · transcribing ${R.pending}…` : ""}`);
    R.timer = setTimeout(tick, 1000);
  };
  tick();
  if (st.sarvam) {
    const flush = () => { while (R.done[R.next] !== undefined) { rxAppend(R.done[R.next]); delete R.done[R.next]; R.next++; } };
    const segment = () => {
      if (!R.on) return;
      const chunks = [];
      const rec = new MediaRecorder(stream);
      const seq = R.seq++;
      rec.ondataavailable = e => e.data.size && chunks.push(e.data);
      rec.onstop = async () => {
        if (R.on) segment();
        const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
        if (blob.size < 2000) { R.done[seq] = ""; flush(); return; }
        R.pending++;
        try {
          const heard = await post("/api/scribe/transcribe", { audio_base64: await rxBlobToBase64(blob), audio_type: blob.type, seq });
          R.done[seq] = heard.text || "";
        } catch (error) { R.done[seq] = ""; showToast(error.message); }
        R.pending--; flush();
        if (!R.on && !R.pending) { rxScribeHint(); stream.getTracks().forEach(t => t.stop()); }
      };
      rec.start();
      R.current = rec;
      R.cut = setTimeout(() => { if (rec.state === "recording") rec.stop(); }, (st.segment_seconds || 25) * 1000);
    };
    segment();
  } else {
    const sr = new SR();
    sr.lang = $("#recDialogForm [name=scribe_lang]")?.value || "en-IN";
    sr.continuous = true; sr.interimResults = false;
    sr.onresult = e => { for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) rxAppend(e.results[i][0].transcript); };
    sr.onend = () => { if (R.on) { try { sr.start(); } catch { /* restarts after silence */ } } };
    sr.onerror = e => { if (e.error === "not-allowed") { showToast("Microphone permission was not given."); rxStopRecording(); } };
    try { sr.start(); R.sr = sr; } catch { showToast("Speech recognition could not start."); rxStopRecording(); }
  }
}
function rxStopRecording() {
  const R = S.rxRec;
  if (!R || !R.on) return;
  R.on = false;
  clearTimeout(R.timer); clearTimeout(R.cut);
  try { if (R.current?.state === "recording") R.current.stop(); } catch { /* ignore */ }
  try { R.sr?.stop(); } catch { /* ignore */ }
  if (R.stream && !R.pending && R.current?.state !== "recording") R.stream.getTracks().forEach(t => t.stop());
  const button = $('[data-rx="record"]');
  if (button) { button.textContent = "🎙 Record"; button.classList.remove("recording"); }
  rxScribeHint(R.pending ? "Finishing the transcription…" : undefined);
}
async function rxDraftFromConversation(button) {
  const form = $("#recDialogForm"), f = form.elements;
  const transcript = f.transcript.value.trim();
  if (S.rxRec?.on) rxStopRecording();
  if (S.rxRec?.pending) return showToast("Wait a moment: the last part of the conversation is still being transcribed.");
  if (transcript.length < 20) return showToast("Record or type the conversation first.");
  button.disabled = true;
  $("#rxDraftStatus").textContent = "Claude Haiku is drafting the prescription…";
  try {
    const d = await post(`/api/patients/${S.rxPatient}/prescriptions/draft`, { transcript });
    if (d.items.length) {
      $("#rxMedRows").innerHTML = rxMedRows(d.items);
      $$("#rxMedRows [name=name]").forEach(input => input.setAttribute("list", "rxDrugList"));
    }
    if (d.context && f.context) f.context.value = d.context;
    if (d.indication && !f.indication.value.trim()) f.indication.value = d.indication;
    if (d.advice && !f.advice.value.trim()) f.advice.value = d.advice;
    if (d.notes) f.consult_notes.value = d.notes;
    $("#rxDraftNote").innerHTML = `<div class="${d.empty || d.uncertain.length ? "check-alert" : "safety-banner"}">
      <strong>${d.empty ? "No prescribed medicine was found in the conversation." : `Draft from the conversation (${d.items.length} medicine${d.items.length > 1 ? "s" : ""}) — check every medicine, dose and duration before signing.`}</strong>
      ${d.uncertain.length ? `<ul>${d.uncertain.map(u => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}<small class="muted">${esc(d.model || "Claude Haiku")}</small></div>`;
    $("#rxDraftStatus").textContent = "";
  } catch (error) {
    $("#rxDraftStatus").textContent = "";
    showToast(error.message);
  } finally { button.disabled = false; }
}
document.addEventListener("click", async e => {
  const el = e.target.closest("[data-rx]");
  if (!el) return;
  const pid = Number(el.dataset.patient || S.patient?.id);
  try {
    if (el.dataset.rx === "new") return openPrescription(pid);
    if (el.dataset.rx === "record") return S.rxRec?.on ? rxStopRecording() : rxStartRecording(el);
    if (el.dataset.rx === "draft") return rxDraftFromConversation(el);
    if (el.dataset.rx === "add-row") {
      $("#rxMedRows").insertAdjacentHTML("beforeend", medRow({}, $$("#rxMedRows tr").length, false));
      $$("#rxMedRows [name=name]").forEach(input => input.setAttribute("list", "rxDrugList"));
      return;
    }
    if (el.dataset.rx === "cancel") {
      return recDialog({ eyebrow: "Prescription", title: "Cancel prescription", sub: "It stays on the record marked cancelled and is removed from the patient portal.",
        body: `<label class="field"><span>Reason</span><input name="reason" maxlength="300" required placeholder="e.g. Wrong dose; replaced by a new prescription" /></label>`,
        submit: "Cancel prescription",
        onSubmit: async f => {
          if (!f.reason.value.trim()) throw new Error("Give the reason.");
          await post(`/api/prescriptions/${el.dataset.id}/cancel`, { reason: f.reason.value });
          showToast("Prescription cancelled.");
          if (S.patient?.id === pid) await loadPatient(pid);
        } });
    }
  } catch (error) { showToast(error.message); }
});

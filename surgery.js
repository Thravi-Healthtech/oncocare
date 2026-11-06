"use strict";
/* Surgical oncology: plan & pre-op checklist, operation note, post-operative course (Clavien–Dindo), histopathology and discharge. */

const SX_TABS = { plan: "sx-plan", operation: "sx-operation", postop: "sx-postop", pathology: "sx-pathology" };
const SX_STATUS = { planned: ["Planned", "neutral"], scheduled: ["Scheduled", "planned"], completed: ["Operated · in hospital", "complete"], discharged: ["Discharged", "complete"], cancelled: ["Cancelled", "alert"] };
const sxPill = s => { const [l, t] = SX_STATUS[s] || [s, "neutral"]; return `<span class="status-pill ${t}">${esc(l)}</span>`; };
const cdBadge = g => g ? `<span class="cd-badge cd-${esc(g.replace(/[ab]$/, ""))}" title="Clavien–Dindo ${esc(g)}">CD ${esc(g)}</span>` : "";

async function sxOptions() { if (!S.sxOptions) S.sxOptions = await api("/api/surgery/options"); return S.sxOptions; }

/* ------------------------------------------------------------------ routing */
EXT.routes.surgery = async (id, tab) => {
  if (!id) return null;
  await loadSx(id);
  return SX_TABS[tab] || "sx-plan";
};
for (const [tab, view] of Object.entries(SX_TABS)) EXT.navHref[view] = () => (S.SX ? `#surgery/${S.SX.case.id}/${tab}` : "#");
EXT.afterShow.push(view => {
  $("#navSX").classList.toggle("hidden", !S.SX || !view.startsWith("sx-"));
  if (S.SX) $("#navSXName").textContent = `Surgery · ${S.SX.case.procedure_planned}`;
  if (view.startsWith("sx-") && S.SX) $("#navPatient").classList.remove("hidden");
});
async function loadSx(id) {
  const data = await api(`/api/surgeries/${id}`);
  setSx(data);
  if (!S.patient || S.patient.id !== data.patient.id) await loadPatient(data.patient.id);
  await sxOptions();
}
function setSx(data) { S.SX = data; renderSx(); }
async function sxCall(method, path, body) {
  const data = await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  if (data?.case) setSx(data);
  return data;
}

/* ------------------------------------------------------------------ patient record rows */
EXT.planRows.push(p => (p.surgeries || []).map(c => ({
  modality: "surgical", date: c.surgery_date || c.created_at?.slice(0, 10) || "", label: c.procedure, html: `<div class="plan-row">
    <div class="plan-row-main"><span class="mod-tag mod-surgical">Surgical</span><strong>${esc(c.procedure)}</strong>
      <small>${[c.intent, c.surgeon, c.surgery_date ? `surgery ${fmtDate(c.surgery_date)}` : "date not set"].filter(Boolean).map(esc).join(" · ")}</small>
      <div class="drug-chips">${sxPill(c.status)}${c.stage ? `<span class="chip">${esc(c.stage)}</span>` : ""}${cdBadge(c.worst_clavien_dindo)}${c.attention.filter(a => a.severity !== "info").slice(0, 2).map(a => `<span class="chip ${a.severity === "alert" ? "chip-alert" : "chip-warn"}">${esc(a.text.split(" — ")[0])}</span>`).join("")}</div>
    </div>
    <div class="plan-row-progress"><div class="progress-track"><span style="width:${Math.round((c.preop_done / c.preop_total) * 100)}%"></span></div><small>Pre-op checklist ${c.preop_done}/${c.preop_total}</small></div>
    <div class="plan-row-actions"><a class="button secondary" href="#surgery/${c.id}/plan">Open case</a></div>
  </div>`,
})));
EXT.patientRowChips.push(p => p.active_surgery ? `<span class="chip chip-sx" title="${esc(p.active_surgery)}">✚ ${p.next_surgery_date ? fmtShort(p.next_surgery_date) : p.active_surgery_status === "completed" ? "post-op" : "planned"}</span>` : "");

/* ------------------------------------------------------------------ render */
function renderSx() {
  const W = S.SX;
  if (!W) return;
  const c = W.case;
  $$(".sx-subtitle").forEach(el => { el.textContent = `${patientName(W.patient)} · ${(c.operation || {}).procedure_performed || c.procedure_planned}`; });
  renderSxPlan(); renderSxOperation(); renderSxPostop(); renderSxPathology();
  updateHeader();
}
function sxDl(pairs) {
  const shown = pairs.filter(([, v]) => v !== undefined && v !== null && v !== "");
  return shown.length ? `<dl class="definition-grid">${shown.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>` : '<p class="muted">Not recorded.</p>';
}
function renderSxPlan() {
  const W = S.SX, c = W.case, s = W.summary, pre = c.preop || {};
  const closed = ["discharged", "cancelled"].includes(c.status);
  $("#sxplanTitle").textContent = c.procedure_planned;
  $("#sxplanActions").innerHTML = `${c.status !== "discharged" ? `<button class="button secondary" data-sx="edit-case">Edit plan</button>` : ""}
    ${["planned", "scheduled"].includes(c.status) ? `<button class="button secondary" data-sx="cancel">Cancel surgery</button>` : ""}
    ${c.status === "cancelled" ? `<button class="button primary" data-sx="replan">Re-plan</button>` : ""}
    <button class="button danger-outline" data-sx="delete">Delete</button>`;
  const checks = W.options.preop_checks;
  $("#sxplan").innerHTML = `
    <article class="cycle-hero sx-hero">
      <div class="cycle-ring"><span>${s.preop_done}</span><small>of ${s.preop_total}</small></div>
      <div class="cycle-hero-copy">
        <div class="inline-heading">${sxPill(c.status)}<span class="muted">${esc([W.options.intents[c.intent], W.options.urgency[c.urgency]].filter(Boolean).join(" · "))}</span></div>
        <h2>${esc(c.procedure_planned)}</h2>
        <p>${esc(c.indication || W.patient.diagnosis || "")}</p>
        <div class="dose-line">
          <span><small>Surgery</small><strong>${c.surgery_date ? fmtDate(c.surgery_date) : "Date not set"}</strong></span>
          <span><small>Admission</small><strong>${c.admission_date ? fmtDate(c.admission_date) : "—"}</strong></span>
          <span><small>Surgeon</small><strong>${esc(c.surgeon || "—")}</strong></span>
          <span><small>Site</small><strong>${esc([c.site, c.laterality].filter(Boolean).join(" · ") || "—")}</strong></span>
        </div>
      </div>
      ${["planned", "scheduled"].includes(c.status) ? `<a class="button light" href="#surgery/${c.id}/operation">Operation note →</a>` : c.status === "completed" ? `<a class="button light" href="#surgery/${c.id}/postop">Post-op notes →</a>` : ""}
    </article>
    ${c.status === "cancelled" ? `<div class="check-alert top-gap">Cancelled: ${esc(c.cancel_reason || "")}</div>` : ""}
    ${attentionHtml(s.attention)}
    <div class="patient-profile-grid top-gap">
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Pre-operative</p><h2>Checklist</h2></div><span class="count-badge ${s.preop_pending.length ? "warning" : ""}">${s.preop_done}/${s.preop_total}</span></div>
        <form id="sxChecklist" class="sx-checklist">${Object.entries(checks).map(([k, label]) => `<label class="confirm-check"><input type="checkbox" name="${k}" ${pre.checks?.[k] ? "checked" : ""} ${closed ? "disabled" : ""} /><span>${esc(label)}</span></label>`).join("")}
          ${closed ? "" : '<div class="modal-actions"><button class="button secondary small" type="submit">Save checklist</button></div>'}</form>
      </article>
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Work-up</p><h2>Fitness &amp; instructions</h2></div></div>
        ${sxDl([["ASA grade", pre.asa], ["Comorbidities", pre.comorbidities], ["Investigations", pre.investigations], ["Nothing by mouth from", pre.fasting_from], ["Special instructions", pre.special_instructions]])}
        ${c.notes ? `<p class="panel-note">${esc(c.notes)}</p>` : ""}
      </article>
      <article class="panel span-full"><div class="panel-heading"><div><p class="eyebrow">Audit trail</p><h2>Case history</h2></div></div>
        <div class="rt-events">${[...W.events].reverse().map(e => `<div class="rt-event"><span class="muted small">${fmtDate(e.event_date)}</span><span>${esc(e.text)}</span><span class="muted small">${esc(e.author)}</span></div>`).join("")}</div>
      </article>
    </div>`;
}
function renderSxOperation() {
  const W = S.SX, c = W.case, op = c.operation;
  $("#sxoperationActions").innerHTML = op && c.status !== "cancelled" ? `<button class="button secondary" data-sx="operation">Amend note</button>` : "";
  if (!op) {
    $("#sxoperation").innerHTML = c.status === "cancelled" ? '<div class="queue-empty">This surgery was cancelled.</div>'
      : `<article class="panel"><div class="queue-empty">No operation note yet.${W.summary.preop_pending.length ? ` Pre-op checklist has ${W.summary.preop_pending.length} open item(s).` : ""}<br><button class="button primary top-gap" data-sx="operation">Write operation note</button></div></article>`;
    return;
  }
  $("#sxoperation").innerHTML = `<article class="panel op-note">
      <div class="panel-heading"><div><p class="eyebrow">Operation note · ${fmtDate(op.operation_date)}</p><h2>${esc(op.procedure_performed)}</h2></div><span class="muted small">Signed by ${esc(op.signed_by)} · ${fmtDateTime(op.signed_at)}</span></div>
      ${sxDl([["Approach", op.approach], ["Anaesthesia", op.anaesthesia], ["Surgeon", op.surgeon], ["Assistants", op.assistants], ["Anaesthetist", op.anaesthetist],
        ["Duration", op.duration_min != null ? `${op.duration_min} min` : null], ["Blood loss", op.ebl_ml != null ? `${op.ebl_ml} ml` : null], ["Transfusion", op.transfusion_units != null ? `${op.transfusion_units} unit(s)` : null],
        ["Drains", op.drains], ["Incision", op.incision]])}
      ${[["Findings", op.findings], ["Procedure details", op.procedure_details], ["Frozen section", op.frozen_section], ["Specimens sent", op.specimens], ["Intra-operative events", op.intraop_events]]
        .filter(([, v]) => v).map(([k, v]) => `<h3 class="op-h">${esc(k)}</h3><p class="op-p">${esc(v)}</p>`).join("")}
    </article>`;
}
function renderSxPostop() {
  const W = S.SX, c = W.case, s = W.summary;
  const notes = W.events.filter(e => !["plan", "status", "operation", "pathology"].includes(e.kind));
  $("#sxpostopActions").innerHTML = c.operation ? `<button class="button primary" data-sx="add-note">+ Post-op note</button>` : "";
  $("#sxpostop").innerHTML = !c.operation ? '<div class="queue-empty">Record the operation note first.</div>' : `
    <div class="metric-grid">
      <article class="metric-card"><div><small>Post-operative day</small><strong>${s.pod ?? "—"}</strong><p>${c.status === "discharged" ? `discharged ${fmtShort(c.discharge_date)}` : "in hospital"}</p></div><span class="metric-dot">◷</span></article>
      <article class="metric-card"><div><small>Length of stay</small><strong>${s.length_of_stay ?? "—"}${s.length_of_stay != null ? " d" : ""}</strong><p>admission to discharge</p></div><span class="metric-dot">⌂</span></article>
      <article class="metric-card"><div><small>Complications</small><strong>${s.complications}</strong><p>${s.worst_clavien_dindo ? `worst Clavien–Dindo ${s.worst_clavien_dindo}` : "none recorded"}</p></div><span class="metric-dot ${s.worst_clavien_dindo ? (["I", "II"].includes(s.worst_clavien_dindo) ? "pending" : "alert") : ""}">!</span></article>
      <article class="metric-card"><div><small>Drains</small><strong class="small-strong">${esc((c.discharge || {}).drains_at_discharge || (c.operation || {}).drains || "None")}</strong><p>${notes.filter(n => n.kind === "drain").length ? "drain notes recorded" : ""}</p></div><span class="metric-dot">⌇</span></article>
    </div>
    <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Day by day</p><h2>Post-operative notes</h2></div><div class="legend-inline"><span>${cdBadge("I")} Clavien–Dindo grade</span></div></div>
      ${notes.length ? notes.map(e => `<div class="postop-note ${e.clavien_dindo ? "complication" : ""}">
          <div class="pod">${e.pod != null ? `POD ${e.pod}` : ""}<small>${fmtShort(e.event_date)}</small></div>
          <div><strong>${esc(e.kind_label)}${e.complication ? `: ${esc(e.complication)}` : ""}</strong> ${cdBadge(e.clavien_dindo)}
            <p>${esc(e.text)}</p><span class="muted small">${[e.drain_output_ml != null ? `Drain ${fmtNum(e.drain_output_ml)} ml` : "", e.pain_score != null ? `Pain ${e.pain_score}/10` : "", e.author].filter(Boolean).map(esc).join(" · ")}</span></div>
          <div>${["discharge"].includes(e.kind) ? "" : `<button class="icon-action danger" data-sx="delete-note" data-id="${e.id}" aria-label="Delete note">🗑</button>`}</div>
        </div>`).join("") : '<div class="queue-empty">No post-operative notes yet.</div>'}
    </article>
    <details class="panel top-gap cd-legend"><summary><strong>Clavien–Dindo classification</strong></summary>${Object.entries(W.options.clavien_dindo).map(([g, d]) => `<p>${cdBadge(g)} ${esc(d)}</p>`).join("")}</details>`;
}
function renderSxPathology() {
  const W = S.SX, c = W.case, p = c.pathology, d = c.discharge;
  $("#sxpathologyActions").innerHTML = c.operation ? `<button class="button secondary" data-sx="pathology">${p ? "Amend histopathology" : "Enter histopathology"}</button>${c.status === "completed" ? '<button class="button primary" data-sx="discharge">Discharge</button>' : ""}` : "";
  $("#sxpathology").innerHTML = !c.operation ? '<div class="queue-empty">Record the operation note first.</div>' : `
    <div class="patient-profile-grid">
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Final histopathology${p?.report_date ? ` · ${fmtDate(p.report_date)}` : ""}</p><h2>${p ? esc(p.histology) : "Awaited"}</h2></div>${W.summary.stage ? `<span class="chip">${esc(W.summary.stage)}</span>` : ""}</div>
        ${p ? sxDl([["Grade", p.grade], ["Tumour size", p.size_mm != null ? `${p.size_mm} mm` : null], ["pT / pN", W.summary.stage], ["Margins", p.margins ? `${p.margins}${p.closest_margin_mm != null ? ` (closest ${p.closest_margin_mm} mm)` : ""}` : null],
          ["Lymph nodes", p.nodes_examined != null ? `${p.nodes_positive ?? 0} positive of ${p.nodes_examined}` : null], ["LVI", p.lvi], ["PNI", p.pni], ["Biomarkers", p.biomarkers], ["Comments", p.comments]]) : '<p class="muted">Enter the final report when it is available.</p>'}
      </article>
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Discharge${d ? ` · ${fmtDate(c.discharge_date)}` : ""}</p><h2>${d ? "Discharged" : c.status === "completed" ? "In hospital" : "—"}</h2></div></div>
        ${d ? sxDl([["Condition", d.condition], ["Drains", d.drains_at_discharge], ["Wound care", d.wound_care], ["Suture removal", d.suture_removal_date ? fmtDate(d.suture_removal_date) : null],
          ["Follow-up", d.follow_up_date ? fmtDate(d.follow_up_date) : null], ["Instructions", d.instructions], ["Signed", `${d.signed_by} · ${fmtDate(d.signed_at)}`]]) : '<p class="muted">Not discharged yet.</p>'}
        ${d ? `<div class="modal-actions"><button class="button primary" data-rec="new-discharge">Create discharge summary</button></div>` : ""}
      </article>
    </div>`;
}

/* ------------------------------------------------------------------ dialogs */
function sxDialog({ eyebrow = "Surgical oncology", title, sub = "", body, submit = "Save", onSubmit }) {
  $("#sxDialogEyebrow").textContent = eyebrow;
  $("#sxDialogTitle").textContent = title;
  $("#sxDialogSub").textContent = sub;
  $("#sxDialogBody").innerHTML = body;
  $("#sxDialogSubmit").textContent = submit;
  formError($("#sxDialogForm"), "");
  S.sxSubmit = onSubmit;
  openModal("sxDialog");
}
const fld = (label, name, value = "", attrs = "", cls = "") => `<label class="field ${cls}"><span>${label}</span><input name="${name}" value="${esc(value ?? "")}" ${attrs} /></label>`;
const area = (label, name, value = "", rows = 2, cls = "span-3") => `<label class="field ${cls}"><span>${label}</span><textarea name="${name}" rows="${rows}" maxlength="3000">${esc(value ?? "")}</textarea></label>`;
const sel = (label, name, options, value = "", cls = "") => `<label class="field ${cls}"><span>${label}</span><select name="${name}"><option value="">—</option>${options.map(([v, l]) => `<option value="${esc(v)}" ${String(value ?? "") === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;

async function openCase(edit = false) {
  const o = await sxOptions();
  const c = edit ? S.SX.case : null;
  const pre = c?.preop || {};
  sxDialog({ eyebrow: "Surgical oncology · case sheet", title: edit ? "Edit surgical plan" : "New surgery", sub: patientName(S.patient), submit: edit ? "Save changes" : "Create case",
    body: `<div class="form-grid three">
        ${fld("Procedure planned", "procedure_planned", c?.procedure_planned, 'maxlength="200" required placeholder="e.g. Right modified radical mastectomy"', "span-2")}
        ${sel("Intent", "intent", Object.entries(o.intents), c?.intent)}
        ${fld("Indication", "indication", c?.indication ?? S.patient?.diagnosis, 'maxlength="300"', "span-2")}
        ${sel("Urgency", "urgency", Object.entries(o.urgency), c?.urgency || "elective")}
        ${fld("Site", "site", c?.site, 'maxlength="160"')}
        ${sel("Laterality", "laterality", [["right", "Right"], ["left", "Left"], ["bilateral", "Bilateral"], ["midline", "Midline"]], c?.laterality)}
        ${fld("Surgeon", "surgeon", c?.surgeon ?? clinicianName(), 'maxlength="80"')}
        ${fld("Admission date", "admission_date", c?.admission_date, 'type="date"')}
        ${fld("Surgery date", "surgery_date", c?.surgery_date, 'type="date"')}
        ${sel("ASA grade", "asa", ["I", "II", "III", "IV", "V"].map(x => [x, `ASA ${x}`]), pre.asa)}
      </div>
      <fieldset class="sx-checks"><legend>Pre-op checklist</legend>${Object.entries(o.preop_checks).map(([k, l]) => `<label class="confirm-check"><input type="checkbox" name="chk_${k}" ${pre.checks?.[k] ? "checked" : ""} /><span>${esc(l)}</span></label>`).join("")}</fieldset>
      <div class="form-grid three">
        ${area("Comorbidities", "comorbidities", pre.comorbidities, 2, "span-3")}
        ${area("Pre-op investigations", "investigations", pre.investigations, 2, "span-3")}
        ${fld("Nothing by mouth from", "fasting_from", pre.fasting_from, 'maxlength="100" placeholder="e.g. 10 pm the night before"')}
        ${fld("Special instructions for the patient", "special_instructions", pre.special_instructions, 'maxlength="300"', "span-2")}
        ${area("Notes", "notes", c?.notes, 2, "span-3")}
      </div>
      ${waTick(S.patient, "Send the surgery date and preparation to the patient on WhatsApp (with a reminder before admission)")}`,
    onSubmit: async f => {
      const checks = Object.fromEntries(Object.keys(o.preop_checks).map(k => [k, f[`chk_${k}`].checked]));
      const body = { procedure_planned: f.procedure_planned.value, intent: f.intent.value || null, indication: f.indication.value, urgency: f.urgency.value || "elective",
        site: f.site.value, laterality: f.laterality.value || null, surgeon: f.surgeon.value, admission_date: f.admission_date.value || null, surgery_date: f.surgery_date.value || null,
        notes: f.notes.value, notify_whatsapp: f.notify_whatsapp.checked,
        preop: { checks, asa: f.asa.value || null, comorbidities: f.comorbidities.value, investigations: f.investigations.value, fasting_from: f.fasting_from.value, special_instructions: f.special_instructions.value } };
      if (!body.procedure_planned.trim()) throw new Error("Enter the planned procedure.");
      const data = edit ? await patch(`/api/surgeries/${S.SX.case.id}`, body) : await post(`/api/patients/${S.patient.id}/surgeries`, body);
      setSx(data);
      showToast(`${edit ? "Surgical plan updated." : "Surgery added to the patient's plans."}${waResultText(data.whatsapp)}`);
      if (!edit) navigate(`#surgery/${data.case.id}/plan`); else await loadPatient(data.patient.id);
    } });
}
function openOperation() {
  const o = S.sxOptions, c = S.SX.case, op = c.operation || {};
  sxDialog({ eyebrow: "Operation note", title: op.procedure_performed ? "Amend operation note" : "Operation note", sub: `${patientName(S.patient)} · planned: ${c.procedure_planned}`, submit: "Save and sign",
    body: `<div class="form-grid three">
        ${fld("Date of operation", "operation_date", op.operation_date || c.surgery_date || todayISO(), `type="date" max="${todayISO()}" required`)}
        ${fld("Procedure performed", "procedure_performed", op.procedure_performed || c.procedure_planned, 'maxlength="300" required', "span-2")}
        ${sel("Approach", "approach", o.approaches.map(x => [x, x]), op.approach)}
        ${sel("Anaesthesia", "anaesthesia", o.anaesthesia.map(x => [x, x]), op.anaesthesia)}
        ${fld("Duration (min)", "duration_min", op.duration_min, 'type="number" min="0"')}
        ${fld("Surgeon", "surgeon", op.surgeon || c.surgeon || clinicianName(), 'maxlength="80" required')}
        ${fld("Assistants", "assistants", op.assistants, 'maxlength="160"')}
        ${fld("Anaesthetist", "anaesthetist", op.anaesthetist, 'maxlength="80"')}
        ${fld("Incision", "incision", op.incision, 'maxlength="300"', "span-3")}
        ${area("Findings", "findings", op.findings)}
        ${area("Procedure details", "procedure_details", op.procedure_details, 4)}
        ${area("Frozen section", "frozen_section", op.frozen_section)}
        ${area("Specimens sent for histopathology", "specimens", op.specimens)}
        ${fld("Blood loss (ml)", "ebl_ml", op.ebl_ml, 'type="number" min="0"')}
        ${fld("Transfusion (units)", "transfusion_units", op.transfusion_units, 'type="number" min="0"')}
        ${fld("Drains", "drains", op.drains, 'maxlength="200"')}
        ${area("Intra-operative events / complications", "intraop_events", op.intraop_events)}
      </div>
      <label class="confirm-check"><input type="checkbox" name="signed" /><span>I performed or supervised this operation and sign this note.</span></label>`,
    onSubmit: async f => {
      const body = Object.fromEntries(["operation_date", "procedure_performed", "approach", "anaesthesia", "duration_min", "surgeon", "assistants", "anaesthetist", "incision", "findings",
        "procedure_details", "frozen_section", "specimens", "ebl_ml", "transfusion_units", "drains", "intraop_events"].map(k => [k, f[k].value]));
      body.signed = f.signed.checked;
      if (!body.signed) throw new Error("Tick the box to sign the operation note.");
      await sxCall("POST", `/api/surgeries/${S.SX.case.id}/operation`, body);
      showToast("Operation note signed.");
      navigate(`#surgery/${S.SX.case.id}/operation`);
    } });
}
function openSxNote() {
  const o = S.sxOptions;
  sxDialog({ eyebrow: "Post-operative course", title: "Add a post-op note", sub: patientName(S.patient), submit: "Save note",
    body: `<div class="form-grid three">
        ${fld("Date", "event_date", todayISO(), `type="date" max="${todayISO()}"`)}
        ${sel("Type", "kind", Object.entries(o.event_kinds), "progress")}
        ${fld("Pain score (0–10)", "pain_score", "", 'type="number" min="0" max="10"')}
        ${area("Note", "text", "", 4)}
        ${fld("Drain output (ml / 24 h)", "drain_output_ml", "", 'type="number" min="0"')}
        ${fld("Complication", "complication", "", 'maxlength="200" placeholder="e.g. seroma, surgical site infection"')}
        ${sel("Clavien–Dindo grade", "clavien_dindo", Object.keys(o.clavien_dindo).map(g => [g, `Grade ${g}`]), "")}
      </div>
      <p class="muted small" id="cdHelp">Complications, returns to theatre and readmissions need a Clavien–Dindo grade.</p>`,
    onSubmit: async f => {
      await sxCall("POST", `/api/surgeries/${S.SX.case.id}/events`, Object.fromEntries(["event_date", "kind", "pain_score", "text", "drain_output_ml", "complication", "clavien_dindo"].map(k => [k, f[k].value || null])));
      showToast("Post-op note saved.");
    } });
}
function openPathology() {
  const p = S.SX.case.pathology || {};
  sxDialog({ eyebrow: "Histopathology", title: "Final histopathology", sub: patientName(S.patient), submit: "Save",
    body: `<div class="form-grid three">
        ${fld("Report date", "report_date", p.report_date || todayISO(), `type="date" max="${todayISO()}"`)}
        ${fld("Histology", "histology", p.histology, 'maxlength="200" required', "span-2")}
        ${fld("Grade", "grade", p.grade, 'maxlength="40"')}
        ${fld("Tumour size (mm)", "size_mm", p.size_mm, 'type="number" min="0" step="any"')}
        ${sel("Margins", "margins", [["negative", "Negative"], ["close", "Close"], ["positive", "Positive"]], p.margins)}
        ${fld("pT", "pt", p.pt, 'maxlength="20" placeholder="e.g. pT2"')}
        ${fld("pN", "pn", p.pn, 'maxlength="20" placeholder="e.g. pN1a"')}
        ${fld("Closest margin (mm)", "closest_margin_mm", p.closest_margin_mm, 'type="number" min="0" step="any"')}
        ${fld("Nodes examined", "nodes_examined", p.nodes_examined, 'type="number" min="0"')}
        ${fld("Nodes positive", "nodes_positive", p.nodes_positive, 'type="number" min="0"')}
        ${sel("Lymphovascular invasion", "lvi", [["absent", "Absent"], ["present", "Present"], ["indeterminate", "Indeterminate"]], p.lvi)}
        ${sel("Perineural invasion", "pni", [["absent", "Absent"], ["present", "Present"]], p.pni)}
        ${fld("Biomarkers", "biomarkers", p.biomarkers, 'maxlength="300" placeholder="e.g. ER+, PR+, HER2−, Ki-67 18%"', "span-2")}
        ${area("Comments", "comments", p.comments)}
      </div>`,
    onSubmit: async f => {
      await sxCall("POST", `/api/surgeries/${S.SX.case.id}/pathology`, Object.fromEntries(["report_date", "histology", "grade", "size_mm", "margins", "pt", "pn", "closest_margin_mm",
        "nodes_examined", "nodes_positive", "lvi", "pni", "biomarkers", "comments"].map(k => [k, f[k].value || null])));
      showToast("Histopathology saved.");
    } });
}
function openDischarge() {
  const c = S.SX.case;
  sxDialog({ eyebrow: "Discharge after surgery", title: "Discharge", sub: `${patientName(S.patient)} · ${c.operation?.procedure_performed || c.procedure_planned}`, submit: "Discharge and sign",
    body: `<div class="form-grid three">
        ${fld("Discharge date", "discharge_date", todayISO(), `type="date" max="${todayISO()}"`)}
        ${fld("Suture / staple removal", "suture_removal_date", "", 'type="date"')}
        ${fld("Follow-up visit", "follow_up_date", "", 'type="date"')}
        ${area("Condition at discharge", "condition", "", 2)}
        ${fld("Drains at discharge", "drains_at_discharge", "", 'maxlength="200" placeholder="None, or e.g. axillary drain in situ"', "span-3")}
        ${area("Wound care", "wound_care", "", 2)}
        ${area("Instructions", "instructions", "", 2)}
      </div>
      ${waTick(S.patient, "Send a reminder for the follow-up visit on WhatsApp")}`,
    onSubmit: async f => {
      const data = await sxCall("POST", `/api/surgeries/${S.SX.case.id}/discharge`, { ...Object.fromEntries(["discharge_date", "suture_removal_date", "follow_up_date", "condition", "drains_at_discharge", "wound_care", "instructions"].map(k => [k, f[k].value || null])), notify_whatsapp: f.notify_whatsapp.checked });
      showToast(`Discharged.${waResultText(data.whatsapp)}`);
    } });
}

/* ------------------------------------------------------------------ events */
document.addEventListener("click", async event => {
  const el = event.target.closest("[data-sx]");
  if (!el) return;
  try {
    switch (el.dataset.sx) {
      case "new-case": closeModals(); return openCase(false);
      case "edit-case": return openCase(true);
      case "operation": await sxOptions(); return openOperation();
      case "add-note": await sxOptions(); return openSxNote();
      case "pathology": return openPathology();
      case "discharge": return openDischarge();
      case "cancel":
        return sxDialog({ title: "Cancel this surgery?", submit: "Cancel surgery", body: `<label class="field"><span>Reason</span><textarea name="reason" rows="2" maxlength="500" required></textarea></label>${waTick(S.patient, "Tell the patient on WhatsApp")}`,
          onSubmit: async f => { const d = await sxCall("PATCH", `/api/surgeries/${S.SX.case.id}`, { status: "cancelled", cancel_reason: f.reason.value, notify_whatsapp: f.notify_whatsapp.checked }); showToast(`Surgery cancelled.${waResultText(d.whatsapp)}`); } });
      case "replan": await sxCall("PATCH", `/api/surgeries/${S.SX.case.id}`, { status: "planned" }); return showToast("Surgery re-planned. Set the new date.");
      case "delete":
        return confirmDialog(`Delete ${S.SX.case.procedure_planned}?`, "The surgical plan, operation note, post-op notes, histopathology and discharge record will be removed.", async () => {
          const pid = S.SX.patient.id; await del(`/api/surgeries/${S.SX.case.id}`); S.SX = null; showToast("Surgical case deleted."); navigate(`#patient/${pid}`);
        });
      case "delete-note":
        return confirmDialog("Delete this note?", "The post-operative note will be removed.", async () => { await sxCall("DELETE", `/api/surgery-events/${el.dataset.id}`); showToast("Note deleted."); }, "Delete");
    }
  } catch (error) { showToast(error.message); }
});
document.addEventListener("submit", async event => {
  if (event.target.id !== "sxChecklist") return;
  event.preventDefault();
  const checks = Object.fromEntries($$("input[type=checkbox]", event.target).map(i => [i.name, i.checked]));
  try { await sxCall("PATCH", `/api/surgeries/${S.SX.case.id}`, { preop: { ...S.SX.case.preop, checks } }); showToast("Checklist saved."); }
  catch (e) { showToast(e.message); }
});
$("#sxDialogForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  await withBusy(form, async () => { await S.sxSubmit(form.elements); closeModals(); });
});

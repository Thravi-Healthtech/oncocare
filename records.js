"use strict";
/* Running summary, clinical notes, uploaded reports and discharge / end-of-treatment summaries. */

const KIND_META = {
  chemo: ["Chemo", "kind-chemo"], rt: ["Radiotherapy", "kind-rt"], rt_review: ["RT review", "kind-rt"], toxicity: ["CTCAE", "kind-tox"], labs: ["Labs", "kind-labs"],
  complaint: ["Patient", "kind-patient"], note: ["Note", "kind-note"], document: ["Report", "kind-doc"], discharge: ["Summary", "kind-note"],
};
const NOTE_CATEGORIES = { progress: "Progress note", procedure: "Procedure", admission: "Admission", phone: "Phone call", counselling: "Counselling", tumour_board: "Tumour board", other: "Other" };
const DOC_TYPES = { imaging: "Imaging report", pathology: "Histopathology", lab: "Lab report", discharge: "Previous discharge summary", referral: "Referral letter", other: "Other report" };
const DS_STATUS = { draft: ["Draft", "planned"], final: ["Final", "complete"], superseded: ["Superseded", "neutral"] };
const METHOD_LABEL = { claude: "AI draft (Claude)", rules: "Rule-based draft", edited: "Edited by clinician" };

/* ------------------------------------------------------------------ routes */
EXT.routes.summary = async id => {
  if (!id) return null;
  if (!S.patient || S.patient.id !== id) await loadPatient(id);
  await loadSummary(id);
  return "summary";
};
EXT.routes.discharge = async id => {
  if (!id) return null;
  S.ds = await api(`/api/discharge-summaries/${id}`);
  if (!S.patient || S.patient.id !== S.ds.patient_id) await loadPatient(S.ds.patient_id);
  renderDischarge();
  return "discharge";
};
EXT.navHref.summary = () => (S.patient ? `#summary/${S.patient.id}` : "#");
EXT.afterShow.push(view => { if (view === "discharge") $("#navPatient").classList.remove("hidden"); });
EXT.patientLoaders.push(async p => { p.discharge_summaries = await api(`/api/patients/${p.id}/discharge-summaries`).catch(() => []); });
EXT.patientPanels.push(p => {
  const list = p.discharge_summaries || [];
  return `<article class="panel span-full"><div class="panel-heading"><div><p class="eyebrow">Running summary &amp; documents</p><h2>Summaries</h2></div>
      <div class="heading-actions"><a class="button secondary" href="#summary/${p.id}">Open running summary</a><button class="button primary" data-rec="new-discharge">Create discharge summary</button></div></div>
    ${list.length ? list.map(d => `<div class="plan-row"><div class="plan-row-main"><strong>${esc(d.title)}</strong>
        <small>${esc(d.kind_label)} · v${d.version} · ${fmtDate(d.period_from)} to ${fmtDate(d.period_to)} · ${d.status === "final" ? `finalised by ${esc(d.finalised_by)} ${fmtDate(d.finalised_at)}` : `drafted ${fmtDate(d.created_at)}`}</small></div>
        <div>${dsPill(d.status)}${d.status === "draft" && d.unsupported ? ` <span class="chip chip-warn">${d.unsupported} sentence(s) to check</span>` : ""}</div>
        <div class="plan-row-actions"><a class="button secondary" href="#discharge/${d.id}">${d.status === "draft" ? "Review draft" : "Open"}</a></div></div>`).join("")
      : '<div class="queue-empty">No discharge summary yet. Create a draft from the record, review it, then finalise and share.</div>'}
  </article>`;
});
function dsPill(status) { const [l, t] = DS_STATUS[status] || [status, "neutral"]; return `<span class="status-pill ${t}">${esc(l)}</span>`; }

/* ------------------------------------------------------------------ running summary */
async function loadSummary(id) {
  const [summary, notes, docs] = await Promise.all([api(`/api/patients/${id}/summary`), api(`/api/patients/${id}/notes`), api(`/api/patients/${id}/documents`)]);
  S.summary = { ...summary, notes, docs };
  S.summaryFilter = S.summaryFilter || "all";
  renderSummary();
}
function citationHtml(sentences, sourcesById) {
  return sentences.map(s => {
    const text = esc(s.text.replace(/\s*(\[S\d+\])+/g, "").trim());
    const chips = s.sources.map(id => `<button class="cite" data-cite="${id}" title="${esc(sourcesById.get(id)?.title || (id === "S0" ? "Patient record" : id))}">${id}</button>`).join("");
    return `<span class="sentence ${s.unsupported ? "unsupported" : ""}" ${s.unsupported ? 'title="No source cited: check this sentence against the record"' : ""}>${text}${chips}</span>`;
  }).join(" ");
}
function snapshotHtml(d) {
  const chemo = d.chemotherapy.filter(c => c.status === "active" || c.next_cycle);
  const rt = d.radiotherapy.filter(c => ["active", "on_hold", "planned"].includes(c.status));
  const tox = d.toxicities.slice(0, 6);
  const abnormal = d.labs.filter(l => l.flag);
  return `<div class="snapshot-grid">
    <article class="panel snap"><p class="eyebrow">On treatment</p>
      ${rt.map(c => `<div class="snap-line"><strong>Radiotherapy · ${esc(c.site)}</strong><span>${c.fractions_delivered}/${c.fractions_planned} fractions · ${gy(c.dose_delivered_cgy)} of ${gy(c.dose_planned_cgy)}</span><span class="muted small">${c.next_fraction_date ? `next ${fmtShort(c.next_fraction_date)} · ends ${fmtShort(c.projected_end_date)}` : esc(c.status.replace("_", " "))}</span><a class="text-button" href="#rt/${c.course_id}/plan">Open</a></div>`).join("")}
      ${chemo.map(c => `<div class="snap-line"><strong>${esc(c.regimen)}</strong><span>${c.cycles_given}/${c.cycles_planned} cycles given</span><span class="muted small">${c.next_cycle ? `next C${c.next_cycle.number} ${fmtShort(c.next_cycle.date)}` : ""}</span><a class="text-button" href="#plan/${c.plan_id}/cycles">Open</a></div>`).join("")}
      ${!rt.length && !chemo.length ? '<p class="muted">No active treatment.</p>' : ""}
    </article>
    <article class="panel snap"><p class="eyebrow">Toxicities (worst grade)</p>
      ${tox.length ? tox.map(t => `<div class="snap-tox">${gradeBadge(t.worst_grade, "small")}<span>${esc(t.term)}</span><span class="muted small">${fmtShort(t.date)} · ${esc(t.source)}</span></div>`).join("") : '<p class="muted">None recorded.</p>'}
    </article>
    <article class="panel snap"><p class="eyebrow">Messages &amp; labs</p>
      <div class="snap-line"><strong>${d.open_messages.count} message(s) awaiting review</strong>${d.open_messages.urgent ? `<span class="chip chip-alert">${d.open_messages.urgent} with red flags</span>` : ""}<a class="text-button" href="#inbox">Inbox</a></div>
      <div class="snap-line"><strong>Labs outside range</strong><span>${abnormal.length ? abnormal.map(l => `${esc(l.test_name)} ${fmtNum(l.value)}${l.flag === "high" ? "↑" : "↓"}`).join(", ") : "None in latest verified values"}</span></div>
      ${d.next_reminder ? `<div class="snap-line"><strong>Next reminder</strong><span>${fmtDateTime(d.next_reminder.scheduled_for)}</span></div>` : ""}
    </article>
  </div>`;
}
function renderSummary() {
  const d = S.summary;
  const p = d.patient;
  $("#summaryTitle").textContent = `${p.first_name} ${p.last_name}`;
  $("#summarySubtitle").textContent = `${p.medical_record_number}${p.age != null ? ` · ${p.age} y` : ""}${p.sex ? ` · ${p.sex}` : ""} · ${p.diagnosis || "No diagnosis recorded"}`;
  const n = d.narrative;
  const byId = new Map(d.items.map(i => [i.id, i]));
  const narrativeSources = n ? new Map(n.sources.map(s => [s.id, s])) : byId;
  const filters = ["all", "rt", "chemo", "toxicity", "complaint", "labs", "note", "document"];
  const items = [...d.items].reverse().filter(i => S.summaryFilter === "all" || i.kind === S.summaryFilter || (S.summaryFilter === "rt" && i.kind === "rt_review"));
  let lastDate = "";
  $("#summaryContent").innerHTML = `
    ${snapshotHtml(d)}
    <div class="summary-layout top-gap">
      <div class="summary-main">
        <article class="panel narrative-panel"><div class="panel-heading"><div><p class="eyebrow">Summary</p><h2>Course so far</h2></div>
            <button class="button ${n ? "secondary" : "primary"} small" data-rec="generate">${n ? "Regenerate" : "Generate summary"}</button></div>
          ${n ? `<p class="narrative">${citationHtml(n.sentences, narrativeSources)}</p>
            <p class="muted small">${esc(METHOD_LABEL[n.method] || n.method)}${n.model ? ` · ${esc(n.model)}` : ""} · ${fmtDateTime(n.generated_at)} by ${esc(n.generated_by || "")}${n.new_items ? ` · <strong class="text-amber">${n.new_items} new item(s) since — regenerate to include them</strong>` : ""}${n.unsupported ? ` · <span class="text-amber">${n.unsupported} sentence(s) without a source</span>` : ""}</p>
            ${n.error ? `<p class="ai-error">${esc(n.error)}</p>` : ""}`
          : `<p class="muted">Write a short summary from the ${d.items.length} items below. ${d.ai_enabled ? "Claude drafts it from these sources only" : "AI is off, so fixed rules write it"}; every sentence links to its source.</p>`}
          <p class="muted small">Drafted from the record for clinician review. Click a source number to see the entry.</p>
        </article>
        <article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Sources</p><h2>Timeline</h2></div><span class="muted">${d.items.length} items</span></div>
          <div class="filter-chips">${filters.map(f => `<button class="chip ${S.summaryFilter === f ? "chip-teal" : ""}" data-rec="filter" data-filter="${f}">${f === "all" ? "All" : (KIND_META[f]?.[0] || f)}</button>`).join("")}</div>
          <div class="source-timeline">${items.map(i => {
            const head = i.date !== lastDate ? `<h3 class="source-date">${fmtDate(i.date)}</h3>` : "";
            lastDate = i.date;
            const [label, cls] = KIND_META[i.kind] || [i.kind, ""];
            return `${head}<div class="source-item ${i.severity ? `sev-${i.severity}` : ""}" id="src-${i.id}"><span class="source-id">${i.id}</span><span class="kind-tag ${cls}">${label}</span>
              <div><strong>${esc(i.title)}</strong><p>${esc(i.detail)}</p></div>${sourceLink(i)}</div>`;
          }).join("") || '<div class="queue-empty">Nothing recorded yet.</div>'}</div>
        </article>
      </div>
      <aside class="summary-side">
        <article class="panel"><div class="panel-heading"><div><p class="eyebrow">In between visits</p><h2>Clinical notes</h2></div><button class="button secondary small" data-rec="add-note">+ Note</button></div>
          ${d.notes.slice(0, 8).map(nt => `<div class="note-item"><div><strong>${esc(NOTE_CATEGORIES[nt.category] || nt.category)}</strong> <span class="muted small">${fmtDate(nt.note_date)} · ${esc(nt.author)}</span></div><p>${esc(nt.text)}</p>
            <div class="note-actions"><button class="text-button" data-rec="edit-note" data-id="${nt.id}">Edit</button><button class="text-button danger-text" data-rec="delete-note" data-id="${nt.id}">Delete</button></div></div>`).join("") || '<p class="muted">No notes yet.</p>'}
        </article>
        <article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Uploaded</p><h2>Reports</h2></div><button class="button secondary small" data-rec="upload-doc">Upload</button></div>
          ${d.docs.map(doc => `<div class="note-item"><div><strong>${esc(doc.title)}</strong> <span class="muted small">${esc(doc.doc_type_label)} · ${fmtDate(doc.doc_date)}</span></div>
            ${doc.ai_summary ? `<p>${esc(doc.ai_summary)}</p><p class="muted small">${doc.summary_method === "claude" ? "AI summary of the report — check against the file" : doc.summary_method === "edited" ? "Summary edited by clinician" : ""}</p>` : doc.text_excerpt ? `<p class="muted">${esc(doc.text_excerpt.slice(0, 220))}…</p>` : '<p class="muted">No text extracted.</p>'}
            <div class="note-actions"><a class="text-button" href="/api/documents/${doc.id}/file" target="_blank" rel="noopener">Open file</a>${d.ai_enabled ? `<button class="text-button" data-rec="summarise-doc" data-id="${doc.id}">${doc.ai_summary ? "Re-summarise" : "Summarise with AI"}</button>` : ""}<button class="text-button danger-text" data-rec="delete-doc" data-id="${doc.id}">Delete</button></div></div>`).join("") || '<p class="muted">No reports uploaded.</p>'}
        </article>
        <article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Discharge</p><h2>Summaries</h2></div><button class="button primary small" data-rec="new-discharge">Create</button></div>
          ${(S.patient.discharge_summaries || []).map(ds => `<div class="note-item"><div><a href="#discharge/${ds.id}"><strong>${esc(ds.title)}</strong></a> ${dsPill(ds.status)}</div><p class="muted small">v${ds.version} · ${fmtDate(ds.period_from)} to ${fmtDate(ds.period_to)}</p></div>`).join("") || '<p class="muted">None yet.</p>'}
        </article>
      </aside>
    </div>`;
}
function sourceLink(i) {
  const r = i.ref || {};
  const href = r.type === "cycle" ? `#plan/${r.plan_id}/cycles` : r.type === "rt_course" ? `#rt/${r.id}/daily` : r.type === "rt_review" ? `#rt/${r.course_id}/weekly`
    : r.type === "report" ? "#inbox" : r.type === "document" ? `/api/documents/${r.id}/file` : r.type === "discharge" ? `#discharge/${r.id}` : null;
  return href ? `<a class="text-button" href="${href}" ${r.type === "document" ? 'target="_blank" rel="noopener"' : ""}>Open</a>` : "<span></span>";
}

/* ------------------------------------------------------------------ dialogs */
function recDialog({ eyebrow = "Patient record", title, sub = "", body, submit = "Save", onSubmit }) {
  $("#recDialogEyebrow").textContent = eyebrow;
  $("#recDialogTitle").textContent = title;
  $("#recDialogSub").textContent = sub;
  $("#recDialogBody").innerHTML = body;
  $("#recDialogSubmit").textContent = submit;
  formError($("#recDialogForm"), "");
  S.recSubmit = onSubmit;
  openModal("recDialog");
}
async function refreshRecords() {
  await loadPatient(S.patient.id);
  if (S.view === "summary") await loadSummary(S.patient.id);
}
function openNote(note = null) {
  recDialog({ eyebrow: "Clinical note", title: note ? "Edit note" : "Add a clinical note", sub: patientName(S.patient), submit: "Save note",
    body: `<div class="form-grid"><label class="field"><span>Date</span><input type="date" name="note_date" max="${todayISO()}" value="${note?.note_date || todayISO()}" /></label>
      <label class="field"><span>Type</span><select name="category">${Object.entries(NOTE_CATEGORIES).map(([k, v]) => `<option value="${k}" ${note?.category === k ? "selected" : ""}>${v}</option>`).join("")}</select></label></div>
      <label class="field"><span>Note</span><textarea name="text" rows="6" maxlength="4000" required>${esc(note?.text || "")}</textarea></label>`,
    onSubmit: async f => {
      const body = { note_date: f.note_date.value, category: f.category.value, text: f.text.value };
      if (note) await patch(`/api/notes/${note.id}`, body); else await post(`/api/patients/${S.patient.id}/notes`, body);
      showToast("Note saved.");
      await refreshRecords();
    } });
}
function openUploadDoc() {
  recDialog({ eyebrow: "Document intake", title: "Upload a report", sub: "Imaging, histopathology, referral or other reports. The text is extracted; with AI on, a short summary is drafted for you to check.", submit: "Upload",
    body: `<div class="form-grid"><label class="field"><span>Type</span><select name="doc_type">${Object.entries(DOC_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <label class="field"><span>Report date</span><input type="date" name="doc_date" max="${todayISO()}" value="${todayISO()}" /></label>
      <label class="field span-2"><span>Title</span><input name="title" maxlength="160" placeholder="e.g. CT thorax with contrast" /></label></div>
      <label class="field"><span>File (PDF, image or text · up to 10 MB)</span><input type="file" name="file" accept=".pdf,.png,.jpg,.jpeg,.webp,.txt" required /></label>
      <label class="field"><span>Note (optional)</span><input name="note" maxlength="1000" /></label>`,
    onSubmit: async f => {
      const file = f.file.files[0];
      if (!file) throw new Error("Choose a file.");
      if (file.size > 10_000_000) throw new Error("The file is larger than 10 MB.");
      const data = await new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(String(r.result).split(",")[1]); r.onerror = reject; r.readAsDataURL(file); });
      $("#recDialogSubmit").textContent = "Uploading…";
      await post(`/api/patients/${S.patient.id}/documents`, { filename: file.name, content_type: file.type || null, data_base64: data, doc_type: f.doc_type.value, doc_date: f.doc_date.value, title: f.title.value, note: f.note.value });
      showToast("Report uploaded.");
      await refreshRecords();
    } });
}
function openNewDischarge(kind = "discharge") {
  recDialog({ eyebrow: "Discharge", title: "Create a summary draft", sub: "The draft is built from the record for this period. You review and edit it before it is finalised.", submit: "Create draft",
    body: `<fieldset class="outcome-options compact-options"><legend>Type</legend>
        <label><input type="radio" name="kind" value="discharge" ${kind === "discharge" ? "checked" : ""} /><span><strong>Discharge summary</strong><small>Admission or day-care episode</small></span></label>
        <label><input type="radio" name="kind" value="end_of_treatment" ${kind === "end_of_treatment" ? "checked" : ""} /><span><strong>End-of-treatment summary</strong><small>After a course of chemotherapy or radiotherapy</small></span></label></fieldset>
      <div class="form-grid"><label class="field"><span>From</span><input type="date" name="period_from" max="${todayISO()}" /><small class="muted">Blank: first treatment date</small></label>
      <label class="field"><span>To</span><input type="date" name="period_to" max="${todayISO()}" value="${todayISO()}" /></label></div>`,
    onSubmit: async f => {
      $("#recDialogSubmit").textContent = "Drafting…";
      const ds = await post(`/api/patients/${S.patient.id}/discharge-summaries`, { kind: f.kind.value, period_from: f.period_from.value || null, period_to: f.period_to.value || null });
      showToast("Draft created. Review each section before finalising.");
      navigate(`#discharge/${ds.id}`);
    } });
}

/* ------------------------------------------------------------------ discharge editor */
function medRow(m = {}, i, locked) {
  const dis = locked ? "disabled" : "";
  return `<tr data-med-row="${i}"><td><input name="name" value="${esc(m.name || "")}" maxlength="120" placeholder="Medicine" ${dis} /></td><td><input name="dose" value="${esc(m.dose || "")}" maxlength="120" ${dis} /></td>
    <td><input name="route" value="${esc(m.route || "")}" maxlength="120" placeholder="Oral" ${dis} /></td><td><input name="frequency" value="${esc(m.frequency || "")}" maxlength="120" placeholder="e.g. twice a day" ${dis} /></td>
    <td><input name="duration" value="${esc(m.duration || "")}" maxlength="120" placeholder="e.g. 5 days" ${dis} /></td><td><input name="instructions" value="${esc(m.instructions || "")}" maxlength="300" placeholder="e.g. after food" ${dis} /></td>
    <td>${locked ? "" : `<button type="button" class="icon-action danger" data-rec="remove-med" aria-label="Remove medicine">×</button>`}</td></tr>`;
}
function renderDischarge() {
  const ds = S.ds, c = ds.content, p = c.patient;
  const locked = ds.status !== "draft";
  const sources = new Map(ds.sources.map(s => [s.id, s]));
  $("#dischargeEyebrow").textContent = `${ds.kind_label} · version ${ds.version}`;
  $("#dischargeTitle").innerHTML = `${esc(c.title)} ${dsPill(ds.status)}`;
  $("#dischargeSubtitle").textContent = `${p.first_name} ${p.last_name} · ${p.medical_record_number} · ${fmtDate(ds.period_from)} to ${fmtDate(ds.period_to)}`;
  $("#dischargeActions").innerHTML = `<a class="button secondary" href="/api/discharge-summaries/${ds.id}/print" target="_blank" rel="noopener">Print / PDF</a>
    ${locked ? `${ds.status === "final" ? `<button class="button secondary" data-rec="revise">New version</button><button class="button primary" data-wa-share="discharge" data-summary="${ds.id}">Share on WhatsApp</button>` : ""}`
      : `<button class="button secondary" data-rec="regenerate">Rebuild from record</button><button class="button danger-outline" data-rec="delete-draft">Delete draft</button><button class="button primary" data-rec="finalise">Review &amp; finalise</button>`}`;
  const course = c.course || {};
  const unsupported = course.unsupported || 0;
  const chemo = c.chemotherapy.map(x => `<li><strong>${esc(x.regimen)}</strong> (${esc(x.drugs.join(", "))}) — ${x.cycles_given} of ${x.cycles_planned} cycles${x.first_date ? `, ${fmtDate(x.first_date)} to ${fmtDate(x.last_date)}` : ""}${x.changes.length ? `; ${esc(x.changes.join(", "))}` : ""}</li>`).join("");
  const rt = c.radiotherapy.map(x => `<li><strong>${esc(x.site)}</strong> — ${gy(x.dose_delivered_cgy)} in ${x.fractions_delivered} of ${x.fractions_planned} fractions${x.first_date ? `, ${fmtDate(x.first_date)} to ${fmtDate(x.last_date)}` : ""}
      <ul>${x.phases.map(ph => `<li>${esc(ph.name)}: ${gy(ph.dose_per_fraction_cgy)} × ${ph.fractions_planned} ${esc(ph.technique || "")} — delivered ${ph.fractions_delivered}/${ph.fractions_planned}</li>`).join("")}
      ${x.gaps.length ? `<li>Interruptions: ${esc(x.gaps.map(g => `${fmtShort(g.date)} ${g.category || "no reason recorded"}`).join("; "))}</li>` : ""}${x.concurrent_chemo ? `<li>Concurrent ${esc(x.concurrent_chemo)}</li>` : ""}${x.completion ? `<li>${esc(x.completion.outcome_label)}</li>` : ""}</ul></li>`).join("");
  const fu = c.follow_up || {};
  $("#dischargeContent").innerHTML = `<div class="discharge-layout">
    <form id="dischargeForm" class="discharge-doc panel" novalidate>
      ${locked ? `<div class="locked-note">${ds.status === "final" ? `Finalised by ${esc(ds.finalised_by)} on ${fmtDateTime(ds.finalised_at)}. Create a new version to change it.` : "This version was replaced by a newer one."}</div>` : '<div class="draft-note"><strong>Draft.</strong> Sections marked <span class="from-record">From the record</span> are rebuilt from the chart; the rest is yours to edit. Medicines, condition, advice and follow-up are never generated.</div>'}
      <section><h2>Diagnosis</h2><textarea name="diagnosis" rows="2" maxlength="4000" ${locked ? "disabled" : ""}>${esc(c.diagnosis || "")}</textarea></section>
      <section><h2>Treatment given <span class="from-record">From the record</span></h2>
        ${chemo ? `<p class="eyebrow">Chemotherapy</p><ul class="ds-list">${chemo}</ul>` : ""}${rt ? `<p class="eyebrow">Radiotherapy</p><ul class="ds-list">${rt}</ul>` : ""}${!chemo && !rt ? '<p class="muted">No treatment recorded in this period.</p>' : ""}</section>
      <section><h2>Course during treatment <span class="method-tag">${esc(METHOD_LABEL[course.method] || course.method || "")}${course.model ? ` · ${esc(course.model)}` : ""}</span></h2>
        ${course.error ? `<p class="ai-error">${esc(course.error)}</p>` : ""}
        <div class="narrative-preview">${citationHtml(course.sentences || [], sources)}</div>
        ${unsupported ? `<p class="text-amber small">${unsupported} sentence(s) cite no source — check or edit them.</p>` : ""}
        ${locked ? "" : `<details class="edit-narrative"><summary>Edit text</summary><textarea name="course_text" rows="8" maxlength="6000">${esc(course.text || "")}</textarea><p class="muted small">Keep the [S#] markers next to facts so the source stays linked. They are removed from the printed copy.</p></details>`}
      </section>
      <section><h2>Toxicities <span class="from-record">From the record</span></h2>
        ${c.toxicities.length ? `<div class="table-wrap"><table class="rt-table compact-table"><thead><tr><th>CTCAE term</th><th>Worst</th><th>Date</th><th>Recorded in</th></tr></thead><tbody>${c.toxicities.map(t => `<tr><td>${esc(t.term)}</td><td>${gradeBadge(t.worst_grade, "small")}</td><td>${fmtDate(t.date)}</td><td>${esc(t.source)}</td></tr>`).join("")}</tbody></table></div>` : '<p class="muted">None recorded.</p>'}</section>
      <section><h2>Complaints between visits <span class="from-record">From the record</span></h2>
        ${c.complaints.length ? `<ul class="ds-list">${c.complaints.map(x => `<li>${fmtDate(x.date)} (${esc(x.source)}): ${esc(x.summary)}${x.red_flags.length ? ` <span class="chip chip-alert">${esc(x.red_flags.join(", "))}</span>` : ""} — <em>${esc(x.outcome)}</em>${x.reply ? `; reply: “${esc(x.reply)}”` : ""}</li>`).join("")}</ul>` : '<p class="muted">None.</p>'}</section>
      <section><h2>Investigations <span class="from-record">From the record</span></h2>
        ${c.labs.length ? `<div class="ctx-labs">${c.labs.map(l => `<span class="ctx-lab ${l.flag ? "flag-" + l.flag : ""}">${esc(l.test_name)} <b>${fmtNum(l.value)}</b> ${fmtUnit(l.unit)}${l.flag ? (l.flag === "high" ? " ↑" : " ↓") : ""} <em>${fmtShort(l.collection_date)}</em></span>`).join("")}</div>` : '<p class="muted">No verified labs in this period.</p>'}
        ${c.documents.length ? `<ul class="ds-list">${c.documents.map(d => `<li><strong>${esc(d.title)}</strong> (${fmtDate(d.date)}): ${esc(d.detail)}</li>`).join("")}</ul>` : ""}</section>
      <section><h2>Condition at discharge <span class="req">required</span></h2><textarea name="condition_at_discharge" rows="2" maxlength="4000" ${locked ? "disabled" : ""} placeholder="e.g. Ambulant, eating normally. Skin: patchy moist desquamation in inframammary fold, healing.">${esc(c.condition_at_discharge || "")}</textarea></section>
      <section><h2>Medicines</h2>
        <div class="table-wrap"><table class="med-table"><thead><tr><th>Medicine</th><th>Dose</th><th>Route</th><th>How often</th><th>For how long</th><th>Instructions</th><th></th></tr></thead><tbody id="dsMedRows">${(c.medications.length || locked ? c.medications : [{}]).map((m, i) => medRow(m, i, locked)).join("")}</tbody></table></div>
        ${locked ? "" : '<button type="button" class="button secondary small" data-rec="add-med">+ Add medicine</button>'}
        ${locked && c.medications.length ? `<p><button type="button" class="button secondary small" data-wa-share="prescription" data-summary="${ds.id}">Send prescription on WhatsApp</button></p>` : ""}
      </section>
      <section><h2>Advice</h2><textarea name="advice" rows="3" maxlength="4000" ${locked ? "disabled" : ""} placeholder="e.g. Skin care, diet, when to call">${esc(c.advice || "")}</textarea></section>
      <section><h2>Follow-up <span class="req">required</span></h2>
        <div class="form-grid three"><label class="field"><span>Date</span><input type="date" name="fu_date" value="${esc(fu.date || "")}" ${locked ? "disabled" : ""} /></label>
          <label class="field"><span>With</span><input name="fu_with" value="${esc(fu.with || "")}" maxlength="120" ${locked ? "disabled" : ""} /></label>
          <label class="confirm-check align-end"><input type="checkbox" name="fu_none" ${fu.none ? "checked" : ""} ${locked ? "disabled" : ""} /><span>No follow-up visit</span></label>
          <label class="field span-3"><span>Instructions</span><input name="fu_instructions" value="${esc(fu.instructions || "")}" maxlength="600" ${locked ? "disabled" : ""} placeholder="e.g. Bring CBC report; review skin" /></label></div>
        ${locked && fu.date && ds.status === "final" ? `<button type="button" class="button secondary small" data-wa-share="reminder" data-visit="${esc(fu.date)}" data-with="${esc(fu.with || "")}">Schedule WhatsApp reminder for ${fmtDate(fu.date)}</button>` : ""}
      </section>
      <p class="form-error" role="alert" hidden></p>
      ${locked ? "" : '<div class="modal-actions sticky-actions"><span class="muted small" id="dsSaveState"></span><button class="button secondary" type="submit">Save draft</button><button class="button primary" type="button" data-rec="finalise">Review &amp; finalise</button></div>'}
    </form>
    <aside class="panel sources-panel"><p class="eyebrow">Sources</p><h2>Record entries used</h2><p class="muted small">Every sentence in the narrative cites these. Click a number in the text to jump here.</p>
      <div class="source-list"><div class="source-item" id="dsrc-S0"><span class="source-id">S0</span><div><strong>Patient record</strong><p>Name, age, sex, diagnosis</p></div></div>
      ${ds.sources.map(s => `<div class="source-item" id="dsrc-${s.id}"><span class="source-id">${s.id}</span><div><strong>${esc(s.title)}</strong><p><span class="muted">${fmtShort(s.date)}</span> ${esc(s.detail)}</p></div></div>`).join("")}</div>
    </aside>
  </div>`;
}
function dischargePayload() {
  const f = $("#dischargeForm").elements;
  const meds = $$("#dsMedRows tr").map(tr => Object.fromEntries($$("input", tr).map(i => [i.name, i.value.trim()]))).filter(m => Object.values(m).some(Boolean));
  const body = { diagnosis: f.diagnosis.value, condition_at_discharge: f.condition_at_discharge.value, advice: f.advice.value, medications: meds,
    follow_up: { date: f.fu_date.value, with: f.fu_with.value, instructions: f.fu_instructions.value, none: f.fu_none.checked } };
  if (f.course_text) body.course_text = f.course_text.value;
  return body;
}
async function saveDischarge(quiet = false) {
  S.ds = await patch(`/api/discharge-summaries/${S.ds.id}`, dischargePayload());
  renderDischarge();
  if (!quiet) showToast("Draft saved.");
}
function openFinalise() {
  const ds = S.ds;
  const unsupported = ds.content.course?.unsupported || 0;
  recDialog({ eyebrow: "Finalise", title: "Finalise this summary?", sub: "A finalised summary is locked, shown to the patient in the portal and can be shared on WhatsApp. Changes need a new version.", submit: "Finalise and sign",
    body: `<ul class="check-list">
        <li class="${ds.content.condition_at_discharge ? "ok" : "bad"}">Condition at discharge ${ds.content.condition_at_discharge ? "entered" : "missing"}</li>
        <li class="${ds.content.follow_up?.date || ds.content.follow_up?.none ? "ok" : "bad"}">Follow-up ${ds.content.follow_up?.date ? fmtDate(ds.content.follow_up.date) : ds.content.follow_up?.none ? "not needed" : "missing"}</li>
        <li class="${unsupported ? "warn" : "ok"}">${unsupported ? `${unsupported} narrative sentence(s) without a source` : "Every narrative sentence cites a source"}</li>
        <li class="ok">${ds.content.medications.length} medicine(s)</li></ul>
      <label class="confirm-check"><input type="checkbox" name="confirmed" /><span>I have reviewed every section against the record and take responsibility for this summary.</span></label>`,
    onSubmit: async f => {
      if (!f.confirmed.checked) throw new Error("Confirm that you reviewed the summary.");
      S.ds = await post(`/api/discharge-summaries/${ds.id}/finalise`, { confirmed: true });
      showToast("Summary finalised. It is now in the patient portal.");
      renderDischarge();
      await loadPatient(S.ds.patient_id);
    } });
}

/* ------------------------------------------------------------------ events */
document.addEventListener("click", async event => {
  const cite = event.target.closest("[data-cite]");
  if (cite) {
    const target = document.getElementById(`dsrc-${cite.dataset.cite}`) || document.getElementById(`src-${cite.dataset.cite}`);
    if (target) { target.scrollIntoView({ behavior: "smooth", block: "center" }); target.classList.add("flash"); setTimeout(() => target.classList.remove("flash"), 1600); }
    return;
  }
  const el = event.target.closest("[data-rec]");
  if (!el) return;
  try {
    switch (el.dataset.rec) {
      case "filter": S.summaryFilter = el.dataset.filter; return renderSummary();
      case "generate": {
        el.disabled = true; el.textContent = "Writing…";
        S.summary = { ...(await post(`/api/patients/${S.patient.id}/summary/narrative`, {})), notes: S.summary.notes, docs: S.summary.docs };
        renderSummary();
        return showToast(S.summary.narrative?.error || (S.summary.narrative?.method === "claude" ? "Summary drafted by Claude from the record." : "Summary written from the record."));
      }
      case "add-note": return openNote();
      case "edit-note": return openNote(S.summary.notes.find(n => n.id === Number(el.dataset.id)));
      case "delete-note": return confirmDialog("Delete this note?", "The note will be removed from the record.", async () => { await del(`/api/notes/${el.dataset.id}`); showToast("Note deleted."); await refreshRecords(); });
      case "upload-doc": return openUploadDoc();
      case "summarise-doc": el.disabled = true; el.textContent = "Summarising…"; await post(`/api/documents/${el.dataset.id}/summarise`, {}); showToast("Summary added. Check it against the file."); return refreshRecords();
      case "delete-doc": return confirmDialog("Delete this report?", "The uploaded file and its summary will be removed.", async () => { await del(`/api/documents/${el.dataset.id}`); showToast("Report deleted."); await refreshRecords(); });
      case "new-discharge": return openNewDischarge(el.dataset.kind || "discharge");
      case "add-med": $("#dsMedRows").insertAdjacentHTML("beforeend", medRow({}, $$("#dsMedRows tr").length, false)); return;
      case "remove-med": el.closest("tr").remove(); return;
      case "regenerate":
        return confirmDialog("Rebuild from the record?", "Treatment, toxicities, complaints and investigations are refreshed, and the narrative is rewritten (your edits to it are replaced). Diagnosis, condition, medicines, advice and follow-up are kept.", async () => {
          await saveDischarge(true);
          S.ds = await post(`/api/discharge-summaries/${S.ds.id}/regenerate`, {});
          renderDischarge();
          showToast("Rebuilt from the record.");
        }, "Rebuild");
      case "finalise": await saveDischarge(true); return openFinalise();
      case "revise": { const ds = await post(`/api/discharge-summaries/${S.ds.id}/revise`, {}); showToast(`Version ${ds.version} created as a draft.`); return navigate(`#discharge/${ds.id}`); }
      case "delete-draft":
        return confirmDialog("Delete this draft?", "The draft will be removed. Finalised versions are kept.", async () => { const pid = S.ds.patient_id; await del(`/api/discharge-summaries/${S.ds.id}`); showToast("Draft deleted."); navigate(`#patient/${pid}`); });
    }
  } catch (error) { showToast(error.message); }
});
document.addEventListener("submit", async event => {
  if (event.target.id !== "dischargeForm") return;
  event.preventDefault();
  const form = event.target;
  await withBusy(form, () => saveDischarge());
});
$("#recDialogForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const label = $("#recDialogSubmit").textContent;
  await withBusy(form, async () => { await S.recSubmit(form.elements); closeModals(); });
  $("#recDialogSubmit").textContent = label;
});

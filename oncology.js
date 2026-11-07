"use strict";
/* Diagnosis & staging at first presentation, tumour measurements during treatment and RECIST 1.1 response. */

const ONC_LATERALITY = { right: "Right", left: "Left", bilateral: "Bilateral", midline: "Midline", na: "Not applicable" };
const ONC_RESP_CLASS = { CR: "chip-ok", PR: "chip-ok", SD: "chip-teal", PD: "chip-alert", NE: "" };

EXT.patientLoaders.push(async p => {
  [p.oncology, p.docs] = await Promise.all([api(`/api/patients/${p.id}/oncology`).catch(() => null), api(`/api/patients/${p.id}/documents`).catch(() => [])]);
});
EXT.patientPanels.unshift(p => {
  const o = p.oncology;
  if (!o) return "";
  const d = o.diagnoses[o.diagnoses.length - 1];
  const latest = o.latest, best = o.best, base = o.baseline;
  return `<article class="panel span-full onc-panel"><div class="panel-heading"><div><p class="eyebrow">Cancer</p><h2>Diagnosis &amp; tumour response</h2></div>
      <div class="heading-actions">${d ? `<button class="button secondary" data-onc="edit-diagnosis" data-id="${d.id}">Edit staging</button>` : ""}
      <button class="button ${d ? "secondary" : "primary"}" data-onc="new-diagnosis">${d ? "+ Another primary" : "Record diagnosis & staging"}</button>
      <button class="button primary" data-onc="new-assessment" ${d || o.assessments.length ? "" : "disabled title=\"Record the diagnosis first\""}>+ Tumour measurement</button></div></div>
    ${o.diagnoses.length ? o.diagnoses.map(diagCard).join("") : '<div class="queue-empty">Record the type of cancer, its stage and markers at the first visit. Tumour measurements from each scan then track how well the treatment works.</div>'}
    ${o.assessments.length ? `<div class="onc-response">
        <div class="onc-kpis">
          ${latest ? `<div><small>Latest response</small><strong>${latest.response ? `<span class="chip ${ONC_RESP_CLASS[latest.response] || ""}">${esc(latest.response)}</span> ${esc(latest.response_label)}` : "Baseline only"}</strong><span class="muted small">${fmtDate(latest.assessment_date)} · ${esc(latest.modality_label)}${latest.response_override ? " · clinician-recorded" : latest.response ? " · RECIST 1.1 (computed)" : ""}</span></div>` : ""}
          ${base && latest && latest.sum_mm != null && latest.id !== base.id ? `<div><small>Tumour size (sum of targets)</small><strong>${fmtNum(base.sum_mm)} → ${fmtNum(latest.sum_mm)} mm</strong><span class="muted small">${latest.change_from_baseline_pct > 0 ? "+" : ""}${fmtNum(latest.change_from_baseline_pct)}% from baseline</span></div>`
            : base ? `<div><small>Baseline size (sum of targets)</small><strong>${fmtNum(base.sum_mm)} mm</strong><span class="muted small">${fmtDate(base.assessment_date)}</span></div>` : ""}
          ${best && best.response ? `<div><small>Best response</small><strong><span class="chip ${ONC_RESP_CLASS[best.response] || ""}">${esc(best.response)}</span></strong><span class="muted small">${fmtDate(best.assessment_date)}</span></div>` : ""}
        </div>
        ${o.series.length >= 2 ? oncChart(o) : ""}
      </div>
      <div class="table-wrap top-gap"><table class="onc-table"><thead><tr><th>Date</th><th>Assessment</th><th>Lesions</th><th>Sum</th><th>Change</th><th>Markers</th><th>Response</th><th></th></tr></thead><tbody>
        ${[...o.assessments].reverse().map(a => `<tr>
          <td>${fmtDate(a.assessment_date)}</td>
          <td>${esc(a.modality_label)}<br><small class="muted">${esc(a.timepoint_label)}${a.cycle_label ? ` · ${esc(a.cycle_label)}` : ""}</small></td>
          <td>${a.lesions.map(l => `<div class="onc-lesion ${l.kind}">${l.kind === "new" ? "<b>New:</b> " : ""}${esc(l.location)}${l.node ? " (node)" : ""}${l.size_mm != null ? ` <b>${fmtNum(l.size_mm)} mm</b>` : ""}${l.kind === "non_target" ? ` <span class="muted">non-target · ${esc(l.status)}</span>` : ""}</div>`).join("")}
            ${a.warnings?.length ? `<small class="text-amber">${a.warnings.map(esc).join("<br>")}</small>` : ""}</td>
          <td>${a.sum_mm != null ? `${fmtNum(a.sum_mm)} mm` : "—"}</td>
          <td>${a.change_from_baseline_pct != null ? `${a.change_from_baseline_pct > 0 ? "+" : ""}${fmtNum(a.change_from_baseline_pct)}%<br><small class="muted">vs baseline</small>` : "—"}</td>
          <td>${a.markers.map(m => `${esc(m.name)} ${esc(m.value)} ${esc(m.unit)}`).join("<br>") || "—"}</td>
          <td>${a.response ? `<span class="chip ${ONC_RESP_CLASS[a.response] || ""}">${esc(a.response)}</span>` : '<span class="muted">Baseline</span>'}
            ${a.response ? `<br><small class="muted">${a.response_override ? `Clinician: ${esc(a.response_note || "")}${a.computed ? ` (computed ${esc(a.computed)})` : ""}` : esc(a.computed_reason || "")}</small>` : ""}</td>
          <td><button class="text-button" data-onc="edit-assessment" data-id="${a.id}">Edit</button></td></tr>`).join("")}
      </tbody></table></div>
      <p class="muted small">Response is computed with RECIST 1.1 against the baseline and the smallest measurement so far; confirm it or record a different response with a reason (e.g. RANO, Lugano, iRECIST).</p>` : ""}
  </article>`;
});

function diagCard(d) {
  return `<div class="onc-diag"><div><strong>${esc([ONC_LATERALITY[d.laterality] && d.laterality !== "na" ? ONC_LATERALITY[d.laterality] : "", d.primary_site].filter(Boolean).join(" "))}</strong>
      ${d.histology ? ` · ${esc(d.histology)}` : ""}${d.grade ? ` · grade ${esc(d.grade)}` : ""}</div>
    <div class="onc-diag-grid">
      <span><small>Stage</small>${esc(d.stage_text || "Not staged")}</span>
      <span><small>Diagnosed</small>${fmtDate(d.diagnosis_date)}${d.method ? ` · ${esc(d.method)}` : ""}</span>
      ${d.biomarkers.length ? `<span><small>Biomarkers</small>${d.biomarkers.map(b => `${esc(b.name)} ${esc(b.value)}`).join(", ")}</span>` : ""}
      ${d.markers.length ? `<span><small>Baseline markers</small>${d.markers.map(m => `${esc(m.name)} ${esc(m.value)} ${esc(m.unit)}`).join(", ")}</span>` : ""}
      <span><small>Intent · ECOG</small>${esc(d.intent ? d.intent[0].toUpperCase() + d.intent.slice(1) : "—")} · ${d.ecog != null ? `ECOG ${d.ecog}` : "—"}</span>
    </div>${d.notes ? `<p class="muted small">${esc(d.notes)}</p>` : ""}</div>`;
}

function oncChart(o) {
  const pts = o.series, base = o.baseline?.sum_mm || pts[0].sum_mm;
  const W = 640, H = 200, L = 44, R = 16, T = 14, B = 34;
  const max = Math.ceil((Math.max(...pts.map(p => p.sum_mm), base) * 1.15 || 10) / 10) * 10;
  const t0 = new Date(pts[0].date).getTime(), t1 = new Date(pts[pts.length - 1].date).getTime() || t0 + 1;
  const x = d => L + ((new Date(d).getTime() - t0) / Math.max(t1 - t0, 1)) * (W - L - R);
  const y = v => T + (1 - v / max) * (H - T - B);
  const pr = base * 0.7;
  const ticks = [0, max / 2, max].map(v => Math.round(v));
  const color = r => ({ CR: "#2f6a47", PR: "#2f6a47", SD: "#0e6e6a", PD: "#b03a2e" }[r] || "#667879");
  return `<figure class="onc-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Sum of target lesion diameters over time">
      ${ticks.map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`).join("")}
      <line x1="${L}" x2="${W - R}" y1="${y(base)}" y2="${y(base)}" class="base"/><text x="${W - R}" y="${y(base) - 5}" text-anchor="end" class="lbl">baseline</text>
      <line x1="${L}" x2="${W - R}" y1="${y(pr)}" y2="${y(pr)}" class="pr"/><text x="${W - R}" y="${y(pr) - 5}" text-anchor="end" class="lbl">−30% (partial response)</text>
      <polyline points="${pts.map(p => `${x(p.date)},${y(p.sum_mm)}`).join(" ")}" class="line"/>
      ${pts.map(p => `<circle cx="${x(p.date)}" cy="${y(p.sum_mm)}" r="5" fill="${color(p.response)}"><title>${p.date}: ${p.sum_mm} mm${p.response ? ` · ${p.response}` : ""}</title></circle>
        <text x="${x(p.date)}" y="${H - 12}" text-anchor="middle">${fmtShort(p.date)}</text>`).join("")}
    </svg><figcaption class="muted small">Sum of target lesion diameters, in mm. Points: green = response, teal = stable, red = progression.</figcaption></figure>`;
}

/* ------------------------------------------------------------------ dialogs */
function pairRows(list, withUnit, prefix) {
  return (list.length ? list : [{}]).map(m => `<div class="onc-pair" data-pair="${prefix}"><input name="${prefix}_name" value="${esc(m.name || "")}" placeholder="${prefix === "bio" ? "e.g. ER" : "e.g. CEA"}" maxlength="60" />
    <input name="${prefix}_value" value="${esc(m.value || "")}" placeholder="${prefix === "bio" ? "e.g. positive (90%)" : "value"}" maxlength="60" />
    ${withUnit ? `<input name="${prefix}_unit" value="${esc(m.unit || "")}" placeholder="unit" maxlength="20" />` : ""}<button type="button" class="icon-action danger" data-onc="remove-pair" aria-label="Remove">×</button></div>`).join("");
}
function readPairs(form, prefix, withUnit) {
  return $$(`[data-pair="${prefix}"]`, form).map(row => ({ name: $(`[name=${prefix}_name]`, row).value.trim(), value: $(`[name=${prefix}_value]`, row).value.trim(), unit: withUnit ? $(`[name=${prefix}_unit]`, row).value.trim() : "" }))
    .filter(m => m.name && m.value);
}
function openDiagnosis(existing) {
  const o = S.patient.oncology, d = existing || {};
  const opt = (list, value) => list.map(v => `<option ${v === value ? "selected" : ""}>${esc(v)}</option>`).join("");
  recDialog({ eyebrow: "Diagnosis & staging", title: existing ? "Edit diagnosis & staging" : "Record diagnosis & staging", sub: `${S.patient.first_name} ${S.patient.last_name} · at the first visit, update if re-staged`,
    body: `<div class="form-grid three">
      <label class="field"><span>Date of diagnosis</span><input type="date" name="diagnosis_date" max="${todayISO()}" value="${esc(d.diagnosis_date || todayISO())}" required /></label>
      <label class="field"><span>Primary site</span><input name="primary_site" list="oncSites" value="${esc(d.primary_site || "")}" required maxlength="80" placeholder="e.g. Breast" /><datalist id="oncSites">${o.options.sites.map(s => `<option value="${esc(s)}"></option>`).join("")}</datalist></label>
      <label class="field"><span>Side</span><select name="laterality"><option value="">—</option>${Object.entries(ONC_LATERALITY).map(([k, v]) => `<option value="${k}" ${d.laterality === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label class="field span-2"><span>Histology</span><input name="histology" value="${esc(d.histology || "")}" maxlength="160" placeholder="e.g. Invasive ductal carcinoma" /></label>
      <label class="field"><span>Grade</span><input name="grade" value="${esc(d.grade || "")}" maxlength="20" placeholder="e.g. 2 / high" /></label>
      <label class="field span-3"><span>How it was diagnosed</span><input name="method" value="${esc(d.method || "")}" maxlength="120" placeholder="e.g. Ultrasound-guided core biopsy; PET-CT" /></label></div>
      <fieldset class="onc-tnm"><legend>TNM staging</legend><div class="form-grid four">
        <label class="field"><span>Type</span><select name="tnm_prefix">${[["c", "Clinical (c)"], ["p", "Pathological (p)"], ["yc", "After therapy, clinical (yc)"], ["yp", "After therapy, pathological (yp)"]].map(([k, v]) => `<option value="${k}" ${(d.tnm_prefix || "c") === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
        <label class="field"><span>T</span><input name="t_stage" value="${esc(d.t_stage || "")}" maxlength="12" placeholder="e.g. 2" /></label>
        <label class="field"><span>N</span><input name="n_stage" value="${esc(d.n_stage || "")}" maxlength="12" placeholder="e.g. 1" /></label>
        <label class="field"><span>M</span><input name="m_stage" value="${esc(d.m_stage || "")}" maxlength="12" placeholder="e.g. 0" /></label>
        <label class="field"><span>Stage group</span><select name="stage_group"><option value="">—</option>${opt(o.options.stage_groups, d.stage_group)}</select></label>
        <label class="field"><span>System</span><input name="staging_system" value="${esc(d.staging_system || "AJCC 8th")}" maxlength="40" placeholder="AJCC 8th / FIGO 2018" /></label>
        <label class="field"><span>Intent</span><select name="intent"><option value="">—</option><option value="curative" ${d.intent === "curative" ? "selected" : ""}>Curative</option><option value="palliative" ${d.intent === "palliative" ? "selected" : ""}>Palliative</option></select></label>
        <label class="field"><span>ECOG</span><select name="ecog"><option value="">—</option>${[0, 1, 2, 3, 4].map(v => `<option ${d.ecog === v ? "selected" : ""}>${v}</option>`).join("")}</select></label></div></fieldset>
      <h3>Biomarkers <small class="muted">receptors, mutations, PD-L1…</small></h3><div id="oncBio">${pairRows(d.biomarkers || [], false, "bio")}</div><button type="button" class="text-button" data-onc="add-pair" data-prefix="bio">+ Biomarker</button>
      <h3>Baseline tumour markers <small class="muted">CEA, CA-125, PSA…</small></h3><div id="oncMk">${pairRows(d.markers || [], true, "mk")}</div><button type="button" class="text-button" data-onc="add-pair" data-prefix="mk">+ Marker</button>
      <label class="field top-gap"><span>Notes</span><textarea name="notes" rows="2" maxlength="1500">${esc(d.notes || "")}</textarea></label>`,
    submit: existing ? "Save changes" : "Save diagnosis",
    onSubmit: async f => {
      const body = { diagnosis_date: f.diagnosis_date.value, primary_site: f.primary_site.value, laterality: f.laterality.value || null, histology: f.histology.value,
        grade: f.grade.value, method: f.method.value, tnm_prefix: f.tnm_prefix.value, t_stage: f.t_stage.value, n_stage: f.n_stage.value, m_stage: f.m_stage.value,
        stage_group: f.stage_group.value || null, staging_system: f.staging_system.value, intent: f.intent.value || null, ecog: f.ecog.value === "" ? null : Number(f.ecog.value),
        biomarkers: readPairs($("#recDialogForm"), "bio", false), markers: readPairs($("#recDialogForm"), "mk", true), notes: f.notes.value };
      if (existing) await patch(`/api/diagnoses/${existing.id}`, body); else await post(`/api/patients/${S.patient.id}/diagnoses`, body);
      showToast(existing ? "Staging updated." : "Diagnosis recorded.");
      await loadPatient(S.patient.id);
    } });
}
function lesionRow(l = {}) {
  return `<tr data-lesion><td><input name="location" value="${esc(l.location || "")}" maxlength="120" placeholder="e.g. Right upper lobe mass" /></td>
    <td><select name="kind">${[["target", "Target"], ["non_target", "Non-target"], ["new", "New lesion"]].map(([k, v]) => `<option value="${k}" ${(l.kind || "target") === k ? "selected" : ""}>${v}</option>`).join("")}</select></td>
    <td class="center"><input type="checkbox" name="node" ${l.node ? "checked" : ""} title="Lymph node (short axis)" /></td>
    <td><input name="size_mm" type="number" min="0" max="500" step="0.1" value="${l.size_mm ?? ""}" placeholder="mm" /></td>
    <td><select name="status"><option value="">—</option>${["present", "absent", "progression"].map(s => `<option value="${s}" ${l.status === s ? "selected" : ""}>${s === "progression" ? "unequivocal progression" : s}</option>`).join("")}</select></td>
    <td><button type="button" class="icon-action danger" data-onc="remove-lesion" aria-label="Remove lesion">×</button></td></tr>`;
}
function openAssessment(existing) {
  const o = S.patient.oncology, a = existing || {};
  const last = o.assessments[o.assessments.length - 1];
  const seedLesions = existing ? a.lesions : last ? last.lesions.filter(l => l.kind !== "new").map(l => ({ ...l, size_mm: null })) : [{}];
  const docs = S.patient.docs || [];
  recDialog({ eyebrow: "Tumour measurement", title: existing ? "Edit tumour measurement" : "Add tumour measurement",
    sub: last && !existing ? "Lesions from the last scan are copied: enter today's sizes (0 if a lesion has gone)." : "Measure up to 5 target lesions (longest diameter; short axis for lymph nodes).",
    body: `<div class="form-grid four">
      <label class="field"><span>Date</span><input type="date" name="assessment_date" max="${todayISO()}" value="${esc(a.assessment_date || todayISO())}" required /></label>
      <label class="field"><span>Assessed by</span><select name="modality">${Object.entries(o.options.modalities).map(([k, v]) => `<option value="${k}" ${(a.modality || last?.modality || "ct") === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label class="field"><span>Timepoint</span><select name="timepoint">${Object.entries(o.options.timepoints).map(([k, v]) => `<option value="${k}" ${(a.timepoint || (o.assessments.length ? "during" : "baseline")) === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label class="field"><span>When in treatment</span><input name="cycle_label" value="${esc(a.cycle_label || "")}" maxlength="60" placeholder="e.g. After cycle 3" /></label></div>
      <div class="table-wrap"><table class="med-table"><thead><tr><th>Lesion / location</th><th>Type</th><th>Node</th><th>Size (mm)</th><th>Non-target status</th><th></th></tr></thead>
        <tbody id="oncLesions">${(seedLesions.length ? seedLesions : [{}]).map(lesionRow).join("")}</tbody></table></div>
      <button type="button" class="button secondary small" data-onc="add-lesion">+ Lesion</button>
      <h3 class="top-gap">Tumour markers</h3><div id="oncMk">${pairRows(a.markers || (last?.markers || []).map(m => ({ name: m.name, unit: m.unit })), true, "mk")}</div><button type="button" class="text-button" data-onc="add-pair" data-prefix="mk">+ Marker</button>
      <label class="field top-gap"><span>Findings (from the report)</span><textarea name="findings" rows="2" maxlength="2000">${esc(a.findings || "")}</textarea></label>
      ${docs.length ? `<label class="field"><span>Report on file (optional)</span><select name="document_id"><option value="">—</option>${docs.map(doc => `<option value="${doc.id}" ${a.document_id === doc.id ? "selected" : ""}>${esc(doc.title)} · ${fmtDate(doc.doc_date)}</option>`).join("")}</select></label>` : ""}
      <details class="top-gap" ${a.response_override ? "open" : ""}><summary>Record a different response than RECIST 1.1</summary><div class="form-grid">
        <label class="field"><span>Response</span><select name="response_override"><option value="">Use the computed RECIST response</option>${Object.entries(o.options.responses).map(([k, v]) => `<option value="${k}" ${a.response_override === k ? "selected" : ""}>${k} · ${v}</option>`).join("")}</select></label>
        <label class="field"><span>Reason</span><input name="response_note" maxlength="500" value="${esc(a.response_note || "")}" placeholder="e.g. Pseudo-progression on immunotherapy (iRECIST)" /></label></div></details>`,
    submit: existing ? "Save changes" : "Save measurement",
    onSubmit: async f => {
      const lesions = $$("#oncLesions tr").map(tr => ({ location: $("[name=location]", tr).value.trim(), kind: $("[name=kind]", tr).value, node: $("[name=node]", tr).checked,
        size_mm: $("[name=size_mm]", tr).value === "" ? null : Number($("[name=size_mm]", tr).value), status: $("[name=status]", tr).value || null })).filter(l => l.location);
      const body = { assessment_date: f.assessment_date.value, modality: f.modality.value, timepoint: f.timepoint.value, cycle_label: f.cycle_label.value, lesions,
        markers: readPairs($("#recDialogForm"), "mk", true), findings: f.findings.value, document_id: f.document_id?.value ? Number(f.document_id.value) : null,
        response_override: f.response_override.value || null, response_note: f.response_note.value };
      const saved = existing ? await patch(`/api/tumour-assessments/${existing.id}`, body) : await post(`/api/patients/${S.patient.id}/tumour-assessments`, body);
      showToast(saved.response ? `Saved · response ${saved.response} (${saved.response_label}).` : "Baseline measurement saved.");
      await loadPatient(S.patient.id);
    } });
  if (existing) $(".modal-actions", $("#recDialogForm")).insertAdjacentHTML("afterbegin", `<button type="button" class="button danger-outline" data-onc="delete-assessment" data-id="${existing.id}">Delete</button>`);
}
document.addEventListener("click", async e => {
  const el = e.target.closest("[data-onc]");
  if (!el) return;
  const o = S.patient?.oncology;
  try {
    switch (el.dataset.onc) {
      case "new-diagnosis": return openDiagnosis(null);
      case "edit-diagnosis": return openDiagnosis(o.diagnoses.find(d => d.id === Number(el.dataset.id)));
      case "new-assessment": return openAssessment(null);
      case "edit-assessment": return openAssessment(o.assessments.find(a => a.id === Number(el.dataset.id)));
      case "add-lesion": $("#oncLesions").insertAdjacentHTML("beforeend", lesionRow({})); return;
      case "remove-lesion": el.closest("tr").remove(); return;
      case "add-pair": $(el.dataset.prefix === "bio" ? "#oncBio" : "#oncMk").insertAdjacentHTML("beforeend", pairRows([{}], el.dataset.prefix === "mk", el.dataset.prefix)); return;
      case "remove-pair": el.closest(".onc-pair").remove(); return;
      case "delete-assessment":
        await del(`/api/tumour-assessments/${el.dataset.id}`);
        closeModals(); showToast("Measurement deleted.");
        return loadPatient(S.patient.id);
    }
  } catch (error) { showToast(error.message); }
});

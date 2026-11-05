"use strict";
/* Radiation oncology workflow: course (cover sheet), plan (constraints, isocentre shifts), daily fraction log,
   weekly early-reaction review and completion summary. Uses helpers and EXT hooks from app.js. */

const RT_TABS = { plan: "rt-plan", daily: "rt-daily", weekly: "rt-weekly", complete: "rt-complete" };
const RT_STATUS = { planned: ["Planned", "neutral"], active: ["On treatment", "complete"], on_hold: ["Paused", "planned"], completed: ["Completed", "complete"], discontinued: ["Stopped", "alert"] };
const DAYS = [[1, "Mon"], [2, "Tue"], [3, "Wed"], [4, "Thu"], [5, "Fri"], [6, "Sat"], [7, "Sun"]];
const gy = cgy => (cgy === null || cgy === undefined) ? "—" : `${Number((cgy / 100).toFixed(2))} Gy`;
const rtPill = status => { const [label, tone] = RT_STATUS[status] || [status, "neutral"]; return `<span class="status-pill ${tone}">${esc(label)}</span>`; };
const sevIcon = sev => sev === "alert" ? "!" : sev === "warning" ? "▲" : "i";

async function rtOptions() {
  if (!S.rtOptions) S.rtOptions = await api("/api/rt/options");
  return S.rtOptions;
}

/* ------------------------------------------------------------------ routing & loading */
EXT.routes.rt = async (id, tab) => {
  if (!id) return null;
  await loadRt(id);
  return RT_TABS[tab] || "rt-plan";
};
for (const [tab, view] of Object.entries(RT_TABS)) EXT.navHref[view] = () => (S.RT ? `#rt/${S.RT.course.id}/${tab}` : "#");
EXT.afterShow.push(view => {
  $("#navRT").classList.toggle("hidden", !S.RT || !view.startsWith("rt-"));
  if (S.RT) $("#navRTName").textContent = `RT · ${S.RT.course.site}`;
  if (view.startsWith("rt-") && S.RT) {
    $("#navPatient").classList.remove("hidden");
  }
});

async function loadRt(id) {
  const data = await api(`/api/rt-courses/${id}`);
  setRt(data);
  if (!S.patient || S.patient.id !== data.patient.id) await loadPatient(data.patient.id);
  await rtOptions();
}
function setRt(data) {
  S.RT = data;
  S.rtReports = null;
  renderRt();
}
async function rtCall(method, path, body, message) {
  const data = await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  if (data?.course) setRt(data);
  if (message) showToast(message);
  return data;
}

/* ------------------------------------------------------------------ patient record panel */
EXT.patientPanels.push(p => {
  const courses = p.rt_courses || [];
  return `<article class="panel span-full"><div class="panel-heading"><div><p class="eyebrow">Radiation oncology</p><h2>Radiotherapy courses</h2></div><button class="button primary" data-rt="new-course">+ New RT course</button></div>
    ${courses.length ? courses.map(c => {
      const pct = c.fractions_planned ? Math.round((c.fractions_delivered / c.fractions_planned) * 100) : 0;
      return `<div class="plan-row">
        <div class="plan-row-main"><strong>${esc(c.site)}</strong>
          <small>${esc(c.prescription)}${c.technique ? ` · ${esc(c.technique)}` : ""}${c.intent ? ` · ${esc(c.intent)}` : ""}${c.start_date ? ` · from ${fmtDate(c.start_date)}` : ""}</small>
          <div class="drug-chips">${rtPill(c.status)}${c.approved_at ? "" : '<span class="chip chip-warn">Plan not approved</span>'}${c.attention.filter(a => a.severity !== "info").slice(0, 2).map(a => `<span class="chip ${a.severity === "alert" ? "chip-alert" : "chip-warn"}">${esc(a.text.split(".")[0])}</span>`).join("")}</div>
        </div>
        <div class="plan-row-progress"><div class="progress-track"><span style="width:${pct}%"></span></div><small>${c.fractions_delivered}/${c.fractions_planned} fractions · ${gy(c.dose_delivered_cgy)} of ${gy(c.dose_planned_cgy)}${c.next_fraction_date ? ` · next ${fmtShort(c.next_fraction_date)}` : ""}</small></div>
        <div class="plan-row-actions"><a class="button secondary" href="#rt/${c.id}/plan">Open course</a></div>
      </div>`;
    }).join("") : '<div class="queue-empty">No radiotherapy course. Create one from the case sheet: prescription by phase, set-up data and dose constraints.</div>'}
  </article>`;
});
EXT.patientRowChips.push(p => p.active_rt_planned ? `<span class="chip chip-teal" title="Radiotherapy fractions delivered">RT ${p.active_rt_fractions}/${p.active_rt_planned}</span>` : "");

/* ------------------------------------------------------------------ render */
function renderRt() {
  const R = S.RT;
  if (!R) return;
  $$(".rt-subtitle").forEach(el => { el.textContent = `${patientName(R.patient)} · ${R.course.site} · ${R.summary.prescription}`; });
  $("#rtPlanTitle").textContent = R.course.site;
  renderRtPlan();
  renderRtDaily();
  renderRtWeekly();
  renderRtComplete();
  updateHeader();
  $("#navRTName").textContent = `RT · ${R.course.site}`;
}

function attentionHtml(items) {
  if (!items.length) return "";
  return `<article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Needs attention</p><h2>Flags from the record</h2></div><span class="muted">Computed; the clinician decides</span></div>
    ${items.map(a => `<div class="attention-item"><span class="signal-icon ${a.severity === "alert" ? "" : "warning"} ${a.severity === "info" ? "info" : ""}">${sevIcon(a.severity)}</span><div><strong>${esc(a.text)}</strong></div><span></span></div>`).join("")}
  </article>`;
}

function renderRtPlan() {
  const R = S.RT, c = R.course, s = R.summary;
  const pct = s.fractions_planned ? Math.round((s.fractions_delivered / s.fractions_planned) * 100) : 0;
  const closed = ["completed", "discontinued"].includes(c.status);
  $("#rtPlanActions").innerHTML = `
    ${!closed ? `<button class="button secondary" data-rt="edit-course">Edit course</button>` : ""}
    ${!c.approved_at && !closed ? `<button class="button primary" data-rt="approve">Approve plan</button>` : ""}
    ${c.status === "active" ? `<button class="button secondary" data-rt="pause">Pause / stop</button>` : ""}
    ${c.status === "on_hold" ? `<button class="button primary" data-rt="resume">Resume treatment</button>` : ""}
    <button class="button danger-outline" data-rt="delete-course">Delete</button>`;
  const setup = Object.entries(c.setup || {});
  const SETUP_LABEL = { head_rest: "Head rest", arm_position: "Arm position", immobilisation: "Immobilisation", bladder: "Bladder", bolus_cm: "Bolus", knee_rest: "Knee rest", special_instructions: "Special instructions" };
  $("#rtPlan").innerHTML = `
    <article class="cycle-hero">
      <div class="cycle-ring" style="--pct:${pct}"><span>${s.fractions_delivered}</span><small>of ${s.fractions_planned}</small></div>
      <div class="cycle-hero-copy">
        <div class="inline-heading">${rtPill(c.status)}<span class="muted">${esc(c.intent || "Intent not recorded")} · ${esc(c.machine || "Machine not recorded")} · ${esc(c.treatment_days_label)}</span></div>
        <h2>${esc(s.prescription)}</h2>
        <p>${esc([c.diagnosis, c.stage, c.histopathology].filter(Boolean).join(" · ") || R.patient.diagnosis || "")}</p>
        <div class="dose-line">
          <span><small>Delivered</small><strong>${gy(s.dose_delivered_cgy)} of ${gy(s.dose_planned_cgy)}</strong></span>
          <span><small>Next fraction</small><strong>${s.next_fraction_date ? fmtDate(s.next_fraction_date) : c.status === "on_hold" ? "Paused" : "—"}</strong></span>
          <span><small>${c.completion ? "Ended" : "Projected last fraction"}</small><strong>${fmtDate(c.completion?.end_date || s.projected_end_date)}</strong></span>
          ${R.chemo_plan ? `<span><small>Concurrent chemotherapy</small><strong><a class="hero-link" href="#plan/${R.chemo_plan.id}/cycles">${esc(R.chemo_plan.regimen_name)}</a></strong></span>` : ""}
        </div>
      </div>
      ${c.status === "active" && c.approved_at ? `<a class="button light" href="#rt/${c.id}/daily">Record fraction →</a>` : !c.approved_at && !closed ? `<button class="button light" data-rt="approve">Approve plan →</button>` : ""}
    </article>
    <div class="metric-grid">
      <article class="metric-card"><div><small>Fractions</small><strong>${s.fractions_delivered}/${s.fractions_planned}</strong><p>${s.fractions_remaining} remaining</p></div><span class="metric-dot">#</span></article>
      <article class="metric-card"><div><small>Dose delivered</small><strong>${gy(s.dose_delivered_cgy)}</strong><p>of ${gy(s.dose_planned_cgy)} prescribed</p></div><span class="metric-dot">Σ</span></article>
      <article class="metric-card"><div><small>Overall treatment time</small><strong>${s.overall_treatment_days ?? "—"}${s.overall_treatment_days ? " d" : ""}</strong><p>${s.planned_treatment_days ? `${s.planned_treatment_days} d if uninterrupted` : "starts with the first fraction"}</p></div><span class="metric-dot ${s.overall_treatment_days > s.planned_treatment_days ? "pending" : ""}">⏱</span></article>
      <article class="metric-card"><div><small>Missed treatment days</small><strong>${s.gaps.length}</strong><p>${s.gaps.filter(g => !g.explained).length} without a reason</p></div><span class="metric-dot ${s.gaps.some(g => !g.explained) ? "alert" : s.gaps.length ? "pending" : ""}">⏸</span></article>
    </div>
    ${attentionHtml(s.attention)}
    <div class="patient-profile-grid top-gap">
      <article class="panel span-full"><div class="panel-heading"><div><p class="eyebrow">Prescription</p><h2>Phases</h2></div>${!closed ? `<button class="button secondary small" data-rt="edit-course">Edit prescription</button>` : ""}</div>
        <div class="table-wrap"><table class="rt-table"><thead><tr><th>Phase</th><th>Technique · energy</th><th>Target</th><th>Dose / #</th><th>Fractions</th><th>Total</th><th>Delivered</th><th title="${esc(s.eqd2_note)}">EQD2 α/β 10 · 3</th></tr></thead><tbody>
          ${R.phases.map(p => `<tr class="${p.id === s.current_phase_id ? "current-row" : ""}"><td><strong>${esc(p.name)}</strong>${p.id === s.current_phase_id ? ' <span class="chip chip-teal">current</span>' : p.status === "complete" ? ' <span class="chip chip-ok">done</span>' : ""}</td>
            <td>${esc([p.technique, p.energy].filter(Boolean).join(" · ") || "—")}</td><td>${esc(p.target || "—")}</td><td>${fmtNum(p.dose_per_fraction_cgy)} cGy</td>
            <td>${p.fractions_planned}</td><td>${gy(p.dose_planned_cgy)}</td><td>${p.fractions_delivered}/${p.fractions_planned} · ${gy(p.dose_delivered_cgy)}</td>
            <td class="muted">${p.eqd2.planned_ab10} · ${p.eqd2.planned_ab3} Gy</td></tr>`).join("")}
        </tbody></table></div>
        <p class="muted small">${esc(s.eqd2_note)}</p>
      </article>
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Cover sheet</p><h2>Diagnosis &amp; set-up</h2></div></div>
        <dl class="definition-grid">
          <div><dt>Diagnosis</dt><dd>${esc(c.diagnosis || R.patient.diagnosis || "—")}</dd></div>
          <div><dt>Stage</dt><dd>${esc(c.stage || "—")}</dd></div>
          <div><dt>Histopathology</dt><dd>${esc(c.histopathology || "—")}</dd></div>
          <div><dt>Intent</dt><dd>${esc(c.intent || "—")}</dd></div>
          <div><dt>Consultant</dt><dd>${esc(c.consultant || "—")}</dd></div>
          <div><dt>Referred by</dt><dd>${esc(c.referred_by || "—")}</dd></div>
          <div><dt>Site group</dt><dd>${esc(c.site_group_label)}${c.laterality ? ` · ${esc(c.laterality)}` : ""}</dd></div>
          <div><dt>Baseline weight</dt><dd>${s.baseline_weight_kg ? `${fmtNum(s.baseline_weight_kg)} kg` : "—"}</dd></div>
          ${setup.map(([k, v]) => `<div><dt>${esc(SETUP_LABEL[k] || k)}</dt><dd>${esc(v)}</dd></div>`).join("")}
        </dl>
        ${c.notes ? `<p class="panel-note">${esc(c.notes)}</p>` : ""}
      </article>
      <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Plan check</p><h2>Approval &amp; dose constraints</h2></div>${!closed ? `<button class="button secondary small" data-rt="constraints">Edit constraints</button>` : ""}</div>
        <div class="approval-line ${c.approved_at ? "ok" : "pending"}">${c.approved_at ? `✓ Approved by <strong>${esc(c.approved_oncologist)}</strong> (radiation oncologist) and <strong>${esc(c.approved_physicist)}</strong> (physicist) · ${fmtDateTime(c.approved_at)}` : `Not approved yet. Treatment cannot be recorded until the plan is approved. ${!closed ? '<button class="text-button" data-rt="approve">Approve</button>' : ""}`}</div>
        ${R.constraints.length ? `<div class="table-wrap"><table class="rt-table compact-table"><thead><tr><th>Structure</th><th>Dose to be received</th><th>Achieved</th><th>Met</th></tr></thead><tbody>
          ${R.constraints.map(k => `<tr><td><strong>${esc(k.structure)}</strong></td><td>${esc(k.constraint_text || "—")}</td><td>${esc(k.achieved_text || "—")}</td><td>${k.met === "yes" ? '<span class="chip chip-ok">✓ Met</span>' : k.met === "no" ? '<span class="chip chip-alert">✗ Not met</span>' : '<span class="chip">—</span>'}</td></tr>`).join("")}
        </tbody></table></div>` : '<div class="queue-empty">No IMRT/VMAT dose constraints recorded.</div>'}
      </article>
      <article class="panel span-full"><div class="panel-heading"><div><p class="eyebrow">Plan sheet</p><h2>Isocentre shift &amp; SSD</h2></div></div>
        <div class="table-wrap"><table class="rt-table compact-table"><thead><tr><th>Phase</th><th>Shift X</th><th>Y</th><th>Z (cm)</th><th>Couch movement</th><th>TPS SSD (AP/RL/LL)</th><th>Actual SSD</th><th>Remarks</th><th></th></tr></thead><tbody>
          ${R.phases.map(p => {
            const ssd = k => ["ap", "rl", "ll"].map(x => p.ssd?.[k]?.[x] ?? "—").join(" / ");
            return `<tr><td><strong>${esc(p.name)}</strong></td><td>${p.shift_x ?? "—"}</td><td>${p.shift_y ?? "—"}</td><td>${p.shift_z ?? "—"}</td><td>${esc(p.couch_movement || "—")}</td><td>${ssd("tps")}</td><td>${ssd("actual")}</td><td>${esc(p.remarks || "")}</td>
              <td>${!closed ? `<button class="mini-button" data-rt="phase-data" data-phase="${p.id}">Edit</button>` : ""}</td></tr>`;
          }).join("")}
        </tbody></table></div>
      </article>
      <article class="panel span-full"><div class="panel-heading"><div><p class="eyebrow">Audit trail</p><h2>Course history</h2></div></div>
        <div class="rt-events">${R.events.slice(0, 30).map(e => `<div class="rt-event"><span class="muted small">${fmtDateTime(e.created_at)}</span><span>${esc(e.summary)}</span><span class="muted small">${esc(e.actor || "")}</span></div>`).join("") || '<div class="queue-empty">No events yet.</div>'}</div>
      </article>
    </div>`;
}

function renderRtDaily() {
  const R = S.RT, c = R.course, s = R.summary;
  const current = R.phases.find(p => p.id === s.current_phase_id);
  let recorder;
  if (!c.approved_at) recorder = `<div class="queue-empty">Approve the plan (physicist and radiation oncologist) before recording treatment. <button class="text-button" data-rt="approve">Approve plan</button></div>`;
  else if (c.status === "on_hold") recorder = `<div class="queue-empty">Treatment is paused. <button class="text-button" data-rt="resume">Resume treatment</button> to record fractions.</div>`;
  else if (["completed", "discontinued"].includes(c.status)) recorder = `<div class="queue-empty">This course is closed (${esc(RT_STATUS[c.status][0].toLowerCase())}). Reopen it from Completion to record more treatment.</div>`;
  else recorder = `<form id="rtFractionForm" class="rt-fraction-form" novalidate>
      <div class="form-grid six">
        <label class="field"><span>Date</span><input type="date" name="treatment_date" value="${todayISO()}" max="${todayISO()}" required /></label>
        <label class="field"><span>Phase</span><select name="phase_id">${R.phases.map(p => `<option value="${p.id}" ${p.id === s.current_phase_id ? "selected" : ""}>${esc(p.name)} (${p.fractions_delivered}/${p.fractions_planned})</option>`).join("")}</select></label>
        <label class="field"><span>Dose (cGy)</span><input type="number" name="dose_cgy" min="1" step="any" placeholder="${current ? fmtNum(current.dose_per_fraction_cgy) : ""}" /></label>
        <label class="field"><span>MU</span><input type="number" name="mu" min="0" step="any" /></label>
        <label class="field"><span>RTT (sign)</span><input name="therapist" maxlength="60" placeholder="Initials" required /></label>
        <label class="field"><span>Note</span><input name="note" maxlength="500" placeholder="Required if dose differs" /></label>
      </div>
      <div class="rt-fraction-foot">
        <label class="confirm-check"><input type="checkbox" name="extra" /><span>Extra fraction (gap compensation)</span></label>
        <label class="confirm-check"><input type="checkbox" name="allow_same_day" /><span>Second fraction on the same day</span></label>
        <span class="muted" id="rtFractionPreview">${current ? `Fraction ${s.fractions_delivered + 1} of ${s.fractions_planned} · ${esc(current.name)} ${current.fractions_delivered + 1}/${current.fractions_planned} · cumulative after this ${gy(s.dose_delivered_cgy + current.dose_per_fraction_cgy)}` : "All planned fractions are delivered."}</span>
        <span class="spacer"></span>
        <p class="form-error" role="alert" hidden></p>
        <button class="button primary" type="submit">Record fraction</button>
      </div>
    </form>`;
  const unexplained = s.gaps.filter(g => !g.explained);
  const byDate = new Map();
  for (const f of R.fractions) byDate.set(f.id, f);
  const rows = [...R.fractions.map(f => ({ type: "f", date: f.treatment_date, f })), ...s.gaps.map(g => ({ type: "g", date: g.date, g }))]
    .sort((a, b) => a.date.localeCompare(b.date) || (a.type === "g" ? -1 : 1));
  const lastId = R.fractions.at(-1)?.id;
  $("#rtDaily").innerHTML = `
    <div class="metric-grid">
      <article class="metric-card"><div><small>Fractions</small><strong>${s.fractions_delivered}/${s.fractions_planned}</strong><p>${s.fractions_remaining} remaining</p></div><span class="metric-dot">#</span></article>
      <article class="metric-card"><div><small>Cumulative dose</small><strong>${gy(s.dose_delivered_cgy)}</strong><p>of ${gy(s.dose_planned_cgy)}</p></div><span class="metric-dot">Σ</span></article>
      <article class="metric-card"><div><small>Next fraction</small><strong class="small-strong">${s.next_fraction_date ? fmtDate(s.next_fraction_date) : "—"}</strong><p>treatment days: ${esc(c.treatment_days_label)}</p></div><span class="metric-dot">→</span></article>
      <article class="metric-card"><div><small>Projected last fraction</small><strong class="small-strong">${fmtDate(c.completion?.end_date || s.projected_end_date)}</strong><p>${s.overall_treatment_days ? `overall ${s.overall_treatment_days} d (${s.planned_treatment_days} planned)` : ""}</p></div><span class="metric-dot ${s.overall_treatment_days > s.planned_treatment_days ? "pending" : ""}">⏱</span></article>
    </div>
    <article class="panel"><div class="panel-heading"><div><p class="eyebrow">Daily treatment sheet</p><h2>Record a fraction</h2></div><span class="muted">Running totals are computed from the log</span></div>${recorder}</article>
    ${unexplained.length ? `<article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Interruptions</p><h2>Missed treatment days without a reason</h2></div><span class="count-badge warning">${unexplained.length}</span></div>
      ${unexplained.map(g => `<form class="gap-row" data-rt-break="${g.date}"><strong>${g.weekday} ${fmtDate(g.date)}</strong>
        <select name="category" required><option value="">Why was treatment not given?</option>${R.options.break_categories.map(x => `<option>${esc(x)}</option>`).join("")}</select>
        <input name="reason" maxlength="300" placeholder="Details (optional)" /><button class="button secondary small" type="submit">Save</button></form>`).join("")}
    </article>` : ""}
    <article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Radiation therapy summary</p><h2>Treatment record</h2></div><span class="muted">${R.fractions.length} fractions${s.gaps.length ? ` · ${s.gaps.length} missed day(s)` : ""}</span></div>
      ${rows.length ? `<div class="table-wrap"><table class="rt-table rt-log"><thead><tr><th>#</th><th>Date</th><th>Phase</th><th>Dose (cGy)</th><th>Phase total</th><th>Course total</th><th>MU</th><th>RTT</th><th>Note</th><th></th></tr></thead><tbody>
        ${rows.map(r => r.type === "g" ? `<tr class="gap-line ${r.g.explained ? "" : "unexplained"}"><td>—</td><td>${r.g.weekday} ${fmtShort(r.g.date)}</td><td colspan="7">Not treated · ${esc(r.g.category || "no reason recorded")}${r.g.reason ? ` — ${esc(r.g.reason)}` : ""}</td><td></td></tr>`
          : `<tr class="${r.f.extra ? "extra-line" : ""}"><td><strong>${r.f.number}</strong></td><td>${new Date(`${r.f.treatment_date}T00:00:00`).toLocaleDateString("en-GB", { weekday: "short" })} ${fmtShort(r.f.treatment_date)}</td>
            <td>${esc(R.phases.find(p => p.id === r.f.phase_id)?.name || `Phase ${r.f.phase_number}`)} · ${r.f.phase_fraction}</td><td>${fmtNum(r.f.dose_cgy)}</td><td>${fmtNum(r.f.phase_total_cgy)}</td><td><strong>${fmtNum(r.f.course_total_cgy)}</strong></td>
            <td>${r.f.mu != null ? fmtNum(r.f.mu) : "—"}</td><td>${esc(r.f.therapist || "")}</td><td class="muted small">${r.f.extra ? '<span class="chip chip-warn">extra</span> ' : ""}${esc(r.f.note || "")}</td>
            <td>${r.f.id === lastId && c.status !== "completed" ? `<button class="icon-action danger" data-rt="delete-fraction" data-fraction="${r.f.id}" title="Remove this entry" aria-label="Remove fraction">🗑</button>` : ""}</td></tr>`).join("")}
      </tbody></table></div>` : '<div class="queue-empty">No fractions recorded yet.</div>'}
    </article>`;
}

function rowMaxGrade(review, row) {
  const items = review.items.filter(i => i.row_key === row.key);
  if (!items.length) return null;
  return items.reduce((m, i) => (i.grade > m.grade ? i : m), items[0]);
}
function renderRtWeekly() {
  const R = S.RT, s = R.summary;
  const reviews = R.reviews;
  const rows = R.reaction_rows;
  const head = reviews.map(r => `<th>Week ${r.week_number}<small>${fmtShort(r.review_date)}</small></th>`).join("");
  const matrix = reviews.length ? `<div class="table-wrap"><table class="rt-matrix"><thead><tr><th>Toxicities</th>${head}</tr></thead><tbody>
      ${rows.map(row => {
        const cells = reviews.map(rv => {
          const items = rv.items.filter(i => i.row_key === row.key);
          if (!items.length) return '<td class="muted">—</td>';
          const worst = rowMaxGrade(rv, row);
          return `<td title="${esc(items.map(i => `${i.ctcae_term} G${i.grade}${i.note ? ` — ${i.note}` : ""}`).join("\n"))}">${worst.grade ? gradeBadge(worst.grade, "small") : '<span class="g0">✓</span>'}${worst.grade ? `<small>${esc(worst.ctcae_term.replace("Dermatitis radiation", "Dermatitis"))}</small>` : ""}</td>`;
        }).join("");
        return `<tr class="${row.relevant ? "" : "dim"}"><th scope="row">${esc(row.label)}${row.relevant ? "" : ' <span class="muted small">(not expected)</span>'}</th>${cells}</tr>`;
      }).join("")}
      <tr><th scope="row">Weight</th>${reviews.map(rv => `<td>${rv.weight_kg ? `<strong>${fmtNum(rv.weight_kg)}</strong> kg` : "—"}</td>`).join("")}</tr>
      <tr><th scope="row">Decision</th>${reviews.map(rv => `<td><span class="chip ${rv.decision === "continue" ? "chip-ok" : rv.decision === "continue_supportive" ? "chip-teal" : "chip-alert"}">${esc(rv.decision_label)}</span></td>`).join("")}</tr>
      <tr><th scope="row"></th>${reviews.map(rv => `<td><button class="mini-button" data-rt="edit-review" data-review="${rv.id}">Open</button></td>`).join("")}</tr>
    </tbody></table></div>` : '<div class="queue-empty">No weekly review yet. Record the first review in week 1.</div>';
  const wc = s.weight_change;
  const reports = (S.rtReports || []).filter(r => r.status === "new");
  $("#rtWeekly").innerHTML = `
    <div class="metric-grid">
      <article class="metric-card"><div><small>Next review</small><strong>Week ${s.next_review_week}</strong><p>${s.review_due ? "due now" : s.latest_review ? `last ${fmtShort(s.latest_review.date)}` : "not started"}</p></div><span class="metric-dot ${s.review_due ? "pending" : ""}">◇</span></article>
      <article class="metric-card"><div><small>Worst reaction so far</small><strong class="small-strong">${s.worst_toxicities[0] ? `${esc(s.worst_toxicities[0].term)} G${s.worst_toxicities[0].grade}` : "None"}</strong><p>${s.worst_toxicities[0] ? `week ${s.worst_toxicities[0].week}` : "clinician-confirmed"}</p></div><span class="metric-dot ${(s.worst_toxicities[0]?.grade || 0) >= 3 ? "alert" : (s.worst_toxicities[0]?.grade || 0) >= 2 ? "pending" : ""}">!</span></article>
      <article class="metric-card"><div><small>Weight</small><strong>${wc ? `${fmtNum(wc.current)} kg` : "—"}</strong><p>${wc ? `${wc.change_pct > 0 ? "+" : ""}${wc.change_pct}% from ${fmtNum(wc.baseline)} kg${wc.ctcae_grade ? ` · weight loss G${wc.ctcae_grade}` : ""}` : "record at each review"}</p></div><span class="metric-dot ${wc?.ctcae_grade >= 2 ? "alert" : wc?.ctcae_grade ? "pending" : ""}">⚖</span></article>
      <article class="metric-card"><div><small>Patient messages</small><strong>${reports.length}</strong><p>awaiting review</p></div><span class="metric-dot ${reports.some(r => r.red_flags.length) ? "alert" : reports.length ? "pending" : ""}">✉</span></article>
    </div>
    ${reports.length ? `<article class="panel"><div class="panel-heading"><div><p class="eyebrow">Between reviews</p><h2>Patient messages awaiting review</h2></div><a class="text-button" href="#inbox">Open inbox</a></div>
      <div class="report-list">${reports.map(r => reportCardHtml(r, {}).replace("</footer>", `<button class="button primary small" data-rt="review-from-report" data-report="${r.id}">Use in week ${s.next_review_week} review</button></footer>`)).join("")}</div></article>` : ""}
    <article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Early reactions during radiation therapy</p><h2>Reactions by week</h2></div><div class="legend-inline"><span>${gradeBadge(1, "small")} CTCAE grade (worst in row)</span><span><span class="g0">✓</span> assessed, none</span></div></div>${matrix}</article>
    ${reviews.length ? `<article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Signed reviews</p><h2>Decisions and notes</h2></div></div>
      ${[...reviews].reverse().map(rv => `<div class="rt-review-line"><div><strong>Week ${rv.week_number} · ${fmtDate(rv.review_date)}</strong> <span class="chip ${rv.decision === "continue" ? "chip-ok" : rv.decision === "continue_supportive" ? "chip-teal" : "chip-alert"}">${esc(rv.decision_label)}</span>
          <p>${rv.items.filter(i => i.grade).map(i => `${esc(i.ctcae_term)} G${i.grade}${i.note ? ` (${esc(i.note)})` : ""}`).join(" · ") || "No acute reactions"}${rv.weight_kg ? ` · ${fmtNum(rv.weight_kg)} kg` : ""}</p>
          ${rv.rationale ? `<p class="muted">${rv.reason_category ? `${esc(rv.reason_category)}: ` : ""}${esc(rv.rationale)}</p>` : ""}${rv.notes ? `<p class="muted">${esc(rv.notes)}</p>` : ""}</div>
        <div class="muted small">${esc(rv.reviewed_by)}<br><button class="mini-button" data-rt="edit-review" data-review="${rv.id}">Edit</button></div></div>`).join("")}</article>` : ""}`;
  if (!S.rtReports) loadRtReports();
}
async function loadRtReports() {
  if (!S.RT) return;
  try {
    S.rtReports = await api(`/api/patients/${S.RT.patient.id}/patient-reports`);
    if (S.view === "rt-weekly") renderRtWeekly();
  } catch { S.rtReports = []; }
}

function renderRtComplete() {
  const R = S.RT, c = R.course, s = R.summary;
  const done = c.completion;
  const stats = done ? done : { start_date: s.first_fraction_date, end_date: s.last_fraction_date, fractions_delivered: s.fractions_delivered, fractions_planned: s.fractions_planned,
    dose_delivered_cgy: s.dose_delivered_cgy, dose_planned_cgy: s.dose_planned_cgy, overall_treatment_days: s.overall_treatment_days, missed_days: s.gaps.length };
  const GAP = { none: "None", not_needed: "Not needed", extra_fractions: "Extra fractions given", other: "Other (see notes)" };
  $("#rtComplete").innerHTML = `
    <article class="panel"><div class="panel-heading"><div><p class="eyebrow">${done ? "Signed completion summary" : "From the treatment record"}</p><h2>${done ? esc(done.outcome_label) : "Treatment so far"}</h2></div>${done ? rtPill(c.status) : ""}</div>
      <dl class="definition-grid four">
        <div><dt>Date from</dt><dd>${fmtDate(stats.start_date)}</dd></div><div><dt>To</dt><dd>${fmtDate(stats.end_date)}</dd></div>
        <div><dt>Fractions</dt><dd>${stats.fractions_delivered} of ${stats.fractions_planned}</dd></div><div><dt>Final total dose</dt><dd>${gy(stats.dose_delivered_cgy)} of ${gy(stats.dose_planned_cgy)}</dd></div>
        <div><dt>Overall treatment time</dt><dd>${stats.overall_treatment_days ?? "—"} days</dd></div><div><dt>Missed days</dt><dd>${stats.missed_days}</dd></div>
        ${done ? `<div><dt>Gap correction</dt><dd>${esc(GAP[done.gap_correction] || done.gap_correction)}</dd></div><div><dt>Signed</dt><dd>${esc(done.signed_by)} · ${fmtDate(done.signed_at)}</dd></div>` : ""}
      </dl>
      ${(done?.phases || R.phases).map(p => `<div class="mini-data"><span>${esc(p.name)}</span><span>${p.delivered ?? p.fractions_delivered}/${p.planned ?? p.fractions_planned} fractions</span><span>${gy(p.dose_delivered_cgy)}</span></div>`).join("")}
      ${done?.notes ? `<p class="panel-note">${esc(done.notes)}</p>` : ""}
      ${done ? `<div class="modal-actions"><button class="button secondary" data-rt="reopen">Reopen course</button><button class="button primary" data-rec="new-discharge" data-kind="end_of_treatment">Create end-of-treatment summary</button></div>` : ""}
    </article>
    ${done ? "" : s.fractions_delivered ? `<article class="panel top-gap"><div class="panel-heading"><div><p class="eyebrow">Sign off</p><h2>Complete the course</h2></div></div>
      <form id="rtCompleteForm" novalidate>
        <fieldset class="outcome-options compact-options"><legend>How did the course end?</legend>
          ${Object.entries(R.options.completion_outcomes).map(([k, v]) => `<label><input type="radio" name="outcome" value="${k}" ${k === (s.fractions_remaining ? "completed_modified" : "completed_as_planned") ? "checked" : ""} /><span><strong>${esc(v)}</strong></span></label>`).join("")}
        </fieldset>
        <div class="form-grid three">
          <label class="field"><span>Last treatment date</span><input type="date" name="end_date" value="${s.last_fraction_date || ""}" /></label>
          <label class="field"><span>Gap correction</span><select name="gap_correction"><option value="none">None (no interruptions)</option><option value="not_needed" ${s.gaps.length ? "selected" : ""}>Interruptions; not needed</option><option value="extra_fractions">Extra fractions given</option><option value="other">Other</option></select></label>
          <label class="field"><span>Signed by</span><input name="signed_by" value="${esc(clinicianName())}" maxlength="80" /></label>
          <label class="field span-3"><span>Notes</span><textarea name="notes" rows="2" maxlength="2000" placeholder="Required if the course changed or stopped early"></textarea></label>
        </div>
        <p class="form-error" role="alert" hidden></p>
        <div class="modal-actions"><button class="button primary" type="submit">Sign completion summary</button></div>
      </form></article>` : '<div class="queue-empty top-gap">No fractions recorded yet.</div>'}`;
}

/* ------------------------------------------------------------------ course form */
function phaseRowHtml(p, i, count) {
  const opts = S.rtOptions || { techniques: [], energies: [] };
  return `<div class="med-row rt-phase-row" data-rt-phase="${i}">
    <label class="field"><span>Phase</span><input data-f="name" value="${esc(p.name || "")}" maxlength="120" placeholder="Phase ${i + 1}" /></label>
    <label class="field"><span>Technique</span><input data-f="technique" list="rtTechList" value="${esc(p.technique || "")}" maxlength="60" /></label>
    <label class="field"><span>Energy</span><input data-f="energy" list="rtEnergyList" value="${esc(p.energy || "")}" maxlength="40" /></label>
    <label class="field"><span>Target</span><input data-f="target" value="${esc(p.target || "")}" maxlength="200" /></label>
    <label class="field"><span>cGy / #</span><input data-f="dose_per_fraction_cgy" type="number" min="1" max="2500" step="any" value="${esc(p.dose_per_fraction_cgy ?? "")}" required /></label>
    <label class="field"><span>Fractions</span><input data-f="fractions_planned" type="number" min="1" max="60" step="1" value="${esc(p.fractions_planned ?? "")}" required /></label>
    <button type="button" class="icon-action danger remove-med" data-rt="remove-phase" data-index="${i}" ${count > 1 && !p.delivered ? "" : "disabled"} aria-label="Remove phase">×</button>
  </div>`;
}
function renderPhaseRows() {
  const phases = S.rtDraft.phases;
  $("#rtPhaseRows").innerHTML = phases.map((p, i) => phaseRowHtml(p, i, phases.length)).join("");
  updateRtTotal();
}
function updateRtTotal() {
  const phases = S.rtDraft.phases.filter(p => Number(p.dose_per_fraction_cgy) > 0 && Number(p.fractions_planned) > 0);
  if (!phases.length) { $("#rtTotal").textContent = ""; return; }
  const total = phases.reduce((n, p) => n + Number(p.dose_per_fraction_cgy) * Number(p.fractions_planned), 0);
  const fx = phases.reduce((n, p) => n + Number(p.fractions_planned), 0);
  $("#rtTotal").innerHTML = `Prescription: <strong>${phases.map(p => `${gy(Number(p.dose_per_fraction_cgy) * Number(p.fractions_planned))}/${p.fractions_planned}#`).join(" + ")}</strong> = ${gy(total)} in ${fx} fractions`;
}
async function openCourseForm(edit = false) {
  await rtOptions();
  const form = $("#rtCourseForm");
  form.reset();
  formError(form, "");
  const o = S.rtOptions;
  form.elements.site_group.innerHTML = Object.entries(o.site_groups).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  const plans = S.patient?.treatment_plans || [];
  form.elements.chemo_plan_id.innerHTML = `<option value="">None</option>${plans.map(p => `<option value="${p.id}">${esc(p.regimen_name)} (${esc(p.status)})</option>`).join("")}`;
  if (!$("#rtTechList")) document.body.insertAdjacentHTML("beforeend", `<datalist id="rtTechList">${o.techniques.map(x => `<option value="${esc(x)}">`).join("")}</datalist><datalist id="rtEnergyList">${o.energies.map(x => `<option value="${esc(x)}">`).join("")}</datalist>`);
  const c = edit ? S.RT.course : null;
  const days = c ? c.treatment_days_list : [1, 2, 3, 4, 5];
  $("#rtDays").innerHTML = DAYS.map(([n, label]) => `<label class="day-chip"><input type="checkbox" name="day" value="${n}" ${days.includes(n) ? "checked" : ""} /><span>${label}</span></label>`).join("");
  $("#rtCourseTitle").textContent = edit ? "Edit radiotherapy course" : "New radiotherapy course";
  $("#rtCourseSubmit").textContent = edit ? "Save changes" : "Create course";
  if (c) {
    for (const k of ["site", "site_group", "laterality", "intent", "machine", "diagnosis", "stage", "histopathology", "consultant", "referred_by", "start_date", "baseline_weight_kg", "notes"]) form.elements[k].value = c[k] ?? "";
    form.elements.chemo_plan_id.value = c.chemo_plan_id || "";
    for (const k of ["head_rest", "arm_position", "immobilisation", "bladder", "bolus_cm", "knee_rest", "special_instructions"]) form.elements[k].value = c.setup?.[k] || "";
    S.rtDraft = { edit: true, phases: S.RT.phases.map(p => ({ name: p.name, technique: p.technique, energy: p.energy, target: p.target, dose_per_fraction_cgy: p.dose_per_fraction_cgy, fractions_planned: p.fractions_planned, delivered: p.fractions_delivered })) };
  } else {
    form.elements.diagnosis.value = S.patient?.diagnosis || "";
    form.elements.consultant.value = clinicianName();
    form.elements.start_date.value = todayISO();
    S.rtDraft = { edit: false, phases: [{ name: "Phase I", technique: "VMAT", energy: "6MV", dose_per_fraction_cgy: "", fractions_planned: "" }] };
  }
  $("#rtSetupBlock").open = Boolean(c && Object.keys(c.setup || {}).length);
  renderPhaseRows();
  openModal("rtCourseModal");
}
async function submitCourse(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const f = form.elements;
  if (!f.site.value.trim()) return formError(form, "Enter the site / target volume.");
  const phases = S.rtDraft.phases.map((p, i) => ({ name: String(p.name || "").trim() || null, technique: String(p.technique || "").trim() || null, energy: String(p.energy || "").trim() || null,
    target: String(p.target || "").trim() || null, dose_per_fraction_cgy: Number(p.dose_per_fraction_cgy), fractions_planned: Number(p.fractions_planned), phase_number: i + 1 }));
  if (phases.some(p => !(p.dose_per_fraction_cgy > 0) || !Number.isInteger(p.fractions_planned) || p.fractions_planned < 1)) return formError(form, "Every phase needs a dose per fraction (cGy) and a whole number of fractions.");
  const days = $$("input[name=day]:checked", form).map(x => Number(x.value));
  if (!days.length) return formError(form, "Choose at least one treatment day.");
  const setup = {};
  for (const k of ["head_rest", "arm_position", "immobilisation", "bladder", "bolus_cm", "knee_rest", "special_instructions"]) if (f[k].value.trim()) setup[k] = f[k].value.trim();
  const body = { site: f.site.value.trim(), site_group: f.site_group.value, laterality: f.laterality.value || null, intent: f.intent.value || null, machine: f.machine.value, diagnosis: f.diagnosis.value,
    stage: f.stage.value, histopathology: f.histopathology.value, consultant: f.consultant.value, referred_by: f.referred_by.value, start_date: f.start_date.value || null,
    baseline_weight_kg: f.baseline_weight_kg.value || null, chemo_plan_id: f.chemo_plan_id.value || null, treatment_days: days, setup, notes: f.notes.value, phases };
  await withBusy(form, async () => {
    const data = S.rtDraft.edit ? await patch(`/api/rt-courses/${S.RT.course.id}`, body) : await post(`/api/patients/${S.patient.id}/rt-courses`, body);
    closeModals();
    setRt(data);
    showToast(S.rtDraft.edit ? "Course updated." : "Radiotherapy course created. Approve the plan before treatment.");
    if (!S.rtDraft.edit) navigate(`#rt/${data.course.id}/plan`);
    else await loadPatient(data.patient.id);
  });
}

/* ------------------------------------------------------------------ small dialogs */
function rtDialog({ eyebrow = "Radiation oncology", title, sub = "", body, submit = "Save", onSubmit }) {
  $("#rtDialogEyebrow").textContent = eyebrow;
  $("#rtDialogTitle").textContent = title;
  $("#rtDialogSub").textContent = sub;
  $("#rtDialogBody").innerHTML = body;
  $("#rtDialogSubmit").textContent = submit;
  formError($("#rtDialogForm"), "");
  S.rtDialogSubmit = onSubmit;
  openModal("rtDialog");
}
function openApprove() {
  const R = S.RT;
  rtDialog({ eyebrow: "Plan approval", title: "Approve radiotherapy plan", sub: `${R.course.site} · ${R.summary.prescription}`, submit: "Approve plan",
    body: `<div class="form-grid"><label class="field"><span>Physicist</span><input name="physicist" maxlength="80" required /></label>
      <label class="field"><span>Radiation oncologist</span><input name="oncologist" maxlength="80" value="${esc(clinicianName())}" /></label></div>
      ${R.constraints.some(c => c.met === "no") ? '<p class="check-alert">One or more dose constraints are marked as not met.</p>' : ""}
      <label class="confirm-check"><input type="checkbox" name="confirmed" /><span>I reviewed the prescription, dose constraints and set-up data.</span></label>
      ${waTick(S.patient, "Send the radiotherapy plan to the patient on WhatsApp")}`,
    onSubmit: async f => {
      if (!f.confirmed.checked) throw new Error("Confirm that you reviewed the plan.");
      const data = await rtCall("POST", `/api/rt-courses/${R.course.id}/approve`, { physicist: f.physicist.value, oncologist: f.oncologist.value, notify_whatsapp: f.notify_whatsapp.checked });
      showToast(`Plan approved. Treatment can be recorded.${waResultText(data.whatsapp)}`);
    } });
}
function constraintRow(c = {}, i) {
  return `<div class="med-row rt-constraint-row" data-constraint="${i}">
    <label class="field"><span>Structure</span><input name="structure" value="${esc(c.structure || "")}" maxlength="120" /></label>
    <label class="field"><span>Dose to be received</span><input name="constraint_text" value="${esc(c.constraint_text || "")}" maxlength="200" placeholder="e.g. V20Gy < 30%" /></label>
    <label class="field"><span>Achieved</span><input name="achieved_text" value="${esc(c.achieved_text || "")}" maxlength="200" /></label>
    <label class="field"><span>Met</span><select name="met"><option value="">—</option><option value="yes" ${c.met === "yes" ? "selected" : ""}>Yes</option><option value="no" ${c.met === "no" ? "selected" : ""}>No</option><option value="na" ${c.met === "na" ? "selected" : ""}>N/A</option></select></label>
  </div>`;
}
function openConstraints() {
  const R = S.RT;
  const rows = [...R.constraints, {}, {}];
  rtDialog({ eyebrow: "IMRT / VMAT", title: "Dose constraints", sub: "Leave a structure empty to remove the row.", submit: "Save constraints",
    body: `<div id="constraintRows" class="med-rows">${rows.map(constraintRow).join("")}</div><button type="button" class="button secondary small" data-rt="add-constraint">+ Add row</button>`,
    onSubmit: async () => {
      const items = $$(".rt-constraint-row").map(row => Object.fromEntries($$("input,select", row).map(el => [el.name, el.value.trim()])));
      await rtCall("PATCH", `/api/rt-courses/${R.course.id}`, { constraints: items.filter(i => i.structure) }, "Constraints saved.");
    } });
}
function openPhaseData(phaseId) {
  const p = S.RT.phases.find(x => x.id === phaseId);
  const v = (k, side) => p.ssd?.[k]?.[side] ?? "";
  rtDialog({ eyebrow: "Plan sheet", title: `${p.name}: isocentre & SSD`, submit: "Save",
    body: `<p class="eyebrow">Isocentre shift (cm)</p><div class="form-grid three"><label class="field"><span>X</span><input name="shift_x" type="number" step="any" value="${p.shift_x ?? ""}" /></label>
      <label class="field"><span>Y</span><input name="shift_y" type="number" step="any" value="${p.shift_y ?? ""}" /></label><label class="field"><span>Z</span><input name="shift_z" type="number" step="any" value="${p.shift_z ?? ""}" /></label></div>
      <label class="field"><span>Couch movement</span><input name="couch_movement" maxlength="300" value="${esc(p.couch_movement || "")}" /></label>
      <p class="eyebrow">SSD (cm)</p><div class="form-grid three">${["tps", "actual"].map(k => ["ap", "rl", "ll"].map(side => `<label class="field"><span>${k === "tps" ? "TPS" : "Actual"} ${side.toUpperCase()}</span><input name="${k}_${side}" type="number" step="any" value="${v(k, side)}" /></label>`).join("")).join("")}</div>
      <label class="field"><span>Remarks</span><input name="remarks" maxlength="300" value="${esc(p.remarks || "")}" /></label>`,
    onSubmit: async f => {
      const ssd = { tps: {}, actual: {} };
      for (const k of ["tps", "actual"]) for (const side of ["ap", "rl", "ll"]) if (f[`${k}_${side}`].value !== "") ssd[k][side] = Number(f[`${k}_${side}`].value);
      await rtCall("PATCH", `/api/rt-phases/${phaseId}`, { shift_x: f.shift_x.value, shift_y: f.shift_y.value, shift_z: f.shift_z.value, couch_movement: f.couch_movement.value, remarks: f.remarks.value, ssd }, "Plan data saved.");
    } });
}
function openPause() {
  const R = S.RT;
  rtDialog({ eyebrow: "Course status", title: "Pause or stop radiotherapy", submit: "Save",
    body: `<fieldset class="outcome-options compact-options"><legend>Change</legend>
      <label><input type="radio" name="status" value="on_hold" checked /><span><strong>Pause treatment</strong><small>Fractions cannot be recorded until resumed</small></span></label>
      <label><input type="radio" name="status" value="discontinued" /><span><strong>Stop radiotherapy</strong><small>Closes the course; record the completion summary</small></span></label></fieldset>
      <label class="field"><span>Reason</span><textarea name="reason" rows="2" maxlength="1000" required></textarea></label>
      ${waTick(S.patient, "Tell the patient on WhatsApp")}`,
    onSubmit: async f => {
      const data = await rtCall("PATCH", `/api/rt-courses/${R.course.id}`, { status: f.status.value, status_reason: f.reason.value, notify_whatsapp: f.notify_whatsapp.checked });
      showToast(`${f.status.value === "on_hold" ? "Treatment paused." : "Radiotherapy stopped."}${waResultText(data.whatsapp)}`);
    } });
}
function openReopen() {
  rtDialog({ title: "Reopen course", submit: "Reopen", body: `<label class="field"><span>Reason</span><textarea name="reason" rows="2" maxlength="500" required></textarea></label>`,
    onSubmit: async f => { await rtCall("POST", `/api/rt-courses/${S.RT.course.id}/reopen`, { reason: f.reason.value }, "Course reopened."); } });
}

/* ------------------------------------------------------------------ weekly review form */
function openReview(reviewId = null, prefill = null) {
  const R = S.RT;
  if (!R) return;
  const form = $("#rtReviewForm");
  form.reset();
  formError(form, "");
  const review = reviewId ? R.reviews.find(r => r.id === reviewId) : null;
  S.rtReviewId = review?.id || null;
  const last = R.reviews.at(-1);
  const lastGrade = (term) => last?.items.find(i => i.ctcae_term === term)?.grade;
  const current = new Map((review?.items || []).map(i => [`${i.row_key}:${i.ctcae_term}`, i]));
  if (prefill) for (const [term, grade, note] of prefill.items) {
    const row = R.reaction_rows.find(r => r.terms.includes(term));
    if (row) current.set(`${row.key}:${term}`, { grade: Number(grade) || 0, note });
  }
  $("#rtReviewTitle").textContent = review ? `Week ${review.week_number} review` : `Week ${R.summary.next_review_week} review`;
  $("#rtReviewSub").textContent = `${patientName(R.patient)} · ${R.course.site} · ${R.summary.fractions_delivered}/${R.summary.fractions_planned} fractions, ${gy(R.summary.dose_delivered_cgy)}`;
  form.elements.review_date.value = review?.review_date || todayISO();
  form.elements.review_date.max = todayISO();
  form.elements.week_number.value = review?.week_number || R.summary.next_review_week;
  form.elements.weight_kg.value = review?.weight_kg ?? "";
  form.elements.reviewed_by.value = review?.reviewed_by || clinicianName();
  form.elements.rationale.value = review?.rationale || prefill?.rationale || "";
  form.elements.notes.value = review?.notes || prefill?.notes || "";
  form.elements.reason_category.innerHTML = `<option value="">Select reason</option>${R.options.reason_categories.map(x => `<option ${review?.reason_category === x ? "selected" : ""}>${esc(x)}</option>`).join("")}`;
  const termBlock = (row, term) => {
    const key = `${row.key}:${term}`;
    const item = current.get(key);
    const grades = Object.keys(row.criteria[term]).map(Number).filter(g => g <= 4);
    const sel = item ? item.grade : (row.relevant ? 0 : null);
    const prev = lastGrade(term);
    return `<div class="rt-term" data-row="${row.key}" data-term="${esc(term)}">
      <div class="rt-term-head"><strong>${esc(term)}</strong>${prev != null && !review ? `<span class="muted small">last week: ${prev ? `G${prev}` : "none"}</span>` : ""}</div>
      <div class="grade-pills">${[0, ...grades].map(g => `<label><input type="radio" name="g:${key}" value="${g}" ${sel === g ? "checked" : ""} /><span>${g ? `G${g}` : "None"}</span></label>`).join("")}</div>
      <p class="rt-criterion" data-criterion>${sel ? esc(row.criteria[term][sel]) : ""}</p>
      <input class="rt-term-note" name="n:${key}" maxlength="300" placeholder="Note (optional)" value="${esc(item?.note || "")}" />
    </div>`;
  };
  const rowBlock = row => `<section class="rt-row ${row.relevant ? "relevant" : ""}"><h3>${esc(row.label)}</h3>${row.terms.map(t => termBlock(row, t)).join("")}</section>`;
  const relevant = R.reaction_rows.filter(r => r.relevant), other = R.reaction_rows.filter(r => !r.relevant);
  const hasOther = other.some(r => r.terms.some(t => current.has(`${r.key}:${t}`)));
  $("#rtReviewRows").innerHTML = `${relevant.map(rowBlock).join("")}
    <details class="rt-other" ${hasOther ? "open" : ""}><summary>Other rows of the sheet <span class="muted small">(not expected for ${esc(R.course.site_group_label.toLowerCase())}; graded only if you choose a grade)</span></summary>${other.map(rowBlock).join("")}</details>
    ${prefill?.source ? `<p class="muted small">Pre-filled from ${esc(prefill.source)}. Suggested grades are not confirmed until you sign.</p>` : ""}`;
  const decision = review?.decision || "continue";
  $("#rtDecisionOptions").innerHTML = Object.entries(R.options.decisions).map(([k, v]) => `<label><input type="radio" name="decision" value="${k}" ${k === decision ? "checked" : ""} /><span>${esc(v)}</span></label>`).join("");
  $("[data-rt=delete-review]").classList.toggle("hidden", !review);
  updateReviewForm();
  openModal("rtReviewModal");
}
function updateReviewForm() {
  const form = $("#rtReviewForm");
  const decision = form.elements.decision?.value || "continue";
  $("#rtDecisionReason").classList.toggle("muted-block", decision === "continue");
  form.elements.reason_category.closest(".field").classList.toggle("hidden", ["continue", "continue_supportive"].includes(decision));
  const w = Number(form.elements.weight_kg.value);
  const base = S.RT?.summary.baseline_weight_kg;
  if (w && base) {
    const pct = Math.round(((w - base) / base) * 1000) / 10;
    const loss = -pct, g = loss >= 20 ? 3 : loss >= 10 ? 2 : loss >= 5 ? 1 : 0;
    $("#rtWeightNote").textContent = `${pct > 0 ? "+" : ""}${pct}% from baseline ${fmtNum(base)} kg${g ? ` · CTCAE weight loss G${g}` : ""}`;
  } else $("#rtWeightNote").textContent = base ? `Baseline ${fmtNum(base)} kg` : "First weight becomes the baseline";
  for (const block of $$(".rt-term", form)) {
    const checked = $("input[type=radio]:checked", block);
    const row = S.RT.reaction_rows.find(r => r.key === block.dataset.row);
    const g = checked ? Number(checked.value) : 0;
    $("[data-criterion]", block).textContent = g ? row.criteria[block.dataset.term][g] : "";
    block.classList.toggle("graded", g > 0);
  }
}
async function submitReview(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.elements.confirmed.checked) return formError(form, "Confirm that you examined the patient and reviewed the criteria.");
  const items = [];
  for (const block of $$(".rt-term", form)) {
    const checked = $("input[type=radio]:checked", block);
    if (!checked) continue;
    const relevant = block.closest(".rt-row").classList.contains("relevant");
    const grade = Number(checked.value);
    if (!relevant && !grade) continue;
    items.push({ row_key: block.dataset.row, ctcae_term: block.dataset.term, grade, note: $(".rt-term-note", block).value.trim() || null });
  }
  const f = form.elements;
  const body = { review_date: f.review_date.value, week_number: f.week_number.value || null, weight_kg: f.weight_kg.value || null, items, decision: f.decision.value,
    reason_category: f.reason_category.value || null, rationale: f.rationale.value.trim() || null, reviewed_by: f.reviewed_by.value.trim(), notes: f.notes.value.trim() || null };
  await withBusy(form, async () => {
    const R = S.RT;
    const data = S.rtReviewId ? await patch(`/api/rt-courses/${R.course.id}/reviews/${S.rtReviewId}`, body) : await post(`/api/rt-courses/${R.course.id}/reviews`, body);
    closeModals();
    setRt(data);
    showToast(`Week ${body.week_number || ""} review signed.${["pause", "stop"].includes(body.decision) ? " Course status updated." : ""}`);
  });
}

/* ------------------------------------------------------------------ events */
document.addEventListener("click", async event => {
  const el = event.target.closest("[data-rt]");
  if (!el) return;
  const action = el.dataset.rt;
  const R = S.RT;
  try {
    switch (action) {
      case "new-course": return openCourseForm(false);
      case "edit-course": return openCourseForm(true);
      case "add-phase": S.rtDraft.phases.push({ name: `Phase ${["I", "II", "III", "IV", "V", "VI"][S.rtDraft.phases.length] || S.rtDraft.phases.length + 1}`, technique: "", energy: "", dose_per_fraction_cgy: "", fractions_planned: "" }); return renderPhaseRows();
      case "remove-phase": S.rtDraft.phases.splice(Number(el.dataset.index), 1); return renderPhaseRows();
      case "approve": return openApprove();
      case "constraints": return openConstraints();
      case "add-constraint": $("#constraintRows").insertAdjacentHTML("beforeend", constraintRow({}, $$(".rt-constraint-row").length)); return;
      case "phase-data": return openPhaseData(Number(el.dataset.phase));
      case "pause": return openPause();
      case "resume": {
        const data = await rtCall("PATCH", `/api/rt-courses/${R.course.id}`, { status: "active", notify_whatsapp: Boolean(S.patient?.wa_opt_in) });
        return showToast(`Treatment resumed.${waResultText(data.whatsapp)}`);
      }
      case "reopen": return openReopen();
      case "delete-course":
        return confirmDialog(`Delete ${R.course.site}?`, "The prescription, plan data, every recorded fraction and the weekly reviews will be removed. This cannot be undone.", async () => {
          const pid = R.patient.id;
          await del(`/api/rt-courses/${R.course.id}`);
          S.RT = null;
          showToast("Radiotherapy course deleted.");
          navigate(`#patient/${pid}`);
        });
      case "delete-fraction":
        return confirmDialog("Remove this fraction?", "Use this only to correct an entry made in error. The removal is kept in the course history.", async () => {
          await rtCall("DELETE", `/api/rt-fractions/${el.dataset.fraction}`, undefined, "Fraction removed.");
        }, "Remove");
      case "new-review": return openReview(null);
      case "edit-review": return openReview(Number(el.dataset.review));
      case "delete-review": {
        const id = S.rtReviewId;
        closeModals();
        return confirmDialog("Delete this weekly review?", "The grades, weight and decision of this review will be removed. The deletion is kept in the course history.", async () => {
          await rtCall("DELETE", `/api/rt-courses/${R.course.id}/reviews/${id}`, undefined, "Review deleted.");
        });
      }
      case "review-from-report": {
        const r = (S.rtReports || []).find(x => x.id === Number(el.dataset.report));
        if (!r) return;
        const items = [];
        for (const i of r.checkin?.items || []) if (i.term) items.push([i.term, i.grade_suggestion || 0, `Patient (${fmtShort(r.created_at)}): ${i.answers.map(a => a.answer).join(", ")}`.slice(0, 290)]);
        for (const c of r.ai?.candidates || []) if (!items.some(x => x[0] === c.term)) items.push([c.term, c.grade_suggestion || 0, (c.evidence || "").slice(0, 290)]);
        return openReview(null, { items, source: `the patient's message of ${fmtDate(r.created_at)}`, notes: `Patient message ${fmtDate(r.created_at)}: ${r.text.slice(0, 400)}` });
      }
    }
  } catch (error) { showToast(error.message); }
});
document.addEventListener("input", event => {
  const row = event.target.closest("[data-rt-phase]");
  if (row && event.target.dataset.f) { S.rtDraft.phases[Number(row.dataset.rtPhase)][event.target.dataset.f] = event.target.value; updateRtTotal(); }
  if (event.target.closest("#rtReviewForm")) updateReviewForm();
});
document.addEventListener("change", event => { if (event.target.closest("#rtReviewForm")) updateReviewForm(); });
document.addEventListener("submit", async event => {
  const form = event.target;
  if (form.id === "rtFractionForm") {
    event.preventDefault();
    const f = form.elements;
    const body = { treatment_date: f.treatment_date.value, phase_id: Number(f.phase_id.value), dose_cgy: f.dose_cgy.value || null, mu: f.mu.value || null,
      therapist: f.therapist.value.trim(), note: f.note.value.trim() || null, extra: f.extra.checked, allow_same_day: f.allow_same_day.checked };
    if (!body.therapist) return formError(form, "Enter the RTT who delivered the fraction.");
    const therapist = body.therapist;
    await withBusy(form, async () => {
      const data = await post(`/api/rt-courses/${S.RT.course.id}/fractions`, body);
      setRt(data);
      const last = data.fractions.at(-1);
      showToast(`Fraction ${last.number} recorded: ${gy(last.dose_cgy)}, cumulative ${gy(last.course_total_cgy)}.`);
      const next = $("#rtFractionForm");
      if (next) next.elements.therapist.value = therapist;
    });
    return;
  }
  if (form.dataset.rtBreak) {
    event.preventDefault();
    const f = form.elements;
    try { await rtCall("POST", `/api/rt-courses/${S.RT.course.id}/breaks`, { break_date: form.dataset.rtBreak, category: f.category.value, reason: f.reason.value }, "Reason recorded."); }
    catch (e) { showToast(e.message); }
    return;
  }
  if (form.id === "rtCompleteForm") {
    event.preventDefault();
    const f = form.elements;
    await withBusy(form, async () => {
      await rtCall("POST", `/api/rt-courses/${S.RT.course.id}/complete`, { outcome: f.outcome.value, end_date: f.end_date.value, gap_correction: f.gap_correction.value, notes: f.notes.value, signed_by: f.signed_by.value }, "Completion summary signed.");
    });
  }
});
$("#rtCourseForm").addEventListener("submit", submitCourse);
$("#rtReviewForm").addEventListener("submit", submitReview);
$("#rtDialogForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  await withBusy(form, async () => { await S.rtDialogSubmit(form.elements); closeModals(); });
});

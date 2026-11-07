"""Cancer diagnosis & staging at first presentation, and tumour measurements over the course of treatment.

Diagnosis: primary site, laterality, histology, grade, how it was diagnosed, TNM (clinical or pathological, AJCC 8th
by default), stage group, biomarkers, ECOG performance status.

Assessments: each scan or examination records lesions (location, longest diameter in mm, short axis for lymph
nodes, target / non-target / new) and tumour markers. The tumour response is computed with RECIST 1.1 against
the baseline and the smallest sum so far (nadir):
  - CR: every target lesion gone (lymph nodes < 10 mm short axis), no non-target disease, no new lesion
  - PR: sum of target diameters down by >= 30 % from baseline
  - PD: sum up by >= 20 % AND >= 5 mm from the nadir, or a new lesion, or unequivocal non-target progression
  - SD: neither PR nor PD
The computed response is decision support: the clinician confirms it or records a different response with a
reason (e.g. pseudo-progression, iRECIST, RANO or Lugano criteria for tumours RECIST does not fit).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from . import records
from .database import row_or_none, rows, transaction, utc_now
from .errors import ApiError

SITES = ["Breast", "Cervix", "Head and neck", "Oral cavity", "Oropharynx", "Larynx", "Lung", "Oesophagus", "Stomach", "Colorectal", "Liver", "Pancreas",
         "Gallbladder", "Ovary", "Endometrium", "Prostate", "Bladder", "Kidney", "Brain", "Thyroid", "Lymphoma", "Leukaemia",
         "Myeloma", "Sarcoma", "Skin (melanoma)", "Unknown primary", "Other"]
STAGE_GROUPS = ["0", "I", "IA", "IB", "IC", "II", "IIA", "IIB", "IIC", "III", "IIIA", "IIIB", "IIIC", "IV", "IVA", "IVB", "IVC", "Not staged"]
INTENTS = ("curative", "palliative", "unknown")
MODALITIES = {"ct": "CT", "mri": "MRI", "pet_ct": "PET-CT", "usg": "Ultrasound", "mammogram": "Mammogram", "xray": "X-ray",
              "endoscopy": "Endoscopy", "clinical": "Clinical examination", "other": "Other"}
TIMEPOINTS = {"baseline": "Baseline (before treatment)", "during": "During treatment", "end": "End of treatment", "follow_up": "Follow-up"}
RESPONSES = {"CR": "Complete response", "PR": "Partial response", "SD": "Stable disease", "PD": "Progressive disease", "NE": "Not evaluable"}
LESION_KINDS = ("target", "non_target", "new")
NON_TARGET_STATUS = ("present", "absent", "progression")


def _text(value: Any, limit: int) -> str | None:
    return records._text(value, limit)


# ---------------------------------------------------------------------------------------------- diagnosis
def _diag_view(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["biomarkers"] = json.loads(row.pop("biomarkers_json") or "[]")
    row["markers"] = json.loads(row.pop("markers_json") or "[]")
    tnm = " ".join(x for x in (f"{row['tnm_prefix'] or ''}T{row['t_stage']}" if row["t_stage"] else "", f"N{row['n_stage']}" if row["n_stage"] else "",
                               f"M{row['m_stage']}" if row["m_stage"] else "") if x)
    row["tnm"] = tnm
    row["stage_text"] = ", ".join(x for x in (tnm, f"Stage {row['stage_group']}" if row["stage_group"] and row["stage_group"] != "Not staged" else row["stage_group"] or "",
                                            row["staging_system"] or "") if x)
    where = " ".join(x for x in (row["laterality"] if row["laterality"] and row["laterality"] not in ("na", "midline") else "", row["primary_site"].lower()) if x)
    what = f"{row['histology']} ({where})" if row["histology"] else f"{row['primary_site']} cancer" + (f" ({row['laterality']})" if row["laterality"] in ("right", "left", "bilateral") else "")
    stage = ", ".join(x for x in (tnm, (f"Stage {row['stage_group']}" if row["stage_group"] and row["stage_group"] != "Not staged" else "") +
                                  (f" ({row['staging_system']})" if row["staging_system"] and row["stage_group"] else "")) if x)
    row["summary"] = ", ".join(x for x in (
        what[0].upper() + what[1:], f"grade {row['grade']}" if row["grade"] else "", stage,
        ", ".join(f"{b['name']} {b['value']}" for b in row["biomarkers"] if b.get("name")), f"ECOG {row['ecog']}" if row["ecog"] is not None else "") if x)
    return row


def _clean_pairs(items: Any, limit: int = 20) -> list[dict[str, str]]:
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and _text(item.get("name"), 60) and _text(item.get("value"), 60):  # a name without a value is an unused row
            out.append({"name": _text(item.get("name"), 60), "value": _text(item.get("value"), 60) or "", "unit": _text(item.get("unit"), 20) or ""})
    return out[:limit]


def _diag_fields(payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    def has(key: str) -> bool:
        return not partial or key in payload
    if has("primary_site"):
        site = _text(payload.get("primary_site"), 80)
        if not site:
            raise ApiError(422, "Choose the primary site", {"fields": ["primary_site"]})
        fields["primary_site"] = site
    if has("diagnosis_date"):
        try:
            day = date.fromisoformat(str(payload.get("diagnosis_date"))[:10])
        except (TypeError, ValueError):
            raise ApiError(422, "Enter the date of diagnosis", {"fields": ["diagnosis_date"]}) from None
        if day > date.today():
            raise ApiError(422, "The diagnosis date is in the future", {"fields": ["diagnosis_date"]})
        fields["diagnosis_date"] = day.isoformat()
    for key, limit in (("histology", 160), ("grade", 20), ("method", 120), ("t_stage", 12), ("n_stage", 12), ("m_stage", 12),
                       ("staging_system", 40), ("notes", 1500), ("laterality", 12)):
        if has(key):
            fields[key] = _text(payload.get(key), limit)
    if has("tnm_prefix"):
        prefix = payload.get("tnm_prefix") or "c"
        if prefix not in ("c", "p", "yc", "yp"):
            raise ApiError(422, "TNM prefix must be c, p, yc or yp")
        fields["tnm_prefix"] = prefix
    if has("stage_group"):
        group = payload.get("stage_group") or None
        if group and group not in STAGE_GROUPS:
            raise ApiError(422, "Unknown stage group", {"fields": ["stage_group"]})
        fields["stage_group"] = group
    if has("intent"):
        intent = payload.get("intent") or None
        if intent and intent not in INTENTS:
            raise ApiError(422, "Intent must be curative or palliative")
        fields["intent"] = intent
    if has("ecog"):
        ecog = payload.get("ecog")
        if ecog in (None, ""):
            fields["ecog"] = None
        else:
            try:
                fields["ecog"] = int(ecog)
            except (TypeError, ValueError):
                raise ApiError(422, "ECOG must be 0 to 4") from None
            if not 0 <= fields["ecog"] <= 4:
                raise ApiError(422, "ECOG must be 0 to 4")
    if has("biomarkers"):
        fields["biomarkers_json"] = json.dumps(_clean_pairs(payload.get("biomarkers")), ensure_ascii=False)
    if has("markers"):
        fields["markers_json"] = json.dumps(_clean_pairs(payload.get("markers")), ensure_ascii=False)
    return fields


def create_diagnosis(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    fields = _diag_fields(payload)
    fields["staging_system"] = fields.get("staging_system") or "AJCC 8th"
    fields["tnm_prefix"] = fields.get("tnm_prefix") or "c"
    with transaction(db) as connection:
        patient = row_or_none(connection, "SELECT id, diagnosis FROM patients WHERE id = ?", (patient_id,))
        if not patient:
            raise ApiError(404, "Patient not found")
        now = utc_now()
        fields |= {"patient_id": patient_id, "recorded_by": actor, "created_at": now, "updated_at": now}
        diag_id = connection.execute(f"INSERT INTO cancer_diagnoses ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                                     tuple(fields.values())).lastrowid
        view = _diag_view(row_or_none(connection, "SELECT * FROM cancer_diagnoses WHERE id = ?", (diag_id,)))
        if not (patient["diagnosis"] or "").strip():  # keep the one-line diagnosis on the patient in step
            connection.execute("UPDATE patients SET diagnosis = ?, updated_at = ? WHERE id = ?", (view["summary"][:300], now, patient_id))
        return view


def update_diagnosis(db: str, diag_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    fields = _diag_fields(payload, partial=True)
    if not fields:
        raise ApiError(422, "Nothing to change")
    with transaction(db) as connection:
        if not row_or_none(connection, "SELECT id FROM cancer_diagnoses WHERE id = ?", (diag_id,)):
            raise ApiError(404, "Diagnosis not found")
        fields |= {"updated_by": actor, "updated_at": utc_now()}
        connection.execute(f"UPDATE cancer_diagnoses SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), diag_id))
        return _diag_view(row_or_none(connection, "SELECT * FROM cancer_diagnoses WHERE id = ?", (diag_id,)))


# ---------------------------------------------------------------------------------------------- assessments
def _lesions(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > 30:
        raise ApiError(422, "Lesions must be a list of at most 30 rows")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        location = _text(item.get("location"), 120)
        if not location:
            if any(item.get(k) not in (None, "") for k in ("size_mm", "status")):
                raise ApiError(422, "Every lesion needs a location")
            continue
        kind = item.get("kind") or "target"
        if kind not in LESION_KINDS:
            raise ApiError(422, "Lesion type must be target, non-target or new")
        lesion: dict[str, Any] = {"location": location, "kind": kind, "node": bool(item.get("node")), "size_mm": None, "status": None,
                                  "key": _text(item.get("key"), 40) or location.lower()}
        if item.get("size_mm") not in (None, ""):
            try:
                size = float(item["size_mm"])
            except (TypeError, ValueError):
                raise ApiError(422, f"Size of '{location}' must be a number in mm") from None
            if not 0 <= size <= 500:
                raise ApiError(422, f"Size of '{location}' must be between 0 and 500 mm")
            lesion["size_mm"] = round(size, 1)
        if kind == "target" and lesion["size_mm"] is None:
            raise ApiError(422, f"Enter the size of target lesion '{location}' (0 if it has gone)")
        if kind == "non_target":
            status = item.get("status") or "present"
            if status not in NON_TARGET_STATUS:
                raise ApiError(422, "Non-target status must be present, absent or progression")
            lesion["status"] = status
        out.append(lesion)
    return out


def recist(assessments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate assessments (date order) with sum of diameters, change vs baseline / nadir and the RECIST 1.1 response."""
    baseline_sum = None
    nadir = None
    out = []
    for a in assessments:
        targets = [l for l in a["lesions"] if l["kind"] == "target"]
        non_targets = [l for l in a["lesions"] if l["kind"] == "non_target"]
        new = any(l["kind"] == "new" for l in a["lesions"]) or bool(a.get("new_lesions"))
        total = round(sum(l["size_mm"] or 0 for l in targets), 1) if targets else None
        result: dict[str, Any] = {"sum_mm": total, "change_from_baseline_pct": None, "change_from_nadir_pct": None, "computed": None, "computed_reason": ""}
        warnings = []
        if a["timepoint"] == "baseline" or baseline_sum is None:
            for l in targets:
                if l["node"] and (l["size_mm"] or 0) < 15:
                    warnings.append(f"{l['location']}: RECIST target nodes need a short axis of at least 15 mm")
                elif not l["node"] and (l["size_mm"] or 0) < 10:
                    warnings.append(f"{l['location']}: RECIST target lesions need to be at least 10 mm")
            if len(targets) > 5:
                warnings.append("RECIST 1.1 allows at most 5 target lesions (2 per organ)")
        result["warnings"] = warnings
        if baseline_sum is None:
            if total is not None:
                baseline_sum = total
                nadir = total
            result["computed"] = None
            result["computed_reason"] = "Baseline" if total is not None else "No target lesion measured"
            out.append(a | result)
            continue
        nt_progression = any(l["status"] == "progression" for l in non_targets)
        if total is None:
            # A new lesion or unequivocal non-target progression is PD even when the targets were not re-measured.
            result["computed"] = "PD" if new or nt_progression else "NE"
            result["computed_reason"] = "New lesion" if new else "Non-target progression" if nt_progression else "Target lesions not measured"
            out.append(a | result)
            continue
        from_base = (total - baseline_sum) / baseline_sum * 100 if baseline_sum else None
        from_nadir = (total - nadir) / nadir * 100 if nadir else (100.0 if total > 0 else 0.0)
        result["change_from_baseline_pct"] = round(from_base, 1) if from_base is not None else None
        result["change_from_nadir_pct"] = round(from_nadir, 1)
        nt_present = any(l["status"] == "present" for l in non_targets)
        all_gone = all((l["size_mm"] or 0) < 10 if l["node"] else (l["size_mm"] or 0) == 0 for l in targets)
        if new or nt_progression or (from_nadir >= 20 and total - nadir >= 5):
            result["computed"] = "PD"
            result["computed_reason"] = "New lesion" if new else "Non-target progression" if nt_progression else \
                f"Sum up {from_nadir:.0f}% ({total - nadir:.0f} mm) from the smallest so far"
        elif all_gone:
            result["computed"] = "PR" if nt_present else "CR"
            result["computed_reason"] = "All target lesions gone" + (", non-target disease still present" if nt_present else "")
        elif from_base is not None and from_base <= -30:
            result["computed"] = "PR"
            result["computed_reason"] = f"Sum down {abs(from_base):.0f}% from baseline"
        else:
            result["computed"] = "SD"
            result["computed_reason"] = f"Sum {'down' if (from_base or 0) < 0 else 'up'} {abs(from_base or 0):.0f}% from baseline"
        nadir = min(nadir, total)
        out.append(a | result)
    for a in out:
        a["response"] = a.get("response_override") or a["computed"]
        a["response_label"] = RESPONSES.get(a["response"] or "", "Baseline" if a["timepoint"] == "baseline" or a["computed_reason"] == "Baseline" else "")
    return out


def _assessment_view(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["lesions"] = json.loads(row.pop("lesions_json") or "[]")
    row["markers"] = json.loads(row.pop("markers_json") or "[]")
    row["modality_label"] = MODALITIES.get(row["modality"], row["modality"])
    row["timepoint_label"] = TIMEPOINTS.get(row["timepoint"], row["timepoint"])
    row["new_lesions"] = bool(row["new_lesions"])
    return row


def _assessment_fields(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        day = date.fromisoformat(str(payload.get("assessment_date"))[:10])
    except (TypeError, ValueError):
        raise ApiError(422, "Enter the date of the scan or examination", {"fields": ["assessment_date"]}) from None
    if day > date.today():
        raise ApiError(422, "The assessment date is in the future", {"fields": ["assessment_date"]})
    modality = payload.get("modality") or "ct"
    if modality not in MODALITIES:
        raise ApiError(422, "Unknown imaging modality")
    timepoint = payload.get("timepoint") or "during"
    if timepoint not in TIMEPOINTS:
        raise ApiError(422, "Unknown timepoint")
    lesions = _lesions(payload.get("lesions") or [])
    markers = _clean_pairs(payload.get("markers"))
    if not lesions and not markers:
        raise ApiError(422, "Record at least one lesion or tumour marker", {"fields": ["lesions"]})
    override = payload.get("response_override") or None
    if override and override not in RESPONSES:
        raise ApiError(422, "Response must be CR, PR, SD, PD or NE")
    note = _text(payload.get("response_note"), 500)
    if override and not note:
        raise ApiError(422, "Give the reason for recording a different response", {"fields": ["response_note"]})
    return {"assessment_date": day.isoformat(), "modality": modality, "timepoint": timepoint, "lesions_json": json.dumps(lesions, ensure_ascii=False),
            "markers_json": json.dumps(markers, ensure_ascii=False), "new_lesions": 1 if payload.get("new_lesions") else 0,
            "response_override": override, "response_note": note, "findings": _text(payload.get("findings"), 2000),
            "document_id": payload.get("document_id") or None, "cycle_label": _text(payload.get("cycle_label"), 60)}


def _all_assessments(connection: sqlite3.Connection, patient_id: int) -> list[dict[str, Any]]:
    return recist([_assessment_view(r) for r in rows(connection, "SELECT * FROM tumour_assessments WHERE patient_id = ? ORDER BY assessment_date, id", (patient_id,))])


def create_assessment(db: str, patient_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    fields = _assessment_fields(payload)
    with transaction(db) as connection:
        if not row_or_none(connection, "SELECT id FROM patients WHERE id = ?", (patient_id,)):
            raise ApiError(404, "Patient not found")
        if fields["document_id"] and not row_or_none(connection, "SELECT id FROM patient_documents WHERE id = ? AND patient_id = ?", (fields["document_id"], patient_id)):
            raise ApiError(422, "That report belongs to another patient")
        diag = row_or_none(connection, "SELECT id FROM cancer_diagnoses WHERE patient_id = ? ORDER BY diagnosis_date DESC, id DESC LIMIT 1", (patient_id,))
        now = utc_now()
        fields |= {"patient_id": patient_id, "diagnosis_id": diag["id"] if diag else None, "recorded_by": actor, "created_at": now, "updated_at": now}
        new_id = connection.execute(f"INSERT INTO tumour_assessments ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                                    tuple(fields.values())).lastrowid
        return next(a for a in _all_assessments(connection, patient_id) if a["id"] == new_id)


def update_assessment(db: str, assessment_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    with transaction(db) as connection:
        row = row_or_none(connection, "SELECT * FROM tumour_assessments WHERE id = ?", (assessment_id,))
        if not row:
            raise ApiError(404, "Assessment not found")
        current = _assessment_view(row)
        merged = {"assessment_date": current["assessment_date"], "modality": current["modality"], "timepoint": current["timepoint"],
                  "lesions": current["lesions"], "markers": current["markers"], "new_lesions": current["new_lesions"],
                  "response_override": current["response_override"], "response_note": current["response_note"], "findings": current["findings"],
                  "document_id": current["document_id"], "cycle_label": current["cycle_label"]} | payload
        fields = _assessment_fields(merged) | {"updated_by": actor, "updated_at": utc_now()}
        connection.execute(f"UPDATE tumour_assessments SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), assessment_id))
        return next(a for a in _all_assessments(connection, row["patient_id"]) if a["id"] == assessment_id)


def delete_assessment(db: str, assessment_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        if not connection.execute("DELETE FROM tumour_assessments WHERE id = ?", (assessment_id,)).rowcount:
            raise ApiError(404, "Assessment not found")
        return {"deleted": True}


# ---------------------------------------------------------------------------------------------- overview
def overview_for(connection: sqlite3.Connection, patient_id: int, period_to: str | None = None) -> dict[str, Any]:
    diagnoses = [_diag_view(r) for r in rows(connection, "SELECT * FROM cancer_diagnoses WHERE patient_id = ? ORDER BY diagnosis_date, id", (patient_id,))]
    assessments = [a for a in _all_assessments(connection, patient_id) if not period_to or a["assessment_date"] <= period_to]
    measured = [a for a in assessments if a["sum_mm"] is not None]
    best = None
    order = {"CR": 0, "PR": 1, "SD": 2, "PD": 3}
    for a in assessments:
        if a["response"] in order and (best is None or order[a["response"]] < order[best["response"]]):
            best = a
    marker_names = sorted({m["name"] for a in assessments for m in a["markers"]} | {m["name"] for d in diagnoses for m in d["markers"]})
    return {"diagnoses": diagnoses, "assessments": assessments,
            "latest": assessments[-1] if assessments else None, "best": best,
            "baseline": next((a for a in assessments if a["sum_mm"] is not None), None),
            "series": [{"date": a["assessment_date"], "sum_mm": a["sum_mm"], "response": a["response"]} for a in measured],
            "marker_names": marker_names,
            "options": {"sites": SITES, "stage_groups": STAGE_GROUPS, "modalities": MODALITIES, "timepoints": TIMEPOINTS, "responses": RESPONSES}}


def overview(db: str, patient_id: int) -> dict[str, Any]:
    with transaction(db) as connection:
        if not row_or_none(connection, "SELECT id FROM patients WHERE id = ?", (patient_id,)):
            raise ApiError(404, "Patient not found")
        return overview_for(connection, patient_id)


def assessment_line(a: dict[str, Any]) -> str:
    targets = [l for l in a["lesions"] if l["kind"] == "target"]
    parts = [f"{a['modality_label']}"]
    if targets:
        parts.append("target lesions " + ", ".join(f"{l['location']} {l['size_mm']:g} mm" for l in targets) + f" (sum {a['sum_mm']:g} mm)")
    if a["change_from_baseline_pct"] is not None:
        parts.append(f"{a['change_from_baseline_pct']:+.0f}% from baseline")
    new = [l["location"] for l in a["lesions"] if l["kind"] == "new"]
    if new or a["new_lesions"]:
        parts.append("new lesion" + (f": {', '.join(new)}" if new else ""))
    if a["markers"]:
        parts.append(", ".join(f"{m['name']} {m['value']} {m['unit']}".strip() for m in a["markers"]))
    if a["response"]:
        parts.append(f"response {a['response']} ({RESPONSES[a['response']].lower()}{', clinician-recorded' if a['response_override'] else ', RECIST 1.1'})")
    return "; ".join(parts)

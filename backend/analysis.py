"""Deterministic analysis: lab trends from baseline, CTCAE lab grading and toxicity trajectories.

Everything here is rule-based against the stored reference data in ``reference.py``. Output is an
attention aid with its reasoning spelled out; it never recommends a treatment action.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .reference import CTCAE_TERMS, CTCAE_VERSION, LAB_CATALOG, drugs_associated_with, term_provenance, to_canonical

GIVEN_STATUSES = {"given", "modified"}


def fmt(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# CTCAE lab grading
# ---------------------------------------------------------------------------

def _below(value: float, lln: float | None, cutoffs: list[tuple[float, int]]) -> tuple[int, str]:
    """cutoffs: [(threshold, grade)] ordered most severe first; grade if value < threshold."""
    for threshold, grade in cutoffs:
        if value < threshold:
            return grade, f"< {fmt(threshold)}"
    if lln is not None and value < lln:
        return 1, f"< LLN ({fmt(lln)})"
    return 0, "within CTCAE grade 0 range"


def _multiple_of(value: float, uln: float | None, cutoffs: list[tuple[float, int]]) -> tuple[int, str]:
    if not uln:
        return 0, "no upper reference limit available"
    ratio = value / uln
    for multiple, grade in cutoffs:
        if ratio > multiple:
            return grade, f"{ratio:.2f} x ULN (> {fmt(multiple)} x ULN)"
    if value > uln:
        return 1, f"{ratio:.2f} x ULN (> ULN)"
    return 0, f"{ratio:.2f} x ULN"


def grade_lab(test_name: str, value: float, unit: str, ref_low: float | None, ref_high: float | None,
              baseline: float | None = None) -> dict[str, Any] | None:
    """Return the CTCAE term/grade implied by one lab value, with the rule that produced it."""
    canonical, problem = to_canonical(test_name, value, unit)
    catalog = LAB_CATALOG.get(test_name)
    if catalog is None:
        return None
    if canonical is None:
        return {"term": None, "grade": None, "reasoning": f"Not graded: {problem}."}
    factor = canonical / value if value else 1.0
    lln = ref_low * factor if ref_low is not None else catalog["low"]
    uln = ref_high * factor if ref_high is not None else catalog["high"]
    u = catalog["unit"]
    shown = f"{test_name} {fmt(canonical)} {u}"
    term, grade, rule, note = None, 0, "", ""

    if test_name == "ANC":
        term = "Neutrophil count decreased"
        grade, rule = _below(canonical, lln, [(500, 4), (1000, 3), (1500, 2)])
    elif test_name == "Platelets":
        term = "Platelet count decreased"
        grade, rule = _below(canonical, lln, [(25, 4), (50, 3), (75, 2)])
    elif test_name == "WBC":
        term = "White blood cell decreased"
        grade, rule = _below(canonical, lln, [(1.0, 4), (2.0, 3), (3.0, 2)])
    elif test_name == "Hemoglobin":
        term = "Anemia"
        grade, rule = _below(canonical, lln, [(8.0, 3), (10.0, 2)])
        if grade == 3:
            note = "Grade 3 also requires that transfusion is indicated; confirm clinically."
    elif test_name == "Magnesium":
        term = "Hypomagnesemia"
        grade, rule = _below(canonical, lln, [(0.7, 4), (0.9, 3), (1.2, 2)])
    elif test_name == "Sodium":
        if canonical > uln:
            return {"term": None, "grade": 0, "reasoning": f"{shown} is above ULN ({fmt(uln)}); hypernatremia is not in the stored term set."}
        term = "Hyponatremia"
        grade, rule = _below(canonical, lln, [(120, 4), (125, 3), (130, 2)])
        if grade == 2:
            note = "125–129 mmol/L is Grade 3 if symptomatic."
    elif test_name == "Potassium":
        if canonical > uln:
            term = "Hyperkalemia"
            grade, rule = 1, f"> ULN ({fmt(uln)})"
            for threshold, g in ((7.0, 4), (6.0, 3), (5.5, 2)):
                if canonical > threshold:
                    grade, rule = g, f"> {fmt(threshold)}"
                    break
        else:
            term = "Hypokalemia"
            grade, rule = _below(canonical, lln, [(2.5, 4), (3.0, 3)])
            if grade == 1:
                note = "Grade 2 if symptomatic or intervention indicated."
    elif test_name == "Creatinine":
        term = "Creatinine increased"
        grade, rule = _multiple_of(canonical, uln, [(6.0, 4), (3.0, 3), (1.5, 2)])
        if baseline:
            base_c, _ = to_canonical(test_name, baseline, unit)
            if base_c:
                ratio = canonical / base_c
                base_grade = 3 if ratio > 3.0 else 2 if ratio > 1.5 else 0
                if base_grade > grade:
                    grade = base_grade
                    rule = f"{ratio:.2f} x baseline {fmt(base_c)} (> {'3.0' if base_grade == 3 else '1.5'} x baseline); ULN rule alone gives a lower grade"
                elif ratio > 1.0:
                    rule += f"; {ratio:.2f} x baseline"
    elif test_name in ("ALT", "AST"):
        term = "Alanine aminotransferase increased" if test_name == "ALT" else "Aspartate aminotransferase increased"
        base_uln = ref_high or catalog["high"]
        if baseline and baseline > base_uln:
            # CTCAE v6.0: baseline >ULN is graded against multiples of the patient's baseline.
            ratio = value / baseline
            grade, rule = 0, f"{ratio:.2f} x abnormal baseline {fmt(baseline)} (v6 baseline >ULN rule)"
            for multiple, g in ((4.0, 4), (2.0, 3), (1.5, 2), (1.0, 1)):
                if ratio > multiple or (g == 1 and ratio >= 1.0 and value > base_uln):
                    grade, rule = g, f"{ratio:.2f} x abnormal baseline {fmt(baseline)} (v6: baseline >ULN graded vs baseline)"
                    break
        else:
            grade, rule = _multiple_of(canonical, uln, [(20.0, 4), (5.0, 3), (3.0, 2)])
    elif test_name == "Bilirubin":
        term = "Blood bilirubin increased"
        grade, rule = _multiple_of(canonical, uln, [(10.0, 4), (3.0, 3), (1.5, 2)])
        if baseline and baseline > (ref_high or catalog["high"]):
            note = "Baseline was >ULN; CTCAE v6.0 grades this against baseline multiples — review manually."
    elif test_name == "ALP":
        term = "Alkaline phosphatase increased"
        grade, rule = _multiple_of(canonical, uln, [(20.0, 4), (5.0, 3), (2.5, 2)])
    elif test_name == "Albumin":
        term = "Hypoalbuminemia"
        grade, rule = _below(canonical, lln, [(2.0, 3), (3.0, 2)])
    elif test_name == "Calcium":
        note = "CTCAE uses corrected calcium; correct for albumin before relying on this grade."
        if canonical > uln:
            term = "Hypercalcemia"
            grade, rule = 1, f"> ULN ({fmt(uln)})"
            for threshold, g in ((13.5, 4), (12.5, 3), (11.5, 2)):
                if canonical > threshold:
                    grade, rule = g, f"> {fmt(threshold)}"
                    break
        else:
            term = "Hypocalcemia"
            grade, rule = _below(canonical, lln, [(6.0, 4), (7.0, 3), (8.0, 2)])
    elif test_name == "eGFR":
        term = "Chronic kidney disease"
        grade, rule = _below(canonical, lln, [(15, 4), (30, 3), (60, 2)])
    elif test_name == "Uric acid":
        term = "Hyperuricemia"
        grade, rule = (1, f"> ULN ({fmt(uln)}); grade 3 only with physiologic consequences") if canonical > uln else (0, "within range")
    else:
        return {"term": None, "grade": None, "reasoning": f"{test_name} has no CTCAE laboratory term in the stored set; trend only."}

    criterion = CTCAE_TERMS[term]["grades"].get(grade) if grade else None
    if grade:
        prov = term_provenance(term)
        label = "CTCAE v6.0" if prov == "CTCAE v6.0" else ("CTCAE (" + prov + ")" if not prov.startswith("CTCAE v5.0") else "CTCAE v5.0 criteria (v6 import pending)")
        reasoning = f"{shown}: {rule} → {label} {term} Grade {grade} (\"{criterion}\")."
    else:
        reasoning = f"{shown}: {rule} → no {term.lower()} grade."
    if note:
        reasoning += f" {note}"
    return {"term": term, "grade": grade, "criterion": criterion, "reasoning": reasoning}


# ---------------------------------------------------------------------------
# Longitudinal plan analysis
# ---------------------------------------------------------------------------

def _cycle_date(cycle: dict[str, Any]) -> str:
    return cycle.get("administered_date") or cycle["planned_date"]


def _lab_status(verified: int, unverified: int) -> str:
    if verified:
        return "verified"
    if unverified:
        return "unverified"
    return "missing"


def _direction(delta: float | None) -> str:
    if delta is None or abs(delta) < 1e-9:
        return "stable"
    return "up" if delta > 0 else "down"


def _is_worse(test_name: str, direction: str, value: float, low: float | None, high: float | None) -> bool:
    worse = LAB_CATALOG.get(test_name, {}).get("worse", "both")
    if direction == "stable":
        return False
    if worse == "both":
        if low is None or high is None:
            return False
        mid = (low + high) / 2
        return (direction == "down" and value < mid) or (direction == "up" and value > mid)
    return direction == worse


# Unfavourable change from baseline (percent) that is worth a flag, per test.
FLAG_PCT = {"Hemoglobin": 15, "WBC": 30, "ANC": 30, "Platelets": 30, "Creatinine": 30, "Urea": 50, "eGFR": 25,
            "Magnesium": 20, "Potassium": 15, "Sodium": 4, "Calcium": 8, "Chloride": 5, "ALT": 100, "AST": 100,
            "Bilirubin": 50, "Direct bilirubin": 50, "ALP": 50, "Albumin": 15, "Total protein": 15, "Uric acid": 30}


def _unfavourable(test_name: str, delta: float, value: float, low: float | None, high: float | None) -> bool:
    worse = LAB_CATALOG.get(test_name, {}).get("worse", "both")
    if abs(delta) < 1e-9:
        return False
    if worse == "both":
        if low is None or high is None:
            return False
        mid = (low + high) / 2
        return (delta < 0 and value < mid) or (delta > 0 and value > mid)
    return (delta > 0) == (worse == "up")


def lab_flags(test_name: str, points: list[dict[str, Any]], labs: list[dict[str, Any]], next_date: str | None) -> list[dict[str, Any]]:
    """Rule-based flags for the latest point in ``points`` (chronological, one per cycle window)."""
    flags: list[dict[str, Any]] = []
    cur, lab = points[-1], labs[-1]
    base = points[0]
    prev = points[-2] if len(points) > 1 else None
    low, high, unit = lab["ref_low"], lab["ref_high"], lab["unit"]
    name = test_name
    limit_word = lambda v: "below LLN" if low is not None and v < low else "above ULN"  # noqa: E731

    if cur["abnormal"] and (prev is None or not prev["abnormal"]) and len(points) > 1:
        flags.append({"code": "crossed_limit", "severity": "warning",
                      "title": f"{name} newly {limit_word(cur['value'])}",
                      "detail": f"{fmt(cur['value'])} {unit} (ref {fmt(low)}–{fmt(high)}); within range at the previous check ({fmt(prev['value'])})."})
    elif cur["abnormal"] and prev is not None and prev["abnormal"]:
        streak = 0
        for p in reversed(points):
            if not p["abnormal"]:
                break
            streak += 1
        flags.append({"code": "persistent_abnormal", "severity": "info",
                      "title": f"{name} {limit_word(cur['value'])} for {streak} consecutive checks",
                      "detail": f"First outside range before C{points[len(points) - streak]['cycle_number']}."})

    threshold = FLAG_PCT.get(name, 25)
    pct = cur["pct_from_baseline"]
    if len(points) > 1 and pct is not None and _unfavourable(name, cur["value"] - base["value"], cur["value"], low, high) and abs(pct) >= threshold:
        flags.append({"code": "baseline_change", "severity": "alert" if abs(pct) >= 2 * threshold else "warning",
                      "title": f"{name} {'+' if pct > 0 else ''}{fmt(pct)}% from baseline",
                      "detail": f"{fmt(base['value'])} → {fmt(cur['value'])} {unit}; flag threshold for {name} is ±{threshold}% in the unfavourable direction."})
    if prev is not None and prev["value"]:
        step = (cur["value"] - prev["value"]) / prev["value"] * 100
        if _unfavourable(name, cur["value"] - prev["value"], cur["value"], low, high) and abs(step) >= threshold:
            flags.append({"code": "rapid_change", "severity": "warning",
                          "title": f"{name} {'+' if step > 0 else ''}{round(step, 1):g}% since the previous check",
                          "detail": f"{fmt(prev['value'])} (C{prev['cycle_number']}) → {fmt(cur['value'])} {unit} (C{cur['cycle_number']}) in one interval."})

    moves = 0
    for a, b in zip(reversed(points[:-1]), reversed(points)):
        if _unfavourable(name, b["value"] - a["value"], b["value"], low, high):
            moves += 1
        else:
            break
    if moves >= 3:
        flags.append({"code": "steady_trend", "severity": "warning",
                      "title": f"{name} worsening at {moves} consecutive checks",
                      "detail": " → ".join(fmt(p["value"]) for p in points[-(moves + 1):]) + f" {unit}."})

    if prev is not None and (cur["ctcae_grade"] or 0) > (prev["ctcae_grade"] or 0):
        flags.append({"code": "ctcae_rise", "severity": "alert" if cur["ctcae_grade"] >= 2 else "warning",
                      "title": f"{cur['ctcae_term']} rose to Grade {cur['ctcae_grade']}",
                      "detail": f"Grade {prev['ctcae_grade'] or 0} at C{prev['cycle_number']} → Grade {cur['ctcae_grade']} at C{cur['cycle_number']}."})

    if name == "Creatinine" and len(points) > 1:
        rise, ratio = cur["value"] - base["value"], (cur["value"] / base["value"] if base["value"] else 0)
        if rise >= 0.3 or ratio >= 1.5:
            flags.append({"code": "kdigo_creatinine", "severity": "alert",
                          "title": "Creatinine rise meets KDIGO AKI magnitude",
                          "detail": f"+{fmt(rise)} mg/dL and {ratio:.2f}× baseline (KDIGO: ≥0.3 mg/dL within 48 h or ≥1.5× within 7 days). Sampling interval is longer, so this is a magnitude flag only."})

    if prev is not None and next_date and not cur["abnormal"]:
        try:
            days_prev = (date.fromisoformat(lab["collection_date"]) - date.fromisoformat(labs[-2]["collection_date"])).days
            days_next = (date.fromisoformat(next_date) - date.fromisoformat(lab["collection_date"])).days
        except ValueError:
            days_prev = days_next = 0
        if days_prev > 0 and days_next > 0:
            projected = cur["value"] + (cur["value"] - prev["value"]) / days_prev * days_next
            crosses = (low is not None and projected < low) or (high is not None and projected > high)
            if crosses and _unfavourable(name, projected - cur["value"], projected, low, high):
                flags.append({"code": "projected_crossing", "severity": "info",
                              "title": f"{name} projected {limit_word(projected)} by {next_date}",
                              "detail": f"Straight-line projection of the last interval gives ≈{fmt(projected)} {unit}. Projection only; not a prediction of toxicity."})
    order = {"alert": 0, "warning": 1, "info": 2}
    return sorted(flags, key=lambda f: order[f["severity"]])


def build_lab_trends(cycles: list[dict[str, Any]], labs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One trend per test: baseline (first pre-cycle value) through the most recent pre-cycle value."""
    by_cycle_number = {c["id"]: c["cycle_number"] for c in cycles}
    verified = [lab for lab in labs if lab["verification_status"] == "verified" and lab["cycle_id"] in by_cycle_number]
    tests: dict[str, dict[int, dict[str, Any]]] = {}
    for lab in sorted(verified, key=lambda item: (item["collection_date"], item["id"])):
        # latest observation per cycle window wins
        tests.setdefault(lab["test_name"], {})[by_cycle_number[lab["cycle_id"]]] = lab
    order = list(LAB_CATALOG)
    trends = []
    for test_name in sorted(tests, key=lambda name: (order.index(name) if name in order else 99, name)):
        per_cycle = tests[test_name]
        numbers = sorted(per_cycle)
        base = per_cycle[numbers[0]]
        points = []
        previous = None
        for number in numbers:
            lab = per_cycle[number]
            delta = None if previous is None else lab["value"] - previous["value"]
            pct = ((lab["value"] - base["value"]) / base["value"] * 100) if base["value"] else None
            graded = grade_lab(test_name, lab["value"], lab["unit"], lab["ref_low"], lab["ref_high"],
                               base["value"] if number != numbers[0] else None)
            abnormal = ((lab["ref_low"] is not None and lab["value"] < lab["ref_low"])
                        or (lab["ref_high"] is not None and lab["value"] > lab["ref_high"]))
            points.append({
                "cycle_number": number, "cycle_id": lab["cycle_id"], "lab_id": lab["id"], "value": lab["value"],
                "collection_date": lab["collection_date"], "delta_from_previous": None if delta is None else round(delta, 4),
                "pct_from_baseline": None if pct is None else round(pct, 1), "direction": _direction(delta),
                "abnormal": abnormal, "ctcae_term": graded.get("term") if graded else None,
                "ctcae_grade": graded.get("grade") if graded else None,
                "ctcae_reasoning": graded.get("reasoning") if graded else None,
            })
            previous = lab
        next_dates = {c["cycle_number"]: (c.get("administered_date") or c["planned_date"]) for c in cycles}
        ordered_labs = [per_cycle[n] for n in numbers]
        for i, point in enumerate(points):
            point["flags"] = lab_flags(test_name, points[:i + 1], ordered_labs[:i + 1], next_dates.get(point["cycle_number"] + 1) if i == len(points) - 1 else None)
        latest = points[-1]
        latest_lab = per_cycle[numbers[-1]]
        unit = latest_lab["unit"]
        overall = _direction(latest["value"] - base["value"]) if len(points) > 1 else "stable"
        worsening = len(points) > 1 and _is_worse(test_name, overall, latest["value"], latest_lab["ref_low"], latest_lab["ref_high"])
        consecutive = 0
        for point in reversed(points[1:]):
            if point["direction"] == overall and overall != "stable":
                consecutive += 1
            else:
                break
        pct = latest["pct_from_baseline"]
        low, high = latest_lab["ref_low"], latest_lab["ref_high"]
        near_limit = latest["abnormal"] or (
            (high is not None and overall == "up" and latest["value"] > high * 0.75)
            or (low is not None and overall == "down" and latest["value"] < low * 1.25))
        meaningful = worsening and near_limit and (latest["abnormal"] or (pct is not None and abs(pct) >= 20))
        reasons = []
        if len(points) == 1:
            reasons.append(f"Only one verified value ({fmt(latest['value'])} {unit}, C{latest['cycle_number']}); trend needs a later value.")
        else:
            reasons.append(
                f"{test_name} {fmt(base['value'])} → {fmt(latest['value'])} {unit} from baseline (C{points[0]['cycle_number']}) "
                f"to pre-C{latest['cycle_number']} ({'+' if (pct or 0) > 0 else ''}{fmt(pct)}%)."
            )
            if consecutive >= 2:
                reasons.append(f"Moved {overall} across {consecutive} consecutive pre-cycle checks.")
            if meaningful and not latest["abnormal"]:
                reasons.append("Still within range but moving towards the reference limit in the unfavourable direction.")
            elif not worsening and overall != "stable":
                reasons.append("Change is not in the unfavourable direction for this test.")
        if latest["abnormal"]:
            first_abnormal = next(p for p in points if p["abnormal"])
            reasons.append(f"Outside reference range ({fmt(latest_lab['ref_low'])}–{fmt(latest_lab['ref_high'])}) since pre-C{first_abnormal['cycle_number']}.")
        if latest["ctcae_grade"]:
            reasons.append(latest["ctcae_reasoning"])
        trends.append({
            "test_name": test_name, "unit": unit, "panel": LAB_CATALOG.get(test_name, {}).get("panel", "Other"),
            "reference_range": {"low": latest_lab["ref_low"], "high": latest_lab["ref_high"]},
            "baseline": {"value": base["value"], "cycle_number": points[0]["cycle_number"], "collection_date": base["collection_date"]},
            "latest": latest, "points": points, "overall_direction": overall, "worsening": worsening,
            "meaningful_change": meaningful or any(f["severity"] != "info" for f in latest["flags"]),
            "consecutive_moves": consecutive, "flags": latest["flags"],
            "max_ctcae_grade": max((p["ctcae_grade"] or 0) for p in points),
            "reasoning": " ".join(reasons),
        })
    return trends


def analyse_plan(plan: dict[str, Any], cycles: list[dict[str, Any]], medications: dict[int, list[dict[str, Any]]],
                 labs: list[dict[str, Any]], symptoms: list[dict[str, Any]], decisions: dict[int, list[dict[str, Any]]],
                 documents: list[dict[str, Any]], plan_medications: list[dict[str, Any]],
                 label_context: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    cycles = sorted(cycles, key=lambda c: c["cycle_number"])
    cycle_ids = {c["id"] for c in cycles}
    regimen_drugs = [m["drug_name"] for m in plan_medications] or sorted({m["drug_name"] for meds in medications.values() for m in meds})

    # Assign unlinked symptoms to the window whose cycle date follows the onset date.
    window_symptoms: dict[int, list[dict[str, Any]]] = {c["id"]: [] for c in cycles}
    for symptom in symptoms:
        target = symptom["cycle_id"] if symptom["cycle_id"] in cycle_ids else None
        if target is None:
            for cycle in cycles:
                if symptom["onset_date"] <= _cycle_date(cycle):
                    target = cycle["id"]
                    break
            if target is None and cycles:
                target = cycles[-1]["id"]
        if target is not None:
            window_symptoms[target].append(symptom)

    trends = build_lab_trends(cycles, labs)
    trend_points = {(t["test_name"], p["cycle_id"]): (t, p) for t in trends for p in t["points"]}
    baselines: dict[str, dict[str, Any]] = {}
    for lab in sorted((l for l in labs if l["verification_status"] == "verified" and l["cycle_id"] in cycle_ids),
                      key=lambda item: (item["collection_date"], item["id"])):
        baselines.setdefault(lab["test_name"], lab)

    exposure: dict[str, float] = {}
    exposure_units: dict[str, str] = {}
    last_grade: dict[str, tuple[int, int]] = {}  # term -> (grade, cycle_number)
    matrix: dict[str, dict[str, Any]] = {}
    out_cycles = []
    next_cycle_id = None
    previous_cycle = None

    for cycle in cycles:
        meds = medications.get(cycle["id"], [])
        window_labs = [lab for lab in labs if lab["cycle_id"] == cycle["id"]]
        verified = [lab for lab in window_labs if lab["verification_status"] == "verified"]
        unverified = [lab for lab in window_labs if lab["verification_status"] == "unverified"]
        lab_status = _lab_status(len(verified), len(unverified))
        drugs_before = [drug for drug, total in exposure.items() if total > 0]
        exposure_snapshot = [{"drug_name": d, "cumulative_dose": round(exposure[d], 2), "dose_unit": exposure_units[d]} for d in drugs_before]

        # Latest verified value per test in this window, with trend context.
        latest_by_test: dict[str, dict[str, Any]] = {}
        for lab in sorted(verified, key=lambda item: (item["collection_date"], item["id"])):
            latest_by_test[lab["test_name"]] = lab
        lab_rows = []
        ctcae_items = []
        for test_name, lab in latest_by_test.items():
            trend, point = trend_points.get((test_name, cycle["id"]), (None, None))
            row = {"lab_id": lab["id"], "test_name": test_name, "value": lab["value"], "unit": lab["unit"],
                   "ref_low": lab["ref_low"], "ref_high": lab["ref_high"], "collection_date": lab["collection_date"],
                   "source": lab["source"], "abnormal": bool(point and point["abnormal"]),
                   "direction": point["direction"] if point else "stable",
                   "pct_from_baseline": point["pct_from_baseline"] if point else None,
                   "delta_from_previous": point["delta_from_previous"] if point else None,
                   "ctcae_term": point["ctcae_term"] if point else None, "ctcae_grade": point["ctcae_grade"] if point else None}
            lab_rows.append(row)
        # CTCAE lab grades: worst grade per term across every verified value in this window.
        worst_lab: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for lab in verified:
            base = baselines.get(lab["test_name"])
            is_base = base is not None and base["id"] == lab["id"]
            graded = grade_lab(lab["test_name"], lab["value"], lab["unit"], lab["ref_low"], lab["ref_high"],
                               None if (base is None or is_base) else base["value"])
            if not graded or not graded.get("grade"):
                continue
            current = worst_lab.get(graded["term"])
            if current is None or graded["grade"] > current[0]["grade"]:
                worst_lab[graded["term"]] = (graded, lab)
        for term, (graded, lab) in worst_lab.items():
            prev = last_grade.get(term)
            trend_label = _toxicity_trend(graded["grade"], prev)
            associated, label_evidence, assoc_source = _associate(term, drugs_before or regimen_drugs, label_context or {})
            reasoning = graded["reasoning"]
            if CTCAE_TERMS.get(term, {}).get("threshold_check") == "differs":
                reasoning += " Imported v6.0 wording differs from the built-in thresholds used for this computation — confirm the grade against the criteria shown."
            if lab["id"] != latest_by_test.get(lab["test_name"], {}).get("id"):
                latest = latest_by_test[lab["test_name"]]
                reasoning += f" Worst value in this window ({lab['collection_date']}); latest {fmt(latest['value'])} {latest['unit']} on {latest['collection_date']}."
            reasoning += _trend_sentence(trend_label, prev)
            reasoning += _association_sentence(associated, exposure_snapshot, label_evidence, assoc_source)
            ctcae_items.append({"term": term, "grade": graded["grade"], "source": "lab", "status": None,
                                "evidence": f"{lab['test_name']} {fmt(lab['value'])} {lab['unit']} on {lab['collection_date']}",
                                "trend": trend_label, "previous_grade": prev[0] if prev else 0,
                                "associated_drugs": associated, "label_evidence": label_evidence, "association_source": assoc_source,
                                "criteria_source": term_provenance(term), "reasoning": reasoning,
                                "confirmation": "Computed from verified lab value — clinician review required"})

        # Clinician-confirmed symptom grades in this window (worst grade per term).
        symptom_rows = sorted(window_symptoms[cycle["id"]], key=lambda s: (s["onset_date"], s["id"]))
        worst_by_term: dict[str, dict[str, Any]] = {}
        for symptom in symptom_rows:
            current = worst_by_term.get(symptom["ctcae_term"])
            if current is None or symptom["grade"] >= current["grade"]:
                worst_by_term[symptom["ctcae_term"]] = symptom
        for term, symptom in worst_by_term.items():
            prev = last_grade.get(term)
            trend_label = _toxicity_trend(symptom["grade"], prev)
            associated, label_evidence, assoc_source = _associate(term, drugs_before or regimen_drugs, label_context or {})
            criterion = CTCAE_TERMS.get(term, {}).get("grades", {}).get(symptom["grade"])
            reasoning = f"{term} Grade {symptom['grade']} confirmed by {symptom['confirmed_by']} against CTCAE v{symptom['ctcae_version']}"
            reasoning += f": \"{criterion}\"." if criterion else "."
            reasoning += f" Onset {symptom['onset_date']}, {symptom['status']}."
            reasoning += _trend_sentence(trend_label, prev)
            if symptom.get("suspected_drug"):
                reasoning += f" Clinician-suspected drug: {symptom['suspected_drug']}."
            reasoning += _association_sentence(associated, exposure_snapshot, label_evidence, assoc_source)
            ctcae_items.append({"term": term, "grade": symptom["grade"], "source": "clinician", "status": symptom["status"],
                                "evidence": symptom.get("note") or "", "trend": trend_label,
                                "previous_grade": prev[0] if prev else 0, "associated_drugs": associated,
                                "label_evidence": label_evidence, "association_source": assoc_source, "criteria_source": term_provenance(term),
                                "suspected_drug": symptom.get("suspected_drug"), "reasoning": reasoning,
                                "symptom_id": symptom["id"], "confirmation": f"Confirmed by {symptom['confirmed_by']}"})

        # Terms seen before but absent now are reported as not re-recorded (only for windows with any data).
        has_assessment = bool(verified or symptom_rows)
        seen_terms = {item["term"] for item in ctcae_items}
        for term, (grade, number) in list(last_grade.items()):
            if term not in seen_terms and has_assessment and CTCAE_TERMS.get(term, {}).get("kind") == "lab":
                lab_test = CTCAE_TERMS[term].get("lab")
                if lab_test in latest_by_test:
                    ctcae_items.append({"term": term, "grade": 0, "source": "lab", "status": "resolved", "evidence": "",
                                        "trend": "resolved", "previous_grade": grade, "associated_drugs": [],
                                        "reasoning": f"{term}: Grade {grade} before C{number}, now within grade 0 range.",
                                        "confirmation": "Computed from verified lab value"})

        ctcae_items.sort(key=lambda item: (-item["grade"], item["term"]))
        for item in ctcae_items:
            if item["grade"]:
                last_grade[item["term"]] = (item["grade"], cycle["cycle_number"])
            elif item["term"] in last_grade:
                del last_grade[item["term"]]
            entry = matrix.setdefault(item["term"], {"term": item["term"], "kind": CTCAE_TERMS.get(item["term"], {}).get("kind", "clinical"),
                                                      "associated_drugs": set(), "grades": {}})
            entry["grades"][str(cycle["cycle_number"])] = max(item["grade"], entry["grades"].get(str(cycle["cycle_number"]), 0))
            entry["associated_drugs"].update(item["associated_drugs"])

        # Attention items for this window.
        attention = []
        is_pending = cycle["status"] in ("planned", "delayed")
        if lab_status == "missing":
            attention.append({"level": "warning", "kind": "labs_missing",
                              "text": "No pre-cycle labs recorded" + (" yet" if is_pending else " before this cycle was given")})
        if unverified:
            attention.append({"level": "warning", "kind": "labs_unverified", "text": f"{len(unverified)} extracted lab value(s) awaiting verification"})
        for item in ctcae_items:
            if item["grade"] >= 3:
                attention.append({"level": "alert", "kind": "ctcae", "text": f"{item['term']} Grade {item['grade']}"})
            elif item["trend"] == "worsening":
                attention.append({"level": "alert", "kind": "ctcae", "text": f"{item['term']} worsened to Grade {item['grade']}"})
            elif item["grade"] >= 2 and item["trend"] == "new":
                attention.append({"level": "alert", "kind": "ctcae", "text": f"New {item['term']} Grade {item['grade']}"})
        for row in lab_rows:
            trend, point = trend_points.get((row["test_name"], cycle["id"]), (None, None))
            flags = [f for f in (point or {}).get("flags", []) if f["code"] != "ctcae_rise"]  # grade rises are CTCAE items above
            row["flags"] = (point or {}).get("flags", [])
            if flags:
                top = flags[0]
                attention.append({"level": top["severity"], "kind": "lab_flag", "code": top["code"], "test_name": row["test_name"],
                                  "direction": trend["overall_direction"] if trend else None, "text": top["title"],
                                  "detail": " · ".join(f["title"] for f in flags[1:]) or top["detail"], "flag_count": len(flags)})
        attention.sort(key=lambda a: {"alert": 0, "warning": 1, "info": 2}.get(a["level"], 3))

        planned_total = sum(1 for m in meds)
        changed = [m for m in meds if m["status"] in ("modified", "held")]
        out_cycles.append({
            **{k: cycle[k] for k in ("id", "cycle_number", "planned_date", "administered_date", "status",
                                      "modification_reason", "reason_category", "recorded_by", "recorded_at")},
            "medications": meds,
            "window": {"from": _cycle_date(previous_cycle) if previous_cycle else None, "to": _cycle_date(cycle)},
            "labs_status": lab_status,
            "pending_labs": sorted(unverified, key=lambda l: (l["collection_date"], l["id"])),
            "lab_counts": {"verified": len(verified), "unverified": len(unverified)},
            "documents": [d for d in documents if d["cycle_id"] == cycle["id"]],
            "labs": sorted(lab_rows, key=lambda r: list(LAB_CATALOG).index(r["test_name"]) if r["test_name"] in LAB_CATALOG else 99),
            "symptoms": symptom_rows,
            "ctcae": ctcae_items,
            "worst_grade": max((item["grade"] for item in ctcae_items), default=0),
            "attention": attention,
            "decisions": decisions.get(cycle["id"], []),
            "exposure_before": exposure_snapshot,
            "dose_summary": {"drugs": planned_total, "changed": len(changed)},
        })
        if cycle["status"] in GIVEN_STATUSES:
            for med in meds:
                if med["status"] in ("given", "modified") and med["actual_dose"] is not None:
                    exposure[med["drug_name"]] = exposure.get(med["drug_name"], 0) + med["actual_dose"]
                    exposure_units[med["drug_name"]] = med["dose_unit"]
        if next_cycle_id is None and cycle["status"] in ("planned", "delayed"):
            next_cycle_id = cycle["id"]
        previous_cycle = cycle

    matrix_rows = []
    for entry in matrix.values():
        if not any(entry["grades"].values()):
            continue
        entry["associated_drugs"] = sorted(entry["associated_drugs"])
        entry["max_grade"] = max(entry["grades"].values())
        matrix_rows.append(entry)
    matrix_rows.sort(key=lambda e: (-e["max_grade"], e["kind"], e["term"]))

    given = [c for c in out_cycles if c["status"] in GIVEN_STATUSES]
    return {
        "plan": plan,
        "medications": plan_medications,
        "cycles": out_cycles,
        "lab_trends": trends,
        "ctcae_matrix": matrix_rows,
        "next_cycle_id": next_cycle_id,
        "summary": {
            "planned_cycles": len(out_cycles),
            "given_cycles": len(given),
            "modified_cycles": sum(1 for c in out_cycles if c["status"] == "modified"),
            "delayed_or_held": sum(1 for c in out_cycles if c["status"] in ("delayed", "held")),
            "cycles_missing_labs": sum(1 for c in out_cycles if c["labs_status"] == "missing"),
            "cumulative_exposure": [{"drug_name": d, "cumulative_dose": round(v, 2), "dose_unit": exposure_units[d]} for d, v in exposure.items()],
        },
        "meta": {"ctcae_version": CTCAE_VERSION, "clinical_recommendation": False,
                 "note": "Deterministic attention aid. Lab grades are computed from verified values; symptom grades are clinician-confirmed."},
    }


def _toxicity_trend(grade: int, previous: tuple[int, int] | None) -> str:
    if previous is None:
        return "new"
    if grade > previous[0]:
        return "worsening"
    if grade < previous[0]:
        return "improving"
    return "stable"


def _trend_sentence(trend: str, previous: tuple[int, int] | None) -> str:
    if trend == "new" or previous is None:
        return " First graded occurrence in this plan."
    return f" Grade {previous[0]} before C{previous[1]} → {trend}."


def _associate(term: str, drugs: list[str], labels: dict[str, dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]], str]:
    """Which regimen drugs are linked to this term: DailyMed label text when cached, else curated shortlist."""
    associated: list[str] = []
    evidence: list[dict[str, Any]] = []
    sources = set()
    for drug in drugs:
        label = labels.get(drug)
        if label:
            sources.add("DailyMed")
            hit = label.get("terms", {}).get(term)
            if hit:
                associated.append(drug)
                for snip in hit["snippets"][:1]:
                    evidence.append({"drug": drug, "section": snip["section"], "text": snip["text"], "boxed": hit.get("boxed", False),
                                     "title": label.get("title"), "url": label.get("url"), "published_date": label.get("published_date")})
        elif drug in drugs_associated_with(term, [drug]):
            sources.add("curated shortlist")
            associated.append(drug)
    source = " + ".join(sorted(sources)) if sources else ("DailyMed" if any(labels.get(d) for d in drugs) else "curated shortlist")
    return associated, evidence, source


def _association_sentence(drugs: list[str], exposure: list[dict[str, Any]], evidence: list[dict[str, Any]] | None = None,
                          source: str = "curated shortlist") -> str:
    if not drugs:
        return f" Not listed for the regimen drugs ({source}); attribution is a clinical judgement."
    parts = []
    for drug in drugs:
        exp = next((e for e in exposure if e["drug_name"] == drug), None)
        parts.append(f"{drug} (cumulative {fmt(exp['cumulative_dose'])} {exp['dose_unit']})" if exp else drug)
    sentence = " Listed for " + ", ".join(parts) + f" ({source})"
    for item in (evidence or [])[:2]:
        sentence += f"; DailyMed {item['drug']} label, {item['section']}{' (boxed)' if item.get('boxed') else ''}: “{item['text'][:220]}”"
    return sentence + ". Association is not causality."


def decision_evidence(analysis: dict[str, Any], cycle_id: int) -> str:
    """Snapshot of what the clinician saw when recording an outcome (stored with the decision)."""
    cycle = next((c for c in analysis["cycles"] if c["id"] == cycle_id), None)
    if not cycle:
        return json.dumps({})
    return json.dumps({
        "labs_status": cycle["labs_status"],
        "labs": [{k: r[k] for k in ("test_name", "value", "unit", "abnormal", "ctcae_grade", "pct_from_baseline")} for r in cycle["labs"]],
        "ctcae": [{k: i[k] for k in ("term", "grade", "source", "trend")} for i in cycle["ctcae"]],
        "attention": cycle["attention"],
    }, separators=(",", ":"))
